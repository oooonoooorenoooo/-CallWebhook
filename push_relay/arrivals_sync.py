"""Daily Berlin arrival snapshot and durable, device-scoped background delivery."""
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from aiohttp import web
from .monitor_actions import BERLIN, day_of


class ArrivalSync:
    def __init__(self, operator):
        self.operator, self.comat, self.db = operator, operator.comat, operator.db
        self.lock = asyncio.Lock()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS comat_arrival_subscribers (device TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS comat_arrival_delivery (
                device TEXT PRIMARY KEY, revision TEXT NOT NULL, attempted REAL NOT NULL,
                attempts INTEGER NOT NULL, sent INTEGER NOT NULL);
        ''')

    def metadata(self, now=None):
        now = time.time() if now is None else now
        row = self.db.execute('SELECT * FROM comat_arrivals WHERE id=1').fetchone()
        current = row is not None and row['day'] == day_of(now)
        ready = bool(current and row['fetched'])
        revision = (row['day'] + ':' + str(int(row['fetched'] * 1000)) + ':' +
                    hashlib.sha256(row['payload'].encode()).hexdigest()[:16]) if ready else ''
        return dict(day=day_of(now), ready=ready, revision=revision,
                    fetched_at=row['fetched'] if ready else 0,
                    error=row['error'] if current else '', scheduled_time='05:00', timezone='Europe/Berlin')

    def read(self, device, body, now=None):
        """Cache-only pages through the existing authenticated watch transport.

        Reading a table never changes flight watches, their expiry, or alarm rules.
        The revision prevents a refresh between pages from producing a mixed list.
        """
        now = time.time() if now is None else now
        offset, revision = body.get('offset', 0), body.get('revision', '')
        if type(offset) is not int or not 0 <= offset <= 50000 or not isinstance(revision, str) or len(revision) > 100:
            raise web.HTTPBadRequest(text='Invalid arrival page')
        meta = self.metadata(now)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO comat_arrival_subscribers VALUES(?)', (device['id'],))
        result = dict(meta, flights=[], next_offset=None, unchanged=False)
        if not meta['ready']:
            return dict(arrivals=result)
        if offset and revision != meta['revision']:
            raise web.HTTPConflict(text='Arrival revision changed; restart download')
        if not offset and revision == meta['revision']:
            result['unchanged'] = True
            return dict(arrivals=result)
        rows = json.loads(self.db.execute('SELECT payload FROM comat_arrivals WHERE id=1').fetchone()[0])
        if offset > len(rows):
            raise web.HTTPBadRequest(text='Invalid arrival offset')
        page, size = [], 0
        for f in rows[offset:offset + 100]:
            item = dict(number=f['number'], airline=f['airline'], carrier=f['carrier'],
                        originCode=f['origin'], origin=f['origin_name'], scheduled=f['reference'],
                        estimated=f['schedule'].get('eta'), aliases=f['aliases'])
            length = len(json.dumps(item).encode())
            if length > 48000:
                raise web.HTTPServiceUnavailable(text='Arrival row too large')
            if size + length > 48000: break
            page.append(item); size += length
        end = offset + len(page)
        return dict(arrivals=dict(result, flights=page, next_offset=end if end < len(rows) else None))

    def schedule(self, now):
        local = datetime.fromtimestamp(now, BERLIN)
        due = local.replace(hour=5, minute=0, second=0, microsecond=0).timestamp()
        if now < due: return
        meta = self.metadata(now)
        if meta['fetched_at'] >= due: return
        if self.comat.api_status.cooldown.get('ber', 0) > now: return
        # Persistent attempted time in Operator prevents retry storms after restarts.
        self.operator.request_arrivals(force=True, now=now)

    async def deliver(self, now):
        async with self.lock:
            meta = self.metadata(now)
            if not meta['ready']: return
            with self.db:
                for table in ('comat_arrival_subscribers', 'comat_arrival_delivery'):
                    self.db.execute('DELETE FROM ' + table + ' WHERE device NOT IN (SELECT id FROM comat_devices)')
            devices = self.db.execute('SELECT d.* FROM comat_devices d JOIN comat_arrival_subscribers s ON s.device=d.id').fetchall()
            for device in devices:
                prior = self.db.execute('SELECT * FROM comat_arrival_delivery WHERE device=?', (device['id'],)).fetchone()
                same = prior is not None and prior['revision'] == meta['revision']
                if same and (prior['sent'] or now - prior['attempted'] < min(3600, 60 * 2 ** min(prior['attempts'], 6))):
                    continue
                if device['firebase_uid'] and not await self.comat.stand_access(device['firebase_uid']):
                    continue
                current = self.db.execute('SELECT * FROM comat_devices WHERE id=?', (device['id'],)).fetchone()
                if current is None or current['credential'] != device['credential']: continue
                # Credentials authorize this sync separately from the 24h flight watch.
                # No active flights or open app are required; revoked grants are excluded.
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO comat_arrival_delivery VALUES(?,?,?,?,0)',
                        (device['id'], meta['revision'], now, prior['attempts'] + 1 if same else 1))
                midnight = datetime.fromtimestamp(now, BERLIN).date() + timedelta(days=1)
                expiry = datetime.combine(midnight, datetime.min.time(), BERLIN).timestamp()
                payload = dict(sync_kind='arrivals', event_id='comatalarm-arrivals',
                               arrivals_revision=meta['revision'], arrivals_day=meta['day'],
                               ttl=max(1, int(expiry - now)))
                try:
                    accepted = bool(self.comat.sender and await self.comat.sender(current['token'], current['environment'], payload))
                except Exception:
                    accepted = False
                with self.db:
                    self.db.execute('UPDATE comat_arrival_delivery SET sent=? WHERE device=? AND revision=?',
                                    (int(accepted), device['id'], meta['revision']))
                self.comat.monitor.record('push_accepted' if accepted else 'push_failed',
                    'Ankunftstabelle ' + meta['day'] + (': Daten-Push von Apple angenommen' if accepted else ': Daten-Push fehlgeschlagen; erneuter Versuch'),
                    device=device['id'], now=now)

    async def run(self):
        while True:
            try:
                now = time.time()
                self.schedule(now)
                await self.deliver(now)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.warning('ComatAlarm arrival_sync_failed kind=%s', type(error).__name__)
            await asyncio.sleep(15)
