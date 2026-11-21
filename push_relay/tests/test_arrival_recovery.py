"""ATA-only gate-arrival recovery through the real provider and worker, offline."""
import hashlib
import json
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

from push_relay.comatalarm import FlightProvider, utc_query_time
from push_relay.fr24_batch import planned_queries
from push_relay.tests import test_aibt_pipeline as aibt_fixtures

RecoveryHTTP, Response = aibt_fixtures.RecoveryHTTP, aibt_fixtures.Response


class ArrivalHTTP(RecoveryHTTP):
    def __init__(self, now, aibt):
        super().__init__(now, aibt)
        self.summary = []
        self.event_rows = None
        self.before_event = None

    def get(self, url, params=None, **kwargs):
        if self.status == 200 and url.endswith('/flight-summary/full'):
            self.calls.append((self.now, 'flight-summary/full', dict(params)))
            return Response({'data': self.summary})
        if self.status == 200 and url.endswith('/historic/flight-events/light'):
            self.calls.append((self.now, 'historic/flight-events/light', dict(params)))
            rows = self.event_rows if self.event_rows is not None else [{'fr24_id': 'abc123', 'events': self.events}]
            return Response({'data': rows}, before=self.before_event)
        return super().get(url, params=params, **kwargs)


class ArrivalProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A fixed morning leaves Berlin's UTC query boundary unambiguous.
        self.now = datetime(2026, 11, 20, 10, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        self.ata, self.aibt = self.now-2400, self.now-2100
        self.flight = dict(id='offline-lh194', number='LH194', reference=self.now-3600,
            fr24ID='abc123', registration='D-TEST', origin='FRA', destination='BER',
            atd=self.now-5400, ata=self.ata, _central=True)
        self.http = ArrivalHTTP(self.now, self.aibt)
        self.provider = FlightProvider()
        self.arrival_lookup = AsyncMock(return_value={})
        self.provider.arrivals.lookup = self.arrival_lookup
        self.schedule, self.aware = AsyncMock(return_value={}), AsyncMock(return_value={})
        for target, replacement in (('push_relay.comatalarm.ClientSession', self.http),
                ('push_relay.flight_schedule.schedule_lookup', self.schedule),
                ('push_relay.flight_schedule.aware_lookup', self.aware)):
            p = patch(target, replacement); p.start(); self.addCleanup(p.stop)

    def event(self, timestamp=None):
        return dict(type='gate_arrival', timestamp=self.aibt if timestamp is None else timestamp,
                    lat=52.36507, lon=13.5053, gspeed=0)

    async def test_before_timeout_known_leg_queries_gate_event_without_summary(self):
        self.flight['ata'] = self.now-600
        self.http.events = [self.event(self.now-300)]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertEqual(result['aibt'], self.now-300)
        self.assertEqual(result['aibt_position']['timestamp'], self.now-300)
        self.assertEqual([c[1] for c in self.http.calls], ['historic/flight-events/light'])
        self.assertEqual(self.http.calls[0][2], {'flight_ids': 'abc123', 'event_types': 'gate_arrival'})

    async def test_before_timeout_temporary_id_discovers_correct_day_and_route(self):
        self.flight.update(fr24ID='TEMP_LH194', ata=self.now-600)
        self.http.summary = [dict(fr24_id='wrong-route', flight='LH194', orig_iata='MUC', dest_iata='BER'),
            dict(fr24_id='other-flight', flight='LH192', orig_iata='FRA', dest_iata='BER'),
            dict(fr24_id='abc123', flight='LH194', orig_iata='FRA', dest_iata='BER',
                 datetime_takeoff=self.now-3600, datetime_landed=self.now-600)]
        self.http.events = [self.event(self.now-300)]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertEqual(result['fr24ID'], 'abc123')
        self.assertEqual(result['aibt'], self.now-300)
        start = datetime.fromtimestamp(self.flight['reference'], ZoneInfo('Europe/Berlin')).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        self.assertEqual(self.http.calls[0][2], dict(flights='LH194', flight_datetime_from=utc_query_time(start),
                                                   flight_datetime_to=utc_query_time(self.now)))
        self.assertEqual(self.http.calls[1][2]['flight_ids'], 'abc123')

    async def test_late_gate_event_is_retrieved_after_25_minute_ui_timeout(self):
        self.http.events = [self.event()]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertEqual(result.get('aibt'), self.aibt)
        self.assertEqual(result['aibt_position']['source'], 'fr24_event')
        self.assertEqual([c[1] for c in self.http.calls], ['historic/flight-events/light'])
        self.schedule.assert_not_awaited(); self.aware.assert_not_awaited()

    async def test_late_missing_id_resolves_exact_leg_then_queries_its_event(self):
        self.flight.pop('fr24ID')
        self.http.summary = [dict(fr24_id='abc123', flight='LH194', orig_iata='FRA', dest_iata='BER',
                                 datetime_takeoff=self.now-5400, datetime_landed=self.ata)]
        self.http.events = [self.event()]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertEqual(result.get('aibt'), self.aibt)
        self.assertEqual([c[1] for c in self.http.calls], ['flight-summary/full', 'historic/flight-events/light'])
        self.assertEqual(planned_queries(self.flight, self.now)[0][0], 'flight-summary/full')
        self.assertNotIn('live/flight-positions/full', [path for path, _ in planned_queries(self.flight, self.now)])

    async def test_empty_events_do_not_synthesize_aibt_or_query_tracks_without_it(self):
        self.http.tracks = [{'fr24_id': 'abc123', 'tracks': [dict(timestamp=self.aibt, lat=52.36507, lon=13.5053, gspeed=0)]}]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertNotIn('aibt', result); self.assertNotIn('aibt_position', result)
        self.assertEqual([c[1] for c in self.http.calls], ['historic/flight-events/light'])
        self.assertNotIn('_error', result)
        self.assertIn('fr24_arrival_exhausted', result)
        self.arrival_lookup.assert_awaited_once()

    async def test_other_leg_and_events_before_ata_or_after_now_are_rejected(self):
        self.http.event_rows = [{'fr24_id': 'another-leg', 'events': [self.event()]},
            {'fr24_id': 'abc123', 'events': [self.event(self.ata-1), self.event(self.now+61)]}]
        result = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertNotIn('aibt', result)
        self.assertEqual(len(self.http.calls), 1)

    async def test_missing_aibt_ends_at_ata_plus_24_hours_and_keeps_provider_backoff(self):
        self.http.events = [self.event()]
        expired = dict(self.flight, ata=self.now-86400)
        self.assertEqual(await self.provider.lookup(expired, 'test-token', self.now), {})
        self.assertEqual(planned_queries(expired, self.now), [])
        self.assertEqual(self.http.calls, [])
        self.http.status = 429
        first = await self.provider.lookup(self.flight, 'test-token', self.now)
        self.assertIn('HTTP 429', first['_error'])
        await self.provider.lookup(self.flight, 'test-token', self.now+61)
        self.assertEqual(len(self.http.calls), 1)


class ArrivalWorkerTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = aibt_fixtures.AIBTWorkerPipelineTests.asyncTearDown
    register = aibt_fixtures.AIBTWorkerPipelineTests.register
    watch = aibt_fixtures.AIBTWorkerPipelineTests.watch
    add_peer = aibt_fixtures.AIBTWorkerPipelineTests.add_peer
    own = aibt_fixtures.AIBTWorkerPipelineTests.own
    row = aibt_fixtures.AIBTWorkerPipelineTests.row
    watched = aibt_fixtures.AIBTWorkerPipelineTests.watched
    tick = aibt_fixtures.AIBTWorkerPipelineTests.tick

    async def asyncSetUp(self):
        await aibt_fixtures.AIBTWorkerPipelineTests.asyncSetUp(self)
        self.flight.update(number='LH194', atd=self.now-5400, ata=self.now-2400,
                           reference=self.now-3600, created_at=self.now-7200)
        self.flight.pop('aibt')
        self.actual_aibt = self.now-2100
        self.http = ArrivalHTTP(self.now, self.actual_aibt)
        p = patch('push_relay.comatalarm.ClientSession', self.http)
        p.start(); self.addCleanup(p.stop)

    async def prepare(self, **changes):
        changes.setdefault('attempts', 0)
        return await aibt_fixtures.AIBTWorkerPipelineTests.prepare(self, **changes)

    def available_event(self):
        self.http.events = [dict(type='gate_arrival', timestamp=self.actual_aibt,
                                 lat=52.36507, lon=13.5053, gspeed=0)]

    async def test_shared_closed_watches_recover_late_aibt_with_one_provider_request(self):
        f = await self.prepare()
        await self.add_peer(f, firebase_token='colleague', server_polling=True, relay_alerts=True,
                            stand_positions=[{'name': 'B06', 'latitude': 52.36507, 'longitude': 13.5053}])
        self.available_event()
        await self.tick()
        for uid in ('reno', 'colleague'):
            self.assertEqual(self.watched(uid)[0]['aibt'], self.actual_aibt)
            self.assertEqual(self.watched(uid)[0]['stand'], 'B06')
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.comat.sender.await_count, 2)
        await self.tick(61)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.comat.sender.await_count, 2)

    async def test_trusted_ata_only_archive_recovers_without_watch_or_push(self):
        await self.prepare(archived=True)
        self.available_event()
        await self.tick()
        item = self.comat.history_sync.rows(self.now)[0]
        self.assertEqual(item.get('aibt'), self.actual_aibt)
        self.assertEqual(item['stand'], 'B06')
        self.assertEqual(item['_parking_recovery']['state'], 'complete')
        self.assertEqual(self.watched('reno'), [])
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_outbox').fetchone()[0], 0)
        self.comat.sender.assert_not_awaited()
        self.schedule.assert_not_awaited(); self.aware.assert_not_awaited()

    async def test_empty_archive_switches_to_alternative_without_another_fr24_attempt(self):
        await self.prepare(archived=True)
        await self.tick()
        item = self.comat.history_sync.rows(self.now)[0]
        self.assertNotIn('aibt', item)
        self.assertEqual(item['_aibt_recovery_attempts'], 1)
        self.assertIn('AIBT noch nicht übernommen', item['_parking_lookup_status'])
        self.assertNotIn('AIBT bestätigt', item['_parking_lookup_status'])
        self.assertIn('fr24_arrival_exhausted', item)
        await self.tick(61)
        self.assertEqual(len(self.http.calls), 1)
        self.available_event()
        self.arrival_lookup.return_value = dict(aibt=self.actual_aibt, aibt_source='FlightStats',
            aibt_position=dict(latitude=52.36507, longitude=13.5053,
                               timestamp=self.actual_aibt-5, source='adsb_lol'))
        await self.tick(301)
        self.assertEqual(len(self.http.calls), 1, 'Even a later usable FR24 response must not be requested')
        saved = self.comat.history_sync.rows(self.now+301)[0]
        self.assertEqual(saved['aibt'], self.actual_aibt)
        self.assertEqual(saved['aibt_source'], 'FlightStats')
        self.assertEqual(saved['stand'], 'B06')
        self.comat.sender.assert_not_awaited()

    async def test_watch_survives_history_handoff_only_within_recovery_window(self):
        await self.prepare()
        await self.own(flights=[])
        self.assertEqual(len(self.watched('reno')), 1)
        with patch('push_relay.comatalarm.time.time', return_value=self.now+86400):
            await self.own(flights=[])
        self.assertEqual(self.watched('reno'), [])

    async def test_authenticated_recovery_only_handoff_never_subscribes_or_replays(self):
        await self.own()
        f = dict(self.flight, archived=True, repair_arrival=True, recovery_only=True)
        response = await self.own(flights=[f])
        payload = await response.json()
        self.assertTrue(payload['arrival_recovery_supported'])
        self.assertEqual(payload['watching'], 0)
        self.assertEqual(self.watched('reno'), [])
        self.assertEqual(self.http.calls, [])
        self.assertEqual(len(self.comat.history_sync.rows(self.now)), 1)
        self.available_event()
        await self.tick()
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['aibt'], self.actual_aibt)
        self.assertEqual(self.watched('reno'), [])
        self.comat.sender.assert_not_awaited()

    async def test_history_id_unknown_creation_and_deletion_are_preserved(self):
        f = await self.prepare(archived=True)
        row = self.comat.db.execute('SELECT identity,payload FROM comat_app_history').fetchone()
        original = json.loads(row['payload'])
        original['_created_at_proven'] = False
        with self.comat.db:
            self.comat.db.execute('UPDATE comat_app_history SET payload=? WHERE identity=?', (json.dumps(original), row['identity']))
        request = dict(self.flight, id=original['id'], created_at=0, repair_arrival=True, recovery_only=True)
        await self.own(flights=[request])
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['created_at'], 0)
        self.comat.shared_monitoring.remove([dict(f, deleted_at=self.now+1)], self.now+1)
        await self.own(flights=[request])
        self.available_event()
        await self.tick(2)
        self.assertEqual(self.comat.history_sync.rows(self.now+2), [])
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.watched('reno'), [])

    async def test_recovery_request_requires_approval_and_does_not_revive_expired_flights(self):
        request = dict(self.flight, repair_arrival=True, recovery_only=True)
        response = await self.watch(flights=[request], server_polling=True)
        self.assertEqual(response.status, 403)
        await self.own(flights=[dict(request, ata=self.now-86401, atd=self.now-90000, reference=self.now-90000)])
        self.assertEqual(self.comat.history_sync.rows(self.now), [])
        self.assertEqual(self.watched('reno'), [])
        self.assertEqual(self.http.calls, [])

    async def test_old_reference_and_legacy_client_history_alias_resolve_only_to_trusted_leg(self):
        f = await self.prepare(archived=True, reference=self.now-3*86400)
        original = self.comat.history_sync.rows(self.now)[0]
        legacy_id = 'PI_HISTORY_'+'f'*64
        request = dict(f, id=legacy_id, created_at=0, repair_arrival=True, recovery_only=True)
        await self.own(flights=[request])
        self.assertEqual(self.watched('reno'), [])
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['id'], original['id'])
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['reference'], f['reference'])
        await self.own(flights=[dict(request, id='PI_HISTORY_'+'e'*64, fr24ID='foreign-leg')])
        self.assertEqual(len(self.comat.history_sync.rows(self.now)), 1)
        self.available_event()
        await self.tick()
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['aibt'], self.actual_aibt)
        self.comat.sender.assert_not_awaited()

    async def test_early_actual_arrival_keeps_its_slightly_future_plan_reference(self):
        request = dict(self.flight, reference=self.now+300, repair_arrival=True, recovery_only=True)
        await self.own(flights=[request])
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['reference'], self.now+300)
        self.assertEqual(self.watched('reno'), [])
        self.available_event()
        await self.tick()
        self.assertEqual(self.comat.history_sync.rows(self.now)[0]['aibt'], self.actual_aibt)
        self.comat.sender.assert_not_awaited()

    async def test_expired_ata_only_archive_stops_requests_and_reports_the_actual_limit(self):
        await self.prepare(archived=True)
        await self.tick()
        count = len(self.http.calls)
        await self.tick(86400)
        self.assertEqual(len(self.http.calls), count)
        item = self.comat.history_sync.rows(self.now+86400)[0]
        self.assertEqual(item['_parking_recovery']['state'], 'expired')
        self.assertIsNone(item['_parking_recovery']['next_attempt'])
        self.assertNotIn('AIBT bestätigt', item['_parking_lookup_status'])

    async def test_archive_budget_and_approval_or_delete_races_still_apply_without_aibt(self):
        f = await self.prepare(archived=True)
        owner = hashlib.sha256(b'private-api-token').hexdigest()
        self.provider.requests[owner] = [self.now]*8
        self.available_event()
        await self.tick()
        self.assertEqual(self.http.calls, [])
        self.provider.requests.clear()
        recovered = dict(aibt=self.actual_aibt, aibt_source='FlightStats',
                         aibt_position=dict(latitude=52.36507, longitude=13.5053,
                                            timestamp=self.actual_aibt, source='adsb_lol'))
        async def revoke(*args, **kwargs):
            self.allowed.remove('reno')
            return recovered
        self.arrival_lookup.side_effect = revoke
        await self.tick(61)
        self.assertNotIn('aibt', self.comat.history_sync.rows(self.now+61)[0])
        self.allowed.add('reno')
        self.provider.cache.clear(); self.comat.shared_results.clear()
        self.provider.arrival_results.clear(); self.provider.arrival_started.clear()
        async def delete(*args, **kwargs):
            self.comat.shared_monitoring.remove([dict(f, deleted_at=self.now+123)], self.now+123)
            return recovered
        self.arrival_lookup.side_effect = delete
        await self.tick(123)
        self.assertEqual(self.comat.history_sync.rows(self.now+124), [])
        self.assertEqual(self.http.calls, [], 'A blocked FR24 leg stays blocked during alternative-source races')
        self.comat.sender.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
