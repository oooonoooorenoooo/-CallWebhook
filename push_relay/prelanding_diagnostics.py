"""Bounded, locally authored explanations for prelanding decisions."""
from datetime import datetime
from zoneinfo import ZoneInfo

from .landing_alert import MIN_WARNING_LEAD, WARNING_LEAD, _assessment, _number


REASONS = {
    'no_live': 'noch keine zugeordnete Live-Beobachtung',
    'terminal': 'Landung oder Abschluss bereits bestätigt',
    'identity': 'Live-Beobachtung nicht eindeutig dem aktuellen Flug zugeordnet',
    'destination': 'Ziel der Live-Beobachtung widerspricht dem überwachten Flug',
    'source': 'Position ist keine bestätigte ADS-B-/MLAT-Beobachtung',
    'ground': 'Live-Beobachtung meldet Bodenstatus',
    'timestamp': 'Zeitpunkt der Live-Beobachtung ist ungültig',
    'stale': 'Live-Beobachtung liegt außerhalb der Frischegrenze',
    'eta_missing': 'Live-Beobachtung enthält keine verwendbare ETA',
    'eta_range': 'Live-ETA liegt außerhalb des Anflugzeitraums',
    'telemetry': 'Höhe, Geschwindigkeit oder Sinkrate passen noch nicht zum Endanflug',
    'geometry': 'Entfernung, Kurs oder Flugzeit passen noch nicht zum Endanflug',
    'descent': 'Höhe über dem Flughafen und Sinkflug passen noch nicht zur Live-ETA',
    'confirmation': 'zweite passende Live-Beobachtung steht noch aus',
    'eta_changed': 'größere ETA-Korrektur benötigt eine neue Bestätigung',
    'observation_gap': 'Beobachtungsabstand benötigt eine neue Bestätigung',
    'not_converging': 'Annäherung oder Höhenabnahme noch nicht bestätigt',
    'window_wait': 'Anflug bestätigt; Drei-Minuten-Fenster noch nicht erreicht',
    'window_passed': 'Drei-Minuten-Fenster bereits unterschritten',
    'triggered': 'Voralarm erkannt; Versandstatus steht im Push-Protokoll',
    'disabled': 'Voralarm in den aktuellen Alarmregeln deaktiviert',
    'calendar_expired': 'Kalenderfreigabe ist abgelaufen',
    'off_shift': 'außerhalb des freigegebenen Schichtfensters',
    'ready': 'Anflug und Drei-Minuten-Fenster bestätigt',
    'live_no_match': 'letzter Live-Abgleich ohne passende aktuelle Flugbeobachtung',
    'live_ambiguous': 'letzter Live-Abgleich enthält mehrere mögliche Flugbewegungen',
    'route_conflict': 'aktuelle Live-Antwort widerspricht der überwachten Route',
}


def receipt(previous, current, preferences, now):
    """Update a small persisted checkpoint; return a safe journal message if due.

    The caller records it only after committing the corresponding watch snapshot.
    No provider body, token, local ID, UID or arbitrary error text is copied.
    """
    candidate, reason, values = _assessment(current, now)
    state = current.get('_landing_alert') or {}
    prior = previous.get('_prelanding_report') or {}
    if not isinstance(prior, dict):
        prior = {}
    count = _number(state.get('samples')) or 0
    terminal = reason == 'terminal'
    if terminal and prior.get('reason') == 'triggered':
        return None
    if reason in ('no_live', 'identity'):
        reason = {'no_match': 'live_no_match', 'ambiguous': 'live_ambiguous',
                  'route_conflict': 'route_conflict'}.get(current.get('_live_lookup_status'), reason)
    if state.get('triggered_at') and not terminal:
        reason = 'triggered'
    elif candidate:
        lead = candidate['eta']-now
        if lead <= MIN_WARNING_LEAD:
            reason = 'window_passed'
        elif count < 2:
            reason = state.get('confirmation_reason', 'confirmation')
        elif lead > WARNING_LEAD:
            reason = 'window_wait'
        else:
            reason = 'ready'
    if not terminal and (not preferences.get('enabled', True) or not preferences.get('alarmPrelanding', True)):
        reason = 'disabled'
    elif not terminal and preferences.get('calendar_valid_until', now+1) <= now:
        reason = 'calendar_expired'
    elif not terminal and 'active_windows' in preferences and not any(a <= now < b for a, b in preferences['active_windows']):
        reason = 'off_shift'
    if reason not in REASONS:
        reason = 'confirmation'
    # Waiting flights without live data do not need an identical log every tick.
    # Keep one final reason on arrival even if the approach never qualified.
    if not current.get('_live') and not current.get('atd') and not current.get('ata'):
        return None
    current['_prelanding_report'] = dict(prior)
    if not terminal and reason != 'triggered':
        current['_prelanding_report']['last_reason'] = reason
    if reason == 'triggered' and prior.get('reason') == reason:
        return None
    if reason == 'terminal' and prior.get('reason') == reason:
        return None
    near = (0 < values.get('lead_seconds', 0) <= 600
            or 4 <= values.get('distance_km', 0) <= 60)
    interval = 60 if near else 300
    if prior.get('reason') == reason and now-(_number(prior.get('logged_at')) or 0) < interval:
        return None
    immediate = {'terminal', 'triggered', 'disabled', 'calendar_expired', 'off_shift',
                 'window_passed', 'eta_changed', 'route_conflict'}
    if (prior and reason not in immediate
            and now-(_number(prior.get('logged_at')) or 0) < 60):
        return None  # Ordinary freshness oscillation cannot flood the journal.
    current['_prelanding_report'].update(reason=reason, logged_at=now)
    parts = ['Voralarm: '+REASONS[reason]]
    last_reason = prior.get('last_reason', prior.get('reason'))
    if reason == 'terminal' and last_reason not in (None, 'terminal', 'triggered'):
        parts.append('zuvor: '+REASONS.get(last_reason, REASONS['confirmation']))
    eta = values.get('eta')
    if eta is not None:
        try:
            parts.append('Live-ETA '+datetime.fromtimestamp(eta, ZoneInfo('Europe/Berlin')).strftime('%H:%M:%S'))
        except (OverflowError, OSError, ValueError):
            pass  # A malformed provider timestamp cannot interrupt monitoring.
    for key, label, suffix in (('lead_seconds', 'Vorlauf', 's'), ('sample_age_seconds', 'Datenalter', 's'),
                               ('distance_km', 'Entfernung', 'km'), ('height_ft', 'Höhe über Flughafen', 'ft'),
                               ('speed_kt', 'Geschwindigkeit', 'kt'), ('vertical_fpm', 'Sinkrate', 'ft/min'),
                               ('course_error', 'Kursabweichung', '°')):
        value = values.get(key)
        if value is not None:
            parts.append(f'{label} {round(value, 1):g} {suffix}')
    if candidate:
        parts.append(f'Bestätigungen {min(2, int(count))}/2')
    if not terminal and reason not in ('live_no_match', 'live_ambiguous', 'route_conflict'):
        lookup = {'no_match': 'kein passender aktueller Treffer', 'ambiguous': 'mehrere mögliche Flugbewegungen',
                  'route_conflict': 'widersprüchliche Route'}.get(current.get('_live_lookup_status'))
        if lookup:
            parts.append('letzter Live-Abgleich: '+lookup)
    return ' · '.join(parts)[:400]
