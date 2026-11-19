"""Lazy FR24 request coalescing for currently authorized, due subscriptions.

Planning performs no I/O. Each batch replaces requests the current polling pass
already needs. The provider's existing request budget, backoff and strict
per-occurrence response parsers remain authoritative.
"""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def planned_queries(flight, now):
    """Mirror central poll eligibility before offering summary/live requests."""
    from .comatalarm import arrival_tracking_expired, departure_from_ber, stamp, utc_query_time
    from .aibt_recovery import due, position
    departing = departure_from_ber(flight)
    if (arrival_tracking_expired(flight, now) or flight.get('cancelled') or flight.get('diverted')
            or (departing and stamp(flight.get('atd')) and flight['atd'] <= now)):
        return []
    repairing = bool(not departing and flight.get('aibt') and not position(flight))
    outbound = bool(flight.get('track_outbound') and not flight.get('_outbound', {}).get('atd'))
    if not departing and flight.get('aibt') and not repairing and not outbound:
        return []
    recovery_due = repairing and due(flight, now)
    if repairing and not recovery_due and not outbound:
        return []
    identifier = flight.get('fr24ID')
    if identifier and identifier.startswith(('TEMP_', 'NS_')):
        identifier = None
    result = []
    if not (identifier and flight.get('ata')):
        if identifier:
            params = {'flight_ids': identifier}
        else:
            start = datetime.fromtimestamp(flight['reference'], ZoneInfo('Europe/Berlin')).replace(
                hour=0, minute=0, second=0, microsecond=0)
            if start.timestamp() >= now:
                return []  # lookup exits its FR24 block before a future-day query.
            params = {'flights': flight['number'],
                      'flight_datetime_from': utc_query_time(start.timestamp()),
                      'flight_datetime_to': utc_query_time(min((start+timedelta(days=1)).timestamp(), now))}
        result.append(('flight-summary/full', params))
    if not flight.get('aibt'):
        result.append(('live/flight-positions/full', {'flights': flight['number']}))
    return result


class FR24BatchRequests:
    def __init__(self):
        self.plans = {}
        self.cache = {}
        self.lock = asyncio.Lock()

    @staticmethod
    def key(token, path, params):
        return hashlib.sha256(token.encode()).hexdigest(), path, json.dumps(params, sort_keys=True, separators=(',', ':'))

    def plan(self, entries, now):
        """Entries are (token, endpoint, original params), never arbitrary routes."""
        groups = {}
        for token, path, params in entries:
            if not token or path not in ('flight-summary/full', 'live/flight-positions/full'):
                continue
            selector = 'flight_ids' if path == 'flight-summary/full' and 'flight_ids' in params else 'flights'
            value = params.get(selector)
            if not isinstance(value, str) or not value or ',' in value:
                continue  # Only combine the provider's existing single queries.
            key = self.key(token, path, params)
            other = {k: v for k, v in params.items() if k != selector}
            family = key[:2] + (selector, json.dumps(other, sort_keys=True, separators=(',', ':')))
            groups.setdefault(family, {})[key] = dict(params)
        self.plans = {}
        for family, entries in groups.items():
            selector = family[2]
            limit = 10 if selector == 'flight_ids' else 15
            items = sorted(entries.items(), key=lambda item: item[1][selector])
            for start in range(0, len(items), limit):
                group = items[start:start+limit]
                for key, _ in group:
                    self.plans[key] = (selector, group)
        self.cache = {key: value for key, value in self.cache.items() if 0 <= now-value[0] < 120}

    def cached(self, key, now):
        value = self.cache.get(key)
        return value if value is not None and 0 <= now-value[0] < 60 else None

    @staticmethod
    def response(cached):
        if cached[2]:
            raise RuntimeError('FR24: Sammelabfrage vorübergehend nicht verfügbar')
        return list(cached[1])

    async def request(self, token, path, params, now, fetch):
        key = self.key(token, path, params)
        cached = self.cached(key, now)
        if cached is not None:
            return self.response(cached)
        plan = self.plans.get(key)
        if plan is None:
            return await fetch(path, params)
        async with self.lock:
            cached = self.cached(key, now)
            if cached is not None:
                return self.response(cached)
            selector, group = plan
            # A newly added watch may change group membership mid-minute. Reuse
            # each original query's cache instead of charging for old members again.
            missing = [(k, p) for k, p in group if self.cached(k, now) is None]
            combined = dict(params)
            combined[selector] = ','.join(p[selector] for _, p in missing)
            try:
                rows = await fetch(path, combined)
            except asyncio.CancelledError:
                raise
            except Exception:
                for member, _ in missing:
                    self.cache[member] = (now, None, True)
                raise
            # Keep the existing provider parsers in charge of flight-ID, route,
            # day and timestamp matching. This also preserves codeshare aliases:
            # a returned operating flight number may differ from the requested one.
            # Raw batches are internal and never returned to clients directly.
            for member, _ in missing:
                self.cache[member] = (now, rows, False)
            return list(rows)
