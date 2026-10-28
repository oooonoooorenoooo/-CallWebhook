import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).parents[1] / 'prepare_asterisk.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

PJSIP = '[transport-udp]\ntype=transport\n[fritz3-endpoint]\ntype=endpoint\n[callwebhook-ios]\nmax_contacts=1\n'
EXTENSIONS = '''[from-fritz]
 same => n,Dial(${PJSIP_DIAL_CONTACTS(callwebhook-ios)},60,b(callwebhook-push-header^s^1(${CW_ID}^${CW_LINE})))
 same => n(done),Hangup()
[callwebhook-push-header]
exten => s,1,Return()
'''


class AsteriskTests(unittest.TestCase):
    def test_separate_endpoint_and_landline_only_context(self):
        pjsip, extensions = module.prepare(PJSIP, EXTENSIONS, 'a' * 32)
        self.assertTrue(pjsip.startswith(PJSIP))
        self.assertIn('context=from-callwebhook-mac', pjsip)
        context = extensions.split('[from-callwebhook-mac]')[1]
        self.assertEqual(context.count('@fritz3-endpoint'), 2)
        self.assertNotIn('fritz1', context)
        self.assertNotIn('fritz2', context)
        self.assertNotIn('easybell', context)
        self.assertNotIn('include', context.splitlines()[2:])

    def test_parallel_ring_scoped_to_line3_and_no_empty_dial(self):
        _, extensions = module.prepare(PJSIP, EXTENSIONS, 'a' * 32)
        self.assertIn('"${CW_LINE}"="3"', extensions)
        self.assertIn('${CW_TARGETS}&${CW_MAC}', extensions)
        self.assertIn('Set(CW_TARGETS=${CW_TARGETS}${CW_MAC})', extensions)
        self.assertIn('GotoIf($["${CW_TARGETS}"=""]?done)', extensions)
        self.assertIn('b(callwebhook-push-header^s^1(${CW_ID}^${CW_LINE}))', extensions)

    def test_reject_injection_duplicate_and_unrecognized_config(self):
        for password in ('short', 'a' * 24 + '\ncontext=evil', 'a' * 24 + ';'):
            with self.assertRaises(ValueError): module.prepare(PJSIP, EXTENSIONS, password)
        with self.assertRaises(ValueError): module.prepare(PJSIP, '', 'a' * 32)
        pjsip, ext = module.prepare(PJSIP, EXTENSIONS, 'a' * 32)
        with self.assertRaises(ValueError): module.prepare(pjsip, ext, 'a' * 32)
