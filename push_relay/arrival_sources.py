"""Bounded non-FR24 evidence for one arriving flight occurrence.

An actual provider in-block time and an independently observed aircraft
position remain separate facts. Only a stationary ground sample within sixty
seconds of that actual time becomes aibt_position. The BER stand index is the
only authority for a stand; this module neither reads nor assigns gate codes.
"""
import asyncio
from collections import deque
from copy import deepcopy
import hashlib
import json
import math
import re
import time
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout

from . import flight_schedule

MAX_SAMPLES = 64
MAX_BODY = 512 * 1024
MAX_FLIGHTS = 256
MAX_REQUESTS_PER_MINUTE = 8
DAILY_LIMITS = {'flightaware': 240, 'flightstats': 480, 'adsb': 400, 'opensky': 320}
REQUEST_TIMEOUT = 4
LOOKUP_TIMEOUT = 12
POSITION_WINDOW = 60
LIVE_SAMPLE_MAX_AGE = 120
POSITION_FALLBACK_DELAY = 0.1
LIVE_WINDOW = 25 * 60
HISTORY_WINDOW = 24 * 3600
CREDENTIAL_KEYS = ('FLIGHTAWARE_API_KEY', 'FLIGHTSTATS_APP_ID', 'FLIGHTSTATS_APP_KEY',
                   'OPENSKY_CLIENT_ID', 'OPENSKY_CLIENT_SECRET')


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _stamp(value):
    number = _number(value)
    if number is not None:
        return number if number > 1_000_000_000 else None
    return flight_schedule.timestamp(value)


def _hex(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{6}', value) else None


def _airframe(flight):
    live = flight.get('_live') if isinstance(flight.get('_live'), dict) else {}
    return _hex(flight.get('icao24') or flight.get('hex') or live.get('hex') or live.get('icao24'))


def _leg_id(flight):
    value = flight.get('fr24ID')
    return (value if isinstance(value, str) and 0 < len(value) <= 160
            and not value.upper().startswith(('TEMP_', 'NS_', 'LANDED_')) else '')


def _identity(flight):
    return dict(number=flight_schedule.flight_number(flight.get('number')),
                reference_day=flight_schedule.flight_day(flight.get('reference')),
                origin=str(flight.get('origin') or '').upper(),
                destination=str(flight.get('destination') or '').upper(),
                registration=flight_schedule.registration_key(flight.get('registration')), fr24_id=_leg_id(flight))


def _credential_key(credentials):
    values = {key: credentials.get(key, '') for key in CREDENTIAL_KEYS}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _coordinate(latitude, longitude):
    # Only BER positions are relevant to this local parking resolver.
    lat, lon = _number(latitude), _number(longitude)
    return lat is not None and lon is not None and 52.3 < lat < 52.45 and 13.4 < lon < 13.65


def _stationary(ground, speed):
    speed = _number(speed)
    return ground is True and speed is not None and 0 <= speed <= 5


def _sample(flight, latitude, longitude, observed, ground, speed, source, now, *, hex=None, registration=None, callsign=None):
    observed = _stamp(observed)
    if not observed or not now-HISTORY_WINDOW <= observed <= now+5 or not _coordinate(latitude, longitude):
        return None
    if not _stationary(ground, speed):
        return None
    known_hex, sample_hex = _airframe(flight), _hex(hex)
    expected_reg = flight_schedule.registration_key(flight.get('registration'))
    sample_reg = flight_schedule.registration_key(registration)
    if known_hex and sample_hex and known_hex != sample_hex:
        return None
    if expected_reg and sample_reg and expected_reg != sample_reg:
        return None
    live = flight.get('_live') if isinstance(flight.get('_live'), dict) else {}
    known_call = flight.get('callsign') or live.get('callsign')
    known_call = re.sub(r'\s+', '', known_call).upper() if isinstance(known_call, str) else ''
    observed_call = re.sub(r'\s+', '', callsign).upper() if isinstance(callsign, str) else ''
    if known_call and observed_call and known_call != observed_call:
        return None
    # Commercial flight numbers and radio callsigns are not interchangeable:
    # LH194 may fly as DLH6HF. Without a known radio alias, the exact airframe
    # plus occurrence/time checks establish the binding; an explicit different
    # numeric commercial flight remains contradictory evidence.
    numeric_call = flight_schedule.flight_number(observed_call)
    if not known_call and numeric_call and numeric_call != flight_schedule.flight_number(flight.get('number')):
        return None
    ata = _stamp(flight.get('ata'))
    if ata and observed < ata:
        return None
    return dict(latitude=latitude, longitude=longitude, timestamp=observed, source=source,
                on_ground=True, groundspeed=float(speed), icao24=sample_hex or known_hex,
                callsign=(observed_call or known_call)[:16],
                **_identity(dict(flight, registration=registration or flight.get('registration'))))


def _restore_samples(flight, now):
    expected = _identity(flight)
    rows = flight.get('_arrival_position_samples')
    if not isinstance(rows, list):
        return []
    samples = []
    for row in rows[-MAX_SAMPLES:]:
        if not isinstance(row, dict) or row.get('source') not in ('adsb_lol', 'opensky', 'flightaware_track'):
            continue
        if any(value and row.get(key) != value for key, value in expected.items() if key != 'fr24_id'):
            continue
        if expected['fr24_id'] and row.get('fr24_id') and expected['fr24_id'] != row['fr24_id']:
            continue
        sample = _sample(flight, row.get('latitude'), row.get('longitude'), row.get('timestamp'),
                         row.get('on_ground'), row.get('groundspeed'), row['source'], now,
                         hex=row.get('icao24'), registration=row.get('registration'), callsign=row.get('callsign'))
        if sample:
            samples.append(sample)
    return samples


def select_position(samples, flight):
    aibt, ata = _stamp(flight.get('aibt')), _stamp(flight.get('ata'))
    if not aibt:
        return None
    matches = [sample for sample in samples if abs(sample['timestamp']-aibt) <= POSITION_WINDOW
               and (not ata or sample['timestamp'] >= ata)]
    if not matches:
        return None
    point = min(matches, key=lambda sample: (abs(sample['timestamp']-aibt), -sample['timestamp']))
    return dict({key: point[key] for key in ('latitude', 'longitude', 'timestamp', 'source')},
                on_ground=True, ground_speed=point['groundspeed'])


def adsb_samples(root, flight, now):
    """ADSB.lol: `now` is seconds (some compatible replies use milliseconds)."""
    if not isinstance(root, dict) or not isinstance(root.get('ac'), list):
        return []
    received = _number(root.get('now'))
    if received is None:
        return []  # Retrieval time cannot substitute for the provider's clock.
    received = received/1000 if received > 10_000_000_000 else received
    samples = []
    known_hex, registration = _airframe(flight), flight_schedule.registration_key(flight.get('registration'))
    if not known_hex and not registration:
        return []
    for row in root['ac']:
        if not isinstance(row, dict):
            continue
        if known_hex and _hex(row.get('hex')) != known_hex:
            continue
        if not known_hex and flight_schedule.registration_key(row.get('r')) != registration:
            continue
        age = _number(row.get('seen_pos'))
        speed = _number(row.get('gs'))
        if age is None or not 0 <= age <= LIVE_SAMPLE_MAX_AGE or speed is None:
            continue
        observed = received-age
        if not now-LIVE_SAMPLE_MAX_AGE <= observed <= now+5:
            continue
        point = _sample(flight, row.get('lat'), row.get('lon'), observed,
                        row.get('alt_baro') == 'ground' or row.get('ground') is True,
                        speed*1.852, 'adsb_lol', now, hex=row.get('hex'),
                        registration=row.get('r'), callsign=row.get('flight'))
        if point:
            samples.append(point)
    return samples


def opensky_samples(root, flight, now, requested_at):
    """Always use time_position, never last_contact or the response fetch time."""
    if not isinstance(root, dict) or not isinstance(root.get('states'), list) or not _airframe(flight):
        return []
    samples = []
    for row in root['states']:
        if not isinstance(row, list) or len(row) < 12 or _hex(row[0]) != _airframe(flight):
            continue
        observed, contact, speed = _stamp(row[3]), _stamp(row[4]), _number(row[9])
        if not observed or not contact or speed is None or not requested_at-120 <= min(observed, contact) <= requested_at+5:
            continue
        # A server that ignored `time` must not turn today's aircraft into a
        # historical gate coordinate, even when its airframe is the same.
        if observed > requested_at+5:
            continue
        point = _sample(flight, row[6], row[5], observed, row[8], speed*3.6,
                        'opensky', now, hex=row[0], callsign=row[1])
        if point:
            samples.append(point)
    return samples


def aware_track_samples(root, flight, now, identifier):
    if not isinstance(root, dict) or not isinstance(root.get('positions'), list):
        return []
    samples, aibt = [], _stamp(flight.get('aibt'))
    for row in root['positions']:
        if not isinstance(row, dict) or row.get('fa_flight_id') not in (None, identifier):
            continue
        speed, altitude = _number(row.get('groundspeed')), _number(row.get('altitude'))
        # AeroAPI X means surface *or near surface*. Require a stationary
        # surface-level observation as well (altitude unit: hundreds of feet).
        if row.get('update_type') != 'X' or speed is None or altitude is None or not 0 <= altitude <= 2:
            continue
        point = _sample(flight, row.get('latitude'), row.get('longitude'), row.get('timestamp'),
                        True, speed*1.852, 'flightaware_track', now)
        if point and (not aibt or abs(point['timestamp']-aibt) <= POSITION_WINDOW):
            samples.append(point)
    return samples


class _LimitedResponse:
    def __init__(self, response):
        self.response, self.status = response, response.status
        self.headers = response.headers

    async def json(self):
        content = getattr(self.response, 'content', None)
        if content is not None and hasattr(content, 'iter_chunked'):
            data = bytearray()
            async for chunk in content.iter_chunked(16*1024):
                if len(data)+len(chunk) > MAX_BODY:
                    raise ValueError('provider response size')
                data.extend(chunk)
            return json.loads(data)
        # Small fake responses used by deterministic tests have no stream.
        value = await self.response.json()
        if len(json.dumps(value).encode()) > MAX_BODY:
            raise ValueError('provider response size')
        return value


class _LimitedContext:
    def __init__(self, context):
        self.context = context

    async def __aenter__(self):
        return _LimitedResponse(await self.context.__aenter__())

    async def __aexit__(self, *args):
        return await self.context.__aexit__(*args)


class _LimitedSession:
    def __init__(self, session):
        self.session = session

    def get(self, url, **kwargs):
        kwargs.update(allow_redirects=False, timeout=ClientTimeout(total=REQUEST_TIMEOUT))
        return _LimitedContext(self.session.get(url, **kwargs))

    def post(self, url, **kwargs):
        kwargs.update(allow_redirects=False, timeout=ClientTimeout(total=REQUEST_TIMEOUT))
        return _LimitedContext(self.session.post(url, **kwargs))


class ArrivalSources:
    """One instance per FlightProvider, shared by every watch of the same leg."""
    def __init__(self, provider, *, aware_lookup=None, schedule_lookup=None):
        self.provider = provider
        self.aware_lookup = aware_lookup or flight_schedule.aware_lookup
        self.schedule_lookup = schedule_lookup or flight_schedule.schedule_lookup
        self.cache, self.tasks, self.history, self.requests, self.tokens = {}, {}, {}, {}, {}
        self.closed = False

    def _key(self, flight, credentials):
        identity = _identity(flight)
        token_digest = flight.get('_arrival_token_digest')
        token_digest = token_digest if isinstance(token_digest, str) and len(token_digest) <= 64 else ''
        generation = _stamp(flight.get('_arrival_generation')) or 0
        return (_credential_key(credentials), *identity.values(), _airframe(flight), token_digest, generation)

    def _reserve(self, provider, credentials, now):
        key = (provider, _credential_key(credentials))
        requests = self.requests.setdefault(key, deque())
        while requests and requests[0] <= now-86400:
            requests.popleft()
        if (self.provider.cooldown.get(provider, 0) > now
                or sum(when > now-60 for when in requests) >= MAX_REQUESTS_PER_MINUTE
                or len(requests) >= DAILY_LIMITS.get(provider, 120)):
            return False
        requests.append(now)
        return True

    async def _cached(self, provider, key, credentials, now, ttl, operation):
        key = (provider, key)
        cached = self.cache.get(key)
        if cached and 0 <= now-cached[0] < cached[1]:
            return deepcopy(cached[2])
        if not self._reserve(provider, credentials, now):
            return None
        try:
            result = await asyncio.wait_for(operation(), REQUEST_TIMEOUT)
        except (ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError, OverflowError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        self.cache[key] = (now, HISTORY_WINDOW if result.get('aibt') else ttl, deepcopy(result))
        # Memory is bounded even when the dashboard is used for many flights.
        self.cache = {key: value for key, value in self.cache.items() if now-value[0] < HISTORY_WINDOW}
        while len(self.cache) > MAX_FLIGHTS*4:
            self.cache.pop(next(iter(self.cache)))
        return result

    async def _get(self, session, url, provider, now, *, params=None, headers=None):
        from .comatalarm import provider_http_status
        async with session.get(url, params=params, headers=headers or {'Accept': 'application/json'}) as response:
            provider_http_status(provider, response, self.provider.cooldown, provider, now)
            if response.status != 200:
                return {}
            root = await response.json()
            return root if isinstance(root, dict) else {}

    async def _access_token(self, session, credentials, now):
        identifier, secret = credentials.get('OPENSKY_CLIENT_ID'), credentials.get('OPENSKY_CLIENT_SECRET')
        if not identifier or not secret:
            return None
        key = _credential_key(credentials)
        cached = self.tokens.get(key)
        if cached and cached[0] > now:
            return cached[1]
        if not self._reserve('opensky', credentials, now):
            return None
        from .comatalarm import provider_http_status
        try:
            async with session.post('https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token',
                                    data={'grant_type': 'client_credentials', 'client_id': identifier, 'client_secret': secret}) as response:
                provider_http_status('OpenSky', response, self.provider.cooldown, 'opensky', now)
                if response.status != 200:
                    return None
                root = await response.json()
                value = root.get('access_token') if isinstance(root, dict) else None
                expiry = _number(root.get('expires_in')) if isinstance(root, dict) else None
                if not isinstance(value, str) or not value or len(value) > 16384 or expiry is None or expiry <= 30:
                    return None
                self.tokens[key] = (now+min(expiry-30, 1800), value)
                self.tokens = {key: value for key, value in self.tokens.items() if value[0] > now}
                return value
        except (ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
            return None

    def _remember(self, flight, key, samples, now):
        merged = (_restore_samples(flight, now)
                  + _restore_samples(dict(flight, _arrival_position_samples=self.history.get(key, [])), now)
                  + _restore_samples(dict(flight, _arrival_position_samples=samples), now))
        # Keeping one actual sample per30s retains a small, useful taxi-in
        # window without allowing persistence to grow on every worker pass.
        buckets, aibt = {}, _stamp(flight.get('aibt'))
        for point in sorted(merged, key=lambda row: row['timestamp']):
            if now-HISTORY_WINDOW <= point['timestamp'] <= now+5:
                bucket = int(point['timestamp']//30)
                previous = buckets.get(bucket)
                if not previous or not aibt or abs(point['timestamp']-aibt) < abs(previous['timestamp']-aibt):
                    buckets[bucket] = point
        saved = list(buckets.values())[-MAX_SAMPLES:]
        self.history[key] = deepcopy(saved)
        self.history = {key: value for key, value in self.history.items() if value and value[-1]['timestamp'] >= now-HISTORY_WINDOW}
        while len(self.history) > MAX_FLIGHTS:
            self.history.pop(next(iter(self.history)))
        return saved

    async def lookup(self, session, flight, now, credentials=None):
        credentials = dict(self.provider.credentials if credentials is None else credentials)
        flight = deepcopy(flight)
        identity = _identity(flight)
        reference = _stamp(flight.get('reference'))
        if (self.closed or not identity['number'] or not reference or not identity['reference_day']
                or identity['destination'] != 'BER' or identity['origin'] == 'BER'
                or flight.get('cancelled') or flight.get('diverted')
                or not -86400 <= now-reference <= 2*86400):
            return {'_arrival_sources_status': 'ineligible', '_arrival_sources_checked_at': now}
        key = self._key(flight, credentials)
        task_key = (*key, _stamp(flight.get('aibt')), flight.get('flightawareID'))
        if task_key in self.tasks:
            return deepcopy(await asyncio.shield(self.tasks[task_key]))
        if len(self.tasks) >= 2:
            return {'_arrival_sources_status': 'paused', '_arrival_sources_checked_at': now}
        task = asyncio.create_task(self._run(_LimitedSession(session), flight, credentials, key, now))
        self.tasks[task_key] = task
        try:
            return deepcopy(await task)
        finally:
            if self.tasks.get(task_key) is task:
                self.tasks.pop(task_key, None)

    async def _run(self, session, flight, credentials, key, now):
        started = time.monotonic()
        observed_now = lambda: now+time.monotonic()-started
        result = {'_arrival_sources_checked_at': now}
        existing = _stamp(flight.get('aibt'))
        if existing:
            result['aibt'] = existing
            if flight.get('aibt_source'):
                result['aibt_source'] = flight['aibt_source']
        for field in ('flightawareID', 'flightstatsID', 'icao24'):
            if flight.get(field):
                result[field] = flight[field]
        paused = False
        samples = self._remember(flight, key, [], now)
        # The caller may already hold a validated event/position pair. A second
        # source must never replace it or spend requests trying to recreate it.
        if existing:
            from .aibt_recovery import position
            confirmed = position(flight)
            if confirmed:
                result['aibt_position'] = deepcopy(confirmed)
                result['_arrival_sources_status'] = 'complete'
                if samples:
                    result['_arrival_position_samples'] = samples
                return result
        jobs = {}
        has_oauth = bool(credentials.get('OPENSKY_CLIENT_ID') and credentials.get('OPENSKY_CLIENT_SECRET'))

        def resolved():
            return dict(flight, **result)

        def live_eligible(snapshot):
            aibt, ata = _stamp(snapshot.get('aibt')), _stamp(snapshot.get('ata'))
            return bool((aibt or ata) and 0 <= now-(aibt or ata) < LIVE_WINDOW
                        and (not aibt or now-aibt <= POSITION_WINDOW+LIVE_SAMPLE_MAX_AGE))

        def remember(observations):
            nonlocal samples
            # Persist each completed observation immediately. A subsequent
            # status-source timeout must not discard this original GPS time.
            samples = self._remember(resolved(), key, samples+observations, observed_now())

        async def actuals():
            nonlocal paused
            ttl = 15 if (_stamp(flight.get('ata')) and 0 <= now-_stamp(flight['ata']) < LIVE_WINDOW) else 300
            if credentials.get('FLIGHTAWARE_API_KEY') and (not existing or not result.get('flightawareID')):
                aware = await self._cached('flightaware', key, credentials, now, ttl,
                    lambda: self.aware_lookup(session, flight, credentials, self.provider.cooldown, now))
                paused |= aware is None
                self._merge_actual(result, flight, aware, now)
            if not result.get('aibt'):
                stats = await self._cached('flightstats', key, credentials, now, ttl,
                    lambda: self.schedule_lookup(session, flight, credentials, self.provider.cooldown, now))
                paused |= stats is None
                self._merge_actual(result, flight, stats, now)

        async def observe_adsb(snapshot, kind, identity):
            nonlocal paused
            if samples and not existing and not jobs['actuals'].done():
                # A saved point may already solve the arriving actual time.
                # Allow an immediately cached status to finish, without giving
                # a slow status request control of fresh position collection.
                await asyncio.wait({jobs['actuals']}, timeout=POSITION_FALLBACK_DELAY)
            if select_position(samples, resolved()) or not live_eligible(resolved()):
                return
            root = await self._cached('adsb', (key, kind, identity), credentials, now, 15,
                lambda: self._get(session, 'https://api.adsb.lol/v2/'+kind+'/'+quote(identity, safe=''), 'adsb', now))
            paused |= root is None
            observed = adsb_samples(root, snapshot, observed_now())
            if observed and observed[0].get('icao24'):
                result['icao24'] = observed[0]['icao24']
            remember(observed)

        async def observe_opensky(snapshot, historical, aibt, adsb_job):
            nonlocal paused
            if adsb_job is not None and not adsb_job.done():
                # A fast ADSB result avoids a redundant request. A slow result
                # cannot spend OpenSky's own authentication/request window.
                await asyncio.wait({adsb_job}, timeout=POSITION_FALLBACK_DELAY)
            current_flight = resolved()
            if select_position(samples, current_flight):
                return
            if not historical and not live_eligible(current_flight):
                return
            if not _stamp(current_flight.get('aibt')) and any(
                    observed_now()-POSITION_WINDOW <= point['timestamp'] <= observed_now()+5 for point in samples):
                return  # A fresh ground sample is already retained for the pending actual time.
            access = await self._access_token(session, credentials, now)
            if has_oauth and not access:
                paused = True
                return
            target = aibt if historical else observed_now()
            params = {'icao24': _airframe(snapshot), 'lamin': '52.30', 'lomin': '13.40', 'lamax': '52.45', 'lomax': '13.65'}
            if historical:
                params['time'] = int(aibt)
            headers = {'Accept': 'application/json'}
            if access:
                headers['Authorization'] = 'Bearer '+access
            root = await self._cached('opensky', (key, int(aibt) if historical else 'live'), credentials, now,
                300 if historical else 15,
                lambda: self._get(session, 'https://opensky-network.org/api/states/all', 'opensky', now, params=params, headers=headers))
            paused |= root is None
            current = observed_now()
            remember(opensky_samples(root, snapshot, current, target if historical else current))

        async def observe_track(snapshot, aibt, identifier):
            nonlocal paused
            track = await self._cached('flightaware', (key, identifier, 'track', aibt), credentials, now, 300,
                lambda: self._get(session, 'https://aeroapi.flightaware.com/aeroapi/flights/'+quote(identifier, safe='')+'/track',
                    'flightaware', now, params={'include_estimated_positions': 'false'},
                    headers={'x-apikey': credentials['FLIGHTAWARE_API_KEY']}))
            paused |= track is None
            remember(aware_track_samples(track, snapshot, observed_now(), identifier))

        def start_positions():
            snapshot = resolved()
            if select_position(samples, snapshot):
                return
            aibt, ata = _stamp(snapshot.get('aibt')), _stamp(snapshot.get('ata'))
            # The observation's timestamp determines whether it belongs to
            # AIBT. A response fetched >60s later may still carry that exact
            # position within the provider's bounded seen_pos allowance.
            live = live_eligible(snapshot)
            known_hex = _airframe(snapshot)
            registration = flight_schedule.registration_key(snapshot.get('registration'))
            if live and (known_hex or registration) and 'adsb' not in jobs:
                kind, identity = ('hex', known_hex) if known_hex else ('reg', registration)
                jobs['adsb'] = asyncio.create_task(observe_adsb(snapshot, kind, identity))
            # Only authenticated OpenSky supports historical requests. An
            # anonymous response always retains its real current position time.
            historical = bool(has_oauth and aibt and POSITION_WINDOW < now-aibt <= 3600)
            mode = ('opensky', int(aibt) if historical else 'live')
            if known_hex and (live or historical) and mode not in jobs:
                jobs[mode] = asyncio.create_task(observe_opensky(snapshot, historical, aibt, jobs.get('adsb')))

        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT):
                jobs['actuals'] = asyncio.create_task(actuals())
                start_positions()
                while True:
                    snapshot = resolved()
                    if select_position(samples, snapshot):
                        break
                    pending = [task for task in jobs.values() if not task.done()]
                    if not pending:
                        aibt = _stamp(snapshot.get('aibt'))
                        identifier = flight_schedule._provider_id(snapshot.get('flightawareID'))
                        if ('track' not in jobs and aibt and identifier and credentials.get('FLIGHTAWARE_API_KEY')
                                and 0 <= now-aibt <= HISTORY_WINDOW):
                            jobs['track'] = asyncio.create_task(observe_track(snapshot, aibt, identifier))
                            pending = [jobs['track']]
                        else:
                            break
                    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    start_positions()
        except asyncio.TimeoutError:
            paused = True
        finally:
            for task in jobs.values():
                if not task.done():
                    task.cancel()
            if jobs:
                await asyncio.gather(*jobs.values(), return_exceptions=True)
        remember([])
        point = select_position(samples, resolved())
        if point:
            result['aibt_position'] = point
        if samples:
            result['_arrival_position_samples'] = samples
        paused |= any(self.provider.cooldown.get(name, 0) > now for name in ('flightaware', 'flightstats', 'adsb', 'opensky'))
        result['_arrival_sources_status'] = ('complete' if result.get('aibt_position') else
            'aibt_only' if result.get('aibt') else 'paused' if paused else 'no_aibt')
        return result

    @staticmethod
    def _merge_actual(result, flight, supplemental, now):
        if not isinstance(supplemental, dict):
            return
        for field in ('flightawareID', 'flightstatsID', 'icao24', 'registration', 'origin', 'destination'):
            if supplemental.get(field) and not (result.get(field) or flight.get(field)):
                result[field] = supplemental[field]
        aibt = _stamp(supplemental.get('aibt'))
        if (not result.get('aibt') and aibt and supplemental.get('aibt_source') in ('FlightAware', 'FlightStats')
                and flight['reference']-86400 <= aibt <= now+60
                and (not _stamp(flight.get('ata')) or aibt >= _stamp(flight['ata']))):
            result.update(aibt=aibt, aibt_source=supplemental['aibt_source'])

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
