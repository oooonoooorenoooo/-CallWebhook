"""Exercise the actual readiness handlers without a full Home Assistant install."""
import ast
import enum
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).parents[1] / 'homeassistant/custom_components/callwebhook/__init__.py'


class CoreState(enum.Enum):
    starting = 'STARTING'
    running = 'RUNNING'
    stopping = 'STOPPING'

    def __str__(self):
        return self.value


def load_handlers(**extra):
    tree = ast.parse(SOURCE.read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in ('bootstrap_state', 'setup_readiness', 'async_setup')]
    namespace = {'CoreState': CoreState, 'HomeAssistant': object, **extra}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def check_state(self, ha_state, bootstrap):
        async def executor(function):
            return function()
        query = Mock(return_value=bootstrap)
        handlers = load_handlers()
        handlers['bootstrap_state'] = query
        result = await handlers['setup_readiness'](SimpleNamespace(state=ha_state, async_add_executor_job=executor))
        return result, query

    async def test_starting_is_not_ready_and_does_not_query_supervisor(self):
        result, query = await self.check_state(CoreState.starting, 'stopped')
        self.assertFalse(result['ready_for_asterisk'])
        query.assert_not_called()

    async def test_running_but_bootstrap_busy_keeps_waiting(self):
        result, _ = await self.check_state(CoreState.running, 'started')
        self.assertFalse(result['ready_for_asterisk'])
        self.assertIn('vorherigen Auftrag', result['message'])

    async def test_only_running_and_stopped_is_ready(self):
        result, _ = await self.check_state(CoreState.running, 'stopped')
        self.assertTrue(result['ready_for_asterisk'])
        for state in ('missing', 'unknown', 'error'):
            result, _ = await self.check_state(CoreState.running, state)
            self.assertFalse(result['ready_for_asterisk'])

    async def test_stopping_never_starts_asterisk(self):
        result, query = await self.check_state(CoreState.stopping, 'stopped')
        self.assertFalse(result['ready_for_asterisk'])
        query.assert_not_called()

    def test_reads_installed_addon_without_store_dependency(self):
        request = Mock(side_effect=[{'addons': [{'slug': '12345678_callwebhook_bootstrap'}]}, {'state': 'stopped'}])
        handlers = load_handlers(supervisor_request=request)
        self.assertEqual(handlers['bootstrap_state'](), 'stopped')
        self.assertEqual([call.args[1] for call in request.call_args_list], ['/addons', '/addons/12345678_callwebhook_bootstrap/info'])

    async def test_mailbox_lifetime_task_cannot_block_startup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            names = ('CallWebhookFritzCredentialsView', 'CallWebhookMailboxSetupView', 'CallWebhookSetupStatusView', 'CallWebhookAsteriskSetupView',
                     'CallWebhookAsteriskSetupStatusView', 'CallWebhookMailboxView',
                     'CallWebhookMailboxDeleteView', 'CallWebhookMailboxArchiveView',
                     'CallWebhookAudioView', 'CallWebhookArchiveAudioView', 'CallWebhookVoIPView',
                     'CallWebhookVoIPCallView', 'CallWebhookVoIPHookView',
                     'CallWebhookPushRelaySetupView', 'CallWebhookPushRelayHostView', 'CallWebhookPushRelayProxyView')
            async def mailbox_loop(hass):
                pass
            async def executor(function, *args):
                return function(*args)
            scheduled = []
            def background(coroutine, name):
                scheduled.append(name)
                coroutine.close()
            handlers = load_handlers(BASE_DIR=Path(directory), ARCHIVE_DIR=Path(directory) / 'archive',
                mailbox_refresh_loop=mailbox_loop, load_voip=lambda: None, **dict.fromkeys(names, object))
            hass = SimpleNamespace(http=SimpleNamespace(register_view=Mock()), async_create_background_task=background, async_add_executor_job=executor)
            self.assertTrue(await handlers['async_setup'](hass, {}))
            self.assertEqual(scheduled, ['CallWebhook mailbox refresh'])


if __name__ == '__main__':
    unittest.main()
