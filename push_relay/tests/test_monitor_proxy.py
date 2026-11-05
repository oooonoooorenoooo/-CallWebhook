"""Execute the isolated HA view without requiring a complete HA installation."""
import ast
import asyncio
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
        namespace['push_relay_target'].assert_not_called()
        tree = ast.parse(SOURCE.read_text())
        routes = next(n.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='RELAY_PUBLIC_ROUTES' for t in n.targets))
        self.assertNotIn(('GET','comatalarm/v1/monitor'),ast.literal_eval(routes))

    async def test_admin_uses_server_side_key_without_leaking_it(self):
        namespace,view = self.load()
        calls=[]
        class Response(Context):
            status=200
            read=AsyncMock(return_value=b'{"flights":[]}')
        class Session(Context):
            def __init__(self,**kw): pass
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
