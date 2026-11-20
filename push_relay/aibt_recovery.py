"""Exact AIBT evidence selection and a shared, bounded recovery policy."""
import math
import re

EVIDENCE_REVISION = 2
MAX_AGE = 24*3600
FAST_INTERVAL = 15


def fast_candidate(flight, now):
    """An identified aircraft on taxi-in needs fresh gate events, not live polls."""
    from .comatalarm import departure_from_ber, stamp
    identifier, ata = flight.get('fr24ID'), stamp(flight.get('ata'))
    return bool(isinstance(identifier, str) and identifier and ',' not in identifier
                and not identifier.upper().startswith(('TEMP_', 'NS_'))
                and ata and -60 <= now-ata < 25*60 and not flight.get('aibt')
                and not departure_from_ber(flight) and not flight.get('cancelled') and not flight.get('diverted'))


def fast_due(flight, now):
    from .comatalarm import stamp
    return fast_candidate(flight, now) and now-(stamp(flight.get('_arrival_event_checked_at')) or 0) >= FAST_INTERVAL


def receipt(flight, previous, now):
    """A safe, sparse diagnostic distinguishes a valid empty reply from no query."""
    state = flight.get('_arrival_event_status')
    if (state == previous.get('_arrival_event_status')
            and now-previous.get('_arrival_event_logged_at', 0) < 300):
        return None
    message = {
        'accepted': 'FR24 gate_arrival: Antwort enthält passende AIBT',
        'no_match': 'FR24 gate_arrival: Antwort geprüft; keine passende AIBT übernommen',
        'paused': 'FR24 gate_arrival: Abruf vorübergehend pausiert',
        'error': 'FR24 gate_arrival: Abruf fehlgeschlagen',
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
    from .comatalarm import match_stand
    point = position(flight)
    return bool(in_window(flight, now) and point and positions and revision(flight) < EVIDENCE_REVISION
                and not match_stand(point, positions))


def pending(flight, now, positions=None):
    return bool(in_window(flight, now) and (not position(flight) or refresh_needed(flight, positions, now)))


def retain_pending(flight, now):
    # The handoff has no model context. Keep a legacy coordinate only until the
    # worker can classify it once with the available index, within the same 24h.
    return bool(in_window(flight, now) and (not position(flight) or revision(flight) < EVIDENCE_REVISION))


def attempts(flight):
    value = flight.get('_aibt_recovery_attempts', 0)
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def next_attempt(flight):
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
        text = ('AIBT bestätigt · Koordinate noch nicht übernommen; Pi prüft mit Warteabstand erneut' if confirmed else
                'Landung bestätigt · AIBT noch nicht übernommen; Pi prüft Gate-Arrival erneut')
        next_at = next_attempt(flight)
    if next_at is not None and not confirmed and fast_candidate(flight, now):
        next_at = max(now, (stamp(flight.get('_arrival_event_checked_at')) or 0)+FAST_INTERVAL)
    if next_at is not None and next_at >= (started_at(flight) or now)+MAX_AGE:
        next_at = None
    last_attempt = max(flight.get('_aibt_recovery_at', 0), flight.get('_arrival_event_checked_at', 0))
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
    result = {'aibt': when}
    if point:
        result['aibt_position'] = point
    return result
