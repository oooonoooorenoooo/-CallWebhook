import copy
from datetime import datetime, timezone
import unittest

from push_relay.landing_alert import (
    AIRPORTS, ELEVATIONS_FT, alert_ttl, prelanding_is_current, prelanding_refresh_candidate, update_landing_alert,
)


NOW = 1_795_000_000.0


def sample(at=NOW, eta=NOW+240, distance=20, altitude=3000, destination="BER", **changes):
    lat, lon = AIRPORTS[destination]
    # Fields from the official FR24 FlightPositionsFull model:
    # https://github.com/Flightradar24/fr24api-sdk-python/blob/main/src/fr24sdk/models/flight.py
    # In particular, that model does not supply an on_ground flag.
    live = dict(fr24_id="abcdef", flight="LH194", orig_iata="FRA", dest_iata=destination, timestamp=at, eta=eta,
                lat=lat+distance/111.195, lon=lon, alt=altitude+ELEVATIONS_FT[destination], gspeed=170,
                vspeed=-700, track=180, source="ADSB")
    live.update(changes)
    return dict(id="device-flight", number="LH194", reference=NOW-3600,
                fr24ID="abcdef", origin="FRA", destination=destination,
                atd=NOW-3600, _live=live)


def approaching():
    first = update_landing_alert({}, sample(), NOW)
    second = dict(first, **sample(at=NOW+60, distance=15, altitude=2300))
    return first, update_landing_alert(first, second, NOW+60)


class LandingAlertTests(unittest.TestCase):
    def test_official_full_schema_does_not_require_an_invented_ground_flag(self):
        self.assertNotIn("on_ground", sample()["_live"])
        first, warned = approaching()
        self.assertEqual(warned["_landing_alert"]["samples"], 2)
        self.assertTrue(prelanding_is_current(warned, warned["_prelanding"], NOW+60))
        for ground in (True, 1, None, "false"):
            with self.subTest(ground=ground):
                current = sample(at=NOW+60, distance=15, altitude=2300, on_ground=ground)
                self.assertNotIn("_prelanding", update_landing_alert(first, current, NOW+60))

    def test_refresh_can_fetch_after_delivery_freshness_but_cannot_alert(self):
        flight = sample(at=NOW-60, eta=NOW+240)
        self.assertTrue(prelanding_refresh_candidate(flight, NOW))
        self.assertNotIn("_prelanding", update_landing_alert({}, flight, NOW))
        self.assertFalse(prelanding_refresh_candidate(flight, NOW+31))
        flight["_live"].pop("eta")
        flight["_schedule"] = {"eta": NOW+240, "sta": NOW+240}
        self.assertFalse(prelanding_refresh_candidate(flight, NOW))
        _, warned = approaching()
        self.assertFalse(prelanding_refresh_candidate(warned, NOW+60))

    @staticmethod
    def approach_phases(lag, fast_refresh):
        misses, warnings, intervals = [], [], []
        for phase in range(60):
            eta, flight, last_fetch = NOW+480+phase, {}, -60
            fetches, warned = [], False
            for offset in range(0, 601, 15):
                refresh = fast_refresh and prelanding_refresh_candidate(flight, NOW+offset)
                if offset % 60 == 0 or refresh and offset-last_fetch >= 30:
                    observed = NOW+offset-lag
                    remaining = eta-observed
                    # Constant 170 kt / -700 ft/min, positions describe the
                    # aircraft at the actual observation time, not receipt time.
                    measured = sample(at=observed, eta=eta, distance=170*1.852*remaining/3600,
                                      altitude=700*remaining/60)
                    flight = dict(flight, **measured)
                    last_fetch = offset
                    fetches.append(offset)
                flight = update_landing_alert(flight, flight, NOW+offset)
                if "_prelanding" in flight:
                    warnings.append(eta-(NOW+offset))
                    warned = True
                    break
            if not warned:
                misses.append(phase)
            intervals.extend(b-a for a, b in zip(fetches, fetches[1:]))
        return misses, warnings, intervals

    def test_thirty_second_final_refresh_closes_all_twenty_and_thirty_second_lag_phases(self):
        for lag in (20, 30):
            with self.subTest(source_age=lag):
                misses, _, _ = self.approach_phases(lag, fast_refresh=False)
                self.assertEqual(len(misses), 15, "the old minute cache misses a quarter of the possible final-approach phases")
                misses, warnings, intervals = self.approach_phases(lag, fast_refresh=True)
                self.assertEqual(misses, [], "targeted fresh observations close the gap without extending delivery freshness")
                self.assertEqual(len(warnings), 60)
                self.assertTrue(all(150 < lead <= 180 for lead in warnings))
                self.assertGreaterEqual(min(intervals), 30, "the final-approach path never becomes 15-second live polling")

    def test_two_fresh_observations_trigger_at_three_minutes(self):
        first, second = approaching()
        self.assertNotIn("_prelanding", first)
        self.assertEqual(second["_landing_alert"]["samples"], 2)
        self.assertEqual(second["_prelanding"]["eta"]-(NOW+60), 180)
        self.assertEqual(second["_prelanding"]["expires_at"], NOW+90)
        self.assertTrue(prelanding_is_current(second, second["_prelanding"], NOW+60))

    def test_confirmed_sample_can_cross_boundary_without_extra_paid_request(self):
        first = update_landing_alert({}, sample(eta=NOW+270, distance=23), NOW)
        second = dict(first, **sample(at=NOW+60, eta=NOW+270, distance=18, altitude=2300))
        second = update_landing_alert(first, second, NOW+60)
        self.assertNotIn("_prelanding", second)
        third = update_landing_alert(second, second, NOW+90)
        self.assertIn("_prelanding", third)
        self.assertEqual(third["_landing_alert"]["samples"], 2)

    def test_repeated_same_sample_does_not_establish_confidence(self):
        first = update_landing_alert({}, sample(eta=NOW+195, distance=16), NOW)
        later = update_landing_alert(first, first, NOW+15)
        self.assertEqual(later["_landing_alert"]["samples"], 1)
        self.assertNotIn("_prelanding", later)

    def test_normal_worker_ticks_preserve_previous_sample_until_next_poll(self):
        first = update_landing_alert({}, sample(at=NOW-3), NOW)
        for offset in (15, 30, 45):
            first = update_landing_alert(first, first, NOW+offset)
            self.assertEqual(first["_landing_alert"]["samples"], 1)
            self.assertNotIn("_prelanding", first)
        current = dict(first, **sample(at=NOW+57, distance=15, altitude=2300))
        warned = update_landing_alert(first, current, NOW+60)
        self.assertIn("_prelanding", warned)
        self.assertEqual(warned["_landing_alert"]["samples"], 2)

    def test_long_stale_gap_discards_confidence_and_never_warns(self):
        first = update_landing_alert({}, sample(at=NOW-3), NOW)
        stale = update_landing_alert(first, first, NOW+91)
        self.assertNotIn("samples", stale["_landing_alert"])
        self.assertNotIn("_prelanding", stale)

    def test_missing_eta_never_uses_schedule_or_other_source_estimate(self):
        first, _ = approaching()
        current = sample(at=NOW+60, distance=15, altitude=2300)
        current["_live"].pop("eta")
        current["_live"]["sta"] = NOW+240
        current["_schedule"] = {"eta": NOW+240, "sta": NOW+240}
        current["eta"] = NOW+240
        self.assertNotIn("_prelanding", update_landing_alert(first, current, NOW+60))

    def test_bad_approaches_and_telemetry_do_not_trigger(self):
        first, _ = approaching()
        cases = [
            {"timestamp": NOW}, {"timestamp": NOW+90}, {"on_ground": True},
            {"on_ground": None}, {"source": "ESTIMATION"}, {"source": "UNKNOWN"},
            {"alt": 18000}, {"alt": 100}, {"alt": float("nan")},
            {"vspeed": 800}, {"vspeed": 0}, {"gspeed": 0}, {"gspeed": True},
            {"track": 0}, {"track": None}, {"lat": 100}, {"lat": float("inf")},
            {"fr24_id": "other"}, {"dest_iata": "HAM"}, {"eta": None},
        ]
        for change in cases:
            with self.subTest(change=change):
                current = sample(at=NOW+60, distance=15, altitude=2300, **change)
                self.assertNotIn("_prelanding", update_landing_alert(first, current, NOW+60))

    def test_terminal_states_and_unknown_destination_do_not_trigger(self):
        first, _ = approaching()
        for field, value in (("ata", NOW+59), ("aibt", NOW+59), ("cancelled", True),
                             ("diverted", True), ("destination", "XYZ"), ("fr24ID", "TEMP_1")):
            with self.subTest(field=field):
                current = sample(at=NOW+60, distance=15, altitude=2300)
                current[field] = value
                self.assertNotIn("_prelanding", update_landing_alert(first, current, NOW+60))

    def test_large_eta_revision_requires_new_confirmation(self):
        first = update_landing_alert({}, sample(eta=NOW+300, distance=23), NOW)
        current = sample(at=NOW+60, distance=15, altitude=2300)
        revised = update_landing_alert(first, current, NOW+60)
        self.assertNotIn("_prelanding", revised)
        self.assertEqual(revised["_landing_alert"]["samples"], 1)

    def test_holding_and_regressing_observations_do_not_confirm(self):
        first = update_landing_alert({}, sample(eta=NOW+240, distance=15), NOW)
        for at, distance, altitude in ((NOW+60, 16, 2300), (NOW-1, 14, 2300), (NOW+60, 14, 3200)):
            with self.subTest(at=at, distance=distance, altitude=altitude):
                result = update_landing_alert(first, sample(at=at, distance=distance, altitude=altitude), NOW+60)
                self.assertNotIn("_prelanding", result)

    def test_late_detection_never_claims_three_minutes(self):
        first = update_landing_alert({}, sample(), NOW)
        late = sample(at=NOW+120, distance=10, altitude=1600)
        self.assertNotIn("_prelanding", update_landing_alert(first, late, NOW+120))

    def test_high_slow_descent_does_not_make_imminent_landing_plausible(self):
        first = update_landing_alert({}, sample(altitude=5700, vspeed=-250), NOW)
        current = sample(at=NOW+60, distance=15, altitude=5450, vspeed=-250)
        self.assertNotIn("_prelanding", update_landing_alert(first, current, NOW+60))
        first = update_landing_alert({}, sample(altitude=3000, vspeed=-250), NOW)
        current = sample(at=NOW+60, distance=15, altitude=2750, vspeed=-250)
        self.assertNotIn("_prelanding", update_landing_alert(first, current, NOW+60))

    def test_elevated_airport_uses_height_above_airfield(self):
        first = update_landing_alert({}, sample(destination="INN"), NOW)
        current = sample(at=NOW+60, distance=15, altitude=2300, destination="INN")
        self.assertEqual(current["_live"]["alt"], 4207)
        self.assertIn("_prelanding", update_landing_alert(first, current, NOW+60))

    def test_go_around_cancels_pending_and_never_replays_same_flight(self):
        _, warned = approaching()
        go_around = dict(warned, **sample(at=NOW+75, distance=14, altitude=2500, vspeed=900))
        cancelled = update_landing_alert(warned, go_around, NOW+75)
        self.assertNotIn("_prelanding", cancelled)
        self.assertEqual(cancelled["_landing_alert"]["triggered_at"], NOW+60)
        next_approach = dict(cancelled, **sample(at=NOW+600, eta=NOW+840, distance=20))
        next_approach = update_landing_alert(cancelled, next_approach, NOW+600)
        next_approach = update_landing_alert(next_approach,
            dict(next_approach, **sample(at=NOW+660, eta=NOW+840, distance=15, altitude=2300)), NOW+660)
        self.assertNotIn("_prelanding", next_approach)

    def test_queue_rechecks_data_and_expires_before_late_warning(self):
        _, warned = approaching()
        marker = warned["_prelanding"]
        self.assertFalse(prelanding_is_current(warned, marker, NOW+91))
        for change in ({"ata": NOW+61}, {"aibt": NOW+61}, {"diverted": True}):
            self.assertFalse(prelanding_is_current(dict(warned, **change), marker, NOW+61))
        revised = copy.deepcopy(warned)
        revised["_live"]["eta"] += 80
        self.assertFalse(prelanding_is_current(revised, marker, NOW+61))

    def test_restart_preserves_confidence_and_trigger_dedupe(self):
        import json
        first, warned = approaching()
        restored = json.loads(json.dumps(first))
        replay = update_landing_alert(restored,
            dict(restored, **sample(at=NOW+60, distance=15, altitude=2300)), NOW+60)
        self.assertIn("_prelanding", replay)
        restored = json.loads(json.dumps(warned))
        replay = update_landing_alert(restored, restored, NOW+75)
        self.assertEqual(replay["_prelanding"]["triggered_at"], NOW+60)

    def test_iso_dates_and_mlat_are_supported(self):
        first, _ = approaching()
        current = sample(at=NOW+60, distance=15, altitude=2300, source="MLAT")
        for field in ("eta", "timestamp"):
            current["_live"][field] = datetime.fromtimestamp(current["_live"][field], timezone.utc).isoformat()
        self.assertIn("_prelanding", update_landing_alert(first, current, NOW+60))

    def test_existing_selected_airports_are_supported(self):
        for airport in AIRPORTS:
            with self.subTest(airport=airport):
                first = update_landing_alert({}, sample(destination=airport), NOW)
                second = sample(at=NOW+60, distance=15, altitude=2300, destination=airport)
                self.assertIn("_prelanding", update_landing_alert(first, second, NOW+60))

    def test_apns_warning_ttl_is_short_and_expired_warning_is_refused(self):
        self.assertEqual(alert_ttl({"flag": "alarmParking"}, NOW), 300)
        self.assertIsNone(alert_ttl({"flag": "alarmPrelanding"}, NOW))
        self.assertIsNone(alert_ttl({"flag": "alarmPrelanding", "expires_at": NOW}, NOW))
        self.assertEqual(alert_ttl({"flag": "alarmPrelanding", "expires_at": NOW+12}, NOW), 12)
        self.assertEqual(alert_ttl({"flag": "alarmPrelanding", "expires_at": NOW+300}, NOW), 30)

    def test_inputs_are_not_mutated(self):
        first = update_landing_alert({}, sample(), NOW)
        current = dict(first, **sample(at=NOW+60, distance=15, altitude=2300))
        before = copy.deepcopy((first, current))
        update_landing_alert(first, current, NOW+60)
        self.assertEqual((first, current), before)


if __name__ == "__main__":
    unittest.main()
