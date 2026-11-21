"""Real in-block evidence and timed observations without any live API calls."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from push_relay.arrival_sources import (ArrivalSources, adsb_samples, opensky_samples,
    aware_track_samples, select_position, MAX_SAMPLES, DAILY_LIMITS, MAX_BODY)
from push_relay.flight_schedule import timestamp


NOW = timestamp('2026-11-20T17:00:00Z')
AIBT = timestamp('2026-11-20T16:59:30Z')


def flight(**changes):
    return dict(dict(number='LH194', reference=timestamp('2026-11-20T16:00:00Z'), origin='FRA',
                     destination='BER', registration='D-AIDA', icao24='3c6424', callsign='DLH6HF',
                     ata=timestamp('2026-11-20T16:51:21Z')), **changes)


def adsb(now=NOW, **changes):
    row = dict(hex='3c6424', r='D-AIDA', flight='DLH6HF ', lat=52.366, lon=13.505,
               alt_baro='ground', gs=0, seen_pos=30)
    row.update(changes)
    return {'now': now, 'ac': [row]}


def opensky(observed=AIBT, callsign='DLH6HF', ground=True, speed=0, hex='3c6424'):
    return {'time': observed, 'states': [[hex, callsign, 'Germany', observed, observed,
            13.505, 52.366, 48, ground, speed, 90, 0, None]]}


def aware_track(observed=AIBT, **changes):
    row = dict(fa_flight_id=None, latitude=52.366, longitude=13.505, timestamp=observed,
               groundspeed=0, altitude=2, update_type='X')
    row.update(changes)
    return {'positions': [row]}


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return deepcopy(self.body)


class HTTP:
    def __init__(self, responses=()):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        return self.request('GET', url, kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, kwargs)

    def request(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError('Unexpected network request: '+url)
        response = self.responses.pop(0)
        return response if isinstance(response, Response) else Response(response)


class PositionEvidenceTests(unittest.TestCase):
    def test_radio_alias_is_not_the_commercial_flight_number(self):
        for changes, callsign in (({}, 'DLH6HF'), ({'number': 'LH192', 'callsign': 'DLH6LJ'}, 'DLH6LJ'),
                                   ({'callsign': None}, 'DLH6HF')):
            f = flight(**changes)
            self.assertEqual(len(adsb_samples(adsb(flight=callsign), f, NOW)), 1)
            self.assertEqual(len(opensky_samples(opensky(callsign=callsign), f, NOW, NOW)), 1)
        self.assertEqual(adsb_samples(adsb(flight='DLH6LJ'), flight(), NOW), [])
        self.assertEqual(adsb_samples(adsb(flight='DLH196'), flight(callsign=None), NOW), [])

    def test_position_requires_aibts_own_clock_and_ground_evidence(self):
        f = flight(aibt=AIBT)
        samples = adsb_samples(adsb(), f, NOW)
        self.assertEqual(select_position(samples, f), dict(latitude=52.366, longitude=13.505,
            timestamp=AIBT, source='adsb_lol', on_ground=True, ground_speed=0.0))
        self.assertIsNone(select_position(samples, flight()))
        for changes in ({'seen_pos': 91}, {'seen_pos': None, 'seen': 30}, {'gs': 3}, {'gs': None},
                        {'gs': True}, {'alt_baro': 150}, {'lat': True}, {'lon': float('nan')},
                        {'hex': '3c6425'}, {'r': 'D-AIDB'}, {'lat': 50}, {'flight': 'DLH6LJ'}):
            with self.subTest(changes=changes):
                parsed = adsb_samples(adsb(**changes), f, NOW)
                self.assertIsNone(select_position(parsed, f))
        root = adsb(now=NOW*1000)
        self.assertEqual(adsb_samples(root, f, NOW)[0]['timestamp'], AIBT)
        root.pop('now')
        self.assertEqual(adsb_samples(root, f, NOW), [])

    def test_samples_before_touchdown_and_beyond_sixty_seconds_are_rejected(self):
        f = flight(aibt=AIBT, ata=AIBT-15)
        self.assertEqual(adsb_samples(adsb(seen_pos=50), f, NOW), [])
        for distance, accepted in ((-60, True), (60, True), (-60.01, False), (60.01, False)):
            with self.subTest(distance=distance):
                points = aware_track_samples(aware_track(observed=AIBT+distance), flight(aibt=AIBT), NOW+90, 'exact-leg')
                self.assertEqual(select_position(points, flight(aibt=AIBT)) is not None, accepted)

    def test_opensky_uses_position_time_not_last_contact_or_fetch_time(self):
        f = flight(aibt=AIBT)
        root = opensky()
        root['time'] = NOW
        root['states'][0][4] = NOW
        self.assertEqual(opensky_samples(root, f, NOW, NOW)[0]['timestamp'], AIBT)
        root['states'][0][3] = AIBT-200
        self.assertEqual(opensky_samples(root, f, NOW, NOW), [])
        root = opensky(observed=NOW)
        self.assertEqual(opensky_samples(root, f, NOW, NOW-1800), [])
        self.assertEqual(opensky_samples(opensky(ground=False), f, NOW, NOW), [])
        self.assertEqual(opensky_samples(opensky(speed=2), f, NOW, NOW), [])

    def test_aware_track_rejects_projection_airborne_movement_and_wrong_flight(self):
        f = flight(aibt=AIBT)
        self.assertEqual(len(aware_track_samples(aware_track(), f, NOW, 'exact-leg')), 1)
        for change in ({'update_type': 'P'}, {'update_type': 'A'}, {'altitude': 3}, {'altitude': None},
                       {'groundspeed': 3}, {'groundspeed': None}, {'fa_flight_id': 'other-leg'},
                       {'timestamp': None}, {'latitude': True}, {'longitude': float('inf')}):
            self.assertEqual(aware_track_samples(aware_track(**change), f, NOW, 'exact-leg'), [])


class ArrivalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.aware, self.stats = AsyncMock(return_value={}), AsyncMock(return_value={})
        self.provider = SimpleNamespace(credentials={}, cooldown={})
        self.sources = ArrivalSources(self.provider, aware_lookup=self.aware, schedule_lookup=self.stats)
        self.addAsyncCleanup(self.sources.close)

    async def test_actual_aibt_and_coordinate_are_combined_in_the_same_call_without_gate(self):
        self.stats.return_value = {'aibt': AIBT, 'aibt_source': 'FlightStats', 'gate': 'A07', 'stand': 'WRONG'}
        http = HTTP([adsb()])
        original = flight()
        result = await self.sources.lookup(http, original, NOW)
        self.assertEqual(result['aibt'], AIBT)
        self.assertEqual(result['aibt_source'], 'FlightStats')
        self.assertEqual(result['_arrival_sources_status'], 'complete')
        self.assertEqual(result['aibt_position']['source'], 'adsb_lol')
        self.assertNotIn('gate', result)
        self.assertNotIn('stand', result)
        self.assertEqual(original, flight(), 'Lookup must not mutate the watch')
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(all('fr24' not in call[1] for call in http.calls))
        self.assertTrue(all(call[2]['allow_redirects'] is False for call in http.calls))

    async def test_flightaware_actual_in_has_priority_and_missing_aibt_falls_through(self):
        self.provider.credentials = {'FLIGHTAWARE_API_KEY': 'private-key'}
        self.aware.return_value = {'aibt': AIBT, 'aibt_source': 'FlightAware'}
        result = await self.sources.lookup(HTTP([adsb()]), flight(), NOW)
        self.assertEqual(result['aibt_source'], 'FlightAware')
        self.stats.assert_not_awaited()
        self.assertNotIn('private-key', json.dumps(result))
        self.aware.return_value = {'eibt': AIBT}
        self.stats.return_value = {'aibt': AIBT+1, 'aibt_source': 'FlightStats'}
        result = await self.sources.lookup(HTTP([adsb()]), flight(number='LH192', callsign='DLH6HF'), NOW)
        self.assertEqual(result['aibt_source'], 'FlightStats')
        self.assertEqual(result['aibt'], AIBT+1)

    async def test_existing_confirmed_aibt_is_preserved_and_does_not_query_stats(self):
        self.provider.credentials = {'FLIGHTAWARE_API_KEY': 'private-key'}
        self.aware.return_value = {'aibt': AIBT+10, 'aibt_source': 'FlightAware', 'flightawareID': 'exact-leg'}
        result = await self.sources.lookup(HTTP([adsb()]), flight(aibt=AIBT, aibt_source='FR24'), NOW)
        self.assertEqual((result['aibt'], result['aibt_source']), (AIBT, 'FR24'))
        self.stats.assert_not_awaited()

    async def test_actual_arrival_before_scheduled_reference_is_eligible(self):
        self.stats.return_value = {'aibt': AIBT, 'aibt_source': 'FlightStats'}
        result = await self.sources.lookup(HTTP([adsb()]), flight(reference=NOW+900), NOW)
        self.assertEqual(result['_arrival_sources_status'], 'complete')

    async def test_no_aibt_never_manufactures_one_from_eibt_gate_or_standstill(self):
        self.stats.return_value = {'eibt': AIBT, 'eta': AIBT, 'ata': AIBT-120, 'gate': 'B13'}
        result = await self.sources.lookup(HTTP([adsb(), opensky()]), flight(), NOW)
        self.assertNotIn('aibt', result)
        self.assertNotIn('aibt_position', result)
        self.assertEqual(result['_arrival_sources_status'], 'no_aibt')
        self.assertTrue(result['_arrival_position_samples'])

    async def test_persisted_samples_recover_after_restart_without_live_queries(self):
        first = await self.sources.lookup(HTTP([adsb(), opensky()]), flight(), NOW)
        restarted = ArrivalSources(self.provider, aware_lookup=self.aware, schedule_lookup=self.stats)
        self.addAsyncCleanup(restarted.close)
        self.stats.return_value = {'aibt': AIBT, 'aibt_source': 'FlightStats'}
        saved = json.loads(json.dumps(dict(flight(), **first)))
        result = await restarted.lookup(HTTP(), saved, NOW+90)
        self.assertEqual(result['_arrival_sources_status'], 'complete')
        self.assertEqual(result['aibt_position']['timestamp'], AIBT)
        self.assertEqual(result['aibt_position']['ground_speed'], 0)

    async def test_stored_position_history_cannot_cross_day_registration_route_or_number(self):
        stored = adsb_samples(adsb(), flight(), NOW)
        for change in ({'number': 'LH192'}, {'registration': 'D-AIDB'}, {'origin': 'MUC'},
                       {'reference': timestamp('2026-11-19T16:00:00Z')}, {'icao24': '3c6425'}):
            with self.subTest(change=change):
                f = flight(aibt=AIBT, aibt_source='FlightStats', _arrival_position_samples=stored, **change)
                # AIBT now older than a minute: no live query and no configured historical API.
                result = await self.sources.lookup(HTTP(), f, NOW+120)
                self.assertNotIn('aibt_position', result)

    async def test_explicit_leg_ids_and_request_generations_do_not_share_tasks_or_samples(self):
        f = flight(fr24ID='opaque-actual-leg-A')
        stored = adsb_samples(adsb(), f, NOW)
        other = flight(fr24ID='opaque-actual-leg-B', aibt=AIBT, _arrival_position_samples=stored)
        result = await self.sources.lookup(HTTP(), other, NOW+120)
        self.assertNotIn('aibt_position', result)
        self.assertNotEqual(self.sources._key(f, {}), self.sources._key(other, {}))
        self.assertNotEqual(self.sources._key(f, {}), self.sources._key(dict(f, _arrival_generation=NOW), {}))
        self.assertNotEqual(self.sources._key(dict(f, _arrival_token_digest='a'), {}),
                            self.sources._key(dict(f, _arrival_token_digest='b'), {}))
        self.assertEqual(self.sources._key(flight(fr24ID='TEMP_ONE'), {}), self.sources._key(flight(fr24ID='NS_TWO'), {}))

    async def test_in_memory_history_is_rechecked_after_new_ata_or_radio_identity(self):
        f = flight(callsign=None)
        key = self.sources._key(f, {})
        stored = adsb_samples(adsb(), f, NOW)
        self.sources.history[key] = stored
        self.assertEqual(self.sources._remember(dict(f, callsign='DLH6LJ'), key, [], NOW), [])
        self.sources.history[key] = stored
        self.assertEqual(self.sources._remember(dict(f, ata=AIBT+1), key, [], NOW), [])

    async def test_historical_aibt_uses_exact_track_and_never_todays_live_aircraft(self):
        self.provider.credentials = {'FLIGHTAWARE_API_KEY': 'test-key', 'OPENSKY_CLIENT_ID': 'id', 'OPENSKY_CLIENT_SECRET': 'secret'}
        f = flight(aibt=AIBT, aibt_source='FlightAware', flightawareID='exact-leg')
        http = HTTP([aware_track(observed=NOW+20*3600)])
        result = await self.sources.lookup(http, f, NOW+20*3600)
        self.assertNotIn('aibt_position', result)
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(http.calls[0][1].endswith('/exact-leg/track'))
        self.assertEqual(http.calls[0][2]['params'], {'include_estimated_positions': 'false'})
        self.aware.assert_not_awaited(); self.stats.assert_not_awaited()
        self.sources.cache.clear()
        result = await self.sources.lookup(HTTP([aware_track()]), f, NOW+20*3600+1)
        self.assertEqual(result['aibt_position']['timestamp'], AIBT)

    async def test_opensky_history_needs_credentials_and_preserves_timestamp(self):
        self.provider.credentials = {'OPENSKY_CLIENT_ID': 'client', 'OPENSKY_CLIENT_SECRET': 'private-secret'}
        f = flight(aibt=AIBT, aibt_source='FlightStats')
        http = HTTP([{'access_token': 'private-access-token', 'expires_in': 1800}, opensky()])
        result = await self.sources.lookup(http, f, NOW+600)
        self.assertEqual(result['aibt_position']['timestamp'], AIBT)
        self.assertEqual(http.calls[0][0], 'POST')
        self.assertEqual(http.calls[1][2]['params']['time'], int(AIBT))
        self.assertNotIn('private-', json.dumps(result))
        self.provider.credentials = {}
        again = await self.sources.lookup(HTTP(), f, NOW+610)
        self.assertNotIn('aibt_position', again, 'Private credential histories must not cross credential sets')

    async def test_same_occurrence_queries_are_coalesced_and_cached_across_phones(self):
        self.stats.return_value = {'aibt': AIBT, 'aibt_source': 'FlightStats'}
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            entered.set()
            await release.wait()
            return self.stats.return_value
        self.stats.side_effect = blocked
        http = HTTP([adsb()])
        first = asyncio.create_task(self.sources.lookup(http, flight(id='one'), NOW))
        await entered.wait()
        second = asyncio.create_task(self.sources.lookup(http, flight(id='two'), NOW))
        await asyncio.sleep(0)
        release.set()
        left, right = await asyncio.gather(first, second)
        self.assertEqual(left, right)
        self.assertEqual(self.stats.await_count, 1)
        self.assertEqual(len(http.calls), 1)
        left['aibt_position']['latitude'] = 0
        self.assertEqual(right['aibt_position']['latitude'], 52.366)
        again = await self.sources.lookup(HTTP(), flight(id='third'), NOW+1)
        self.assertEqual(again['aibt_position']['latitude'], 52.366)
        self.assertEqual(self.stats.await_count, 1)

    async def test_shared_minute_budget_cooldown_and_rotation_are_respected(self):
        f = flight(icao24=None, registration=None)
        for index in range(10):
            await self.sources.lookup(HTTP(), dict(f, number='LH'+str(190+index)), NOW)
        self.assertEqual(self.stats.await_count, 8)
        self.provider.cooldown['flightstats'] = NOW+300
        await self.sources.lookup(HTTP(), dict(f, number='LH300'), NOW+61)
        self.assertEqual(self.stats.await_count, 8)
        self.provider.cooldown.clear()
        self.provider.credentials = {'FLIGHTSTATS_APP_KEY': 'rotated-key'}
        await self.sources.lookup(HTTP(), f, NOW+62)
        self.assertEqual(self.stats.await_count, 9)

    async def test_daily_limits_bound_community_requests_and_leave_healthcheck_headroom(self):
        for provider, limit in DAILY_LIMITS.items():
            self.assertTrue(all(self.sources._reserve(provider, {}, NOW+index*61) for index in range(limit)))
            self.assertFalse(self.sources._reserve(provider, {}, NOW+limit*61))
        self.assertLess(DAILY_LIMITS['adsb'], 500)
        self.assertLess(DAILY_LIMITS['opensky'], 400)

    async def test_provider_429_backoff_is_shared_and_provider_body_does_not_leak(self):
        self.stats.return_value = {'aibt': AIBT, 'aibt_source': 'FlightStats'}
        http = HTTP([Response({'secret': 'not-for-logs'}, 429, {'Retry-After': '1200'}), opensky()])
        result = await self.sources.lookup(http, flight(), NOW)
        self.assertGreaterEqual(self.provider.cooldown['adsb'], NOW+1200)
        self.assertEqual(result['_arrival_sources_status'], 'complete')
        self.assertNotIn('not-for-logs', json.dumps(result))

    async def test_large_response_and_callback_timeout_leave_actual_aibt_intact(self):
        self.stats.return_value = {'aibt': AIBT, 'aibt_source': 'FlightStats'}
        http = HTTP([{'large': 'x'*(MAX_BODY+1)}, opensky()])
        result = await self.sources.lookup(http, flight(), NOW)
        self.assertEqual(result['aibt'], AIBT)
        self.assertEqual(result['aibt_position']['source'], 'opensky')
        async def blocked(*args): await asyncio.Event().wait()
        self.stats.side_effect = blocked
        with patch('push_relay.arrival_sources.REQUEST_TIMEOUT', 0.01):
            result = await self.sources.lookup(HTTP(), flight(number='LH196', icao24=None, registration=None), NOW)
        self.assertNotIn('aibt', result)

    async def test_lookups_and_restored_history_are_memory_bounded(self):
        f = flight(aibt=AIBT, aibt_source='FlightStats')
        samples = []
        for index in range(100):
            row = adsb_samples(adsb(now=NOW+index*30, seen_pos=0), f, NOW+index*30)
            samples.extend(row)
        key = self.sources._key(f, {})
        saved = self.sources._remember(f, key, samples, NOW+3000)
        self.assertLessEqual(len(saved), MAX_SAMPLES)
        self.assertEqual(len({int(point['timestamp']//30) for point in saved}), len(saved))

    async def test_closed_cancelled_diverted_outbound_and_old_flights_do_not_query(self):
        for changes in ({'cancelled': True}, {'diverted': True}, {'origin': 'BER', 'destination': 'FRA'},
                        {'number': '../../secret'}, {'reference': NOW-3*86400}):
            result = await self.sources.lookup(HTTP(), flight(**changes), NOW)
            self.assertEqual(result['_arrival_sources_status'], 'ineligible')
        await self.sources.close()
        self.assertEqual((await self.sources.lookup(HTTP(), flight(), NOW))['_arrival_sources_status'], 'ineligible')
        self.stats.assert_not_awaited()


class ActualHTTPContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual parsers, source order and position transport together."""
    def aware(self, **changes):
        return {'flights': [dict(dict(ident='DLH194', ident_iata='LH194',
            scheduled_out='2026-11-20T16:00:00Z', origin={'code_iata': 'FRA'},
            destination={'code_iata': 'BER'}, registration='DAIDA',
            actual_on='2026-11-20T16:51:21Z', actual_in='2026-11-20T16:59:30Z',
            fa_flight_id='DLH194-1795190400-airline-1234', gate_destination='A07'), **changes)]}

    def stats(self):
        return {'flightStatuses': [dict(carrierFsCode='LH', flightNumber='194', flightId=1234,
            departureAirportFsCode='FRA', arrivalAirportFsCode='BER', flightEquipment={'tailNumber': 'D-AIDA'},
            airportResources={'arrivalGate': 'A07'}, operationalTimes={
            'scheduledGateDeparture': {'dateUtc': '2026-11-20T16:00:00Z'},
            'actualRunwayArrival': {'dateUtc': '2026-11-20T16:51:21Z'},
            'actualGateArrival': {'dateUtc': '2026-11-20T16:59:30Z'}})]}

    async def test_real_flightaware_actual_in_and_adslol_point_complete_without_flightstats(self):
        provider = SimpleNamespace(credentials={'FLIGHTAWARE_API_KEY': 'fixture-private-key'}, cooldown={})
        sources = ArrivalSources(provider)
        self.addAsyncCleanup(sources.close)
        http = HTTP([self.aware(), adsb()])
        result = await sources.lookup(http, flight(), NOW)
        self.assertEqual(result['aibt'], AIBT)
        self.assertEqual(result['aibt_source'], 'FlightAware')
        self.assertEqual(result['flightawareID'], 'DLH194-1795190400-airline-1234')
        self.assertEqual(result['aibt_position']['timestamp'], AIBT)
        self.assertEqual(len(http.calls), 2)
        self.assertIn('aeroapi/flights/LH194', http.calls[0][1])
        self.assertEqual(http.calls[0][2]['headers']['x-apikey'], 'fixture-private-key')
        self.assertNotIn('fixture-private-key', json.dumps(result))
        self.assertNotIn('gate', result)
        self.assertNotIn('stand', result)

    async def test_missing_flightaware_actual_falls_through_to_official_stats_and_keeps_track_id(self):
        provider = SimpleNamespace(credentials={'FLIGHTAWARE_API_KEY': 'fixture-fa',
            'FLIGHTSTATS_APP_ID': 'fixture-id', 'FLIGHTSTATS_APP_KEY': 'fixture-fs'}, cooldown={})
        sources = ArrivalSources(provider)
        self.addAsyncCleanup(sources.close)
        http = HTTP([self.aware(actual_in=None), self.stats(), adsb()])
        result = await sources.lookup(http, flight(), NOW)
        self.assertEqual((result['aibt'], result['aibt_source']), (AIBT, 'FlightStats'))
        self.assertEqual(result['flightawareID'], 'DLH194-1795190400-airline-1234')
        self.assertEqual(result['flightstatsID'], '1234')
        self.assertEqual(result['_arrival_sources_status'], 'complete')
        self.assertIn('/flight/status/LH/194/arr/2026/11/20', http.calls[1][1])
        self.assertEqual(http.calls[1][2]['params']['airport'], 'BER')
        self.assertEqual(len(http.calls), 3)

    async def test_wrong_flightaware_airframe_cannot_contribute_aibt_or_track_identity(self):
        provider = SimpleNamespace(credentials={'FLIGHTAWARE_API_KEY': 'fixture-fa',
            'FLIGHTSTATS_APP_ID': 'fixture-id', 'FLIGHTSTATS_APP_KEY': 'fixture-fs'}, cooldown={})
        sources = ArrivalSources(provider)
        self.addAsyncCleanup(sources.close)
        http = HTTP([self.aware(registration='D-AIDB'), self.stats(), adsb()])
        result = await sources.lookup(http, flight(), NOW)
        self.assertEqual(result['aibt_source'], 'FlightStats')
        self.assertNotIn('flightawareID', result)
        self.assertEqual(result['aibt_position']['source'], 'adsb_lol')


if __name__ == '__main__':
    unittest.main()
