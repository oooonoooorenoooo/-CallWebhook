"""Actual HTTP envelopes and bounded late AIBT repair, without live API calls."""
import hashlib
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from push_relay.comatalarm import FlightProvider
from push_relay.tests import test_comatalarm as fixtures
from push_relay.tests import test_shared_monitoring as shared_fixtures


class Response:
    status = 200
    headers = {'Content-Type': 'application/json', 'Retry-After': '600'}

    def __init__(self, body, status=200, before=None):
        self.body, self.status = body, status
        self.before = before

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if self.before:
            await self.before()
        return self.body


class RecoveryHTTP:
    def __init__(self, now, aibt):
        self.now, self.aibt = now, aibt
        self.calls, self.events, self.positions, self.tracks = [], [], [], []
        self.status = 200
        self.before_track = None

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, params=None, **kwargs):
        if 'fr24api.flightradar24.com/api/' not in url:
            return Response({'data': {'items': []}})
        path = url.split('/api/')[1]
        self.calls.append((self.now, path, dict(params)))
        if self.status != 200:
            return Response({}, self.status)
        if path == 'historic/flight-events/light':
            return Response({'data': [{'fr24_id': 'abc123', 'events': self.events}]})
        if path == 'historic/flight-positions/light':
            return Response({'data': self.positions})
        if path == 'flight-tracks':
            # Official Python/MCP SDKs use a top-level array for this endpoint.
            return Response(self.tracks, before=self.before_track)
        return Response({'data': []})


class ProviderAIBTHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = time.time()
        self.aibt = self.now-34*60
        self.flight = dict(id='pi-lh172', number='LH172', reference=self.now-3600,
            registration='D-AIDL', fr24ID='abc123', origin='FRA', destination='BER',
            atd=self.now-7200, ata=self.aibt-300, aibt=self.aibt, _central=True)
        self.http = RecoveryHTTP(self.now, self.aibt)
        self.provider = FlightProvider()
        for target, replacement in (
            ('push_relay.comatalarm.ClientSession', self.http),
            ('push_relay.flight_schedule.schedule_lookup', AsyncMock(return_value={})),
            ('push_relay.flight_schedule.aware_lookup', AsyncMock(return_value={}))):
            active = patch(target, replacement)
            active.start()
            self.addCleanup(active.stop)

    def point(self, **changes):
        result = dict(timestamp=self.aibt-5, lat=52.36507, lon=13.5053, gspeed=0, source='ADSB')
        result.update(changes)
        return result

    async def test_official_top_level_track_array_recovers_exact_aibt_sample(self):
        self.http.tracks = [{'fr24_id': 'abc123', 'tracks': [self.point()]}]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertNotIn('_error', result)
        self.assertEqual(result['aibt_position']['timestamp'], self.aibt-5)
        self.assertEqual(result['aibt_position']['latitude'], 52.36507)
        self.assertEqual([c[1] for c in self.http.calls],
                         ['historic/flight-events/light', 'historic/flight-positions/light', 'flight-tracks'])

    async def test_latest_event_cannot_reuse_an_older_events_coordinate(self):
        self.http.events = [dict(type='gate_arrival', timestamp=self.aibt-120,
                                 lat=52.36, lon=13.49),
                            dict(type='gate_arrival', timestamp=self.aibt)]
        self.http.tracks = [{'fr24_id': 'abc123', 'tracks': [self.point()]}]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertEqual(result['aibt'], self.aibt)
        self.assertEqual(result['aibt_position']['timestamp'], self.aibt-5)
        self.assertEqual(result['aibt_position']['latitude'], 52.36507)

    async def test_actual_event_point_precedes_gate_reference_metadata(self):
        self.http.events = [dict(type='gate_arrival', timestamp=self.aibt,
            lat=52.36507, lon=13.5053, gspeed=0,
            details={'gate_lat': 52.36, 'gate_lon': 13.49, 'gate_ident': 'reference-only'})]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertEqual(result['aibt_position']['latitude'], 52.36507)
        self.assertEqual(len(self.http.calls), 1)

    async def test_moving_aircraft_point_is_not_overridden_by_gate_reference(self):
        self.http.events = [dict(type='gate_arrival', timestamp=self.aibt, lat=52.365, lon=13.505, gspeed=20,
                                details={'gate_lat': 52.36507, 'gate_lon': 13.5053})]
        self.http.tracks = [{'fr24_id': 'abc123', 'tracks': [self.point()]}]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertEqual(result['aibt_position']['source'], 'fr24_track')
        self.assertEqual(result['aibt_position']['timestamp'], self.aibt-5)

    async def test_gate_details_only_and_nested_event_position_remain_compatible(self):
        for details, source in (({'gate_lat': 52.36507, 'gate_lon': 13.5053}, 'fr24_gate_reference'),
                                ({'position': {'lat': 52.36507, 'lon': 13.5053, 'timestamp': self.aibt}}, 'fr24_event')):
            self.provider = FlightProvider()
            self.http.calls.clear()
            self.http.events = [dict(type='gate_arrival', timestamp=self.aibt, details=details)]
            result = await self.provider.lookup(self.flight, 'test-token', self.now)
            self.assertEqual(result['aibt_position']['source'], source)
            self.assertEqual(len(self.http.calls), 1)

    async def test_skip_and_forced_refresh_are_separate_provider_cache_variants(self):
        self.http.events = [dict(type='gate_arrival', timestamp=self.aibt, lat=52.36507, lon=13.5053)]
        await self.provider.lookup(dict(self.flight, _skip_parking_recovery=True), 'test-token', self.now)
        self.assertEqual(self.http.calls, [])
        first = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertEqual(first['aibt_position']['latitude'], 52.36507)
        other = FlightProvider()
        self.http.calls.clear()
        old = dict(self.flight, aibt_position={'latitude': 52.36, 'longitude': 13.49, 'timestamp': self.aibt})
        await other.lookup(old, 'test-token', self.now)
        self.assertEqual(self.http.calls, [])
        repaired = await other.lookup(dict(old, _refresh_aibt_coordinate=True), 'test-token', self.now)
        self.assertEqual(repaired['aibt_position']['latitude'], 52.36507)
        self.assertEqual(len(self.http.calls), 1)

    async def test_wrong_leg_moving_or_stale_tracks_never_become_parking(self):
        self.http.tracks = [{'fr24_id': 'other', 'tracks': [self.point()]},
                            {'fr24_id': 'abc123', 'tracks': [self.point(gspeed=30), self.point(timestamp=self.aibt-61)]}]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertNotIn('aibt_position', result)
        self.assertNotIn('_error', result, 'An empty valid observation is distinct from a transport failure')


class AIBTWorkerPipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch
    add_peer = fixtures.ComatAlarmTests.add_peer
    own = shared_fixtures.SharedMonitoringTests.own
    row = shared_fixtures.SharedMonitoringTests.row
    watched = shared_fixtures.SharedMonitoringTests.watched

    async def asyncSetUp(self):
        await shared_fixtures.SharedMonitoringTests.asyncSetUp(self)
        self.flight.update(number='LH172', registration='D-AIDL', fr24ID='abc123',
                           atd=self.now-3600, ata=self.now-900, aibt=self.now-600)
        self.http = RecoveryHTTP(self.now, self.flight['aibt'])
        self.provider = FlightProvider()
        self.comat.provider = self.provider
        self.schedule, self.aware = AsyncMock(return_value={}), AsyncMock(return_value={})
        for target, replacement in (('push_relay.comatalarm.ClientSession', self.http),
                ('push_relay.flight_schedule.schedule_lookup', self.schedule),
                ('push_relay.flight_schedule.aware_lookup', self.aware)):
            active = patch(target, replacement)
            active.start()
            self.addCleanup(active.stop)

    def point(self, **changes):
        result = dict(timestamp=self.flight['aibt']-5, lat=52.36507, lon=13.5053, gspeed=0)
        result.update(changes)
        return result

    async def prepare(self, archived=False, attempts=6, coordinate=None, positions=True, **changes):
        await self.own(stand_positions=[{'name': 'B06', 'latitude': 52.36507, 'longitude': 13.5053}] if positions else [])
        f = dict(self.flight, _aibt_recovery_attempts=attempts, _aibt_recovery_at=self.now-301)
        if coordinate:
            f['aibt_position'] = coordinate
        f.update(changes)
        if archived:
            self.comat.history_sync.capture([f], self.now)
        else:
            with self.comat.db:
                self.comat.db.execute('UPDATE comat_devices SET watch=? WHERE id=?', (json.dumps([f]), self.id))
        return f

    async def tick(self, elapsed=0):
        self.http.now = self.now+elapsed
        await self.comat.tick(self.http.now)

    def repaired_track(self):
        self.http.tracks = [{'fr24_id': 'abc123', 'tracks': [self.point()]}]

    async def test_old_six_attempt_watch_recovers_and_keeps_real_gps_timestamp(self):
        await self.prepare()
        self.repaired_track()
        await self.tick()
        result = self.watched('reno')[0]
        self.assertEqual(result['aibt_position']['timestamp'], self.flight['aibt']-5)
        self.assertEqual(result['stand'], 'B06')
        self.assertEqual(result['_aibt_recovery_attempts'], 7)
        self.assertEqual(result['_parking_recovery']['state'], 'complete')
        self.comat.sender.assert_not_awaited()

    async def test_six_empty_checks_back_off_then_accept_later_data(self):
        await self.prepare(attempts=0)
        for elapsed in (0, 301, 602, 903, 1204, 1505):
            await self.tick(elapsed)
        self.assertEqual(self.watched('reno')[0]['_aibt_recovery_attempts'], 6)
        count = len(self.http.calls)
        await self.tick(1806)
        self.assertEqual(len(self.http.calls), count)
        await self.tick(2406)
        self.assertEqual(self.watched('reno')[0]['_aibt_recovery_attempts'], 7)
        self.repaired_track()
        await self.tick(4207)
        self.assertEqual(self.watched('reno')[0]['stand'], 'B06')
        self.assertEqual(self.watched('reno')[0]['_aibt_recovery_attempts'], 8)

    async def test_legacy_unmatched_coordinate_gets_one_revision_refresh_with_historic_preference(self):
        old = {'latitude': 52.36, 'longitude': 13.49, 'timestamp': self.flight['aibt']}
        await self.prepare(coordinate=old)
        self.http.events = [dict(type='gate_arrival', timestamp=self.flight['aibt'],
                                details={'gate_lat': old['latitude'], 'gate_lon': old['longitude']})]
        self.repaired_track()
        await self.tick()
        saved = self.watched('reno')[0]
        self.assertEqual(saved['aibt_position']['source'], 'fr24_track')
        self.assertEqual(saved['stand'], 'B06')
        self.assertEqual(len(self.http.calls), 3)
        await self.tick(61)
        await self.tick(361)
        self.assertEqual(len(self.http.calls), 3)

    async def test_unresolved_revision_refresh_is_bounded_and_does_not_loosen_stand_match(self):
        old = {'latitude': 52.36, 'longitude': 13.49, 'timestamp': self.flight['aibt']}
        await self.prepare(coordinate=old)
        self.http.events = [dict(type='gate_arrival', timestamp=self.flight['aibt'],
                                details={'gate_lat': old['latitude'], 'gate_lon': old['longitude']})]
        await self.tick()
        self.assertEqual(self.watched('reno')[0]['_parking_recovery']['state'], 'ambiguous')
        self.assertNotIn('stand', self.watched('reno')[0])
        for elapsed in (61, 901, 3601):
            await self.tick(elapsed)
        self.assertEqual(len(self.http.calls), 3)

    async def test_newer_aibt_clears_saved_old_coordinate_and_stand_in_watch_and_history(self):
        old = {'latitude': 52.36, 'longitude': 13.49, 'timestamp': self.flight['aibt']}
        await self.prepare(coordinate=old, stand='OLD')
        self.http.events = [dict(type='gate_arrival', timestamp=self.flight['aibt']+120)]
        await self.tick()
        for saved in (self.watched('reno')[0], self.comat.history_sync.rows(self.now)[0]):
            self.assertEqual(saved['aibt'], self.flight['aibt']+120)
            self.assertFalse(saved.get('aibt_position'))
            self.assertFalse(saved.get('stand'))

    async def test_missing_index_does_not_cause_paid_coordinate_search(self):
        await self.prepare(coordinate={'latitude': 52.36, 'longitude': 13.49, 'timestamp': self.flight['aibt']}, positions=False)
        await self.tick()
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.watched('reno')[0]['_parking_recovery']['state'], 'index_missing')

    async def test_budget_and_429_failures_do_not_spend_attempt_or_revision(self):
        await self.prepare()
        owner = hashlib.sha256(b'private-api-token').hexdigest()
        self.provider.requests[owner] = [self.now]*8
        await self.tick()
        first = self.watched('reno')[0]
        self.assertEqual(first['_aibt_recovery_attempts'], 6)
        self.assertEqual(first.get('_aibt_evidence_revision', 0), 0)
        self.assertEqual(self.http.calls, [])
        self.http.status = 429
        await self.tick(301)
        second = self.watched('reno')[0]
        self.assertEqual(second['_aibt_recovery_attempts'], 6)
        self.assertEqual(second['_parking_recovery']['state'], 'paused')
        self.assertGreaterEqual(second['_parking_recovery']['next_attempt'], self.now+901)
        await self.tick(602)
        self.assertEqual(len(self.http.calls), 1)

    async def test_repair_stops_at_24h_and_handoff_retains_only_existing_pending_watch(self):
        await self.prepare()
        await self.own(flights=[])
        self.assertEqual(len(self.watched('reno')), 1)
        expiry = self.flight['aibt']+86400-self.now
        await self.tick(expiry)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.watched('reno')[0]['_parking_recovery']['state'], 'expired')

    async def test_trusted_history_only_repairs_without_watches_pushes_or_other_providers(self):
        await self.prepare(archived=True, track_outbound=True, _outbound={'id': 'done-leg', 'atd': self.now-60})
        self.repaired_track()
        await self.tick()
        item = self.comat.history_sync.rows(self.now)[0]
        self.assertEqual(item['aibt_position']['timestamp'], self.flight['aibt']-5)
        self.assertEqual(item['aibt_position']['source'], 'fr24_track')
        self.assertEqual(item['stand'], 'B06')
        self.assertEqual(item['_outbound']['id'], 'done-leg')
        self.assertEqual(item['_aibt_recovery_attempts'], 7)
        self.assertEqual(item['_parking_recovery']['state'], 'complete')
        self.assertEqual(self.watched('reno'), [])
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_outbox').fetchone()[0], 0)
        self.comat.sender.assert_not_awaited()
        self.schedule.assert_not_awaited()
        self.aware.assert_not_awaited()
        self.assertEqual([c[1] for c in self.http.calls],
                         ['historic/flight-events/light', 'historic/flight-positions/light', 'flight-tracks'])
        await self.tick(15)
        self.assertEqual(len(self.http.calls), 3)

    async def test_archive_unmatched_legacy_point_gets_same_bounded_revision_repair(self):
        await self.prepare(archived=True, coordinate={'latitude': 52.36, 'longitude': 13.49, 'timestamp': self.flight['aibt']})
        self.repaired_track()
        await self.tick()
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['stand'], 'B06')
        self.assertEqual(self.watched('reno'), [])
        self.comat.sender.assert_not_awaited()

    async def test_empty_archive_attempt_persists_backoff_and_revision(self):
        await self.prepare(archived=True)
        await self.tick()
        item = self.comat.history_sync.rows(self.now)[0]
        self.assertEqual(item['_aibt_recovery_attempts'], 7)
        self.assertEqual(item['_aibt_evidence_revision'], 2)
        self.assertGreater(item['_parking_recovery']['next_attempt'], self.now+15)
        for elapsed in (15, 30, 61, 301):
            await self.tick(elapsed)
        self.assertEqual(len(self.http.calls), 3)

    async def test_archive_repair_requires_remaining_shared_http_budget(self):
        await self.prepare(archived=True)
        owner = hashlib.sha256(b'private-api-token').hexdigest()
        self.provider.requests[owner] = [self.now]*6
        await self.tick()
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['_aibt_recovery_attempts'], 6)

    async def test_archive_repair_obeys_deletion_revocation_and_replacement_races(self):
        for mutation in ('delete', 'readd', 'device', 'token', 'approval'):
            with self.subTest(mutation=mutation):
                self.allowed.add('reno')
                self.provider.requests.clear()
                self.provider.cache.clear()
                self.comat.shared_results.clear()
                self.comat.db.execute('DELETE FROM comat_app_history')
                self.comat.db.execute('DELETE FROM comat_shared_removed')
                self.comat.db.commit()
                if mutation == 'device':
                    pass
                response = await self.register()
                self.headers = {'Authorization': 'Bearer '+(await response.json())['credential']}
                f = await self.prepare(archived=True)
                self.repaired_track()
                async def race():
                    if mutation in ('delete', 'readd'):
                        self.comat.shared_monitoring.remove([dict(f, deleted_at=self.now+1)], self.now+1)
                        if mutation == 'readd':
                            self.comat.history_sync.capture([dict(f, id='new-instance', created_at=self.now+2)], self.now+2)
                    elif mutation == 'device':
                        self.comat.db.execute('DELETE FROM comat_devices WHERE id=?', (self.id,)); self.comat.db.commit()
                    elif mutation == 'token':
                        self.comat.db.execute("UPDATE comat_devices SET api_token='rotated' WHERE id=?", (self.id,)); self.comat.db.commit()
                    else:
                        self.allowed.remove('reno')
                self.http.before_track = race
                await self.tick()
                for item in self.comat.history_sync.rows(self.now+3):
                    self.assertFalse(item.get('aibt_position'))
                self.comat.sender.assert_not_awaited()
                self.http.before_track = None


if __name__ == '__main__':
    unittest.main()
