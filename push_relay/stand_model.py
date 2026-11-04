"""Persistent BER stand geometry and empirical prediction, owned by the Pi.

No network access. Only protected FR24 AIBT/GPS confirmations are learned;
administrator imports preserve the existing app's observations and weighting.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
from datetime import datetime
from zoneinfo import ZoneInfo

MAX_AGE = 730 * 86400
KINDS = {'stand','runway','stop_bar_cat_i','stop_bar_cat_ii_iii','holding','crossing','reference'}


def clean(value, limit=256):
    value = str(value or '').strip().upper()[:limit]
    return '' if value in ('-', '--', '—', 'NULL', 'NONE', 'N/A') else value


def distance(a, b):
    lat1, lat2 = math.radians(a['latitude']), math.radians(b['latitude'])
    dlon = math.radians(b['longitude']-a['longitude'])
    return 6371000*2*math.asin(min(1, math.sqrt(math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2)))


def number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def coordinate(row):
    return isinstance(row, dict) and number(row.get('latitude')) and number(row.get('longitude')) and 52.3 < row['latitude'] < 52.45 and 13.4 < row['longitude'] < 13.65


def bucket(when):
    return datetime.fromtimestamp(when, ZoneInfo('Europe/Berlin')).hour//2


class StandModel:
    def __init__(self, db, encrypt, decrypt):
        self.db, self.encrypt, self.decrypt = db, encrypt, decrypt
        db.execute('CREATE TABLE IF NOT EXISTS comat_stand_models (airport TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        row = db.execute("SELECT payload FROM comat_stand_models WHERE airport='BER'").fetchone()
        self.data = decrypt(row[0]) if row else {}
        self.data = self.data if isinstance(self.data, dict) else {}
        self.data.setdefault('positions', [])
        self.data.setdefault('observations', [])
        self.data.setdefault('flights', {})
        self.data.setdefault('revision', 0)
        # This lives beside relay.sqlite3 in /data, included in HA add-on backups.
        filename = db.execute('PRAGMA database_list').fetchone()[2]
        self.backup = Path(filename).with_suffix('.stands-backup') if filename else None
        if not self.data['positions'] and self.backup and self.backup.exists():
            restored = decrypt(self.backup.read_text())
            if isinstance(restored, dict) and restored.get('positions'):
                self.data = restored
        self.dirty = False

    @property
    def ready(self):
        return any(p['kind'] == 'stand' for p in self.data['positions'])

    def positions(self):
        return [p for p in self.data['positions'] if p['kind'] == 'stand']

    def metadata(self):
        return dict(airport='BER', ready=self.ready, revision=self.data['revision'],
                    observations=len(self.data['observations']), package_digest=self.data.get('package_digest',''),
                    imports=self.data.get('imports', {}))

    def save(self):
        if not self.dirty:
            return
        self.data['revision'] += 1
        payload = self.encrypt(self.data)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO comat_stand_models VALUES(?,?)', ('BER', payload))
        self.dirty = False
        if self.backup:
            temporary = self.backup.with_suffix('.stands-backup.tmp')
            fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(payload); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, self.backup)

    def upload(self, payload, uid, now):
        """Resumable bounded chunks. Only the verified administrator calls this."""
        if not isinstance(payload, dict) or payload.get('airport') != 'BER':
            raise ValueError('Unsupported stand airport')
        kind, digest = payload.get('kind'), payload.get('digest')
        rows, offset, total = payload.get('rows'), payload.get('offset'), payload.get('total')
        if kind not in ('package','observations') or not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
            raise ValueError('Invalid stand import')
        maximum = 2500 if kind == 'observations' else 2000
        if not isinstance(rows,list) or not 1 <= len(rows) <= 250 or type(offset) is not int or type(total) is not int or not 0 <= offset < total <= maximum or offset+len(rows) > total:
            raise ValueError('Invalid stand import chunk')
        imports = self.data.setdefault('imports', {})
        if imports.get(kind) == digest:
            return dict(offset=total, complete=True)
        key = uid+':'+kind
        stages = self.data.setdefault('staging', {})
        stage = stages.get(key)
        if not stage or stage['digest'] != digest:
            if offset != 0:
                return dict(offset=0, complete=False)
            stage = dict(digest=digest, total=total, rows=[], updated=now)
        if stage['total'] != total:
            raise ValueError('Import size changed')
        if offset != len(stage['rows']):
            return dict(offset=len(stage['rows']), complete=False)
        validated = [self.position(r) if kind == 'package' else self.observation(r, now) for r in rows]
        stage['rows'].extend(validated); stage['updated'] = now
        stages[key] = stage
        self.dirty = True
        complete = len(stage['rows']) == total
        if complete:
            if kind == 'package':
                if not any(p['kind']=='stand' for p in stage['rows']):
                    raise ValueError('Stand geometry missing')
                self.data['positions'] = stage['rows']
                self.data['package_digest'] = digest
            else:
                if not self.ready:
                    raise ValueError('Stand package missing')
                for observation in stage['rows']:
                    self.learn(observation, now)
            imports[kind] = digest
            del stages[key]
        self.save()
        return dict(offset=offset+len(rows), complete=complete)

    @staticmethod
    def position(row):
        if not coordinate(row) or row.get('kind','stand') not in KINDS:
            raise ValueError('Invalid reference position')
        name = clean(row.get('name'), 64)
        if not name or any(ord(c)<32 for c in name):
            raise ValueError('Invalid reference name')
        return dict(name=name, latitude=row['latitude'], longitude=row['longitude'], kind=row.get('kind','stand'))

    @staticmethod
    def observation(row, now):
        if not isinstance(row,dict) or not number(row.get('observedAt')) or type(row.get('timeBucket')) is not int or not 0 <= row['timeBucket'] < 12:
            raise ValueError('Invalid stand observation')
        result = {k:clean(row.get(k)) for k in ('id','stand','paxGate','exit','corridor','runway','airline','aircraft')}
        when = row['observedAt']
        if when > 1e12: when /= 1000  # Existing Swift vault uses milliseconds.
        if not 1e9 < when <= now+60 or not result['id'] or not result['stand']:
            raise ValueError('Invalid observation date or identity')
        result.update(observedAt=when, timeBucket=row['timeBucket'])
        return result

    def learn(self, observation, now):
        if observation['observedAt'] < now-MAX_AGE or observation['stand'] not in {p['name'] for p in self.positions()}:
            return
        # Legacy local IDs differed between phones. The same exact observed
        # arrival/features forms a second identity; FR24 identity survives later
        # gate corrections. Never aggregate independently learned weights.
        def fingerprint(o):
            return tuple(o[k] for k in ('observedAt','stand','airline','aircraft','paxGate'))
        for prior in self.data['observations']:
            if prior['id'] == observation['id'] or fingerprint(prior) == fingerprint(observation):
                return
        self.data['observations'].append(observation)
        self.data['observations'] = sorted((o for o in self.data['observations'] if o['observedAt'] >= now-MAX_AGE), key=lambda o:o['observedAt'])[-2500:]
        self.dirty = True

    def features(self, points):
        runway, corridor = '', []
        references = [p for p in self.data['positions'] if p['kind'] != 'stand']
        for point in sorted(points, key=lambda p:p['timestamp']):
            ranked = sorted(((distance(point,p),p) for p in references), key=lambda pair:pair[0])
            if not runway:
                runway = next((p['name'] for d,p in ranked if p['kind']=='runway' and d<=900),'')
            label = next((p['kind']+'/'+p['name'] for d,p in ranked if p['kind']!='runway' and d<=100),'')
            if label and (not corridor or corridor[-1]!=label): corridor.append(label)
        return clean(runway), clean('>'.join(corridor[-6:]))

    @staticmethod
    def flight_key(flight):
        # Scheduled occurrence remains stable when FR24 ID is discovered later.
        day = datetime.fromtimestamp(flight['reference'], ZoneInfo('Europe/Berlin')).date().isoformat()
        return flight['number']+':'+day+':'+flight.get('origin','')+':'+flight.get('destination','')

    def apply(self, flight, now, match):
        if not self.ready:
            return None
        if flight.get('destination') != 'BER' or flight.get('origin') == 'BER':
            return dict(candidates=[], evidence=0, issued_at=now)
        fallback = self.flight_key(flight)
        identifier = str(flight.get('fr24ID') or '').upper()
        key = 'FR24:'+identifier if identifier and not identifier.startswith(('TEMP_','NS_')) else fallback
        if key != fallback and key not in self.data['flights'] and fallback in self.data['flights']:
            self.data['flights'][key] = self.data['flights'].pop(fallback)
            self.dirty = True
        state = self.data['flights'].setdefault(key, dict(points=[], updated=now))
        before = json.dumps(state, sort_keys=True)
        checked = flight.get('_checked_at',0)
        if state.get('source_checked_at',0) > checked and state.get('prediction') and not flight.get('aibt'):
            return state['prediction']
        state['source_checked_at'] = checked
        # Only samples already fetched by the flight worker, no extra requests.
        live = flight.get('_live') or {}
        point = dict(latitude=live.get('lat'), longitude=live.get('lon'), timestamp=live.get('timestamp'))
        ata, aibt = flight.get('ata'), flight.get('aibt')
        if ata and coordinate(point) and number(point['timestamp']) and ata <= point['timestamp'] <= (aibt or now)+60:
            if not any(p['timestamp']==point['timestamp'] for p in state['points']):
                state['points'] = sorted(state['points']+[point],key=lambda p:p['timestamp'])[-120:]
        runway, corridor = self.features(state['points'])
        schedule = flight.get('_schedule') or {}
        when = ata or aibt or schedule.get('eta') or schedule.get('sta') or flight['reference']
        context = dict(id='FR24_ID:'+str(flight.get('fr24ID','')), stand='', paxGate=clean(flight.get('gate')), exit=corridor.split('>')[0], corridor=corridor, runway=runway, timeBucket=bucket(when), airline=clean(flight['number'])[:2], aircraft=clean(live.get('type') or schedule.get('aircraftType')), observedAt=when)
        position = flight.get('aibt_position') or {}
        # This evidence comes from the provider, never from a client forecast.
        if ata and aibt and ata <= aibt <= now and coordinate(position) and number(position.get('timestamp')) and ata <= position['timestamp'] and abs(position['timestamp']-aibt)<=60 and flight.get('fr24ID'):
            actual = match(position, self.positions())
            if actual:
                context['stand'] = actual
                self.learn(context, now)
        prediction = self.predict(context, now) if not (aibt or flight.get('cancelled') or flight.get('diverted')) else dict(candidates=[],evidence=0)
        previous = state.get('prediction', {})
        prediction['issued_at'] = previous.get('issued_at',now) if previous.get('candidates') == prediction['candidates'] else now
        if prediction != previous or json.dumps(state, sort_keys=True)!=before:
            state.update(prediction=prediction, updated=now)
            self.dirty = True
        self.data['flights'] = {k:v for k,v in self.data['flights'].items() if v['updated'] >= now-3*86400}
        self.save()
        return prediction

    def predict(self, context, now):
        scores, evidence = {}, 0
        allowed = {p['name'] for p in self.positions()}
        for o in self.data['observations']:
            if o['stand'] not in allowed or o['observedAt'] < now-MAX_AGE: continue
            weight, strong = 0, False
            for key, amount in (('paxGate',8), ('exit',7)):
                if context[key] and context[key] == o[key]: weight += amount; strong = True
            prefix = 0
            if context['corridor'] and o['corridor']:
                for a,b in zip(context['corridor'].split('>'), o['corridor'].split('>')):
                    if a != b: break
                    prefix += 1
            if prefix: weight += 2*min(3,prefix); strong = True
            if not strong: continue
            if context['runway'] and context['runway'] == o['runway']: weight += 1.5
            delta = abs(context['timeBucket']-o['timeBucket']); delta = min(delta,12-delta)
            weight += 3 if delta == 0 else 1.5 if delta == 1 else 0
            for key in ('airline','aircraft'):
                if context[key] and context[key] == o[key]: weight += 1
            scores[o['stand']] = scores.get(o['stand'],0) + weight*max(math.exp(-max(0,now-o['observedAt'])/(270*86400)),.35)
            evidence += 1
        total = sum(scores.values())
        if not total: return dict(candidates=[],evidence=0)
        reliability = min(1-math.exp(-evidence/3),.96)
        candidates = sorted((dict(stand=k,percent=math.floor(v/total*reliability*100+.5)) for k,v in scores.items()),key=lambda x:(-x['percent'],x['stand']))[:3]
        shown = [c for c in candidates[:2] if c['percent']>=5]
        labels = [('erwartet ' if i==0 and c['percent']>=55 else '')+f"{c['stand']} {c['percent']} %" for i,c in enumerate(shown)]
        unresolved = max(0,100-sum(c['percent'] for c in shown))
        if unresolved>=5: labels.append(f'offen {unresolved} %')
        return dict(candidates=candidates,evidence=evidence,text='Standprognose '+' · '.join(labels))
