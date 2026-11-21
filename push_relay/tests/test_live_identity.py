"""Optional FR24 route fields and conservative live-only leg resolution."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from push_relay.comatalarm import FlightProvider
from push_relay.live_identity import select_live_position as select_live_result
from push_relay.tests import test_comatalarm as fixtures
from push_relay.tests.test_fr24_batch import Response
from push_relay.tests.test_landing_alert import NOW, sample


def select_live_position(rows, flight, now):
    return select_live_result(rows, flight, now)[0]


def official_row(at=NOW, **changes):
    """Use ISO timestamps, optional route fields and no on_ground extension."""
    row = sample(at=at, **changes)["_live"]
    for field in ("timestamp", "eta"):
        if isinstance(row.get(field), (int, float)):
            row[field] = datetime.fromtimestamp(row[field], timezone.utc).isoformat().replace("+00:00", "Z")
    row["squawk"] = "1234"
    return row


class LiveIdentityTests(unittest.TestCase):
    def setUp(self):
        self.flight = sample()

    def test_exact_leg_survives_missing_optional_route_fields(self):
        for changes in ({"orig_iata": None}, {"dest_iata": None}, {"orig_iata": "", "dest_iata": ""}):
            with self.subTest(changes=changes):
                row = official_row(**changes)
                self.assertIs(select_live_position([row], self.flight, NOW), row)
        row = official_row()
        row.pop("orig_iata")
        row.pop("dest_iata")
        self.assertIs(select_live_position([row], self.flight, NOW), row)

    def test_exact_leg_still_rejects_explicit_route_conflicts(self):
        for changes in ({"orig_iata": "HAM"}, {"dest_iata": "HAM"}, {"dest_iata": True}):
            with self.subTest(changes=changes):
                self.assertIsNone(select_live_position([official_row(**changes)], self.flight, NOW))

    def test_route_conflict_blocks_other_rows_of_the_same_exact_leg(self):
        row = official_row()
        conflict = official_row(at=NOW+5, dest_iata="HAM")
        self.assertEqual(select_live_result([row, conflict], self.flight, NOW), (None, "route_conflict"))

    def test_exact_leg_wins_over_same_number_and_preserves_codeshares(self):
        matching = official_row(flight="LH999")
        other = official_row(at=NOW+5, fr24_id="abcdef1")
        self.assertIs(select_live_position([other, matching], self.flight, NOW), matching)
        self.assertIsNone(select_live_position([other], self.flight, NOW))

    def test_exact_leg_can_continue_across_midnight(self):
        row = official_row()
        self.flight["reference"] = NOW-86400
        self.assertIs(select_live_position([row], self.flight, NOW), row)

    def test_provider_identity_is_an_opaque_exact_string(self):
        for identifier in ("right", "provider-leg/42", "AbCdEf"):
            with self.subTest(identifier=identifier):
                row = official_row(fr24_id=identifier)
                flight = dict(self.flight, fr24ID=identifier)
                self.assertIs(select_live_position([row], flight, NOW), row)
                self.assertIsNone(select_live_position([row], dict(flight, fr24ID=identifier.upper()), NOW))

    def test_unresolved_leg_can_adopt_an_opaque_provider_identity(self):
        unresolved = dict(self.flight, fr24ID="TEMP_example")
        row = official_row(fr24_id="provider-leg/42")
        self.assertIs(select_live_position([row], unresolved, NOW), row)
        other = official_row(fr24_id="provider-leg/43")
        self.assertEqual(select_live_result([row, other], unresolved, NOW), (None, "ambiguous"))

    def test_unresolved_leg_requires_unique_current_number_and_complete_route(self):
        row = official_row()
        for identifier in (None, "", "TEMP_example", "NS_example"):
            with self.subTest(identifier=identifier):
                self.assertIs(select_live_position([row], dict(self.flight, fr24ID=identifier), NOW), row)
        unresolved = dict(self.flight, fr24ID="TEMP_example")
        for changes in ({"flight": "LH999"}, {"orig_iata": None}, {"dest_iata": None},
                        {"orig_iata": "HAM"}, {"dest_iata": "HAM"}, {"fr24_id": "TEMP_example"}):
            with self.subTest(changes=changes):
                self.assertIsNone(select_live_position([official_row(**changes)], unresolved, NOW))
        for changes in ({"origin": ""}, {"destination": ""}, {"reference": None}):
            with self.subTest(changes=changes):
                self.assertIsNone(select_live_position([row], dict(unresolved, **changes), NOW))

    def test_unresolved_leg_never_borrows_same_number_from_another_day(self):
        row = official_row()
        for days in (-1, 1):
            unresolved = dict(self.flight, fr24ID="TEMP_example", reference=NOW+days*86400)
            self.assertIsNone(select_live_position([row], unresolved, NOW))

    def test_unresolved_leg_uses_berlin_day_not_utc_day(self):
        now = datetime(2026, 11, 21, 23, 10, tzinfo=timezone.utc).timestamp()
        same_berlin_day = now+3600  # UTC dates differ; Berlin dates agree.
        unresolved = dict(self.flight, fr24ID="TEMP_example", reference=same_berlin_day)
        row = official_row(at=now)
        self.assertIs(select_live_position([row], unresolved, now), row)
        unresolved["reference"] = now-3600  # Same UTC day; previous Berlin day.
        self.assertIsNone(select_live_position([row], unresolved, now))

    def test_multiple_matching_ids_are_ambiguous_even_with_different_timestamps(self):
        unresolved = dict(self.flight, fr24ID="TEMP_example")
        one, two = official_row(), official_row(at=NOW+5, fr24_id="abcdef1")
        self.assertIsNone(select_live_position([one, two], unresolved, NOW))
        newer_same_leg = official_row(at=NOW+5)
        self.assertIs(select_live_position([one, newer_same_leg], unresolved, NOW), newer_same_leg)

    def test_stale_future_or_invalid_positions_do_not_resolve_a_leg(self):
        unresolved = dict(self.flight, fr24ID="TEMP_example")
        for changes in ({"timestamp": NOW-121}, {"timestamp": NOW+11}, {"timestamp": None},
                        {"lat": True}, {"lat": 91}, {"lon": float("nan")}, {"fr24_id": None}):
            with self.subTest(changes=changes):
                self.assertIsNone(select_live_position([official_row(**changes)], unresolved, NOW))


class LiveOnlyHTTP:
    """Actual provider parsing and monitor; only HTTP responses are synthetic."""
    def __init__(self):
        self.live = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        if url.endswith("/live/flight-positions/full"):
            return Response({"data": [self.live]})
        if url.endswith("/flight-summary/full"):
            return Response({"data": []})
        return Response({"data": {"items": []}})


class LiveIdentityPipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ComatAlarmTests.asyncSetUp
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch

    async def run_approach(self, identifier, missing_route=False):
        self.comat.stand_access = AsyncMock(return_value={"uid": "reno", "administrator": True})
        self.comat.apple_access.store.device_uid = Mock(return_value="reno")
        self.flight.update(number="LH194", fr24ID=identifier, origin="FRA", destination="BER", atd=self.now-3600)
        response = await self.watch(firebase_token="reno", server_polling=True, relay_alerts=True)
        self.assertEqual(response.status, 200)
        self.comat.provider = FlightProvider()
        http = LiveOnlyHTTP()
        with patch("push_relay.comatalarm.ClientSession", return_value=http), \
             patch("push_relay.flight_schedule.schedule_lookup", AsyncMock(return_value={})), \
             patch("push_relay.flight_schedule.aware_lookup", AsyncMock(return_value={})):
            for offset in (0, 15, 30, 45, 60, 75):
                if offset in (0, 60):
                    http.live = official_row(at=self.now+offset-3, eta=self.now+240,
                                             distance=20 if offset == 0 else 15,
                                             altitude=3000 if offset == 0 else 2300)
                    if missing_route:
                        http.live.pop("orig_iata")
                        http.live["dest_iata"] = None
                await self.comat.tick(self.now+offset)
        saved = json.loads(self.comat.db.execute("SELECT watch FROM comat_devices WHERE id=?", (self.id,)).fetchone()[0])[0]
        self.assertEqual(saved["fr24ID"], "abcdef")
        self.assertEqual(saved["destination"], "BER")
        self.assertEqual(saved["_live"]["fr24_id"], "abcdef")
        warnings = [call for call in self.comat.sender.call_args_list if call.args[2]["flag"] == "alarmPrelanding"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(150 < warnings[0].args[2]["prelanding"]["eta"]-warnings[0].args[2]["prelanding"]["triggered_at"] <= 180)

    async def test_exact_leg_missing_destination_reaches_persisted_outbox_and_apns(self):
        await self.run_approach("abcdef", missing_route=True)

    async def test_empty_summary_adopts_unique_live_leg_and_delivers_warning(self):
        await self.run_approach("TEMP_example")

    async def test_new_route_conflict_invalidates_the_previous_live_sample(self):
        provider = FlightProvider()
        http = LiveOnlyHTTP()
        http.live = official_row(at=self.now, eta=self.now+180, dest_iata="HAM")
        flight = sample(at=self.now-10, eta=self.now+180)
        flight.update(_central=True, reference=self.now-100)
        with patch("push_relay.comatalarm.ClientSession", return_value=http), \
             patch("push_relay.flight_schedule.schedule_lookup", AsyncMock(return_value={})), \
             patch("push_relay.flight_schedule.aware_lookup", AsyncMock(return_value={})):
            changes = await provider.lookup(flight, "synthetic-offline-token", self.now)
        self.assertEqual(changes["_live_lookup_status"], "route_conflict")
        self.assertEqual(dict(flight, **changes)["_live"], {})
