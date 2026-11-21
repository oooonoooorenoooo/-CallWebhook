"""A short-lived, live-data-only warning close to the predicted touchdown.

An actual landing timestamp is only known after landing. This policy therefore
requires two fresh, consistent observations of a plausible final approach. It
never schedules an alarm from a timetable or fills a missing ETA with STA.
State belongs to the flight occurrence and is persisted by the monitor.
"""
from datetime import datetime, timezone
import math


# Same destination reference points as ComatAirport.all in the iOS app. They
# validate proximity/course, not runway assignment or a precise touchdown point.
AIRPORTS = {
    "BER": (52.364444, 13.509722), "FRA": (50.033333, 8.570556),
    "MUC": (48.353783, 11.786086), "HAM": (53.630278, 9.991111),
    "DUS": (51.289444, 6.766667), "CGN": (50.865833, 7.142778),
    "STR": (48.689878, 9.221964), "NUE": (49.498731, 11.078900),
    "HAJ": (52.461056, 9.685078), "LEJ": (51.423889, 12.236389),
    "BRE": (53.047779, 8.786667), "DRS": (51.132833, 13.767161),
    "ERF": (50.979811, 10.958114), "SCN": (49.214600, 7.109500),
    "ZRH": (47.464722, 8.549167), "GVA": (46.238064, 6.108997),
    "BSL": (47.600000, 7.533333), "VIE": (48.110278, 16.569722),
    "GRZ": (46.991111, 15.439167), "INN": (47.260278, 11.343889),
    "SZG": (47.793333, 13.004167), "LNZ": (48.238889, 14.187500),
    "BRU": (50.901389, 4.484444), "AMS": (52.308056, 4.764167),
    "LHR": (51.477500, -0.461389), "CDG": (49.012779, 2.550000),
    "ORY": (48.723333, 2.379444), "CPH": (55.617917, 12.656111),
    "PRG": (50.100833, 14.260000), "WAW": (52.165833, 20.967222),
    "BUD": (47.436667, 19.255278), "FCO": (41.804444, 12.250833),
    "MXP": (45.630278, 8.728056), "MAD": (40.472222, -3.560833),
    "BCN": (41.296944, 2.078333), "LIS": (38.774167, -9.134167),
    "ATH": (37.936389, 23.944444), "IST": (41.275278, 28.751944),
    "HEL": (60.317222, 24.963333), "OSL": (60.193917, 11.100361),
    "ARN": (59.651944, 17.918611),
}

# Aerodrome elevation above mean sea level, feet. Published by the dataset
# maintainer at https://davidmegginson.github.io/ourairports-data/airports.csv
# (2026-11-18; field definition: https://ourairports.com/help/data-dictionary.html).
# FR24 barometric altitude is approximate; wide tolerances below are deliberate.
ELEVATIONS_FT = {
    "BER": 157, "FRA": 364, "MUC": 1487, "HAM": 53, "DUS": 147,
    "CGN": 302, "STR": 1276, "NUE": 1046, "HAJ": 183, "LEJ": 465,
    "BRE": 14, "DRS": 755, "ERF": 1036, "SCN": 1058, "ZRH": 1417,
    "GVA": 1411, "BSL": 885, "VIE": 600, "GRZ": 1115, "INN": 1907,
    "SZG": 1411, "LNZ": 980, "BRU": 175, "AMS": -11, "LHR": 83,
    "CDG": 392, "ORY": 291, "CPH": 17, "PRG": 1247, "WAW": 362,
    "BUD": 495, "FCO": 13, "MXP": 768, "MAD": 1998, "BCN": 12,
    "LIS": 374, "ATH": 308, "IST": 325, "HEL": 179, "OSL": 681,
    "ARN": 137,
}

MAX_SAMPLE_AGE = 45
WARNING_LEAD = 180
MIN_WARNING_LEAD = 150
# A bounded one-minute revision must not erase independently measured approach
# convergence. Larger corrections still require a new observation pair.
MAX_ETA_CHANGE = 60


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _stamp(value):
    number = _number(value)
    if number is not None:
        return number if number > 1_000_000_000 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def _assessment(flight, now, max_age=MAX_SAMPLE_AGE, refresh=False):
    """Return evidence, an authored reason and safe numeric diagnostics.

    Selecting a flight for a new observation is deliberately broader than
    authorizing an alert from that observation. ETA is only required to alert.
    """
    values = {}
    if any(flight.get(k) for k in ("ata", "aibt", "cancelled", "diverted")):
        return None, 'terminal', values
    live = flight.get("_live")
    if not isinstance(live, dict):
        return None, 'no_live', values
    destination = str(flight.get("destination") or "").upper()
    airport = AIRPORTS.get(destination)
    identifier = live.get("fr24_id")
    if (not airport or not isinstance(identifier, str) or not identifier
            or identifier.upper().startswith(("TEMP_", "NS_", "LANDED_"))
            or flight.get("fr24ID") != identifier):
        return None, 'identity', values
    # The exact leg ID binds the observation. Route fields are optional in the
    # official full-position model; an explicit conflicting route still vetoes it.
    if live.get('dest_iata') and str(live['dest_iata']).upper() != destination:
        return None, 'destination', values
    # Older FR24 responses may omit source. Explicit estimated/replayed tracks
    # are never proof of a live final approach.
    source = str(live.get("source") or "").upper()
    if source and source not in ("ADS-B", "ADSB", "ADS_B", "MLAT"):
        return None, 'source', values
    # The official FR24 FlightPositionsFull schema has no on_ground field.
    # Missing optional extensions cannot veto an otherwise measured approach;
    # explicit ground/invalid values still veto it, as do the altitude, speed,
    # descent, geometry and freshness checks below.
    if "on_ground" in live and live.get("on_ground") not in (False, 0):
        return None, 'ground', values
    observed, eta = _stamp(live.get("timestamp")), _stamp(live.get("eta"))
    if observed is None:
        return None, 'timestamp', values
    values['sample_age_seconds'] = now-observed
    if eta is not None:
        values.update(eta=eta, lead_seconds=eta-now)
    if not -5 <= now-observed <= max_age:
        return None, 'stale', values
    lat, lon, altitude, speed, vertical, track = (
        _number(live.get(k)) for k in ("lat", "lon", "alt", "gspeed", "vspeed", "track"))
    if (any(v is None for v in (lat, lon, altitude, speed, vertical, track))
            or not -90 <= lat <= 90 or not -180 <= lon <= 180
            or not 0 <= track <= 360):
        return None, 'telemetry', values
    height = altitude-ELEVATIONS_FT[destination]
    values.update(height_ft=height, speed_kt=speed, vertical_fpm=vertical)
    if refresh:
        if not (400 <= height <= 9000 and 80 <= speed <= 300 and -3500 <= vertical <= 200):
            return None, 'telemetry', values
    elif not (250 <= altitude <= 6000 and 80 <= speed <= 260 and -2500 <= vertical <= -250):
        return None, 'telemetry', values
    origin_lat, origin_lon = math.radians(lat), math.radians(lon)
    target_lat, target_lon = map(math.radians, airport)
    delta_lon = target_lon-origin_lon
    a = math.sin((target_lat-origin_lat)/2)**2 + math.cos(origin_lat)*math.cos(target_lat)*math.sin(delta_lon/2)**2
    distance = 6371 * 2 * math.asin(math.sqrt(min(1, max(0, a))))
    bearing = math.degrees(math.atan2(math.sin(delta_lon)*math.cos(target_lat),
        math.cos(origin_lat)*math.sin(target_lat)-math.sin(origin_lat)*math.cos(target_lat)*math.cos(delta_lon))) % 360
    course_error = abs((track-bearing+180) % 360-180)
    values.update(distance_km=distance, height_ft=height, course_error=course_error)
    if refresh:
        if not 4 <= distance <= 60 or course_error > 60:
            return None, 'geometry', values
        return {'fr24_id': identifier, 'destination': destination, 'observed_at': observed,
                'eta': eta, 'altitude_ft': altitude, 'distance_km': distance}, 'refresh', values
    if eta is None:
        return None, 'eta_missing', values
    if not 0 < eta-now <= 600:
        return None, 'eta_range', values
    direct_seconds = distance/(speed*1.852)*3600
    if not 4 <= distance <= 30 or course_error > 40 or abs(direct_seconds-(eta-observed)) > 90:
        return None, 'geometry', values
    path_angle = math.degrees(math.atan2(height*0.3048, distance*1000))
    descent_seconds = height/(-vertical)*60
    # A low vertical speed at several thousand feet is not evidence of a
    # three-minute final, even if the provider's ETA claims otherwise. Check
    # relative height, geometric approach slope and time to lose that height.
    if not 400 <= height <= 4500 or not 1.5 <= path_angle <= 5.5 or abs(descent_seconds-(eta-observed)) > 120:
        return None, 'descent', values
    return {"fr24_id": identifier, "destination": destination, "observed_at": observed,
            "eta": eta, "altitude_ft": altitude, "distance_km": distance}, 'eligible', values


def _candidate(flight, now, max_age=MAX_SAMPLE_AGE):
    return _assessment(flight, now, max_age=max_age)[0]

def prelanding_refresh_candidate(flight, now):
    """Fetch every 30s once measured flight data indicates an inbound approach.

    A point up to 90s old may justify fetching a new observation, never sending
    an alert. Delivery still uses MAX_SAMPLE_AGE and the original lead window.
    It must start before the narrower delivery gate: waiting for all alarm
    conditions before fetching the confirming observation can miss the window.
    Neither a live nor a timetable ETA is needed to request that observation.
    """
    state = flight.get("_landing_alert")
    if (isinstance(state, dict) and state.get("fr24_id") == flight.get("fr24ID")
            and "triggered_at" in state):
        return False
    candidate, _, _ = _assessment(flight, now, max_age=90, refresh=True)
    return bool(candidate)


def update_landing_alert(previous, current, now):
    """Return a flight snapshot with durable confidence and warning state.

    Call on each monitor tick, including ticks without a paid provider request.
    Reusing one sample never increases confidence, but a still-fresh confirmed
    sample may cross the three-minute boundary during an ordinary 15s tick.
    """
    result = dict(current)
    stored = current.get("_landing_alert", previous.get("_landing_alert", {}))
    stored = dict(stored) if isinstance(stored, dict) else {}
    marker = current.get("_prelanding", previous.get("_prelanding"))
    result.pop("_prelanding", None)
    candidate = _candidate(current, now)
    identifier = current.get("fr24ID")
    if stored.get("fr24_id") != identifier:
        stored = {}
    triggered = {k: stored[k] for k in ("fr24_id", "destination", "triggered_at") if k in stored}
    if candidate is None:
        # Normal provider polling is 60s, and received positions are already a
        # few seconds old. Aging past the *delivery* freshness limit must not
        # erase the previous observation before the next poll can confirm it.
        # Retain confidence for at most one bounded observation gap, but never
        # retain a warning or treat an aged position as current evidence.
        aged = _candidate(current, now, max_age=90)
        if (aged is not None and stored.get("observed_at") == aged["observed_at"]
                and all(stored.get(k) == v for k, v in aged.items())):
            result["_landing_alert"] = stored
            return result
        # A go-around, stale data, missing ETA or heading change cancels a pending
        # warning. Keep the occurrence's trigger marker to prevent repeated alerts.
        result["_landing_alert"] = triggered
        return result
    same_leg = (stored.get("fr24_id") == candidate["fr24_id"]
                and stored.get("destination") == candidate["destination"])
    old_observed = _number(stored.get("observed_at"))
    count = 1
    confirmation_reason = 'confirmation'
    if same_leg and old_observed is not None:
        gap = candidate["observed_at"]-old_observed
        if gap == 0 and candidate == {k: stored.get(k) for k in candidate}:
            count = stored.get("samples", 1)
            confirmation_reason = stored.get('confirmation_reason', 'confirmation')
        elif (10 <= gap <= 90 and abs(candidate["eta"]-stored.get("eta", 0)) <= MAX_ETA_CHANGE
                and candidate["distance_km"] < stored.get("distance_km", 0)-0.15
                and candidate["altitude_ft"] < stored.get("altitude_ft", 0)):
            count = min(3, stored.get("samples", 1)+1)
        elif not 10 <= gap <= 90:
            confirmation_reason = 'observation_gap'
        elif abs(candidate['eta']-stored.get('eta', 0)) > MAX_ETA_CHANGE:
            confirmation_reason = 'eta_changed'
        else:
            confirmation_reason = 'not_converging'
    state = dict(candidate, samples=count)
    if count < 2:
        state['confirmation_reason'] = confirmation_reason
    if same_leg and "triggered_at" in triggered:
        state["triggered_at"] = triggered["triggered_at"]
    lead = candidate["eta"]-now
    if "triggered_at" not in state and count >= 2 and MIN_WARNING_LEAD < lead <= WARNING_LEAD:
        state["triggered_at"] = now
        marker = {"fr24_id": candidate["fr24_id"], "eta": candidate["eta"],
                  "observed_at": candidate["observed_at"], "triggered_at": now,
                  "expires_at": min(now+30, candidate["eta"]-MIN_WARNING_LEAD)}
    result["_landing_alert"] = state
    if isinstance(marker, dict):
        result["_prelanding"] = marker
        if not prelanding_is_current(result, marker, now):
            result.pop("_prelanding", None)
    return result


def prelanding_is_current(flight, marker, now):
    """Recheck a queued warning immediately before each APNs delivery attempt."""
    if not isinstance(marker, dict) or not isinstance(flight.get("_prelanding"), dict):
        return False
    expiry, predicted = _number(marker.get("expires_at")), _stamp(marker.get("eta"))
    candidate = _candidate(flight, now)
    return bool(candidate and expiry is not None and predicted is not None and now < expiry
                and candidate["fr24_id"] == marker.get("fr24_id")
                and MIN_WARNING_LEAD < candidate["eta"]-now <= WARNING_LEAD
                and abs(candidate["eta"]-predicted) <= MAX_ETA_CHANGE)


def alert_ttl(payload, now):
    """Bound APNs storage too; an offline phone must not get an old warning."""
    if payload.get("flag") != "alarmPrelanding":
        return 300
    expiry = _number(payload.get("expires_at"))
    if expiry is None or expiry <= now:
        return None
    return max(1, min(30, math.floor(expiry-now)))
