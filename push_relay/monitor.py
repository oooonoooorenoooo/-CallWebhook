"""Read-only operator diagnostics. No provider calls, credentials or raw logs."""
import hashlib
import hmac
import json
import logging
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from aiohttp import web

FLIGHT_KEYS = ('number','reference','fr24ID','origin','destination','registration','atd','ata','aibt','gate','gate_source','stand','cancelled','diverted','packets')
LIVE_KEYS = ('lat','lon','alt','gspeed','track','vspeed','on_ground','timestamp','type','hex','reg','eta','sta')
SCHEDULE_KEYS = ('std','etd','atd','sta','eta','ata','aibt','eibt','gate','gate_source','registration','aircraftType','cancelled','diverted')


def select(data, keys):
    return {k:data[k] for k in keys if k in data and isinstance(data[k], (str,int,float,bool,type(None)))} if isinstance(data,dict) else {}


def packet_counts(value):
    parts = str(value or '').split('/')
    return {key:int(v) for key,v in zip(('in','out'),parts) if v.strip().isdigit() and len(v.strip())<=18}


def device_label(value):
    return 'Gerät ' + hashlib.sha256(value.encode()).hexdigest()[:6]


class Monitor:
    def __init__(self, comat):
        self.comat, self.db = comat, comat.db
        self.started = time.time()
        self.cycle_started = self.cycle_finished = 0
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS comat_monitor_events (
                id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, kind TEXT NOT NULL,
                flight TEXT NOT NULL, device TEXT NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comat_monitor_reads (device TEXT PRIMARY KEY, read_at REAL NOT NULL);
        ''')
        self.handler = SafeLogHandler(self)

    def record(self, kind, message, flight='', device='', now=None):
        # Callers pass locally authored messages only; never provider bodies.
        with self.db:
            self.db.execute('INSERT INTO comat_monitor_events(timestamp,kind,flight,device,message) VALUES(?,?,?,?,?)',
                (time.time() if now is None else now, kind, flight[:32], device_label(device) if device else '', message[:400]))
            self.db.execute('DELETE FROM comat_monitor_events WHERE id NOT IN (SELECT id FROM comat_monitor_events ORDER BY id DESC LIMIT 1000)')

    async def get(self, request):
        key = os.environ.get('COMATALARM_SETUP_KEY','')
        supplied = request.headers.get('X-ComatAlarm-Setup-Key','')
        if len(key) < 32 or not hmac.compare_digest(key, supplied):
            raise web.HTTPUnauthorized()
        return web.json_response(self.snapshot(), headers={'Cache-Control':'no-store'})

    def snapshot(self, now=None):
        from .comatalarm import departure_from_ber, previous_parking_ready, arrival_tracking_expired
        now = time.time() if now is None else now
        devices, groups = [], {}
        for row in self.db.execute('SELECT * FROM comat_devices').fetchall():
            last_read = self.db.execute('SELECT read_at FROM comat_monitor_reads WHERE device=?',(row['id'],)).fetchone()
            devices.append(dict(name=device_label(row['id']), expires=row['expires'], last_poll=row['last_poll'],
                last_app_read=last_read[0] if last_read else None, server_polling=bool(row['server_polling']),
                token_present=bool(row['api_token']), error=bool(row['error']), expired=row['expires']<=now))
            for f in json.loads(row['watch']):
                day = datetime.fromtimestamp(f['reference'],ZoneInfo('Europe/Berlin')).date().isoformat()
                key = (f['number'],day,f.get('origin',''),f.get('destination',''))
                status = 'Überwacht'
                if row['expires'] <= now: status = 'Registrierung abgelaufen'
                elif row['error']: status = 'Abfrage gestört'
                elif not row['api_token']: status = 'API-Zugang fehlt'
                elif not row['server_polling'] and row['foreground_until'] > now: status = 'App übernimmt Abfrage'
                elif f.get('cancelled'): status = 'Annulliert'
                elif f.get('diverted'): status = 'Umgeleitet'
                elif arrival_tracking_expired(f,now): status = 'Nachlauf beendet'
                elif departure_from_ber(f) and f.get('atd') and f['atd'] <= now:
                    status = 'Abflug abgeschlossen' if previous_parking_ready(f) else 'Vorflug-Reparatur / Wartefrist'
                elif f.get('aibt') and not f.get('repair_arrival') and not (f.get('track_outbound') and not f.get('_outbound',{}).get('atd')):
                    status = 'Am Stand abgeschlossen'
                reader = dict(device=device_label(row['id']), status=status, last_poll=row['last_poll'], last_app_read=last_read[0] if last_read else None)
                score = (row['expires']>now, f.get('_checked_at',0) or row['last_poll'])
                existing = groups.get(key)
                if existing and existing['_score'] > score:
                    existing['subscribers'].append(reader)
                    continue
                result = select(f, FLIGHT_KEYS)
                result.update(day=day, live=select(f.get('_live'),LIVE_KEYS), schedule=select(f.get('_schedule'),SCHEDULE_KEYS),
                    prediction=select(f.get('_stand_prediction'),('text','issued_at')), checked_at=f.get('_checked_at'),
                    inbound=select(f.get('_inbound'),FLIGHT_KEYS), outbound=select(f.get('_outbound'),('number','destination','atd')),
                    packets=packet_counts(f.get('packets')),
                    subscribers=(existing['subscribers'] if existing else [])+[reader], _score=score)
                live = result['live']
                ts = live.get('timestamp')
                result['position_age'] = max(0,now-ts) if isinstance(ts,(float,int)) else None
                result['position_stale'] = result['position_age'] is None or result['position_age']>120 or ts>now+10
                if f.get('destination') == 'BER' and all(isinstance(live.get(k),(int,float)) for k in ('lat','lon')):
                    lat, lon = math.radians(live['lat']), math.radians(live['lon'])
                    dlat, dlon = math.radians(52.3667)-lat, math.radians(13.5033)-lon
                    h = math.sin(dlat/2)**2+math.cos(lat)*math.cos(math.radians(52.3667))*math.sin(dlon/2)**2
                    result['distance_km'] = round(6371*2*math.asin(min(1,math.sqrt(max(0,h)))),1)
                groups[key] = result
        flights = sorted(groups.values(),key=lambda f:(f['day'],f['number']))
        for f in flights: f.pop('_score',None)
        events = [dict(r) for r in self.db.execute('SELECT timestamp,kind,flight,device,message FROM comat_monitor_events ORDER BY id DESC LIMIT 150')]
        outbox = self.db.execute('SELECT SUM(sent=0 AND created>?),SUM(sent=1),SUM(sent=0 AND created<=?) FROM comat_outbox',(now-900,now-900)).fetchone()
        meta = self.comat.stand_model.metadata()
        return dict(schema=1, generated_at=now, started_at=self.started, cycle_started=self.cycle_started, cycle_finished=self.cycle_finished,
            api_status=self.comat.api_status.snapshot(), flights=flights, devices=devices, events=events,
            pushes=dict(pending=outbox[0] or 0, accepted=outbox[1] or 0, expired=outbox[2] or 0),
            stand_model=select(meta,('ready','observations','revision')), package_status=self.comat.ber_importer.status)


class SafeLogHandler(logging.Handler):
    def __init__(self, monitor):
        super().__init__(logging.WARNING)
        self.monitor = monitor

    def emit(self, record):
        # Exact message templates only. Unknown logs and exception text are not exported.
        template = str(record.msg)
        message = None
        if template.startswith('ComatAlarm provider=%s HTTP=%s') and isinstance(record.args,tuple):
            name, status = record.args[:2]
            if name in ('BER','FR24','FlightAware','FlightStats','ADSB.lol','OpenSky') and isinstance(status,int):
                message = f'{name}: HTTP {status}; Anbieterabfrage fehlgeschlagen'
        elif template.startswith('ComatAlarm approval_check_failed'):
            message = 'Firebase-Gerätefreigabe konnte nicht geprüft werden'
        elif template.startswith('ComatAlarm monitor_cycle_failed'):
            message = 'Abfragezyklus abgebrochen; erneuter Versuch nach 15 Sekunden'
        elif template.startswith('ComatAlarm flight_poll_failed'):
            message = 'Flugabfrage fehlgeschlagen'
        elif template.startswith('ComatAlarm monitor_skip reason=device_approval'):
            message = 'Überwachung pausiert: Gerätefreigabe nicht bestätigt'
        elif template == 'ComatAlarm: %s':
            message = 'Standplatzpaket: ' + ('Entschlüsselung fehlgeschlagen' if 'entschlüsselt' in str(record.args) else 'Importmeldung; Add-on-Protokoll prüfen')
        if message:
            try:
                self.monitor.record('error',message,now=record.created)
            except Exception:
                pass  # Diagnostics must never interrupt polling or APNs.
