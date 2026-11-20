"""Exercise the real persisted monitor/outbox; provider/APNs stay offline."""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

from push_relay.tests import test_comatalarm as fixtures
from push_relay.tests.test_landing_alert import sample
from push_relay.comatalarm import FlightProvider


class FullPositionSession:
    """Offline HTTP boundary; production provider parsing/batching stay real."""
    def __init__(self, now):
        self.now = now
        self.tick = now
        self.live = {}
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((self.tick, path))
        if path.endswith("/flight-summary/full"):
            payload = {"data": [{"fr24_id": "abcdef", "flight": "LH194", "orig_iata": "FRA",
                                 "dest_iata": "BER", "datetime_takeoff": self.now-3600}]}
        elif path.endswith("/live/flight-positions/full"):
            payload = {"data": [dict(self.live)]}
        elif "ber.berlin-airport.de" in url:
            payload = {"data": {"items": []}}
        else:
            payload = {"data": {}}

        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                return payload
        return Response()


class LandingPipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ComatAlarmTests.asyncSetUp
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch
    add_peer = fixtures.ComatAlarmTests.add_peer

    def live(self, at=0, distance=20, altitude=3000, eta=240, **changes):
        return sample(at=self.now+at-3, eta=self.now+eta, distance=distance,
                      altitude=altitude, **changes)["_live"]

    async def prepare(self, preferences=None, **fields):
        self.flight.update(number="LH194", fr24ID="abcdef", origin="FRA",
                           destination="BER", atd=self.now-3600)
        response = await self.watch(server_polling=True, relay_alerts=True,
                                    preferences=preferences or {}, **fields)
        self.assertEqual(response.status, 200)

    async def first_observation(self):
        self.comat.provider.lookup.return_value = {"_live": self.live()}
        for offset in (0, 15, 30, 45):
            await self.comat.tick(self.now+offset)
        self.comat.sender.assert_not_awaited()

    async def test_closed_apps_receive_one_warning_each_from_one_shared_query(self):
        await self.prepare()
        await self.add_peer(dict(self.flight, id="peer-flight"), server_polling=True, relay_alerts=True)
        self.assertNotIn("on_ground", self.live(), "the full provider schema has no ground flag")
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

    async def test_real_full_response_and_targeted_refresh_reach_both_closed_apps(self):
        async def access(uid):
            return {"uid": uid, "administrator": uid == "reno"} if uid in {"reno", "colleague"} else None
        self.comat.stand_access = AsyncMock(side_effect=access)
        self.comat.apple_access.store.device_uid = Mock(side_effect=lambda token: token)
        await self.prepare(firebase_token="reno")
        await self.add_peer(dict(self.flight, id="peer-real-flight"), server_polling=True, relay_alerts=True,
                            firebase_token="colleague")
        self.comat.provider = FlightProvider()
        session = FullPositionSession(self.now)
        eta = self.now+500  # Old 60s polling misses this phase with 20s-old data.
        with patch("push_relay.comatalarm.ClientSession", return_value=session):
            for offset in range(0, 346, 15):
                session.tick = self.now+offset
                observed = session.tick-20
                remaining = eta-observed
                session.live = sample(at=observed, eta=eta, distance=170*1.852*remaining/3600,
                                      altitude=700*remaining/60)["_live"]
                self.assertNotIn("on_ground", session.live)
                await self.comat.tick(session.tick)
        self.assertEqual(self.comat.sender.await_count, 2, "one shared official-schema approach must reach both closed apps")
        for call in self.comat.sender.call_args_list:
            payload = call.args[2]
            self.assertEqual(payload["flag"], "alarmPrelanding")
            self.assertEqual(payload["prelanding"]["eta"], eta)
            self.assertTrue(150 < eta-payload["prelanding"]["triggered_at"] <= 180)
        live_times = [at for at, path in session.calls if path.endswith("/live/flight-positions/full")]
        self.assertTrue(any(b-a == 30 for a, b in zip(live_times, live_times[1:])),
                        "the targeted final-approach refresh must cross the former blind phase")
        self.assertTrue(all(b-a >= 30 for a, b in zip(live_times, live_times[1:])),
                        "closed-device fanout must not duplicate or accelerate provider requests")
        summary_times = [at for at, path in session.calls if path.endswith("/flight-summary/full")]
        self.assertTrue(all(b-a >= 60 for a, b in zip(summary_times, summary_times[1:])),
                        "the faster final approach must not also accelerate schedule/summary calls")

    async def test_warning_respects_each_devices_current_shift_window(self):
        await self.prepare({"enabled": True, "alarmPrelanding": True,
                            "active_windows": [[self.now-60, self.now+3600]],
                            "calendar_valid_until": self.now+3600})
        await self.add_peer(dict(self.flight, id="off-shift-peer"), server_polling=True, relay_alerts=True,
                            preferences={"enabled": True, "alarmPrelanding": True, "active_windows": [],
                                         "calendar_valid_until": self.now+3600})
        await self.first_observation()
        self.comat.provider.lookup.return_value = {"_live": self.live(at=60, distance=15, altitude=2300)}
        await self.comat.tick(self.now+60)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.assertEqual(self.comat.sender.call_args.args[0], "a"*64,
                         "the active-shift device receives the warning while the free colleague stays muted")

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
