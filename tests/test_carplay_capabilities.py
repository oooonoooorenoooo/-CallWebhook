import unittest
from scripts.configure_carplay import configure, OPTIONAL

class CapabilityTests(unittest.TestCase):
    def test_pending_approval_keeps_existing_capabilities(self):
        original = {'aps-environment': '$(APS_ENVIRONMENT)', 'com.apple.developer.calling-app': True}
        self.assertEqual(configure({'Entitlements': {}}, original), original)

    def test_only_actual_boolean_grants_are_enabled(self):
        result = configure({'Entitlements': {OPTIONAL[0]: True, OPTIONAL[1]: 'true'}}, {OPTIONAL[1]: True})
        self.assertEqual(result, {OPTIONAL[0]: True})

    def test_both_grants(self):
        values = dict.fromkeys(OPTIONAL, True)
        self.assertEqual(configure({'Entitlements': values}, {}), values)
