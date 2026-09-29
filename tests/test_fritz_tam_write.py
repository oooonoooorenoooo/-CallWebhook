import ast
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
import xml.etree.ElementTree as ET
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).parents[1] / 'homeassistant/custom_components/callwebhook/__init__.py'


def load():
    names = {'fritz_tam_wizard_form', 'create_fritz_tam', 'FritzTAMConfigurationError', 'fritz_web_sid', 'fritz_tam_form', 'tam_numbers_match', 'configure_fritz_tams', 'FritzConfirmation', 'wait_fritz_confirmation'}
    nodes = [n for n in ast.parse(SOURCE.read_text()).body if getattr(n, 'name', '') in names]
    ns = dict(ET=ET, urlparse=urlparse, parse_qs=parse_qs, HOST='192.168.178.1', secrets=secrets, time=time)
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


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.ns = load()
        self.factor = self.ns['FritzConfirmation']()
        self.factor.owner = 'admin-a'

    def test_available_choices_and_server_phone_code(self):
        self.factor.begin('button,dtmf,googleauth;9876', {'isAvailable':True,'isConfigured':True})
        state = self.factor.snapshot()
        self.assertEqual(state['methods'], ['button','phone','otp'])
        self.assertEqual(state['phone_code'], '*19876')
        self.factor.begin('button,dtmf,googleauth;9876', {'isAvailable':True,'isConfigured':False})
        self.assertEqual(self.factor.snapshot()['methods'], ['button','phone'])
        self.factor.begin('button,dtmf;invalid', {})
        self.assertEqual(self.factor.snapshot()['methods'], ['button'])

    def test_otp_is_bound_to_owner_challenge_and_availability(self):
        self.factor.begin('button,googleauth', {'isAvailable':True,'isConfigured':True})
        key = self.factor.snapshot()['id']
        for owner, challenge, code in [('admin-b',key,'123456'), ('admin-a','stale','123456'), ('admin-a',key,'bad')]:
            with self.assertRaises(ValueError):
                self.factor.submit(owner, {'id':challenge,'action':'otp','code':code})
        self.factor.submit('admin-a', {'id':key,'action':'otp','code':'123456'})
        self.assertNotIn('123456', str(self.factor.snapshot()))
        self.assertEqual(self.factor.take(), ('otp','123456'))
        self.assertIsNone(self.factor.take())
        self.factor.begin('button', {})
        with self.assertRaises(ValueError):
            self.factor.submit('admin-a', {'id':self.factor.snapshot()['id'],'action':'otp','code':'123456'})
        self.factor.clear()
        self.assertEqual(self.factor.snapshot(), {})

    def test_wrong_otp_can_retry_but_only_active_state_confirms(self):
        submitted = []
        checks = []
        def request(path, fields, method):
            if 'tfa_googleauth_info' in fields:
                result = {'googleauth':{'isAvailable':True,'isConfigured':True}}
            elif 'tfa_googleauth' in fields:
                submitted.append(fields['tfa_googleauth'])
                result = {'err':1 if len(submitted)==1 else 0}
            else:
                checks.append(True)
                if len(checks) <= 2:
                    if len(checks)==2: self.assertIn('nicht akzeptiert', self.factor.snapshot()['error'])
                    self.factor.submit('admin-a', {'id':self.factor.snapshot()['id'],'action':'otp',
                        'code':'111111' if len(checks)==1 else '222222'})
                result = {'done':len(checks)==3, 'active':len(checks)==3}
            return SimpleNamespace(json=lambda:result)
        self.ns['time'] = SimpleNamespace(monotonic=lambda:0, sleep=lambda _:None)
        self.ns['wait_fritz_confirmation'](request, 'button,googleauth', self.factor)
        self.assertEqual(submitted, ['111111','222222'])
        self.assertEqual(len(checks), 3)
        self.assertEqual(self.factor.snapshot(), {})

    def test_cancel_and_expiry_clear_pending_code_and_stop_router_request(self):
        for cancel in (True,False):
            with self.subTest(cancel=cancel):
                clock = [0]
                calls = []
                def request(path, fields, method):
                    calls.append(fields)
                    if cancel and 'tfa_active' in fields:
                        self.factor.submit('admin-a', {'id':self.factor.snapshot()['id'],'action':'cancel'})
                    return SimpleNamespace(json=lambda:{'done':False})
                self.ns['time'] = SimpleNamespace(monotonic=lambda:clock[0], sleep=lambda _:clock.__setitem__(0,clock[0]+61))
                with self.assertRaises(self.ns['FritzTAMConfigurationError']):
                    self.ns['wait_fritz_confirmation'](request, 'button', self.factor)
                self.assertEqual(calls[-1], {'tfa_cancel':''})
                self.assertEqual(self.factor.snapshot(), {})


class TAMCreationTests(unittest.TestCase):
    def form(self, stage, body='', index=0):
        return f'''<form name="mainform" action="/assis/assi_tam_intern.lua">
        <input type="hidden" name="New_CurrSide" value="{stage}">
        <input type="hidden" name="Old_WhoAmI" value="/assis/assi_tam_intern.lua">
        <input type="hidden" name="Old_TamNr" value="{index}">
        <input type="hidden" name="Old_UseUsbStick" value="1">
        <input type="hidden" name="sid" value="secret">
        {body}</form>'''

    def setup_wizard(self, *, mismatch=False, confirmation=False, wrong_summary=False):
        ns = load()
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        posted = []
        inventory = {**{f'd{i}':'0' for i in range(5)}, **{f'n{i}':'' for i in range(5)}}
        def response(method, url, **kwargs):
            fields = kwargs.get('data', kwargs.get('params', {}))
            text, data = '', {}
            if url.endswith('query.lua'):
                data = {'display':'1'} if 'display' in fields else inventory
            elif url.endswith('assi_tam_intern.lua'):
                if method == 'GET':
                    text = self.form('AssiTamInternEinrichten')
                elif fields['New_CurrSide'] == 'AssiTamInternEinrichten':
                    self.assertEqual(fields['New_OperationMode'], '1')
                    self.assertEqual(fields['New_Delay'], '6')
                    self.assertEqual(fields['New_TamName'], 'CallWebhook SIM 1')
                    self.assertEqual(fields['Old_UseUsbStick'], '1')
                    text = self.form('AssiTamInternIncoming', '''
                    <input type="radio" name="NewFnc_ConnectToAll" value="T" checked>
                    <input type="checkbox" name="NewFnc_Sip0" id="nr0" checked><label for="nr0">030 10001</label>
                    <input type="checkbox" name="NewFnc_Sip2" id="nr2"><label for="nr2">030 10002<span> ●</span></label>
                    <label for="nr2">Internetrufnummer (2)</label>''')
                else:
                    self.assertEqual(fields['NewFnc_ConnectToAll'], 'F')
                    self.assertNotIn('NewFnc_Sip0', fields)
                    self.assertEqual(fields['NewFnc_Sip2'], 'on')
                    text = self.form('AssiTamInternSummary', f'''
                    <input type="hidden" name="OldFnc_ConnectToAll" value="{'T' if wrong_summary else 'F'}">
                    <input type="hidden" name="OldFnc_IncomingNr1" value="Sip2">
                    <input type="hidden" name="OldFnc_IncomingNr2" value="">''')
            elif url.endswith('data.lua'):
                posted.append(fields)
                data = {'data': {'Submit_Save': 'twofactor', 'twofactor': 'button'}} if confirmation and len(posted)==1 else {'data': {'Submit_Save': 'ok'}}
            return SimpleNamespace(status_code=200, text=text, json=lambda:data)
        session.request.side_effect = response
        ns.update(requests=SimpleNamespace(Session=lambda:session), fritz_web_sid=lambda:'sid',
                  read_tam_info=Mock(return_value={'NewPhoneNumbers':'03010001' if mismatch else '03010002', 'NewEnable':'1'}),
                  wait_fritz_confirmation=Mock())
        return ns, posted, inventory

    def test_creates_visible_mailbox_with_exact_number_and_confirmation(self):
        ns, posted, _ = self.setup_wizard(confirmation=True)
        self.assertEqual(ns['create_fritz_tam'](1, '03010002', lambda _:None), 0)
        self.assertEqual(len(posted), 2)
        ns['wait_fritz_confirmation'].assert_called_once()
        self.assertNotIn('confirmed', posted[0])
        self.assertIn('confirmed', posted[1])
        self.assertEqual(posted[1]['OldFnc_IncomingNr1'], 'Sip2')

    def test_wrong_readback_fails_and_catch_all_summary_never_saved(self):
        ns, posted, _ = self.setup_wizard(mismatch=True)
        with self.assertRaisesRegex(ns['FritzTAMConfigurationError'], 'Zurücklesen'):
            ns['create_fritz_tam'](1, '03010002', lambda _:None)
        self.assertEqual(len(posted), 1)
        ns, posted, _ = self.setup_wizard(wrong_summary=True)
        with self.assertRaises(ns['FritzTAMConfigurationError']):
            ns['create_fritz_tam'](1, '03010002', lambda _:None)
        self.assertEqual(posted, [])

    def test_existing_visible_slot_is_not_overwritten_and_retry_is_idempotent(self):
        ns, posted, inventory = self.setup_wizard()
        inventory['d0'] = '1'
        inventory['n0'] = 'Other mailbox'
        with self.assertRaises(ns['FritzTAMConfigurationError']):
            ns['create_fritz_tam'](1, '03010002', lambda _:None)
        self.assertFalse(posted)
        inventory['n0'] = 'CallWebhook SIM 1'
        self.assertEqual(ns['create_fritz_tam'](1, '03010002', lambda _:None), 0)
        self.assertFalse(posted)

    def test_wizard_rejects_unknown_page_full_router_or_sign_in_form(self):
        ns = load()
        for html in ('<form name="login"></form>', self.form('AssiTamInternEinrichten', index=-1),
                     self.form('AssiTamInternSummary'),
                     self.form('AssiTamInternEinrichten').replace('/assis/assi_tam_intern.lua', '/other.lua')):
            with self.assertRaises(ns['FritzTAMConfigurationError']):
                ns['fritz_tam_wizard_form'](html, 'AssiTamInternEinrichten')


class FreshMailboxSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_new_lines_resolve_real_indexes_before_saving(self):
        import asyncio
        nodes = [node for node in ast.parse(SOURCE.read_text()).body if getattr(node, 'name', '') == 'run_mailbox_setup']
        created, configured, saved = [], [], []
        def create(line, number, report, confirmation):
            created.append((line, number))
            return {1:2, 2:0, 3:4}[line]
        async def executor(fn, *args): return fn(*args)
        ns = dict(_tam_setup_state={}, _tam_confirmation=object(), _refresh_lock=asyncio.Lock(),
                  create_fritz_tam=create, configure_fritz_tams=lambda groups, *args:configured.append(groups),
                  save_setup=lambda *args:saved.append(args), FritzTAMConfigurationError=ValueError)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
        hass = SimpleNamespace(loop=SimpleNamespace(call_soon_threadsafe=lambda fn,*args:fn(*args)), async_add_executor_job=executor)
        payload = {**{f'mailbox_tam_{i}':-2 for i in range(1,4)}, 'line_numbers':{'1':'03010001','2':'03010002','3':'03010003'}}
        await ns['run_mailbox_setup'](hass, payload, {})
        self.assertEqual(created, [(1,'03010001'),(2,'03010002'),(3,'03010003')])
        self.assertEqual(configured, [{2:['03010001'],0:['03010002'],4:['03010003']}])
        self.assertEqual(saved, [(2,0,4)])
        self.assertEqual(ns['_tam_setup_state']['state'], 'completed')
        self.assertEqual(ns['_tam_setup_state']['assignments'], {'mailbox_tam_1':2,'mailbox_tam_2':0,'mailbox_tam_3':4})
        self.assertEqual(payload['mailbox_tam_1'], -2)
        # No persisted success if a subsequent router operation fails.
        ns['create_fritz_tam'] = Mock(side_effect=ValueError('router rejected creation'))
        saved.clear()
        await ns['run_mailbox_setup'](hass, payload, {})
        self.assertEqual(ns['_tam_setup_state']['state'], 'error')
        self.assertFalse(saved)
