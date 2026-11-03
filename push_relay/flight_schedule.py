"""Shared schedule lookups for Pi-owned tracking (never manufacture runway events)."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def timestamp(value, zone="UTC"):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.replace(tzinfo=date.tzinfo or ZoneInfo(zone)).timestamp()
    except (ValueError, KeyError):
        return None


def compact(values):
    return {k:v for k,v in values.items() if v is not None and v != ""}


def ber_schedule(row, departing):
    result = {}
    fields = {"std": ("dep_scheduled_time", "scheduled_time"), "etd": ("dep_estimated_time",)} if departing else {"sta": ("arr_scheduled_time", "scheduled_time"), "eta": ("arr_estimated_time",)}
    for key, candidates in fields.items():
        result[key] = next((timestamp(row.get(k), "Europe/Berlin") for k in candidates if timestamp(row.get(k), "Europe/Berlin")), None)
    result.update(origin=row.get("dep_airport_iata"), destination=row.get("arr_airport_iata"), registration=row.get("aircraft_reg"), aircraftType=row.get("aircraft_type"))
    return compact(result)


def flightstats(root, official, reference):
    if official:
        rows = root.get("flightStatuses") or []
        def value(row, key):
            return timestamp((row.get("operationalTimes", {}).get(key) or {}).get("dateUtc"))
        if not rows:
            return {}
        row = min(rows, key=lambda r: abs((value(r, "scheduledGateDeparture") or reference)-reference))
        result = {k:value(row, v) for k,v in {"std":"scheduledGateDeparture", "etd":"estimatedGateDeparture", "atd":"actualRunwayDeparture", "sta":"scheduledGateArrival", "eta":"estimatedRunwayArrival", "ata":"actualRunwayArrival", "eibt":"estimatedGateArrival"}.items()}
        result["eta"] = result["eta"] or result["eibt"]
        equipment = row.get("flightEquipment") or {}
        result.update(origin=row.get("departureAirportFsCode"), destination=row.get("arrivalAirportFsCode"), registration=equipment.get("tailNumber"), aircraftType=equipment.get("actualEquipmentIataCode") or equipment.get("scheduledEquipmentIataCode"), cancelled=row.get("status")=="C", diverted=row.get("status")=="D")
    else:
        row = root.get("data", root) or {}
        schedule = row.get("schedule") or {}
        departure, arrival = row.get("departureAirport") or {}, row.get("arrivalAirport") or {}
        def value(key, airport):
            return timestamp(schedule.get(key+"UTC")) or timestamp(schedule.get(key), airport.get("timeZoneRegionName") or "UTC")
        result = {k:value(v, departure if k in ("std","etd","atd") else arrival) for k,v in {"std":"scheduledDeparture", "etd":"estimatedActualDeparture", "atd":"actualRunwayDeparture", "sta":"scheduledArrival", "eta":"estimatedActualArrival", "ata":"actualRunwayArrival", "eibt":"estimatedGateArrival"}.items()}
        note = row.get("flightNote") or {}
        if result["atd"] is None and note.get("hasActualRunwayDepartureTime") is True:
            result["atd"] = result["etd"]
        if result["ata"] is None and note.get("hasActualRunwayArrivalTime") is True:
            result["ata"] = result["eta"]
        equipment, status = row.get("equipment") or {}, row.get("status") or {}
        result.update(origin=departure.get("iata") or departure.get("fs"), destination=arrival.get("iata") or arrival.get("fs"), registration=equipment.get("tailNumber"), aircraftType=equipment.get("iata"), cancelled=status.get("statusCode")=="C" or "cancel" in str(status.get("status", "")).lower(), diverted=status.get("statusCode")=="D" or "divert" in str(status.get("status", "")).lower())
    # AIBT remains the FR24 gate-arrival event, not a schedule estimate.
    return compact(result)


async def schedule_lookup(session, flight, credentials, cooldown, now):
    from .comatalarm import retry_delay
    if cooldown.get("flightstats", 0) > now:
        return {}
    parts = re.fullmatch(r"([A-Z][A-Z0-9]{1,2}?)([0-9]{1,5})", flight["number"])
    if not parts:
        return {}
    carrier, digits = parts.groups()
    day = datetime.fromtimestamp(flight["reference"], ZoneInfo("Europe/Berlin"))
    official = bool(credentials.get("FLIGHTSTATS_APP_ID") and credentials.get("FLIGHTSTATS_APP_KEY"))
    suffix = f"{carrier}/{digits}/{day.year}/{day.month}/{day.day}"
    if official:
        url = f"https://api.flightstats.com/flex/flightstatus/rest/v2/json/flight/status/{carrier}/{digits}/dep/{day.year}/{day.month}/{day.day}"
        params = {"appId":credentials["FLIGHTSTATS_APP_ID"], "appKey":credentials["FLIGHTSTATS_APP_KEY"], "utc":"false"}
    else:
        url, params = "https://www.flightstats.com/v2/api-next/flight-tracker/"+suffix, None
    async with session.get(url, params=params, headers={"Accept":"application/json", "Referer":"https://www.flightstats.com/", "User-Agent":"ComatAlarm/1.0"}, allow_redirects=False) as response:
        if response.status in (401,402,403,429):
            cooldown["flightstats"] = now + retry_delay(response, now)
        if response.status != 200:
            return {}
        result = flightstats(await response.json(), official, flight["reference"])
    if any(flight.get(k) and result.get(k) and flight[k] != result[k] for k in ("origin","destination")):
        return {}
    for k in ("atd","ata"):
        if k in result and not flight["reference"]-86400 <= result[k] <= now+60:
            result.pop(k)
    return result


async def aware_lookup(session, flight, credentials, cooldown, now):
    from .comatalarm import retry_delay
    key = credentials.get("FLIGHTAWARE_API_KEY")
    if not key or cooldown.get("flightaware", 0) > now:
        return {}
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    params = {"start":iso(flight["reference"]-86400), "end":iso(flight["reference"]+86400)}
    async with session.get("https://aeroapi.flightaware.com/aeroapi/flights/"+flight["number"], params=params, headers={"x-apikey":key}, allow_redirects=False) as response:
        if response.status in (401,402,403,429):
            cooldown["flightaware"] = now + retry_delay(response, now)
        if response.status != 200:
            return {}
        rows = (await response.json()).get("flights") or []
    day = datetime.fromtimestamp(flight["reference"], ZoneInfo("Europe/Berlin")).date()
    def matches(row):
        when = timestamp(row.get("scheduled_out")) or timestamp(row.get("scheduled_off"))
        if not when or datetime.fromtimestamp(when, ZoneInfo("Europe/Berlin")).date() != day:
            return False
        for field in ("origin", "destination"):
            airport = (row.get(field) or {}).get("code_iata")
            if flight.get(field) and airport and airport != flight[field]:
                return False
        return True
    rows = [r for r in rows if isinstance(r,dict) and matches(r)]
    if not rows:
        return {}
    row = min(rows, key=lambda r: abs((timestamp(r.get("scheduled_out")) or timestamp(r.get("scheduled_off")))-flight["reference"]))
    result = {k:timestamp(row.get(v)) for k,v in {"std":"scheduled_out","etd":"estimated_out","atd":"actual_off","sta":"scheduled_in","eta":"estimated_on","ata":"actual_on","eibt":"estimated_in"}.items()}
    result["eta"] = result["eta"] or result["eibt"]
    result.update(registration=row.get("registration"), aircraftType=row.get("aircraft_type"), cancelled=row.get("cancelled") is True, diverted=row.get("diverted") is True)
    return compact(result)
