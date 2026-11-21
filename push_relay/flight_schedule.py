"""Shared schedule lookups for Pi-owned tracking (never manufacture runway events)."""
import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class ScheduleCache:
    """At most two private supplemental lookups; never writes application state."""
    def __init__(self, provider):
        self.provider = provider
        self.results, self.started, self.tasks = {}, {}, {}
        self.wakeup = None
        self.closed = False

    def key(self, flight, token, credentials=None):
        credentials = self.provider.credentials if credentials is None else credentials
        fingerprint = hashlib.sha256(json.dumps(credentials, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return (hashlib.sha256(token.encode()).hexdigest(), fingerprint, bool(flight.get('_central')),
                flight['number'], flight['reference'],
                flight.get('origin') or '', flight.get('destination') or '',
                str(flight.get('fr24ID') or '').upper(), flight.get('registration') or '')

    def last_started(self, flight, token):
        return self.started.get(self.key(flight, token), 0)

    def get(self, flight, token, now):
        entry = self.results.get(self.key(flight, token))
        if entry is None or not 0 <= now-entry[0] < 1800:
            return {}
        result = dict(entry[1])
        result['_schedule_checked_at'] = entry[0]
        return result

    def request(self, flight, token, now):
        key = self.key(flight, token)
        if (not self.closed and key not in self.tasks and len(self.tasks) < 2
                and now-self.started.get(key, 0) >= 60):
            snapshot, credentials = dict(flight), dict(self.provider.credentials)
            self.started[key] = now
            task = asyncio.create_task(self._fetch(key, snapshot, token, credentials, now))
            self.tasks[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        # No queued tasks: the authorized worker reoffers still-due candidates,
        # oldest first, while completed entries keep their normal60s interval.
        return self.get(flight, token, now)

    async def _fetch(self, key, flight, token, credentials, now):
        result = await self.provider.lookup(dict(flight, _schedule_only=True),
                                            token, now, schedule_credentials=credentials)
        # A result belonging to another route cannot supplement this occurrence.
        if any(flight.get(k) and result.get(k) and flight[k] != result[k] for k in ('origin', 'destination')):
            result = {}
        allowed = ('_schedule', 'gate', 'gate_source', 'atd', 'ata', 'aibt', 'aibt_source',
                   'aibt_position', 'flightawareID', 'flightstatsID', 'icao24', 'origin',
                   'destination', 'registration', 'cancelled', 'diverted')
        self.results[key] = (now, {k: result[k] for k in allowed if k in result})
        self.results = {k: v for k, v in self.results.items() if now-v[0] < 1800}
        self.started = {k: v for k, v in self.started.items() if now-v < 1800 or k in self.tasks}

    def _finished(self, key, task):
        if self.tasks.get(key) is task:
            self.tasks.pop(key, None)
        if not task.cancelled():
            task.exception()  # Consume failures; retry only through the normal interval.
        if not self.closed and self.wakeup:
            self.wakeup()

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


def timestamp(value, zone="UTC"):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.replace(tzinfo=date.tzinfo or ZoneInfo(zone)).timestamp()
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        return None


def compact(values):
    return {k:v for k,v in values.items() if v is not None and v != ""}


def passenger_gate(value, source):
    """Provider gate evidence for prediction, never a confirmed parking stand."""
    if not isinstance(value, str):
        return {}
    value = value.strip().upper()
    if not value or value in ("-", "--", "—", "NULL", "NONE", "N/A") or len(value) > 32 or any(ord(c) < 32 for c in value):
        return {}
    return {"gate": value, "gate_source": source}


def ber_schedule(row, departing):
    result = {}
    fields = {"std": ("dep_scheduled_time", "scheduled_time"), "etd": ("dep_estimated_time",)} if departing else {"sta": ("arr_scheduled_time", "scheduled_time"), "eta": ("arr_estimated_time",)}
    for key, candidates in fields.items():
        result[key] = next((timestamp(row.get(k), "Europe/Berlin") for k in candidates if timestamp(row.get(k), "Europe/Berlin")), None)
    result.update(origin=row.get("dep_airport_iata"), destination=row.get("arr_airport_iata"), registration=row.get("aircraft_reg"), aircraftType=row.get("aircraft_type"))
    return compact(result)


BERLIN = ZoneInfo("Europe/Berlin")
# Same commercial-carrier aliases used by the app. Unknown operators are not
# guessed from the numeric suffix of a callsign.
CARRIERS = dict(LH='DLH', VL='LHX', EW='EWG', OS='AUA', LX='SWR', SN='BEL', CL='CLH',
                UA='UAL', AC='ACA', NH='ANA', SQ='SIA', TG='THA', OZ='AAR', CA='CCA',
                TP='TAP', TK='THY', ET='ETH', MS='MSR', AI='AIC', BR='EVA', SA='SAA',
                NZ='ANZ', ZH='CSZ', A3='AEE', OU='CTN', LO='LOT', MH='MAS', UL='ALK',
                AV='AVA', CM='CMP', AZ='ITY', HO='DKH')


def flight_number(value):
    value = re.sub(r'\s+', '', value).upper() if isinstance(value, str) else ''
    match = re.fullmatch(r'([A-Z0-9]{2}|[A-Z]{3})([0-9]{1,5})', value)
    if not match:
        return ''
    carrier, number = match.groups()
    carrier = next((iata for iata, icao in CARRIERS.items() if icao == carrier), carrier)
    return carrier+str(int(number))


def registration_key(value):
    return re.sub(r'[^A-Z0-9]', '', value.upper()) if isinstance(value, str) else ''


def flight_day(value):
    try:
        if isinstance(value, bool):
            return ''
        return datetime.fromtimestamp(value, BERLIN).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return ''


def _object(value):
    return value if isinstance(value, dict) else {}


def _provider_id(value):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value) else None


def _hex(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r'[A-Fa-f0-9]{6}', value) else None


def _departure_day(when, zone=None, local=None):
    try:
        if isinstance(local, str):
            return datetime.fromisoformat(local.replace('Z', '+00:00')).date().isoformat()
        if when and isinstance(zone, str) and zone:
            return datetime.fromtimestamp(when, ZoneInfo(zone)).date().isoformat()
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        pass
    return None


def _occurrence_time(flight, row):
    # Pi incoming rows use STA (otherwise STD) for their reference date. A
    # transatlantic arrival's preceding-day departure belongs to the same leg.
    arriving = flight.get('destination') == 'BER' and flight.get('origin') != 'BER'
    return (row.get('sta') or row.get('std')) if arriving else row.get('std')


def _matches(flight, row, strict=False):
    """Schedules may be partial; an actual gate event needs a complete binding."""
    numbers = row.get('_numbers') or []
    expected = flight_number(flight.get('number'))
    if (numbers and expected not in numbers) or (strict and (not expected or expected not in numbers)):
        return False
    day, when = flight_day(flight.get('reference')), _occurrence_time(flight, row)
    if (when and day != flight_day(when)) or (strict and (not day or not when)):
        return False
    for field in ('origin', 'destination'):
        expected, actual = flight.get(field), row.get(field)
        if (expected and actual and str(expected).upper() != str(actual).upper()) or (strict and not actual):
            return False
    expected, actual = registration_key(flight.get('registration')), registration_key(row.get('registration'))
    if (expected and actual and expected != actual) or (strict and expected and not actual):
        return False
    if any(flight.get(key) and row.get(key) and str(flight[key]) != str(row[key])
           for key in ('flightawareID', 'flightstatsID')):
        return False
    return True


def _select(rows, flight, reference):
    rows = [row for row in rows if not flight or _matches(flight, row)]
    if not rows:
        return {}
    strict = [row for row in rows if flight and _matches(flight, row, strict=True)]
    # A flight number can identify several legs. No nearest-row guess may turn
    # ambiguous provider data into a confirmed AIBT or a track identity.
    identity = lambda row: (row.get('flightawareID'), row.get('flightstatsID'), row.get('std'),
                            row.get('origin'), row.get('destination'), registration_key(row.get('registration')))
    confirmed = bool(strict and len({identity(row) for row in strict}) == 1)
    chosen = strict if confirmed else rows
    result = dict(min(chosen, key=lambda row: (abs((_occurrence_time(flight or {}, row) or reference)-reference),
                                              not bool(row.get('aibt')), -(row.get('aibt') or 0))))
    result.pop('_numbers', None)
    if flight and not confirmed:
        for key in ('aibt', 'aibt_source', 'flightawareID', 'flightstatsID', 'icao24'):
            result.pop(key, None)
    return compact(result)


def _valid_actuals(result, flight, now):
    for key in ('atd', 'ata', 'aibt'):
        if key in result and not flight['reference']-86400 <= result[key] <= now+60:
            result.pop(key)
    aibt = result.get('aibt')
    if aibt and (result.get('cancelled') or result.get('diverted')
                 or aibt < max(flight.get('ata') or 0, result.get('ata') or 0, result.get('std') or 0)):
        result.pop('aibt')
    if 'aibt' not in result:
        result.pop('aibt_source', None)
    return result


def _stats_numbers(row, official):
    numbers = [flight_number(row.get(key)) for key in ('flight', 'flightNumber', 'number')]
    carrier = _object(row.get('carrier')) or _object(row.get('airline'))
    codes = [row.get('carrierFsCode')] if official else [carrier.get(k) for k in ('iata', 'fs', 'icao')]
    digits = str(row.get('flightNumber') or '')
    if re.fullmatch(r'[0-9]{1,5}', digits):
        numbers.extend(flight_number(str(code)+digits) for code in codes if code)
    return list({number for number in numbers if number})


def _stats_row(row, official, departing):
    if official:
        def value(key):
            return timestamp(_object(_object(row.get('operationalTimes')).get(key)).get('dateUtc'))
        result = {k:value(v) for k,v in {"std":"scheduledGateDeparture", "etd":"estimatedGateDeparture", "atd":"actualRunwayDeparture", "sta":"scheduledGateArrival", "eta":"estimatedRunwayArrival", "ata":"actualRunwayArrival", "eibt":"estimatedGateArrival", "aibt":"actualGateArrival"}.items()}
        result["eta"] = result["eta"] or result["eibt"]
        equipment = _object(row.get("flightEquipment"))
        result.update(passenger_gate(_object(row.get("airportResources")).get("departureGate" if departing else "arrivalGate"), "FlightStats"))
        result.update(origin=row.get("departureAirportFsCode"), destination=row.get("arrivalAirportFsCode"), registration=equipment.get("tailNumber"), aircraftType=equipment.get("actualEquipmentIataCode") or equipment.get("scheduledEquipmentIataCode"), cancelled=row.get("status")=="C", diverted=row.get("status")=="D")
    else:
        schedule = _object(row.get("schedule"))
        departure, arrival = _object(row.get("departureAirport")), _object(row.get("arrivalAirport"))
        def value(key, airport):
            return timestamp(schedule.get(key+"UTC")) or timestamp(schedule.get(key), airport.get("timeZoneRegionName") or "UTC")
        result = {k:value(v, departure if k in ("std","etd","atd") else arrival) for k,v in {"std":"scheduledDeparture", "etd":"estimatedActualDeparture", "atd":"actualRunwayDeparture", "sta":"scheduledArrival", "eta":"estimatedActualArrival", "ata":"actualRunwayArrival", "eibt":"estimatedGateArrival", "aibt":"actualGateArrival"}.items()}
        note = _object(row.get("flightNote"))
        if result["atd"] is None and note.get("hasActualRunwayDepartureTime") is True:
            result["atd"] = result["etd"]
        if result["ata"] is None and note.get("hasActualRunwayArrivalTime") is True:
            result["ata"] = result["eta"]
        # The website combines an estimate and an actual time in one field. It
        # proves in-block only when the explicit gate-arrival actual flag is set.
        if result['aibt'] is None and note.get('hasActualGateArrivalTime') is True:
            result['aibt'] = value('estimatedActualArrival', arrival)
        equipment, status = _object(row.get("equipment")), _object(row.get("status"))
        result.update(passenger_gate((departure if departing else arrival).get("gate"), "FlightStats"))
        result.update(origin=departure.get("iata") or departure.get("fs"), destination=arrival.get("iata") or arrival.get("fs"), registration=equipment.get("tailNumber"), aircraftType=equipment.get("iata"), cancelled=status.get("statusCode")=="C" or "cancel" in str(status.get("status", "")).lower(), diverted=status.get("statusCode")=="D" or "divert" in str(status.get("status", "")).lower())
        result['_departure_day'] = _departure_day(result.get('std'), departure.get('timeZoneRegionName'), schedule.get('scheduledDeparture'))
    result.update(_numbers=_stats_numbers(row, official), flightstatsID=_provider_id(row.get('flightId')),
                  icao24=_hex(equipment.get('icao24') or equipment.get('hex')))
    if result.get('aibt'):
        result['aibt_source'] = 'FlightStats'
    return compact(result)


def flightstats(root, official, reference, departing=False, flight=None):
    if not isinstance(root, dict):
        return {}
    rows = root.get('flightStatuses') if official else [root.get('data', root)]
    if not isinstance(rows, list):
        return {}
    return _select([_stats_row(row, official, departing) for row in rows if isinstance(row, dict)], flight, reference)


async def schedule_lookup(session, flight, credentials, cooldown, now):
    from .comatalarm import provider_http_status
    if cooldown.get("flightstats", 0) > now:
        return {}
    parts = re.fullmatch(r"([A-Z][A-Z0-9]{1,2}?)([0-9]{1,5})", flight["number"])
    if not parts:
        return {}
    carrier, digits = parts.groups()
    day = datetime.fromtimestamp(flight["reference"], ZoneInfo("Europe/Berlin"))
    official = bool(credentials.get("FLIGHTSTATS_APP_ID") and credentials.get("FLIGHTSTATS_APP_KEY"))
    arriving = flight.get('destination') == 'BER' and flight.get('origin') != 'BER'
    if not official and arriving:
        # The existing public website route takes the departure date. A known
        # schedule supplies it without guessing an additional preceding-day
        # request. Occurrence validation still uses the incoming STA date.
        departure_day = _object(flight.get('_schedule')).get('_departure_day')
        if isinstance(departure_day, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', departure_day):
            try:
                day = datetime.fromisoformat(departure_day)
            except ValueError:
                pass
    suffix = f"{carrier}/{digits}/{day.year}/{day.month}/{day.day}"
    if official:
        direction = 'arr' if arriving else 'dep'
        url = f"https://api.flightstats.com/flex/flightstatus/rest/v2/json/flight/status/{carrier}/{digits}/{direction}/{day.year}/{day.month}/{day.day}"
        params = {"appId":credentials["FLIGHTSTATS_APP_ID"], "appKey":credentials["FLIGHTSTATS_APP_KEY"], "utc":"false"}
        if arriving:
            params['airport'] = 'BER'
    else:
        url, params = "https://www.flightstats.com/v2/api-next/flight-tracker/"+suffix, None
    async with session.get(url, params=params, headers={"Accept":"application/json", "Referer":"https://www.flightstats.com/", "User-Agent":"ComatAlarm/1.0"}, allow_redirects=False) as response:
        provider_http_status("FlightStats", response, cooldown, "flightstats", now)
        if response.status != 200:
            return {}
        result = flightstats(await response.json(), official, flight["reference"], flight.get("origin") == "BER", flight=flight)
    if any(flight.get(k) and result.get(k) and flight[k] != result[k] for k in ("origin","destination")):
        return {}
    return _valid_actuals(result, flight, now)


async def aware_lookup(session, flight, credentials, cooldown, now):
    from .comatalarm import provider_http_status, utc_query_time
    key = credentials.get("FLIGHTAWARE_API_KEY")
    if not key or cooldown.get("flightaware", 0) > now:
        return {}
    iso = lambda t: utc_query_time(t, "Z")
    params = {"start":iso(flight["reference"]-86400), "end":iso(flight["reference"]+86400)}
    async with session.get("https://aeroapi.flightaware.com/aeroapi/flights/"+flight["number"], params=params, headers={"x-apikey":key}, allow_redirects=False) as response:
        provider_http_status("FlightAware", response, cooldown, "flightaware", now)
        if response.status != 200:
            return {}
        rows = _object(await response.json()).get("flights") or []
    parsed = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        result = {k:timestamp(row.get(v)) for k,v in {"std":"scheduled_out","etd":"estimated_out","atd":"actual_off","sta":"scheduled_in","eta":"estimated_on","ata":"actual_on","eibt":"estimated_in", "aibt":"actual_in"}.items()}
        result['std'] = result['std'] or timestamp(row.get('scheduled_off'))
        if not _occurrence_time(flight, result):
            continue
        result["eta"] = result["eta"] or result["eibt"]
        result.update(passenger_gate(row.get("gate_origin" if flight.get("origin") == "BER" else "gate_destination"), "FlightAware"))
        numbers = [row.get(key) for key in ('ident', 'ident_iata', 'ident_icao')]
        numbers += row.get('codeshares_iata') if isinstance(row.get('codeshares_iata'), list) else []
        numbers += row.get('codeshares') if isinstance(row.get('codeshares'), list) else []
        result.update(_numbers=list({flight_number(number) for number in numbers if flight_number(number)}),
                      origin=_object(row.get('origin')).get('code_iata'), destination=_object(row.get('destination')).get('code_iata'),
                      registration=row.get("registration"), aircraftType=row.get("aircraft_type"),
                      cancelled=row.get("cancelled") is True, diverted=row.get("diverted") is True,
                      flightawareID=_provider_id(row.get('fa_flight_id')), icao24=_hex(row.get('icao24') or row.get('hex')))
        result['_departure_day'] = _departure_day(result.get('std'), _object(row.get('origin')).get('timezone'))
        if result.get('aibt'):
            result['aibt_source'] = 'FlightAware'
        parsed.append(compact(result))
    return _valid_actuals(_select(parsed, flight, flight['reference']), flight, now)
