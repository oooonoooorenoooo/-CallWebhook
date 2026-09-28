import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('provision', Path(__file__).parents[1] / 'callwebhook_bootstrap/provision.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addons = self.root / 'addon_configs'
        (self.addons / '3e533915_asterisk').mkdir(parents=True)
        self.request = self.root / 'request.json'
        self.result = self.root / 'result.json'

    def run_request(self, addon='3e533915_asterisk'):
        self.request.write_text(json.dumps({'job_id': 'job-1', 'addon': addon,
            'pjsip': '[callwebhook-ios]\ntype=aor\n',
            'extensions': '[from-callwebhook-ios]\n'}))
        return module.provision(self.request, self.result, self.addons)

    def test_writes_real_mount_and_acknowledges_job(self):
        result = self.run_request()
        self.assertTrue(result['ok'])
        self.assertEqual(result['job_id'], 'job-1')
        self.assertFalse(self.request.exists())
        self.assertEqual(len(result['files']), 2)
        for file in result['files']:
            self.assertEqual(Path(file).stat().st_mode & 0o777, 0o600)

    def test_missing_mount_is_not_created(self):
        result = self.run_request('deadbeef_asterisk')
        self.assertFalse(result['ok'])
        self.assertFalse((self.addons / 'deadbeef_asterisk').exists())

    def test_rejects_path_traversal(self):
        self.assertFalse(self.run_request('../outside_asterisk')['ok'])

    def test_failed_second_file_restores_first(self):
        custom = self.addons / '3e533915_asterisk/asterisk/custom'
        custom.mkdir(parents=True)
        (custom / 'pjsip.conf').write_text('existing configuration')
        outside = self.root / 'outside'
        outside.write_text('untouched')
        (custom / 'extensions.conf').symlink_to(outside)
        self.assertFalse(self.run_request()['ok'])
        self.assertEqual((custom / 'pjsip.conf').read_text(), 'existing configuration')
        self.assertEqual(outside.read_text(), 'untouched')


if __name__ == '__main__':
    unittest.main()
