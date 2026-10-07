"""ComatAlarm alert registrations and bounded background flight monitoring.

Separate credentials, tables and APNs topic; never uses CallWebhook VoIP grants.
"""
import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout, ClientError, web

BUNDLE_ID = "de.comatalarm.app.ios.U98PKCA4W7"
FLAGS = ("alarmTakeoff", "alarmLanded", "alarmGate", "alarmParking", "alarmCancelled", "alarmDiverted")
FIELDS = ("id", "number", "reference", "fr24ID", "origin", "destination", "atd", "ata", "aibt", "gate", "cancelled", "diverted")


def stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 1_000_000_000 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def transitions(old, new):
    number = new["number"]
    events = []
    for field, flag, title, body in (
        ("atd", "alarmTakeoff", "Abgehoben", f"{number} ist abgehoben."),
        ("ata", "alarmLanded", "Gelandet", f"{number} ist gelandet."),
        ("aibt", "alarmParking", "Am Stand angekommen", f"{number} hat seine Abstellposition erreicht."),
        ("cancelled", "alarmCancelled", "Flug gestrichen", f"{number} wurde gestrichen."),
        ("diverted", "alarmDiverted", "Flug umgeleitet", f"{number} wurde umgeleitet."),
    ):
        if not old.get(field) and new.get(field):
            events.append((field, flag, title, body))
    if new.get("gate") and old.get("gate") != new["gate"]:
        events.append(("gate:" + new["gate"], "alarmGate", "Gate-Update", f"{number} · Gate {new['gate']}"))
    return events


class FlightProvider:
    def __init__(self):
        self.cooldown = {}
        self.cache = {}

    async def lookup(self, flight, token, now):
        cache_key = (hashlib.sha256(token.encode()).hexdigest(), flight["number"], flight["reference"], flight.get("fr24ID"))
        cached = self.cache.get(cache_key)
        if cached and now - cached[0] < 60:
            return dict(cached[1])
        self.cache = {k: v for k, v in self.cache.items() if now - v[0] < 120}
        result = {}
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            async def fr24(path, params):
                identity = cache_key[0]
                if self.cooldown.get(identity, 0) > now:
                    raise RuntimeError("FR24: Abfrage pausiert (Tarif, Token oder Limit prüfen)")
                async with session.get("https://fr24api.flightradar24.com/api/" + path, params=params,
                        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Accept-Version": "v1"}, allow_redirects=False) as response:
                    if response.status in (401, 402, 403, 429):
                        self.cooldown[identity] = now + 300
                    if response.status != 200:
                        raise RuntimeError(f"FR24: HTTP {response.status}")
                    body = await response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                        raise RuntimeError("FR24: Antwortformat ungültig")
                    return body["data"]

            identifier = flight.get("fr24ID")
            provider_error = ""
            try:
                if token:
                    if identifier:
                        params = {"flight_ids": identifier}
                    else:
                        start = datetime.fromtimestamp(flight["reference"], ZoneInfo("Europe/Berlin")).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                        if start >= now:
                            raise RuntimeError("Flugtag liegt in der Zukunft")
                        params = {"flights": flight["number"], "flight_datetime_from": datetime.fromtimestamp(start, timezone.utc).isoformat(), "flight_datetime_to": datetime.fromtimestamp(min((datetime.fromtimestamp(start, ZoneInfo("Europe/Berlin")) + timedelta(days=1)).timestamp(), now), timezone.utc).isoformat()}
                    rows = await fr24("flight-summary/full", params)
                    matches = [r for r in rows if isinstance(r, dict) and
                        (not identifier or r.get("fr24_id") == identifier) and
                        (identifier or r.get("flight") == flight["number"]) and
                        (not flight.get("destination") or r.get("dest_iata") == flight["destination"])]
                    if matches:
                        row = min(matches, key=lambda r: abs((stamp(r.get("datetime_takeoff")) or flight["reference"]) - flight["reference"]))
                        identifier = row.get("fr24_id")
                        result.update(fr24ID=identifier, atd=stamp(row.get("datetime_takeoff")), ata=stamp(row.get("datetime_landed")))
                        actual, planned = row.get("dest_iata_actual"), row.get("dest_iata")
                        if actual and planned and actual != planned:
                            result["diverted"] = True
                    if identifier and (result.get("ata") or flight.get("ata")):
                        rows = await fr24("historic/flight-events/full", {"flight_ids": identifier, "event_types": "landed,gate_arrival"})
                        for row in rows:
                            if not isinstance(row, dict) or row.get("fr24_id") != identifier:
                                continue
                            for event in row.get("events", []):
                                if not isinstance(event, dict):
                                    continue
                                when = next((stamp(event.get(k)) for k in ("timestamp", "time", "event_time", "datetime") if stamp(event.get(k))), None)
                                if event.get("type") == "gate_arrival" and when and when <= now + 60 and when >= (result.get("ata") or flight.get("ata") or when):
                                    result["aibt"] = when
            except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                provider_error = "FR24: Token, Tarif oder Verbindung prüfen"
            # BER remains the gate source; a passenger gate is never a GPS stand.
            date = datetime.fromtimestamp(flight["reference"], ZoneInfo("Europe/Berlin")).date()
            params = {"arrivalDeparture": "D" if flight.get("origin") == "BER" else "A", "dateFrom": date.isoformat() + "T00:00:00", "dateUntil": (date + timedelta(days=1)).isoformat(), "search": flight["number"], "lang": "de", "page": "1", "terminal": ""}
            try:
                async with session.get("https://ber.berlin-airport.de/api.flights.json", params=params, allow_redirects=False) as response:
                    if response.status == 200:
                        body = await response.json()
                        for row in body.get("data", {}).get("items", []):
                            aliases = [row.get("flight_number", "")] + row.get("code_shares", [])
                            if flight["number"] not in [re.sub(r"\s", "", a).upper() for a in aliases if isinstance(a, str)]:
                                continue
                            gate = row.get("gate")
                            if isinstance(gate, str) and gate.strip() and gate != "-":
                                result["gate"] = gate[:32]
                            status = str(row.get("status", row.get("status_text", ""))).lower()
                            if any(x in status for x in ("cancel", "annull", "gestrichen")):
                                result["cancelled"] = True
                            break
            except (ValueError, TypeError, AttributeError, ClientError, OSError, asyncio.TimeoutError):
                pass
        result = {k: v for k, v in result.items() if v is not None}
        if provider_error:
            result["_error"] = provider_error
        self.cache[cache_key] = (now, result)
        return result


class ComatAlarm:
    def __init__(self, relay, sender=None, provider=None):
        self.relay, self.db, self.sender = relay, relay.db, sender
        self.provider = provider or FlightProvider()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS comat_devices (
                id TEXT PRIMARY KEY, credential TEXT NOT NULL, token TEXT NOT NULL,
                environment TEXT NOT NULL, watch TEXT NOT NULL, preferences TEXT NOT NULL,
                api_token TEXT NOT NULL, foreground_until REAL NOT NULL, expires REAL NOT NULL,
                last_poll REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS comat_outbox (
                device TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL,
                created REAL NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(device,event));
        """)

    def device(self, request):
        value = request.headers.get("Authorization", "")
        if not re.fullmatch(r"Bearer [0-9a-f]{64}", value):
            raise web.HTTPUnauthorized()
        row = self.db.execute("SELECT * FROM comat_devices WHERE credential=?", (hashlib.sha256(value[7:].encode()).hexdigest(),)).fetchone()
        if not row:
            raise web.HTTPUnauthorized()
        return row

    async def register(self, request):
        self.relay.limit("comat-register:" + (request.remote or "unknown"), 10, 60)
        configured = os.environ.get("COMATALARM_SETUP_KEY", "")
        supplied = request.headers.get("X-ComatAlarm-Setup-Key", "")
        if len(configured) < 32 or not hmac.compare_digest(configured, supplied):
            raise web.HTTPUnauthorized()
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Invalid registration")
        identifier = str(UUID(body["device_id"]))
        token, environment = body.get("token"), body.get("environment")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32,512}", token) or environment not in (("production", "development") if self.relay.allow_development else ("production",)):
            raise ValueError("Invalid registration")
        credential = secrets.token_hex(32)
        if self.db.execute("SELECT count(*) FROM comat_devices").fetchone()[0] >= 10 and not self.db.execute("SELECT 1 FROM comat_devices WHERE id=?", (identifier,)).fetchone():
            raise web.HTTPConflict(text="Device limit reached")
        with self.db:
            self.db.execute("""INSERT INTO comat_devices(id,credential,token,environment,watch,preferences,api_token,foreground_until,expires)
                VALUES(?,?,?,?, '[]','{}','',0,?) ON CONFLICT(id) DO UPDATE SET credential=excluded.credential,token=excluded.token,environment=excluded.environment,expires=excluded.expires""",
                (identifier, hashlib.sha256(credential.encode()).hexdigest(), token, environment, time.time() + 86400))
        return web.json_response({"credential": credential, "registered": True, "version": 1})

    async def watch(self, request):
        device = self.device(request)
        self.relay.limit("comat-watch:" + device["id"], 12, 60)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Invalid watch")
        flights, preferences = body.get("flights"), body.get("preferences")
        if not isinstance(flights, list) or len(flights) > 2 or not isinstance(preferences, dict):
            raise ValueError("Invalid watch")
        now = time.time()
        prior = {f["id"]: f for f in json.loads(device["watch"])}
        clean = []
        for f in flights:
            if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not 1 <= len(f["id"]) <= 160 or not isinstance(f.get("number"), str) or not re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,5}", f["number"]):
                raise ValueError("Invalid flight")
            reference = f.get("reference")
            if not isinstance(reference, (float, int)) or isinstance(reference, bool) or not math.isfinite(reference) or abs(reference - now) > 48 * 3600:
                raise ValueError("Invalid reference")
            row = {k: f[k] for k in FIELDS if k in f}
            for field in ("atd", "ata", "aibt"):
                if field in row and (stamp(row[field]) is None or not reference - 86400 <= row[field] <= now + 60):
                    raise ValueError("Invalid event time")
            for field in ("fr24ID", "origin", "destination", "gate"):
                if field in row and (not isinstance(row[field], str) or len(row[field]) > 64):
                    raise ValueError("Invalid flight field")
            for field in ("cancelled", "diverted"):
                if field in row and not isinstance(row[field], bool):
                    raise ValueError("Invalid flag")
            old = prior.get(row["id"], {})
            for key in ("atd", "ata", "aibt", "fr24ID", "cancelled", "diverted"):
                if old.get(key):
                    row[key] = old[key]
            if any(entry["id"] == row["id"] for entry in clean):
                raise ValueError("Duplicate flight")
            clean.append(row)
        prefs = {key: preferences.get(key, True) for key in FLAGS + ("enabled",)}
        if not all(isinstance(v, bool) for v in prefs.values()) or not isinstance(body.get("foreground"), bool):
            raise ValueError("Invalid preferences")
        windows = preferences.get("active_windows")
        if windows is not None:
            if not isinstance(windows, list) or len(windows) > 100:
                raise ValueError("Invalid calendar windows")
            for window in windows:
                if not isinstance(window, list) or len(window) != 2 or not all(isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in window) or not now-86400 <= window[0] < window[1] <= now+2*86400:
                    raise ValueError("Invalid calendar window")
            prefs["active_windows"] = windows
        api_token = body.get("fr24_token", device["api_token"])
        if not isinstance(api_token, str) or len(api_token) > 8192 or '\n' in api_token or '\r' in api_token:
            raise ValueError("Invalid API token")
        with self.db:
            self.db.execute("UPDATE comat_devices SET watch=?,preferences=?,api_token=?,foreground_until=?,expires=? WHERE id=?", (json.dumps(clean),json.dumps(prefs),api_token,now+90 if body["foreground"] else 0,now+86400,device["id"]))
        return web.json_response({"watching": len(clean), "background_ready": bool(api_token), "expires": now+86400})

    async def state(self, request):
        row = self.device(request)
        return web.json_response({"flights": json.loads(row["watch"]), "last_poll": row["last_poll"], "error": row["error"], "background_ready": bool(row["api_token"]), "expires": row["expires"]})

    async def revoke(self, request):
        row = self.device(request)
        with self.db:
            self.db.execute("DELETE FROM comat_devices WHERE id=?", (row["id"],))
            self.db.execute("DELETE FROM comat_outbox WHERE device=?", (row["id"],))
        return web.json_response({"revoked": True})

    async def test(self, request):
        row = self.device(request)
        self.relay.limit("comat-test:" + row["id"], 3, 60)
        try:
            accepted = bool(self.sender and await self.sender(row["token"], row["environment"], {"title":"ComatAlarm · Push-Test", "body":"Diese Meldung kommt über deinen Home-Assistant-Push-Dienst.", "event_id":secrets.token_hex(16)}))
        except Exception:
            accepted = False
        return web.json_response({"accepted": accepted}, status=200 if accepted else 502)

    @staticmethod
    def enabled(prefs, now):
        return prefs.get("enabled", True) and ("active_windows" not in prefs or any(start <= now < end for start, end in prefs["active_windows"]))

    async def tick(self, now=None):
        now = time.time() if now is None else now
        for device in self.db.execute("SELECT * FROM comat_devices WHERE expires>? AND foreground_until<=?", (now,now)).fetchall():
            prefs = json.loads(device["preferences"])
            flights = json.loads(device["watch"])
            if now-device["last_poll"] >= 60:
                error, pending = "", []
                for flight in flights:
                    if flight.get("aibt") or flight.get("cancelled") or flight.get("diverted"):
                        continue
                    try:
                        changes = dict(await self.provider.lookup(flight, device["api_token"], now))
                        error = changes.pop("_error", error)
                        updated = dict(flight, **changes)
                        for event, flag, title, body in transitions(flight, updated):
                            if self.enabled(prefs, now) and prefs.get(flag, True):
                                event_id = hashlib.sha256((flight["id"] + ':' + event).encode()).hexdigest()
                                payload = json.dumps({"title":title,"body":body,"flight_id":flight["id"],"event_id":event_id,"flag":flag})
                                pending.append((device["id"],event_id,payload,now))
                        flight.update(changes)
                    except Exception:
                        error = "Flugabfrage fehlgeschlagen; Token, Tarif und Verbindung prüfen"
                # Compare-and-swap the entire snapshot after all network I/O. Removed
                # flights, changed preferences and foreground takeovers win the race.
                with self.db:
                    changed = self.db.execute("UPDATE comat_devices SET watch=?,last_poll=?,error=? WHERE id=? AND watch=? AND preferences=? AND foreground_until<=? AND expires>?",(json.dumps(flights),now,error,device["id"],device["watch"],device["preferences"],now,now)).rowcount
                    if changed:
                        self.db.executemany("INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)", pending)
            for item in self.db.execute("SELECT * FROM comat_outbox WHERE device=? AND sent=0 AND created>?",(device["id"],now-900)).fetchall():
                current = self.db.execute("SELECT * FROM comat_devices WHERE id=?", (device["id"],)).fetchone()
                if not current:
                    break
                payload = json.loads(item["payload"])
                prefs = json.loads(current["preferences"])
                present = any(f["id"] == payload["flight_id"] for f in json.loads(current["watch"]))
                if not present or not self.enabled(prefs, now) or not prefs.get(payload["flag"], True) or current["foreground_until"] > now:
                    with self.db:
                        self.db.execute("DELETE FROM comat_outbox WHERE device=? AND event=?", (device["id"],item["event"]))
                    continue
                try:
                    accepted = bool(self.sender and await self.sender(current["token"],current["environment"],payload))
                except Exception:
                    accepted = False
                with self.db:
                    if accepted:
                        self.db.execute("UPDATE comat_outbox SET sent=1 WHERE device=? AND event=?",(device["id"],item["event"]))
                    else:
                        self.db.execute("UPDATE comat_devices SET error=? WHERE id=?",("Apple hat die Push-Meldung nicht angenommen; APNs-Konfiguration prüfen",device["id"]))
        with self.db:
            self.db.execute("DELETE FROM comat_outbox WHERE created<?",(now-2*86400,))
            self.db.execute("UPDATE comat_devices SET api_token='',watch='[]' WHERE expires<=?",(now,))

    def routes(self, app):
        for method, path, handler in (("POST","register",self.register),("POST","watch",self.watch),("GET","state",self.state),("DELETE","registration",self.revoke),("POST","test",self.test)):
            app.router.add_route(method,"/comatalarm/v1/"+path,handler)
        async def worker():
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # Retry; never log tokens or private flight snapshots.
                await asyncio.sleep(15)
        async def context(app):
            task = asyncio.create_task(worker())
            yield
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        app.cleanup_ctx.append(context)
