"""Prioritized arrival handoff and 30s approach observations, real provider."""
import asyncio
import hashlib
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from push_relay.comatalarm import FlightProvider
from push_relay.tests import test_shared_monitoring as shared_fixtures
from push_relay.tests import test_comatalarm as fixtures
from push_relay.tests import test_fr24_batch as batch_fixtures
from push_relay.tests import test_aibt_pipeline as aibt_fixtures


class FastHTTP(batch_fixtures.ProviderHTTP):
    def __init__(self, flights, now):
        super().__init__(flights, now)
        self.events = {}
        self.before_event = None

    def get(self, url, params=None, headers=None, **kwargs):
        if url.endswith('/historic/flight-events/light'):
            self.calls.append((self.now, 'historic/flight-events/light', dict(params), headers['Authorization']))
            if self.status != 200:
                return aibt_fixtures.Response({}, self.status)
            rows = [{'fr24_id': identifier, 'events': self.events.get(identifier, [])}
                    for identifier in params['flight_ids'].split(',') if identifier in self.flights]
            return aibt_fixtures.Response({'data': rows}, before=self.before_event)
        return super().get(url, params=params, headers=headers, **kwargs)


class FastAlertPollingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = shared_fixtures.SharedMonitoringTests.asyncSetUp
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch
    add_peer = fixtures.ComatAlarmTests.add_peer
    own = shared_fixtures.SharedMonitoringTests.own
    row = shared_fixtures.SharedMonitoringTests.row
    watched = shared_fixtures.SharedMonitoringTests.watched

    def configure(self, flights):
        self.comat.provider = FlightProvider()
        self.arrival_lookup = AsyncMock(return_value={})
        self.comat.provider.arrivals.lookup = self.arrival_lookup
        self.http = FastHTTP(flights, self.now)
        self.schedule, self.aware = AsyncMock(return_value={}), AsyncMock(return_value={})
        for target, replacement in (('push_relay.comatalarm.ClientSession', self.http),
                ('push_relay.flight_schedule.schedule_lookup', self.schedule),
                ('push_relay.flight_schedule.aware_lookup', self.aware)):
            p = patch(target, replacement); p.start(); self.addCleanup(p.stop)

    @staticmethod
    def positions():
        return [{'name': 'B06', 'latitude': 52.36507, 'longitude': 13.5053}]

    def taxi_flights(self, count):
        return [dict(f, ata=self.now-120) for f in batch_fixtures.flights(count, self.now)]

    def event(self, at):
        return dict(type='gate_arrival', timestamp=at, lat=52.36507, lon=13.5053, gspeed=0)

    async def tick(self, offset):
        self.http.now = self.now+offset
        await self.comat.tick(self.http.now)
        await aibt_fixtures.settle_arrivals(self.comat, self.http.now)

    async def test_alternative_between_ticks_bypasses_warm_cache_and_commits_before_both_pushes(self):
        f = self.taxi_flights(1)[0]
        self.configure([f])
        # The old normal lookup really contains an empty gate-event response.
        initial = await self.comat.provider.lookup(dict(f, _central=True), 'private-api-token', self.now)
        self.assertNotIn('aibt', initial)
        await self.own(flights=[f], stand_positions=self.positions())
        await self.add_peer(f, firebase_token='colleague', server_polling=True, relay_alerts=True,
                            stand_positions=self.positions())
        with self.comat.db:
            for row in self.comat.db.execute('SELECT id FROM comat_devices').fetchall():
                self.comat.db.execute('UPDATE comat_devices SET watch=?,last_poll=? WHERE id=?',
                    (json.dumps([dict(f, **initial)]), self.now, row['id']))
        self.http.calls.clear(); self.schedule.reset_mock(); self.aware.reset_mock()
        await self.tick(0)
        self.assertEqual(self.http.calls, [])
        actual = self.now+1
        self.http.events[f['fr24ID']] = [self.event(actual)]
        self.arrival_lookup.return_value = dict(aibt=actual, aibt_source='FlightStats',
            aibt_position=dict(latitude=52.36507, longitude=13.5053, timestamp=actual, source='adsb_lol'))
        async def assert_atomic(token, environment, payload):
            row = self.comat.db.execute('SELECT watch FROM comat_devices WHERE token=?', (token,)).fetchone()
            saved = json.loads(row['watch'])[0]
            self.assertEqual(saved['aibt'], actual)
            self.assertEqual(saved['aibt_position']['timestamp'], actual)
            self.assertEqual(saved['stand'], 'B06')
            archived = self.comat.history_sync.rows(self.now+15)[0]
            self.assertEqual(archived['aibt'], actual)
            self.assertEqual(archived['stand'], 'B06')
            self.assertEqual(payload['flight_state']['stand'], 'B06')
            return True
        self.comat.sender.side_effect = assert_atomic
        await self.tick(15)
        self.assertEqual(self.http.calls, [], 'The first empty FR24 reply remains final for this leg')
        self.assertEqual(self.comat.sender.await_count, 2)
        self.assertEqual(self.row('reno')['last_poll'], self.now, 'Fast work cannot postpone regular airborne polling')
        for offset in (30, 45, 60):
            await self.tick(offset)
        self.assertEqual(len(self.http.calls), 0)
        self.assertEqual(self.comat.sender.await_count, 2)
        self.schedule.assert_not_awaited(); self.aware.assert_not_awaited()

    async def test_all_gate_events_and_pushes_precede_an_unrelated_slow_schedule(self):
        air, taxi = batch_fixtures.flights(2, self.now)
        taxi['ata'] = self.now-120
        self.configure([air, taxi])
        # Airborne work is intentionally first in the uploaded watch.
        await self.own(flights=[air, taxi], stand_positions=self.positions())
        self.http.events[taxi['fr24ID']] = [self.event(self.now-10)]
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_schedule(*args):
            entered.set()
            await release.wait()
            return {}
        self.schedule.side_effect = slow_schedule
        task = asyncio.create_task(self.tick(0))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.comat.sender.await_count, 1)
            self.assertEqual(self.comat.sender.call_args.args[2]['flag'], 'alarmParking')
            self.assertEqual(self.http.calls[0][1], 'historic/flight-events/light')
            self.assertTrue(next(f for f in self.watched('reno') if f['id'] == taxi['id']).get('aibt_position'))
        finally:
            release.set()
            await task

    async def test_partial_aibt_then_local_stand_notifies_both_closed_apps_once(self):
        flight = self.taxi_flights(1)[0]
        self.configure([flight])
        await self.own(flights=[flight], stand_positions=self.positions())
        await self.add_peer(dict(flight, id='other-closed-phone-flight'), firebase_token='colleague',
                            server_polling=True, relay_alerts=True, stand_positions=self.positions())
        actual, observed = self.now-10, self.now-15
        self.arrival_lookup.return_value = dict(aibt=actual, aibt_source='FlightStats',
            _arrival_sources_checked_at=self.now, _arrival_sources_status='aibt_only')

        # The real worker receives the empty FR24 reply and commits only the
        # independently confirmed time. Neither phone opens or posts again.
        await self.tick(0)
        for uid in ('reno', 'colleague'):
            saved = self.watched(uid)[0]
            self.assertEqual(saved['aibt'], actual)
            self.assertEqual(saved['aibt_source'], 'FlightStats')
            self.assertFalse(saved.get('aibt_position'))
            self.assertFalse(saved.get('stand'))
            self.assertIn('fr24_arrival_exhausted', saved)
        self.comat.sender.assert_not_awaited()
        self.assertEqual([call[1] for call in self.http.calls], ['historic/flight-events/light'])

        point = dict(latitude=52.36507, longitude=13.5053, timestamp=observed,
                     source='adsb_lol', on_ground=True, ground_speed=0.0)
        self.arrival_lookup.return_value = dict(aibt=actual, aibt_source='FlightStats',
            aibt_position=point, _arrival_sources_checked_at=self.now+15,
            _arrival_sources_status='complete')

        async def assert_committed_before_push(token, environment, payload):
            saved = json.loads(self.comat.db.execute(
                'SELECT watch FROM comat_devices WHERE token=?', (token,)).fetchone()['watch'])[0]
            self.assertEqual(saved['aibt'], actual)
            self.assertEqual(saved['aibt_position'], point)
            self.assertEqual(saved['stand'], 'B06')
            self.assertEqual(payload['flag'], 'alarmParking')
            self.assertEqual(payload['flight_state']['aibt'], actual)
            self.assertEqual(payload['flight_state']['aibt_position'], point)
            self.assertEqual(payload['flight_state']['stand'], 'B06')
            self.assertIn('Stand B06', payload['body'])
            return True

        self.comat.sender.side_effect = assert_committed_before_push
        # A known AIBT must keep the active arrival's 15-second fast lane while
        # its independently observed coordinate is still missing.
        await self.tick(15)
        self.assertEqual(self.comat.sender.await_count, 2)
        self.assertEqual(len({call.args[0] for call in self.comat.sender.call_args_list}), 2)
        day = datetime.fromtimestamp(flight['reference'], ZoneInfo('Europe/Berlin')).date().isoformat()
        expected_event = hashlib.sha256((flight['number']+':'+day+':aibt').encode()).hexdigest()
        self.assertEqual({call.args[2]['event_id'] for call in self.comat.sender.call_args_list}, {expected_event})
        for uid in ('reno', 'colleague'):
            self.assertEqual(self.watched(uid)[0]['stand'], 'B06')
        for offset in (30, 45, 60):
            await self.tick(offset)
        self.assertEqual(self.comat.sender.await_count, 2, 'Each recipient gets only the first confirmed stand')
        self.assertEqual([call[1] for call in self.http.calls], ['historic/flight-events/light'],
                         'Completing parking cannot retry the exhausted FR24 occurrence')

    async def test_fifteen_event_ids_share_one_call_and_429_never_fans_out(self):
        tracked = self.taxi_flights(16)
        self.configure(tracked)
        await self.own(flights=tracked)
        await self.tick(0)
        calls = [c for c in self.http.calls if c[1] == 'historic/flight-events/light']
        self.assertEqual(sorted(len(c[2]['flight_ids'].split(',')) for c in calls), [1, 15])
        self.assertCountEqual([identifier for c in calls for identifier in c[2]['flight_ids'].split(',')],
                              [f['fr24ID'] for f in tracked])
        await self.tick(15)
        self.assertEqual(len(self.http.calls), 2, 'Every empty member must leave the FR24 batch permanently')
        fresh = self.taxi_flights(17)[-1]
        self.http.flights[fresh['fr24ID']] = fresh
        await self.own(flights=tracked+[fresh])
        self.http.status = 429
        await self.tick(30)
        self.assertEqual(len(self.http.calls), 3)
        self.assertEqual(self.http.calls[-1][2]['flight_ids'], fresh['fr24ID'])
        await self.tick(45)
        self.assertEqual(len(self.http.calls), 3)
        self.assertTrue(all(f.get('fr24_arrival_exhausted') for f in self.watched('reno')))
        self.assertGreater(self.comat.provider.cooldown[hashlib.sha256(b'private-api-token').hexdigest()], self.now+45)
        self.comat.sender.assert_not_awaited()

    async def test_twelve_approaches_and_fifteen_taxi_legs_keep_the_shared_eight_request_budget(self):
        tracked = batch_fixtures.flights(27, self.now)
        for f in tracked[12:]:
            f['ata'] = self.now-120
        self.configure(tracked)
        await self.own(flights=tracked)
        await self.add_peer(tracked[0], flights=[], firebase_token='colleague', server_polling=True, relay_alerts=True)
        for offset in (0, 15, 30, 45, 60, 75, 90, 105, 120):
            await self.tick(offset)
            recent = [c for c in self.http.calls if self.now+offset-60 < c[0] <= self.now+offset]
            self.assertLessEqual(len(recent), 8)
            for uid in ('reno', 'colleague'):
                saved = self.watched(uid)
                self.assertTrue(all(f.get('_live_checked_at', 0) >= self.now+offset-60
                                    for f in saved if f['fr24ID'] in {x['fr24ID'] for x in tracked[:12]}))
        first_minute = [c for c in self.http.calls if self.now <= c[0] < self.now+60]
        self.assertEqual(len(first_minute), 5)
        self.assertEqual(sum(c[1] == 'historic/flight-events/light' for c in self.http.calls), 1)
        self.assertEqual(sum(c[1] == 'live/flight-positions/full' for c in first_minute), 2)
        self.assertEqual(sum(c[1] == 'flight-summary/full' for c in first_minute), 2)
        self.assertFalse(any(f.get('aibt') for f in self.watched('reno')))
        alerts = [c for c in self.comat.sender.call_args_list if c.args[2]['flag'] == 'alarmPrelanding']
        self.assertEqual(len(alerts), 24)

    async def test_event_batches_keep_token_and_current_approval_boundaries(self):
        tracked = self.taxi_flights(2)
        self.configure(tracked)
        await self.own(flights=[tracked[0]])
        await self.add_peer(tracked[1], firebase_token='muted', server_polling=True, relay_alerts=True)
        self.allowed.remove('muted')
        await self.tick(0)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.http.calls[0][2]['flight_ids'], tracked[0]['fr24ID'])
        # Direct real-provider plans additionally prove no cache crosses tokens.
        provider = FlightProvider()
        provider.plan_batches([(dict(tracked[0], _arrival_fast=True), 'one'),
                               (dict(tracked[1], _arrival_fast=True), 'two')], self.now)
        self.http.calls.clear()
        for f, token in zip(tracked, ('one', 'two')):
            await provider.lookup(dict(f, _central=True, _arrival_fast=True), token, self.now)
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual([c[3] for c in self.http.calls], ['Bearer one', 'Bearer two'])
        self.assertEqual([c[2]['flight_ids'] for c in self.http.calls], [f['fr24ID'] for f in tracked])

    async def test_successful_empty_event_reply_is_visible_without_error_or_log_spam(self):
        f = self.taxi_flights(1)[0]
        self.configure([f])
        await self.own(flights=[f], stand_positions=self.positions())
        await self.tick(0)
        saved = self.watched('reno')[0]
        self.assertEqual(saved['_arrival_event_status'], 'no_match')
        self.assertEqual(saved['_arrival_event_received_at'], self.now)
        records = self.comat.db.execute("SELECT message FROM comat_monitor_events WHERE kind='arrival'").fetchall()
        self.assertEqual(len(records), 1)
        self.assertIn('kein weiterer FR24-Ankunftsabruf', records[0]['message'])
        self.assertIn(f['fr24ID'], records[0]['message'])
        self.assertNotIn('private-api-token', records[0]['message'])
        await self.tick(15)
        self.assertEqual(self.watched('reno')[0]['_arrival_event_received_at'], self.now)
        self.assertEqual(self.comat.db.execute("SELECT count(*) FROM comat_monitor_events WHERE kind='arrival'").fetchone()[0], 1)
        self.http.events[f['fr24ID']] = [self.event(self.now+20)]
        self.arrival_lookup.return_value = dict(aibt=self.now+20, aibt_source='FlightStats',
            aibt_position=dict(latitude=52.36507, longitude=13.5053,
                               timestamp=self.now+20, source='adsb_lol'))
        await self.tick(30)
        saved = self.watched('reno')[0]
        self.assertEqual(saved['_arrival_event_status'], 'no_match')
        self.assertEqual(saved['aibt_source'], 'FlightStats')
        self.assertEqual(saved['aibt'], self.now+20)
        self.assertEqual(self.comat.db.execute("SELECT count(*) FROM comat_monitor_events WHERE kind='arrival'").fetchone()[0], 1)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.comat.sender.await_count, 1)

    async def test_delete_credential_uid_token_and_approval_changes_win_during_event_io(self):
        for mutation in ('delete', 'credential', 'uid', 'token', 'approval'):
            with self.subTest(mutation=mutation):
                self.allowed.add('reno')
                for table in ('comat_outbox', 'comat_delivered', 'comat_devices', 'comat_app_history', 'comat_shared_removed'):
                    self.comat.db.execute('DELETE FROM '+table)
                self.comat.db.commit()
                self.comat.shared_results.clear()
                self.comat.last_successful_lookup.clear()
                response = await self.register()
                self.headers = {'Authorization': 'Bearer '+(await response.json())['credential']}
                f = self.taxi_flights(1)[0]
                self.configure([f])
                await self.own(flights=[f], stand_positions=self.positions())
                self.http.events[f['fr24ID']] = [self.event(self.now-10)]
                async def change():
                    if mutation == 'delete':
                        self.comat.shared_monitoring.remove([dict(f, deleted_at=self.now+1)], self.now+1)
                    elif mutation == 'approval':
                        self.allowed.remove('reno')
                    else:
                        field = {'credential': 'credential', 'uid': 'firebase_uid', 'token': 'api_token'}[mutation]
                        self.comat.db.execute('UPDATE comat_devices SET '+field+'=? WHERE id=?', ('changed', self.id))
                        self.comat.db.commit()
                self.http.before_event = change
                await self.tick(0)
                row = self.comat.db.execute('SELECT watch FROM comat_devices WHERE id=?', (self.id,)).fetchone()
                self.assertFalse(any(f.get('aibt') for f in json.loads(row['watch'])))
                self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_outbox').fetchone()[0], 0)
                self.assertFalse(any(f.get('aibt') for f in self.comat.history_sync.rows(self.now+1)))
                self.comat.sender.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
