"""Real-provider capacity, token isolation and bounded HTTP batching regressions."""
import hashlib
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from push_relay.comatalarm import FlightProvider, utc_query_time
from push_relay.fr24_batch import FR24BatchRequests, planned_queries
from push_relay.tests import test_comatalarm as fixtures
from push_relay.tests import test_shared_monitoring as shared_fixtures
from push_relay.tests.test_landing_alert import sample


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status
        self.headers = {'Content-Type': 'application/json', 'Retry-After': '120'}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.data


class ProviderHTTP:
    """Only transport is stubbed: real lookup, matching, budget and cache execute."""
    def __init__(self, flights, now):
        self.flights = {f['fr24ID']: f for f in flights}
        self.now, self.start = now, now
        self.calls = []
        self.status = 200
        self.omit = set()
        self.extra_live = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, params=None, headers=None, **kwargs):
        if 'fr24api.flightradar24.com/api/' not in url:
            return Response({'data': {'items': []}})
        path = url.split('/api/', 1)[1]
        self.calls.append((self.now, path, dict(params), headers['Authorization']))
        if self.status != 200:
            return Response({}, self.status)
        if 'flight_ids' in params:
            flights = [self.flights[i] for i in params['flight_ids'].split(',') if i in self.flights]
        elif 'flights' in params:
            flights = [f for f in self.flights.values() if f['number'] in params['flights'].split(',')]
        else:
            flights = []
        flights = [f for f in flights if f['fr24ID'] not in self.omit]
        if path == 'flight-summary/full':
            if 'flight_datetime_from' in params:
                start = datetime.fromisoformat(params['flight_datetime_from'].replace('Z', '+00:00')).timestamp()
                end = datetime.fromisoformat(params['flight_datetime_to'].replace('Z', '+00:00')).timestamp()
                flights = [f for f in flights if start <= f['atd'] <= end]
            return Response({'data': [dict(fr24_id=f['fr24ID'], flight=f['number'],
                orig_iata=f['origin'], dest_iata=f['destination'], reg=f['registration'],
                datetime_takeoff=f['atd']) for f in flights]})
        if path == 'live/flight-positions/full':
            offset = self.now-self.start
            rows = []
            for f in flights:
                live = sample(at=self.now-3, eta=self.start+240, distance=20-offset/12,
                              altitude=3000-offset*700/60)['_live']
                live.update(fr24_id=f['fr24ID'], flight=f['number'], reg=f['registration'],
                            orig_iata=f['origin'], dest_iata=f['destination'])
                rows.append(live)
            return Response({'data': rows + self.extra_live})
        return Response({'data': []})


def flights(count, now):
    return [dict(id=f'flight-{i}', number=f'LH{180+i}', reference=now-100,
                 created_at=now-120, fr24ID=f'{0xabcdef+i:x}', origin='FRA',
                 destination='BER', registration=f'D-A{i:03}', atd=now-3600)
            for i in range(count)]


class SharedProviderCapacityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = shared_fixtures.SharedMonitoringTests.asyncSetUp
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch
    add_peer = fixtures.ComatAlarmTests.add_peer
    own = shared_fixtures.SharedMonitoringTests.own
    colleague = shared_fixtures.SharedMonitoringTests.colleague
    row = shared_fixtures.SharedMonitoringTests.row
    watched = shared_fixtures.SharedMonitoringTests.watched

    async def capacity(self, count):
        tracked = flights(count, self.now)
        await self.own(flights=tracked)
        await self.colleague(flights=[])
        self.comat.provider = FlightProvider()
        http = ProviderHTTP(tracked, self.now)
        with patch('push_relay.comatalarm.ClientSession', http), \
             patch('push_relay.flight_schedule.schedule_lookup', AsyncMock(return_value={})), \
             patch('push_relay.flight_schedule.aware_lookup', AsyncMock(return_value={})):
            for offset in (0, 15, 30, 45, 60, 75, 90, 105, 120):
                http.now = self.now+offset
                await self.comat.tick(http.now)
                if offset % 60 == 0:
                    for uid in ('reno', 'colleague'):
                        saved = self.watched(uid)
                        self.assertEqual(len(saved), count)
                        self.assertTrue(all(f.get('_live', {}).get('timestamp') == http.now-3 for f in saved),
                                        f'{count} flights: every authorized recipient needs a fresh live sample at {offset}s')
                        if offset == 60:
                            self.assertTrue(all(f.get('_prelanding', {}).get('eta') == self.now+240 for f in saved))
        expected_requests = (count+9)//10 + (count+14)//15
        for offset in (0, 60, 120):
            calls = [c for c in http.calls if c[0] == self.now+offset]
            self.assertEqual(len(calls), expected_requests)
            self.assertLessEqual(len(calls), 8)
            queried_ids, queried_numbers = [], []
            for _, path, params, _ in calls:
                selector, limit = ('flight_ids', 10) if path == 'flight-summary/full' else ('flights', 15)
                members = params[selector].split(',')
                self.assertLessEqual(len(members), limit)
                (queried_ids if selector == 'flight_ids' else queried_numbers).extend(members)
            self.assertCountEqual(queried_ids, [f['fr24ID'] for f in tracked])
            self.assertCountEqual(queried_numbers, [f['number'] for f in tracked])
        self.assertEqual(len(http.calls), 3*expected_requests, '15-second local ticks must not buy new observations')
        alerts = [c for c in self.comat.sender.call_args_list if c.args[2]['flag'] == 'alarmPrelanding']
        self.assertEqual(len(alerts), count*2, 'one timely prelanding warning for every flight and recipient')

    async def test_five_active_flights_keep_minute_samples_for_two_closed_apps(self):
        await self.capacity(5)

    async def test_eight_active_flights_keep_minute_samples_for_two_closed_apps(self):
        await self.capacity(8)

    async def test_twelve_active_flights_keep_minute_samples_for_two_closed_apps(self):
        await self.capacity(12)

    async def test_unapproved_watch_is_not_added_to_provider_batches(self):
        tracked = flights(2, self.now)
        await self.own(flights=[tracked[0]])
        await self.add_peer(tracked[1], firebase_token='muted', server_polling=True, relay_alerts=True)
        self.allowed.remove('muted')
        self.comat.provider = FlightProvider()
        http = ProviderHTTP(tracked, self.now)
        with patch('push_relay.comatalarm.ClientSession', http), \
             patch('push_relay.flight_schedule.schedule_lookup', AsyncMock(return_value={})), \
             patch('push_relay.flight_schedule.aware_lookup', AsyncMock(return_value={})):
            await self.comat.tick(self.now)
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0][2], {'flight_ids': tracked[0]['fr24ID']})
        self.assertEqual(http.calls[1][2], {'flights': tracked[0]['number']})


class ProviderBatchMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1_795_000_000.0
        self.tracked = flights(8, self.now)
        self.provider = FlightProvider()
        self.http = ProviderHTTP(self.tracked, self.now)
        self.sessions = patch('push_relay.comatalarm.ClientSession', self.http)
        self.schedule = patch('push_relay.flight_schedule.schedule_lookup', AsyncMock(return_value={}))
        self.aware = patch('push_relay.flight_schedule.aware_lookup', AsyncMock(return_value={}))
        for active in (self.sessions, self.schedule, self.aware):
            active.start()
            self.addCleanup(active.stop)

    def plan(self, selected=None, token='first-token'):
        self.provider.plan_batches([(f, token) for f in (self.tracked if selected is None else selected)], self.now)

    async def lookup(self, flight, token='first-token'):
        return await self.provider.lookup(dict(flight, _central=True), token, self.now)

    async def test_partial_batch_response_never_borrows_another_flight_or_retries_singles(self):
        self.http.omit.add(self.tracked[-1]['fr24ID'])
        self.http.extra_live = [dict(sample(at=self.now, eta=self.now+240)['_live'],
                                     fr24_id='not-requested', flight='LH999', orig_iata='FRA')]
        self.plan()
        results = [await self.lookup(f) for f in self.tracked]
        self.assertTrue(all(r.get('_live', {}).get('fr24_id') == f['fr24ID']
                            for f, r in zip(self.tracked[:-1], results[:-1])))
        self.assertNotIn('_live', results[-1])
        self.assertNotIn('fr24ID', results[-1])
        self.assertEqual(len(self.http.calls), 2)

    async def test_same_number_on_two_days_keeps_exact_leg_for_live_matching(self):
        current, previous = self.tracked[:2]
        previous.update(number=current['number'], reference=self.now-86400, atd=self.now-90000)
        self.plan([current, previous])
        first, second = await self.lookup(current), await self.lookup(previous)
        self.assertEqual(first['_live']['fr24_id'], current['fr24ID'])
        self.assertEqual(second['_live']['fr24_id'], previous['fr24ID'])
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(self.http.calls[1][2]['flights'], current['number'])

    async def test_two_tokens_never_share_a_request_or_cached_response(self):
        self.provider.plan_batches([(self.tracked[0], 'one'), (self.tracked[1], 'two')], self.now)
        await self.lookup(self.tracked[0], 'one')
        await self.lookup(self.tracked[1], 'two')
        self.assertEqual(len(self.http.calls), 4)
        for _, path, params, auth in self.http.calls:
            f = self.tracked[0 if auth == 'Bearer one' else 1]
            self.assertEqual(params, {'flight_ids': f['fr24ID']} if path == 'flight-summary/full' else {'flights': f['number']})

    async def test_429_retains_real_provider_backoff_without_batch_fallback_storm(self):
        self.http.status = 429
        self.plan()
        results = [await self.lookup(f) for f in self.tracked]
        self.assertTrue(all(r.get('_error') for r in results))
        self.assertEqual(len(self.http.calls), 1)
        identity = hashlib.sha256(b'first-token').hexdigest()
        self.assertGreaterEqual(self.provider.cooldown[identity], self.now+120)
        self.now += 61
        self.http.now = self.now
        self.plan()
        await self.lookup(self.tracked[0])
        self.assertEqual(len(self.http.calls), 1)

    async def test_existing_eight_per_minute_budget_still_blocks_extra_requests(self):
        identity = hashlib.sha256(b'first-token').hexdigest()
        self.provider.requests[identity] = [self.now-1]*8
        self.plan()
        results = [await self.lookup(f) for f in self.tracked]
        self.assertTrue(all(r.get('_error') for r in results))
        self.assertEqual(self.http.calls, [])
        self.assertEqual(len(self.provider.requests[identity]), 8)

    async def test_new_member_midminute_queries_only_its_uncached_rows(self):
        self.plan(self.tracked[:2])
        await self.lookup(self.tracked[0])
        self.now += 15
        self.http.now = self.now
        self.plan(self.tracked[:3])
        await self.lookup(self.tracked[2])
        self.assertEqual(len(self.http.calls), 4)
        self.assertEqual(self.http.calls[2][2], {'flight_ids': self.tracked[2]['fr24ID']})
        self.assertEqual(self.http.calls[3][2], {'flights': self.tracked[2]['number']})

    async def test_unknown_ids_from_distinct_berlin_days_are_not_combined(self):
        day = datetime.fromtimestamp(self.now, ZoneInfo('Europe/Berlin')).replace(hour=0, minute=0, second=0, microsecond=0)
        one, two = self.tracked[:2]
        one.update(reference=day.timestamp()+3600, atd=day.timestamp()+1800)
        two.update(reference=(day-timedelta(days=1)).timestamp()+3600,
                   atd=(day-timedelta(days=1)).timestamp()+1800)
        selected = [dict(one, fr24ID=None), dict(two, fr24ID=None)]
        self.plan(selected)
        results = [await self.lookup(f) for f in selected]
        summaries = [c for c in self.http.calls if c[1] == 'flight-summary/full']
        self.assertEqual(len(summaries), 2)
        self.assertEqual([c[2]['flights'] for c in summaries], [one['number'], two['number']])
        self.assertNotEqual(summaries[0][2]['flight_datetime_from'], summaries[1][2]['flight_datetime_from'])
        self.assertEqual([r['fr24ID'] for r in results], [one['fr24ID'], two['fr24ID']])


class BatchPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_summary_and_live_selectors_are_limited_to_fifteen(self):
        batch = FR24BatchRequests()
        entries = [('token', 'flight-summary/full', {'flights': f'LH{i}', 'flight_datetime_from': '2026-11-01T00:00:00Z',
                                                    'flight_datetime_to': '2026-11-01T01:00:00Z'}) for i in range(32)]
        entries += [('token', 'live/flight-positions/full', {'flights': f'LH{i}'}) for i in range(32)]
        fetch = AsyncMock(return_value=[])
        batch.plan(entries, 1000)
        for token, path, params in entries:
            self.assertEqual(await batch.request(token, path, params, 1000, fetch), [])
        self.assertEqual(fetch.await_count, 6)
        self.assertTrue(all(1 <= len(c.args[1]['flights'].split(',')) <= 15 for c in fetch.call_args_list))

    async def test_all_extra_query_parameters_are_part_of_group_identity(self):
        batch = FR24BatchRequests()
        entries = [('token', 'live/flight-positions/full', {'flights': 'LH1', 'limit': '1'}),
                   ('token', 'live/flight-positions/full', {'flights': 'LH2', 'limit': '2'}),
                   ('token', 'live/flight-positions/full', {'flights': 'LH3'})]
        fetch = AsyncMock(return_value=[])
        batch.plan(entries, 1000)
        for token, path, params in entries:
            await batch.request(token, path, params, 1000, fetch)
        self.assertEqual(fetch.await_count, 3)
        self.assertCountEqual([c.args[1] for c in fetch.call_args_list], [e[2] for e in entries])

    def test_future_completed_and_deferred_flights_are_not_planned(self):
        now = 1_795_000_000.0
        base = flights(1, now)[0]
        cases = [dict(base, cancelled=True), dict(base, diverted=True),
                 dict(base, origin='BER', destination='FRA', atd=now-100),
                 dict(base, aibt=now-60, aibt_position={'latitude': 52.36507, 'longitude': 13.5053, 'timestamp': now-60}),
                 dict(base, aibt=now-60, _aibt_recovery_at=now-10),
                 dict(base, aibt=now-60, _aibt_recovery_attempts=6, _aibt_recovery_at=now, _aibt_evidence_revision=2),
                 dict(base, fr24ID=None, reference=now+86400)]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(planned_queries(candidate, now), [])
        self.assertEqual(len(planned_queries(base, now)), 2)

    def test_one_damaged_candidate_does_not_break_planning_for_others(self):
        now = 1_795_000_000.0
        base = flights(1, now)[0]
        provider = FlightProvider()
        broken = [dict(base, reference='invalid', fr24ID=None), dict(base, _outbound=123, track_outbound=True),
                  dict(base, reference=float('inf'), fr24ID=None), {'number': 'LH999'}, dict(base, fr24ID=123)]
        provider.plan_batches([(f, 'token') for f in broken] + [(base, 'token')], now)
        self.assertEqual(len(provider.batches.plans), 2)


if __name__ == '__main__':
    unittest.main()
