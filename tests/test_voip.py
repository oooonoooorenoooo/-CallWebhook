"""Exercise real backend handlers without HA or sending any live notifications."""
import ast
import asyncio
import json
import hashlib
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
from urllib.parse import urlparse

SOURCE = Path(__file__).parents[1] / 'homeassistant/custom_components/callwebhook/__init__.py'


class View:
    def json(self, payload, status_code=200):
        return status_code, payload


class VoIPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        names = ('load_voip', 'save_voip', 'voip_configured', 'voip_dialplan', 'fritz_incoming_pjsip', 'send_voip_push',
                 'CallWebhookVoIPView', 'CallWebhookVoIPCallView', 'CallWebhookVoIPHookView')
        nodes = [n for n in ast.parse(SOURCE.read_text()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
        self.ns = dict(json=json, asyncio=asyncio, os=os, secrets=secrets, time=time,
            hashlib=hashlib, urlparse=urlparse, BACKEND_API_VERSION=9,
            RELAY_PUBLIC_PREFIX='/api/callwebhook/push-relay',
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

    async def test_reload_and_registration_preserve_existing_push_credentials(self):
        saved = dict(key='existing-private-key', key_id='A'*10, team_id='B'*10,
                     hook_secret='c'*64, device={'token':'a'*64, 'environment':'development'})
        self.ns['save_voip'](saved)
        self.ns['load_voip']()
        view = self.ns['CallWebhookVoIPView']()
        code, _ = await view.post(self.request({'action':'register', 'token':'b'*64, 'environment':'production'}))
        self.assertEqual(code, 200)
        persisted = json.loads(self.ns['VOIP_FILE'].read_text())
        for field in ('key', 'key_id', 'team_id', 'hook_secret'):
            self.assertEqual(persisted[field], saved[field])
        status = (await view.get(self.request()))[1]
        self.assertTrue(status['configured'])
        self.assertNotIn('existing-private-key', json.dumps(status))

    async def test_reject_invalid_registration_without_overwriting(self):
        view = self.ns['CallWebhookVoIPView']()
        for body in ([], {}, {'action':'register', 'token':'https://attacker', 'environment':'production'},
                     {'action':'register', 'token':'a'*64, 'environment':'test'}):
            self.assertEqual((await view.post(self.request(body)))[0], 400)
        self.assertFalse(self.ns['VOIP_FILE'].exists())

    async def test_relay_registration_bound_to_device_and_credentials_never_returned(self):
        view = self.ns['CallWebhookVoIPView']()
        token = 'b'*64
        await view.post(self.request(dict(action='register', token=token, environment='production')))
        relay = AsyncMock(return_value=dict(registered=True, token_hash=hashlib.sha256(token.encode()).hexdigest(), environment='production'))
        self.ns['relay_request'] = relay
        payload = dict(action='relay', url='https://push.example.com', credential='d'*64)
        self.assertEqual((await view.post(self.request(payload)))[0], 200)
        state = (await view.get(self.request()))[1]
        self.assertTrue(state['configured'])
        self.assertEqual(state['mode'], 'relay')
        self.assertFalse(state['route_ready'])
        self.assertNotIn('d'*64, json.dumps(state))
        self.assertNotIn('key', self.ns['_voip'])
        self.assertIn('X-CallWebhook-ID', self.ns['voip_dialplan']('[outgoing]\n'))
        relay.return_value['token_hash'] = 'wrong-device'
        self.assertEqual((await view.post(self.request(payload)))[0], 400)
        for url in ('http://push.example.com', 'https://user:pass@push.example.com', 'https://push.example.com/?query=1'):
            self.assertEqual((await view.post(self.request(dict(payload, url=url))))[0], 400)
        await view.post(self.request(dict(action='register', token='c'*64, environment='production')))
        self.assertNotIn('relay_credential', self.ns['_voip'])
        self.assertFalse((await view.get(self.request()))[1]['configured'])

    async def test_relay_sends_only_call_metadata_not_device_token_or_apple_key(self):
        self.ns['_voip'].update(relay_url='https://push.example.com', relay_credential='d'*64,
                               device=dict(token='b'*64, environment='production'))
        relay = AsyncMock(return_value={'accepted': True})
        self.ns['relay_request'] = relay
        with patch.dict(sys.modules, aioapns=SimpleNamespace(APNs=None, NotificationRequest=None, PushType=None)):
            call_id = str(uuid4())
            self.assertTrue(await self.ns['send_voip_push'](call_id, '030123'))
            relay.assert_awaited_once_with('https://push.example.com', 'd'*64, '/v1/ring', {'call_id': call_id, 'caller': '030123'})
            relay.side_effect = RuntimeError('offline')
            self.assertFalse(await self.ns['send_voip_push'](str(uuid4()), '030123'))

    async def test_hook_requires_secret_and_deduplicates_push(self):
        view = self.ns['CallWebhookVoIPHookView']()
        call_id = str(uuid4())
        request = self.request(query={'id':call_id, 'caller':'030123'})
        send = AsyncMock(return_value=False)
        self.ns['send_voip_push'] = send
        self.assertEqual((await view.get(request, 'wrong', 'ring')).status, 401)
        secret = self.ns['_voip']['hook_secret']
        self.assertEqual((await view.get(request, secret, 'ring')).text, 'push_failed')
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


    def test_fritz_incoming_matches_router_without_changing_authenticated_phone_or_outgoing_lines(self):
        phone = "[callwebhook-ios]\ntype=endpoint\nauth=callwebhook-ios-auth\ncontext=from-callwebhook-ios\n"
        trunks = "".join(f"[fritz{i}-registration]\ntype=registration\nserver_uri=sip:192.168.178.1\nline=yes\nendpoint=fritz{i}-endpoint\n"
                         f"[fritz{i}-auth]\ntype=auth\npassword=unchanged-{i}\n" for i in range(1, 4))
        other = "[easybell-registration]\ntype=registration\nserver_uri=sip:voip.easybell.de\n"
        original = phone + trunks + other
        repair = self.ns['fritz_incoming_pjsip']
        result = repair(original)
        self.assertTrue(result.startswith(original))
        self.assertIn('context=from-fritz\nidentify_by=ip', result)
        self.assertIn('endpoint=callwebhook-fritz-incoming\nmatch=192.168.178.1\nsrv_lookups=no', result)
        self.assertEqual(result.count('type=identify'), 1)
        self.assertEqual(repair(result), result)
        # Updating a router replaces the app-owned match, never broadens it.
        moved = repair(result.replace('server_uri=sip:192.168.178.1', 'server_uri=sip:192.168.178.254'))
        self.assertIn('match=192.168.178.254\n', moved)
        self.assertNotIn('match=192.168.178.1\n', moved)

    def test_fritz_match_requires_one_host_and_rejects_networks_and_injected_criteria(self):
        repair = self.ns['fritz_incoming_pjsip']
        base = '[fritz1-registration]\ntype=registration\nserver_uri=sip:'
        for host in ('192.168.178.1/24', '0.0.0.0', '[::]', '224.0.0.1', 'fritz.box,evil.example',
                     'user@fritz.box', 'fritz.box?x=1', 'fritz.box:bad', '*.example', 'fritz..box'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                repair(base + host + '\n')
        with self.assertRaises(ValueError):
            repair('[callwebhook-ios]\ntype=endpoint\n')
        with self.assertRaises(ValueError):
            repair(base + '192.168.178.1\n[fritz2-registration]\nserver_uri=sip:192.168.178.2\n')
        for uri, host in [('fritz.box', 'fritz.box'), ('192.168.178.1:5060', '192.168.178.1'),
                          ('[fd00::1]:5060', 'fd00::1')]:
            self.assertIn('match=' + host + '\n', repair(base + uri + '\n'))

    async def test_old_persisted_route_requires_repair_before_being_ready(self):
        view = self.ns['CallWebhookVoIPView']()
        self.ns['_voip'].update(route_ready=True)
        self.assertFalse((await view.get(self.request()))[1]['route_ready'])
        self.ns['_voip']['incoming_route_revision'] = 3
        self.assertTrue((await view.get(self.request()))[1]['route_ready'])
        self.ns['_voip']['route_ready'] = False
        self.assertFalse((await view.get(self.request()))[1]['route_ready'])


    async def test_incoming_line_metadata_survives_end_and_rejects_invalid_values(self):
        self.ns['send_voip_push'] = AsyncMock(return_value=False)
        hook = self.ns['CallWebhookVoIPHookView']()
        view = self.ns['CallWebhookVoIPCallView']()
        for raw, expected in [('1', 1), ('2', 2), ('3', 3), ('0', None), ('99', None), ('SIM 1', None), ('', None)]:
            call_id = str(uuid4())
            request = self.request(query={'id': call_id, 'line': raw})
            await hook.get(request, self.ns['_voip']['hook_secret'], 'ring')
            self.assertEqual((await view.get(request, call_id))[1]['line'], expected)
            await hook.get(request, self.ns['_voip']['hook_secret'], 'end')
            state = (await view.get(request, call_id))[1]
            self.assertEqual(state['line'], expected)
            self.assertFalse(state['active'])

    def test_dialplan_preserves_incoming_destination_before_goto_and_forwards_line(self):
        self.ns['_voip'].update(device={'token': 'a'*64}, key='private')
        plan = self.ns['voip_dialplan']('[outgoing]\n')
        for line in (1, 2, 3):
            self.assertIn(f'"${{EXTEN}}"="callwhapp{line}"]?Set(__CW_LINE={line})', plan)
        self.assertLess(plan.index('Set(__CW_LINE=3)'), plan.index('same => n,Goto(s,1)'))
        self.assertIn('&line=${CW_LINE}', plan)
        self.assertIn('s^1(${CW_ID}^${CW_LINE})', plan)
        self.assertIn('Set(PJSIP_HEADER(add,X-CallWebhook-Line)=${ARG2})', plan)
        self.assertEqual(plan, self.ns['voip_dialplan'](plan))

    def test_hangup_stops_before_wildcard_can_reenter_ring(self):
        self.ns['_voip'].update(device={'token': 'a'*64}, key='private')
        plan = self.ns['voip_dialplan']('')
        hangup = plan.split('exten => h,1,')[1].split('\n\n')[0]
        self.assertEqual(hangup.splitlines()[-1].strip(), 'same => n,Hangup()')
        self.assertNotIn('Goto(', hangup)

    async def test_accepted_push_without_iphone_ack_reports_timeout(self):
        self.ns['send_voip_push'] = AsyncMock(return_value=True)
        async def timeout(awaitable, **kwargs):
            awaitable.close()
            raise asyncio.TimeoutError
        with patch.object(asyncio, 'wait_for', timeout):
            response = await self.ns['CallWebhookVoIPHookView']().get(
                self.request(query={'id': str(uuid4())}), self.ns['_voip']['hook_secret'], 'ring')
        self.assertEqual(response.text, 'wake_timeout')
        self.assertIn('keine Bereitschaftsbestätigung', self.ns['_voip_last_status'])
