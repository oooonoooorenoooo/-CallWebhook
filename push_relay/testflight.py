"""Master-only App Store Connect adapter. Credentials never leave the relay."""
import base64
import json
import os
import re
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

BUNDLE_ID = 'de.comatalarm.app.ios.U98PKCA4W7'


def failure(message, status=502):
    return web.json_response({'message': message}, status=status)


class AppleError(Exception):
    pass


class TestFlight:
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
                    reasons = {401:'App-Store-Connect-Schlüssel wurde abgelehnt.',403:'Dem App-Store-Connect-Schlüssel fehlt die Berechtigung für Tester.',409:'Apple meldet einen Konflikt. Testgruppe und Tester in App Store Connect prüfen.',429:'Apple begrenzt die Anfragen. Bitte später erneut versuchen.'}
                    raise AppleError(reasons.get(response.status, 'App Store Connect meldet einen Fehler. Bitte später erneut versuchen.'))
                return await response.json() if response.status != 204 else {}

    async def groups(self):
        apps = await self.request('GET','apps',params={'filter[bundleId]':BUNDLE_ID,'limit':'2'})
        if len(apps.get('data',[])) != 1:
            raise AppleError('ComatAlarm ist für diesen App-Store-Connect-Schlüssel nicht eindeutig verfügbar.')
        app_id = apps['data'][0]['id']
        result = await self.request('GET', f'apps/{app_id}/betaGroups', params={'limit':'200'})
        if result.get('links',{}).get('next'):
            raise AppleError('Zu viele Testgruppen. Bitte in App Store Connect verwalten.')
        return [{'id':x['id'],'name':x['attributes']['name']} for x in result.get('data',[]) if x.get('attributes',{}).get('isInternalGroup') is False]

    async def add(self, email, group_id):
        if not isinstance(email,str) or len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            raise ValueError('Gültige E-Mail-Adresse eingeben.')
        if not isinstance(group_id,str) or group_id not in {x['id'] for x in await self.groups()}:
            raise ValueError('Eine externe ComatAlarm-Testgruppe auswählen.')
        result = await self.request('GET','betaTesters',params={'filter[email]':email,'limit':'2'})
        existing = [x for x in result.get('data',[]) if x.get('attributes',{}).get('email','').casefold()==email.casefold()]
        if existing:
            await self.request('POST', f'betaGroups/{group_id}/relationships/betaTesters', {'data':[{'type':'betaTesters','id':existing[0]['id']}]})
        else:
            await self.request('POST','betaTesters',{'data':{'type':'betaTesters','attributes':{'email':email},'relationships':{'betaGroups':{'data':[{'type':'betaGroups','id':group_id}]}}}})
