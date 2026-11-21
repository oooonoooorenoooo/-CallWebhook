"""Persistent subscriptions for the existing, approved ComatAlarm shared group.

Only the caller supplies freshly authorized devices. Legacy registrations never
participate, and provider credentials are not copied between device records.
"""
import hashlib
import json
import math


class SharedMonitoring:
    def __init__(self, comat):
        self.comat, self.db = comat, comat.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS comat_shared_removed (
            identity TEXT PRIMARY KEY, payload TEXT NOT NULL, deleted_at REAL NOT NULL)''')

    def removed(self, flight):
        from .comatalarm import same_occurrence
        created = flight.get('created_at', 0)
        for row in self.db.execute('SELECT payload,deleted_at FROM comat_shared_removed'):
            if same_occurrence(flight, json.loads(row['payload'])) and created <= row['deleted_at']:
                return True
        return False

    @staticmethod
    def current(flight, now):
        reference = flight.get('reference')
        return (isinstance(reference, (float, int)) and not isinstance(reference, bool)
                and math.isfinite(reference) and abs(reference-now) <= 48*3600)

    def retain(self, incoming, prior, now):
        """A stale phone snapshot cannot delete a newly shared subscription."""
        from .comatalarm import same_occurrence
        from .aibt_recovery import retain_pending
        result = [f for f in incoming if not self.removed(f)]
        for f in prior:
            repairing = retain_pending(f, now)
            # Importing a completed flight into phone history removes it from
            # the next active handoff. Finish only its already-running bounded
            # coordinate repair; never recreate a watch from archived records.
            if ((f.get('_shared_subscription') or repairing) and self.current(f, now)
                    and not self.removed(f) and not self.comat.monitor.operator.removed(f)
                    and not any(same_occurrence(f, other) for other in result)):
                retained = dict(f)
                if repairing and f.get('aibt') and not f.get('track_outbound'):
                    retained['_arrival_recovery_only'] = True
                result.append(retained)
        return result

    def remove(self, removals, now):
        """Apply explicit deletion evidence to the approved group's copies only."""
        from .comatalarm import same_occurrence
        with self.db:
            for removed in removals:
                deleted_at = removed['deleted_at']
                digest = hashlib.sha256(json.dumps(
                    [removed['number'], removed['reference'], removed.get('fr24ID'),
                     removed.get('origin'), removed.get('destination')], sort_keys=True).encode()).hexdigest()
                self.db.execute('''INSERT INTO comat_shared_removed VALUES(?,?,?)
                    ON CONFLICT(identity) DO UPDATE SET payload=excluded.payload,deleted_at=excluded.deleted_at
                    WHERE excluded.deleted_at>comat_shared_removed.deleted_at''',
                    (digest, json.dumps(removed), deleted_at))
            for device in self.db.execute("SELECT id,watch FROM comat_devices WHERE server_polling=1 AND firebase_uid!=''").fetchall():
                old = json.loads(device['watch'])
                clean = [f for f in old if not self.removed(f)]
                if clean == old:
                    continue
                self.db.execute('UPDATE comat_devices SET watch=? WHERE id=?', (json.dumps(clean), device['id']))
                removed_ids = {f['id'] for f in old if not any(same_occurrence(f, c) for c in clean)}
                for event in self.db.execute('SELECT event,payload FROM comat_outbox WHERE device=? AND sent=0', (device['id'],)).fetchall():
                    if json.loads(event['payload']).get('flight_id') in removed_ids:
                        self.db.execute('DELETE FROM comat_outbox WHERE device=? AND event=?', (device['id'], event['event']))
            # The app archive retains the current Berlin calendar month. Keep
            # its deletion evidence just as long, even after active watches end.
            from .history_sync import day_of
            for row in self.db.execute('SELECT identity,payload FROM comat_shared_removed WHERE deleted_at<?', (now-7*86400,)).fetchall():
                try:
                    f = json.loads(row['payload'])
                    retained = (day_of(f['reference'])[:7] == day_of(now)[:7]
                                or day_of(f['reference']+48*3600)[:7] == day_of(now)[:7]
                                or self.comat.history_sync.retained_occurrence(f, now))
                except (KeyError, TypeError, ValueError, OverflowError):
                    retained = True
                if not retained:
                    self.db.execute('DELETE FROM comat_shared_removed WHERE identity=?', (row['identity'],))

    def fanout(self, devices, now):
        """Seed new recipients with a baseline; later milestones notify each one."""
        from .comatalarm import same_occurrence, departure_from_ber, ARRIVAL_FIELDS
        from .aibt_recovery import valid_exhaustion
        decoded = []
        for device in devices:
            try:
                watch = json.loads(device['watch'])
                if not isinstance(watch, list) or any(not isinstance(f, dict) for f in watch):
                    raise ValueError()
                decoded.append((device, watch))
            except (ValueError, TypeError):
                # One damaged device snapshot must not stop the independent
                # subscriptions. Preserve it for diagnostics; do not erase it.
                self.comat.monitor.record('error', 'Gespeicherte Geräteüberwachung konnte nicht gelesen werden',
                                          device=device['id'], now=now)
        union = []
        for device, watch in decoded:
            for flight in watch:
                if flight.get('_arrival_recovery_only'):
                    continue  # Archive repair never creates new subscriptions.
                if (not self.current(flight, now) or self.removed(flight)
                        or self.comat.monitor.operator.removed(flight)):
                    continue
                if flight.get('cancelled') or flight.get('diverted'):
                    continue
                completed = flight.get('atd') if departure_from_ber(flight) else flight.get('aibt')
                if completed and now-completed > 900 and not flight.get('track_outbound'):
                    continue
                prior = next((f for f in union if same_occurrence(f, flight)), None)
                if prior is None:
                    union.append(dict(flight))
                else:
                    # Milestones discovered by any current server subscriber are
                    # baseline evidence for a newly joining device, not old alerts.
                    for key in ('atd', 'ata', 'aibt', 'fr24ID', 'origin', 'destination',
                                'registration', 'stand', 'aibt_position', '_prelanding') + ARRIVAL_FIELDS:
                        if flight.get(key) and not prior.get(key):
                            prior[key] = flight[key]
                    if flight.get('_checked_at', 0) > prior.get('_checked_at', 0):
                        for key in ('_live', '_schedule', '_checked_at', 'gate', 'gate_source',
                                    '_landing_alert', '_stand_prediction'):
                            if key in flight:
                                prior[key] = flight[key]
        with self.db:
            for device, watch in decoded:
                clean = [dict(f) for f in watch if not self.removed(f)]
                for flight in union:
                    existing = next((old for old in clean if same_occurrence(flight, old)), None)
                    if existing is not None:
                        # Share the provider decision without copying a new
                        # milestone over the recipient's notification baseline.
                        marker = flight.get('fr24_arrival_exhausted')
                        if valid_exhaustion(existing, marker) and not valid_exhaustion(existing):
                            existing['fr24_arrival_exhausted'] = dict(marker)
                        continue
                    shared = dict(flight, _shared_subscription=True)
                    if any(old['id'] == shared['id'] for old in clean):
                        identity = json.dumps([shared['number'], shared['reference'], shared.get('fr24ID')])
                        shared['id'] = 'SHARED_' + hashlib.sha256(identity.encode()).hexdigest()[:32]
                    clean.append(shared)
                if clean != watch:
                    self.db.execute('UPDATE comat_devices SET watch=?,last_poll=0 WHERE id=? AND watch=?',
                                    (json.dumps(clean), device['id'], device['watch']))
                    self.comat.monitor.record('subscription', 'Gemeinsame Flugüberwachungen auf dem Pi aktualisiert',
                                              device=device['id'], now=now)
