import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from aiohttp import web

SOURCE = Path(__file__).parents[2] / 'homeassistant/custom_components/callwebhook/__init__.py'

class AppleProxyTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, endpoint, authorization, method='POST', query=''):
        tree = ast.parse(SOURCE.read_text())
        nodes = [n for n in tree.body if getattr(n, 'name', '') == 'forward_push_relay'
                 or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'RELAY_PUBLIC_ROUTES' for t in n.targets)]
        target = AsyncMock(return_value=None)
        ns = {'web': web, 'push_relay_target': target}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
        async def chunks(size):
            yield b'{"appleToken":"dummy"}'
        request = SimpleNamespace(method=method, query_string=query,
            headers={'Authorization': authorization}, content_length=22,
            content=SimpleNamespace(iter_chunked=chunks), app={'hass': object()})
        response = await ns['forward_push_relay'](request, endpoint)
        target.assert_awaited_once()
        return response

    async def test_firebase_token_reaches_apple_route(self):
        response = await self.call('comatalarm/v1/apple/approve', 'Bearer header.payload.signature')
        self.assertEqual(response.status, 503)  # No running add-on in this test.

    async def test_existing_device_token_still_reaches_watch(self):
        self.assertEqual((await self.call('comatalarm/v1/watch', 'Bearer ' + 'a' * 64)).status, 503)

    async def test_jwt_is_not_accepted_as_legacy_device_token(self):
        with self.assertRaises(web.HTTPUnauthorized):
            await self.call('comatalarm/v1/watch', 'Bearer header.payload.signature')

    async def test_bad_apple_tokens_are_rejected(self):
        for token in ['', 'Bearer ' + 'a' * 64, 'Bearer a.b.c\n', 'Bearer ' + 'a' * 16000 + '.b.c']:
            with self.assertRaises(web.HTTPUnauthorized):
                await self.call('comatalarm/v1/apple/approve', token)

    async def test_only_exact_post_without_query_is_routed(self):
        for endpoint, method, query in [('comatalarm/v1/apple/approve', 'GET', ''),
                ('comatalarm/v1/apple/approve/extra', 'POST', ''),
                ('comatalarm/v1/apple/approve', 'POST', 'redirect=elsewhere')]:
            with self.assertRaises(web.HTTPNotFound):
                await self.call(endpoint, 'Bearer a.b.c', method, query)
