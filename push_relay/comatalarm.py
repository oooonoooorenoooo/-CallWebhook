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
from .live_identity import select_live_position
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
FIELDS = ("id", "number", "reference", "created_at", "fr24ID", "origin", "destination", "atd", "ata", "aibt", "gate", "registration", "cancelled", "diverted", "packets", "stand", "repair_arrival", "track_outbound", "outbound_number", "aibt_source", "aibt_position", "fr24_arrival_exhausted")
ARRIVAL_FIELDS = ('aibt_source', 'aibt_position', 'fr24_arrival_exhausted',
                  '_arrival_sources_checked_at', '_arrival_sources_status', '_arrival_position_samples',
                  'flightawareID', 'flightstatsID', 'icao24', 'callsign')

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
        raw = f.get("fr24ID")
        value = raw.upper() if isinstance(raw, str) else ''
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
    from .aibt_recovery import position as arrival_position
    number = new["number"]
    events = []
    if new.get("_prelanding") and not old.get("_prelanding"):
        events.append(("prelanding", "alarmPrelanding", "Landung in etwa 3 Minuten",
                       f"{number} · Landung voraussichtlich in etwa 3 Minuten."))
    for field, flag, title, body in (
        ("atd", "alarmTakeoff", "Abgehoben", f"{number} ist abgehoben."),
        ("ata", "alarmLanded", "Gelandet", f"{number} ist gelandet."),
        ("cancelled", "alarmCancelled", "Flug gestrichen", f"{number} wurde gestrichen."),
        ("diverted", "alarmDiverted", "Flug umgeleitet", f"{number} wurde umgeleitet."),
    ):
        if not old.get(field) and new.get(field):
            events.append((field, flag, title, body))
    def parked(flight):
        return bool(not departure_from_ber(flight) and arrival_position(flight)
                    and isinstance(flight.get('stand'), str) and flight['stand'].strip())
    if parked(new) and not parked(old):
        body = f"{number} · {new.get('registration') or 'Kennzeichen unbekannt'} · Stand {new['stand']}"
        packages = package_instructions(new.get('packets'))
        events.append(('aibt', 'alarmParking', 'Am Stand angekommen', body + (' · '+packages if packages else '')))
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


def live_refresh_due(flight, now):
    from .landing_alert import prelanding_refresh_candidate
    return bool(prelanding_refresh_candidate(flight, now)
                and now-(stamp(flight.get('_live_checked_at')) or stamp(flight.get('_checked_at')) or 0) >= 30)


class FlightProvider:
    def __init__(self):
        self.cooldown = {}
        self.cache = {}
        self.requests = {}
        self.credentials = {}
        self.aware_cache = {}
        self.arrival_exhaustions = {}
        self.arrival_tasks, self.arrival_results, self.arrival_started = {}, {}, {}
        self.arrival_wakeup = None
        from .fr24_batch import FR24BatchRequests
        self.batches = FR24BatchRequests()
        from .flight_schedule import ScheduleCache
        self.schedules = ScheduleCache(self)
        from .arrival_sources import ArrivalSources
        self.arrivals = ArrivalSources(self)
        self.background_schedules = False

    def arrival_key(self, flight, credentials=None):
        credentials = self.credentials if credentials is None else credentials
        version = hashlib.sha256(json.dumps(credentials, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return (version, flight['number'], datetime.fromtimestamp(flight['reference'], ZoneInfo('Europe/Berlin')).date().isoformat(),
                str(flight.get('fr24ID') or '').upper(), flight.get('origin'), flight.get('destination'),
                registration_key(flight.get('registration')), flight.get('_arrival_token_digest', ''),
                flight.get('_arrival_generation', 0))

    def arrival_changes(self, flight, now):
        entry = self.arrival_results.get(self.arrival_key(flight))
        if not entry or not 0 <= now-entry[0] < 24*3600 or entry[0] <= flight.get('_arrival_sources_checked_at', 0):
            return {}
        values = dict(entry[1])
        actual = stamp(values.get('aibt'))
        if ((flight.get('aibt') and actual is not None and actual != flight['aibt'])
                or (actual and stamp(flight.get('ata')) and actual < stamp(flight['ata']))):
            values.pop('aibt', None); values.pop('aibt_source', None); values.pop('aibt_position', None)
        if values.get('aibt_position'):
            from .aibt_recovery import position
            if not position(dict(flight, **values)):
                values.pop('aibt_position', None)
        return values

    def request_arrival(self, flight, now, credentials):
        """Start alternatives immediately without blocking live alarms or APNs."""
        key = self.arrival_key(flight, credentials)
        if key not in self.arrival_tasks and len(self.arrival_tasks) < 2 and now-self.arrival_started.get(key, 0) >= 15:
            snapshot, credentials = dict(flight), dict(credentials)
            self.arrival_started[key] = now
            task = asyncio.create_task(self._fetch_arrival(key, snapshot, now, credentials))
            self.arrival_tasks[key] = task
            task.add_done_callback(lambda done: self._arrival_finished(key, done))
        return self.arrival_changes(flight, now)

    async def _fetch_arrival(self, key, flight, now, credentials):
        try:
            async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                values = await self.arrivals.lookup(session, flight, now, credentials=credentials)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            logging.warning('ComatAlarm arrival_sources_failed kind=%s', type(exception).__name__)
            values = {'_arrival_sources_status': 'paused'}
        self.arrival_results[key] = (now, dict(values, _arrival_sources_checked_at=now))
        self.arrival_results = {k: value for k, value in self.arrival_results.items() if now-value[0] < 24*3600}
        self.arrival_started = {k: value for k, value in self.arrival_started.items() if now-value < 24*3600 or k in self.arrival_tasks}
        while len(self.arrival_results) > 256:
            self.arrival_results.pop(next(iter(self.arrival_results)))

    def _arrival_finished(self, key, task):
        if self.arrival_tasks.get(key) is task:
            self.arrival_tasks.pop(key, None)
        if not task.cancelled():
            task.exception()
        if self.arrival_wakeup:
            self.arrival_wakeup()

    async def close_arrivals(self):
        self.arrival_wakeup = None
        tasks = list(self.arrival_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.arrival_tasks.clear()
        await self.arrivals.close()

    def schedule_changes(self, flight, token, now):
        result = self.schedules.get(flight, token, now)
        if not result or result['_schedule_checked_at'] <= flight.get('_schedule_checked_at', 0):
            return {}
        # Supplemental observations may fill gaps; they never replace confirmed
        # runway events or an identified route with an older cached response.
        for key in ('atd', 'ata', 'aibt', 'origin', 'destination'):
            if flight.get(key):
                result.pop(key, None)
                if key == 'aibt':
                    result.pop('aibt_source', None)
        if result.get('aibt') and stamp(flight.get('ata')) and result['aibt'] < flight['ata']:
            result.pop('aibt', None); result.pop('aibt_source', None); result.pop('aibt_position', None)
        live = flight.get('_live') or {}
        if (result.get('registration') and flight.get('registration')
                and registration_key(result['registration']) != registration_key(flight['registration'])):
            observed = stamp(live.get('timestamp')) if isinstance(live, dict) else None
            if observed and now-observed < 120:
                result.pop('registration', None)
            else:
                result['_live'] = {}
                if departure_from_ber(dict(flight, **result)):
                    result.update(_inbound={}, stand=None)
        return result

    def plan_batches(self, flights, now):
        from .fr24_batch import planned_queries
        from .aibt_recovery import fr24_exhausted, valid_exhaustion, exhaustion_marker
        flights = list(flights)
        for flight, _ in flights:
            if isinstance(flight, dict) and fr24_exhausted(flight):
                marker = (flight['fr24_arrival_exhausted'] if valid_exhaustion(flight) else
                          exhaustion_marker(flight, stamp(flight.get('_arrival_event_received_at')) or now, 'legacy_no_data'))
                self.remember_arrival_exhaustion(marker)
        queries = []
        for flight, token in flights:
            try:
                if not isinstance(token, str):
                    continue
                marker = next((value for value in self.arrival_exhaustions.values() if valid_exhaustion(flight, value)), None)
                if marker:
                    flight = dict(flight, fr24_arrival_exhausted=marker)
                queries.extend((token, path, params) for path, params in planned_queries(flight, now))
            except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
                # A damaged stored candidate must not interrupt other devices.
                # Their normal per-device poll isolation remains authoritative.
                continue
        self.batches.plan(queries, now)

    def remember_arrival_exhaustion(self, marker):
        key = (marker.get('fr24_id', ''), marker['flight_number'], marker['reference_day'], marker.get('registration', ''))
        self.arrival_exhaustions[key] = marker
        if marker.get('fr24_id'):
            self.batches.exclude_arrival(marker['fr24_id'], marker['at'])

    def available_requests(self, token, now):
        identity = hashlib.sha256(token.encode()).hexdigest()
        if self.cooldown.get(identity, 0) > now:
            return 0
        return max(0, 8-sum(now-t < 60 for t in self.requests.get(identity, [])))

    async def previous_arrival(self, flight, fr24, now, session=None, credentials=None):
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
        # This is the same arrival occurrence even when displayed by its next
        # departure. A previous empty arrival reply must not be reopened here.
        stale = now-inbound["ata"] > 25*60
        from .aibt_recovery import gate_arrival, position, fr24_exhausted, valid_exhaustion, exhaustion_marker
        occurrence = dict(inbound, fr24ID=inbound['id'], reference=inbound.get('reference') or inbound['ata'])
        for field in ('_arrival_token_digest', '_arrival_generation'):
            if field in flight:
                occurrence[field] = flight[field]
        inbound['reference'] = occurrence['reference']
        marker = next((value for value in self.arrival_exhaustions.values()
                       if valid_exhaustion(occurrence, value)), None)
        if marker:
            occurrence['fr24_arrival_exhausted'] = marker
        elif stamp(inbound.get('event_checked_at')) and not position(inbound):
            occurrence['fr24_arrival_exhausted'] = exhaustion_marker(occurrence, inbound['event_checked_at'], 'legacy_no_data')
        if not fr24_exhausted(occurrence):
            try:
                events = await fr24("historic/flight-events/light", {"flight_ids":inbound["id"], "event_types":"gate_arrival"})
                inbound["event_checked_at"] = now
                if stale:
                    inbound["recovery_attempts"] = inbound.get("recovery_attempts", 0)+1
                inbound.update(gate_arrival(events, inbound['id'], inbound['ata'], cutoff))
            except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                occurrence['fr24_arrival_exhausted'] = exhaustion_marker(occurrence, now, 'error')
                result['_error'] = 'Vorflug-Ankunftsabruf nicht verfügbar; weitere Quellen übernehmen'
            occurrence.update(inbound)
            if not position(inbound) and not fr24_exhausted(occurrence):
                occurrence['fr24_arrival_exhausted'] = exhaustion_marker(occurrence, now,
                    'incomplete' if inbound.get('aibt') else 'no_data')
        if fr24_exhausted(occurrence):
            marker = occurrence['fr24_arrival_exhausted']
            inbound['fr24_arrival_exhausted'] = marker
            self.remember_arrival_exhaustion(marker)
            if session is not None:
                supplemental = (self.request_arrival(occurrence, now, self.credentials if credentials is None else credentials)
                                if self.background_schedules else
                                await self.arrivals.lookup(session, occurrence, now, credentials=credentials))
                if (supplemental.get('aibt') or occurrence.get('aibt') or now+1) <= cutoff:
                    inbound.update(supplemental)
        if not position(inbound):
            inbound.pop('aibt_position', None)
        return result

    @staticmethod
    async def parking_position(fr24, identifier, number, aibt, ata, cutoff):
        """Recover evidence for this exact leg, never use its current live position."""
        def select(points, source):
            valid = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                observed, speed = stamp(point.get("timestamp")), point.get("gspeed")
                if (observed and abs(observed-aibt) <= 60 and (ata or aibt-60) <= observed <= cutoff
                    and valid_coordinate(point.get("lat"), point.get("lon"))
                    and isinstance(speed, (int,float)) and not isinstance(speed,bool)
                    and 0 <= speed*1.852 <= 5):
                    valid.append(dict(latitude=point["lat"], longitude=point["lon"], timestamp=observed, source=source))
            return min(valid, key=lambda p: abs(p["timestamp"]-aibt)) if valid else None
        rows = await fr24("historic/flight-positions/light",
                         {"timestamp":str(int(aibt)), "flights":number, "limit":"10"})
        point = select((r for r in rows if isinstance(r, dict) and r.get("fr24_id") == identifier), 'fr24_historic')
        if point:
            return point
        # A timestamp snapshot may omit the leg after tracking stops. Its complete
        # track can still contain the stationary AIBT sample; query by exact ID.
        rows = await fr24("flight-tracks", {"flight_id":identifier})
        return select((p for row in rows if isinstance(row, dict) and row.get("fr24_id") == identifier
                      for p in (row.get("tracks") or []) if isinstance(p, dict)), 'fr24_track')

    async def lookup(self, flight, token, now, schedule_credentials=None):
        from .aibt_recovery import (EVIDENCE_REVISION, gate_arrival, position, in_window, fast_candidate,
                                    fr24_exhausted, exhaustion_marker, valid_exhaustion)
        credentials = self.credentials if schedule_credentials is None else schedule_credentials
        self.arrival_exhaustions = {key: value for key, value in self.arrival_exhaustions.items()
                                   if 0 <= now-value.get('at', 0) < 2*86400}
        if not fr24_exhausted(flight):
            marker = next((value for value in self.arrival_exhaustions.values() if valid_exhaustion(flight, value)), None)
            if marker:
                flight = dict(flight, fr24_arrival_exhausted=marker)
        initial_marker = None
        if fr24_exhausted(flight):
            initial_marker = (flight['fr24_arrival_exhausted'] if valid_exhaustion(flight) else
                              exhaustion_marker(flight, stamp(flight.get('_arrival_event_received_at')) or now, 'legacy_no_data'))
            flight = dict(flight, fr24_arrival_exhausted=initial_marker)
        credential_version = hashlib.sha256(json.dumps(credentials, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if flight.get('_arrival_fast'):
            if not flight.get('_central') or not fast_candidate(flight, now):
                return {}
            flight = dict(flight, _parking_only=True)
        if flight.get('_live_only'):
            from .landing_alert import prelanding_refresh_candidate
            if not flight.get('_central') or not prelanding_refresh_candidate(flight, now):
                return {}
        late_arrival = arrival_tracking_expired(flight, now)
        if late_arrival:
            if not flight.get('_central') or not in_window(flight, now):
                return {}
            if not (flight.get('track_outbound') and not (flight.get('_outbound') or {}).get('atd')):
                # The UI's 25-minute timeout only ends normal live tracking.
                # A delayed gate event still receives sparse, exact-leg repair.
                flight = dict(flight, _parking_only=True)
        cache_key = (hashlib.sha256(token.encode()).hexdigest(), flight["number"], flight["reference"], flight.get("fr24ID"), bool(flight.get("_central")), bool(flight.get("_skip_parking_recovery")), registration_key(flight.get("registration")), bool(flight.get('_refresh_aibt_coordinate')), bool(flight.get('_parking_only')), bool(flight.get('_arrival_fast')), bool(flight.get('_live_only')), bool(flight.get('_schedule_only')), credential_version, fr24_exhausted(flight), flight.get('_arrival_generation', 0), flight.get('_arrival_token_digest', ''))
        cached = self.cache.get(cache_key)
        if cached and now - cached[0] < (15 if flight.get('_arrival_fast') else 30 if flight.get('_live_only') else 60):
            return dict(cached[1])
        self.cache = {k: v for k, v in self.cache.items() if now - v[0] < 120}
        result = {'fr24_arrival_exhausted': initial_marker} if initial_marker else {}
        fallback_called = False
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            def exhaust_arrival(reason):
                resolved = dict(flight, **{k: v for k, v in result.items() if v is not None})
                marker = (resolved['fr24_arrival_exhausted'] if valid_exhaustion(resolved) else
                          exhaustion_marker(resolved, now, reason))
                result['fr24_arrival_exhausted'] = marker
                key = (marker['fr24_id'], marker['flight_number'], marker['reference_day'], marker['registration'])
                self.remember_arrival_exhaustion(marker)

            async def arrival_fallback():
                nonlocal fallback_called
                if flight.get('_schedule_only') or flight.get('_live_only') or not flight.get('_central'):
                    return
                resolved = dict(flight, **{k: v for k, v in result.items() if v is not None})
                if (not in_window(resolved, now) or position(resolved)
                        or flight.get('_skip_parking_recovery') or fallback_called):
                    return
                if fr24_exhausted(resolved) or not token:
                    exhaust_arrival('unavailable' if not token else 'no_data')
                    resolved.update(fr24_arrival_exhausted=result['fr24_arrival_exhausted'])
                    fallback_called = True
                    if self.background_schedules:
                        result.update(self.request_arrival(resolved, now, credentials))
                    else:
                        result.update(await self.arrivals.lookup(session, resolved, now, credentials=credentials))

            async def fetch_fr24(path, params):
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
                    if path == 'flight-tracks' and isinstance(body, list):
                        return body
                    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                        raise RuntimeError("FR24: Antwortformat ungültig")
                    return body["data"]

            async def fr24(path, params):
                return await self.batches.request(token, path, params, now, fetch_fr24)

            if not flight.get('_schedule_only') and flight.get("_central") and departure_from_ber(flight) and stamp(flight.get("atd")) and flight["atd"] <= now:
                # Recover the BER turnaround, not this departure's later arrival
                # at another airport. No live/schedule/provider fan-out is needed.
                try:
                    result = await self.previous_arrival(flight, fr24, now, session, credentials) if token else {"_error":"FR24-Token fehlt"}
                except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                    result = {"_error":"Vorflug-Abgleich vorübergehend nicht verfügbar"}
                self.cache[cache_key] = (now, result)
                return result

            identifier = flight.get("fr24ID")
            if identifier and identifier.startswith(("TEMP_", "NS_")):
                identifier = None
            provider_error = ('FR24-Token fehlt' if not flight.get('_schedule_only') and not token and flight.get('_central') and (flight.get('ata') or flight.get('aibt'))
                              and (not position(flight) or flight.get('_refresh_aibt_coordinate')) else '')
            try:
                if token and not flight.get('_schedule_only'):
                    arrival_stopped = fr24_exhausted(dict(flight, **result)) and not departure_from_ber(flight)
                    if not arrival_stopped and not flight.get('_live_only') and not (identifier and flight.get("ata")):
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
                            for key, field in (('icao24', 'hex'), ('callsign', 'callsign')):
                                if row.get(field): result[key] = row[field]
                            for key, field in (("origin","orig_iata"),("destination","dest_iata")):
                                if row.get(field): result[key] = row[field]
                            actual, planned = row.get("dest_iata_actual"), row.get("dest_iata")
                            if actual and planned and actual != planned:
                                result["diverted"] = True
                    if (not arrival_stopped and not flight.get('_live_only') and not departure_from_ber(dict(flight, **result)) and identifier
                            and (result.get("ata") or flight.get("ata") or flight.get("aibt"))
                            and (not position(flight) or flight.get('_refresh_aibt_coordinate')) and not flight.get("_skip_parking_recovery")):
                        result['_arrival_event_checked_at'] = now
                        result['_arrival_event_status'] = 'request'
                        event_params = {"flight_ids": identifier, "event_types": "gate_arrival"}
                        rows = await fr24("historic/flight-events/light", event_params)
                        result['_arrival_event_checked_at'] = self.batches.checked_at(token, 'historic/flight-events/light', event_params, now)
                        result['_arrival_event_received_at'] = result['_arrival_event_checked_at']
                        evidence = gate_arrival(rows, identifier, result.get('ata') or flight.get('ata'), now+60)
                        identity_row = next((row for row in rows if isinstance(row, dict) and str(row.get('fr24_id', '')).upper() == str(identifier).upper()), {})
                        for key, field in (('icao24', 'hex'), ('callsign', 'callsign')):
                            if identity_row.get(field): result[key] = identity_row[field]
                        result.update(evidence)
                        complete = bool(evidence.get('aibt') and position(dict(flight, **result)))
                        result['_arrival_event_status'] = 'accepted' if complete else 'incomplete' if evidence.get('aibt') else 'no_match'
                        if not complete:
                            exhaust_arrival('incomplete' if evidence.get('aibt') else 'no_data')
                        result['_aibt_evidence_revision'] = EVIDENCE_REVISION
                    if flight.get("_central") and not flight.get('_parking_only') and not flight.get('_live_only') and not departure_from_ber(flight) and flight.get("track_outbound") and flight.get("registration") and (flight.get("ata") or flight.get("aibt")):
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
                    if not arrival_stopped and flight.get("_central") and not flight.get('_parking_only') and not late_arrival and not (result.get("aibt") or flight.get("aibt") or result.get('ata') or flight.get('ata')):
                        result['_live_checked_at'] = now
                        rows = await fr24("live/flight-positions/full", {"flights": flight["number"]})
                        result['_live_checked_at'] = self.batches.checked_at(token, 'live/flight-positions/full', {'flights': flight['number']}, now)
                        live, live_status = select_live_position(rows, dict(flight, **result), now)
                        result['_live_lookup_status'] = live_status
                        if live_status == 'route_conflict':
                            result['_live'] = {}  # A new contradiction invalidates the old approach sample.
                        if live is not None:
                            # Carry the actual provider ID into the watched occurrence,
                            # including a unique live leg not yet returned by Summary.
                            result["fr24ID"] = live["fr24_id"]
                            # Only retain fields consumed by FR24Client.sample; no credentials.
                            result["_live"] = {k:v for k,v in live.items() if k in ("fr24_id","hex","lat","lon","alt","gspeed","track","vspeed","on_ground","timestamp","eta","sta","reg","orig_iata","dest_iata","type","source")}
                            for key, field in (("origin","orig_iata"),("destination","dest_iata"),("registration","reg")):
                                if live.get(field): result[key] = live[field]
            except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError) as exception:
                # Our HTTP/shape errors are locally authored; provider bodies
                # never enter diagnostics. A decoder failure is not a tariff claim.
                if isinstance(exception, RuntimeError) and str(exception).startswith('FR24: '):
                    provider_error = str(exception)
                elif isinstance(exception, (ValueError, TypeError)):
                    provider_error = 'FR24: Antwort konnte nicht verarbeitet werden'
                else:
                    provider_error = 'FR24: Abfrage vorübergehend nicht verfügbar'
                if result.get('_arrival_event_status') == 'request':
                    result['_arrival_event_status'] = ('paused' if provider_error in
                        ('FR24: Minutenschutz aktiv', 'FR24: Abfrage pausiert (Tarif, Token oder Limit prüfen)') else 'error')
            resolved_arrival = dict(flight, **{k: v for k, v in result.items() if v is not None})
            if (not flight.get('_schedule_only') and not flight.get('_live_only')
                    and in_window(resolved_arrival, now) and not position(resolved_arrival)
                    and not flight.get('_skip_parking_recovery') and not fr24_exhausted(resolved_arrival)):
                exhaust_arrival('error' if provider_error else 'no_data')
            await arrival_fallback()
            if flight.get('_parking_only') or flight.get('_live_only'):
                result = {k: v for k, v in result.items() if v is not None}
                if provider_error:
                    result['_error'] = provider_error
                self.cache[cache_key] = (now, result)
                return result
            if self.background_schedules and not flight.get('_schedule_only'):
                resolved = dict(flight, **{k: v for k, v in result.items() if v is not None})
                self.schedules.request(resolved, token, now)
                supplemental = self.schedule_changes(resolved, token, now)
                result = dict(supplemental, **result, _checked_at=now)
                if departure_from_ber(dict(flight, **result)):
                    result.pop('aibt', None); result.pop('aibt_position', None)
                    try:
                        if token:
                            result.update(await self.previous_arrival(dict(flight, **result), fr24, now, session, credentials))
                    except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                        provider_error = 'Vorflug-Abgleich vorübergehend nicht verfügbar'
                result = {k: v for k, v in result.items() if v is not None}
                if provider_error:
                    result['_error'] = provider_error
                self.cache[cache_key] = (now, result)
                return result
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
                    schedule = await schedule_lookup(session, flight, credentials, self.cooldown, now)
                except (RuntimeError, ValueError, TypeError, AttributeError, ClientError, OSError, asyncio.TimeoutError):
                    pass
                # Preserve the existing low-frequency FlightAware fallback/status check.
                aware_key = (cache_key[0], credential_version, flight["number"], int(flight["reference"]//86400))
                aware = self.aware_cache.get(aware_key)
                if not aware or now-aware[0] >= (1800 if schedule.get("std") else 300):
                    try:
                        values = await aware_lookup(session, flight, credentials, self.cooldown, now)
                        self.aware_cache[aware_key] = (now, values)
                        aware = (now, values)
                    except (RuntimeError, ValueError, TypeError, AttributeError, ClientError, OSError, asyncio.TimeoutError):
                        pass
                self.aware_cache = {k:v for k,v in self.aware_cache.items() if now-v[0] < 86400}
                if aware:
                    fallback = dict(aware[1]); fallback.update(schedule)
                    if aware[1].get('aibt'):
                        fallback.update(aibt=aware[1]['aibt'], aibt_source=aware[1].get('aibt_source', 'FlightAware'))
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
                if not (flight.get('aibt') or result.get('aibt')) and schedule.get('aibt'):
                    result['aibt'] = schedule['aibt']
                    result['aibt_source'] = schedule.get('aibt_source', '')
                result["_checked_at"] = now
                for key in ("origin", "destination", "registration", 'flightawareID', 'flightstatsID', 'icao24', 'callsign'):
                    if not result.get(key) and schedule.get(key): result[key] = schedule[key]
                if result.get("registration") and registration_key(result["registration"]) != registration_key(flight.get("registration")) and "_live" not in result:
                    result["_live"] = {}
                resolved = dict(flight, **{k:v for k,v in result.items() if v is not None})
                if departure_from_ber(resolved):
                    # Destination-arrival milestones never describe the BER stand.
                    result.pop("aibt", None); result.pop("aibt_position", None)
                    try:
                        if token and not flight.get('_schedule_only'): result.update(await self.previous_arrival(resolved, fr24, now, session, credentials))
                    except (RuntimeError, ValueError, TypeError, ClientError, OSError, asyncio.TimeoutError):
                        provider_error = "Vorflug-Abgleich vorübergehend nicht verfügbar"
                        previous = flight.get("_inbound") or {}
                        if registration_key(previous.get("registration")) != registration_key(resolved.get("registration")):
                            result["_inbound"] = {}
                elif (in_window(resolved, now) and not position(resolved) and not flight.get('_schedule_only')
                      and not flight.get('_skip_parking_recovery')):
                    # A supplemental actual may be the first evidence of arrival.
                    # Reuse it immediately; do not wait for another FR24 cycle.
                    exhaust_arrival('no_data')
                    await arrival_fallback()
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

    def request_refresh(self, token, manual=True, now=None):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.refresh(token, manual=manual, now=now))
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
        from .history_sync import HistorySync
        self.history_sync = HistorySync(self)

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
        # Preserve approved historical snapshots before the 48h active-watch
        # filter or a first post-restart handoff can remove them.
        for r in devices:
            try:
                saved = json.loads(r['watch'])
                if isinstance(saved, list):
                    self.history_sync.capture(saved, now)
            except (TypeError, ValueError):
                continue
        self.history_sync.backfill(now)
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
            identifier = removal.get('id')
            if identifier is not None and (not isinstance(identifier, str) or not 1 <= len(identifier) <= 160):
                raise ValueError('Invalid removed flight ID')
            if 'number' in removal or 'reference' in removal:
                number, reference = removal.get('number'), removal.get('reference')
                if (not isinstance(number, str) or not re.fullmatch(r'[A-Z0-9]{2,3}[0-9]{1,5}', number)
                        or not isinstance(reference, (int, float)) or isinstance(reference, bool)
                        or not math.isfinite(reference) or reference <= 1_000_000_000):
                    raise ValueError('Invalid removed occurrence')
                if reference > now+7*86400:
                    continue
                from .history_sync import day_of
                row = {k: removal[k] for k in ('id', 'number', 'reference', 'fr24ID', 'origin', 'destination') if k in removal}
                for key in ('fr24ID', 'origin', 'destination'):
                    if key in row and (not isinstance(row[key], str) or len(row[key]) > 64):
                        raise ValueError('Invalid removed occurrence')
                if (abs(reference-deleted_at) > 7*86400 and day_of(reference)[:7] != day_of(now)[:7]
                        and not self.history_sync.retained_occurrence(row, now)):
                    continue
            else:
                row = next((dict(f) for f in prior if identifier and f['id'].upper() == identifier.upper()), None)
                if row is None:
                    row = self.history_sync.resolve(identifier, now)
                if row is None:
                    continue
            if deleted_at < now-7*86400:
                from .history_sync import day_of
                if day_of(row['reference'])[:7] != day_of(now)[:7] and not self.history_sync.retained_occurrence(row, now):
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
        history_sync = body.get('history_sync') is True
        # Flight changes and the resumable stand-model upload share this route.
        # Coalesced handoffs must fit beside uploads without delaying a new watch;
        # paid provider polling remains separately bounded by the worker budget.
        self.relay.limit(('comat-history:' if history_sync else 'comat-arrivals:' if arrival_sync else 'comat-watch:') + device['id'], 60 if history_sync else 30 if arrival_sync else 24, 60)
        access = await self.bind_stand_access(device, body)
        # Authorization may await Firebase while the worker stores a newer
        # milestone. Re-authenticate and merge from the current snapshot, never
        # from the row captured before that await. Revoked credentials fail here.
        device = self.device(request)
        if access and device['firebase_uid'] != access['uid']:
            raise web.HTTPForbidden(text='Firebase device identity changed during handoff')
        if history_sync:
            if not access:
                raise web.HTTPForbidden(text='Firebase device approval required for history')
            now = time.time()
            if device['server_polling']:
                self.history_sync.capture(json.loads(device['watch']), now)
            self.history_sync.backfill(now)
            return web.json_response(self.history_sync.read(body, now), headers={'Cache-Control': 'no-store'})
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
        if access and device['server_polling']:
            self.history_sync.capture(list(prior.values()), now)
        self.history_sync.backfill(now)
        removals = self.validated_removals(body.get('removed_flights'), list(prior.values()), now)
        clean, recovery_rows = [], []
        for f in flights:
            recovery_only = isinstance(f, dict) and f.get('recovery_only') is True
            if isinstance(f, dict) and 'recovery_only' in f and not isinstance(f['recovery_only'], bool):
                raise ValueError('Invalid recovery ownership')
            if recovery_only and (f.get('repair_arrival') is not True or not access or body.get('server_polling') is not True):
                raise web.HTTPForbidden(text='Approved central arrival recovery required')
            if not recovery_only and isinstance(f, dict) and (f.get('archived') is True or str(f.get('id', '')).startswith('PI_HISTORY_')):
                continue  # Imported history never becomes a provider subscription.
            if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not 1 <= len(f["id"]) <= 160 or not isinstance(f.get("number"), str) or not re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,5}", f["number"]):
                raise ValueError("Invalid flight")
            reference = f.get("reference")
            if (not isinstance(reference, (float, int)) or isinstance(reference, bool) or not math.isfinite(reference)
                    or (not recovery_only and abs(reference-now) > 48*3600)
                    or (recovery_only and not 1_000_000_000 < reference <= now+48*3600)):
                raise ValueError("Invalid reference")
            row = {k: f[k] for k in FIELDS if k in f}
            if recovery_only and row.get('created_at') == 0:
                row.pop('created_at')  # History uses zero for an unknown creation date.
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
            from .aibt_recovery import valid_exhaustion, position as arrival_position
            if 'aibt_source' in row and row['aibt_source'] not in ('FR24', 'FlightAware', 'FlightStats'):
                raise ValueError('Invalid arrival source')
            if 'aibt_position' in row and not arrival_position(row):
                raise ValueError('Invalid arrival coordinate')
            if 'fr24_arrival_exhausted' in row:
                marker = row['fr24_arrival_exhausted']
                if not valid_exhaustion(row) or stamp(marker.get('at')) > now+60:
                    raise ValueError('Invalid arrival provider handoff')
                row['fr24_arrival_exhausted'] = {k: marker[k] for k in
                    ('fr24_id', 'flight_number', 'reference_day', 'registration', 'at', 'reason') if k in marker}
            old = prior.get(row["id"]) or next((p for p in prior.values() if same_occurrence(p, row)), {})
            if recovery_only and old and not same_occurrence(old, row):
                old = {}
            if old and self.shared_monitoring.removed(old):
                old = {}
            for key in ("atd", "ata", "aibt", "fr24ID", "origin", "destination", "registration", "stand", "aibt_position", "cancelled", "diverted", "_live", "_schedule", "_checked_at", "_schedule_checked_at", "_live_checked_at", "_live_lookup_status", "_repair_attempted_at", "_repair_attempts", "_aibt_recovery_at", "_aibt_recovery_attempts", "_aibt_recovery_retry_at", "_aibt_evidence_revision", "_arrival_event_checked_at", "_arrival_event_received_at", "_arrival_event_status", "_arrival_event_logged_at", "_parking_lookup_status", "_parking_recovery", "_outbound", "_shared_subscription", "_landing_alert", "_prelanding", "_prelanding_report"):
                if old.get(key):
                    row[key] = old[key]
            for key in ARRIVAL_FIELDS:
                if old.get(key):
                    row[key] = old[key]
            if old.get('_arrival_recovery_only'):
                row['_arrival_recovery_only'] = True
            if row.get('fr24_arrival_exhausted') and not valid_exhaustion(row):
                row.pop('fr24_arrival_exhausted')
            for key in ("_inbound", "_inbound_check_at", "_inbound_registration", "_departure_finalized", "_departure_repair_at", "_departure_repair_attempts"):
                if key in old: row[key] = old[key]
            # A stale phone repost must not erase or relabel Pi-owned gate data.
            if old.get("gate_source") and old.get("gate"):
                row.update(gate=old["gate"], gate_source=old["gate_source"])
            if departure_from_ber(row) and "_inbound" in old:
                row["stand"] = old.get("stand")
            if recovery_only:
                from .aibt_recovery import in_window
                # A capability-gated request for a recent local history record
                # is only evidence for the bounded archive repair queue. It
                # cannot subscribe an archived occurrence or replay its alerts.
                archived = self.history_sync.resolve(row['id'], now)
                if archived is None and row['id'].startswith('PI_HISTORY_'):
                    matches = [json.loads(r['payload']) for r in self.db.execute('SELECT payload FROM comat_app_history')
                               if same_occurrence(json.loads(r['payload']), row)]
                    if len(matches) != 1:
                        continue  # Old client-generated aliases must resolve to trusted server history.
                    archived = matches[0]
                if archived and not same_occurrence(archived, row):
                    raise ValueError('Arrival recovery identity mismatch')
                if (stamp(row.get('ata')) and in_window(row, now)
                        and not arrival_position(row) and not departure_from_ber(row)):
                    if archived:
                        row['created_at'] = archived['created_at']
                        if row['id'] == archived['id']:
                            row.pop('id')
                    recovery_rows.append((row, archived.get('_created_at_proven') if archived else None))
                continue
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
        if access and server_polling:
            self.history_sync.capture(clean, now)
            for row, created_known in recovery_rows:
                self.history_sync.capture([row], now, created_known=created_known, baseline=True)
        await self.deliver(device["id"], now)
        self.monitor.archive(now)
        self.poll_wakeup.set()
        if status_request:
            self.api_status.request_refresh(api_token)
        current = self.device(request)
        if current['firebase_uid'] != device['firebase_uid'] or (access and current['firebase_uid'] != access['uid']):
            raise web.HTTPForbidden(text='Firebase device identity changed during handoff')
        return web.json_response(dict(self.stand_metadata(access), shared_watch_supported=True, token_refresh_supported=True,
                                     arrival_recovery_supported=bool(access),
                                     history_sync_supported=bool(access), history=self.history_sync.metadata(now) if access else None,
                                     token_refreshed=refresh_push, api_status=self.api_status.snapshot(), watching=len(clean),
                                     background_ready=bool(api_token or (self.shared_tokens.get(device['id']) if access else '')),
                                     expires=now+86400, relay_alerts=relay_alerts, flights=clean))

    async def state(self, request):
        row = self.device(request)
        access = await self.stand_access(row['firebase_uid'])
        if row['firebase_uid'] and not access:
            raise web.HTTPForbidden(text='Firebase device approval required')
        current = self.device(request)
        if current['firebase_uid'] != row['firebase_uid'] or (access and current['firebase_uid'] != access['uid']):
            raise web.HTTPForbidden(text='Firebase device identity changed during state read')
        row = current
        now = time.time()
        if access:
            if row['server_polling']:
                self.history_sync.capture(json.loads(row['watch']), now)
            self.history_sync.backfill(now)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO comat_monitor_reads VALUES(?,?)",(row["id"],now))
        return web.json_response({**self.stand_metadata(access), "shared_watch_supported": True, "token_refresh_supported": True,
            "arrival_recovery_supported": bool(access),
            "history_sync_supported": bool(access), "history": self.history_sync.metadata(now) if access else None,
            "arrivals": self.monitor.operator.arrival_sync.metadata(), "server_polling_supported": True, "api_status_supported": True, "api_status": self.api_status.snapshot(), "relay_alerts_supported": True, "flights": self.central_predictions(json.loads(row["watch"]), access, now), "last_poll": row["last_poll"], "error": row["error"], "background_ready": bool(row["api_token"] or (self.shared_tokens.get(row['id']) if access else '')), "expires": row["expires"]}, headers={'Cache-Control': 'no-store'})

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

    def arrival_context(self, flight, token):
        """A late async result cannot cross token rotation or delete/re-add."""
        generation = 0
        for row in self.db.execute('SELECT payload,deleted_at FROM comat_shared_removed'):
            try:
                if same_occurrence(flight, json.loads(row['payload'])):
                    generation = max(generation, row['deleted_at'])
            except (KeyError, TypeError, ValueError):
                continue
        return dict(flight, _arrival_token_digest=hashlib.sha256(token.encode()).hexdigest(),
                    _arrival_generation=generation)

    async def shared_lookup(self, flight, token, now, central=False):
        flight = self.arrival_context(flight, token)
        # Foreground leadership is separate from a phone merely showing the app.
        # Its synced event snapshot supplies background recipients without another
        # provider request for the same flight.
        for peer in ([] if central else self.db.execute("SELECT watch FROM comat_devices WHERE expires>? AND api_foreground_until>?", (now, now)).fetchall()):
            for snapshot in json.loads(peer["watch"]):
                if same_occurrence(flight, snapshot):
                    return {k: snapshot[k] for k in ("atd", "ata", "aibt", "fr24ID", "registration", "gate", "gate_source", "cancelled", "diverted") + ARRIVAL_FIELDS if snapshot.get(k)}
        identity = hashlib.sha256(token.encode()).hexdigest()
        configured = getattr(self.provider, 'credentials', {})
        version = hashlib.sha256(json.dumps(configured if isinstance(configured, dict) else {},
                                           sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.shared_results = [entry for entry in self.shared_results if now-entry[2] < 60]
        ttl = 15 if flight.get('_arrival_fast') else 30 if flight.get('_live_only') else 60
        for owner, snapshot, checked, result in self.shared_results:
            from .aibt_recovery import fr24_exhausted
            if (owner == identity and snapshot.get('_credential_version') == version and now-checked < ttl and bool(snapshot.get("_central")) == central
                    and fr24_exhausted(snapshot) == fr24_exhausted(flight)
                    and snapshot.get('_arrival_generation', 0) == flight.get('_arrival_generation', 0)
                    and all(bool(snapshot.get(k)) == bool(flight.get(k)) for k in
                        ('_skip_parking_recovery', '_refresh_aibt_coordinate', '_parking_only', '_arrival_fast', '_live_only'))
                    and same_occurrence(snapshot, flight) and registration_key(snapshot.get("registration")) == registration_key(flight.get("registration"))):
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
        self.shared_results.append((identity, dict(flight, _central=central, _credential_version=version,
            **{k: result[k] for k in ("fr24ID", "origin", "destination", "fr24_arrival_exhausted") if result.get(k)}), now, result))
        return dict(result)

    async def apply_ready_schedules(self, device, now):
        """The existing worker alone commits completed private cache results."""
        from .landing_alert import update_landing_alert
        from .prelanding_diagnostics import receipt as landing_receipt
        if device['id'] not in self.shared_authorized:
            return
        access = await self.stand_access(device['firebase_uid'])
        current = self.db.execute('SELECT * FROM comat_devices WHERE id=?', (device['id'],)).fetchone()
        if (not access or current is None or access['uid'] != device['firebase_uid']
                or any(current[k] != device[k] for k in ('credential', 'firebase_uid', 'api_token'))
                or not current['server_polling'] or current['expires'] <= now):
            return
        device = current
        from . import aibt_recovery
        positions = self.stand_model.positions() if self.stand_model.ready else unprotect_index(device['stand_index'])
        flights, prefs = json.loads(device['watch']), json.loads(device['preferences'])
        token = self.shared_tokens.get(device['id'], device['api_token'])
        eligible = []
        for flight in flights:
            if not isinstance(flight, dict) or not self.shared_monitoring.current(flight, now):
                continue
            if flight.get('cancelled') or flight.get('diverted'):
                continue
            if departure_from_ber(flight) and flight.get('atd') and not flight.get('_inbound'):
                continue
            if (not departure_from_ber(flight) and (flight.get('ata') or flight.get('aibt'))
                    and not aibt_recovery.in_window(flight, now) and not flight.get('track_outbound')):
                continue
            from .aibt_recovery import position as arrival_position
            if (not departure_from_ber(flight) and arrival_position(flight)
                    and not (flight.get('track_outbound') and not (flight.get('_outbound') or {}).get('atd'))):
                continue
            eligible.append(flight)
        pending, landing_receipts = [], []
        for flight in sorted(eligible, key=lambda f: self.provider.schedules.last_started(dict(f, _central=True), token)):
            # New unknown routes first get their normal FR24 resolution. Later
            # cache refreshes are offered oldest-first, so a2-task limit cannot
            # repeatedly select the same first two flights and starve the rest.
            query = self.arrival_context(dict(flight, _central=True), token)
            if (not aibt_recovery.fr24_exhausted(flight)
                    and (stamp(flight.get('_checked_at')) or self.provider.schedules.last_started(query, token))):
                self.provider.schedules.request(query, token, now)
            changes = self.provider.schedule_changes(query, token, now)
            changes.update(self.provider.arrival_changes(query, now))
            previous = flight.get('_inbound') or {}
            if departure_from_ber(flight) and previous.get('id') and previous.get('ata'):
                inbound_query = self.arrival_context(dict(previous, fr24ID=previous['id'], reference=previous.get('reference') or previous['ata']), token)
                inbound_changes = self.provider.arrival_changes(inbound_query, now)
                if inbound_changes:
                    inbound = dict(previous, **inbound_changes)
                    if (inbound.get('aibt') or now+1) <= min(stamp(flight.get('atd')) or now, now):
                        changes['_inbound'] = inbound
                        if aibt_recovery.position(inbound):
                            changes['stand'] = match_stand(inbound['aibt_position'], positions)
                            changes['_departure_finalized'] = bool(flight.get('atd'))
            if not changes:
                continue
            updated = dict(flight, **changes)
            if updated.get('aibt_position') and not aibt_recovery.position(updated):
                updated.update(aibt_position=None, stand=None)
            if not departure_from_ber(updated) and (updated.get('ata') or updated.get('aibt')):
                updated.update(aibt_recovery.describe(updated, positions, now, ''))
            updated = update_landing_alert(flight, updated, now)
            message = landing_receipt(flight, updated, prefs, now)
            if message:
                landing_receipts.append((message, flight['number']))
            pending.extend(self.events(device['id'], flight, updated, prefs, now))
            flight.clear(); flight.update(updated)
        encoded = json.dumps(flights)
        if encoded == device['watch']:
            return
        with self.db:
            changed = self.db.execute('''UPDATE comat_devices SET watch=?
                WHERE id=? AND watch=? AND preferences=? AND credential=? AND firebase_uid=?
                AND api_token=? AND server_polling=1 AND expires>?''',
                (encoded, device['id'], device['watch'], device['preferences'], device['credential'],
                 device['firebase_uid'], device['api_token'], now)).rowcount
            if changed:
                self.db.executemany('INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)', pending)
                self.history_sync.capture(flights, now)
        if changed:
            for message, number in landing_receipts:
                self.monitor.record('prelanding', message, number, device['id'], now)
            await self.deliver(device['id'], now)

    async def tick(self, now=None):
        async with self.poll_lock:
            self.monitor.cycle_started = time.time() if now is None else now
            await self._tick(time.time() if now is None else now)
            self.monitor.cycle_finished = time.time() if now is None else now

    async def _tick(self, now):
        from .aibt_recovery import fast_due
        await self.refresh_shared_subscriptions(now)
        report = now-self.last_monitor_report >= 300
        if report:
            self.last_monitor_report = now
            rows = self.db.execute("SELECT watch,expires,server_polling FROM comat_devices").fetchall()
            MONITOR_LOG.info("ComatAlarm monitor_cycle epoch=%d registrations=%d eligible=%d expired=%d central=%d",
                            int(now), len(rows), sum(r['expires'] > now for r in rows),
                            sum(r['expires'] <= now for r in rows), sum(r['expires'] > now and r['server_polling'] for r in rows))
        self.provider.credentials = json.loads(self.db.execute("SELECT credentials FROM comat_api_status WHERE id=1").fetchone()[0])
        # Plan only the same authorized, due central subscriptions the worker is
        # about to visit. No network request occurs until lookup needs a member.
        if isinstance(self.provider, FlightProvider):
            from .aibt_recovery import valid_exhaustion
            for archived in self.db.execute('SELECT payload FROM comat_app_history'):
                try:
                    flight = json.loads(archived['payload'])
                    marker = flight.get('fr24_arrival_exhausted')
                    if valid_exhaustion(flight) and 0 <= now-marker['at'] < 2*86400:
                        self.provider.remember_arrival_exhaustion(marker)
                except (KeyError, TypeError, ValueError):
                    continue
            self.provider.background_schedules = True
            self.provider.schedules.wakeup = self.poll_wakeup.set
            self.provider.arrival_wakeup = self.poll_wakeup.set
            for device in self.db.execute('SELECT * FROM comat_devices WHERE expires>? AND server_polling=1', (now,)).fetchall():
                try:
                    await self.apply_ready_schedules(device, now)
                except asyncio.CancelledError:
                    raise
                except Exception as exception:
                    logging.warning('ComatAlarm schedule_cache_apply_failed kind=%s', type(exception).__name__)
            batch_flights = []
            for row in self.db.execute("SELECT * FROM comat_devices WHERE expires>? AND server_polling=1", (now,)).fetchall():
                if row['id'] not in self.shared_authorized:
                    continue
                try:
                    flights = json.loads(row['watch'])
                    if not isinstance(flights, list):
                        continue
                    for f in flights:
                        if not isinstance(f, dict) or not self.shared_monitoring.current(f, now):
                            continue
                        token = self.shared_tokens.get(row['id'], row['api_token'])
                        if fast_due(f, now):
                            batch_flights.append((dict(f, _arrival_fast=True), token))
                        elif live_refresh_due(f, now):
                            batch_flights.append((dict(f, _live_only=True), token))
                        if now-row['last_poll'] >= 60:
                            batch_flights.append((f, token))
                except (AttributeError, KeyError, OverflowError, ValueError, TypeError):
                    continue
            self.provider.plan_batches(batch_flights, now)
            # All timely event/live work precedes every slow schedule lookup,
            # not merely the unrelated flights on one particular device.
            for quick in ('live', 'arrival'):
                for device in self.db.execute('SELECT * FROM comat_devices WHERE expires>? AND server_polling=1', (now,)).fetchall():
                    if device['id'] not in self.shared_authorized:
                        continue
                    try:
                        due = fast_due if quick == 'arrival' else live_refresh_due
                        if not any(due(f, now) for f in json.loads(device['watch']) if isinstance(f, dict)):
                            continue
                        await self.poll_device(device, now, report, quick=quick)
                        await self.deliver(device['id'], now)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exception:
                        logging.warning('ComatAlarm quick_poll_failed kind=%s', type(exception).__name__)
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
        await self.repair_archived_arrival(now)
        active = self.db.execute("SELECT api_token FROM comat_devices WHERE expires>? AND api_token!='' ORDER BY expires DESC LIMIT 1", (now,)).fetchone()
        if active:
            self.api_status.request_refresh(active['api_token'], manual=False, now=now)
        self.monitor.archive(now)
        with self.db:
            self.db.execute("DELETE FROM comat_outbox WHERE created<?",(now-2*86400,))
            self.db.execute("DELETE FROM comat_delivered WHERE sent_at<?", (now-2*86400,))
            # Bound, centrally monitored devices survive app inactivity and
            # temporary authorization outages. Revocation still stops all work.
            self.db.execute("UPDATE comat_devices SET api_token='',watch='[]' WHERE expires<=? AND NOT (server_polling=1 AND firebase_uid!='')",(now,))

    async def poll_device(self, device, now, report=False, quick=None):
        from .landing_alert import update_landing_alert
        from .prelanding_diagnostics import receipt as landing_receipt
        from . import aibt_recovery
        access = await self.stand_access(device['firebase_uid'])
        if device['firebase_uid'] and not access:
            with self.db:
                self.db.execute("UPDATE comat_devices SET error=? WHERE id=?", ("Pi-Abfrage pausiert: Firebase-Gerätefreigabe konnte nicht bestätigt werden", device['id']))
            if report:
                logging.warning("ComatAlarm monitor_skip reason=device_approval")
            return
        current = self.db.execute('SELECT * FROM comat_devices WHERE id=?', (device['id'],)).fetchone()
        if (current is None or current['credential'] != device['credential']
                or current['firebase_uid'] != device['firebase_uid']):
            return
        device = current
        positions = self.stand_model.positions() if access and self.stand_model.ready else unprotect_index(device['stand_index'])
        prefs = json.loads(device["preferences"])
        if access and device['server_polling']:
            self.history_sync.capture(json.loads(device['watch']), now)
        flights = [f for f in json.loads(device["watch"]) if self.shared_monitoring.current(f, now)]
        api_token = self.shared_tokens.get(device["id"], device["api_token"])
        calendar_error = "Kalenderdaten veraltet – App zur Aktualisierung öffnen" if prefs.get("calendar_valid_until", now+1) <= now else ""
        if quick or now-device["last_poll"] >= 60:
            error, pending, receipts, landing_receipts = calendar_error, [], [], []
            ordered = sorted(flights, key=lambda f: (bool(f.get('aibt')) or arrival_tracking_expired(f, now),
                self.last_successful_lookup.get(self.polling_key(f, api_token), 0)))
            for flight in ordered:
                if quick and not (aibt_recovery.fast_due(flight, now) if quick == 'arrival' else live_refresh_due(flight, now)):
                    continue
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
                late_arrival = arrival_tracking_expired(flight, now)
                if not departure_from_ber(flight) and (flight.get('aibt') or late_arrival):
                    flight.update(aibt_recovery.describe(flight, positions, now))
                refresh_coordinate = aibt_recovery.refresh_needed(flight, positions, now)
                repairing = bool(device["server_polling"] and not departure_from_ber(flight) and (flight.get("aibt") or late_arrival)
                                 and (not aibt_recovery.position(flight) or refresh_coordinate))
                outbound = bool(device["server_polling"] and flight.get("track_outbound") and not flight.get("_outbound", {}).get("atd"))
                if (not quick and isinstance(self.provider, FlightProvider) and device['server_polling']
                        and aibt_recovery.fast_candidate(flight, now) and not outbound):
                    continue  # The globally prioritized event pass owns taxi-in.
                if (late_arrival and not (repairing and aibt_recovery.in_window(flight, now))) or (not departure_from_ber(flight) and flight.get("aibt") and not repairing and not outbound) or flight.get("cancelled") or flight.get("diverted"):
                    continue
                # The arrival pass already checked fast_due above. A time-only
                # result must not put its missing coordinate back behind the
                # slower historical-recovery interval.
                recovery_due = repairing and (quick == 'arrival' or aibt_recovery.due(flight, now, positions))
                if repairing and not recovery_due and not outbound:
                    continue
                try:
                    query = dict(flight, _skip_parking_recovery=True) if repairing and not recovery_due else flight
                    if quick == 'arrival':
                        query = dict(flight, _arrival_fast=True, _parking_only=True)
                    elif quick == 'live':
                        query = dict(flight, _live_only=True)
                    elif aibt_recovery.fast_candidate(flight, now) and not aibt_recovery.fast_due(flight, now):
                        query = dict(query, _skip_parking_recovery=True)
                    if recovery_due and refresh_coordinate:
                        query = dict(query, _refresh_aibt_coordinate=True)
                    changes = await self.shared_lookup(query, api_token, now, central=bool(device["server_polling"]))
                    lookup_error = changes.pop("_error", "")
                    error = lookup_error or error
                    if lookup_error:
                        self.monitor.record("error", "Anbieterabfrage fehlgeschlagen oder pausiert", flight["number"], device["id"], now)
                    if recovery_due:
                        retry_at = self.provider.cooldown.get(hashlib.sha256(api_token.encode()).hexdigest(), 0) if isinstance(self.provider, FlightProvider) else 0
                        changes.update(aibt_recovery.attempt_update(flight, now, lookup_error, retry_at))
                    if changes.get("aibt_position"):
                        changes["stand"] = match_stand(changes["aibt_position"], positions)
                    if "_inbound" in changes:
                        point = (changes["_inbound"] or {}).get("aibt_position")
                        changes["stand"] = match_stand(point, positions) if point else None
                    updated = dict(flight, **changes)
                    if updated.get('aibt_position') and not aibt_recovery.position(updated) and not departure_from_ber(updated):
                        changes.update(aibt_position=None, stand=None)
                        updated.update(changes)
                    if not departure_from_ber(updated) and (updated.get("aibt") or late_arrival or quick == 'arrival'):
                        changes.update(aibt_recovery.describe(updated, positions, now, lookup_error))
                        status = changes['_parking_lookup_status']
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
                    message = aibt_recovery.receipt(updated, flight, now)
                    if message:
                        updated['_arrival_event_logged_at'] = now
                        receipts.append((message, flight['number']))
                    updated = update_landing_alert(flight, updated, now)
                    message = landing_receipt(flight, updated, prefs, now)
                    if message:
                        landing_receipts.append((message, flight['number']))
                    pending.extend(self.events(device["id"], flight, updated, prefs, now))
                    flight.clear(); flight.update(updated)
                except Exception as exception:
                    logging.warning("ComatAlarm flight_poll_failed kind=%s", type(exception).__name__)
                    error = "Flugabfrage fehlgeschlagen; Token, Tarif und Verbindung prüfen"
            # Compare-and-swap the entire snapshot after all network I/O. Removed
            # flights, changed preferences and foreground takeovers win the race.
            if device['firebase_uid']:
                access = await self.stand_access(device['firebase_uid'])
                if not access or access['uid'] != device['firebase_uid']:
                    return
            with self.db:
                checked = device['last_poll'] if quick else now
                changed = self.db.execute("""UPDATE comat_devices SET watch=?,last_poll=?,error=?
                    WHERE id=? AND watch=? AND preferences=? AND credential=? AND firebase_uid=? AND api_token=?
                    AND stand_index=? AND server_polling=? AND (server_polling=1 OR foreground_until<=?) AND expires>?""",
                    (json.dumps(flights), checked, error, device['id'], device['watch'], device['preferences'],
                     device['credential'], device['firebase_uid'], device['api_token'], device['stand_index'],
                     device['server_polling'], now, now)).rowcount
                if changed:
                    self.db.executemany("INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)", pending)
                    current = self.db.execute('SELECT credential,firebase_uid,server_polling FROM comat_devices WHERE id=?', (device['id'],)).fetchone()
                    if (access and current['server_polling'] and current['credential'] == device['credential']
                            and current['firebase_uid'] == device['firebase_uid'] == access['uid']):
                        self.history_sync.capture(flights, now)
            if changed:
                for message, number in receipts:
                    self.monitor.record('arrival', message, number, device['id'], now)
                for message, number in landing_receipts:
                    self.monitor.record('prelanding', message, number, device['id'], now)
            if report:
                MONITOR_LOG.info("ComatAlarm monitor_result epoch=%d flights=%d committed=%s provider_error=%s events=%d",
                                int(now), len(flights), bool(changed), bool(error), len(pending) if changed else 0)
        else:
            # Evaluate the short final-approach window on every worker pass using
            # the last fresh observation; this performs no extra provider call.
            pending, landing_receipts = [], []
            for flight in flights:
                updated = update_landing_alert(flight, flight, now)
                message = landing_receipt(flight, updated, prefs, now)
                if message:
                    landing_receipts.append((message, flight['number']))
                pending.extend(self.events(device["id"], flight, updated, prefs, now))
                flight.clear(); flight.update(updated)
            encoded = json.dumps(flights)
            if encoded != device["watch"] or calendar_error:
                with self.db:
                    changed = self.db.execute("UPDATE comat_devices SET watch=?,error=? WHERE id=? AND watch=? AND preferences=? AND expires>?",
                        (encoded, calendar_error or device["error"], device["id"], device["watch"], device["preferences"], now)).rowcount
                    if changed:
                        self.db.executemany("INSERT OR IGNORE INTO comat_outbox VALUES(?,?,?,?,0)", pending)
                if changed:
                    for message, number in landing_receipts:
                        self.monitor.record('prelanding', message, number, device['id'], now)

    async def repair_archived_arrival(self, now):
        """Repair one trusted recent archive after live work, without subscriptions or alerts."""
        from . import aibt_recovery
        if not isinstance(self.provider, FlightProvider):
            return
        approved = [r for r in self.db.execute("SELECT * FROM comat_devices WHERE server_polling=1 AND expires>? AND firebase_uid!=''", (now,))
                    if r['id'] in self.shared_authorized]
        watched = []
        for row in approved:
            try:
                watched.extend(f for f in json.loads(row['watch']) if isinstance(f, dict))
            except (TypeError, ValueError):
                continue
        for source in approved:
            token = source['api_token']
            access = await self.stand_access(source['firebase_uid'])
            current = self.db.execute('SELECT * FROM comat_devices WHERE id=?', (source['id'],)).fetchone()
            if (not access or current is None or access['uid'] != source['firebase_uid']
                    or any(current[k] != source[k] for k in ('credential', 'firebase_uid', 'api_token'))
                    or not current['server_polling'] or current['expires'] <= now):
                continue
            positions = self.stand_model.positions() if self.stand_model.ready else unprotect_index(current['stand_index'])
            candidates = []
            for row in self.db.execute('SELECT * FROM comat_app_history'):
                try:
                    flight = json.loads(row['payload'])
                    if ((flight.get('aibt') or arrival_tracking_expired(flight, now))
                            and (aibt_recovery.due(flight, now, positions) or self.provider.arrival_changes(self.arrival_context(flight, token), now))
                            and not self.history_sync.removed(flight)
                            and not any(same_occurrence(flight, f) for f in watched)):
                        candidates.append((row, flight))
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
            candidates.sort(key=lambda item: item[1].get('_aibt_recovery_at', 0))
            if not candidates:
                return
            row, flight = candidates[0]
            # Alternative sources have their own budgets. An empty/exhausted
            # FR24 account cannot suspend arrival recovery for the whole group.
            refresh = aibt_recovery.refresh_needed(flight, positions, now)
            query = self.arrival_context(dict(flight, _refresh_aibt_coordinate=refresh, _parking_only=True), token)
            changes = self.provider.arrival_changes(query, now)
            completed_job = bool(changes)
            if not changes:
                changes = await self.shared_lookup(query, token, now, central=True)
            lookup_error = changes.pop('_error', '')
            # Authorization may change during either provider I/O or the final
            # approval await. Every check below is repeated after the last await.
            access = await self.stand_access(source['firebase_uid'])
            current = self.db.execute('SELECT * FROM comat_devices WHERE id=?', (source['id'],)).fetchone()
            latest = self.db.execute('SELECT payload FROM comat_app_history WHERE identity=?', (row['identity'],)).fetchone()
            if (not access or current is None or access['uid'] != source['firebase_uid']
                    or any(current[k] != source[k] for k in ('credential', 'firebase_uid', 'api_token'))
                    or not current['server_polling'] or current['expires'] <= now
                    or latest is None or latest['payload'] != row['payload'] or self.history_sync.removed(flight)):
                return
            retry_at = self.provider.cooldown.get(hashlib.sha256(token.encode()).hexdigest(), 0)
            if not completed_job:
                changes.update(aibt_recovery.attempt_update(flight, now, lookup_error, retry_at))
            updated = dict(flight, **changes)
            if updated.get('aibt_position') and not aibt_recovery.position(updated):
                updated.pop('aibt_position', None)
                updated.pop('stand', None)
            updated.update(aibt_recovery.describe(updated, positions, now, lookup_error, fast=False))
            message = aibt_recovery.receipt(updated, flight, now)
            if message:
                updated['_arrival_event_logged_at'] = now
            # Preserve original aliases and unknown creation provenance; a repair
            # is neither a fresh user creation nor a new subscription.
            updated.pop('id', None)
            self.history_sync.capture([updated], now, created_known=bool(flight.get('_created_at_proven')))
            if message:
                self.monitor.record('arrival', message, flight['number'], source['id'], now)
            return  # At most one archive chain in a worker pass; HTTP budget is shared.

    def events(self, device_id, old, updated, prefs, now):
        if old.get('_arrival_recovery_only') or updated.get('_arrival_recovery_only'):
            return []
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
                state = {k:updated[k] for k in ("id","atd","ata","aibt","stand",
                        'aibt_source', 'aibt_position', 'fr24_arrival_exhausted') if updated.get(k)}
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
                    data['flight_occurrence'] = dict(number=updated['number'], day=day,
                        fr24_id=updated.get('fr24ID'), origin=updated.get('origin', ''),
                        destination=updated.get('destination', ''))
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
                flights = json.loads(current['watch'])
                flight = next((f for f in flights if f['id'] == payload['flight_id']), None)
                from .landing_alert import prelanding_is_current
                prelanding = payload['flag'] == 'alarmPrelanding'
                reason = ''
                if prelanding:
                    marker = payload.get('prelanding')

                    def same_warning(candidate):
                        # Local IDs can change when iOS merges a shared alias.
                        # Resolve the queued warning, never merely the flight
                        # number or a reused local ID. The existing event hash
                        # also proves number/day for pre-upgrade outbox rows.
                        if (not isinstance(marker, dict) or not marker.get('fr24_id')
                                or candidate.get('fr24ID') != marker['fr24_id']
                                or candidate.get('_prelanding') != marker
                                or stamp(marker.get('triggered_at')) is None
                                or payload.get('expires_at') != marker.get('expires_at')):
                            return False
                        reference = stamp(candidate.get('reference'))
                        number = candidate.get('number')
                        if reference is None or not isinstance(number, str):
                            return False
                        day = datetime.fromtimestamp(reference, ZoneInfo('Europe/Berlin')).date().isoformat()
                        event_id = hashlib.sha256((number + ':' + day + ':prelanding').encode()).hexdigest()
                        if item['event'] != event_id or payload.get('event_id') != event_id:
                            return False
                        if 'flight_occurrence' not in payload:
                            return True
                        occurrence = payload['flight_occurrence']
                        return (isinstance(occurrence, dict) and occurrence.get('number') == number
                            and occurrence.get('day') == day and occurrence.get('fr24_id') == candidate.get('fr24ID')
                            and all(not occurrence.get(key) or occurrence[key] == candidate.get(key)
                                    for key in ('origin', 'destination')))

                    if flight is None or not same_warning(flight):
                        matches = [f for f in flights if same_warning(f)]
                        flight = matches[0] if len(matches) == 1 else None
                        if len(matches) > 1:
                            reason = 'prelanding_ambiguous'
                    expiry = stamp(payload.get('expires_at'))
                    if expiry is not None and expiry <= now:
                        reason = 'prelanding_expired'
                    elif not flight and not reason:
                        reason = 'prelanding_identity_changed'
                if not reason:
                    if not flight:
                        reason = 'flight_missing'
                    elif prelanding and (self.monitor.operator.removed(flight)
                            or current['server_polling'] and current['firebase_uid'] and self.shared_monitoring.removed(flight)):
                        reason = 'flight_removed'
                    elif prelanding and not prelanding_is_current(flight, payload.get('prelanding'), now):
                        reason = 'prelanding_stale'
                    elif not self.enabled(prefs, now):
                        reason = ('alarms_disabled' if not prefs.get('enabled', True)
                                  else 'calendar_expired' if prefs.get('calendar_valid_until', now+1) <= now
                                  else 'outside_calendar_window')
                    elif not prefs.get(payload['flag'], True):
                        reason = 'alarm_disabled'
                    elif current['foreground_until'] > now and not prefs.get('relay_alerts', False):
                        reason = 'foreground_local_owner'
                if reason:
                    explanation = {'prelanding_ambiguous': 'mehrere Flugkopien passen zur vorgemerkten Warnung',
                        'prelanding_expired': 'Gültigkeit des Voralarms abgelaufen',
                        'prelanding_identity_changed': 'Flug oder vorgemerkte Warnung nicht mehr eindeutig vorhanden',
                        'flight_missing': 'Flug nicht mehr überwacht', 'flight_removed': 'Flug wurde gelöscht',
                        'prelanding_stale': 'Anflugdaten nicht mehr für den Voralarm gültig',
                        'alarms_disabled': 'Alarmmeldungen deaktiviert', 'calendar_expired': 'Kalenderdaten abgelaufen',
                        'outside_calendar_window': 'außerhalb einer aktiven Kalenderschicht',
                        'alarm_disabled': 'dieser Alarm deaktiviert',
                        'foreground_local_owner': 'Vordergrund-App übernimmt die Alarmmeldung'}[reason]
                    MONITOR_LOG.info('ComatAlarm push_skipped epoch=%d event=%s reason=%s', int(now), payload['flag'], reason)
                    self.monitor.record('push_skipped', payload['flag'] + ': ' + explanation,
                                        flight['number'] if flight else '', device_id, now)
                    with self.db:
                        self.db.execute("DELETE FROM comat_outbox WHERE device=? AND event=?", (device_id,item["event"]))
                    continue
                if prelanding and flight['id'] != payload['flight_id']:
                    # Preserve the event/collapse key and deadline; only route
                    # the widget snapshot to this device's current local alias.
                    payload = dict(payload, flight_id=flight['id'],
                                   flight_state=dict(payload.get('flight_state') or {}, id=flight['id']))
                delivered = self.db.execute("SELECT 1 FROM comat_delivered WHERE token=? AND environment=? AND event=?", (current["token"], current["environment"], item["event"])).fetchone()
                try:
                    accepted = bool(delivered) or bool(self.sender and await self.sender(current["token"],current["environment"],payload))
                except Exception:
                    accepted = False
                number = flight['number']
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
                cycle_start = asyncio.get_running_loop().time()
                self.poll_wakeup.clear()
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # No exception text/traceback: provider errors may contain secrets.
                    logging.warning("ComatAlarm monitor_cycle_failed kind=%s; retry_in_seconds=15", type(error).__name__)
                try:
                    # Keep a target cadence from the start of the cycle instead
                    # of adding another 15 seconds after slow provider work.
                    remaining = max(0, 15-(asyncio.get_running_loop().time()-cycle_start))
                    await asyncio.wait_for(self.poll_wakeup.wait(), timeout=remaining)
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
            if isinstance(self.provider, FlightProvider):
                await self.provider.schedules.close()
                await self.provider.close_arrivals()
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


