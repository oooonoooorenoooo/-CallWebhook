"""Bind a current FR24 position to one watched flight occurrence.

Route fields are optional in the official FlightPositionsFull model. An exact
leg ID can therefore survive a missing route field, but never a contradiction.
Without an established ID, require one current, fully matching route on the
watch's Berlin calendar day; a flight number alone cannot identify a leg.
"""
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")
PLACEHOLDERS = ("TEMP_", "NS_", "LANDED_")


def _text(value):
    return value.strip().upper() if isinstance(value, str) else ""


def _identifier(value):
    # The Full response model defines a string, not a restricted ID grammar.
    # Preserve provider IDs verbatim; only local placeholders are ineligible.
    return value if _text(value) and not _text(value).startswith(PLACEHOLDERS) else None


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 1_000_000_000 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def select_live_position(rows, flight, now):
    """Return (fresh provider row or None, fixed diagnostic status)."""
    stored = flight.get("fr24ID")
    identifier = _identifier(stored)
    if stored and identifier is None and not _text(stored).startswith(PLACEHOLDERS):
        return None, "no_match"  # Never replace a malformed identity by another leg.
    route = {"orig_iata": _text(flight.get("origin")), "dest_iata": _text(flight.get("destination"))}
    reference = _timestamp(flight.get("reference"))
    if identifier is None:
        if reference is None or not all(route.values()) or not _text(flight.get("number")):
            return None, "no_match"
        try:
            if datetime.fromtimestamp(reference, BERLIN).date() != datetime.fromtimestamp(now, BERLIN).date():
                return None, "no_match"
        except (ValueError, OverflowError, OSError):
            return None, "no_match"
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not _identifier(row.get("fr24_id")):
            continue
        if identifier is not None:
            if _identifier(row["fr24_id"]) != identifier:
                continue
        elif _text(row.get("flight")) != _text(flight["number"]):
            continue
        observed = _timestamp(row.get("timestamp"))
        if observed is None or not now-120 <= observed <= now+10:
            continue
        if identifier is None and datetime.fromtimestamp(observed, BERLIN).date() != datetime.fromtimestamp(reference, BERLIN).date():
            continue
        conflicting = any(expected and _text(row.get(field)) and _text(row[field]) != expected
                          for field, expected in route.items())
        if identifier is not None and conflicting:
            return None, "route_conflict"
        if any((row.get(field) is not None and not isinstance(row[field], str))
               or (identifier is None and _text(row.get(field)) != expected)
               for field, expected in route.items()):
            continue
        lat, lon = row.get("lat"), row.get("lon")
        if (not all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon))
                or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            continue
        matches.append((observed, row))
    if not matches:
        return None, "no_match"
    if len({_identifier(row["fr24_id"]) for _, row in matches}) != 1:
        return None, "ambiguous"
    return max(matches, key=lambda item: item[0])[1], "accepted"
