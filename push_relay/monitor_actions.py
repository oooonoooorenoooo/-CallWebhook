"""Administrator-only Pi controls and a shared, bounded BER arrival cache."""
import asyncio
import hashlib
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from aiohttp import ClientSession, ClientTimeout, web
from .flight_schedule import ber_schedule, timestamp

BERLIN = ZoneInfo('Europe/Berlin')
AIRLINES = dict(A3='Aegean Airlines', AC='Air Canada', CA='Air China', AI='Air India',
    NZ='Air New Zealand', NH='ANA', OZ='Asiana Airlines', OS='Austrian Airlines', AV='Avianca',
    SN='Brussels Airlines', CM='Copa Airlines', OU='Croatia Airlines', MS='EgyptAir', ET='Ethiopian Airlines',
    BR='EVA Air', AZ='ITA Airways', LO='LOT', LH='Lufthansa', VL='Lufthansa City Airlines',
    ZH='Shenzhen Airlines', SQ='Singapore Airlines', SA='South African Airways', LX='SWISS',
    TP='TAP Air Portugal', TG='Thai Airways', TK='Turkish Airlines', UA='United Airlines', EW='Eurowings')


def day_of(reference):
    return datetime.fromtimestamp(reference, BERLIN).date().isoformat()


def identity(f):
    return json.dumps([f['number'], f.get('day') or day_of(f['reference']), f.get('origin',''), f.get('destination','')])


def key_of(f):
    return hashlib.sha256(identity(f).encode()).hexdigest()


def normalized(value):
    value = re.sub(r'\s', '', value).upper() if isinstance(value,str) else ''
    return value if re.fullmatch(r'[A-Z0-9]{2,3}[0-9]{1,5}', value) else ''


def parse_arrivals(rows, day):
    result = {}
    for r in rows:
        if not isinstance(r,dict) or r.get('is_departure') is True:
            continue
        status = str(r.get('status',''))+' '+str(r.get('status_text',''))
        if any(s in status.lower() for s in ('cancel','annull','gestrichen','landed','gelandet','arrived','angekommen')) or timestamp(r.get('arr_actual_time'),'Europe/Berlin'):
            continue
        number = normalized(r.get('operating_flight_number') or r.get('flight_number'))
        parts = re.fullmatch(r'([A-Z0-9]{2}|[A-Z]{3})([0-9]{1,5})',number)
        carrier = str(r.get('operating_airline_iata') or (parts[1] if parts else '')).strip().upper()
        if carrier not in AIRLINES or (carrier=='OZ' and day>='2026-12-17'):
            continue
        s = ber_schedule(r, False)
        if not s.get('sta') or day_of(s['sta']) != day or s.get('destination') not in (None,'','BER'):
            continue
        aliases = {number, normalized(r.get('flight_number'))}
        aliases.update(normalized(x) for x in r.get('code_shares',[]) if isinstance(x,str))
        f = dict(number=number, reference=s['sta'], day=day, origin=s.get('origin',''), destination='BER',
                 airline=AIRLINES[carrier], carrier=carrier, origin_name=str(r.get('dep_airport_name') or s.get('origin',''))[:120],
                 schedule=s, aliases=sorted(aliases-{''}))
        f['key'] = key_of(f)
        if f['key'] in result:
            f['aliases'] = sorted(set(f['aliases']+result[f['key']]['aliases']))
        result[f['key']] = f
    return sorted(result.values(),key=lambda f:f['schedule'].get('eta',f['reference']))


class Operator:
    def __init__(self, monitor):
        self.monitor, self.comat, self.db = monitor, monitor.comat, monitor.db
        self.task = None
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS comat_removed (identity TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comat_manual (identity TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS comat_arrivals (id INTEGER PRIMARY KEY, day TEXT NOT NULL,
                fetched REAL NOT NULL, attempted REAL NOT NULL, payload TEXT NOT NULL, error TEXT NOT NULL);
        ''')

    def removed(self, f):
        from .comatalarm import same_occurrence
        return any(same_occurrence(f,json.loads(r[0])) for r in self.db.execute('SELECT payload FROM comat_removed'))

    def merge(self, flights, prior, now):
        from .comatalarm import same_occurrence
        with self.db:
            self.db.execute('DELETE FROM comat_manual WHERE expires<=?',(now,))
            for r in self.db.execute('SELECT identity,payload FROM comat_removed').fetchall():
                saved = json.loads(r['payload'])
                if day_of(saved['reference'])[:7]!=day_of(now)[:7] and saved['reference']<now-3*86400:
                    self.db.execute('DELETE FROM comat_removed WHERE identity=?',(r['identity'],))
        flights = [f for f in flights if not self.removed(f)]
        for r in self.db.execute('SELECT payload FROM comat_manual WHERE expires>?',(now,)):
            manual = json.loads(r[0])
            if self.removed(manual) or any(same_occurrence(f,manual) for f in flights):
                continue
            flights.append(next((f for f in prior if same_occurrence(f,manual)),manual))
        return flights

    def board(self, flights, now):
        row = self.db.execute('SELECT * FROM comat_arrivals WHERE id=1').fetchone()
        day = day_of(now)
        current = row is not None and row['day']==day
        items = json.loads(row['payload']) if current else []
        for item in items:
            item['tracked'] = any(f['number'] in item['aliases'] and (f.get('day') or day_of(f['reference']))==day and
                (not f.get('origin') or f['origin']==item['origin']) and f.get('destination') in ('','BER',None) for f in flights)
            item['available'] = item['schedule'].get('eta',item['reference'])>=now
        return dict(day=day, fetched_at=row['fetched'] if current else 0,
                    loading=self.task is not None and not self.task.done(),
                    error=row['error'] if current else '', flights=[f for f in items if f['available'] or f['tracked']])

    def request_arrivals(self, force=False):
        now = time.time()
        row = self.db.execute('SELECT * FROM comat_arrivals WHERE id=1').fetchone()
        if self.task and not self.task.done(): return
        if row and row['day']==day_of(now) and (now-row['attempted']<300 or (row['fetched'] and not force)):
            return
        self.task = asyncio.create_task(self.refresh_arrivals(now))

    async def fetch_arrivals(self, now):
        from .comatalarm import ber_request, BER_FLIGHTS_URL, provider_http_status
        cooldown = self.comat.api_status.cooldown
        if cooldown.get('ber',0)>now:
            raise ValueError('BER-Abfrage pausiert · Anbieter-Sperrfrist')
        params, headers = ber_request('',now)
        rows, signatures = [], set()
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with asyncio.timeout(90):
                for page in range(1,101):
                    params['page'] = str(page)
                    async with session.get(BER_FLIGHTS_URL,params=params,headers=headers,allow_redirects=False) as response:
                        error = provider_http_status('BER',response,cooldown,'ber',time.time())
                        if error: raise ValueError('BER-Ankunftstabelle: '+error)
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw)>2_000_000: raise ValueError('BER-Antwort zu groß')
                        payload = json.loads(raw).get('data',{})
                        items = payload.get('items')
                        if not isinstance(items,list): raise ValueError('BER-Ankunftstabelle: ungültige Antwort')
                        signature = hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()
                        if not items: return parse_arrivals(rows,day_of(now))
                        if signature in signatures: raise ValueError('BER-Ankunftstabelle: wiederholte Seite; Liste nicht vollständig')
                        signatures.add(signature); rows.extend(items)
                        pagination = payload.get('pagination') or payload
                        pages = next((pagination.get(k) for k in ('total_pages','totalPages','page_count') if isinstance(pagination.get(k),int)),None)
                        if pages and page>=pages: return parse_arrivals(rows,day_of(now))
        raise ValueError('BER-Ankunftstabelle: Seitenlimit erreicht')

    async def refresh_arrivals(self, now):
        previous = self.db.execute('SELECT * FROM comat_arrivals WHERE id=1').fetchone()
        keep = previous is not None and previous['day']==day_of(now)
        fetched, payload = (previous['fetched'],previous['payload']) if keep else (0,'[]')
        error = ''
        # Persist attempts, so restarting the add-on cannot hammer a blocked provider.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO comat_arrivals VALUES(1,?,?,?,?,?)',(day_of(now),fetched,now,payload,''))
        try:
            payload = json.dumps(await self.fetch_arrivals(now))
            fetched = time.time()
        except asyncio.CancelledError:
            raise
        except ValueError as exc:
            # Only locally authored errors; never expose HTTP response text.
            error = str(exc) if str(exc).startswith(('BER-','BER-A')) else 'BER-Ankunftstabelle konnte nicht gelesen werden'
        except Exception:
            error = 'BER-Ankunftstabelle nicht erreichbar; später erneut versuchen'
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO comat_arrivals VALUES(1,?,?,?,?,?)',(day_of(now),fetched,now,payload,error))
        if error: self.monitor.record('error',error)

    async def command(self, body):
        action, key = body.get('action'), body.get('key')
        if action in ('arrivals','refresh_arrivals'):
            self.request_arrivals(force=action=='refresh_arrivals')
            return dict(ok=True)
        if action not in ('delete','track') or not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key):
            raise web.HTTPBadRequest(text='Ungültiger Monitor-Befehl')
        if action=='delete':
            async with self.comat.delivery_lock:
                return self.delete(key)
        return await self.track(key)

    def delete(self, key):
        flights = self.monitor.snapshot(with_history=False)['flights']
        flights += [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM comat_monitor_history')]
        f = next((f for f in flights if key_of(f)==key),None)
        if not f: return dict(ok=True, message='Flug bereits entfernt')
        from .comatalarm import same_occurrence
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO comat_removed VALUES(?,?)',(identity(f),json.dumps(f)))
            for row in self.db.execute('SELECT id,watch FROM comat_devices').fetchall():
                old = json.loads(row['watch'])
                removed = {x['id'] for x in old if same_occurrence(f,x)}
                self.db.execute('UPDATE comat_devices SET watch=? WHERE id=?',(json.dumps([x for x in old if x['id'] not in removed]),row['id']))
                for event in self.db.execute('SELECT event,payload FROM comat_outbox WHERE device=? AND sent=0',(row['id'],)).fetchall():
                    if json.loads(event['payload']).get('flight_id') in removed:
                        self.db.execute('DELETE FROM comat_outbox WHERE device=? AND event=?',(row['id'],event['event']))
            for table in ('comat_manual','comat_monitor_history'):
                for row in self.db.execute('SELECT identity,payload FROM '+table).fetchall():
                    if same_occurrence(f,json.loads(row['payload'])):
                        self.db.execute('DELETE FROM '+table+' WHERE identity=?',(row['identity'],))
        self.monitor.record('service','Flug auf dem Pi gelöscht; erneute Geräteübergabe für diesen Flugtag gesperrt',f['number'])
        return dict(ok=True,message=f['number']+' auf dem Pi entfernt')

    async def track(self, key):
        from .comatalarm import same_occurrence
        now = time.time()
        board = self.board([],now)
        f = next((f for f in board['flights'] if f['key']==key and f['available']),None)
        if not f or board['error'] or not board['fetched_at']:
            raise web.HTTPConflict(text='Ankunftstabelle aktualisieren; Flug nicht mehr verfügbar')
        candidates = self.db.execute("SELECT * FROM comat_devices WHERE expires>? AND api_token!='' AND server_polling=1",(now,)).fetchall()
        async def permitted(r):
            return r['id'] if not r['firebase_uid'] or await self.comat.stand_access(r['firebase_uid']) else None
        approved = await asyncio.gather(*(permitted(r) for r in candidates))
        # Re-read after approval I/O to respect revoked registrations / concurrent changes.
        devices = [r for r in self.db.execute("SELECT * FROM comat_devices WHERE expires>? AND api_token!='' AND server_polling=1",(time.time(),)) if r['id'] in approved]
        if not devices:
            raise web.HTTPConflict(text='Kein freigegebenes Gerät mit eingerichteter Pi-Abfrage verbunden')
        flight = dict(id='PI_'+key[:32],number=f['number'],reference=f['reference'],origin=f['origin'],destination='BER',_schedule=f['schedule'])
        already = any(same_occurrence(flight,x) or (x['number'] in f['aliases'] and day_of(x['reference'])==f['day']) for r in devices for x in json.loads(r['watch']))
        if already: return dict(ok=True,message=f['number']+' wird bereits überwacht')
        with self.db:
            for r in self.db.execute('SELECT identity,payload FROM comat_removed').fetchall():
                if same_occurrence(flight,json.loads(r['payload'])): self.db.execute('DELETE FROM comat_removed WHERE identity=?',(r['identity'],))
            self.db.execute('INSERT OR REPLACE INTO comat_manual VALUES(?,?,?)',(identity(flight),json.dumps(flight),now+86400))
            for r in devices:
                watch = json.loads(r['watch'])+[flight]
                self.db.execute('UPDATE comat_devices SET watch=?,expires=?,last_poll=0 WHERE id=?',(json.dumps(watch),now+86400,r['id']))
        self.monitor.record('service','Über Ankunftstabelle zur Pi-Überwachung hinzugefügt',f['number'])
        self.monitor.archive(now)
        return dict(ok=True,message=f['number']+' wird jetzt auf dem Pi überwacht')
