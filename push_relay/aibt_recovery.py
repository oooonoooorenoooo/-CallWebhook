"""Exact AIBT evidence selection and a shared, bounded recovery policy."""
import math

EVIDENCE_REVISION = 2
MAX_AGE = 24*3600


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


def in_window(flight, now):
    from .comatalarm import departure_from_ber, stamp
    aibt = stamp(flight.get('aibt'))
    return bool(aibt and -60 <= now-aibt < MAX_AGE and not departure_from_ber(flight)
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
        state, text = 'expired', 'AIBT bestätigt · innerhalb von 24 Stunden keine passende Koordinate übernommen'
    elif error:
        state, text = 'paused', 'AIBT bestätigt · Koordinatenabruf pausiert: '+error
        next_at = next_attempt(flight)
    else:
        state, text = 'pending', 'AIBT bestätigt · Koordinate noch nicht übernommen; Pi prüft mit Warteabstand erneut'
        next_at = next_attempt(flight)
    if next_at is not None and next_at >= (stamp(flight.get('aibt')) or now)+MAX_AGE:
        next_at = None
    meta = dict(state=state, last_attempt=flight.get('_aibt_recovery_at', 0), next_attempt=next_at,
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
