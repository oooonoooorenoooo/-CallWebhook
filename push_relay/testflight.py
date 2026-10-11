"""Master-only App Store Connect adapter. Credentials never leave the relay."""
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

BUNDLE_ID = 'de.comatalarm.app.ios.U98PKCA4W7'


def failure(message, status=502):
    return web.json_response({'message': message}, status=status)


class AppleError(Exception):
    pass


class TestFlight:
    def __init__(self, db=None):
        self.db = db
        self.lock = asyncio.Lock()
        if db is not None:
            db.execute("CREATE TABLE IF NOT EXISTS comat_testflight_requests (email TEXT PRIMARY KEY, group_id TEXT NOT NULL, first_name TEXT NOT NULL, last_name TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)")

    def statuses(self):
        if self.db is None:
            return []
        return [dict(row) for row in self.db.execute("SELECT email,state,message FROM comat_testflight_requests ORDER BY updated DESC LIMIT 20")]

    def jwt(self):
        issuer, key_id, filename = (os.environ.get(k, '') for k in ('ASC_ISSUER_ID', 'ASC_KEY_ID', 'ASC_KEY_FILE'))
        if not all((issuer, key_id, filename)):
            raise AppleError('Im Push-Add-on fehlen App-Store-Connect-Issuer-ID, Key-ID und privater Schlüssel. Der APNs-Schlüssel ist dafür nicht geeignet.')
        encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b'=')
        now = int(time.time())
        content = b'.'.join(encode(json.dumps(x).encode()) for x in ({'alg':'ES256','kid':key_id,'typ':'JWT'}, {'iss':issuer,'iat':now-5,'exp':now+600,'aud':'appstoreconnect-v1'}))
        try:
            key = serialization.load_pem_private_key(Path(filename).read_bytes(), password=None)
            r, s = utils.decode_dss_signature(key.sign(content, ec.ECDSA(hashes.SHA256())))
        except Exception:
            raise AppleError('App-Store-Connect-Schlüssel im Push-Add-on prüfen.') from None
        return (content + b'.' + encode(r.to_bytes(32,'big') + s.to_bytes(32,'big'))).decode()

    async def request(self, method, path, body=None, params=None):
        token = self.jwt()
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            async with session.request(method, 'https://api.appstoreconnect.apple.com/v1/' + path,
                    headers={'Authorization':'Bearer '+token}, json=body, params=params, allow_redirects=False) as response:
                if response.status not in (200, 201, 204):
                    reasons = {401:'App-Store-Connect-Schlüssel wurde abgelehnt.',403:'Dem App-Store-Connect-Schlüssel fehlt die Berechtigung für Benutzerverwaltung oder TestFlight. Neue Tester erhalten nur Marketing für ComatAlarm.',409:'Apple meldet einen Konflikt. Testgruppe und Tester in App Store Connect prüfen.',429:'Apple begrenzt die Anfragen. Bitte später erneut versuchen.'}
                    raise AppleError(reasons.get(response.status, 'App Store Connect meldet einen Fehler. Bitte später erneut versuchen.'))
                return await response.json() if response.status != 204 else {}

    async def app_id(self):
        apps = await self.request('GET', 'apps', params={'filter[bundleId]': BUNDLE_ID, 'limit': '2'})
        if len(apps.get('data', [])) != 1:
            raise AppleError('ComatAlarm ist für diesen App-Store-Connect-Schlüssel nicht eindeutig verfügbar.')
        return apps['data'][0]['id']

    async def groups(self, app_id=None):
        app_id = app_id or await self.app_id()
        result = await self.request('GET', f'apps/{app_id}/betaGroups', params={'limit': '200'})
        if result.get('links', {}).get('next'):
            raise AppleError('Zu viele Testgruppen. Bitte in App Store Connect verwalten.')
        return [{'id': x['id'], 'name': x['attributes']['name']} for x in result.get('data', [])
                if x.get('attributes', {}).get('isInternalGroup') is True]

    async def in_group(self, resource_type, resource_id, group_id):
        # Apple exposes build membership from the group; the inverse GET
        # /builds/{id}/relationships/betaGroups is not an API endpoint.
        path = f'betaGroups/{group_id}/relationships/builds' if resource_type == 'builds' else f'betaTesters/{resource_id}/relationships/betaGroups'
        expected = resource_id if resource_type == 'builds' else group_id
        result = await self.request('GET', path, params={'limit': '200'})
        if expected in {x['id'] for x in result.get('data', [])}:
            return True
        if result.get('links', {}).get('next'):
            raise AppleError('Zu viele Gruppenzuordnungen. Bitte in App Store Connect prüfen.')
        return False

    @staticmethod
    def validate(email, group_id, first_name, last_name):
        if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            raise ValueError('Gültige E-Mail-Adresse eingeben.')
        if not isinstance(group_id, str) or not group_id:
            raise ValueError('Eine interne ComatAlarm-Testgruppe auswählen.')
        if any(not isinstance(x, str) or not x.strip() or len(x) > 100 or any(ord(c) < 32 for c in x) for x in (first_name, last_name)):
            raise ValueError('Vor- und Nachnamen eingeben.')

    async def configure(self, email, group_id, first_name, last_name, allow_invite=True):
        self.validate(email, group_id, first_name, last_name)
        app_id = await self.app_id()
        if group_id not in {x['id'] for x in await self.groups(app_id)}:
            raise ValueError('Eine interne ComatAlarm-Testgruppe auswählen.')
        users = await self.request('GET', 'users', params={'filter[username]': email, 'limit': '2'})
        users = [x for x in users.get('data', []) if x.get('attributes', {}).get('username', '').casefold() == email.casefold()]
        if not users:
            invitations = await self.request('GET', 'userInvitations', params={'filter[email]': email, 'limit': '2'})
            pending = [x for x in invitations.get('data', []) if x.get('attributes', {}).get('email', '').casefold() == email.casefold()]
            if not pending:
                if not allow_invite:
                    return {'state': 'pending', 'message': 'Apple-Einladung noch nicht angenommen oder abgelaufen. Bei Bedarf erneut einladen.'}
                await self.request('POST', 'userInvitations', {'data': {
                    'type': 'userInvitations',
                    'attributes': {'email': email, 'firstName': first_name.strip(), 'lastName': last_name.strip(),
                                   'roles': ['MARKETING'], 'allAppsVisible': False, 'provisioningAllowed': False},
                    'relationships': {'visibleApps': {'data': [{'type': 'apps', 'id': app_id}]}}}})
            return {'state': 'pending', 'message': 'Apple-Benutzer-Einladung ausstehend. Nach Annahme werden Testgruppe und neuester verfügbarer Build automatisch zugewiesen.'}
        user = users[0]
        attributes = user.get('attributes', {})
        if not set(attributes.get('roles', [])) & {'ACCOUNT_HOLDER', 'ADMIN', 'APP_MANAGER', 'DEVELOPER', 'MARKETING'}:
            raise AppleError('Bestehender Benutzer hat keine für interne Tests geeignete Rolle. Bitte in App Store Connect prüfen; bestehende Rechte wurden nicht verändert.')
        if not attributes.get('allAppsVisible'):
            apps = await self.request('GET', f"users/{user['id']}/visibleApps", params={'limit': '200'})
            if app_id not in {x['id'] for x in apps.get('data', [])}:
                raise AppleError('Bestehender Benutzer hat keinen Zugriff auf ComatAlarm. Bitte in App Store Connect zuweisen; bestehende Rechte wurden nicht verändert.')
        builds = await self.request('GET', 'builds', params={'filter[app]': app_id, 'filter[processingState]': 'VALID',
                                    'filter[expired]': 'false', 'sort': '-uploadedDate', 'limit': '1'})
        builds = builds.get('data', [])
        if not builds or builds[0].get('attributes', {}).get('expired') is not False or builds[0].get('attributes', {}).get('processingState') != 'VALID':
            raise AppleError('Noch kein verarbeiteter, gültiger Build verfügbar. Die Zuweisung wird erneut versucht.')
        build = builds[0]
        # Apple checks encryption/test readiness; never report success if assignment fails.
        if not await self.in_group('builds', build['id'], group_id):
            await self.request('POST', f'betaGroups/{group_id}/relationships/builds', {'data': [{'type': 'builds', 'id': build['id']}]})
        result = await self.request('GET', 'betaTesters', params={'filter[email]': email, 'limit': '2'})
        existing = [x for x in result.get('data', []) if x.get('attributes', {}).get('email', '').casefold() == email.casefold()]
        if existing:
            if not await self.in_group('betaTesters', existing[0]['id'], group_id):
                await self.request('POST', f'betaGroups/{group_id}/relationships/betaTesters', {'data': [{'type': 'betaTesters', 'id': existing[0]['id']}]})
        else:
            await self.request('POST', 'betaTesters', {'data': {'type': 'betaTesters', 'attributes': {'email': email},
                               'relationships': {'betaGroups': {'data': [{'type': 'betaGroups', 'id': group_id}]}}}})
        version = build.get('attributes', {}).get('version', build['id'])
        return {'state': 'assigned', 'message': f'Interner Testzugang eingerichtet · Build {version}. TestFlight-Einladung annehmen. Bestehende Benutzerrechte bleiben unverändert.'}

    def save_result(self, email, result):
        self.db.execute('UPDATE comat_testflight_requests SET state=?, message=?, updated=? WHERE email=?',
                        (result['state'], result['message'], time.time(), email))
        self.db.commit()

    async def add(self, email, group_id, first_name, last_name):
        self.validate(email, group_id, first_name, last_name)
        email = email.casefold()
        async with self.lock:
            if self.db is None:
                return await self.configure(email, group_id, first_name, last_name)
            count = self.db.execute("SELECT count(*) FROM comat_testflight_requests WHERE state = 'pending'").fetchone()[0]
            existing = self.db.execute('SELECT email FROM comat_testflight_requests WHERE email=?', (email,)).fetchone()
            if count >= 20 and not existing:
                raise ValueError('Maximal 20 offene Einladungen. Bitte zuerst bestehende Einladungen in App Store Connect klären.')
            now = time.time()
            self.db.execute('INSERT OR REPLACE INTO comat_testflight_requests VALUES (?,?,?,?,?,?,?,?)',
                            (email, group_id, first_name, last_name, 'pending', 'Zuweisung wird geprüft.', now, now))
            self.db.commit()
            try:
                result = await self.configure(email, group_id, first_name, last_name)
            except (AppleError, ValueError, ClientError, asyncio.TimeoutError) as error:
                self.save_result(email, {'state': 'pending', 'message': str(error) if isinstance(error, (AppleError, ValueError)) else 'Apple nicht erreichbar. Zuweisung wird erneut geprüft.'})
                raise
            self.save_result(email, result)
            return result

    async def reconcile(self):
        if self.db is None or self.lock.locked():
            return
        async with self.lock:
            rows = self.db.execute("SELECT * FROM comat_testflight_requests WHERE state='pending' AND updated<? ORDER BY updated LIMIT 20", (time.time()-300,)).fetchall()
            for row in rows:
                if row['created'] < time.time()-7*86400:
                    result = {'state': 'expired', 'message': 'Zuweisung nach sieben Tagen beendet. Apple-Einladung prüfen und bei Bedarf erneut hinzufügen.'}
                else:
                    try:
                        # Retries may assign an accepted user, but must never send another invitation.
                        result = await self.configure(row['email'], row['group_id'], row['first_name'], row['last_name'], allow_invite=False)
                    except (AppleError, ValueError, ClientError, asyncio.TimeoutError) as error:
                        result = {'state': 'pending', 'message': str(error) if isinstance(error, (AppleError, ValueError)) else 'Apple momentan nicht erreichbar. Zuweisung wird erneut geprüft.'}
                self.save_result(row['email'], result)
