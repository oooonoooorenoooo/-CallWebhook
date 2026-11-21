"""Pending prelanding alerts survive alias changes, never occurrence changes."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from push_relay.landing_alert import prelanding_is_current, update_landing_alert
from push_relay.tests import test_comatalarm as fixtures
from push_relay.tests.test_landing_alert import sample


class PrelandingDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch

    async def asyncSetUp(self):
        await fixtures.ComatAlarmTests.asyncSetUp(self)
        self.flight.update(number="LH194", fr24ID="abcdef", origin="FRA",
                           destination="BER", atd=self.now-3600)
        response = await self.watch(server_polling=True, relay_alerts=True)
        self.assertEqual(response.status, 200)
        first = dict(self.flight, _live=sample(at=self.now-63, eta=self.now+180)["_live"])
        first = update_landing_alert({}, first, self.now-60)
        current = dict(first, _live=sample(at=self.now-3, eta=self.now+180,
                                          distance=15, altitude=2300)["_live"])
        self.warned = update_landing_alert(first, current, self.now)
        self.assertTrue(prelanding_is_current(self.warned, self.warned["_prelanding"], self.now))
        prefs = json.loads(self.device()["preferences"])
        self.pending, = self.comat.events(self.id, first, self.warned, prefs, self.now)
        self.reset_pending([self.warned])
        self.comat.sender.return_value = False
        await self.comat.deliver(self.id, self.now)
        self.comat.sender.assert_awaited_once()
        self.comat.sender.reset_mock()
        self.comat.sender.return_value = True

    def device(self):
        return self.comat.db.execute("SELECT * FROM comat_devices WHERE id=?", (self.id,)).fetchone()

    def reset_pending(self, flights, *, payload=None, preferences=None):
        with self.comat.db:
            self.comat.db.execute("UPDATE comat_devices SET watch=? WHERE id=?", (json.dumps(flights), self.id))
            if preferences is not None:
                self.comat.db.execute("UPDATE comat_devices SET preferences=? WHERE id=?",
                                      (json.dumps(preferences), self.id))
            self.comat.db.execute("DELETE FROM comat_outbox WHERE device=?", (self.id,))
            self.comat.db.execute("DELETE FROM comat_delivered")
            values = list(self.pending)
            if payload is not None:
                values[2] = json.dumps(payload)
            self.comat.db.execute("INSERT INTO comat_outbox VALUES(?,?,?,?,0)", values)
        self.comat.sender.reset_mock()

    def alias(self, **changes):
        return dict(copy.deepcopy(self.warned), id="same-occurrence-new-alias", **changes)

    def assert_discarded(self):
        self.comat.sender.assert_not_awaited()
        self.assertEqual(self.comat.db.execute("SELECT COUNT(*) FROM comat_outbox WHERE sent=0").fetchone()[0], 0)

    async def test_current_id_retries_normally(self):
        await self.comat.deliver(self.id, self.now+1)
        self.comat.sender.assert_awaited_once()

    async def test_watch_alias_change_retries_the_same_warning_exactly_once(self):
        with patch("push_relay.comatalarm.time.time", return_value=self.now+1):
            response = await self.watch(flights=[dict(self.flight, id="same-occurrence-new-alias")],
                                        server_polling=True, relay_alerts=True)
        self.assertEqual(response.status, 200)
        self.comat.sender.assert_awaited_once()
        payload = self.comat.sender.call_args.args[2]
        self.assertEqual(payload["flight_id"], "same-occurrence-new-alias")
        self.assertEqual(payload["flight_state"]["id"], payload["flight_id"])
        self.assertEqual(payload["event_id"], self.pending[1])
        self.assertEqual(payload["prelanding"], self.warned["_prelanding"])
        await self.comat.deliver(self.id, self.now+2)
        self.comat.sender.assert_awaited_once()

    async def test_legacy_payload_can_retry_after_alias_change(self):
        payload = json.loads(self.pending[2])
        payload.pop("flight_occurrence", None)
        self.reset_pending([self.alias()], payload=payload)
        await self.comat.deliver(self.id, self.now+1)
        self.comat.sender.assert_awaited_once()
        self.assertEqual(self.comat.sender.call_args.args[2]["flight_id"], "same-occurrence-new-alias")

    async def test_alias_retry_still_deduplicates_the_apns_destination(self):
        self.reset_pending([self.alias()])
        with self.comat.db:
            self.comat.db.execute("INSERT INTO comat_delivered VALUES(?,?,?,?)",
                                  (self.device()["token"], "production", self.pending[1], self.now))
        await self.comat.deliver(self.id, self.now+1)
        self.comat.sender.assert_not_awaited()
        row = self.comat.db.execute("SELECT sent FROM comat_outbox").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)

    async def test_same_local_id_cannot_transfer_warning_to_another_number_day_or_leg(self):
        for legacy in (False, True):
            for changes in ({"number": "LH195"}, {"reference": self.flight["reference"]+86400},
                            {"fr24ID": "different-leg"}):
                with self.subTest(legacy=legacy, changes=changes):
                    payload = json.loads(self.pending[2])
                    if legacy:
                        payload.pop("flight_occurrence", None)
                    flight = dict(copy.deepcopy(self.warned), **changes)
                    if "fr24ID" in changes:
                        flight["_live"]["fr24_id"] = changes["fr24ID"]
                    self.reset_pending([flight], payload=payload)
                    await self.comat.deliver(self.id, self.now+1)
                    self.assert_discarded()

    async def test_alias_fallback_rejects_another_number_day_leg_or_route(self):
        for changes in ({"number": "LH195"}, {"reference": self.flight["reference"]+86400},
                        {"fr24ID": "different-leg"}, {"origin": "MUC"}):
            with self.subTest(changes=changes):
                self.reset_pending([self.alias(**changes)])
                await self.comat.deliver(self.id, self.now+1)
                self.assert_discarded()

    async def test_changed_warning_marker_is_not_the_queued_event(self):
        flight = copy.deepcopy(self.warned)
        flight["_prelanding"]["triggered_at"] += 1
        self.reset_pending([flight])
        await self.comat.deliver(self.id, self.now+1)
        self.assert_discarded()

    async def test_ambiguous_aliases_do_not_choose_a_recipient_by_list_order(self):
        self.reset_pending([self.alias(), dict(self.alias(), id="second-alias")])
        await self.comat.deliver(self.id, self.now+1)
        self.assert_discarded()

    async def test_expired_or_landed_alias_never_retries(self):
        for flight, at in ((self.alias(), self.now+31), (self.alias(ata=self.now+1), self.now+2)):
            with self.subTest(at=at, landed=bool(flight.get("ata"))):
                self.reset_pending([flight])
                await self.comat.deliver(self.id, at)
                self.assert_discarded()

    async def test_current_master_individual_and_calendar_rules_apply_to_aliases(self):
        for prefs in ({"enabled": False}, {"alarmPrelanding": False},
                      {"active_windows": []}, {"calendar_valid_until": self.now}):
            with self.subTest(preferences=prefs):
                self.reset_pending([self.alias()], preferences=prefs)
                await self.comat.deliver(self.id, self.now+1)
                self.assert_discarded()

    async def test_authorization_rechecks_current_preferences_and_deletion(self):
        with self.comat.db:
            self.comat.db.execute("UPDATE comat_devices SET firebase_uid='approved' WHERE id=?", (self.id,))
        for change in ("preferences", "delete"):
            with self.subTest(change=change):
                self.reset_pending([self.alias()], preferences={})
                async def authorize(uid):
                    with self.comat.db:
                        if change == "preferences":
                            self.comat.db.execute("UPDATE comat_devices SET preferences=? WHERE id=?",
                                                  (json.dumps({"alarmPrelanding": False}), self.id))
                        else:
                            self.comat.shared_monitoring.remove([dict(self.flight, deleted_at=self.now+1)], self.now+1)
                    return {"uid": uid, "administrator": True}
                self.comat.stand_access = AsyncMock(side_effect=authorize)
                await self.comat.deliver(self.id, self.now+1)
                self.assert_discarded()

    async def test_unconfirmed_approval_does_not_send_and_can_retry_when_restored(self):
        self.reset_pending([self.alias()])
        with self.comat.db:
            self.comat.db.execute("UPDATE comat_devices SET firebase_uid='approved' WHERE id=?", (self.id,))
        self.comat.stand_access = AsyncMock(return_value=None)
        await self.comat.deliver(self.id, self.now+1)
        self.comat.sender.assert_not_awaited()
        self.assertEqual(self.comat.db.execute("SELECT COUNT(*) FROM comat_outbox WHERE sent=0").fetchone()[0], 1)
        self.comat.stand_access.return_value = {"uid": "approved", "administrator": True}
        await self.comat.deliver(self.id, self.now+2)
        self.comat.sender.assert_awaited_once()

    async def test_shared_and_operator_deletion_markers_block_stale_aliases(self):
        with self.comat.db:
            self.comat.db.execute("UPDATE comat_devices SET firebase_uid='approved' WHERE id=?", (self.id,))
        self.comat.stand_access = AsyncMock(return_value={"uid": "approved", "administrator": True})
        for table in ("comat_shared_removed", "comat_removed"):
            with self.subTest(table=table):
                self.reset_pending([self.alias()])
                with self.comat.db:
                    self.comat.db.execute("DELETE FROM comat_shared_removed")
                    self.comat.db.execute("DELETE FROM comat_removed")
                    if table == "comat_shared_removed":
                        self.comat.db.execute("INSERT INTO comat_shared_removed VALUES(?,?,?)",
                                              ("deleted", json.dumps(self.flight), self.now+1))
                    else:
                        self.comat.db.execute("INSERT INTO comat_removed VALUES(?,?)", ("deleted", json.dumps(self.flight)))
                await self.comat.deliver(self.id, self.now+1)
                self.assert_discarded()

    async def test_shared_deletion_does_not_apply_to_an_unbound_registration(self):
        self.reset_pending([self.alias()])
        with self.comat.db:
            self.comat.db.execute("INSERT INTO comat_shared_removed VALUES(?,?,?)",
                                  ("deleted", json.dumps(self.flight), self.now+1))
        await self.comat.deliver(self.id, self.now+1)
        self.comat.sender.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
