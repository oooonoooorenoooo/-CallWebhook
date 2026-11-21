"""Independent arrival handoff contracts; synthetic transport and occurrence data."""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from push_relay.aibt_recovery import exhaustion_marker, fr24_exhausted, position
from push_relay.comatalarm import FlightProvider, transitions
from push_relay.fr24_batch import planned_queries
from push_relay.history_sync import HistorySync
from push_relay.arrival_sources import ArrivalSources, adsb_samples
from push_relay.tests.test_arrival_recovery import ArrivalHTTP
from push_relay.tests import test_shared_monitoring as shared_fixtures


NOW = datetime(2026, 11, 20, 17, tzinfo=timezone.utc).timestamp()


def occurrence(**changes):
    flight = dict(id='synthetic-phone-row', number='LH908', reference=NOW-3600,
                  created_at=NOW-7200, fr24ID='abc123', registration='D-TEST',
                  origin='FRA', destination='BER', atd=NOW-3000, ata=NOW-600,
                  _central=True)
    flight.update(changes)
    return flight


class ArrivalMarkerContracts(unittest.TestCase):
    def test_marker_survives_json_roundtrip_and_is_bound_to_the_occurrence(self):
        flight = occurrence()
        marker = exhaustion_marker(flight, NOW, 'no_data')
        restored = json.loads(json.dumps(dict(flight, fr24_arrival_exhausted=marker)))
        self.assertTrue(fr24_exhausted(restored))
        self.assertEqual(set(marker), {'fr24_id', 'flight_number', 'reference_day',
                                      'registration', 'at', 'reason'})
        for change in (dict(fr24ID='another-real-leg'), dict(number='LH909'),
                       dict(reference=flight['reference']+86400), dict(registration='D-OTHER')):
            with self.subTest(change=change):
                self.assertFalse(fr24_exhausted(dict(restored, **change)))
        restored.pop('registration')
        self.assertTrue(fr24_exhausted(restored), 'An absent optional field cannot undo the same-leg marker')

    def test_exhausted_leg_is_not_replanned_in_any_arrival_poll_mode(self):
        flight = occurrence()
        flight['fr24_arrival_exhausted'] = exhaustion_marker(flight, NOW)
        for fields in ({}, {'_arrival_fast': True}, {'_parking_only': True}):
            with self.subTest(fields=fields):
                self.assertEqual(planned_queries(dict(flight, **fields), NOW+60), [])

    def test_previous_empty_receipt_migrates_without_needing_another_paid_reply(self):
        flight = occurrence(_arrival_event_received_at=NOW-10, _arrival_event_status='no_match')
        self.assertTrue(fr24_exhausted(flight))
        self.assertEqual(planned_queries(dict(flight, _arrival_fast=True), NOW+60), [])

    def test_historic_aibt_cannot_accept_a_current_position(self):
        aibt = NOW-3600
        flight = occurrence(ata=aibt-180, aibt=aibt, aibt_source='FlightStats',
                            aibt_position=dict(latitude=52.36507, longitude=13.5053,
                                               timestamp=NOW, source='adsb_lol'))
        self.assertIsNone(position(flight))
        flight['aibt_position']['timestamp'] = aibt-5
        self.assertIsNotNone(position(flight))

    def test_parking_transition_waits_for_valid_position_and_stand_with_stable_event_key(self):
        before = occurrence()
        timed = dict(before, aibt=NOW-300, aibt_source='FlightStats')
        point = dict(latitude=52.36507, longitude=13.5053, timestamp=NOW-305,
                     source='adsb_lol', on_ground=True, ground_speed=0.0)
        incomplete = [timed, dict(timed, gate='B06'), dict(timed, stand='B06'),
                      dict(timed, aibt_position=point),
                      dict(timed, aibt_position=point, stand=' '),
                      dict(timed, aibt_position=dict(point, timestamp=NOW), stand='B06')]
        for current in incomplete:
            with self.subTest(current=current):
                self.assertFalse(any(event[1] == 'alarmParking' for event in transitions(before, current)))
        complete = dict(timed, aibt_position=point, stand='B06')
        self.assertEqual([event[:2] for event in transitions(timed, complete)], [('aibt', 'alarmParking')])
        self.assertEqual(transitions(complete, dict(complete)), [])
        self.assertEqual(transitions(complete, dict(complete, stand='B07')), [],
                         'A stand correction cannot create a second parking milestone')


class ArrivalProviderContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flight = occurrence()
        self.http = ArrivalHTTP(NOW, NOW-300)
        self.provider = self.new_provider()
        for target, replacement in (
                ('push_relay.comatalarm.ClientSession', self.http),
                ('push_relay.flight_schedule.schedule_lookup', AsyncMock(return_value={})),
                ('push_relay.flight_schedule.aware_lookup', AsyncMock(return_value={}))):
            context = patch(target, replacement)
            context.start()
            self.addCleanup(context.stop)

    @staticmethod
    def new_provider():
        provider = FlightProvider()
        provider.arrivals.lookup = AsyncMock(return_value={})
        return provider

    async def test_first_empty_reply_stops_other_device_and_token_after_cache_expiry(self):
        first = await self.provider.lookup(self.flight, 'synthetic-token-one', NOW)
        self.assertIn('fr24_arrival_exhausted', first)
        self.assertNotIn('aibt', first)
        self.assertEqual(len(self.http.calls), 1)
        # A stale second device has not yet received the marker. The provider's
        # shared occurrence record still prevents another paid query.
        self.http.now = NOW+121
        second = await self.provider.lookup(dict(self.flight, id='other-phone-row'),
                                            'synthetic-token-two', NOW+121)
        self.assertIn('fr24_arrival_exhausted', second)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.provider.arrivals.lookup.await_count, 2)

    async def test_persisted_marker_blocks_fr24_after_restart_even_with_new_token(self):
        first = await self.provider.lookup(self.flight, 'synthetic-token-one', NOW)
        persisted = json.loads(json.dumps(dict(self.flight, **first)))
        restarted = self.new_provider()
        self.http.now = NOW+3601
        await restarted.lookup(persisted, 'rotated-synthetic-token', NOW+3601)
        self.assertEqual(len(self.http.calls), 1)
        restarted.arrivals.lookup.assert_awaited_once()

    async def test_exhaustion_of_one_leg_does_not_block_another_real_leg(self):
        await self.provider.lookup(self.flight, 'synthetic-token', NOW)
        other = dict(self.flight, fr24ID='other-real-leg', registration='D-OTHER')
        self.http.now = NOW+61
        await self.provider.lookup(other, 'synthetic-token', NOW+61)
        self.assertEqual([call[2]['flight_ids'] for call in self.http.calls], ['abc123', 'other-real-leg'])

    async def test_partial_event_keeps_real_aibt_and_never_queries_fr24_coordinates(self):
        self.http.events = [dict(type='gate_arrival', timestamp=NOW-300)]
        result = await self.provider.lookup(self.flight, 'synthetic-token', NOW)
        self.assertEqual(result['aibt'], NOW-300)
        self.assertEqual(result['aibt_source'], 'FR24')
        self.assertEqual(result['fr24_arrival_exhausted']['reason'], 'incomplete')
        self.assertNotIn('aibt_position', result)
        self.assertEqual([call[1] for call in self.http.calls], ['historic/flight-events/light'])
        self.assertEqual(self.provider.arrivals.lookup.call_args.args[1]['aibt'], NOW-300)

    async def test_complete_event_needs_no_external_gate_or_stand_code(self):
        self.http.events = [dict(type='gate_arrival', timestamp=NOW-300,
                                 details=dict(gate_ident=None, gate_lat=52.36507, gate_lon=13.5053))]
        result = await self.provider.lookup(self.flight, 'synthetic-token', NOW)
        self.assertEqual(result['aibt'], NOW-300)
        self.assertEqual(result['aibt_position']['timestamp'], NOW-300)
        self.assertNotIn('gate', result)
        self.assertNotIn('stand', result)
        self.assertNotIn('fr24_arrival_exhausted', result)
        self.provider.arrivals.lookup.assert_not_awaited()

    async def test_runway_arrival_and_gate_estimate_are_never_substituted_for_aibt(self):
        flight = dict(self.flight, _schedule={'ata': NOW-600, 'eibt': NOW-300, 'eta': NOW-500})
        result = await self.provider.lookup(flight, 'synthetic-token', NOW)
        self.assertNotIn('aibt', result)
        self.assertNotIn('aibt_position', result)

    async def test_stale_batch_plan_cannot_send_an_exhausted_leg_with_another_flight(self):
        first = dict(self.flight, _arrival_fast=True)
        other = dict(first, number='LH909', fr24ID='other-real-leg', registration='D-OTHER')
        entries = [(first, 'synthetic-token'), (other, 'synthetic-token')]
        self.provider.plan_batches(entries, NOW)
        await self.provider.lookup(first, 'synthetic-token', NOW)
        self.assertIn('abc123', self.http.calls[0][2]['flight_ids'].split(','))
        # The first leg has no marker in this stale scheduling snapshot; the
        # provider nevertheless already knows its first reply was unusable.
        self.http.now = NOW+61
        self.provider.plan_batches(entries, NOW+61)
        await self.provider.lookup(other, 'synthetic-token', NOW+61)
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(self.http.calls[1][2]['flight_ids'], 'other-real-leg')

    async def test_saved_ground_sample_survives_history_and_helper_restart(self):
        flight = occurrence(aibt=NOW-300, aibt_source='FlightStats', icao24='abc456')
        observed = adsb_samples({'now': NOW-300, 'ac': [dict(hex='abc456', r='D-TEST',
            flight='DLH908', lat=52.36507, lon=13.5053, gs=0, alt_baro='ground', seen_pos=5)]},
            flight, NOW-300)
        self.assertEqual(len(observed), 1)
        saved = HistorySync.sanitize(dict(flight, _arrival_position_samples=observed), NOW)
        restored = json.loads(json.dumps(saved))
        sources = ArrivalSources(SimpleNamespace(credentials={}, cooldown={}))
        # With an AIBT five minutes old, no OAuth credentials and no FlightAware
        # key, the only valid answer is this actual stored sample.
        result = await sources.lookup(object(), restored, NOW)
        self.assertEqual(result['aibt_position']['timestamp'], NOW-305)
        self.assertEqual(result['aibt_position']['source'], 'adsb_lol')
        self.assertEqual(result['_arrival_sources_status'], 'complete')

    async def test_delete_readd_generation_cannot_reuse_outer_provider_result_cache(self):
        provider = self.provider
        provider.background_schedules = True
        self.addAsyncCleanup(provider.close_arrivals)
        first_context = dict(self.flight, _parking_only=True,
                             _arrival_token_digest='synthetic-context', _arrival_generation=0)
        first_context['fr24_arrival_exhausted'] = exhaustion_marker(first_context, NOW-10)
        ready = dict(aibt=NOW-300, aibt_source='FlightStats', _arrival_sources_checked_at=NOW-1,
                     aibt_position=dict(latitude=52.36507, longitude=13.5053,
                                        timestamp=NOW-300, source='adsb_lol'))
        provider.arrival_results[provider.arrival_key(first_context)] = (NOW-1, ready)
        first = await provider.lookup(first_context, 'synthetic-token', NOW)
        self.assertEqual(first['aibt'], NOW-300)

        # The background cache correctly separates a deletion followed by an
        # explicit re-add. The outer lookup cache must honor the same context.
        readded = dict(first_context, _arrival_generation=NOW+0.5)
        self.assertEqual(provider.arrival_changes(readded, NOW+1), {})
        second = await provider.lookup(readded, 'synthetic-token', NOW+1)
        self.assertNotIn('aibt', second)
        self.assertNotIn('aibt_position', second)
        self.assertIn('fr24_arrival_exhausted', second)
        self.assertEqual(self.http.calls, [])


class ArrivalPersistenceContracts(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = shared_fixtures.SharedMonitoringTests.asyncSetUp
    asyncTearDown = shared_fixtures.SharedMonitoringTests.asyncTearDown
    register = shared_fixtures.SharedMonitoringTests.register
    watch = shared_fixtures.SharedMonitoringTests.watch
    add_peer = shared_fixtures.SharedMonitoringTests.add_peer
    own = shared_fixtures.SharedMonitoringTests.own
    row = shared_fixtures.SharedMonitoringTests.row
    watched = shared_fixtures.SharedMonitoringTests.watched

    async def test_stale_watch_repost_and_peer_keep_the_same_leg_marker(self):
        self.flight.update(fr24ID='shared-real-leg', registration='D-TEST',
                           atd=self.now-3600, ata=self.now-600)
        marker = exhaustion_marker(self.flight, self.now)
        await self.own(flights=[self.flight])
        await self.add_peer(dict(self.flight, id='peer-row'), firebase_token='colleague',
                            server_polling=True, relay_alerts=True)
        self.comat.provider.lookup.return_value = dict(fr24_arrival_exhausted=marker,
                                                       _arrival_sources_checked_at=self.now)
        await self.comat.tick(self.now+1)
        await self.own(flights=[self.flight])
        for uid in ('reno', 'colleague'):
            self.assertEqual(self.watched(uid)[0]['fr24_arrival_exhausted'], marker)

    async def test_archive_reload_and_stale_merge_preserve_source_marker_and_position(self):
        await self.own()
        flight = dict(self.flight, fr24ID='shared-real-leg', registration='D-TEST',
                      atd=self.now-3600, ata=self.now-600, aibt=self.now-300,
                      aibt_source='FlightStats', aibt_position=dict(latitude=52.36507,
                          longitude=13.5053, timestamp=self.now-305, source='adsb_lol'))
        flight['fr24_arrival_exhausted'] = exhaustion_marker(flight, self.now-240)
        self.comat.history_sync.capture([flight], self.now)
        self.comat.history_sync = HistorySync(self.comat)
        stale = {k: v for k, v in flight.items() if k not in
                 ('aibt', 'aibt_source', 'aibt_position', 'fr24_arrival_exhausted')}
        self.comat.history_sync.capture([stale], self.now+1)
        saved = self.comat.history_sync.rows(self.now+1)[0]
        self.assertEqual(saved['fr24_arrival_exhausted'], flight['fr24_arrival_exhausted'])
        self.assertEqual(saved['aibt_source'], 'FlightStats')
        self.assertEqual(saved['aibt_position']['source'], 'adsb_lol')
        self.assertEqual(saved['aibt_position']['timestamp'], self.now-305)


if __name__ == '__main__':
    unittest.main()
