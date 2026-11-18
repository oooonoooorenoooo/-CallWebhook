"""ComatAlarm alert registrations and bounded background flight monitoring.

Separate credentials, tables and APNs topic; never uses CallWebhook VoIP grants.
"""
import asyncio
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .testflight import TestFlight, AppleError, failure
from .apple_access import AppleAccess, AccessDenied
from .stand_model import StandModel
from .ber_package import BERPackageImporter
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
FLAGS = ("alarmTakeoff", "alarmPrelanding", "alarmLanded", "alarmGate", "alarmParking", "alarmCancelled", "alarmDiverted")
FIELDS = ("id", "number", "reference", "created_at", "fr24ID", "origin", "destination", "atd", "ata", "aibt", "gate", "registration", "cancelled", "diverted", "packets", "stand", "repair_arrival", "track_outbound", "outbound_number")

MONITOR_LOG = logging.getLogger("ComatAlarm.monitor")
MONITOR_LOG.setLevel(logging.INFO)

BER_FLIGHTS_URL = "https://ber.berlin-airport.de/api.flights.json"


def ber_request(number, reference, departing=False):
    """Same BER request contract as the app, with an honest Pi identifier.

    Use this for both shared health checks and actual flight lookups so a
    malformed health request cannot place all BER flight queries in backoff.
    """
    day = datetime.fromtimestamp(reference, ZoneInfo("Europe/Berlin")).date()
    normalized = re.sub(r"\s", "", number).upper()
    parts = re.fullmatch(r"([A-Z0-9]{2}|[A-Z]{3})([0-9]{1,5})", normalized)
    search = " ".join(parts.groups()) if parts else normalized
    params = {"arrivalDeparture": "D" if departing else "A", "dateFrom": day.isoformat() + "T00:00:00",
              "dateUntil": (day + timedelta(days=1)).isoformat(), "search": search, "lang": "de", "page": "1", "terminal": ""}
    headers = {"Accept": "application/json", "User-Agent": "ComatAlarm/1.0 Pi",
               "Referer": "https://ber.berlin-airport.de/de/fliegen/abfluege-ankuenfte.html"}
    return params, headers



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
    if new.get("_prelanding") and not old.get("_prelanding"):
        events.append(("prelanding", "alarmPrelanding", "Landung in etwa 3 Minuten",
                       f"{number} · Landung voraussichtlich in etwa 3 Minuten."))
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
    # Fixed labels only: never log arbitrary headers, cookies or error bodies.
    edge = ""
    if name == "BER":
        headers = getattr(response, "headers", {})
        gateway = "CloudFront" if "cloudfront" in (str(headers.get("X-Cache", "")) + str(headers.get("Via", ""))).lower() else "unknown"
        edge = " gateway=" + gateway
    logging.warning("ComatAlarm provider=%s HTTP=%s response=%s pause_seconds=%s%s",
                    name, response.status, kind, max(0, int(cooldown.get(key, now)-now)), edge)
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
            point = await self.parking_position(fr24, inbound["id"], inbound["number"],
                                                inbound["aibt"], inbound["ata"], cutoff)
            if point:
                inbound["aibt_position"] = point
        return result

    @staticmethod
    async def parking_position(fr24, identifier, number, aibt, ata, cutoff):
        """Recover evidence for this exact leg, never use its current live position."""
        def select(points):
            valid = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                observed, speed = stamp(point.get("timestamp")), point.get("gspeed")
                if (observed and abs(observed-aibt) <= 60 and (ata or aibt-60) <= observed <= cutoff
                    and valid_coordinate(point.get("lat"), point.get("lon"))
                    and isinstance(speed, (int,float)) and not isinstance(speed,bool)
                    and 0 <= speed*1.852 <= 5):
                    valid.append(dict(latitude=point["lat"], longitude=point["lon"], timestamp=observed))
            return min(valid, key=lambda p: abs(p["timestamp"]-aibt)) if valid else None
        rows = await fr24("historic/flight-positions/light",
                         {"timestamp":str(int(aibt)), "flights":number, "limit":"10"})
        point = select(r for r in rows if isinstance(r, dict) and r.get("fr24_id") == identifier)
        if point:
            return point
        # A timestamp snapshot may omit the leg after tracking stops. Its complete
        # track can still contain the stationary AIBT sample; query by exact ID.
        rows = await fr24("flight-tracks", {"flight_id":identifier})
        return select(p for row in rows if isinstance(row, dict) and row.get("fr24_id") == identifier
                      for p in (row.get("tracks") or []) if isinstance(p, dict))

    async def lookup(self, flight, token, now):
        if arrival_tracking_expired(flight, now):
            return {}
        cache_key = (hashlib.sha256(token.encode()).hexdigest(), flight["number"], flight["reference"], flight.get("fr24ID"), bool(flight.get("_central")), bool(flight.get("_skip_parking_recovery")), registration_key(flight.get("registration")))
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
                    if not departure_from_ber(dict(flight, **result)) and identifier and (result.get("ata") or flight.get("ata") or flight.get("aibt")) and not flight.get("aibt_position") and not flight.get("_skip_parking_recovery"):
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
                            point = await self.parking_position(fr24, identifier, flight["number"], aibt,
                                                                result.get("ata") or flight.get("ata"), now)
                            if point:
                                result["aibt_position"] = point
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
                                result["_live"] = {k:v for k,v in live.items() if k in ("fr24_id","hex","lat","lon","alt","gspeed","track","vspeed","on_ground","timestamp","eta","sta","reg","orig_iata","dest_iata","type","source")}
                                for key, field in (("origin","orig_iata"),("destination","dest_iata"),("registration","reg")):
                                    if live.get(field): result[key] = live[field]
            except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                provider_error = "FR24: Token, Tarif oder Verbindung prüfen"
            # Prefer BER gate evidence; other schedule sources can fill gaps.
            from .flight_schedule import passenger_gate
            params, headers = ber_request(flight["number"], flight["reference"], flight.get("origin") == "BER")
            try:
                if self.cooldown.get("ber", 0) > now:
                    raise RuntimeError("BER: Abfrage pausiert")
                async with session.get(BER_FLIGHTS_URL, params=params, headers=headers, allow_redirects=False) as response:
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

            ber_params, ber_headers = ber_request("LH194", now)
            responses = await asyncio.gather(fr24(), flightstats(), flightaware(),
                get("BER", BER_FLIGHTS_URL, ber_params, ber_headers),
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
        self.poll_wakeup = asyncio.Event()
        self.shared_tokens = {}
        self.shared_authorized = set()
        self.shared_results = []
        self.last_successful_lookup = {}
        self.testflight = TestFlight(self.db)
        self.apple_access = AppleAccess(self.relay, self.testflight)
        self.stand_model = StandModel(self.db, protect_index, unprotect_index)
        self.ber_importer = BERPackageImporter(self.stand_model, self.apple_access.store)
        self.stand_access_cache = {}
        self.last_monitor_report = 0
        from .monitor import Monitor
        self.monitor = Monitor(self)
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
        from .shared_monitoring import SharedMonitoring
        self.shared_monitoring = SharedMonitoring(self)

    async def refresh_shared_subscriptions(self, now):
        """Authorization, renewal and group subscriptions run without any app."""
        candidates = self.db.execute("SELECT id,firebase_uid FROM comat_devices WHERE server_polling=1 AND firebase_uid!=''").fetchall()
        results = await asyncio.gather(*(self.stand_access(r['firebase_uid']) for r in candidates), return_exceptions=True)
        approved = {r['id']: r['firebase_uid'] for r, access in zip(candidates, results)
                    if access and not isinstance(access, BaseException)}
        # Re-read after authorization I/O: a revoked or rebound grant cannot be
        # brought back by the worker's earlier snapshot.
        devices = [r for r in self.db.execute("SELECT * FROM comat_devices WHERE server_polling=1 AND firebase_uid!=''").fetchall()
                   if approved.get(r['id']) == r['firebase_uid']]
        self.shared_authorized = {r['id'] for r in devices}
        donor = next((r['api_token'] for r in devices if r['api_token']), '')
        self.shared_tokens = {r['id']: r['api_token'] or donor for r in devices}
        with self.db:
            for r in devices:
                if r['expires'] < now+12*3600:
                    self.db.execute('UPDATE comat_devices SET expires=? WHERE id=?', (now+86400, r['id']))
        self.shared_monitoring.fanout(devices, now)

    def validated_removals(self, value, prior, now):
        """Resolve ID-only tombstones only against the authenticated device."""
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 200:
            raise ValueError('Invalid removed flights')
        result = []
        for removal in value:
            if not isinstance(removal, dict):
                raise ValueError('Invalid removed flight')
            deleted_at = removal.get('deleted_at')
            if (not isinstance(deleted_at, (int, float)) or isinstance(deleted_at, bool)
                    or not math.isfinite(deleted_at) or not 1_000_000_000 < deleted_at <= now+60):
                raise ValueError('Invalid deletion time')
            if deleted_at < now-7*86400:
                continue
            identifier = removal.get('id')
            if identifier is not None and (not isinstance(identifier, str) or not 1 <= len(identifier) <= 160):
                raise ValueError('Invalid removed flight ID')
            if 'number' in removal or 'reference' in removal:
                number, reference = removal.get('number'), removal.get('reference')
                if (not isinstance(number, str) or not re.fullmatch(r'[A-Z0-9]{2,3}[0-9]{1,5}', number)
                        or not isinstance(reference, (int, float)) or isinstance(reference, bool)
                        or not math.isfinite(reference) or reference <= 1_000_000_000):
                    raise ValueError('Invalid removed occurrence')
                if abs(reference-deleted_at) > 7*86400:
                    continue
                row = {k: removal[k] for k in ('id', 'number', 'reference', 'fr24ID', 'origin', 'destination') if k in removal}
                for key in ('fr24ID', 'origin', 'destination'):
                    if key in row and (not isinstance(row[key], str) or len(row[key]) > 64):
                        raise ValueError('Invalid removed occurrence')
            else:
                row = next((dict(f) for f in prior if identifier and f['id'].upper() == identifier.upper()), None)
                if row is None:
                    continue
            row['deleted_at'] = deleted_at
            result.append(row)
        return result

    async def stand_access(self, uid):
        if not uid:
            return None
        cached = self.stand_access_cache.get(uid)
        now = time.time()
        if cached and now-cached[0] < 60:
            return cached[1]
        try:
            access = await asyncio.wait_for(asyncio.to_thread(self.apple_access.store.device_access, uid), 3)
        except Exception as error:
            logging.warning("ComatAlarm approval_check_failed kind=%s", type(error).__name__)
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
            if access['administrator']:
                result['stand_package_import_status'] = self.ber_importer.status
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
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Invalid watch")
        arrival_sync = body.get('arrivals_sync') is True
        # Flight changes and the resumable stand-model upload share this route.
        # Coalesced handoffs must fit beside uploads without delaying a new watch;
        # paid provider polling remains separately bounded by the worker budget.
        self.relay.limit(('comat-arrivals:' if arrival_sync else 'comat-watch:') + device['id'], 30 if arrival_sync else 24, 60)
        access = await self.bind_stand_access(device, body)
        # Authorization may await Firebase while the worker stores a newer
        # milestone. Re-authenticate and merge from the current snapshot, never
        # from the row captured before that await. Revoked credentials fail here.
        device = self.device(request)
        if access and device['firebase_uid'] != access['uid']:
            raise web.HTTPForbidden(text='Firebase device identity changed during handoff')
        if arrival_sync:
            return web.json_response(self.monitor.operator.arrival_sync.read(device, body))
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
        removals = self.validated_removals(body.get('removed_flights'), list(prior.values()), now)
        clean = []
        for f in flights:
            if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not 1 <= len(f["id"]) <= 160 or not isinstance(f.get("number"), str) or not re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,5}", f["number"]):
                raise ValueError("Invalid flight")
            reference = f.get("reference")
            if not isinstance(reference, (float, int)) or isinstance(reference, bool) or not math.isfinite(reference) or abs(reference - now) > 48 * 3600:
                raise ValueError("Invalid reference")
            row = {k: f[k] for k in FIELDS if k in f}
            created_at = row.get('created_at')
            if created_at is not None and (not isinstance(created_at, (float, int)) or isinstance(created_at, bool)
                    or not math.isfinite(created_at) or not 1_000_000_000 < created_at <= now+60):
                raise ValueError('Invalid creation time')
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
            if old and self.shared_monitoring.removed(old):
                old = {}
            for key in ("atd", "ata", "aibt", "fr24ID", "origin", "destination", "registration", "stand", "aibt_position", "cancelled", "diverted", "_live", "_schedule", "_checked_at", "_repair_attempted_at", "_repair_attempts", "_aibt_recovery_at", "_aibt_recovery_attempts", "_parking_lookup_status", "_outbound", "_shared_subscription", "_landing_alert", "_prelanding"):
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
                if not isinstance(window, list) or len(window) != 2 or not all(isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in window) or not now-86400 <= window[0] < window[1] <= now+14*86400+60:
                    raise ValueError("Invalid calendar window")
            prefs["active_windows"] = windows
            valid_until = preferences.get('calendar_valid_until')
            if valid_until is not None:
                if (not isinstance(valid_until, (int, float)) or isinstance(valid_until, bool)
                        or not math.isfinite(valid_until) or not now <= valid_until <= now+14*86400+60):
                    raise ValueError('Invalid calendar validity')
                prefs['calendar_valid_until'] = valid_until
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
        push_token, push_environment = body.get('push_token'), body.get('push_environment')
        refresh_push = push_token is not None or push_environment is not None
        if refresh_push:
            if not access:
                raise web.HTTPForbidden(text='Firebase device approval required for token refresh')
            if (not isinstance(push_token, str) or not re.fullmatch(r'[0-9a-f]{32,512}', push_token)
                    or push_environment not in (('production', 'development') if self.relay.allow_development else ('production',))):
                raise ValueError('Invalid push token refresh')
            duplicate = self.db.execute('SELECT firebase_uid FROM comat_devices WHERE token=? AND environment=? AND id<>?',
                                        (push_token, push_environment, device['id'])).fetchall()
            if any(r['firebase_uid'] != access['uid'] for r in duplicate):
                raise web.HTTPConflict(text='Apple token belongs to another device identity')
        index = body.get("stand_positions")
        encrypted_index = device["stand_index"]
        if index:
            if not isinstance(index, list) or len(index) > 1000:
                raise ValueError("Invalid stand index")
            for position in index:
                if not isinstance(position, dict) or not isinstance(position.get("name"), str) or not re.fullmatch(r"[A-Z0-9-]{1,16}", position["name"]) or not valid_coordinate(position.get("latitude"), position.get("longitude")):
                    raise ValueError("Invalid stand position")
            encrypted_index = protect_index(index)
        # Apply Pi additions and removals after all authorization awaits. A stale
        # phone handoff cannot resurrect a deleted occurrence or erase a Pi watch.
        current = self.db.execute("SELECT watch FROM comat_devices WHERE id=?", (device["id"],)).fetchone()
        if current is None: raise web.HTTPUnauthorized()
        if access and server_polling:
            self.shared_monitoring.remove(removals, now)
            current = self.db.execute("SELECT watch FROM comat_devices WHERE id=?", (device["id"],)).fetchone()
            if current is None: raise web.HTTPUnauthorized()
            clean = self.shared_monitoring.retain(clean, json.loads(current[0]), now)
        clean = self.monitor.operator.merge(clean, json.loads(current[0]), now)
        if access and server_polling:
            clean = [f for f in clean if not self.shared_monitoring.removed(f)]
        clean = self.central_predictions(clean, access, now)
        self.monitor.archive(now)
        with self.db:
            if refresh_push:
                duplicates = self.db.execute('SELECT id FROM comat_devices WHERE token=? AND environment=? AND id<>?',
                                             (push_token, push_environment, device['id'])).fetchall()
                for duplicate in duplicates:
                    self.db.execute('DELETE FROM comat_outbox WHERE device=?', (duplicate['id'],))
                    self.db.execute('DELETE FROM comat_devices WHERE id=?', (duplicate['id'],))
                self.db.execute('UPDATE comat_devices SET token=?,environment=? WHERE id=?',
                                (push_token, push_environment, device['id']))
            self.db.execute("UPDATE comat_devices SET server_polling=? WHERE id=?", (int(server_polling), device["id"]))
            self.db.execute("UPDATE comat_devices SET stand_index=?,api_foreground_until=? WHERE id=?", (encrypted_index, now+90 if body["foreground"] and api_foreground else 0, device["id"]))
            self.db.execute("UPDATE comat_devices SET watch=?,preferences=?,api_token=?,foreground_until=?,expires=? WHERE id=?", (json.dumps(clean),json.dumps(prefs),api_token,now+90 if body["foreground"] else 0,now+86400,device["id"]))
            if any(not any(same_occurrence(f, p) for p in prior.values()) for f in clean):
                self.db.execute('UPDATE comat_devices SET last_poll=0 WHERE id=?', (device['id'],))
            if relay_alerts:
                for row in clean:
                    old = prior.get(row["id"]) or next((p for p in prior.values() if same_occurrence(p, row)), None)
                    # Newly subscribed historical events are a baseline, not alerts.
                    if old is not None:
                        self.db.executemany("INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)", self.events(device["id"], old, row, prefs, now))
        await self.deliver(device["id"], now)
        self.monitor.archive(now)
        self.poll_wakeup.set()
        if status_request:
            self.api_status.request_refresh(api_token)
        return web.json_response(dict(self.stand_metadata(access), shared_watch_supported=True, token_refresh_supported=True,
                                     token_refreshed=refresh_push, api_status=self.api_status.snapshot(), watching=len(clean),
                                     background_ready=bool(api_token or (self.shared_tokens.get(device['id']) if access else '')),
                                     expires=now+86400, relay_alerts=relay_alerts, flights=clean))

    async def state(self, request):
        row = self.device(request)
        access = await self.stand_access(row['firebase_uid'])
        if row['firebase_uid'] and not access:
            raise web.HTTPForbidden(text='Firebase device approval required')
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO comat_monitor_reads VALUES(?,?)",(row["id"],time.time()))
        return web.json_response({**self.stand_metadata(access), "shared_watch_supported": True, "token_refresh_supported": True, "arrivals": self.monitor.operator.arrival_sync.metadata(), "server_polling_supported": True, "api_status_supported": True, "api_status": self.api_status.snapshot(), "relay_alerts_supported": True, "flights": self.central_predictions(json.loads(row["watch"]), access, time.time()), "last_poll": row["last_poll"], "error": row["error"], "background_ready": bool(row["api_token"] or (self.shared_tokens.get(row['id']) if access else '')), "expires": row["expires"]})

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
        return (prefs.get("enabled", True) and prefs.get('calendar_valid_until', now+1) > now
                and ("active_windows" not in prefs or any(start <= now < end for start, end in prefs["active_windows"])))

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
            if owner == identity and bool(snapshot.get("_central")) == central and bool(snapshot.get("_skip_parking_recovery")) == bool(flight.get("_skip_parking_recovery")) and same_occurrence(snapshot, flight) and registration_key(snapshot.get("registration")) == registration_key(flight.get("registration")):
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
            self.monitor.cycle_started = time.time() if now is None else now
            await self._tick(time.time() if now is None else now)
            self.monitor.cycle_finished = time.time() if now is None else now

    async def _tick(self, now):
        await self.refresh_shared_subscriptions(now)
        report = now-self.last_monitor_report >= 300
        if report:
            self.last_monitor_report = now
            rows = self.db.execute("SELECT watch,expires,server_polling FROM comat_devices").fetchall()
            MONITOR_LOG.info("ComatAlarm monitor_cycle epoch=%d registrations=%d eligible=%d expired=%d central=%d",
                            int(now), len(rows), sum(r['expires'] > now for r in rows),
                            sum(r['expires'] <= now for r in rows), sum(r['expires'] > now and r['server_polling'] for r in rows))
        self.provider.credentials = json.loads(self.db.execute("SELECT credentials FROM comat_api_status WHERE id=1").fetchone()[0])
        delivered_devices = set()
        for device in self.db.execute("SELECT * FROM comat_devices WHERE expires>? AND (server_polling=1 OR foreground_until<=?)", (now,now)).fetchall():
            try:
                await self.poll_device(device, now, report)
                # Deliver an already recognized event before waiting for unrelated
                # flights/devices and their potentially slow provider requests.
                await self.deliver(device["id"], now)
                delivered_devices.add(device['id'])
            except asyncio.CancelledError:
                raise
            except Exception as exception:
                logging.warning("ComatAlarm device_poll_failed kind=%s", type(exception).__name__)
                self.monitor.record("error", "Geräteüberwachung wird erneut versucht", device=device["id"], now=now)
        for device in self.db.execute("SELECT id FROM comat_devices WHERE expires>?", (now,)).fetchall():
            if device['id'] not in delivered_devices:
                await self.deliver(device["id"], now)
        active = self.db.execute("SELECT api_token FROM comat_devices WHERE expires>? AND api_token!='' ORDER BY expires DESC LIMIT 1", (now,)).fetchone()
        if active:
            await self.api_status.refresh(active["api_token"], now=now)
        self.monitor.archive(now)
        with self.db:
            self.db.execute("DELETE FROM comat_outbox WHERE created<?",(now-2*86400,))
            self.db.execute("DELETE FROM comat_delivered WHERE sent_at<?", (now-2*86400,))
            # Bound, centrally monitored devices survive app inactivity and
            # temporary authorization outages. Revocation still stops all work.
            self.db.execute("UPDATE comat_devices SET api_token='',watch='[]' WHERE expires<=? AND NOT (server_polling=1 AND firebase_uid!='')",(now,))

    async def poll_device(self, device, now, report=False):
        from .landing_alert import update_landing_alert
        access = await self.stand_access(device['firebase_uid'])
        if device['firebase_uid'] and not access:
            with self.db:
                self.db.execute("UPDATE comat_devices SET error=? WHERE id=?", ("Pi-Abfrage pausiert: Firebase-Gerätefreigabe konnte nicht bestätigt werden", device['id']))
            if report:
                logging.warning("ComatAlarm monitor_skip reason=device_approval")
            return
        positions = self.stand_model.positions() if access and self.stand_model.ready else unprotect_index(device['stand_index'])
        prefs = json.loads(device["preferences"])
        flights = [f for f in json.loads(device["watch"]) if self.shared_monitoring.current(f, now)]
        api_token = self.shared_tokens.get(device["id"], device["api_token"])
        calendar_error = "Kalenderdaten veraltet – App zur Aktualisierung öffnen" if prefs.get("calendar_valid_until", now+1) <= now else ""
        if now-device["last_poll"] >= 60:
            error, pending = calendar_error, []
            ordered = sorted(flights, key=lambda f: self.last_successful_lookup.get(self.polling_key(f, api_token), 0))
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
                repairing = bool(device["server_polling"] and not departure_from_ber(flight) and flight.get("aibt") and not flight.get("aibt_position"))
                outbound = bool(device["server_polling"] and flight.get("track_outbound") and not flight.get("_outbound", {}).get("atd"))
                if arrival_tracking_expired(flight, now) or (not departure_from_ber(flight) and flight.get("aibt") and not repairing and not outbound) or flight.get("cancelled") or flight.get("diverted"):
                    continue
                recovery_due = repairing and now-flight["aibt"] <= 86400 and flight.get("_aibt_recovery_attempts", 0) < 6 and now-flight.get("_aibt_recovery_at", 0) >= 300
                if repairing and not recovery_due and not outbound:
                    continue
                try:
                    query = dict(flight, _skip_parking_recovery=True) if repairing and not recovery_due else flight
                    changes = await self.shared_lookup(query, api_token, now, central=bool(device["server_polling"]))
                    lookup_error = changes.pop("_error", "")
                    error = lookup_error or error
                    if lookup_error:
                        self.monitor.record("error", "Anbieterabfrage fehlgeschlagen oder pausiert", flight["number"], device["id"], now)
                    if recovery_due:
                        changes["_aibt_recovery_at"] = now
                        # Provider failures / minute budgets do not spend the
                        # finite data-recovery budget, but still back off.
                        if not lookup_error:
                            changes["_aibt_recovery_attempts"] = flight.get("_aibt_recovery_attempts", 0) + 1
                    if changes.get("aibt_position"):
                        stand = match_stand(changes["aibt_position"], positions)
                        if stand:
                            changes["stand"] = stand
                    if "_inbound" in changes:
                        point = (changes["_inbound"] or {}).get("aibt_position")
                        changes["stand"] = match_stand(point, positions) if point else None
                    updated = dict(flight, **changes)
                    if not departure_from_ber(updated) and updated.get("aibt"):
                        status = ("AIBT-Koordinate übernommen" if updated.get("aibt_position") else
                                  "AIBT bestätigt · Koordinatenabruf pausiert: " + lookup_error if lookup_error else
                                  "AIBT bestätigt · Koordinaten noch nicht übernommen; Pi prüft erneut")
                        if not updated.get("aibt_position") and updated.get("_aibt_recovery_attempts", 0) >= 6:
                            status = "AIBT bestätigt · Koordinatenabruf nach sechs Versuchen ohne Treffer"
                        changes["_parking_lookup_status"] = status
                        if status != flight.get("_parking_lookup_status"):
                            self.monitor.record("parking", status, flight["number"], device["id"], now)
                    if departure_from_ber(updated) and updated.get("atd"):
                        changes["_departure_finalized"] = previous_parking_ready(updated)
                        if not changes["_departure_finalized"]:
                            changes["_departure_repair_at"] = now
                            if not lookup_error:
                                changes["_departure_repair_attempts"] = flight.get("_departure_repair_attempts", 0)+1
                    if access and self.stand_model.ready:
                        changes['_stand_prediction'] = self.stand_model.apply(updated, now, match_stand)
                        updated.update(changes)
                    updated.update(changes)
                    updated = update_landing_alert(flight, updated, now)
                    pending.extend(self.events(device["id"], flight, updated, prefs, now))
                    flight.clear(); flight.update(updated)
                except Exception as exception:
                    logging.warning("ComatAlarm flight_poll_failed kind=%s", type(exception).__name__)
                    error = "Flugabfrage fehlgeschlagen; Token, Tarif und Verbindung prüfen"
            # Compare-and-swap the entire snapshot after all network I/O. Removed
            # flights, changed preferences and foreground takeovers win the race.
            with self.db:
                changed = self.db.execute("UPDATE comat_devices SET watch=?,last_poll=?,error=? WHERE id=? AND watch=? AND preferences=? AND (server_polling=1 OR foreground_until<=?) AND expires>?",(json.dumps(flights),now,error,device["id"],device["watch"],device["preferences"],now,now)).rowcount
                if changed:
                    self.db.executemany("INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)", pending)
            if report:
                MONITOR_LOG.info("ComatAlarm monitor_result epoch=%d flights=%d committed=%s provider_error=%s events=%d",
                                int(now), len(flights), bool(changed), bool(error), len(pending) if changed else 0)
        else:
            # Evaluate the short final-approach window on every worker pass using
            # the last fresh observation; this performs no extra provider call.
            pending = []
            for flight in flights:
                updated = update_landing_alert(flight, flight, now)
                pending.extend(self.events(device["id"], flight, updated, prefs, now))
                flight.clear(); flight.update(updated)
            encoded = json.dumps(flights)
            if encoded != device["watch"] or calendar_error:
                with self.db:
                    changed = self.db.execute("UPDATE comat_devices SET watch=?,error=? WHERE id=? AND watch=? AND preferences=? AND expires>?",
                        (encoded, calendar_error or device["error"], device["id"], device["watch"], device["preferences"], now)).rowcount
                    if changed:
                        self.db.executemany("INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)", pending)

    def events(self, device_id, old, updated, prefs, now):
        pending = []
        for event, flag, title, body in transitions(old, updated):
            observed = stamp(updated.get(event))
            MONITOR_LOG.info("ComatAlarm event_observed epoch=%d event=%s event_age_seconds=%d allowed=%s",
                             int(now), flag, max(0, int(now-observed)) if observed else -1,
                             bool(self.enabled(prefs, now) and prefs.get(flag, True)))
            allowed = self.enabled(prefs, now) and prefs.get(flag, True)
            self.monitor.record("event", flag + (" erkannt; Push vorgemerkt" if allowed else " erkannt; Alarmregel deaktiviert"), updated["number"], device_id, now)
            if allowed:
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
                data = {"title":title,"body":body,"flight_id":updated["id"],"event_id":event_id,"flag":flag,"flight_state":state}
                if flag == 'alarmPrelanding':
                    data['prelanding'] = dict(updated['_prelanding'])
                    data['expires_at'] = updated['_prelanding']['expires_at']
                payload = json.dumps(data)
                pending.append((device_id, event_id, payload, now))
        return pending

    async def deliver(self, device_id, now):
        async with self.delivery_lock:
            for item in self.db.execute("SELECT * FROM comat_outbox WHERE device=? AND sent=0 AND created>?",(device_id,now-900)).fetchall():
                current = self.db.execute("SELECT * FROM comat_devices WHERE id=?", (device_id,)).fetchone()
                if not current:
                    break
                identity = (current['credential'], current['firebase_uid'])
                if current['firebase_uid'] and not await self.stand_access(current['firebase_uid']):
                    continue
                # An app may delete a flight, revoke its grant, change alarm
                # preferences or rotate its APNs token while authorization waits.
                current = self.db.execute('SELECT * FROM comat_devices WHERE id=?', (device_id,)).fetchone()
                if not current:
                    break
                if (current['credential'], current['firebase_uid']) != identity:
                    continue
                if not self.db.execute('SELECT 1 FROM comat_outbox WHERE device=? AND event=? AND sent=0',
                                       (device_id, item['event'])).fetchone():
                    continue
                payload = json.loads(item["payload"])
                prefs = json.loads(current["preferences"])
                flight = next((f for f in json.loads(current["watch"]) if f["id"] == payload["flight_id"]), None)
                from .landing_alert import prelanding_is_current
                outdated = payload['flag'] == 'alarmPrelanding' and not prelanding_is_current(flight or {}, payload.get('prelanding'), now)
                if not flight or outdated or not self.enabled(prefs, now) or not prefs.get(payload["flag"], True) or (current["foreground_until"] > now and not prefs.get("relay_alerts", False)):
                    self.monitor.record("push_skipped", payload["flag"] + ": durch Flug-/Alarmregel verworfen", device=device_id, now=now)
                    with self.db:
                        self.db.execute("DELETE FROM comat_outbox WHERE device=? AND event=?", (device_id,item["event"]))
                    continue
                delivered = self.db.execute("SELECT 1 FROM comat_delivered WHERE token=? AND environment=? AND event=?", (current["token"], current["environment"], item["event"])).fetchone()
                try:
                    accepted = bool(delivered) or bool(self.sender and await self.sender(current["token"],current["environment"],payload))
                except Exception:
                    accepted = False
                number = next((f["number"] for f in json.loads(current["watch"]) if f["id"] == payload["flight_id"]), "")
                self.monitor.record("push_accepted" if accepted else "push_failed", payload["flag"] + (": bereits an Apple übergeben" if delivered else ": von Apple angenommen" if accepted else ": von Apple nicht angenommen; erneuter Versuch"), number, device_id, now)
                with self.db:
                    if accepted:
                        if not delivered:
                            MONITOR_LOG.info("ComatAlarm push_accepted epoch=%d event=%s delay_seconds=%d", int(now), payload['flag'], max(0,int(now-item['created'])))
                        self.db.execute("INSERT OR IGNORE INTO comat_delivered VALUES(?,?,?,?)", (current["token"], current["environment"], item["event"], now))
                        self.db.execute("UPDATE comat_outbox SET sent=1 WHERE device=? AND event=?",(device_id,item["event"]))
                    else:
                        self.db.execute("UPDATE comat_devices SET error=? WHERE id=?",("Apple hat die Push-Meldung nicht angenommen; APNs-Konfiguration prüfen",device_id))

    def routes(self, app):
        app.router.add_get("/comatalarm/v1/monitor", self.monitor.get)
        app.router.add_post("/comatalarm/v1/monitor", self.monitor.post)
        app.router.add_post("/comatalarm/v1/apple/approve", self.apple_access.approve)
        for method, path, handler in (("POST","register",self.register),("POST","watch",self.watch),("GET","state",self.state),("DELETE","registration",self.revoke),("POST","test",self.test),("GET","testflight/groups",self.test_groups),("POST","testflight/testers",self.add_tester)):
            app.router.add_route(method,"/comatalarm/v1/"+path,handler)
        async def worker():
            while True:
                self.poll_wakeup.clear()
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # No exception text/traceback: provider errors may contain secrets.
                    logging.warning("ComatAlarm monitor_cycle_failed kind=%s; retry_in_seconds=15", type(error).__name__)
                try:
                    await asyncio.wait_for(self.poll_wakeup.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
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
            logging.getLogger().addHandler(self.monitor.handler)
            self.monitor.record("service", "Push-Dienst gestartet")
            tasks = [asyncio.create_task(worker()), asyncio.create_task(tester_worker()), asyncio.create_task(self.ber_importer.run()),
                     asyncio.create_task(self.monitor.operator.arrival_sync.run())]
            yield
            if self.monitor.operator.task is not None:
                tasks.append(self.monitor.operator.task)
            if self.api_status.task is not None:
                tasks.append(self.api_status.task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logging.getLogger().removeHandler(self.monitor.handler)
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


