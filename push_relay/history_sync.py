"""Approved shared-flight history, independently retained from active watches.

Only authenticated polling/handoffs and the Pi operator can capture records.
The administrative dashboard projection is never a general sharing source.
"""
import hashlib
import json
import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from aiohttp import web

BERLIN = ZoneInfo('Europe/Berlin')
PREFIX = 'PI_HISTORY_'
SCALARS = ('number', 'reference', 'created_at', 'fr24ID', 'origin', 'destination',
           'registration', 'atd', 'ata', 'aibt', 'gate', 'gate_source', 'stand',
           'cancelled', 'diverted', 'packets', 'track_outbound', 'outbound_number',
           '_aibt_recovery_attempts', '_aibt_recovery_at', '_aibt_recovery_retry_at',
           '_aibt_evidence_revision', '_parking_lookup_status')
SCHEDULE = ('std', 'etd', 'atd', 'sta', 'eta', 'ata', 'aibt', 'eibt', 'gate',
            'gate_source', 'registration', 'aircraftType', 'origin', 'destination',
            'cancelled', 'diverted')
LEG = ('id', 'number', 'registration', 'origin', 'destination', 'atd', 'ata', 'aibt')


def day_of(reference):
    return datetime.fromtimestamp(reference, BERLIN).date().isoformat()


def scalar_fields(value, fields):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in fields:
        v = value.get(key)
        if isinstance(v, str) and 0 < len(v) <= 160:
            result[key] = v
        elif isinstance(v, bool):
            result[key] = v
        elif isinstance(v, (int, float)) and math.isfinite(v):
            result[key] = v
    return result


def actual_identifier(flight):
    value = flight.get('fr24ID')
    return value.upper() if isinstance(value, str) and value and not value.upper().startswith(('TEMP_', 'NS_')) else None


def history_time(flight):
    """Match the app's history date; occurrence reference remains unchanged."""
    from .comatalarm import departure_from_ber
    if departure_from_ber(flight):
        return flight.get('atd') or flight.get('_terminal_observed_at') or flight['reference']
    return flight.get('aibt') or flight.get('ata') or flight.get('_terminal_observed_at') or flight['reference']


class HistorySync:
    def __init__(self, comat):
        self.comat, self.db = comat, comat.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS comat_app_history (
                identity TEXT PRIMARY KEY, day TEXT NOT NULL,
                saved_at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS comat_app_history_day ON comat_app_history(day);
        ''')

    def prune(self, now):
        with self.db:
            self.db.execute('DELETE FROM comat_app_history WHERE substr(day,1,7)<>?', (day_of(now)[:7],))

    def removed(self, flight):
        # An inferred legacy creation date never proves a later explicit re-add.
        evidence = dict(flight, created_at=flight.get('created_at', 0) if flight.get('_created_at_proven') else 0)
        return self.comat.monitor.operator.removed(evidence) or self.comat.shared_monitoring.removed(evidence)

    @staticmethod
    def sanitize(flight, now, aliases=(), created_known=None):
        from .comatalarm import stamp, valid_coordinate
        number, reference = flight.get('number'), flight.get('reference')
        if (not isinstance(number, str) or not re.fullmatch(r'[A-Z0-9]{2,3}[0-9]{1,5}', number)
                or not isinstance(reference, (int, float)) or isinstance(reference, bool)
                or not math.isfinite(reference) or reference <= 1_000_000_000):
            raise ValueError('Invalid history occurrence')
        result = scalar_fields(flight, SCALARS)
        for key in ('atd', 'ata', 'aibt'):
            value = stamp(flight.get(key))
            if value is not None and value <= now+60:
                result[key] = value
            else:
                result.pop(key, None)
        if (result.get('atd') and result.get('ata') and result['ata'] < result['atd']) or (
                result.get('ata') and result.get('aibt') and result['aibt'] < result['ata']-60):
            raise ValueError('Invalid history event order')
        for key in ('cancelled', 'diverted', 'track_outbound'):
            if key in result and not isinstance(result[key], bool):
                result.pop(key)
        created = flight.get('created_at')
        proven = (isinstance(created, (int, float)) and not isinstance(created, bool)
                  and math.isfinite(created) and 1_000_000_000 < created <= now+60)
        result['created_at'] = created if proven else reference
        result['_created_at_proven'] = proven and created_known is not False
        identifiers = list(aliases) + [flight.get('id')]
        result['entry_aliases'] = sorted({v for v in identifiers if isinstance(v, str) and 0 < len(v) <= 160})[:128]
        schedule = scalar_fields(flight.get('_schedule'), SCHEDULE)
        if schedule:
            result['_schedule'] = schedule
        recovery = scalar_fields(flight.get('_parking_recovery'),
                                 ('state', 'last_attempt', 'next_attempt', 'attempts', 'evidence_revision', 'error'))
        if recovery:
            recovery.setdefault('next_attempt', None)
            result['_parking_recovery'] = recovery

        def position(value):
            if not isinstance(value, dict):
                return None
            if valid_coordinate(value.get('latitude'), value.get('longitude')) and stamp(value.get('timestamp')):
                point = {k: value[k] for k in ('latitude', 'longitude', 'timestamp')}
                if value.get('source') in ('fr24_event', 'fr24_gate_reference', 'fr24_historic', 'fr24_track'):
                    point['source'] = value['source']
                return point
            return None

        point = position(flight.get('aibt_position'))
        if point:
            result['aibt_position'] = point
        for key in ('_inbound', '_outbound'):
            leg = scalar_fields(flight.get(key), LEG)
            point = position((flight.get(key) or {}).get('aibt_position')) if isinstance(flight.get(key), dict) else None
            if point:
                leg['aibt_position'] = point
            if leg or isinstance(flight.get(key), dict):
                result[key] = leg
        return result

    @staticmethod
    def merge(old, incoming):
        """Missing facts in a later handoff cannot undo confirmed completion."""
        from .comatalarm import registration_key, departure_from_ber
        result = dict(old)
        changed_inbound = False
        for key, value in incoming.items():
            if key in ('entry_aliases', 'created_at', '_created_at_proven', 'number', 'reference'):
                continue
            if key == '_schedule':
                result[key] = dict(old.get(key) or {}, **value)
            elif key in ('_inbound', '_outbound'):
                previous = old.get(key) or {}
                identified = bool(previous.get('id') and value.get('id'))
                different_id = identified and str(previous['id']).upper() != str(value['id']).upper()
                new_leg = (not value or different_id or (not identified and any(previous.get(k) and value.get(k) and
                           str(previous[k]).upper() != str(value[k]).upper() for k in ('number', 'origin', 'destination')))
                           or (registration_key(previous.get('registration')) and registration_key(value.get('registration'))
                               and registration_key(previous['registration']) != registration_key(value['registration'])))
                result[key] = dict(value) if new_leg else dict(previous, **value)
                changed_inbound = changed_inbound or (key == '_inbound' and new_leg)
            elif key in ('cancelled', 'diverted'):
                result[key] = bool(old.get(key) or value)
            elif key == '_terminal_observed_at':
                result[key] = old.get(key) or value
            elif value is not None and value != '':
                result[key] = value
        result['entry_aliases'] = sorted(set(old.get('entry_aliases', []) + incoming.get('entry_aliases', [])))[:128]
        known = [v['created_at'] for v in (old, incoming) if v.get('_created_at_proven')]
        result['_created_at_proven'] = bool(known)
        result['created_at'] = min(known) if known else min(old['created_at'], incoming['created_at'])
        aircraft_changed = (departure_from_ber(result) and registration_key(old.get('registration'))
                            and registration_key(incoming.get('registration'))
                            and registration_key(old['registration']) != registration_key(incoming['registration']))
        if aircraft_changed and registration_key(result.get('_inbound', {}).get('registration')) != registration_key(result.get('registration')):
            result['_inbound'] = {}
            changed_inbound = True
        if changed_inbound and departure_from_ber(result):
            result.pop('stand', None)
            if incoming.get('_inbound', {}).get('aibt_position') and incoming.get('stand'):
                result['stand'] = incoming['stand']
        if result.get('aibt_position') and not departure_from_ber(result):
            from .aibt_recovery import position
            if not position(result):
                result.pop('aibt_position', None)
                result.pop('stand', None)
        return result

    def capture(self, flights, now=None, created_known=None, baseline=False, observed_at=None):
        """Caller has already verified source authorization; this performs no I/O."""
        from .comatalarm import same_occurrence
        now = time.time() if now is None else now
        self.prune(now)
        month = day_of(now)[:7]
        for original in flights:
            try:
                if not isinstance(original, dict):
                    continue
                incoming = self.sanitize(original, now, original.get('entry_aliases', ()), created_known)
                if incoming.get('cancelled') or incoming.get('diverted'):
                    incoming['_terminal_observed_at'] = now if observed_at is None else observed_at
                if self.removed(incoming):
                    continue
                candidates = [r for r in self.db.execute('SELECT * FROM comat_app_history')
                              if same_occurrence(incoming, json.loads(r['payload']))]
                # An unidentified row cannot choose between two real legs. A
                # colliding old app ID never overrides distinct provider IDs.
                if len(candidates) > 1:
                    exact = [r for r in candidates if actual_identifier(incoming)
                             and actual_identifier(json.loads(r['payload'])) == actual_identifier(incoming)]
                    if len(exact) != 1:
                        continue
                    candidates = exact
                prior = candidates[0] if candidates else None
                old = json.loads(prior['payload']) if prior else None
                if old and not self.removed(old):
                    # A dashboard backfill can fill missing facts, but must not
                    # overwrite newer raw server evidence already captured.
                    incoming = self.merge(incoming, old) if baseline else self.merge(old, incoming)
                    incoming.update(number=old['number'], reference=old['reference'])
                history_day = day_of(history_time(incoming))
                if history_day[:7] != month:
                    continue
                incoming['history_day'] = history_day
                identity = prior['identity'] if prior else hashlib.sha256(json.dumps(
                    [incoming['number'], day_of(incoming['reference']), incoming.get('origin', ''),
                     incoming.get('destination', ''), actual_identifier(incoming) or ''],
                    separators=(',', ':')).encode()).hexdigest()
                incoming['id'] = PREFIX + identity
                payload = json.dumps(incoming, sort_keys=True, ensure_ascii=False)
                with self.db:
                    self.db.execute('''INSERT INTO comat_app_history VALUES(?,?,?,?)
                        ON CONFLICT(identity) DO UPDATE SET day=excluded.day,saved_at=excluded.saved_at,payload=excluded.payload
                        WHERE payload<>excluded.payload''', (identity, history_day, now, payload))
            except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
                continue  # One corrupt saved row cannot interrupt pushes or peers.

    def legacy_record(self, archived, source, captured_at, created_known=False, observed_at=None):
        """Translate only a proven Pi source; never expose the admin projection."""
        from .monitor_actions import key_of
        record = {k: archived[k] for k in SCALARS if k in archived}
        record.update(id=source.get('id') or 'PI_' + key_of(archived)[:32])
        record['created_at'] = source.get('created_at', min(archived['reference'], captured_at))
        record['_schedule'] = archived.get('schedule', source.get('_schedule', {}))
        for old, new in (('inbound', '_inbound'), ('outbound', '_outbound')):
            if isinstance(archived.get(old), dict):
                record[new] = archived[old]
        counts = archived.get('packets')
        if isinstance(counts, dict):
            values = [counts.get(k, 0) for k in ('in', 'out')]
            record.pop('packets', None)
            if counts and all(type(v) is int and v >= 0 for v in values):
                record['packets'] = '/'.join(map(str, values))
        self.capture([record], captured_at, created_known=created_known, baseline=True, observed_at=observed_at)

    def backfill(self, now=None):
        """Recover provable older Pi records using local, retained evidence only."""
        from .comatalarm import same_occurrence
        now = time.time() if now is None else now
        self.prune(now)
        archived = list(self.db.execute('SELECT * FROM comat_monitor_history'))
        manual = []
        for row in self.db.execute('SELECT payload,expires FROM comat_manual'):
            try:
                manual.append(json.loads(row['payload']) | {'_operator_created_at': row['expires']-86400})
            except (TypeError, ValueError):
                continue
        proofs = {}
        for row in self.db.execute("SELECT timestamp,flight FROM comat_monitor_events WHERE kind='service' AND message=?",
                                   ('Über Ankunftstabelle zur Pi-Überwachung hinzugefügt',)):
            proofs.setdefault((row['flight'], day_of(row['timestamp'])), []).append(row['timestamp'])
        occurrences = {}
        for row in archived:
            try:
                f = json.loads(row['payload'])
                occurrences.setdefault((f['number'], row['day']), []).append(row)
            except (KeyError, TypeError, ValueError):
                continue
        for row in archived:
            try:
                f = json.loads(row['payload'])
                sources = [m for m in manual if same_occurrence(m, f)]
                if len(sources) == 1:
                    source = dict(sources[0])
                    source.setdefault('created_at', source['_operator_created_at'])
                    self.legacy_record(f, source, now, created_known=True, observed_at=row['saved_at'])
                    continue
                key = (f['number'], row['day'])
                evidence = [t for t in proofs.get(key, []) if t <= row['saved_at']+60]
                if len(occurrences[key]) == 1 and len(evidence) == 1:
                    self.legacy_record(f, {'created_at': evidence[0]}, now, created_known=True, observed_at=row['saved_at'])
            except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
                continue

    def resolve(self, identifier, now):
        if not isinstance(identifier, str):
            return None
        matches = []
        for row in self.db.execute('SELECT payload FROM comat_app_history WHERE substr(day,1,7)=?', (day_of(now)[:7],)):
            f = json.loads(row['payload'])
            if identifier.upper() == f['id'].upper():
                return f
            if any(identifier.upper() == alias.upper() for alias in f.get('entry_aliases', [])):
                matches.append(f)
        return matches[0] if len(matches) == 1 else None

    def retained_occurrence(self, flight, now):
        from .comatalarm import same_occurrence
        return any(same_occurrence(flight, json.loads(row['payload'])) for row in self.db.execute(
            'SELECT payload FROM comat_app_history WHERE substr(day,1,7)=?', (day_of(now)[:7],)))

    def rows(self, now):
        from .comatalarm import departure_from_ber, arrival_tracking_expired
        self.prune(now)
        result = []
        for row in self.db.execute('SELECT * FROM comat_app_history ORDER BY day,identity'):
            f = json.loads(row['payload'])
            if self.removed(f):
                continue
            if (f.get('track_outbound') and not f.get('_outbound', {}).get('atd')
                    and not f.get('cancelled') and not f.get('diverted')):
                continue
            completed = (f.get('atd') if departure_from_ber(f) else f.get('aibt'))
            if f.get('track_outbound') and f.get('_outbound', {}).get('atd'):
                completed = f['_outbound']['atd']
            if not (completed or f.get('cancelled') or f.get('diverted') or arrival_tracking_expired(f, now)):
                continue
            if not f.pop('_created_at_proven', None):
                f['created_at'] = 0  # Unknown is never evidence of a later re-add.
            observed = f.pop('_terminal_observed_at', None)
            f.update(archived=True, archived_at=row['saved_at'])
            if completed or observed:
                f['completed_at'] = completed or observed
            result.append(f)
        return result

    @staticmethod
    def describe(rows, now):
        month = day_of(now)[:7]
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        return dict(month=month, timezone='Europe/Berlin', ready=True, revision=month+':'+digest, count=len(rows))

    def metadata(self, now=None):
        now = time.time() if now is None else now
        return self.describe(self.rows(now), now)

    def read(self, body, now=None):
        now = time.time() if now is None else now
        offset, revision = body.get('offset', 0), body.get('revision', '')
        if type(offset) is not int or not 0 <= offset <= 50000 or not isinstance(revision, str) or len(revision) > 100:
            raise web.HTTPBadRequest(text='Invalid history page')
        rows = self.rows(now)
        meta = self.describe(rows, now)
        result = dict(meta, flights=[], next_offset=None, unchanged=False)
        if offset and revision != meta['revision']:
            raise web.HTTPConflict(text='History revision changed; restart download')
        if not offset and revision == meta['revision']:
            result['unchanged'] = True
            return dict(history=result)
        if offset > len(rows):
            raise web.HTTPBadRequest(text='Invalid history offset')
        page, size = [], 0
        for flight in rows[offset:offset+100]:
            length = len(json.dumps(flight).encode())
            if length > 48000:
                raise web.HTTPServiceUnavailable(text='History row too large')
            if size+length > 48000:
                break
            page.append(flight)
            size += length
        end = offset+len(page)
        return dict(history=dict(result, flights=page, next_offset=end if end < len(rows) else None))
