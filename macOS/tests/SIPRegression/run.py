import socket, subprocess, time, re, uuid, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
(ROOT/'work/sip-regression').mkdir(parents=True, exist_ok=True)
APP=ROOT/'work/ring-build/Build/Products/Debug/SIPRegression.app/Contents/MacOS/SIPRegression'

def headers(data):
    out={}
    for line in data.split('\r\n')[1:]:
        if ':' in line:
            key,value=line.split(':',1);out[key.lower()]=value.strip()
    return out

def run(reason, expected, decline=False, requires_view=True):
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',0));sock.settimeout(.1)
    port=sock.getsockname()[1]
    with open(ROOT/'work/sip-regression/runtime.log','w') as log:
        proc=subprocess.Popen([str(APP),str(port)] + (["decline"] if decline else []),stdout=log,stderr=log)
        sent=False; cancelled=False; invite_at=None;cancel_at=None; deadline=time.monotonic()+15
        while time.monotonic()<deadline and proc.poll() is None:
            try: raw,addr=sock.recvfrom(65535)
            except socket.timeout: continue
            msg=raw.decode(errors='replace');h=headers(msg)
            if msg.startswith('REGISTER'):
                response='SIP/2.0 200 OK\r\n'+''.join(f'{n}: {h[n.lower()]}\r\n' for n in ['Via','From','To','Call-ID','CSeq'])+f'Contact: {h["contact"]}\r\nExpires: 3600\r\nContent-Length: 0\r\n\r\n'
                sock.sendto(response.encode(),addr)
                if not sent:
                    sent=True;time.sleep(.05)
                    target=re.search(r'<([^>]+)>',h['contact']).group(1)
                    branch='z9hG4bK'+uuid.uuid4().hex;cid=uuid.uuid4().hex; cw=str(uuid.uuid4())
                    via=f'SIP/2.0/UDP 127.0.0.1:{port};branch={branch};rport'
                    from_=f'<sip:555123@127.0.0.1:{port}>;tag=peer'
                    to=f'<sip:callwebhook-mac-test@127.0.0.1:{port}>'
                    base=f'Via: {via}\r\nFrom: {from_}\r\nTo: {to}\r\nCall-ID: {cid}\r\n'
                    sdp='v=0\r\no=peer 1 1 IN IP4 127.0.0.1\r\ns=test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 25000 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n'
                    invite=f'INVITE {target} SIP/2.0\r\n{base}CSeq: 1 INVITE\r\nMax-Forwards: 70\r\nContact: <sip:peer@127.0.0.1:{port}>\r\nX-CallWebhook-Line: 3\r\nX-CallWebhook-ID: {cw}\r\nContent-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}'
                    invite_at=time.time();sock.sendto(invite.encode(),addr)
            elif msg.startswith('SIP/2.0 180') and not cancelled and not decline:
                cancelled=True;time.sleep(.15)
                reasonline=f'Reason: {reason}\r\n' if reason else ''
                cancel=f'CANCEL {target} SIP/2.0\r\n{base}CSeq: 1 CANCEL\r\nMax-Forwards: 70\r\n{reasonline}Content-Length: 0\r\n\r\n'
                cancel_at=time.time();sock.sendto(cancel.encode(),addr)
            elif msg.startswith('SIP/2.0 487'):
                ack=f'ACK {target} SIP/2.0\r\n{base}CSeq: 1 ACK\r\nMax-Forwards: 70\r\nContent-Length: 0\r\n\r\n'
                sock.sendto(ack.encode(),addr)
        if proc.poll() is None:proc.kill()
        proc.wait()
    sock.close()
    text=(ROOT/'work/sip-regression/runtime.log').read_text()
    match=re.search(r'RESULT (\{.*\})',text)
    if not match:
        print(text[-5000:]);raise AssertionError('No result')
    result=json.loads(match.group(1));result['reason']=reason;result['cancel_to_stop_ms']=round((result['stopped_at']-cancel_at)*1000,2) if cancel_at else None;result['invite_to_incoming_ms']=round((result['incoming_at']-invite_at)*1000,2)
    print(json.dumps(result),flush=True)
    assert result['status']==expected,(result,expected)
    assert result['ring_stopped']
    if requires_view:
        assert result['view_at'] > 0
        result['invite_to_view_ms'] = round((result['view_at']-invite_at)*1000,2)
    return result

if __name__ == "__main__":
    results=[]
    for reason,expected in [('Q.850;cause=26;text="Answered elsewhere"','answered_elsewhere'),('SIP;cause=200;text="Call completed elsewhere"','answered_elsewhere'),('', 'missed'),('Q.850;cause=16;text="Normal clearing"','missed'),('SIP;cause=487;text="Request terminated"','missed')]:
        results.append(run(reason,expected))
    results.append(run('', 'declined', decline=True))
    (ROOT/'work/sip-regression/results.json').write_text(json.dumps(results,indent=2))
