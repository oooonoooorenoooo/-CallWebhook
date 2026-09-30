import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).parents[2]
BACKEND = ROOT / 'homeassistant/custom_components/callwebhook/__init__.py'


def backend_functions():
    names = {'find_push_relay', 'push_relay_target', 'forward_push_relay'}
    nodes = [n for n in ast.parse(BACKEND.read_text()).body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    ns = dict(asyncio=asyncio, time=time, web=web, ClientSession=ClientSession, ClientTimeout=ClientTimeout,
              _relay_host_lock=asyncio.Lock(), _relay_host_cache=(0, None),
              RELAY_PUBLIC_ROUTES={('GET','healthz'),('POST','v1/challenge'),('POST','v1/register'),
                  ('GET','v1/registration'),('DELETE','v1/registration'),('POST','v1/ring')})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BACKEND), 'exec'), ns)
    return ns


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_actual_repository_slug_and_internal_hostname(self):
        ns = backend_functions()
        responses = {
            '/addons': {'addons': [{'slug':'deadbeef_callwebhook_push_relay'}, {'slug':'badbad_callwebhook_push_relay'}]},
            '/store': {'repositories': [{'slug':'deadbeef','source':'https://github.com/oooonoooorenoooo/-CallWebhook.git'}]},
            '/addons/deadbeef_callwebhook_push_relay/info': {'state':'started'},
        }
        ns['supervisor_request'] = lambda method, path, **kw: responses[path]
        self.assertEqual(ns['find_push_relay'](), 'http://deadbeef-callwebhook-push-relay:8080')
        responses['/addons/deadbeef_callwebhook_push_relay/info']['state'] = 'stopped'
        self.assertIsNone(ns['find_push_relay']())
        responses['/store']['repositories'] = [{'slug':'deadbeef','source':'https://github.com/other/repo'}]
        self.assertIsNone(ns['find_push_relay']())

    async def test_transient_supervisor_failure_keeps_verified_target_and_is_cached(self):
        ns = backend_functions()
        known = 'http://deadbeef-callwebhook-push-relay:8080'
        ns['_relay_host_cache'] = (0, known)
        executor = AsyncMock(side_effect=RuntimeError('Supervisor busy'))
        hass = SimpleNamespace(async_add_executor_job=executor)
        self.assertEqual(await ns['push_relay_target'](hass), known)
        self.assertEqual(await ns['push_relay_target'](hass), known)
        self.assertEqual(executor.await_count, 1)


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = backend_functions()
        self.forwarded = []
        async def upstream(request):
            self.forwarded.append((request.method, request.path, dict(request.headers), await request.read()))
            return web.json_response({'ready': True})
        server = web.Application()
        server.router.add_route('*', '/{path:.*}', upstream)
        self.upstream = TestServer(server)
        await self.upstream.start_server()
        self.ns['push_relay_target'] = AsyncMock(return_value=str(self.upstream.make_url('')).rstrip('/'))
        async def forward(request):
            return await self.ns['forward_push_relay'](request, request.match_info['endpoint'])
        proxy = web.Application()
        proxy['hass'] = object()
        proxy.router.add_route('*', '/api/callwebhook/push-relay/{endpoint:.*}', forward)
        self.client = TestClient(TestServer(proxy))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()

    async def test_only_fixed_routes_and_methods_are_forwarded(self):
        for path in ('supervisor', 'host', 'v1/register?target=http://other', 'v1/other'):
            self.assertEqual((await self.client.post('/api/callwebhook/push-relay/' + path, json={})).status, 404)
        self.assertEqual((await self.client.delete('/api/callwebhook/push-relay/healthz')).status, 404)
        self.assertFalse(self.forwarded)

    async def test_ha_credentials_and_cookies_are_not_forwarded(self):
        response = await self.client.post('/api/callwebhook/push-relay/v1/challenge', json={'key_id':'public'},
            headers={'Authorization':'Bearer HA-SECRET', 'Cookie':'HA-COOKIE', 'X-Supervisor-Token':'PRIVATE'})
        self.assertEqual(response.status, 200)
        method, path, headers, body = self.forwarded[0]
        self.assertEqual(path, '/v1/challenge')
        self.assertEqual(json.loads(body), {'key_id':'public'})
        for header in ('Authorization', 'Cookie', 'X-Supervisor-Token'):
            self.assertNotIn(header, headers)

    async def test_device_grant_required_and_body_limit_enforced(self):
        url = '/api/callwebhook/push-relay/v1/ring'
        for headers in ({}, {'Authorization':'Bearer HA-SECRET'}):
            self.assertEqual((await self.client.post(url, headers=headers, json={})).status, 401)
        headers = {'Authorization':'Bearer ' + 'd'*64}
        self.assertEqual((await self.client.post(url, headers=headers, data=b'a'*32769)).status, 413)
        self.assertEqual((await self.client.post(url, headers=headers, json={'call_id':'id'})).status, 200)
        self.assertEqual(self.forwarded[0][2]['Authorization'], headers['Authorization'])


class AddonOptionsTests(unittest.TestCase):
    def test_pem_formats_validated_and_only_private_addon_directory_used(self):
        spec = importlib.util.spec_from_file_location('relay_addon', ROOT / 'callwebhook_push_relay/run.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        key = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        with tempfile.TemporaryDirectory() as directory:
            for value in (key, key.replace('\n', '\\n'), key.replace('\n', ' ')):
                settings = module.configure(dict(apns_team_id='A'*10, apns_key_id='B'*10, apns_private_key=value), Path(directory))
                self.assertEqual(Path(settings['APNS_KEY_FILE']).read_text(), key)
                self.assertEqual(Path(settings['APNS_KEY_FILE']).stat().st_mode & 0o777, 0o600)
                self.assertNotIn(key, json.dumps(settings))
                self.assertEqual(settings['ALLOW_DEVELOPMENT'], '0')
            options = dict(apns_team_id='A'*10, apns_key_id='B'*10, apns_private_key=key)
            self.assertEqual(module.configure(dict(options, allow_development=True), Path(directory))['ALLOW_DEVELOPMENT'], '1')
            self.assertEqual(module.configure(dict(options, allow_development=False), Path(directory))['ALLOW_DEVELOPMENT'], '0')
            with self.assertRaises(ValueError):
                module.configure(dict(options, allow_development='false'), Path(directory))
            with self.assertRaises(ValueError): module.normalize_key('not a private key')
