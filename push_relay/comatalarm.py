"""ComatAlarm alert registrations and bounded background flight monitoring.

Separate credentials, tables and APNs topic; never uses CallWebhook VoIP grants.
"""
import asyncio
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .testflight import TestFlight, AppleError, failure
from .apple_access import AppleAccess, AccessDenied
from .stand_model import StandModel
import hashlib
import hmac
import json
import logging
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
FIELDS = ("id", "number", "reference", "fr24ID", "origin", "destination", "atd", "ata", "aibt", "gate", "registration", "cancelled", "diverted", "packets", "stand", "repair_arrival", "track_outbound", "outbound_number")



def same_occurrence(a, b):
    def identifier(f):
        value = f.get("fr24ID", "").upper()
        return value if value and not value.startswith(("TEMP_", "NS_")) else None
    left, right = identifier(a), identifier(b)
    if left and right:
        return left == right
    day = lambda f: datetime.fromtimestamp(f["reference"], ZoneInfo("Europe/Berlin")).date()
    if a["number"] != b["number"] or day(a) != day(b):
        return False
    return all(not a.get(k) or not b.get(k) or a[k] == b[k] for k in ("origin", "destination"))


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


def registration_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def departure_from_ber(flight):
    return flight.get("origin") == "BER" and bool(flight.get("destination")) and flight["destination"] != "BER"


def previous_parking_ready(flight):
    previous = flight.get("_inbound") or {}
    point = previous.get("aibt_position") or {}
    return bool(previous.get("aibt") and previous.get("ata") and previous.get("id")
        and previous.get("destination") == "BER" and previous["id"] != flight.get("fr24ID")
        and registration_key(previous.get("registration"))
        and registration_key(previous.get("registration")) == registration_key(flight.get("registration"))
        and previous["ata"] <= previous["aibt"] <= (flight.get("atd") or time.time())
        and valid_coordinate(point.get("latitude"), point.get("longitude"))
        and stamp(point.get("timestamp")) and point["timestamp"] >= previous["ata"]
        and abs(point["timestamp"]-previous["aibt"]) <= 60)


def previous_ber_leg(rows, flight, now):
    """The immediately preceding movement of the assigned aircraft, never number - 1.

    Inspect all routes so an intervening departure/airborne leg cannot make an
    older BER arrival look like the current turnaround. Missing identity is not
    evidence. A date range also covers overnight stops.
    """
    registration = registration_key(flight.get("registration"))
    cutoff = min(stamp(flight.get("atd")) or now, now)
    movements = []
    for row in rows:
        if not isinstance(row, dict) or not registration or registration_key(row.get("reg") or row.get("registration")) != registration:
            continue
        identifier = row.get("fr24_id")
        if not identifier or identifier == flight.get("fr24ID"):
            continue
        takeoff = stamp(row.get("datetime_takeoff"))
        seen = stamp(row.get("first_seen"))
        started = takeoff or seen
        if not started or not cutoff-48*3600 <= started < cutoff:
            continue
        # The current departure may already have ground observations but no ATD.
        known_id = flight.get("fr24ID") or ""
        if (not known_id or known_id.startswith(("TEMP_", "NS_"))) and row.get("flight") == flight["number"] and row.get("orig_iata") == "BER" and row.get("dest_iata") == flight.get("destination") and abs(started-flight["reference"]) < 6*3600:
            continue
        movements.append((started, row))
    if not movements:
        return None
    latest = max(t for t, _ in movements)
    candidates = {r["fr24_id"]:r for t,r in movements if t == latest}
    if len(candidates) != 1:
        return None
    row = next(iter(candidates.values()))
    arrived = stamp(row.get("datetime_landed"))
    destination = row.get("dest_iata_actual") or row.get("dest_iata")
    if destination != "BER" or not row.get("flight") or not arrived or not latest <= arrived <= cutoff:
        return None
    return {"id":row["fr24_id"], "number":row["flight"], "registration":flight["registration"],
            "origin":row.get("orig_iata") or "", "destination":"BER", "ata":arrived}


def package_instructions(packets):
    result = []
    for index, value in enumerate(str(packets or "").split("/")[:2]):
        value = value.strip()
        if not re.fullmatch(r"[0-9]{1,18}", value):
            continue
        count = int(value)
        if count > 0:
            result.append(f"{count} {'Paket' if count == 1 else 'Pakete'} {'abzuholen' if index == 0 else 'abzugeben'}")
    return " · ".join(result)


def transitions(old, new):
    number = new["number"]
    events = []
    for field, flag, title, body in (
        ("atd", "alarmTakeoff", "Abgehoben", f"{number} ist abgehoben."),
        ("ata", "alarmLanded", "Gelandet", f"{number} ist gelandet."),
        ("aibt", "alarmParking", "Am Stand angekommen", f"{number} · {new.get('registration') or 'Kennzeichen unbekannt'} · Stand {new.get('stand') or 'nicht ermittelt'}" + (" · " + package_instructions(new.get("packets")) if package_instructions(new.get("packets")) else "")),
        ("cancelled", "alarmCancelled", "Flug gestrichen", f"{number} wurde gestrichen."),
        ("diverted", "alarmDiverted", "Flug umgeleitet", f"{number} wurde umgeleitet."),
    ):
        if not old.get(field) and new.get(field):
            events.append((field, flag, title, body))
    if new.get("gate") and old.get("gate") != new["gate"]:
        events.append(("gate:" + new["gate"], "alarmGate", "Gate-Update", f"{number} · Gate {new['gate']}"))
    if departure_from_ber(new) and not new.get("atd") and new.get("stand"):
        inbound, previous = new.get("_inbound") or {}, old.get("_inbound") or {}
        if inbound.get("aibt") and (inbound.get("id") != previous.get("id") or old.get("stand") != new["stand"]):
            body = f"{number} · {new.get('registration') or 'Kennzeichen unbekannt'} · Stand {new['stand']} · Vorflug {inbound['number']}"
            packages = package_instructions(new.get("packets"))
            events.append(("inbound-parking:"+inbound["id"], "alarmParking", "Standplatz für Abflug", body + (" · "+packages if packages else "")))
    return events


def retry_delay(response, now):
    from email.utils import parsedate_to_datetime
    delay = 86400 if response.status == 402 else 300
    value = getattr(response, "headers", {}).get("Retry-After", "")
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(delay, seconds)
    except (ValueError, TypeError):
        try:
            return max(delay, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            pass
    return delay


def utc_query_time(value, suffix=""):
    """Provider query timestamps: UTC seconds, no local offset or fractions.

    FR24 documents YYYY-MM-DDTHH:MM:SS; AeroAPI uses the trailing Z.
    """
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + suffix


def provider_http_status(name, response, cooldown, key, now):
    """Share provider backoff and expose safe diagnostics, never response bodies."""
    if 200 <= response.status < 300:
        return None
    if response.status in (401, 402, 403, 429):
        cooldown[key] = max(cooldown.get(key, 0), now + retry_delay(response, now))
    content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
    kind = "JSON" if "json" in content_type else "HTML" if "html" in content_type else "other"
    logging.warning("ComatAlarm provider=%s HTTP=%s response=%s pause_seconds=%s",
                    name, response.status, kind, max(0, int(cooldown.get(key, now)-now)))
    reason = {400: "Anfrageparameter abgelehnt", 401: "Zugangsdaten abgelehnt",
              402: "Tarif oder Guthaben prüfen", 403: "Anbieter verweigert Zugriff",
              429: "Abfragelimit erreicht"}.get(response.status)
    return f"HTTP {response.status}" + (f" · {reason}" if reason else "")


def arrival_tracking_expired(flight, now):
    return bool(not departure_from_ber(flight) and flight.get("ata") and not flight.get("aibt") and now - flight["ata"] >= 25 * 60)


class FlightProvider:
    def __init__(self):
        self.cooldown = {}
        self.cache = {}
        self.requests = {}
        self.credentials = {}
        self.aware_cache = {}

    async def previous_arrival(self, flight, fr24, now):
        """Pi-only lookup; persisted throttles survive client reposts and restarts."""
        old = flight.get("_inbound") or {}
        reg = registration_key(flight.get("registration"))
        same_aircraft = reg and reg == registration_key(old.get("registration"))
        if reg == flight.get("_inbound_registration") and now-flight.get("_inbound_check_at", 0) < 300:
            return {}
        if not reg:
            return {"_inbound":{}}
        cutoff = min(stamp(flight.get("atd")) or now, now)
        rows = await fr24("flight-summary/full", {"registrations":flight["registration"],
            "flight_datetime_from":utc_query_time(cutoff-48*3600),
            "flight_datetime_to":utc_query_time(cutoff), "limit":"100"})
        result = {"_inbound_check_at":now, "_inbound_registration":reg, "_inbound":{}}
        # A truncated response cannot prove which leg immediately preceded this one.
        inbound = previous_ber_leg(rows, flight, now) if len(rows) < 100 else None
        if not inbound:
            return result
        if same_aircraft and old.get("id") == inbound["id"]:
            inbound = dict(old, **inbound)
        result["_inbound"] = inbound
        if inbound.get("aibt") and inbound.get("aibt_position"):
            return result
        # Delayed historic events get two bounded recovery attempts; ongoing
        # taxi-in is checked at the same five-minute interval as the rotation.
        stale = now-inbound["ata"] > 25*60
        # After takeoff the outer poller owns a separate bounded recovery budget.
        # Old pre-departure attempts must not permanently suppress a late event.
        departed = bool(stamp(flight.get("atd")) and flight["atd"] <= now)
        if stale and ((not departed and inbound.get("recovery_attempts", 0) >= 2) or now-inbound.get("event_checked_at", 0) < 900):
            return result
        events = await fr24("historic/flight-events/light", {"flight_ids":inbound["id"], "event_types":"gate_arrival"})
        inbound["event_checked_at"] = now
        if stale:
            inbound["recovery_attempts"] = inbound.get("recovery_attempts", 0)+1
        for row in events:
            if not isinstance(row, dict) or row.get("fr24_id") != inbound["id"]:
                continue
            for event in row.get("events", []):
                if not isinstance(event, dict) or event.get("type") != "gate_arrival":
                    continue
                when = next((stamp(event.get(k)) for k in ("timestamp","time","event_time","datetime") if stamp(event.get(k))), None)
                if not when or not inbound["ata"] <= when <= cutoff:
                    continue
                inbound["aibt"] = when
                details = event.get("details") or {}
                locations = [{"lat":details.get("gate_lat"), "lon":details.get("gate_lon")}, event, event.get("position") or {}, details.get("position") or {}]
                for location in locations:
                    lat, lon = location.get("lat", location.get("latitude")), location.get("lon", location.get("longitude"))
                    if valid_coordinate(lat, lon):
                        inbound["aibt_position"] = {"latitude":lat, "longitude":lon, "timestamp":when}
                        break
        if inbound.get("aibt") and not inbound.get("aibt_position"):
            points = await fr24("historic/flight-positions/light", {"timestamp":str(int(inbound["aibt"])), "flights":inbound["number"], "limit":"10"})
            for point in points:
                if not isinstance(point, dict) or point.get("fr24_id") != inbound["id"]:
                    continue
                observed, speed = stamp(point.get("timestamp")), point.get("gspeed")
                if observed and abs(observed-inbound["aibt"]) <= 60 and inbound["ata"] <= observed <= cutoff and valid_coordinate(point.get("lat"), point.get("lon")) and isinstance(speed, (int,float)) and not isinstance(speed,bool) and 0 <= speed*1.852 <= 5:
                    inbound["aibt_position"] = {"latitude":point["lat"], "longitude":point["lon"], "timestamp":observed}
                    break
        return result

    async def lookup(self, flight, token, now):
        if arrival_tracking_expired(flight, now):
            return {}
        cache_key = (hashlib.sha256(token.encode()).hexdigest(), flight["number"], flight["reference"], flight.get("fr24ID"), bool(flight.get("_central")), registration_key(flight.get("registration")))
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
                recent = [t for t in self.requests.get(identity, []) if now-t < 60]
                self.requests[identity] = recent
                if len(recent) >= 8:
                    raise RuntimeError("FR24: Minutenschutz aktiv")
                recent.append(now)
                async with session.get("https://fr24api.flightradar24.com/api/" + path, params=params,
                        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Accept-Version": "v1"}, allow_redirects=False) as response:
                    error = provider_http_status("FR24", response, self.cooldown, identity, now)
                    if error:
                        raise RuntimeError("FR24: " + error)
                    body = await response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                        raise RuntimeError("FR24: Antwortformat ungültig")
                    return body["data"]

            if flight.get("_central") and departure_from_ber(flight) and stamp(flight.get("atd")) and flight["atd"] <= now:
                # Recover the BER turnaround, not this departure's later arrival
                # at another airport. No live/schedule/provider fan-out is needed.
                try:
                    result = await self.previous_arrival(flight, fr24, now) if token else {"_error":"FR24-Token fehlt"}
                except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                    result = {"_error":"Vorflug-Abgleich vorübergehend nicht verfügbar"}
                self.cache[cache_key] = (now, result)
                return result

            identifier = flight.get("fr24ID")
            if identifier and identifier.startswith(("TEMP_", "NS_")):
                identifier = None
            provider_error = ""
            try:
                if token:
                    if not (identifier and flight.get("ata")):
                        if identifier:
                            params = {"flight_ids": identifier}
                        else:
                            start = datetime.fromtimestamp(flight["reference"], ZoneInfo("Europe/Berlin")).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                            if start >= now:
                                raise RuntimeError("Flugtag liegt in der Zukunft")
                            params = {"flights": flight["number"], "flight_datetime_from": utc_query_time(start), "flight_datetime_to": utc_query_time(min((datetime.fromtimestamp(start, ZoneInfo("Europe/Berlin")) + timedelta(days=1)).timestamp(), now))}
                        rows = await fr24("flight-summary/full", params)
                        matches = [r for r in rows if isinstance(r, dict) and
                            (not identifier or r.get("fr24_id") == identifier) and
                            (identifier or r.get("flight") == flight["number"]) and
                            (not flight.get("destination") or r.get("dest_iata") == flight["destination"]) and
                            (not flight.get("origin") or r.get("orig_iata") == flight["origin"])]
                        if matches:
                            row = min(matches, key=lambda r: abs((stamp(r.get("datetime_takeoff")) or flight["reference"]) - flight["reference"]))
                            identifier = row.get("fr24_id")
                            result.update(registration=row.get("reg") or row.get("registration") or flight.get("registration"), fr24ID=identifier, atd=stamp(row.get("datetime_takeoff")), ata=stamp(row.get("datetime_landed")))
                            for key, field in (("origin","orig_iata"),("destination","dest_iata")):
                                if row.get(field): result[key] = row[field]
                            actual, planned = row.get("dest_iata_actual"), row.get("dest_iata")
                            if actual and planned and actual != planned:
                                result["diverted"] = True
                    if not departure_from_ber(dict(flight, **result)) and identifier and (result.get("ata") or flight.get("ata") or flight.get("aibt")) and not flight.get("aibt_position"):
                        rows = await fr24("historic/flight-events/light", {"flight_ids": identifier, "event_types": "gate_arrival"})
                        for row in rows:
                            if not isinstance(row, dict) or row.get("fr24_id") != identifier:
                                continue
                            for event in row.get("events", []):
                                if not isinstance(event, dict):
                                    continue
                                when = next((stamp(event.get(k)) for k in ("timestamp", "time", "event_time", "datetime") if stamp(event.get(k))), None)
                                if event.get("type") == "gate_arrival" and when and when <= now + 60 and when >= (result.get("ata") or flight.get("ata") or when):
                                    result["aibt"] = when
                                    details = event.get("details") or {}
                                    lat, lon = details.get("gate_lat"), details.get("gate_lon")
                                    if not valid_coordinate(lat, lon):
                                        for location in (event, event.get("position") or {}, details.get("position") or {}):
                                            lat, lon = location.get("lat", location.get("latitude")), location.get("lon", location.get("longitude"))
                                            if valid_coordinate(lat, lon):
                                                break
                                    if valid_coordinate(lat, lon):
                                        result["aibt_position"] = {"latitude":lat,"longitude":lon,"timestamp":when}
                        aibt = result.get("aibt") or flight.get("aibt")
                        if flight.get("_central") and aibt and not result.get("aibt_position"):
                            rows = await fr24("historic/flight-positions/light", {"timestamp": str(int(aibt)), "flights": flight["number"], "limit":"1"})
                            for point in rows:
                                if not isinstance(point, dict) or point.get("fr24_id") != identifier:
                                    continue
                                observed = stamp(point.get("timestamp"))
                                speed = point.get("gspeed")
                                if observed and abs(observed-aibt) <= 60 and valid_coordinate(point.get("lat"), point.get("lon")) and isinstance(speed, (int,float)) and not isinstance(speed,bool) and 0 <= speed*1.852 <= 5:
                                    result["aibt_position"] = {"latitude":point["lat"],"longitude":point["lon"],"timestamp":observed}
                    if flight.get("_central") and not departure_from_ber(flight) and flight.get("track_outbound") and flight.get("registration") and (flight.get("ata") or flight.get("aibt")):
                        airport = flight.get("destination") or "BER"
                        def matches_outbound(row):
                            return (isinstance(row, dict) and row.get("orig_iata") == airport
                                and (not flight.get("outbound_number") or row.get("flight") == flight["outbound_number"])
                                and row.get("flight") and row.get("fr24_id") != identifier)
                        outgoing = await fr24("flight-summary/full", {"registrations":flight["registration"],
                            "flight_datetime_from":utc_query_time(flight.get("aibt") or flight["ata"]),
                            "flight_datetime_to":utc_query_time(now)})
                        outgoing = [r for r in outgoing if matches_outbound(r) and stamp(r.get("datetime_takeoff")) and (flight.get("aibt") or flight["ata"]) <= stamp(r["datetime_takeoff"]) <= now]
                        if outgoing:
                            row = min(outgoing, key=lambda r: stamp(r["datetime_takeoff"]))
                            result["_outbound"] = {"number":row["flight"],"id":row.get("fr24_id"),"destination":row.get("dest_iata") or "","atd":stamp(row["datetime_takeoff"])}
                        else:
                            outgoing = await fr24("live/flight-positions/full", {"registrations":flight["registration"]})
                            row = next((r for r in outgoing if matches_outbound(r)), None)
                            if row:
                                result["_outbound"] = {"number":row["flight"],"id":row.get("fr24_id"),"destination":row.get("dest_iata") or ""}
                    if flight.get("_central") and not (result.get("aibt") or flight.get("aibt")):
                        rows = await fr24("live/flight-positions/full", {"flights": flight["number"]})
                        matches = [r for r in rows if isinstance(r, dict)
                            and (r.get("fr24_id") == identifier if identifier else r.get("flight") == flight["number"])
                            and (not flight.get("destination") or r.get("dest_iata") == flight["destination"])
                            and (not flight.get("origin") or r.get("orig_iata") == flight["origin"])
                            and stamp(r.get("timestamp")) and now-120 <= stamp(r["timestamp"]) <= now+10]
                        if matches:
                            live = max(matches, key=lambda r: stamp(r["timestamp"]))
                            lat, lon = live.get("lat"), live.get("lon")
                            if all(isinstance(v,(float,int)) and not isinstance(v,bool) and math.isfinite(v) for v in (lat,lon)) and -90<=lat<=90 and -180<=lon<=180:
                                # Only retain fields consumed by FR24Client.sample; no credentials.
                                result["_live"] = {k:v for k,v in live.items() if k in ("fr24_id","hex","lat","lon","alt","gspeed","track","vspeed","on_ground","timestamp","eta","sta","reg","orig_iata","dest_iata","type")}
                                for key, field in (("origin","orig_iata"),("destination","dest_iata"),("registration","reg")):
                                    if live.get(field): result[key] = live[field]
            except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                provider_error = "FR24: Token, Tarif oder Verbindung prüfen"
            # Prefer BER gate evidence; other schedule sources can fill gaps.
            from .flight_schedule import passenger_gate
            date = datetime.fromtimestamp(flight["reference"], ZoneInfo("Europe/Berlin")).date()
            params = {"arrivalDeparture": "D" if flight.get("origin") == "BER" else "A", "dateFrom": date.isoformat() + "T00:00:00", "dateUntil": (date + timedelta(days=1)).isoformat(), "search": flight["number"], "lang": "de", "page": "1", "terminal": ""}
            try:
                if self.cooldown.get("ber", 0) > now:
                    raise RuntimeError("BER: Abfrage pausiert")
                async with session.get("https://ber.berlin-airport.de/api.flights.json", params=params, allow_redirects=False) as response:
                    provider_http_status("BER", response, self.cooldown, "ber", now)
                    if response.status == 200:
                        body = await response.json()
                        for row in body.get("data", {}).get("items", []):
                            aliases = [row.get("flight_number", "")] + row.get("code_shares", [])
                            if flight["number"] not in [re.sub(r"\s", "", a).upper() for a in aliases if isinstance(a, str)]:
                                continue
                            if flight.get("_central"):
                                from .flight_schedule import ber_schedule
                                result["_schedule"] = ber_schedule(row, flight.get("origin") == "BER")
                            result.update(passenger_gate(row.get("gate"), "BER"))
                            status = str(row.get("status", row.get("status_text", ""))).lower()
                            if any(x in status for x in ("cancel", "annull", "gestrichen")):
                                result["cancelled"] = True
                            break
            except (RuntimeError, ValueError, TypeError, AttributeError, ClientError, OSError, asyncio.TimeoutError):
                pass
            if flight.get("_central"):
                from .flight_schedule import schedule_lookup, aware_lookup
                schedule = {}
                try:
                    schedule = await schedule_lookup(session, flight, self.credentials, self.cooldown, now)
                except (RuntimeError, ValueError, TypeError, AttributeError, ClientError, OSError, asyncio.TimeoutError):
                    pass
                # Preserve the existing low-frequency FlightAware fallback/status check.
                aware_key = (cache_key[0], flight["number"], int(flight["reference"]//86400))
                aware = self.aware_cache.get(aware_key)
                if not aware or now-aware[0] >= (1800 if schedule.get("std") else 300):
                    try:
                        values = await aware_lookup(session, flight, self.credentials, self.cooldown, now)
                        self.aware_cache[aware_key] = (now, values)
                        aware = (now, values)
                    except (RuntimeError, ValueError, TypeError, AttributeError, ClientError, OSError, asyncio.TimeoutError):
                        pass
                self.aware_cache = {k:v for k,v in self.aware_cache.items() if now-v[0] < 86400}
                if aware:
                    fallback = dict(aware[1]); fallback.update(schedule)
                    for flag in ("cancelled", "diverted"):
                        fallback[flag] = bool(schedule.get(flag) or aware[1].get(flag))
                    schedule = fallback
                # Prefer BER where available, then FlightStats, then FlightAware.
                # Reuse the existing responses: no additional gate API requests.
                schedule.update(result.get("_schedule", {}))
                result["_schedule"] = schedule
                if not result.get("gate") and schedule.get("gate"):
                    result.update(gate=schedule["gate"], gate_source=schedule["gate_source"])
                for key in ("atd", "ata", "cancelled", "diverted"):
                    if not result.get(key) and schedule.get(key):
                        result[key] = schedule[key]
                result["_checked_at"] = now
                for key in ("origin", "destination", "registration"):
                    if not result.get(key) and schedule.get(key): result[key] = schedule[key]
                if result.get("registration") and registration_key(result["registration"]) != registration_key(flight.get("registration")) and "_live" not in result:
                    result["_live"] = {}
                resolved = dict(flight, **{k:v for k,v in result.items() if v is not None})
                if departure_from_ber(resolved):
                    # Destination-arrival milestones never describe the BER stand.
                    result.pop("aibt", None); result.pop("aibt_position", None)
                    try:
                        if token: result.update(await self.previous_arrival(resolved, fr24, now))
                    except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                        provider_error = "Vorflug-Abgleich vorübergehend nicht verfügbar"
                        previous = flight.get("_inbound") or {}
                        if registration_key(previous.get("registration")) != registration_key(resolved.get("registration")):
                            result["_inbound"] = {}
        for key in ("atd", "ata", "aibt"):
            if result.get(key) is not None and not flight["reference"]-86400 <= result[key] <= now+60:
                result.pop(key)
                if key == "aibt": result.pop("aibt_position", None)
        result = {k: v for k, v in result.items() if v is not None}
        if provider_error:
            result["_error"] = provider_error
        self.cache[cache_key] = (now, result)
        return result



class SharedAPIStatus:
    """One persistent health snapshot for the shared provider accounts."""
    names = ("FR24", "FlightStats", "FlightAware", "BER", "ADSB.lol", "OpenSky")
    keys = ("FLIGHTSTATS_APP_ID", "FLIGHTSTATS_APP_KEY", "FLIGHTAWARE_API_KEY", "OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET")

    def __init__(self, db, provider):
        self.db, self.provider = db, provider
        self.lock = asyncio.Lock()
        self.task = None
        self.cooldown = provider.cooldown
        db.execute("CREATE TABLE IF NOT EXISTS comat_api_status (id INTEGER PRIMARY KEY CHECK(id=1), credentials TEXT NOT NULL, checked REAL NOT NULL, results TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO comat_api_status VALUES(1,'{}',0,'{}')")
        db.commit()

    def configure(self, values):
        if values is None:
            return
        if not isinstance(values, dict) or any(k not in self.keys or not isinstance(v, str) or len(v)>8192 or any(ord(c)<32 for c in v) for k,v in values.items()):
            raise ValueError("Invalid provider credentials")
        old = json.loads(self.db.execute("SELECT credentials FROM comat_api_status WHERE id=1").fetchone()[0])
        updated = dict(old, **{k:v for k,v in values.items() if v})
        if updated != old:
            with self.db:
                self.db.execute("UPDATE comat_api_status SET credentials=?,checked=0,results='{}' WHERE id=1", (json.dumps(updated),))

    def snapshot(self):
        row = self.db.execute("SELECT checked,results FROM comat_api_status WHERE id=1").fetchone()
        return {"checked_at": row["checked"], "results": json.loads(row["results"]), "valid_for": 1800}

    def request_refresh(self, token):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.refresh(token, manual=True))
            self.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    async def refresh(self, token, manual=False, now=None):
        now = time.time() if now is None else now
        async with self.lock:
            snapshot = self.snapshot()
            if snapshot["checked_at"] and now-snapshot["checked_at"] < (60 if manual else 900):
                return snapshot
            credentials = json.loads(self.db.execute("SELECT credentials FROM comat_api_status WHERE id=1").fetchone()[0])
            results = await self.probe(token, credentials, now)
            with self.db:
                self.db.execute("UPDATE comat_api_status SET checked=?,results=? WHERE id=1", (now,json.dumps(results)))
            return self.snapshot()

    async def probe(self, token, credentials, now):
        day = datetime.fromtimestamp(now, ZoneInfo("Europe/Berlin"))
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
            async def get(name, url, params=None, headers=None, method="GET", data=None):
                key = hashlib.sha256(token.encode()).hexdigest() if name == "FR24" else name.lower()
                if self.cooldown.get(key, 0) > now:
                    return "Abfrage pausiert · Anbieter-Sperrfrist"
                try:
                    async with session.request(method, url, params=params, headers=headers, data=data, allow_redirects=False) as response:
                        error = provider_http_status(name, response, self.cooldown, key, now)
                        if error:
                            return error
                        payload = await response.json()
                        return payload if isinstance(payload, (dict,list)) else "Ungültige Antwort"
                except Exception:
                    return "Verbindung oder Antwort fehlgeschlagen"

            async def fr24():
                if not token:
                    return "Zugang fehlt"
                identity = hashlib.sha256(token.encode()).hexdigest()
                if self.provider.cooldown.get(identity,0) > now:
                    return "Abfrage pausiert · Anbieter-Sperrfrist"
                recent = [t for t in self.provider.requests.get(identity,[]) if now-t < 60]
                self.provider.requests[identity] = recent
                if len(recent) >= 8:
                    return "Abfrage pausiert · Minutenschutz"
                recent.append(now)
                payload = await get("FR24", "https://fr24api.flightradar24.com/api/flight-summary/full",
                    {"flights":"LH194", "flight_datetime_from":utc_query_time(now-86400), "flight_datetime_to":utc_query_time(now)},
                    {"Authorization":"Bearer "+token,"Accept":"application/json","Accept-Version":"v1"})
                return payload

            async def flightstats():
                if credentials.get("FLIGHTSTATS_APP_ID") and credentials.get("FLIGHTSTATS_APP_KEY"):
                    return await get("FlightStats", f"https://api.flightstats.com/flex/flightstatus/rest/v2/json/flight/status/LH/194/dep/{day.year}/{day.month}/{day.day}",
                        {"appId":credentials["FLIGHTSTATS_APP_ID"],"appKey":credentials["FLIGHTSTATS_APP_KEY"],"utc":"false"})
                return await get("FlightStats", f"https://www.flightstats.com/v2/api-next/flight-tracker/LH/194/{day.year}/{day.month}/{day.day}",
                    headers={"Accept":"application/json","User-Agent":"ComatAlarm/1.0","Referer":"https://www.flightstats.com/"})

            async def flightaware():
                key = credentials.get("FLIGHTAWARE_API_KEY")
                if not key:
                    return "Zugang fehlt"
                return await get("FlightAware", "https://aeroapi.flightaware.com/aeroapi/flights/DLH194",
                    {"start":utc_query_time(now-86400, "Z"),"end":utc_query_time(now, "Z")}, {"x-apikey":key})

            async def opensky():
                headers = {}
                if credentials.get("OPENSKY_CLIENT_ID") and credentials.get("OPENSKY_CLIENT_SECRET"):
                    auth = await get("OpenSky", "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
                        method="POST", data={"grant_type":"client_credentials","client_id":credentials["OPENSKY_CLIENT_ID"],"client_secret":credentials["OPENSKY_CLIENT_SECRET"]})
                    if isinstance(auth,str):
                        return auth
                    if not isinstance(auth,dict) or not auth.get("access_token"):
                        return "Anmeldung fehlgeschlagen"
                    headers["Authorization"] = "Bearer "+auth["access_token"]
                return await get("OpenSky", "https://opensky-network.org/api/states/all",
                    {"lamin":"52.30","lomin":"13.38","lamax":"52.44","lomax":"13.62"}, headers)

            responses = await asyncio.gather(fr24(), flightstats(), flightaware(),
                get("BER", "https://ber.berlin-airport.de/api.flights.json", {"arrivalDeparture":"A","dateFrom":day.date().isoformat()+"T00:00:00","dateUntil":(day.date()+timedelta(days=1)).isoformat(),"search":"LH194","lang":"de","page":"1","terminal":""}),
                get("ADSB.lol", "https://api.adsb.lol/v2/point/52.3667/13.5033/10"), opensky())
            result = {}
            for name,payload in zip(self.names,responses):
                if isinstance(payload,str):
                    result[name] = payload
                elif isinstance(payload,dict) and payload.get("error"):
                    result[name] = "Anbieter meldet Fehler"
                elif not isinstance(payload,dict):
                    result[name] = "Ungültige Antwort"
                elif name == "FR24" and not isinstance(payload.get("data"),list):
                    result[name] = "Ungültige Antwort"
                elif name == "FlightAware" and not isinstance(payload.get("flights"),list):
                    result[name] = "Ungültige Antwort"
                elif name == "ADSB.lol" and not isinstance(payload.get("ac"),list):
                    result[name] = "Ungültige Antwort"
                elif name == "OpenSky" and "time" not in payload:
                    result[name] = "Ungültige Antwort"
                elif name == "BER" and not isinstance(payload.get("data"),dict):
                    result[name] = "Ungültige Antwort"
                elif not payload or (name == "FR24" and not payload["data"]) or (name == "FlightAware" and not payload["flights"]):
                    result[name] = "Erreichbar · keine Flugdaten für Prüfflug"
                else:
                    result[name] = "OK"
            return result


class ComatAlarm:
    def __init__(self, relay, sender=None, provider=None):
        self.relay, self.db, self.sender = relay, relay.db, sender
        self.provider = provider or FlightProvider()
        self.api_status = SharedAPIStatus(self.db, self.provider)
        self.poll_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.shared_results = []
        self.last_successful_lookup = {}
        self.testflight = TestFlight(self.db)
        self.apple_access = AppleAccess(self.relay, self.testflight)
        self.stand_model = StandModel(self.db, protect_index, unprotect_index)
        self.stand_access_cache = {}
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
            CREATE TABLE IF NOT EXISTS comat_delivered (
                token TEXT NOT NULL, environment TEXT NOT NULL, event TEXT NOT NULL,
                sent_at REAL NOT NULL, PRIMARY KEY(token,environment,event));
        """)

        if "api_foreground_until" not in {row[1] for row in self.db.execute("PRAGMA table_info(comat_devices)")}:
            self.db.execute("ALTER TABLE comat_devices ADD COLUMN api_foreground_until REAL NOT NULL DEFAULT 0")

        if "server_polling" not in {row[1] for row in self.db.execute("PRAGMA table_info(comat_devices)")}:
            self.db.execute("ALTER TABLE comat_devices ADD COLUMN server_polling INTEGER NOT NULL DEFAULT 0")

        if "stand_index" not in {row[1] for row in self.db.execute("PRAGMA table_info(comat_devices)")}:
            self.db.execute("ALTER TABLE comat_devices ADD COLUMN stand_index TEXT NOT NULL DEFAULT ''")

        if "firebase_uid" not in {row[1] for row in self.db.execute("PRAGMA table_info(comat_devices)")}:
            self.db.execute("ALTER TABLE comat_devices ADD COLUMN firebase_uid TEXT NOT NULL DEFAULT ''")

    async def stand_access(self, uid):
        if not uid:
            return None
        cached = self.stand_access_cache.get(uid)
        now = time.time()
        if cached and now-cached[0] < 60:
            return cached[1]
        try:
            access = await asyncio.wait_for(asyncio.to_thread(self.apple_access.store.device_access, uid), 3)
        except Exception:
            access = None  # Fail closed; never erase saved data on a network error.
        self.stand_access_cache[uid] = (now, access)
        return access

    async def bind_stand_access(self, device, body):
        uid = device['firebase_uid']
        token = body.get('firebase_token')
        if token is not None:
            if not isinstance(token, str) or not 1 <= len(token) <= 16000:
                raise ValueError('Invalid Firebase device token')
            try:
                verified = await asyncio.wait_for(asyncio.to_thread(self.apple_access.store.device_uid, token), 3)
            except Exception:
                raise web.HTTPForbidden(text='Firebase device authentication failed') from None
            if uid and uid != verified:
                raise web.HTTPForbidden(text='Relay credential belongs to another Firebase device')
            uid = verified
        access = await self.stand_access(uid)
        if (uid or token) and not access:
            raise web.HTTPForbidden(text='Firebase device approval required')
        if access and uid != device['firebase_uid']:
            with self.db:
                self.db.execute('UPDATE comat_devices SET firebase_uid=? WHERE id=?', (uid, device['id']))
        return access

    def stand_metadata(self, access):
        result = {'stand_model_supported': True, 'stand_model_authorized': bool(access)}
        if access:
            result['stand_model'] = self.stand_model.metadata()
            result['stand_model_administrator'] = access['administrator']
        return result

    def central_predictions(self, flights, access, now):
        if access and self.stand_model.ready:
            for flight in flights:
                flight['_stand_prediction'] = self.stand_model.apply(flight, now, match_stand)
        else:
            for flight in flights:
                flight.pop('_stand_prediction', None)
        return flights

    def master(self, request):
        self.device(request)
        configured = os.environ.get("COMATALARM_SETUP_KEY", "")
        supplied = request.headers.get("X-ComatAlarm-Setup-Key", "")
        if len(configured) < 32 or not hmac.compare_digest(configured, supplied):
            raise web.HTTPUnauthorized()
        self.relay.limit("comat-testflight", 6, 60)

    async def test_groups(self, request):
        self.master(request)
        try:
            return web.json_response({"tester_mode":"internal", "groups":await self.testflight.groups(), "requests":self.testflight.statuses()})
        except AppleError as error:
            return failure(str(error), 503)
        except (ClientError, asyncio.TimeoutError):
            return failure("App Store Connect ist momentan nicht erreichbar.")

    async def add_tester(self, request):
        self.master(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Ungültige Eingabe.")
            if body.get("mode") != "internal":
                raise ValueError("Bitte ComatAlarm aktualisieren: Einladungen erfolgen jetzt als interne Tester mit App-Store-Connect-Zugang.")
            result = await self.testflight.add(body.get("email"), body.get("group_id"), body.get("first_name"), body.get("last_name"))
            return web.json_response(result)
        except ValueError as error:
            return failure(str(error), 400)
        except AppleError as error:
            return failure(str(error), 503)
        except (ClientError, asyncio.TimeoutError):
            return failure("App Store Connect ist momentan nicht erreichbar.")

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
            # A phone can be provisioned once manually and again by the master.
            # Replace obsolete grants for this exact APNs destination, not peers.
            duplicates = self.db.execute("SELECT id FROM comat_devices WHERE token=? AND environment=? AND id<>?", (token, environment, identifier)).fetchall()
            for duplicate in duplicates:
                self.db.execute("DELETE FROM comat_outbox WHERE device=?", (duplicate["id"],))
                self.db.execute("DELETE FROM comat_devices WHERE id=?", (duplicate["id"],))
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
        access = await self.bind_stand_access(device, body)
        if body.get('stand_sync') is True:
            if not access or not access['administrator']:
                raise web.HTTPForbidden(text='Administrator required for model import')
            receipt = self.stand_model.upload(body.get('stand_import'), access['uid'], time.time())
            return web.json_response(dict(self.stand_metadata(access), stand_receipt=receipt))
        flights, preferences = body.get("flights"), body.get("preferences")
        relay_alerts = body.get("relay_alerts", False)
        if not isinstance(relay_alerts, bool):
            raise ValueError("Invalid alert ownership")
        if not isinstance(flights, list) or not isinstance(preferences, dict):
            raise ValueError("Invalid watch")
        status_request = body.get("api_status_request", False)
        if not isinstance(status_request, bool):
            raise ValueError("Invalid status request")
        self.api_status.configure(body.get("api_credentials"))
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
            for field in ("fr24ID", "origin", "destination", "gate", "registration", "packets", "stand", "outbound_number"):
                if field in row and (not isinstance(row[field], str) or len(row[field]) > 64):
                    raise ValueError("Invalid flight field")
            for field in ("cancelled", "diverted", "repair_arrival", "track_outbound"):
                if field in row and not isinstance(row[field], bool):
                    raise ValueError("Invalid flag")
            old = prior.get(row["id"]) or next((p for p in prior.values() if same_occurrence(p, row)), {})
            for key in ("atd", "ata", "aibt", "fr24ID", "origin", "destination", "registration", "stand", "aibt_position", "cancelled", "diverted", "_live", "_schedule", "_checked_at", "_repair_attempted_at", "_repair_attempts", "_outbound"):
                if old.get(key):
                    row[key] = old[key]
            for key in ("_inbound", "_inbound_check_at", "_inbound_registration", "_departure_finalized", "_departure_repair_at", "_departure_repair_attempts"):
                if key in old: row[key] = old[key]
            # A stale phone repost must not erase or relabel Pi-owned gate data.
            if old.get("gate_source") and old.get("gate"):
                row.update(gate=old["gate"], gate_source=old["gate_source"])
            if departure_from_ber(row) and "_inbound" in old:
                row["stand"] = old.get("stand")
            if any(entry["id"] == row["id"] for entry in clean):
                raise ValueError("Duplicate flight")
            clean.append(row)
        prefs = {key: preferences.get(key, True) for key in FLAGS + ("enabled",)}
        if not all(isinstance(v, bool) for v in prefs.values()) or not isinstance(body.get("foreground"), bool):
            raise ValueError("Invalid preferences")
        prefs["relay_alerts"] = relay_alerts
        windows = preferences.get("active_windows")
        if windows is not None:
            if not isinstance(windows, list) or len(windows) > 100:
                raise ValueError("Invalid calendar windows")
            for window in windows:
                if not isinstance(window, list) or len(window) != 2 or not all(isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in window) or not now-86400 <= window[0] < window[1] <= now+2*86400:
                    raise ValueError("Invalid calendar window")
            prefs["active_windows"] = windows
        server_polling = body.get("server_polling", False)
        if not isinstance(server_polling, bool):
            raise ValueError("Invalid polling ownership")
        api_foreground = False if server_polling else body.get("api_foreground", body["foreground"])
        if not isinstance(api_foreground, bool):
            raise ValueError("Invalid API foreground flag")
        api_token = body.get("fr24_token", device["api_token"])
        if not isinstance(api_token, str) or len(api_token) > 8192 or '\n' in api_token or '\r' in api_token:
            raise ValueError("Invalid API token")
        # Older clients send an empty token when a background keychain read fails.
        # Keep the registered credential during handoff; explicit foreground clearing
        # and registration revocation retain their existing semantics.
        if not api_token and not body["foreground"]:
            api_token = device["api_token"]
        index = body.get("stand_positions")
        encrypted_index = device["stand_index"]
        if index:
            if not isinstance(index, list) or len(index) > 1000:
                raise ValueError("Invalid stand index")
            for position in index:
                if not isinstance(position, dict) or not isinstance(position.get("name"), str) or not re.fullmatch(r"[A-Z0-9-]{1,16}", position["name"]) or not valid_coordinate(position.get("latitude"), position.get("longitude")):
                    raise ValueError("Invalid stand position")
            encrypted_index = protect_index(index)
        clean = self.central_predictions(clean, access, now)
        with self.db:
            self.db.execute("UPDATE comat_devices SET server_polling=? WHERE id=?", (int(server_polling), device["id"]))
            self.db.execute("UPDATE comat_devices SET stand_index=?,api_foreground_until=? WHERE id=?", (encrypted_index, now+90 if body["foreground"] and api_foreground else 0, device["id"]))
            self.db.execute("UPDATE comat_devices SET watch=?,preferences=?,api_token=?,foreground_until=?,expires=? WHERE id=?", (json.dumps(clean),json.dumps(prefs),api_token,now+90 if body["foreground"] else 0,now+86400,device["id"]))
            if relay_alerts:
                for row in clean:
                    old = prior.get(row["id"]) or next((p for p in prior.values() if same_occurrence(p, row)), None)
                    # Newly subscribed historical events are a baseline, not alerts.
                    if old is not None:
                        self.db.executemany("INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)", self.events(device["id"], old, row, prefs, now))
        await self.deliver(device["id"], now)
        if status_request:
            self.api_status.request_refresh(api_token)
        return web.json_response(dict(self.stand_metadata(access), api_status=self.api_status.snapshot(), watching=len(clean), background_ready=bool(api_token), expires=now+86400, relay_alerts=relay_alerts, flights=clean))

    async def state(self, request):
        row = self.device(request)
        access = await self.stand_access(row['firebase_uid'])
        if row['firebase_uid'] and not access:
            raise web.HTTPForbidden(text='Firebase device approval required')
        return web.json_response({**self.stand_metadata(access), "server_polling_supported": True, "api_status_supported": True, "api_status": self.api_status.snapshot(), "relay_alerts_supported": True, "flights": self.central_predictions(json.loads(row["watch"]), access, time.time()), "last_poll": row["last_poll"], "error": row["error"], "background_ready": bool(row["api_token"]), "expires": row["expires"]})

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

    @staticmethod
    def polling_key(flight, token):
        day = datetime.fromtimestamp(flight["reference"], ZoneInfo("Europe/Berlin")).date().isoformat()
        return (hashlib.sha256(token.encode()).hexdigest(), flight["number"], day, flight.get("origin", ""), flight.get("destination", ""))

    async def shared_lookup(self, flight, token, now, central=False):
        # Foreground leadership is separate from a phone merely showing the app.
        # Its synced event snapshot supplies background recipients without another
        # provider request for the same flight.
        for peer in ([] if central else self.db.execute("SELECT watch FROM comat_devices WHERE expires>? AND api_foreground_until>?", (now, now)).fetchall()):
            for snapshot in json.loads(peer["watch"]):
                if same_occurrence(flight, snapshot):
                    return {k: snapshot[k] for k in ("atd", "ata", "aibt", "fr24ID", "registration", "gate", "gate_source", "cancelled", "diverted") if snapshot.get(k)}
        identity = hashlib.sha256(token.encode()).hexdigest()
        self.shared_results = [entry for entry in self.shared_results if now-entry[2] < 60]
        for owner, snapshot, _, result in self.shared_results:
            if owner == identity and bool(snapshot.get("_central")) == central and same_occurrence(snapshot, flight) and registration_key(snapshot.get("registration")) == registration_key(flight.get("registration")):
                return dict(result)
        try:
            query = dict(flight, _central=True) if central else flight
            result = dict(await self.provider.lookup(query, token, now))
        except Exception:
            result = {"_error": "Flugabfrage fehlgeschlagen; Token, Tarif und Verbindung prüfen"}
        # Flights deferred by the API budget keep priority over flights already
        # queried successfully, independent of each phone's local record IDs.
        if not result.get("_error"):
            self.last_successful_lookup[self.polling_key(flight, token)] = now
        self.last_successful_lookup = {k: v for k, v in self.last_successful_lookup.items() if now-v < 2*86400}
        # Store provider IDs discovered by this request to separate real legs.
        self.shared_results.append((identity, dict(flight, _central=central, **{k: result[k] for k in ("fr24ID", "origin", "destination") if result.get(k)}), now, result))
        return dict(result)

    async def tick(self, now=None):
        async with self.poll_lock:
            await self._tick(time.time() if now is None else now)

    async def _tick(self, now):
        self.provider.credentials = json.loads(self.db.execute("SELECT credentials FROM comat_api_status WHERE id=1").fetchone()[0])
        for device in self.db.execute("SELECT * FROM comat_devices WHERE expires>? AND (server_polling=1 OR foreground_until<=?)", (now,now)).fetchall():
            access = await self.stand_access(device['firebase_uid'])
            if device['firebase_uid'] and not access:
                continue
            positions = self.stand_model.positions() if access and self.stand_model.ready else unprotect_index(device['stand_index'])
            prefs = json.loads(device["preferences"])
            flights = json.loads(device["watch"])
            if now-device["last_poll"] >= 60:
                error, pending = "", []
                ordered = sorted(flights, key=lambda f: self.last_successful_lookup.get(self.polling_key(f, device["api_token"]), 0))
                for flight in ordered:
                    departed = departure_from_ber(flight) and stamp(flight.get("atd")) and flight["atd"] <= now
                    if departed:
                        if previous_parking_ready(flight):
                            # A newly installed local index can now match saved
                            # evidence without spending another FR24 request.
                            flight["stand"] = match_stand(flight["_inbound"]["aibt_position"], positions) or flight.get("stand")
                            flight["_departure_finalized"] = True
                            continue
                        # Reopen legacy finalizations that had no predecessor
                        # evidence. At most four attempts, 15 minutes apart, 24h.
                        if now-flight["atd"] >= 86400 or flight.get("_departure_repair_attempts", 0) >= 4:
                            flight["_departure_finalized"] = True
                            continue
                        if now-flight.get("_departure_repair_at", 0) < 900:
                            continue
                    repairing = bool(device["server_polling"] and flight.get("repair_arrival") and flight.get("aibt") and not flight.get("aibt_position"))
                    outbound = bool(device["server_polling"] and flight.get("track_outbound") and not flight.get("_outbound", {}).get("atd"))
                    if arrival_tracking_expired(flight, now) or (not departure_from_ber(flight) and flight.get("aibt") and not repairing and not outbound) or flight.get("cancelled") or flight.get("diverted"):
                        continue
                    if repairing and (flight.get("_repair_attempts", 0) >= 2 or now-flight.get("_repair_attempted_at", 0) < 900):
                        continue
                    try:
                        changes = await self.shared_lookup(flight, device["api_token"], now, central=bool(device["server_polling"]))
                        lookup_error = changes.pop("_error", "")
                        error = lookup_error or error
                        if repairing:
                            changes["_repair_attempted_at"] = now
                            changes["_repair_attempts"] = flight.get("_repair_attempts", 0) + 1
                        if changes.get("aibt_position"):
                            stand = match_stand(changes["aibt_position"], positions)
                            if stand:
                                changes["stand"] = stand
                        if "_inbound" in changes:
                            point = (changes["_inbound"] or {}).get("aibt_position")
                            changes["stand"] = match_stand(point, positions) if point else None
                        updated = dict(flight, **changes)
                        if departure_from_ber(updated) and updated.get("atd"):
                            changes["_departure_finalized"] = previous_parking_ready(updated)
                            if not changes["_departure_finalized"]:
                                changes["_departure_repair_at"] = now
                                if not lookup_error:
                                    changes["_departure_repair_attempts"] = flight.get("_departure_repair_attempts", 0)+1
                        if access and self.stand_model.ready:
                            changes['_stand_prediction'] = self.stand_model.apply(updated, now, match_stand)
                            updated.update(changes)
                        pending.extend(self.events(device["id"], flight, updated, prefs, now))
                        flight.update(changes)
                    except Exception:
                        error = "Flugabfrage fehlgeschlagen; Token, Tarif und Verbindung prüfen"
                # Compare-and-swap the entire snapshot after all network I/O. Removed
                # flights, changed preferences and foreground takeovers win the race.
                with self.db:
                    changed = self.db.execute("UPDATE comat_devices SET watch=?,last_poll=?,error=? WHERE id=? AND watch=? AND preferences=? AND (server_polling=1 OR foreground_until<=?) AND expires>?",(json.dumps(flights),now,error,device["id"],device["watch"],device["preferences"],now,now)).rowcount
                    if changed:
                        self.db.executemany("INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)", pending)
        for device in self.db.execute("SELECT id FROM comat_devices WHERE expires>?", (now,)).fetchall():
            await self.deliver(device["id"], now)
        active = self.db.execute("SELECT api_token FROM comat_devices WHERE expires>? AND api_token!='' ORDER BY expires DESC LIMIT 1", (now,)).fetchone()
        if active:
            await self.api_status.refresh(active["api_token"], now=now)
        with self.db:
            self.db.execute("DELETE FROM comat_outbox WHERE created<?",(now-2*86400,))
            self.db.execute("DELETE FROM comat_delivered WHERE sent_at<?", (now-2*86400,))
            self.db.execute("UPDATE comat_devices SET api_token='',watch='[]' WHERE expires<=?",(now,))

    def events(self, device_id, old, updated, prefs, now):
        pending = []
        for event, flag, title, body in transitions(old, updated):
            if self.enabled(prefs, now) and prefs.get(flag, True):
                # Independent of a phone's local record ID or corrected FR24 ID.
                day = datetime.fromtimestamp(updated["reference"], ZoneInfo("Europe/Berlin")).date().isoformat()
                identity = updated["number"] + ":" + day + ":" + event
                event_id = hashlib.sha256(identity.encode()).hexdigest()
                state = {k:updated[k] for k in ("id","atd","ata","aibt","stand") if updated.get(k)}
                if '_stand_prediction' in updated:
                    prediction = updated['_stand_prediction'] or {}
                    state['prediction'] = prediction.get('text', '')
                    state['prediction_at'] = prediction.get('issued_at', now)
                if departure_from_ber(updated):
                    state["departure_from_ber"] = True
                    if updated.get("_inbound", {}).get("aibt"):
                        state["parking_aibt"] = updated["_inbound"]["aibt"]
                payload = json.dumps({"title":title,"body":body,"flight_id":updated["id"],"event_id":event_id,"flag":flag,"flight_state":state})
                pending.append((device_id, event_id, payload, now))
        return pending

    async def deliver(self, device_id, now):
        async with self.delivery_lock:
            for item in self.db.execute("SELECT * FROM comat_outbox WHERE device=? AND sent=0 AND created>?",(device_id,now-900)).fetchall():
                current = self.db.execute("SELECT * FROM comat_devices WHERE id=?", (device_id,)).fetchone()
                if not current:
                    break
                if current['firebase_uid'] and not await self.stand_access(current['firebase_uid']):
                    continue
                payload = json.loads(item["payload"])
                prefs = json.loads(current["preferences"])
                present = any(f["id"] == payload["flight_id"] for f in json.loads(current["watch"]))
                if not present or not self.enabled(prefs, now) or not prefs.get(payload["flag"], True) or (current["foreground_until"] > now and not prefs.get("relay_alerts", False)):
                    with self.db:
                        self.db.execute("DELETE FROM comat_outbox WHERE device=? AND event=?", (device_id,item["event"]))
                    continue
                delivered = self.db.execute("SELECT 1 FROM comat_delivered WHERE token=? AND environment=? AND event=?", (current["token"], current["environment"], item["event"])).fetchone()
                try:
                    accepted = bool(delivered) or bool(self.sender and await self.sender(current["token"],current["environment"],payload))
                except Exception:
                    accepted = False
                with self.db:
                    if accepted:
                        self.db.execute("INSERT OR IGNORE INTO comat_delivered VALUES(?,?,?,?)", (current["token"], current["environment"], item["event"], now))
                        self.db.execute("UPDATE comat_outbox SET sent=1 WHERE device=? AND event=?",(device_id,item["event"]))
                    else:
                        self.db.execute("UPDATE comat_devices SET error=? WHERE id=?",("Apple hat die Push-Meldung nicht angenommen; APNs-Konfiguration prüfen",device_id))

    def routes(self, app):
        app.router.add_post("/comatalarm/v1/apple/approve", self.apple_access.approve)
        for method, path, handler in (("POST","register",self.register),("POST","watch",self.watch),("GET","state",self.state),("DELETE","registration",self.revoke),("POST","test",self.test),("GET","testflight/groups",self.test_groups),("POST","testflight/testers",self.add_tester)):
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
        async def tester_worker():
            while True:
                try:
                    await self.testflight.reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # Do not log recipient addresses or credentials.
                await asyncio.sleep(60)
        async def context(app):
            tasks = [asyncio.create_task(worker()), asyncio.create_task(tester_worker())]
            yield
            if self.api_status.task is not None:
                tasks.append(self.api_status.task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        app.cleanup_ctx.append(context)


def valid_coordinate(lat, lon):
    return all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (lat,lon)) and 52.3 < lat < 52.45 and 13.4 < lon < 13.65


def protect_index(index):
    key = hashlib.sha256(os.environ["COMATALARM_SETUP_KEY"].encode()).digest()
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, json.dumps(index).encode(), b'comatalarm-stands-v1')).decode()


def unprotect_index(value):
    if not value:
        return []
    try:
        raw = base64.b64decode(value)
        key = hashlib.sha256(os.environ["COMATALARM_SETUP_KEY"].encode()).digest()
        return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], b'comatalarm-stands-v1'))
    except Exception:
        return []


def match_stand(point, positions):
    if not valid_coordinate(point.get('latitude'),point.get('longitude')):
        return None
    distances = {}
    for p in positions:
        a,b = math.radians(point['latitude']),math.radians(p['latitude'])
        dlon = math.radians(p['longitude']-point['longitude'])
        h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(dlon/2)**2
        distance = 6371000 * 2 * math.asin(min(1,math.sqrt(h)))
        distances[p['name']] = min(distances.get(p['name'],float('inf')),distance)
    ranked = sorted(distances,key=distances.get)
    if ranked and distances[ranked[0]] <= 160 and (len(ranked)==1 or distances[ranked[1]]-distances[ranked[0]] >= 12):
        return ranked[0]
    return None


