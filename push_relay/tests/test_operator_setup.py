"""Fresh install, reuse and failure coverage without a live Supervisor or APNs key."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

BACKEND = Path(__file__).parents[2] / 'homeassistant/custom_components/callwebhook/__init__.py'


def load():
    names = {'relay_operator_credentials', 'provision_push_relay'}
    nodes = [node for node in ast.parse(BACKEND.read_text()).body if getattr(node, 'name', '') in names]
    ns = {'time': SimpleNamespace(monotonic=lambda: 0, sleep=lambda seconds: None)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BACKEND), 'exec'), ns)
    return ns


def credentials():
    return {'apns_team_id': 'ABCDEFGHIJ', 'apns_key_id': '0123456789',
        'apns_private_key': ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()}


class OperatorSetupTests(unittest.TestCase):
    def simulate(self, installed, options):
        ns = load()
        state = dict(installed=installed, options=options.copy(), state='started' if installed else 'stopped', boot='auto')
        writes = []
        slug = 'a1b2c3_callwebhook_push_relay'
        def request(method, endpoint, payload=None, **kw):
            if method == 'POST':
                writes.append((endpoint, payload))
                if endpoint.endswith('/install'): state['installed'] = True
                if endpoint.endswith('/options'): state['options'] = payload['options']; state['boot'] = payload['boot']
                if endpoint.endswith(('/start', '/restart')): state['state'] = 'started'
                return {}
            if endpoint == '/store':
                return {'repositories': [{'slug': 'a1b2c3', 'source': 'https://github.com/oooonoooorenoooo/-CallWebhook.git'}],
                    'addons': [{'slug': slug}, {'slug': 'badbad_callwebhook_push_relay'}]}
            if endpoint in (f'/store/addons/{slug}', f'/addons/{slug}/info'): return dict(state)
            raise AssertionError(endpoint)
        ns['supervisor_request'] = request
        ns['requests'] = SimpleNamespace(get=Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {'ready': True})))
        return ns, state, writes

    def test_fresh_install_uses_real_slug_configures_key_and_starts(self):
        ns, state, writes = self.simulate(False, {'unrelated': 'keep'})
        key = credentials()
        progress = []
        ns['provision_push_relay'](key, lambda step, message: progress.append(step))
        self.assertEqual(progress, [0, 1, 2, 3])
        self.assertEqual(state['options'], dict(key, unrelated='keep'))
        self.assertEqual(state['state'], 'started')
        self.assertEqual([p for p, _ in writes], ['/store/reload', '/store/addons/a1b2c3_callwebhook_push_relay/install',
            '/addons/a1b2c3_callwebhook_push_relay/options', '/addons/a1b2c3_callwebhook_push_relay/start'])
        self.assertNotIn('apns_private_key', repr(progress))

    def test_existing_service_reuses_key_without_restart_or_install(self):
        ns, state, writes = self.simulate(True, credentials())
        ns['provision_push_relay']({}, lambda *args: None)
        self.assertEqual(writes, [('/store/reload', None)])

    def test_missing_key_does_not_start_or_report_success(self):
        ns, state, writes = self.simulate(False, {})
        with self.assertRaisesRegex(ValueError, 'APNs-Schlüssel'):
            ns['provision_push_relay']({}, lambda *args: None)
        self.assertEqual(state['state'], 'stopped')
        self.assertFalse(any(p.endswith('/start') for p, _ in writes))

    def test_bad_keys_are_rejected_without_echoing_secrets(self):
        ns = load()
        key = credentials()
        key['apns_private_key'] = 'SECRET_INVALID_PRIVATE_KEY'
        with self.assertRaises(ValueError) as error:
            ns['relay_operator_credentials'](key)
        self.assertNotIn('SECRET', str(error.exception))
        self.assertEqual(ns['relay_operator_credentials']({}), {})
        key = credentials()
        self.assertEqual(ns['relay_operator_credentials'](key), key)
