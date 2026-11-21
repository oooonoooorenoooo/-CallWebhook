"""Synthetic final approaches at the real live-position JSON boundary.

These are reproducible policy cases, not recordings of the reported flights.
The simulated touchdown is used only to generate coherent position/altitude/
velocity samples. Production receives an independent provider ETA in `_live`.
Minute precision is a tested input case, not a claim about every FR24 response.
"""
from datetime import datetime, timezone
import math
import unittest

from push_relay.landing_alert import (
    AIRPORTS, ELEVATIONS_FT, _candidate, prelanding_is_current,
    prelanding_refresh_candidate, update_landing_alert,
)


NOW = 1_795_000_020.0  # Exactly on a minute boundary.


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def approach(observed, touchdown, eta, speed=150):
    """A measured 3-degree/250-degree approach, with 25ft/64fpm resolution.

    The touchdown is offset 2km along and 0.7km across the airport reference
    point. These offsets exercise a runway-like geometry without asserting a
    specific BER runway threshold. ETA and observation use JSON ISO strings;
    no invented on_ground extension is needed.
    """
    lat, lon = AIRPORTS["BER"]
    distance = speed * 1.852 * (touchdown - observed) / 3600
    angle = math.radians(3)
    course = math.radians(250)
    east = -math.sin(course) * (distance + 2) + math.cos(course) * 0.7
    north = -math.cos(course) * (distance + 2) - math.sin(course) * 0.7
    height = distance * 1000 * math.tan(angle) / 0.3048
    vertical = -speed * 1.852 / 3.6 * math.tan(angle) / 0.3048 * 60
    live = dict(
        fr24_id="abcdef", flight="LH194", orig_iata="FRA", dest_iata="BER",
        timestamp=iso(observed), eta=iso(eta), source="ADSB",
        lat=lat + north / 111.195,
        lon=lon + east / (111.195 * math.cos(math.radians(lat))),
        alt=round((height + ELEVATIONS_FT["BER"]) / 25) * 25,
        gspeed=speed, vspeed=round(vertical / 64) * 64, track=250,
    )
    return dict(
        id="scenario-flight", number="LH194", reference=NOW - 3600,
        fr24ID="abcdef", origin="FRA", destination="BER", atd=NOW - 3600,
        _live=live,
    )


class PrelandingScenarioTests(unittest.TestCase):
    def advance(self, state, offset, touchdown, eta, lag=20, speed=150):
        now = NOW + offset
        current = dict(state, **approach(now - lag, touchdown, eta, speed))
        self.assertNotIn("on_ground", current["_live"])
        self.assertIsNotNone(_candidate(current, now), "Each confidence point must pass the unchanged delivery geometry")
        return update_landing_alert(state, current, now)

    def assert_current_warning(self, state, now):
        self.assertIn("_prelanding", state)
        marker = state["_prelanding"]
        candidate = _candidate(state, now)
        self.assertIsNotNone(candidate)
        self.assertGreaterEqual(state["_landing_alert"]["samples"], 2)
        self.assertLessEqual(now - candidate["observed_at"], 45)
        self.assertGreater(candidate["eta"] - now, 150)
        self.assertLessEqual(candidate["eta"] - now, 180)
        self.assertTrue(prelanding_is_current(state, marker, now))
        self.assertFalse(prelanding_is_current(state, marker, marker["expires_at"]))

    def test_unchanged_minute_eta_confirms_independent_converging_points(self):
        state = {}
        for offset in (0, 30, 60):
            state = self.advance(state, offset, NOW + 240, NOW + 240)
        self.assertEqual(state["_landing_alert"]["samples"], 3)
        self.assert_current_warning(state, NOW + 60)

    def test_one_minute_eta_correction_at_warning_boundary_keeps_measured_confidence(self):
        state = {}
        for offset in (0, 30):
            state = self.advance(state, offset, NOW + 240, NOW + 300)
        self.assertEqual(state["_landing_alert"]["samples"], 2)
        self.assertNotIn("_prelanding", state)
        state = self.advance(state, 60, NOW + 240, NOW + 240)
        self.assert_current_warning(state, NOW + 60)

    def test_adjacent_minute_eta_revisions_do_not_erase_measured_convergence(self):
        # A prediction near the half-minute rounding boundary can select
        # neighboring minutes while the measured trajectory keeps converging.
        state = {}
        for offset, eta in ((0, NOW + 240), (30, NOW + 180), (60, NOW + 240)):
            state = self.advance(state, offset, NOW + 210, eta)
            if offset == 30:
                self.assertNotIn("_prelanding", state, "Exactly 150 seconds remains too late")
        self.assert_current_warning(state, NOW + 60)

    def test_revision_larger_than_one_minute_still_requires_new_confirmation(self):
        state = {}
        for offset in (0, 30):
            state = self.advance(state, offset, NOW + 240, NOW + 301)
        self.assertEqual(state["_landing_alert"]["samples"], 2)
        state = self.advance(state, 60, NOW + 240, NOW + 240)
        self.assertEqual(state["_landing_alert"]["samples"], 1)
        self.assertNotIn("_prelanding", state)
        state = self.advance(state, 90, NOW + 240, NOW + 240)
        self.assertEqual(state["_landing_alert"]["samples"], 2)
        self.assertNotIn("_prelanding", state, "New confirmation at 150 seconds cannot reopen the warning window")

    def test_first_valid_point_inside_warning_window_is_not_enough(self):
        state = self.advance({}, 0, NOW + 180, NOW + 180)
        self.assertEqual(state["_landing_alert"]["samples"], 1)
        self.assertNotIn("_prelanding", state)
        state = self.advance(state, 30, NOW + 180, NOW + 180)
        self.assertEqual(state["_landing_alert"]["samples"], 2)
        self.assertNotIn("_prelanding", state)

    def test_refresh_starts_above_delivery_height_without_authorizing_a_warning(self):
        current = approach(NOW - 20, NOW + 290, NOW + 290, speed=170)
        self.assertGreater(current["_live"]["alt"] - ELEVATIONS_FT["BER"], 4500)
        self.assertIsNone(_candidate(current, NOW))
        self.assertTrue(prelanding_refresh_candidate(current, NOW))
        result = update_landing_alert({}, current, NOW)
        self.assertNotIn("_prelanding", result)
        self.assertNotIn("samples", result["_landing_alert"])

    def test_refresh_without_eta_collects_evidence_but_cannot_create_a_warning(self):
        current = approach(NOW - 20, NOW + 240, NOW + 240)
        current["_live"].pop("eta")
        self.assertTrue(prelanding_refresh_candidate(current, NOW))
        self.assertIsNone(_candidate(current, NOW))
        self.assertNotIn("_prelanding", update_landing_alert({}, current, NOW))

    def test_refresh_still_rejects_untrusted_or_stale_position_evidence(self):
        cases = (
            {"on_ground": True}, {"source": "ESTIMATION"},
            {"fr24_id": "different-leg"}, {"dest_iata": "HAM"},
            {"timestamp": iso(NOW - 91)}, {"lat": None},
        )
        for change in cases:
            with self.subTest(change=change):
                current = approach(NOW - 20, NOW + 240, NOW + 240)
                current["_live"].update(change)
                self.assertFalse(prelanding_refresh_candidate(current, NOW))
        for field in ("ata", "aibt", "cancelled", "diverted"):
            with self.subTest(terminal_field=field):
                current = approach(NOW - 20, NOW + 240, NOW + 240)
                current[field] = NOW - 1
                self.assertFalse(prelanding_refresh_candidate(current, NOW))

    def test_minute_eta_and_forty_second_source_lag_do_not_miss_refresh_entry(self):
        # ETA is 20s earlier than the generated touchdown, well inside the
        # original direct-time and descent-time plausibility tolerances.
        touchdown, eta = NOW + 500, NOW + 480
        state, last_fetch, fetches = {}, -60, []
        for offset in range(0, 391, 15):
            now = NOW + offset
            refresh = prelanding_refresh_candidate(state, now)
            if offset % 60 == 0 or refresh and offset - last_fetch >= 30:
                state = dict(state, **approach(now - 40, touchdown, eta, speed=170))
                last_fetch = offset
                fetches.append(offset)
            state = update_landing_alert(state, state, now)
            if "_prelanding" in state:
                self.assert_current_warning(state, now)
                self.assertEqual(eta - now, 180)
                self.assertTrue(any(b - a == 30 for a, b in zip(fetches, fetches[1:])))
                self.assertTrue(all(b - a >= 30 for a, b in zip(fetches, fetches[1:])))
                break
        else:
            self.fail("The minute polling phase entered the strict approach gate too late to confirm before 150 seconds")


if __name__ == "__main__":
    unittest.main()
