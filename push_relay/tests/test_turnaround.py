import time
import unittest
from unittest.mock import AsyncMock, patch

from push_relay.comatalarm import FlightProvider, previous_ber_leg, transitions


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.flight = dict(number="LH207", origin="BER", destination="FRA", registration="D-AIZD", reference=self.now+1800)

    def leg(self, number="LH948", identifier="inbound", **changes):
        return dict(dict(fr24_id=identifier, flight=number, reg="D-AIZD", orig_iata="MUC", dest_iata="BER",
                         datetime_takeoff=self.now-5000, datetime_landed=self.now-1000), **changes)

    def test_registration_and_real_sequence_not_number_minus_one(self):
        rows = [self.leg("LH206", "wrong-aircraft", reg="D-AAAA"), self.leg()]
        result = previous_ber_leg(rows, self.flight, self.now)
        self.assertEqual(result["number"], "LH948")
        self.assertEqual(result["id"], "inbound")

    def test_overnight_and_normalized_registration(self):
        row = self.leg(reg="DAIZD", datetime_takeoff=self.now-40000, datetime_landed=self.now-35000)
        self.assertEqual(previous_ber_leg([row], self.flight, self.now)["ata"], self.now-35000)

    def test_intervening_leg_or_diversion_rejects_old_ber_stand(self):
        later = self.leg("LH999", "later", orig_iata="BER", dest_iata="HAM", datetime_takeoff=self.now-500, datetime_landed=None)
        self.assertIsNone(previous_ber_leg([self.leg(), later], self.flight, self.now))
        later.update(orig_iata="HAM", dest_iata="BER", datetime_landed=self.now-100, dest_iata_actual="LEJ")
        self.assertIsNone(previous_ber_leg([self.leg(), later], self.flight, self.now))

    def test_future_arrival_unknown_registration_and_ambiguous_movements(self):
        self.assertIsNone(previous_ber_leg([self.leg(datetime_landed=self.now+60)], self.flight, self.now))
        self.assertIsNone(previous_ber_leg([self.leg()], dict(self.flight, registration=""), self.now))
        self.assertIsNone(previous_ber_leg([self.leg(), self.leg(identifier="other")], self.flight, self.now))

    def test_airborne_next_inbound_blocks_previous_rotation(self):
        latest = self.leg("LH888", "airborne", datetime_takeoff=self.now-500, datetime_landed=None)
        self.assertIsNone(previous_ber_leg([self.leg(), latest], self.flight, self.now))

    def test_current_departure_is_excluded_but_future_rotations_are_not_used(self):
        departing = dict(self.flight, fr24ID="outgoing", atd=self.now-100)
        own = self.leg("LH207", "outgoing", orig_iata="BER", dest_iata="FRA", datetime_takeoff=self.now-100, datetime_landed=None)
        later = self.leg("LH208", "future", datetime_takeoff=self.now+3600, datetime_landed=self.now+5000)
        self.assertEqual(previous_ber_leg([later, own, self.leg()], departing, self.now)["id"], "inbound")

    def test_placeholder_id_does_not_make_current_ground_departure_block_its_predecessor(self):
        own = self.leg("LH207", "outgoing", orig_iata="BER", dest_iata="FRA",
                       datetime_takeoff=None, first_seen=self.now-100, datetime_landed=None)
        for placeholder in ("TEMP_LH207", "NS_LH207"):
            result = previous_ber_leg([own, self.leg()], dict(self.flight, fr24ID=placeholder), self.now)
            self.assertEqual(result["id"], "inbound")


class PreviousArrivalTests(unittest.IsolatedAsyncioTestCase):
    async def test_departed_flight_retries_event_after_predeparture_budget_was_spent(self):
        now = time.time()
        old = dict(id="206", number="LH206", registration="D-AIZD", ata=now-8000,
                   recovery_attempts=2, event_checked_at=now-1800)
        flight = dict(number="LH207", origin="BER", destination="FRA", registration="D-AIZD", reference=now-500,
                      atd=now-300, _inbound=old, _inbound_check_at=now-1800, _inbound_registration="DAIZD")
        fr24 = AsyncMock(side_effect=[
            [dict(fr24_id="206", flight="LH206", reg="D-AIZD", orig_iata="FRA", dest_iata="BER", datetime_takeoff=now-10000, datetime_landed=now-8000)],
            [dict(fr24_id="206", events=[dict(type="gate_arrival", timestamp=now-7500, details={"gate_lat":52.36507,"gate_lon":13.5053})])]])
        result = await FlightProvider().previous_arrival(flight, fr24, now)
        self.assertEqual(result["_inbound"]["aibt"], now-7500)
        self.assertEqual(fr24.await_count, 2)

    async def test_postdeparture_recovery_does_not_query_own_destination_or_schedules(self):
        now = time.time()
        flight = dict(number="LH207", origin="BER", destination="FRA", registration="D-AIZD", reference=now-500, atd=now-300, _central=True)
        with patch("push_relay.comatalarm.ClientSession") as cls, patch("push_relay.flight_schedule.schedule_lookup", new_callable=AsyncMock) as schedule:
            session = cls.return_value.__aenter__.return_value
            provider = FlightProvider()
            provider.previous_arrival = AsyncMock(return_value={"_inbound":{}})
            await provider.lookup(flight, "secret", now)
        provider.previous_arrival.assert_awaited_once()
        schedule.assert_not_awaited()
        session.get.assert_not_called()

    async def test_real_event_and_historic_position_are_separate_from_departure(self):
        now = time.time()
        flight = dict(number="LH207", origin="BER", destination="FRA", registration="D-AIZD", reference=now+1800)
        fr24 = AsyncMock(side_effect=[
            [dict(fr24_id="206", flight="LH206", reg="D-AIZD", orig_iata="FRA", dest_iata="BER", datetime_takeoff=now-4000, datetime_landed=now-800)],
            [dict(fr24_id="206", events=[dict(type="gate_arrival", timestamp=now-600, details={"gate_ident":"A30"})])],
            [dict(fr24_id="wrong", lat=52.36507, lon=13.5053, timestamp=now-600, gspeed=0),
             dict(fr24_id="206", lat=52.36507, lon=13.5053, timestamp=now-600, gspeed=0)]])
        result = await FlightProvider().previous_arrival(flight, fr24, now)
        self.assertNotIn("aibt", result)
        self.assertNotIn("ata", result)
        self.assertEqual(result["_inbound"]["aibt"], now-600)
        self.assertEqual(result["_inbound"]["aibt_position"]["latitude"], 52.36507)
        self.assertEqual(fr24.call_args_list[1].args[1]["flight_ids"], "206")
        self.assertEqual(fr24.call_args_list[2].args[1]["flights"], "LH206")
        self.assertNotIn("airports", fr24.call_args_list[0].args[1])

    async def test_cached_event_throttle_and_aircraft_change(self):
        now = time.time()
        old = dict(id="old", number="LH206", registration="D-OLD", ata=now-4000, aibt=now-3600,
                   aibt_position=dict(latitude=52.36507, longitude=13.5053, timestamp=now-3600))
        flight = dict(number="LH207", origin="BER", destination="FRA", registration="D-OLD", reference=now+1800,
                      _inbound=old, _inbound_check_at=now-10, _inbound_registration="DOLD")
        provider, fr24 = FlightProvider(), AsyncMock(return_value=[])
        self.assertEqual(await provider.previous_arrival(flight, fr24, now), {})
        fr24.assert_not_awaited()
        self.assertEqual((await provider.previous_arrival(dict(flight, registration="D-NEW"), fr24, now))["_inbound"], {})
        self.assertEqual(fr24.await_count, 1)

    async def test_unknown_aibt_is_not_synthesized_and_recovery_is_bounded(self):
        now = time.time()
        leg = dict(fr24_id="206", flight="LH206", reg="D-AIZD", orig_iata="FRA", dest_iata="BER", datetime_takeoff=now-10000, datetime_landed=now-8000)
        flight = dict(number="LH207", origin="BER", destination="FRA", registration="D-AIZD", reference=now+1800)
        fr24 = AsyncMock(side_effect=[[leg], [], [leg], [], [leg]])
        provider = FlightProvider()
        for offset in (0, 901, 1802):
            result = await provider.previous_arrival(flight, fr24, now+offset)
            flight.update(result)
        self.assertNotIn("aibt", result["_inbound"])
        self.assertEqual(result["_inbound"]["recovery_attempts"], 2)
        self.assertEqual(fr24.await_count, 5)

    async def test_full_lookup_discovers_departure_route_before_previous_arrival(self):
        now = time.time()
        schedule = dict(origin="BER", destination="FRA", registration="D-AIZD", std=now+1800)
        calls = []
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self): return {"data": []}
        session = AsyncMock()
        session.get = lambda url, **kwargs: Response()
        async def previous(flight, fr24, at):
            calls.append(flight)
            return {"_inbound": {}}
        with patch("push_relay.comatalarm.ClientSession") as cls, patch("push_relay.flight_schedule.schedule_lookup", return_value=schedule), patch("push_relay.flight_schedule.aware_lookup", return_value={}):
            cls.return_value.__aenter__.return_value = session
            provider = FlightProvider(); provider.previous_arrival = previous
            result = await provider.lookup(dict(number="LH207", reference=now, _central=True), "secret", now)
        self.assertEqual(calls[0]["origin"], "BER")
        self.assertEqual(calls[0]["registration"], "D-AIZD")
        self.assertEqual(result["destination"], "FRA")

    def test_predecessor_push_does_not_fake_departure_arrival(self):
        before = dict(number="LH207", origin="BER", destination="FRA")
        after = dict(before, registration="D-AIZD", stand="A12", packets="0/2", _inbound=dict(id="206", number="LH206", aibt=time.time()-600))
        events = transitions(before, after)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "inbound-parking:206")
        self.assertIn("2 Pakete abzugeben", events[0][3])
        self.assertEqual(transitions(after, dict(after)), [])
        self.assertEqual([e for e in transitions(before, dict(after, atd=time.time())) if e[0].startswith("inbound")], [])
