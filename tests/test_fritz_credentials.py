"""Credential persistence must preserve other secrets and restrict writes to admins."""
import ast
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'homeassistant/custom_components/callwebhook/__init__.py'

class View:
    def json(self, payload, status_code=200):
        return status_code, payload

class Forbidden(Exception):
    pass

class CredentialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'secrets.yaml'
        nodes = [n for n in ast.parse(SOURCE.read_text()).body
                 if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in
                 ('read_secret', 'save_fritz_credentials', 'CallWebhookFritzCredentialsView')]
        self.ns = dict(Path=Path, json=json, os=os, HomeAssistantView=View,
                       web=SimpleNamespace(HTTPForbidden=Forbidden), SECRETS_FILE=self.path,
                       _fritz_credentials_lock=asyncio.Lock())
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.ns)

    def save(self, password='new-password'):
        self.ns['save_fritz_credentials'](self.path, 'user', password)

    def test_special_characters_roundtrip_and_private_mode(self):
        password = ' # ü : " \\ newline\nend\' '
        self.save(password)
        self.assertEqual(self.ns['read_secret'](self.path, 'fritz_callwebhook_password'), password)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_preserves_unrelated_comments_tags_and_multiline(self):
        other = '# keep me\nother: !env_var SOMETHING\ntext: |\n  first\n  second\n'
        self.path.write_text(other + 'fritz_callwebhook_user: old\nfritz_callwebhook_password: |\n  old\n  password\n')
        self.save()
        self.assertTrue(self.path.read_text().startswith(other))
        self.save('changed')
        self.assertEqual(self.path.read_text().count('fritz_callwebhook_user:'), 1)

    def test_duplicates_are_replaced(self):
        self.path.write_text('fritz_callwebhook_user: old\nfritz_callwebhook_user: duplicate\n')
        self.save()
        self.assertEqual(self.ns['read_secret'](self.path, 'fritz_callwebhook_user'), 'user')

    def test_unsafe_yaml_is_unchanged(self):
        for source in ('broken: [', '{other: value}', 'fritz_callwebhook_user: &shared old\nother: *shared\n',
                       'other: &shared old\nfritz_callwebhook_user: *shared\n'):
            with self.subTest(source=source):
                self.path.write_text(source)
                with self.assertRaises(ValueError):
                    self.save()
                self.assertEqual(self.path.read_text(), source)

    def test_symlink_is_unchanged(self):
        target = self.path.with_name('target.yaml')
        target.write_text('other: untouched\n')
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(target.read_text(), 'other: untouched\n')

    async def post(self, payload, admin=True):
        async def read(): return payload
        async def executor(fn, *args): return fn(*args)
        request = SimpleNamespace(json=read, app={'hass': SimpleNamespace(async_add_executor_job=executor)},
                                  get=lambda key: SimpleNamespace(is_admin=admin))
        with patch.dict(sys.modules, {'homeassistant.components.http.const': SimpleNamespace(KEY_HASS_USER='hass_user')}):
            return await self.ns['CallWebhookFritzCredentialsView']().post(request)

    async def test_endpoint_requires_admin_and_never_returns_credentials(self):
        payload = {'username': 'user', 'password': 'sensitive-test-value'}
        with self.assertRaises(Forbidden):
            await self.post(payload, admin=False)
        self.assertFalse(self.path.exists())
        self.assertEqual(await self.post(payload), (200, {'ok': True}))
        before = self.path.read_text()
        status, response = await self.post(dict(payload, filename='other.yaml'))
        self.assertEqual(status, 400)
        self.assertNotIn(payload['password'], str(response))
        self.assertEqual(self.path.read_text(), before)
