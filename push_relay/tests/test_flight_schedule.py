import time
import unittest
from unittest.mock import AsyncMock, patch
from push_relay.flight_schedule import flightstats, ber_schedule, timestamp, passenger_gate, aware_lookup, schedule_lookup
from push_relay.comatalarm import FlightProvider


class ScheduleTests(unittest.TestCase):
    def test_flightstats_gate_is_selected_for_the_ber_leg(self):
        public = {'data':{'departureAirport':{'gate':'Z 1'}, 'arrivalAirport':{'gate':' c1 '}}}
        official = {'flightStatuses':[{'airportResources':{'departureGate':'Z 1', 'arrivalGate':' c1 '}}]}
        for root, is_official in ((public, False), (official, True)):
            with self.subTest(official=is_official):
                arriving = flightstats(root, is_official, time.time())
                departing = flightstats(root, is_official, time.time(), departing=True)
                self.assertEqual((arriving['gate'], arriving['gate_source']), ('C1', 'FlightStats'))
                self.assertEqual(departing['gate'], 'Z 1')
                self.assertNotIn('stand', arriving)
                self.assertNotIn('aibt', arriving)

    def test_missing_gate_values_do_not_overwrite_valid_fallbacks(self):
        for gate in (None, '', ' - ', '—', 'NULL', 'N/A', {}, 42, 'x'*33, 'A\n1'):
            self.assertEqual(passenger_gate(gate, 'FlightStats'), {})

    def test_gate_departure_is_not_takeoff(self):
        root = {'schedule':{'scheduledDepartureUTC':'2026-11-03T12:00:00Z','estimatedActualDepartureUTC':'2026-11-03T12:03:00Z'}, 'flightNote':{'hasActualGateDepartureTime':True}}
        result = flightstats(root,False,time.time())
        self.assertIn('etd',result)
        self.assertNotIn('atd',result)
        root['flightNote']['hasActualRunwayDepartureTime'] = True
        self.assertEqual(flightstats(root,False,time.time())['atd'], result['etd'])

    def test_ber_local_time_and_no_false_actual_arrival(self):
        result = ber_schedule({'scheduled_time':'2026-11-03T13:55:00','arr_estimated_time':'2026-11-03T13:45:00'},False)
        self.assertEqual(result['sta'],timestamp('2026-11-03T12:55:00Z'))
        self.assertNotIn('ata',result)
        self.assertNotIn('aibt',result)

    def test_official_runway_events_and_gate_estimate_are_distinct(self):
        result = flightstats({'flightStatuses':[{'operationalTimes':{'actualRunwayDeparture':{'dateUtc':'2026-11-03T12:17:00Z'},'estimatedGateArrival':{'dateUtc':'2026-11-03T13:25:00Z'},'actualGateArrival':{'dateUtc':'2026-11-03T13:26:00Z'}}}]},True,time.time())
        self.assertEqual(result['atd'],timestamp('2026-11-03T12:17:00Z'))
        self.assertIn('eibt',result)
        self.assertEqual(result['aibt'], timestamp('2026-11-03T13:26:00Z'))
        self.assertEqual(result['aibt_source'], 'FlightStats')

    def test_website_actual_gate_flag_is_required_for_the_combined_field(self):
        for note in ({}, {'hasActualRunwayArrivalTime': True}, {'hasActualGateArrivalTime': 'true'}, {'hasActualGateArrivalTime': True}):
            root = {'schedule': {'estimatedActualArrivalUTC': '2026-11-03T13:26:00Z',
                                 'estimatedGateArrivalUTC': '2026-11-03T13:25:00Z'}, 'flightNote': note}
            result = flightstats(root, False, time.time())
            if note.get('hasActualGateArrivalTime') is True:
                self.assertEqual(result['aibt'], timestamp('2026-11-03T13:26:00Z'))
                self.assertEqual(result['aibt_source'], 'FlightStats')
            else:
                self.assertNotIn('aibt', result)
        root['schedule']['actualGateArrivalUTC'] = '2026-11-03T13:28:00Z'
        self.assertEqual(flightstats(root, False, time.time())['aibt'], timestamp('2026-11-03T13:28:00Z'))


class ActualArrivalIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = timestamp('2026-11-20T17:10:00Z')
        self.flight = dict(number='LH194', reference=timestamp('2026-11-20T16:00:00Z'),
                           origin='FRA', destination='BER', registration='D-AIDA', ata=timestamp('2026-11-20T16:51:21Z'))
        self.aware = dict(ident='DLH194', ident_iata='LH194', scheduled_out='2026-11-20T16:00:00Z',
                          origin={'code_iata': 'FRA'}, destination={'code_iata': 'BER'}, registration='DAIDA',
                          actual_on='2026-11-20T16:51:21Z', actual_in='2026-11-20T16:58:00Z',
                          fa_flight_id='DLH194-1795190400-airline-1234', estimated_in='2026-11-20T16:57:00Z')
        self.stats = dict(carrierFsCode='LH', flightNumber='194', flightId=1234,
                          departureAirportFsCode='FRA', arrivalAirportFsCode='BER',
                          flightEquipment={'tailNumber': 'DAIDA'}, operationalTimes={
                          'scheduledGateDeparture': {'dateUtc': '2026-11-20T16:00:00Z'},
                          'actualRunwayArrival': {'dateUtc': '2026-11-20T16:51:21Z'},
                          'actualGateArrival': {'dateUtc': '2026-11-20T16:58:00Z'}})

    def session(self, root):
        class Response:
            status, headers = 200, {}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self): return root
        session = AsyncMock()
        session.get = lambda *args, **kwargs: Response()
        return session

    async def test_actual_in_and_provider_id_require_the_matching_leg(self):
        result = await aware_lookup(self.session({'flights': [self.aware]}), self.flight,
                                    {'FLIGHTAWARE_API_KEY': 'not-a-real-key'}, {}, self.now)
        self.assertEqual(result['aibt'], timestamp('2026-11-20T16:58:00Z'))
        self.assertEqual(result['aibt_source'], 'FlightAware')
        self.assertEqual(result['flightawareID'], self.aware['fa_flight_id'])
        self.assertNotIn('stand', result)
        for change in ({'ident': 'DLH196', 'ident_iata': 'LH196'}, {'scheduled_out': '2026-11-19T16:00:00Z'},
                       {'origin': {'code_iata': 'MUC'}}, {'registration': 'D-AIDB'},
                       {'destination': {}}, {'registration': None}, {'ident': None, 'ident_iata': None}):
            with self.subTest(change=change):
                result = await aware_lookup(self.session({'flights': [dict(self.aware, **change)]}), self.flight,
                                            {'FLIGHTAWARE_API_KEY': 'test'}, {}, self.now)
                self.assertNotIn('aibt', result)
                self.assertNotIn('flightawareID', result)

    async def test_future_prelanding_and_estimated_arrivals_are_not_aibt(self):
        for change in ({'actual_in': None}, {'actual_in': '2026-11-20T16:50:00Z'},
                       {'actual_in': '2026-11-20T17:15:00Z'}, {'cancelled': True}, {'diverted': True}):
            result = await aware_lookup(self.session({'flights': [dict(self.aware, **change)]}), self.flight,
                                        {'FLIGHTAWARE_API_KEY': 'test'}, {}, self.now)
            self.assertNotIn('aibt', result)
            self.assertNotIn('aibt_source', result)

    async def test_two_same_day_legs_do_not_choose_an_aibt_by_nearest_schedule(self):
        second = dict(self.aware, fa_flight_id='another-leg', scheduled_out='2026-11-20T16:10:00Z')
        result = await aware_lookup(self.session({'flights': [self.aware, second]}), self.flight,
                                    {'FLIGHTAWARE_API_KEY': 'test'}, {}, self.now)
        self.assertNotIn('aibt', result)
        self.assertNotIn('flightawareID', result)

    async def test_flightstats_actual_gate_arrival_checks_occurrence_before_selection(self):
        wrong = dict(self.stats, flightId=4321, departureAirportFsCode='MUC')
        result = await schedule_lookup(self.session({'flightStatuses': [wrong, self.stats]}), self.flight,
                                       {'FLIGHTSTATS_APP_ID': 'test', 'FLIGHTSTATS_APP_KEY': 'test'}, {}, self.now)
        self.assertEqual(result['aibt'], timestamp('2026-11-20T16:58:00Z'))
        self.assertEqual((result['aibt_source'], result['flightstatsID']), ('FlightStats', '1234'))
        for change in ({'carrierFsCode': 'EW'}, {'flightNumber': '196'}, {'flightEquipment': {'tailNumber': 'D-AIDB'}},
                       {'arrivalAirportFsCode': None}, {'flightEquipment': {}},
                       {'operationalTimes': dict(self.stats['operationalTimes'], scheduledGateDeparture={'dateUtc': '2026-11-19T16:00:00Z'})}):
            result = await schedule_lookup(self.session({'flightStatuses': [dict(self.stats, **change)]}), self.flight,
                                           {'FLIGHTSTATS_APP_ID': 'test', 'FLIGHTSTATS_APP_KEY': 'test'}, {}, self.now)
            self.assertNotIn('aibt', result)

    async def test_public_actual_flag_is_bound_to_flight_number_date_route_and_tail(self):
        root = {'data': {'carrier': {'iata': 'LH'}, 'flightNumber': '194', 'flightId': 1234,
                'schedule': {'scheduledDepartureUTC': '2026-11-20T16:00:00Z', 'estimatedActualArrivalUTC': '2026-11-20T16:58:00Z'},
                'flightNote': {'hasActualGateArrivalTime': True}, 'departureAirport': {'iata': 'FRA'},
                'arrivalAirport': {'iata': 'BER', 'gate': 'A07'}, 'equipment': {'tailNumber': 'D-AIDA'}}}
        result = await schedule_lookup(self.session(root), self.flight, {}, {}, self.now)
        self.assertEqual(result['aibt'], timestamp('2026-11-20T16:58:00Z'))
        self.assertNotIn('stand', result)
        root['data']['flightNumber'] = '196'
        self.assertEqual(await schedule_lookup(self.session(root), self.flight, {}, {}, self.now), {})

    async def test_overnight_incoming_matches_arrival_day_and_rejects_neighboring_leg(self):
        reference = timestamp('2026-11-20T23:35:00Z')  # Nov21 in Berlin
        f = dict(self.flight, reference=reference, ata=reference-180)
        row = dict(self.aware, scheduled_out='2026-11-20T22:35:00Z', scheduled_in='2026-11-20T23:35:00Z',
                   actual_on='2026-11-20T23:32:00Z', actual_in='2026-11-20T23:40:00Z')
        result = await aware_lookup(self.session({'flights': [row]}), f, {'FLIGHTAWARE_API_KEY': 'fixture'}, {}, reference+900)
        self.assertEqual(result['aibt'], reference+300)
        previous = dict(row, scheduled_out='2026-11-19T22:35:00Z', scheduled_in='2026-11-19T23:35:00Z',
                        actual_on='2026-11-19T23:32:00Z', actual_in='2026-11-19T23:40:00Z')
        self.assertEqual(await aware_lookup(self.session({'flights': [previous]}), f,
                                           {'FLIGHTAWARE_API_KEY': 'fixture'}, {}, reference+900), {})
        stats = dict(self.stats, operationalTimes={
            'scheduledGateDeparture': {'dateUtc': row['scheduled_out']},
            'scheduledGateArrival': {'dateUtc': row['scheduled_in']},
            'actualRunwayArrival': {'dateUtc': row['actual_on']},
            'actualGateArrival': {'dateUtc': row['actual_in']}})
        result = await schedule_lookup(self.session({'flightStatuses': [stats]}), f,
                                        {'FLIGHTSTATS_APP_ID': 'fixture', 'FLIGHTSTATS_APP_KEY': 'fixture'}, {}, reference+900)
        self.assertEqual(result['aibt'], reference+300)
        stats['operationalTimes']['scheduledGateArrival']['dateUtc'] = '2026-11-19T23:35:00Z'
        self.assertEqual(await schedule_lookup(self.session({'flightStatuses': [stats]}), f,
                                              {'FLIGHTSTATS_APP_ID': 'fixture', 'FLIGHTSTATS_APP_KEY': 'fixture'}, {}, reference+900), {})

    async def test_website_uses_only_a_proven_origin_local_departure_day(self):
        calls = []
        session = self.session({})
        get = session.get
        def record(url, **kwargs):
            calls.append(url)
            return get(url, **kwargs)
        session.get = record
        f = dict(self.flight, reference=timestamp('2026-11-21T08:00:00Z'),
                 _schedule={'std': timestamp('2026-11-21T04:00:00Z'), '_departure_day': '2026-11-20'})
        await schedule_lookup(session, f, {}, {}, f['reference']+300)
        self.assertTrue(calls[-1].endswith('/2026/11/20'))
        f['_schedule'].pop('_departure_day')
        await schedule_lookup(session, f, {}, {}, f['reference']+300)
        self.assertTrue(calls[-1].endswith('/2026/11/21'), 'Do not infer JFK local calendar date from Berlin UTC conversion')


class CentralProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ber_denial_keeps_schedule_gate_fallback_and_shared_cache(self):
        now = timestamp('2026-11-03T21:24:00Z')
        for ber_status, stats_gate, expected, source in (
                (403, 'C1', 'C1', 'FlightStats'),
                (403, '-', 'B3', 'FlightAware'),
                (200, 'C1', 'A2', 'BER')):
            with self.subTest(ber=ber_status, stats_gate=stats_gate):
                calls = []
                class Response:
                    headers = {}
                    def __init__(self, status, body): self.status, self.body = status, body
                    async def __aenter__(self): return self
                    async def __aexit__(self, *args): pass
                    async def json(self): return self.body
                def get(url, **kwargs):
                    calls.append(url)
                    if 'ber.berlin-airport' in url:
                        return Response(ber_status, {'data':{'items':[{'flight_number':'LH 206', 'gate':'A2'}]}})
                    if 'flightstats.com' in url:
                        return Response(200, {'data':{'schedule':{'scheduledDepartureUTC':'2026-11-03T21:00:00Z'},
                            'departureAirport':{'iata':'FRA', 'gate':'WRONG'}, 'arrivalAirport':{'iata':'BER', 'gate':stats_gate}}})
                    if 'flightaware.com' in url:
                        return Response(200, {'flights':[{'scheduled_out':'2026-11-03T21:00:00Z',
                            'origin':{'code_iata':'FRA'}, 'destination':{'code_iata':'BER'}, 'gate_origin':'WRONG', 'gate_destination':'B3'}]})
                    self.fail('Unexpected provider request: '+url)
                session = AsyncMock(); session.get = get
                with patch('push_relay.comatalarm.ClientSession') as cls:
                    cls.return_value.__aenter__.return_value = session
                    provider = FlightProvider(); provider.credentials = {'FLIGHTAWARE_API_KEY':'test-key'}
                    flight = dict(number='LH206', reference=now, origin='FRA', destination='BER', _central=True)
                    result = await provider.lookup(flight, '', now)
                    again = await provider.lookup(dict(flight, id='another-phone'), '', now+1)
                    self.assertEqual((result['gate'], result['gate_source']), (expected, source))
                    self.assertEqual(result, again)
                    self.assertEqual(len(calls), 3, 'reuse schedule responses and the same flight across phones')
                    self.assertNotIn('stand', result)
                    self.assertNotIn('aibt', result)
                    if ber_status == 403:
                        self.assertGreater(provider.cooldown['ber'], now+61)
                        later = await provider.lookup(flight, '', now+61)
                        self.assertEqual(later['gate'], expected)
                        self.assertEqual(sum('ber.berlin-airport' in url for url in calls), 1, 'respect BER cooldown')
                        self.assertEqual(len(calls), 4, 'FlightAware keeps its existing low-frequency cache')

    async def test_live_snapshot_matches_occurrence_and_is_shared_with_schedule(self):
        now = timestamp('2026-11-03T12:30:00Z')
        payloads = [
            {'data':[{'fr24_id':'right','flight':'LH186','orig_iata':'FRA','dest_iata':'BER','datetime_takeoff':now-600}]},
            {'data':[{'fr24_id':'wrong','flight':'LH186','orig_iata':'FRA','dest_iata':'BER','timestamp':now,'lat':1,'lon':1}, {'fr24_id':'right','flight':'LH186','orig_iata':'FRA','dest_iata':'BER','timestamp':now-30,'lat':52.1,'lon':13.5,'gspeed':400}]},
            {'data':{'items':[{'flight_number':'LH 186','scheduled_time':'2026-11-03T13:55:00','gate':'A30'}]}},
            {'data':{'schedule':{'scheduledDepartureUTC':'2026-11-03T12:00:00Z'}}}]
        calls=[]
        class Response:
            status=200
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def json(self): return payloads.pop(0)
        session=AsyncMock()
        def get(url, **kwargs):
            calls.append(url)
            return Response()
        session.get=get
        with patch('push_relay.comatalarm.ClientSession') as cls:
            cls.return_value.__aenter__.return_value=session
            provider=FlightProvider()
            flight=dict(number='LH186', reference=now, origin='FRA',destination='BER',_central=True)
            result=await provider.lookup(flight,'secret',now)
            again=await provider.lookup(flight,'secret',now+1)
        self.assertEqual(result['_live']['lat'],52.1)
        self.assertIn('std',result['_schedule'])
        self.assertIn('sta',result['_schedule'])
        self.assertEqual(result['gate'],'A30')
        self.assertEqual(result,again)
        self.assertEqual(len(calls),4)
