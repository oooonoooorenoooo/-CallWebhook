"""Exercise the real persisted monitor/outbox; provider/APNs stay offline."""
import json
import unittest

from push_relay.tests import test_comatalarm as fixtures
from push_relay.tests.test_landing_alert import sample


class LandingPipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ComatAlarmTests.asyncSetUp
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch
    add_peer = fixtures.ComatAlarmTests.add_peer

    def live(self, at=0, distance=20, altitude=3000, eta=240, **changes):
        return sample(at=self.now+at-3, eta=self.now+eta, distance=distance,
                      altitude=altitude, **changes)["_live"]

    async def prepare(self, preferences=None):
        self.flight.update(number="LH194", fr24ID="abcdef", origin="FRA",
                           destination="BER", atd=self.now-3600)
        response = await self.watch(server_polling=True, relay_alerts=True,
                                    preferences=preferences or {})
        self.assertEqual(response.status, 200)

    async def first_observation(self):
        self.comat.provider.lookup.return_value = {"_live": self.live()}
        for offset in (0, 15, 30, 45):
            await self.comat.tick(self.now+offset)
        self.comat.sender.assert_not_awaited()

    async def test_closed_apps_receive_one_warning_each_from_one_shared_query(self):
        await self.prepare()
        await self.add_peer(dict(self.flight, id="peer-flight"), server_polling=True, relay_alerts=True)
        await self.first_observation()
        self.comat.provider.lookup.return_value = {"_live": self.live(at=60, distance=15, altitude=2300)}
        await self.comat.tick(self.now+60)
        self.assertEqual(self.comat.provider.lookup.await_count, 2)
        self.assertEqual(self.comat.sender.await_count, 2)
        for call in self.comat.sender.call_args_list:
            payload = call.args[2]
            self.assertEqual(payload["flag"], "alarmPrelanding")
            self.assertEqual(payload["expires_at"], self.now+90)
        for offset in (75, 90, 105, 120):
            await self.comat.tick(self.now+offset)
        self.assertEqual(self.comat.sender.await_count, 2)

    async def test_warning_boundary_is_checked_without_provider_refresh(self):
        await self.prepare()
        self.comat.provider.lookup.return_value = {"_live": self.live(distance=23, eta=270)}
        await self.comat.tick(self.now)
        self.comat.provider.lookup.return_value = {"_live": self.live(at=60, distance=18, altitude=2300, eta=270)}
        await self.comat.tick(self.now+60)
        self.comat.sender.assert_not_awaited()
        await self.comat.tick(self.now+75)
        await self.comat.tick(self.now+90)
        self.assertEqual(self.comat.provider.lookup.await_count, 2)
        self.assertEqual(self.comat.sender.await_count, 1)

    async def test_disabled_warning_does_not_suppress_normal_landing_alarm(self):
        await self.prepare({"alarmPrelanding": False})
        await self.first_observation()
        self.comat.provider.lookup.return_value = {"_live": self.live(at=60, distance=15, altitude=2300)}
        await self.comat.tick(self.now+60)
        self.comat.sender.assert_not_awaited()
        self.comat.provider.lookup.return_value = {"ata": self.now+120}
        await self.comat.tick(self.now+121)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.assertEqual(self.comat.sender.call_args.args[2]["flag"], "alarmLanded")

    async def test_failed_warning_is_not_retried_after_its_short_deadline(self):
        await self.prepare()
        await self.first_observation()
        self.comat.sender.return_value = False
        self.comat.provider.lookup.return_value = {"_live": self.live(at=60, distance=15, altitude=2300)}
        await self.comat.tick(self.now+60)
        self.assertEqual(self.comat.sender.await_count, 1)
        await self.comat.tick(self.now+91)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.assertEqual(self.comat.db.execute("SELECT COUNT(*) FROM comat_outbox WHERE sent=0").fetchone()[0], 0)

    async def test_pending_warning_is_discarded_when_landed_before_retry(self):
        await self.prepare()
        await self.first_observation()
        self.comat.sender.return_value = False
        self.comat.provider.lookup.return_value = {"_live": self.live(at=60, distance=15, altitude=2300)}
        await self.comat.tick(self.now+60)
        saved = self.comat.db.execute("SELECT watch FROM comat_devices WHERE id=?", (self.id,)).fetchone()[0]
        flights = json.loads(saved)
        flights[0]["ata"] = self.now+61
        with self.comat.db:
            self.comat.db.execute("UPDATE comat_devices SET watch=? WHERE id=?", (json.dumps(flights), self.id))
        await self.comat.deliver(self.id, self.now+62)
        self.assertEqual(self.comat.sender.await_count, 1)


if __name__ == "__main__":
    unittest.main()
