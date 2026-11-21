"""Exact arrival evidence and a persistent, per-leg provider handoff."""
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

EVIDENCE_REVISION = 2
MAX_AGE = 24*3600
FAST_INTERVAL = 15


def exhaustion_marker(flight, now, reason='no_data'):
    """Persist only occurrence identity, never a token or provider response."""
    from .comatalarm import stamp, registration_key
    identifier = flight.get('fr24ID')
    identifier = identifier if isinstance(identifier, str) and not identifier.upper().startswith(('TEMP_', 'NS_')) else ''
    reference = stamp(flight.get('reference'))
    return dict(fr24_id=identifier or '', flight_number=flight.get('number', ''),
                reference_day=datetime.fromtimestamp(reference, ZoneInfo('Europe/Berlin')).date().isoformat() if reference else '',
                registration=registration_key(flight.get('registration')), at=now,
                reason=reason if reason in ('no_data', 'incomplete', 'error', 'unavailable', 'legacy_no_data') else 'no_data')


def valid_exhaustion(flight, marker=None):
    from .comatalarm import stamp, registration_key
    marker = flight.get('fr24_arrival_exhausted') if marker is None else marker
    if not isinstance(marker, dict) or not stamp(marker.get('at')):
        return False
    identity = exhaustion_marker(flight, marker['at'])
    if not marker.get('flight_number') or not marker.get('reference_day'):
        return False
    for key in ('fr24_id', 'flight_number', 'reference_day'):
        left, right = identity.get(key), marker.get(key, '')
        if not isinstance(right, str) or len(right) > 64 or (left and right and left.upper() != right.upper()):
            return False
    left, right = identity.get('registration'), registration_key(marker.get('registration'))
    return not (left and right and left != right)


def fr24_exhausted(flight):
    if valid_exhaustion(flight):
        return True
    # Upgrade stored empty replies without paying for that same leg again.
    from .comatalarm import stamp
    return bool(stamp(flight.get('_arrival_event_received_at')) and (
        flight.get('_arrival_event_status') == 'no_match' or
        (flight.get('_arrival_event_status') == 'accepted' and not position(flight))))


def fast_candidate(flight, now):
    """Taxi-in needs timely arrival evidence from the current source chain."""
    from .comatalarm import departure_from_ber, stamp
    identifier, ata = flight.get('fr24ID'), stamp(flight.get('ata'))
    identified = isinstance(identifier, str) and identifier and ',' not in identifier and not identifier.upper().startswith(('TEMP_', 'NS_'))
    return bool((identified or fr24_exhausted(flight))
                and ata and -60 <= now-ata < 25*60 and not position(flight)
                and not departure_from_ber(flight) and not flight.get('cancelled') and not flight.get('diverted'))


def fast_due(flight, now):
    from .comatalarm import stamp
    checked = max(stamp(flight.get('_arrival_event_checked_at')) or 0, stamp(flight.get('_arrival_sources_checked_at')) or 0)
    return fast_candidate(flight, now) and now-checked >= FAST_INTERVAL


def receipt(flight, previous, now):
    """A safe, sparse diagnostic distinguishes a valid empty reply from no query."""
    state = flight.get('_arrival_event_status')
    if (state == previous.get('_arrival_event_status')
            and now-previous.get('_arrival_event_logged_at', 0) < 300):
        return None
    message = {
        'accepted': 'FR24 gate_arrival: Antwort enthält passende AIBT',
        'no_match': 'FR24 ohne passende Ankunftsdaten · weitere Quellen übernehmen; kein weiterer FR24-Ankunftsabruf',
        'incomplete': 'FR24-AIBT übernommen · Koordinate fehlt; weitere Quellen übernehmen',
        'paused': 'FR24-Ankunftsabruf nicht verfügbar · weitere Quellen übernehmen',
        'error': 'FR24-Ankunftsabruf fehlgeschlagen · weitere Quellen übernehmen',
    }.get(state)
    if message:
        identifier = flight.get('fr24ID')
        if isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier):
            message += ' · Leg '+identifier
    return message


def position(flight):
    from .comatalarm import stamp, valid_coordinate
    point, aibt = flight.get('aibt_position'), stamp(flight.get('aibt'))
    if not isinstance(point, dict) or not aibt:
        return None
    observed, ata = stamp(point.get('timestamp')), stamp(flight.get('ata'))
    if (observed and abs(observed-aibt) <= 60 and (not ata or observed >= ata)
            and valid_coordinate(point.get('latitude'), point.get('longitude'))):
        return point
    return None


def started_at(flight):
    from .comatalarm import stamp
    # ATA is the only safe clock for a missing gate-arrival event. Neither a
    # planned/EIBT time nor a stationary position proves that event occurred.
    return stamp(flight.get('aibt')) or stamp(flight.get('ata'))


def in_window(flight, now):
    from .comatalarm import departure_from_ber, stamp
    started = started_at(flight)
    return bool(started and -60 <= now-started < MAX_AGE and not departure_from_ber(flight)
                and not flight.get('cancelled') and not flight.get('diverted'))


def revision(flight):
    value = flight.get('_aibt_evidence_revision', 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def refresh_needed(flight, positions, now):
    # A usable AIBT coordinate is the provider's complete contract. Missing or
    # ambiguous local stand mapping must not trigger more external requests.
    return False


def pending(flight, now, positions=None):
    return bool(in_window(flight, now) and (not position(flight) or refresh_needed(flight, positions, now)))


def retain_pending(flight, now):
    return bool(in_window(flight, now) and not position(flight))


def attempts(flight):
    value = flight.get('_aibt_recovery_attempts', 0)
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def next_attempt(flight):
    if fr24_exhausted(flight):
        # The historic FR24 backoff must never delay independent arrival data.
        return max(flight.get('_arrival_sources_checked_at', 0), flight.get('_aibt_recovery_at', 0)) + 60
    count = attempts(flight)
    delay = 300 if count < 6 else min(21600, 900 * 2**min(5, count-6))
    # An already exhausted pre-fix watch gets the corrected decoder promptly,
    # while respecting its last short retry interval and provider backoff.
    if revision(flight) < EVIDENCE_REVISION:
        delay = 300
    return max(flight.get('_aibt_recovery_at', 0)+delay, flight.get('_aibt_recovery_retry_at', 0))


def due(flight, now, positions=None):
    return pending(flight, now, positions) and now >= next_attempt(flight)


def attempt_update(flight, now, error='', retry_at=0):
    result = dict(_aibt_recovery_at=now, _aibt_recovery_retry_at=max(now+300, retry_at) if error else 0)
    if not error:
        result.update(_aibt_recovery_attempts=attempts(flight)+1, _aibt_evidence_revision=EVIDENCE_REVISION)
    return result


def describe(flight, positions, now, error=None):
    from .comatalarm import match_stand, stamp
    point = position(flight)
    confirmed = bool(stamp(flight.get('aibt')))
    previous = flight.get('_parking_recovery') or {}
    error = previous.get('error', '') if error is None else error
    next_at = None
    if point:
        stand = match_stand(point, positions)
        if stand:
            state, text = 'complete', 'AIBT-Koordinate übernommen · Stand '+stand
        elif positions:
            state, text = 'ambiguous', 'AIBT-Koordinate übernommen · Standplatzabgleich noch ohne eindeutige Zuordnung'
        else:
            state, text = 'index_missing', 'AIBT-Koordinate übernommen · BER-Standplatzindex noch nicht verfügbar'
        if refresh_needed(flight, positions, now):
            next_at = next_attempt(flight)
    elif not in_window(flight, now):
        state = 'expired'
        text = ('AIBT bestätigt · innerhalb von 24 Stunden keine passende Koordinate übernommen' if confirmed else
                'Landung bestätigt · innerhalb von 24 Stunden keine AIBT übernommen')
    elif error:
        state = 'paused'
        text = ('AIBT bestätigt · Koordinatenabruf pausiert: ' if confirmed else
                'Landung bestätigt · AIBT-Abfrage pausiert: ')+error
        next_at = next_attempt(flight)
    else:
        state = 'pending'
        text = (('AIBT bestätigt · weitere Quellen prüfen die Koordinate' if confirmed else
                 'Landung bestätigt · AIBT noch nicht übernommen; weitere Quellen werden geprüft') if fr24_exhausted(flight) else
                ('AIBT bestätigt · Koordinate noch nicht übernommen' if confirmed else
                 'Landung bestätigt · AIBT noch nicht übernommen; Ankunftsdaten werden geprüft'))
        next_at = next_attempt(flight)
    if next_at is not None and not confirmed and fast_candidate(flight, now):
        next_at = max(now, max(stamp(flight.get('_arrival_event_checked_at')) or 0,
                               stamp(flight.get('_arrival_sources_checked_at')) or 0)+FAST_INTERVAL)
    if next_at is not None and next_at >= (started_at(flight) or now)+MAX_AGE:
        next_at = None
    last_attempt = max(flight.get('_aibt_recovery_at', 0), flight.get('_arrival_event_checked_at', 0),
                       flight.get('_arrival_sources_checked_at', 0))
    meta = dict(state=state, last_attempt=last_attempt, next_attempt=next_at,
                attempts=attempts(flight), evidence_revision=revision(flight))
    if error and state == 'paused':
        meta['error'] = error
    result = dict(_parking_lookup_status=text, _parking_recovery=meta)
    if point and positions and match_stand(point, positions):
        result.update(stand=match_stand(point, positions), _aibt_evidence_revision=EVIDENCE_REVISION)
        meta['evidence_revision'] = EVIDENCE_REVISION
    return result


def gate_arrival(rows, identifier, ata, cutoff):
    """Select one event first, so GPS and AIBT can never come from different events."""
    from .comatalarm import stamp, valid_coordinate
    choices = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get('fr24_id', '')).upper() != str(identifier).upper():
            continue
        for event in row.get('events') or []:
            if not isinstance(event, dict) or event.get('type') != 'gate_arrival':
                continue
            when = next((stamp(event.get(k)) for k in ('timestamp', 'time', 'event_time', 'datetime')
                         if stamp(event.get(k))), None)
            if not when or when > cutoff or (ata and when < ata):
                continue
            point = None
            details = event.get('details') if isinstance(event.get('details'), dict) else {}
            locations = (event, event.get('position'), details.get('position'))
            has_aircraft_point = any(isinstance(location, dict) and
                valid_coordinate(location.get('lat', location.get('latitude')), location.get('lon', location.get('longitude')))
                for location in locations)
            speed = event.get('gspeed')
            stationary = speed is None or (isinstance(speed, (int, float)) and not isinstance(speed, bool)
                                           and math.isfinite(speed) and 0 <= speed*1.852 <= 5)
            if stationary:
                for location in locations:
                    if not isinstance(location, dict):
                        continue
                    lat, lon = location.get('lat', location.get('latitude')), location.get('lon', location.get('longitude'))
                    observed = stamp(location.get('timestamp')) or when
                    if valid_coordinate(lat, lon) and abs(observed-when) <= 60 and (not ata or observed >= ata):
                        point = dict(latitude=lat, longitude=lon, timestamp=observed, source='fr24_event')
                        break
            if point is None and not has_aircraft_point and valid_coordinate(details.get('gate_lat'), details.get('gate_lon')):
                # This is the provider's gate reference, not a freshly measured
                # aircraft GPS sample. Preserve that distinction for repair.
                point = dict(latitude=details['gate_lat'], longitude=details['gate_lon'], timestamp=when,
                             source='fr24_gate_reference')
            choices.append((when, 2 if point and point['source'] == 'fr24_event' else 1 if point else 0, point))
    if not choices:
        return {}
    when, _, point = max(choices, key=lambda item: item[:2])
    result = {'aibt': when, 'aibt_source': 'FR24'}
    if point:
        result['aibt_position'] = point
    return result
