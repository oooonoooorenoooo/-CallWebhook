import time
import unittest
from unittest.mock import AsyncMock, patch
from push_relay.flight_schedule import flightstats, ber_schedule, timestamp, passenger_gate
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
        self.assertNotIn('aibt',result)


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
        now = time.time()
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
