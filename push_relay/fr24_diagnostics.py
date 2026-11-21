"""Explicit administrator diagnostics; provider credentials stay on the Pi.

This request deliberately bypasses flight parsing and result caches so an
operator can inspect the actual historical event response without changing a
flight, its stand, its history, or notification state.
"""
import asyncio
import hashlib
import json
import math
import re
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, web


ENDPOINT = 'https://fr24api.flightradar24.com/api/historic/flight-events/light'
MAX_BODY = 256 * 1024  # Fits the HA proxy even with worst-case JSON escaping.
MAX_ENCODED_BODY = MAX_BODY * 6
MANUAL_INTERVAL = 15
REDACTED = '[Zugangsdaten entfernt]'
_SENSITIVE = re.compile(r'(?:authorization|proxyauthorization|(?:access|refresh|api|fr24|firebase|id)?token|(?:api|setup)?key|secret|password|passwd|credential|cookie|setcookie)', re.I)
_AUTH = re.compile(r'\b(?:Bearer|Basic)\s+[^\s,;<>"\']+', re.I)
_TEXT_SECRET = re.compile(r'''(?ix)
    (\b(?:authorization|proxy[-_]?authorization|(?:access|refresh|api|fr24|firebase|id)[-_]?token|token|(?:api|setup)[-_]?key|secret|password|passwd|credential|cookie|set[-_]?cookie)
    \s*["']?\s*[:=]\s*)
    (?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;<>]+)
''')


def _secret_key(key):
    normalized = re.sub(r'[^a-z0-9]', '', key.lower())
    return bool(_SENSITIVE.fullmatch(normalized) or normalized.endswith(('token', 'secret', 'password', 'credential')))


def _clean_text(value, token, truncated=False):
    text = value.replace(token, REDACTED) if token else value
    text = _AUTH.sub(REDACTED, text)
    text = _TEXT_SECRET.sub(lambda match: match[1] + REDACTED, text)
    if truncated and token:
        # A response-size cutoff must not leave a partially echoed credential.
        for length in range(min(len(token)-1, len(text)), 3, -1):
            if text.endswith(token[:length]):
                text = text[:-length] + REDACTED
                break
    return text


def _clean_json(value, token):
    if isinstance(value, dict):
        return {_clean_text(key, token): REDACTED if _secret_key(key) else _clean_json(item, token)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_json(item, token) for item in value]
    if isinstance(value, str):
        return _clean_text(value, token)
    return value


def _reject_constant(value):
    raise ValueError('Non-JSON number')


class HistoricEventsDiagnostic:
    def __init__(self, monitor):
        self.monitor, self.comat = monitor, monitor.comat
        self.busy = False
        self.next_request_at = 0

    @staticmethod
    def parameters(body):
        identifier = body.get('fr24_id')
        event_types = body.get('event_types', 'gate_arrival')
        if (not isinstance(identifier, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier)):
            raise web.HTTPBadRequest(text='Eine einzelne gültige FR24-ID eingeben')
        if event_types not in ('gate_arrival', 'all', ''):
            raise web.HTTPBadRequest(text='Ungültiger Ereignisfilter')
        return {'flight_ids': identifier, 'event_types': event_types or 'all'}

    async def _source(self):
        now = time.time()
        candidates = list(self.comat.db.execute(
            "SELECT * FROM comat_devices WHERE expires>? AND api_token!='' ORDER BY expires DESC,id LIMIT 8", (now,)))

        async def approved(row):
            token = row['api_token']
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(c) < 32 or ord(c) == 127 for c in token):
                return False
            if not row['firebase_uid']:
                return True  # Existing unexpired legacy registration.
            try:
                access = await self.comat.stand_access(row['firebase_uid'])
                return isinstance(access, dict) and access.get('uid') == row['firebase_uid']
            except Exception:
                return False

        allowed = await asyncio.gather(*(approved(row) for row in candidates))
        for source, permit in zip(candidates, allowed):
            if not permit:
                continue
            current = self.comat.db.execute('SELECT * FROM comat_devices WHERE id=?', (source['id'],)).fetchone()
            if (current is not None and current['expires'] > time.time()
                    and all(current[key] == source[key] for key in ('credential', 'firebase_uid', 'api_token'))):
                return current
        return None

    async def run(self, body):
        params = self.parameters(body)
        now = time.time()
        diagnostic = dict(schema='comatalarm.fr24-events.v1', requested_at=now, completed_at=now,
            endpoint=ENDPOINT, method='GET', params=params, http_status=None, content_type='',
            response=None, response_text=None, response_is_json=False, error=None, message='',
            response_truncated=False, redacted=False, retry_after_seconds=0)

        def finish(error=None, message='', retry=0):
            diagnostic.update(completed_at=time.time(), error=error, message=message,
                              retry_after_seconds=max(0, math.ceil(retry)))
            return {'ok': error is None, 'diagnostic': diagnostic}

        if self.busy:
            return finish('request_busy', 'Eine manuelle FR24-Abfrage läuft bereits. Bitte das Ergebnis abwarten.')
        if now < self.next_request_at:
            return finish('manual_limit', 'Noch keine neue FR24-Abfrage: kurze Pause zwischen manuellen Abfragen.', self.next_request_at-now)
        self.busy = True
        try:
            source = await self._source()
            if source is None:
                return finish('missing_token', 'Kein FR24-Token einer aktuell freigegebenen Geräteregistrierung auf dem Pi verfügbar.')
            token = source['api_token']
            provider = self.comat.provider
            identity = hashlib.sha256(token.encode()).hexdigest()
            now = time.time()
            if provider.cooldown.get(identity, 0) > now:
                return finish('provider_cooldown', 'Keine FR24-Abfrage gesendet: Die bestehende Anbieter-Sperrfrist läuft noch.', provider.cooldown[identity]-now)
            recent = [stamp for stamp in provider.requests.get(identity, []) if now-stamp < 60]
            provider.requests[identity] = recent
            if len(recent) >= 8:
                return finish('minute_limit', 'Keine FR24-Abfrage gesendet: Das gemeinsame Minutenlimit ist erreicht.', min(recent)+60-now)
            # No await between shared-budget reservation and request preparation.
            recent.append(now)
            self.next_request_at = now + MANUAL_INTERVAL
            async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                async with session.get(ENDPOINT, params=params,
                        headers={'Authorization': 'Bearer '+token, 'Accept': 'application/json', 'Accept-Version': 'v1'},
                        allow_redirects=False) as response:
                    from .comatalarm import provider_http_status
                    diagnostic['http_status'] = response.status
                    content_type = str(response.headers.get('Content-Type', ''))
                    clean_content_type = _clean_text(content_type, token)
                    diagnostic['content_type'] = clean_content_type[:200]
                    diagnostic['redacted'] = clean_content_type != content_type
                    provider_error = provider_http_status('FR24', response, provider.cooldown, identity, time.time())
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        remaining = MAX_BODY-len(raw)
                        raw.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            diagnostic['response_truncated'] = True
                            break
                    text = raw.decode('utf-8', errors='replace')
                    parsed = None
                    valid_json = False
                    if not diagnostic['response_truncated']:
                        try:
                            parsed = json.loads(text, parse_constant=_reject_constant)
                            cleaned = _clean_json(parsed, token)
                            diagnostic['response'] = cleaned
                            diagnostic['response_is_json'] = True
                            diagnostic['redacted'] |= cleaned != parsed
                            valid_json = True
                        except (ValueError, TypeError, RecursionError):
                            pass
                    if not valid_json:
                        cleaned = _clean_text(text, token, diagnostic['response_truncated'])
                        diagnostic['response_text'] = cleaned
                        diagnostic['redacted'] |= cleaned != text
                    # Redaction can expand a short credential many times. Bound
                    # the delivered body as well as the upstream bytes so the HA
                    # proxy can always return the actual status and a diagnosis.
                    safe_body = diagnostic['response'] if valid_json else diagnostic['response_text']
                    if len(json.dumps(safe_body).encode('utf-8')) > MAX_ENCODED_BODY:
                        safe_text = json.dumps(safe_body, ensure_ascii=False) if valid_json else safe_body
                        diagnostic['response_text'] = safe_text.encode('utf-8')[:MAX_BODY].decode('utf-8', errors='ignore')
                        diagnostic['response'] = None
                        diagnostic['response_is_json'] = False
                        diagnostic['response_truncated'] = True
                        return finish('response_too_large', 'Die bereinigte FR24-Antwort überschreitet die Anzeigegrenze; angezeigter Rohtext ist gekürzt.')
                    if diagnostic['response_truncated']:
                        return finish('response_too_large', 'FR24-Antwort größer als 256 KiB; angezeigter Rohtext ist gekürzt.')
                    if provider_error:
                        return finish('http_error', 'FR24 hat mit '+provider_error+' geantwortet.', provider.cooldown.get(identity, 0)-time.time())
                    if not valid_json:
                        return finish('non_json_response', 'FR24-Antwort empfangen, aber der Antworttext ist kein gültiges JSON.')
                    return finish(message='FR24-Antwort frisch abgerufen; keine Flugdaten oder Pushes verändert.')
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return finish('timeout', 'FR24-Abfrage nach 10 Sekunden ohne vollständige Antwort beendet; kein automatischer Wiederholungsversuch.')
        except (ClientError, OSError):
            return finish('connection_error', 'FR24-Verbindung fehlgeschlagen; kein automatischer Wiederholungsversuch.')
        except Exception:
            return finish('request_failed', 'Die manuelle FR24-Abfrage konnte nicht abgeschlossen werden.')
        finally:
            self.busy = False
