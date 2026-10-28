"""A Mac-only merge must not publish an iPhone IPA."""
from pathlib import Path
import unittest


class ReleaseScopeTests(unittest.TestCase):
    def test_mac_changes_do_not_request_iphone_release(self):
        workflow = (Path(__file__).parents[2] / '.github/workflows/build-ios.yml').read_text()
        expression = next(line.strip()[7:] for line in workflow.splitlines() if line.strip().startswith("return path.startswith("))
        namespace = {}
        exec('def backend_only(path):\n    return ' + expression, namespace)
        scope = namespace['backend_only']
        for path in ['macOS/project.yml', 'macOS/CallWebhookMac/MacSIPClient.swift', '.github/workflows/build-macos.yml']:
            self.assertTrue(scope(path), path)
        for path in ['CallWebhook/SIPService.swift', 'project.yml']:
            self.assertFalse(scope(path), path)
