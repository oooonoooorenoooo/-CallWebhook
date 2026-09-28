"""Verify per-line assignments reach persistent backend configuration."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

SOURCE = Path(__file__).parents[1] / 'homeassistant/custom_components/callwebhook/__init__.py'


class View:
    def json(self, payload, status_code=200):
        return status_code, payload


class MailboxSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        selected = [node for node in ast.parse(SOURCE.read_text()).body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                    and node.name in ('save_setup', 'get_configured_tams', 'CallWebhookMailboxSetupView')]
        self.ns = dict(json=json, BASE_DIR=path, SETUP_FILE=path / 'setup.json',
                       DEFAULT_TAMS=('1', '2'), HomeAssistantView=View,
                       _refresh_lock=asyncio.Lock(), _asterisk_setup_state={'state': 'done'})
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), self.ns)

    async def post(self, payload):
        async def read():
            return payload
        async def executor(fn, *args):
            return fn(*args)
        request = SimpleNamespace(json=read, app={'hass': SimpleNamespace(async_add_executor_job=executor)})
        return await self.ns['CallWebhookMailboxSetupView']().post(request)

    async def test_shared_mailbox_retains_line_mapping_and_fetches_once(self):
        assignments = dict(mailbox_tam_1=2, mailbox_tam_2=-1, mailbox_tam_3=2)
        status, response = await self.post(assignments)
        self.assertEqual(status, 200)
        self.assertEqual(response['assignments'], assignments)
        saved = json.loads(self.ns['SETUP_FILE'].read_text())
        self.assertEqual(saved, {'tams': [2], 'assignments': assignments})
        self.assertEqual(self.ns['get_configured_tams'](), ('2',))

    async def test_none_clears_previous_selection(self):
        await self.post(dict(mailbox_tam_1=0, mailbox_tam_2=1, mailbox_tam_3=2))
        await self.post(dict(mailbox_tam_1=-1, mailbox_tam_2=-1, mailbox_tam_3=-1))
        self.assertEqual(self.ns['get_configured_tams'](), ())

    async def test_invalid_payload_cannot_overwrite_selection(self):
        valid = dict(mailbox_tam_1=0, mailbox_tam_2=1, mailbox_tam_3=-1)
        await self.post(valid)
        before = self.ns['SETUP_FILE'].read_text()
        for payload in ([], {}, {**valid, 'mailbox_tam_1': True},
                        {**valid, 'mailbox_tam_1': 10}, {**valid, 'mailbox_tam_1': -2}):
            status, _ = await self.post(payload)
            self.assertEqual(status, 400)
            self.assertEqual(self.ns['SETUP_FILE'].read_text(), before)

    async def test_provisioning_conflict_is_retryable_without_writing(self):
        self.ns['_asterisk_setup_state']['state'] = 'running'
        status, _ = await self.post(dict(mailbox_tam_1=0, mailbox_tam_2=-1, mailbox_tam_3=-1))
        self.assertEqual(status, 409)
        self.assertFalse(self.ns['SETUP_FILE'].exists())


if __name__ == '__main__':
    unittest.main()
