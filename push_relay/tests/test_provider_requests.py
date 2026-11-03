"""Request contracts and shared backoff across health checks and flight polling."""
import hashlib
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from push_relay.comatalarm import FlightProvider, SharedAPIStatus


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class Session:
    def __init__(self, codes=None):
        self.calls = []
        self.codes = codes or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        name = next((n for host, n in (("fr24api", "FR24"), ("aeroapi", "FlightAware"),
            ("ber.berlin", "BER"), ("flightstats", "FlightStats"), ("adsb.lol", "ADSB.lol"),
            ("opensky", "OpenSky")) if host in url), None)
        self.calls.append((name, url, kwargs))
        code = self.codes.get(name, 200)
        payload = {"FR24": {"data": []}, "FlightAware": {"flights": []}, "BER": {"data": {"items": []}},
                   "FlightStats": {"data": {}}, "ADSB.lol": {"ac": []}, "OpenSky": {"time": 123}}

        class Response:
            status = code
            headers = {"Content-Type": "text/html; charset=utf-8", "Retry-After": "1200"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                if code != 200:
                    raise AssertionError("Do not consume arbitrary provider error bodies")
                return payload[name]
        return Response()

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class ProviderRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.provider = FlightProvider()
        self.provider.credentials = {"FLIGHTAWARE_API_KEY": "fa-secret"}
        self.status = SharedAPIStatus(self.db, self.provider)

    async def test_health_dates_are_utc_seconds_in_winter_summer_and_at_midnight(self):
        for value in ("2026-11-03T22:03:00.456+01:00", "2026-07-03T22:03:00.456+02:00",
                      "2026-10-25T02:30:00.456+02:00", "2026-10-25T02:30:00.456+01:00",
                      "2026-11-04T00:00:00+01:00"):
            with self.subTest(value=value):
                now, session = epoch(value), Session()
                with patch("push_relay.comatalarm.ClientSession", return_value=session):
                    result = await self.status.probe("fr-secret", self.provider.credentials, now)
                for name, start_key, end_key, fmt in (
                    ("FR24", "flight_datetime_from", "flight_datetime_to", "%Y-%m-%dT%H:%M:%S"),
                    ("FlightAware", "start", "end", "%Y-%m-%dT%H:%M:%SZ")):
                    params = next(kw["params"] for n, _, kw in session.calls if n == name)
                    start = datetime.strptime(params[start_key], fmt).replace(tzinfo=timezone.utc).timestamp()
                    end = datetime.strptime(params[end_key], fmt).replace(tzinfo=timezone.utc).timestamp()
                    self.assertEqual(end, int(now))
                    self.assertEqual(end-start, 86400, "No zero-width range at local midnight")
                    self.assertTrue(result[name].startswith("Erreichbar"))

    async def test_flight_and_previous_arrival_queries_use_the_same_date_contract(self):
        now = epoch("2026-11-03T22:03:00.456+01:00")
        session = Session()
        flight = dict(number="LH207", reference=now-300, origin="BER", destination="FRA",
                      registration="D-AIZD", _central=True)
        with patch("push_relay.comatalarm.ClientSession", return_value=session):
            await self.provider.lookup(flight, "fr-secret", now)
        queries = [kw["params"] for n, _, kw in session.calls if n == "FR24" and "flight_datetime_from" in kw["params"]]
        self.assertEqual(len(queries), 2, "Current flight and actual previous arrival")
        self.assertEqual(queries[0]["flight_datetime_from"], "2026-11-02T23:00:00")
        self.assertEqual(queries[1]["flight_datetime_from"], "2026-11-01T21:03:00")
        self.assertTrue(all(q["flight_datetime_to"] == "2026-11-03T21:03:00" for q in queries))
        aware = next(kw["params"] for n, _, kw in session.calls if n == "FlightAware")
        self.assertEqual(aware, {"start": "2026-11-02T20:58:00Z", "end": "2026-11-04T20:58:00Z"})

    async def test_status_denials_pause_flight_polling_and_expire_without_bypassing(self):
        now = epoch("2026-11-03T21:03:00Z")
        session = Session({"BER": 403, "FlightAware": 429, "FR24": 403})
        flight = dict(number="LH186", reference=now-300, origin="FRA", destination="BER", _central=True)
        with patch("push_relay.comatalarm.ClientSession", return_value=session):
            with self.assertLogs(level="WARNING") as logs:
                result = await self.status.probe("fr-secret", self.provider.credentials, now)
            self.assertEqual(result["BER"], "HTTP 403 · Anbieter verweigert Zugriff")
            self.assertEqual(result["FlightStats"], "OK", "Other providers remain independent")
            self.assertEqual(self.provider.cooldown["ber"], now+1200)
            key = hashlib.sha256(b"fr-secret").hexdigest()
            self.assertEqual(self.provider.cooldown[key], now+1200)
            session.calls.clear()
            await self.provider.lookup(flight, "fr-secret", now+61)
            await self.status.probe("fr-secret", self.provider.credentials, now+62)
            self.assertFalse(any(n in ("BER", "FlightAware", "FR24") for n, _, _ in session.calls))
            session.codes.clear()
            result = await self.status.probe("fr-secret", self.provider.credentials, now+1201)
            self.assertEqual(result["BER"], "OK")
        output = " ".join(logs.output)
        self.assertIn("provider=BER HTTP=403 response=HTML pause_seconds=1200", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("LH", output)

    async def test_polling_denials_also_pause_manual_status_checks(self):
        now = epoch("2026-11-03T21:03:00Z")
        session = Session({"BER": 403, "FlightAware": 403, "FlightStats": 429})
        flight = dict(number="LH186", reference=now-300, origin="FRA", destination="BER", _central=True)
        with patch("push_relay.comatalarm.ClientSession", return_value=session):
            with self.assertLogs(level="WARNING"):
                await self.provider.lookup(flight, "", now)
            session.calls.clear()
            result = await self.status.probe("", self.provider.credentials, now+61)
        for name in ("BER", "FlightAware", "FlightStats"):
            self.assertIn("pausiert", result[name])
            self.assertFalse(any(n == name for n, _, _ in session.calls))


if __name__ == "__main__":
    unittest.main()
