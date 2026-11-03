import time
import unittest
from unittest.mock import AsyncMock, patch
from push_relay.flight_schedule import flightstats, ber_schedule, timestamp
from push_relay.comatalarm import FlightProvider


class ScheduleTests(unittest.TestCase):
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
