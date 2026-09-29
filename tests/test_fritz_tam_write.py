import ast
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
import xml.etree.ElementTree as ET
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).parents[1] / 'homeassistant/custom_components/callwebhook/__init__.py'


def load():
    names = {'FritzTAMConfigurationError', 'fritz_web_sid', 'fritz_tam_form', 'tam_numbers_match', 'configure_fritz_tams'}
    nodes = [n for n in ast.parse(SOURCE.read_text()).body if getattr(n, 'name', '') in names]
    ns = dict(ET=ET, urlparse=urlparse, parse_qs=parse_qs, HOST='192.168.178.1')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns


# Synthetic FRITZ!OS form contract; not a copy of vendor firmware or user data.
FORM = '''<form id="main_form" method="POST" action="/fon_devices/edit_tam.lua">
<input name="TamNr" type="hidden" value="1">
<input name="tam_name" value="Büro &amp; Familie">
<input type="radio" name="num_selection" value="all_nums" checked>
<input type="radio" name="num_selection" value="sel_nums">
<input type="checkbox" name="num_1" value="03010001" checked>
<input type="checkbox" name="num_2" value="03010002">
<input type="checkbox" name="num_3" value="03010003">
<select name="call_delay"><option value="0">Sofort</option><option value="25" selected>25 s</option></select>
<select name="rec_len"><option value="60">60</option><option value="180" selected>180</option></select>
<input type="radio" name="operation_mode" value="rec">
<input type="radio" name="operation_mode" value="timectrl" checked>
<input name="use_remote" type="checkbox" checked><input name="pin" value="4821">
<input name="email_send" type="checkbox" checked><input name="email_send_del_call" type="checkbox">
<select name="mail_type"><option value="custom" selected>custom</option></select>
<input name="email_addr" value="mail@example.test"><input name="usb_usage" type="checkbox" checked>
<input name="unrelated_disabled" disabled value="not sent">
<button name="delete" type="submit">Delete</button><button name="apply" type="submit">Save</button></form>'''
TIMER = '<rule id="1" enabled="1"><item time="0800" action="1" day="31"/><item time="1730" action="0" day="31"/></rule>'


class TAMWriteTests(unittest.TestCase):
    def setUp(self): self.ns = load()

    def sid_response(self, value):
        from xml.sax.saxutils import escape
        xml = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            '<s:Body><u:X_AVM-DE_CreateUrlSIDResponse xmlns:u="urn:dslforum-org:service:DeviceConfig:1">'
            '<NewX_AVM-DE_UrlSID>' + escape(value) + '</NewX_AVM-DE_UrlSID>'
            '</u:X_AVM-DE_CreateUrlSIDResponse></s:Body></s:Envelope>')
        response = SimpleNamespace(content=xml.encode(), raise_for_status=lambda:None)
        post = Mock(return_value=response)
        self.ns.update(requests=SimpleNamespace(post=post), get_auth=lambda:'digest-auth')
        return post

    def test_web_session_accepts_native_sid_assignment(self):
        post = self.sid_response('sid=1234567890abcdef')
        self.assertEqual(self.ns['fritz_web_sid'](), '1234567890abcdef')
        self.assertEqual(post.call_args.args[0], 'http://192.168.178.1:49000/upnp/control/deviceconfig')
        self.assertEqual(post.call_args.kwargs['auth'], 'digest-auth')

    def test_web_session_accepts_query_or_full_url_without_following_it(self):
        for value in ('?sid=1234567890abcdef', ' http://fritz.box/?sid=1234567890abcdef ',
                      '/index.lua?sid=1234567890abcdef&lang=de'):
            with self.subTest(value=value):
                post = self.sid_response(value)
                self.assertEqual(self.ns['fritz_web_sid'](), '1234567890abcdef')
                self.assertEqual(post.call_count, 1)
                self.assertIn('192.168.178.1:49000', post.call_args.args[0])

    def test_web_session_rejects_missing_invalid_or_ambiguous_ids(self):
        for value in ('', 'sid=0000000000000000', 'sid=bad', 'session=1234567890abcdef',
                      'sid=1234567890abcdef&sid=fedcba0987654321', 'sid=1234567890abcdefjunk'):
            with self.subTest(value=value):
                self.sid_response(value)
                with self.assertRaises(self.ns['FritzTAMConfigurationError']) as caught:
                    self.ns['fritz_web_sid']()
                self.assertNotIn('1234567890abcdef', str(caught.exception))

    def test_selected_number_written_with_original_options_and_calendar(self):
        fields = self.ns['fritz_tam_form'](FORM, 1, ['030 10002'], TIMER)
        self.assertEqual({k:v for k,v in fields.items() if k.startswith('num_')},
            {'num_selection':'sel_nums', 'num_2':'03010002'})
        self.assertEqual(fields['tam_name'], 'Büro & Familie')
        self.assertEqual(fields['call_delay'], '25')
        self.assertEqual(fields['rec_len'], '180')
        self.assertEqual(fields['pin'], '4821')
        self.assertEqual(fields['email_addr'], 'mail@example.test')
        self.assertEqual(fields['usb_usage'], 'on')
        self.assertEqual(fields['timer_item_0'], '0800;1;31')
        self.assertEqual(fields['timer_item_1'], '1730;0;31')
        self.assertNotIn('email_send_del_call', fields)
        self.assertNotIn('delete', fields)
        self.assertNotIn('unrelated_disabled', fields)

    def test_unreadable_form_number_or_timer_never_produces_write(self):
        for html, index, numbers, timer in [
            (FORM, 0, ['03010002'], TIMER), (FORM, 1, ['3'], TIMER),
            (FORM, 1, ['03099999'], TIMER), (FORM, 1, ['03010002'], ''),
            (FORM, 1, ['03010002'], '<rule id="0"/>'),
            (FORM, 1, ['03010002'], 'not xml'), ('<html>login</html>', 1, ['03010002'], TIMER),
        ]:
            with self.subTest(index=index, numbers=numbers, timer=timer):
                with self.assertRaises(self.ns['FritzTAMConfigurationError']):
                    self.ns['fritz_tam_form'](html, index, numbers, timer)

    def test_readback_requires_exact_nonempty_number_set(self):
        matches = self.ns['tam_numbers_match']
        self.assertFalse(matches({'NewPhoneNumbers':''}, ['03010002']))
        self.assertFalse(matches({'NewPhoneNumbers':'3'}, ['03010002']))
        self.assertFalse(matches({'NewPhoneNumbers':'03010001,03010002'}, ['03010002']))
        self.assertTrue(matches({'NewPhoneNumbers':'03010001,03010002'}, ['03010002','03010001']))

    def test_real_write_path_then_independent_readback(self):
        read = Mock(side_effect=[{'NewPhoneNumbers':'','NewEnable':'1'},
            {'NewPhoneNumbers':'03010002','NewEnable':'1'}])
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        posted = []
        def request(method, url, **kw):
            if url.endswith('edit_tam.lua'):
                return SimpleNamespace(status_code=200, text=FORM)
            if url.endswith('query.lua'):
                return SimpleNamespace(status_code=200, json=lambda:{'cw_timer':TIMER})
            if url.endswith('data.lua'):
                posted.append(kw['data'])
                return SimpleNamespace(status_code=200, json=lambda:{'data':{'apply':'ok'}})
            self.fail(url)
        session.request.side_effect = request
        self.ns.update(requests=SimpleNamespace(Session=lambda:session), fritz_web_sid=lambda:'1234567890abcdef', read_tam_info=read)
        self.ns['configure_fritz_tams']({1:['03010002']}, lambda message:None)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]['num_2'], '03010002')
        self.assertEqual(posted[0]['timer_item_0'], '0800;1;31')
        # A successful HTTP/write reply cannot mask unchanged router state.
        read.side_effect = [{'NewPhoneNumbers':'','NewEnable':'1'}]*2
        with self.assertRaisesRegex(self.ns['FritzTAMConfigurationError'], 'zurückgelesene'):
            self.ns['configure_fritz_tams']({1:['03010002']}, lambda message:None)

    def test_twofactor_requires_actual_confirmation_before_resubmission(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                ns = load()
                session = Mock()
                session.__enter__ = Mock(return_value=session)
                session.__exit__ = Mock(return_value=False)
                submissions = []
                def request(method, url, **kw):
                    if url.endswith('edit_tam.lua'): return SimpleNamespace(status_code=200, text=FORM)
                    if url.endswith('query.lua'): result = {'cw_timer':TIMER}
                    elif url.endswith('data.lua'):
                        submissions.append(kw['data'].copy())
                        result = {'data':{'apply':'twofactor','twofactor':'button'}} if len(submissions)==1 else {'data':{'apply':'ok'}}
                    else: result = {'done':True,'active':confirmed}
                    return SimpleNamespace(status_code=200, json=lambda:result)
                session.request.side_effect = request
                ns.update(requests=SimpleNamespace(Session=lambda:session), fritz_web_sid=lambda:'1234567890abcdef',
                    read_tam_info=Mock(side_effect=[{'NewPhoneNumbers':'','NewEnable':'1'},
                        {'NewPhoneNumbers':'03010002','NewEnable':'1'}]), time=SimpleNamespace(monotonic=lambda:0, sleep=lambda _:None))
                if confirmed:
                    ns['configure_fritz_tams']({1:['03010002']}, lambda message:None)
                    self.assertEqual(len(submissions), 2)
                    self.assertIn('twofactor', submissions[1])
                else:
                    with self.assertRaisesRegex(ns['FritzTAMConfigurationError'], 'abgebrochen'):
                        ns['configure_fritz_tams']({1:['03010002']}, lambda message:None)
                    self.assertEqual(len(submissions), 1)
