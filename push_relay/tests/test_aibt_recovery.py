"""Recover exact-leg stationary AIBT evidence after live tracking ends."""
import time
import unittest
from unittest.mock import AsyncMock, patch
from push_relay.comatalarm import FlightProvider

class AIBTRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_snapshot_recovers_closest_stationary_point_from_exact_track(self):
        aibt = time.time()-600
        point = dict(timestamp=aibt-5, lat=52.36507, lon=13.5053, gspeed=0)
        fr24 = AsyncMock(side_effect=[[], [dict(fr24_id='other', tracks=[point]),
            dict(fr24_id='abc', tracks=[dict(point,timestamp=aibt-55),point,
                dict(point,timestamp=aibt,gspeed=20)])]])
        result = await FlightProvider.parking_position(fr24,'abc','LH206',aibt,aibt-300,aibt+600)
        self.assertEqual(result['timestamp'],aibt-5)
        self.assertEqual(fr24.call_args_list[-1].args,('flight-tracks',{'flight_id':'abc'}))

    async def test_snapshot_wrong_leg_does_not_hide_target_and_needs_no_track(self):
        aibt=time.time()-600
        point=dict(timestamp=aibt,lat=52.36507,lon=13.5053,gspeed=0)
        fr24=AsyncMock(return_value=[dict(point,fr24_id='wrong'),dict(point,fr24_id='abc')])
        self.assertIsNotNone(await FlightProvider.parking_position(fr24,'abc','LH206',aibt,aibt-300,aibt+600))
        self.assertEqual(fr24.await_count,1)
        self.assertEqual(fr24.call_args.args[1]['limit'],'10')

    async def test_unrelated_stale_moving_and_postdeparture_points_are_rejected(self):
        aibt=time.time()-600
        point=dict(timestamp=aibt,lat=52.36507,lon=13.5053,gspeed=0)
        for row in [dict(fr24_id='other',tracks=[point]),
                    dict(fr24_id='abc',tracks=[dict(point,timestamp=aibt-61)]),
                    dict(fr24_id='abc',tracks=[dict(point,gspeed=5)]),
                    dict(fr24_id='abc',tracks=[dict(point,timestamp=aibt+30)]),
                    dict(fr24_id='abc',tracks=[dict(point,lat=48.3)])]:
            fr24=AsyncMock(side_effect=[[],[row]])
            self.assertIsNone(await FlightProvider.parking_position(fr24,'abc','LH206',aibt,aibt-300,aibt+20))

    async def test_official_event_top_level_coordinates_need_no_historic_snapshot(self):
        now=time.time()
        calls=[]
        class Response:
            status=200
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def json(self):
                return {'data':[{'fr24_id':'abc','events':[dict(type='gate_arrival', timestamp=now-60,
                    lat=52.36507,lon=13.5053,details=None)]}]}
        def get(url,**kwargs):
            calls.append(url)
            return Response()
        with patch('push_relay.comatalarm.ClientSession') as cls, \
             patch('push_relay.flight_schedule.schedule_lookup',return_value={}), \
             patch('push_relay.flight_schedule.aware_lookup',return_value={}):
            cls.return_value.__aenter__.return_value.get=get
            result=await FlightProvider().lookup(dict(number='LH206',fr24ID='abc',reference=now-300,
                ata=now-100,destination='BER',_central=True),'secret',now)
        self.assertEqual(result['aibt_position']['latitude'],52.36507)
        self.assertFalse(any('flight-positions' in c or 'flight-tracks' in c for c in calls))
