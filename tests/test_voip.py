"""Exercise real backend handlers without HA or sending any live notifications."""
import ast
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

SOURCE = Path(__file__).parents[1] / 'homeassistant/custom_components/callwebhook/__init__.py'


class View:
    def json(self, payload, status_code=200):
        return status_code, payload


class VoIPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        names = ('load_voip', 'save_voip', 'voip_dialplan', 'send_voip_push',
                 'CallWebhookVoIPView', 'CallWebhookVoIPCallView', 'CallWebhookVoIPHookView')
        nodes = [n for n in ast.parse(SOURCE.read_text()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
        self.ns = dict(json=json, asyncio=asyncio, os=os, secrets=secrets, time=time,
            BASE_DIR=path, VOIP_FILE=path/'voip.json', VOIP_TOPIC='test.app.voip', _voip={},
            _voip_calls={}, _voip_clients={}, _voip_lock=asyncio.Lock(), _voip_last_status='',
            HomeAssistantView=View, web=SimpleNamespace(Response=lambda **kw: SimpleNamespace(**kw)))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.ns)
        self.ns['load_voip']()

    def request(self, payload=None, query=None):
        async def read(): return payload
        async def executor(fn, *args): return fn(*args)
        return SimpleNamespace(json=read, query=query or {}, app={'hass': SimpleNamespace(async_add_executor_job=executor)})

    async def test_device_rotation_persists_and_old_invalidation_does_not_remove_new_token(self):
        view = self.ns['CallWebhookVoIPView']()
        client = object()
        self.ns['_voip_clients']['production'] = client
        for token in ('a'*64, 'b'*64):
            code, _ = await view.post(self.request({'action':'register', 'token':token, 'environment':'production'}))
            self.assertEqual(code, 200)
        await view.post(self.request({'action':'unregister', 'token':'a'*64}))
        saved = json.loads(self.ns['VOIP_FILE'].read_text())
        self.assertEqual(saved['device']['token'], 'b'*64)
        self.assertIs(self.ns['_voip_clients']['production'], client)
        self.assertEqual(self.ns['VOIP_FILE'].stat().st_mode & 0o777, 0o600)
        code, status = await view.get(self.request())
        self.assertNotIn('b'*64, json.dumps(status))
        self.assertNotIn('hook_secret', status)
        await view.post(self.request({'action':'unregister', 'token':'b'*64}))
        self.assertNotIn('device', self.ns['_voip'])

    async def test_reject_invalid_registration_without_overwriting(self):
        view = self.ns['CallWebhookVoIPView']()
        for body in ([], {}, {'action':'register', 'token':'https://attacker', 'environment':'production'},
                     {'action':'register', 'token':'a'*64, 'environment':'test'}):
            self.assertEqual((await view.post(self.request(body)))[0], 400)
        self.assertFalse(self.ns['VOIP_FILE'].exists())

    async def test_hook_requires_secret_and_deduplicates_push(self):
        view = self.ns['CallWebhookVoIPHookView']()
        call_id = str(uuid4())
        request = self.request(query={'id':call_id, 'caller':'030123'})
        send = AsyncMock(return_value=False)
        self.ns['send_voip_push'] = send
        self.assertEqual((await view.get(request, 'wrong', 'ring')).status, 401)
        secret = self.ns['_voip']['hook_secret']
        self.assertEqual((await view.get(request, secret, 'ring')).text, 'ready')
        self.assertEqual((await view.get(request, secret, 'ring')).text, 'duplicate')
        send.assert_awaited_once_with(call_id, '030123')
        await view.get(request, secret, 'end')
        self.assertEqual((await view.get(request, secret, 'ring')).text, 'cancelled')
        state = await self.ns['CallWebhookVoIPCallView']().get(request, call_id)
        self.assertFalse(state[1]['active'])

    async def test_ready_unblocks_asterisk_and_decline_cancels_call(self):
        call_id = str(uuid4())
        secret = self.ns['_voip']['hook_secret']
        call_view = self.ns['CallWebhookVoIPCallView']()
        async def send(id, caller):
            self.assertEqual((await call_view.post(self.request({'action':'ready'}), id))[0], 200)
            self.assertTrue(self.ns['_voip_calls'][id]['ready'].is_set())
            return True
        self.ns['send_voip_push'] = send
        hook = self.ns['CallWebhookVoIPHookView']()
        await hook.get(self.request(query={'id':call_id}), secret, 'ring')
        await call_view.post(self.request({'action':'end'}), call_id)
        self.assertFalse((await call_view.get(self.request(), call_id))[1]['active'])
        self.assertEqual((await call_view.post(self.request({'action':'ready'}), call_id))[0], 410)

    async def test_sender_uses_voip_short_ttl_and_correct_environment(self):
        self.ns['_voip'].update(key='private', key_id='A'*10, team_id='B'*10,
                                device={'token':'a'*64, 'environment':'production'})
        clients, requests = [], []
        class Client:
            def __init__(self, **kwargs): clients.append(kwargs)
            async def send_notification(self, request):
                requests.append(request)
                return SimpleNamespace(is_successful=True)
        module = SimpleNamespace(APNs=Client, NotificationRequest=lambda **kw: kw, PushType=SimpleNamespace(VOIP='voip'))
        with patch.dict(sys.modules, aioapns=module):
            self.assertTrue(await self.ns['send_voip_push'](str(uuid4()), '030123'))
        self.assertEqual(clients[0]['topic'], 'test.app.voip')
        self.assertFalse(clients[0]['use_sandbox'])
        self.assertEqual(requests[0]['push_type'], 'voip')
        self.assertEqual(requests[0]['time_to_live'], 5)
        self.assertEqual(requests[0]['priority'], 10)
        self.assertNotIn('private', json.dumps(requests[0]))

    def test_dialplan_migration_preserves_outgoing_and_does_not_duplicate_contexts(self):
        old = '[from-callwebhook-ios]\nexten => _X.,1,Dial(PJSIP/${EXTEN}@fritz1-endpoint)\n[from-fritz]\nexten => s,1,Hangup()\n'
        self.assertEqual(self.ns['voip_dialplan'](old), old)
        self.ns['_voip'].update(device={'token':'a'*64}, key='private')
        updated = self.ns['voip_dialplan'](old)
        self.assertIn('Dial(PJSIP/${EXTEN}@fritz1-endpoint)', updated)
        self.assertIn('X-CallWebhook-ID', updated)
        self.assertIn('PJSIP_DIAL_CONTACTS(callwebhook-ios)', updated)
        self.assertIn('URIENCODE(${CALLERID(num)})', updated)
        self.assertEqual(updated.count('[from-fritz]'), 1)
        self.assertEqual(updated, self.ns['voip_dialplan'](updated))
        self.assertLess(updated.index('/ring?'), updated.index('Dial(${PJSIP_DIAL_CONTACTS'))
        self.assertNotIn('private', updated)
