"""Execute the isolated HA view without requiring a complete HA installation."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from aiohttp import web, ClientTimeout

SOURCE = Path(__file__).resolve().parents[2] / 'homeassistant/custom_components/callwebhook/__init__.py'

class Context:
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass

class MonitorProxyTests(unittest.IsolatedAsyncioTestCase):
    def load(self):
        tree = ast.parse(SOURCE.read_text())
        view = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ComatAlarmMonitorView')
        namespace = dict(HomeAssistantView=object,web=web,urlparse=urlparse,ClientTimeout=ClientTimeout,
            push_relay_target=AsyncMock(return_value='http://123abc-callwebhook-push-relay:8080'))
        exec(compile(ast.Module(body=[view],type_ignores=[]),str(SOURCE),'exec'),namespace)
        return namespace,namespace['ComatAlarmMonitorView']()

    async def test_requires_authenticated_admin_and_keeps_route_out_of_public_proxy(self):
        namespace,view = self.load()
        self.assertTrue(view.requires_auth)
        for user in (None,SimpleNamespace(is_admin=False)):
            with self.assertRaises(web.HTTPForbidden):
                await view.get({'hass_user':user})
            with self.assertRaises(web.HTTPForbidden):
                await view.post({'hass_user':user})
        namespace['push_relay_target'].assert_not_called()
        tree = ast.parse(SOURCE.read_text())
        routes = next(n.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='RELAY_PUBLIC_ROUTES' for t in n.targets))
        self.assertNotIn(('GET','comatalarm/v1/monitor'),ast.literal_eval(routes))
        self.assertNotIn(('POST','comatalarm/v1/monitor'),ast.literal_eval(routes))

    async def test_admin_uses_server_side_key_without_leaking_it(self):
        namespace,view = self.load()
        calls=[]
        class Response(Context):
            status=200
            read=AsyncMock(return_value=b'{"flights":[]}')
        class Session(Context):
            def __init__(self,**kw): pass
            def post(self,url,**kw):
                calls.append((url,kw)); return Response()
            def get(self,url,**kw):
                calls.append((url,kw)); return Response()
        def supervisor(method,path):
            self.assertEqual(path,'/addons/123abc_callwebhook_push_relay/info')
            return {'options':{'comatalarm_setup_key':'private-key-'+'x'*32}}
        namespace.update(ClientSession=Session,supervisor_request=supervisor)
        async def executor(fn,*args): return fn(*args)
        class Request(dict): pass
        req=Request(hass_user=SimpleNamespace(is_admin=True))
        req.query={'day':'2026-11-05'}
        req.app={'hass':SimpleNamespace(async_add_executor_job=executor)}
        result=await view.get(req)
        self.assertEqual(result.status,200)
        self.assertNotIn('private-key',result.text)
        self.assertEqual(calls[0][0],'http://123abc-callwebhook-push-relay:8080/comatalarm/v1/monitor')
        self.assertEqual(calls[0][1]['params'], {'day':'2026-11-05'})
        self.assertFalse(calls[0][1]['allow_redirects'])
        self.assertTrue(calls[0][1]['headers']['X-ComatAlarm-Setup-Key'].startswith('private-key'))

        req.json=AsyncMock(return_value={'action':'delete','key':'a'*64,'unexpected':'untrusted'})
        result=await view.post(req)
        self.assertEqual(result.status,200)
        self.assertEqual(calls[-1][1]['json'], {'action':'delete','key':'a'*64})
        self.assertNotIn('private-key',result.text)
        req.json=AsyncMock(return_value={'action':'delete','key':'../bad'})
        with self.assertRaises(web.HTTPBadRequest): await view.post(req)

    async def test_historic_diagnostic_forwards_only_fixed_fields_and_preserves_provider_evidence(self):
        namespace,view = self.load()
        calls=[]
        response_body=b''
        class Response(Context):
            status=200
            async def read(self): return response_body
        class Session(Context):
            def __init__(self,**kw):
                self_timeout=kw['timeout'].total
                if self_timeout != 20: raise AssertionError('diagnostic uses bounded command timeout')
            def post(self,url,**kw):
                calls.append((url,kw)); return Response()
        namespace.update(ClientSession=Session,supervisor_request=lambda *args:{'options':{'comatalarm_setup_key':'private-key-'+'x'*32}})
        async def executor(fn,*args): return fn(*args)
        class Request(dict): pass
        req=Request(hass_user=SimpleNamespace(is_admin=True))
        req.query={}
        req.app={'hass':SimpleNamespace(async_add_executor_job=executor)}
        for status in (200,401,429,500):
            for event_types in ('gate_arrival','all'):
                with self.subTest(status=status,event_types=event_types):
                    diagnostic={'http_status':status,'params':{'flight_ids':'42a83e5a','event_types':event_types},
                        'response':{'data':[{'fr24_id':'42a83e5a','events':[{'type':'gate_arrival',
                        'timestamp':'2023-01-27T06:16:18Z','details':{'gate_ident':None,'gate_lat':55.61253,'gate_lon':12.63116}}]}]}}
                    response_body=json.dumps({'ok':status==200,'diagnostic':diagnostic},indent=2).encode()
                    req.json=AsyncMock(return_value={'action':'fr24_historic_events','fr24_id':' 42a83e5a ',
                        'event_types':event_types,'url':'https://untrusted.invalid/','token':'untrusted','key':'a'*64})
                    result=await view.post(req)
                    self.assertEqual(result.status,200)
                    self.assertEqual(result.body,response_body)
                    self.assertEqual(result.headers['Cache-Control'],'no-store')
                    self.assertNotIn('private-key',result.text)
                    self.assertEqual(calls[-1][0],'http://123abc-callwebhook-push-relay:8080/comatalarm/v1/monitor')
                    self.assertEqual(calls[-1][1]['json'],{'action':'fr24_historic_events','fr24_id':'42a83e5a','event_types':event_types})
                    self.assertFalse(calls[-1][1]['allow_redirects'])
        req.json=AsyncMock(return_value={'action':'fr24_historic_events','fr24_id':'opaque-ID_1'})
        await view.post(req)
        self.assertEqual(calls[-1][1]['json']['event_types'],'gate_arrival')
        self.assertEqual(calls[-1][1]['json']['fr24_id'],'opaque-ID_1')

    async def test_historic_diagnostic_rejects_invalid_parameters_before_resolving_addon(self):
        namespace,view=self.load()
        class Request(dict): pass
        req=Request(hass_user=SimpleNamespace(is_admin=True))
        invalid=[{'fr24_id':value} for value in (None,False,42,[],{},'', ' ', 'x'*65,'../leg','a,b','id?event_types=all','id\nnext','ä')]
        invalid += [{'fr24_id':'42a83e5a','event_types':value} for value in (None,False,[],{},'landed','all,gate_arrival')]
        for params in invalid:
            with self.subTest(params=params):
                req.json=AsyncMock(return_value={'action':'fr24_historic_events',**params})
                with self.assertRaises(web.HTTPBadRequest): await view.post(req)
        namespace['push_relay_target'].assert_not_called()
