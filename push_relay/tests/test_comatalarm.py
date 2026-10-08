import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from aiohttp.test_utils import TestClient, TestServer
from push_relay.server import Relay, application
from push_relay.comatalarm import FlightProvider

class ComatAlarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, COMATALARM_SETUP_KEY='s'*64)
        self.env.start()
        self.relay = Relay(str(Path(self.temp.name)/'relay.db'), 'TEAM.app', b'', AsyncMock())
        self.comat = self.relay.comatalarm
        self.comat.provider = AsyncMock()
        self.comat.provider.lookup.return_value = {}
        self.comat.sender = AsyncMock(return_value=True)
        app = application(self.relay)
        app.cleanup_ctx.clear() # Deterministic clock; tests drive tick explicitly.
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.id = str(uuid4())
        self.now = time.time()
        self.flight = dict(id='test-flight', number='TK1729', reference=self.now-100)
        response = await self.register()
        self.headers = {'Authorization':'Bearer '+(await response.json())['credential']}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()
        self.env.stop()

    async def register(self, key='s'*64, environment='production'):
        return await self.client.post('/comatalarm/v1/register', headers={'X-ComatAlarm-Setup-Key':key}, json={'device_id':self.id,'token':'a'*64,'environment':environment})

    async def watch(self, **kw):
        body = dict(flights=[self.flight],preferences={},foreground=False,fr24_token='private-api-token')
        body.update(kw)
        return await self.client.post('/comatalarm/v1/watch', headers=self.headers, json=body)

    async def test_authentication_environment_and_credential_isolation(self):
        for key in ('', 'x'*64): self.assertEqual((await self.register(key)).status,401)
        with patch.dict(os.environ, COMATALARM_SETUP_KEY=''):
            self.assertEqual((await self.register('')).status,401)
        self.assertEqual((await self.register(environment='development')).status,400)
        self.assertEqual((await self.client.get('/v1/registration',headers=self.headers)).status,401)
        self.assertEqual((await self.client.get('/comatalarm/v1/state')).status,401)

    async def test_watch_validation_and_no_secret_in_state(self):
        for fields in (dict(flights=[self.flight]*3), dict(flights=[self.flight]*2), dict(preferences={'enabled':'true'}), dict(foreground=1), dict(flights=[dict(self.flight, reference=0)]),dict(fr24_token='x\ny')):
            self.assertEqual((await self.watch(**fields)).status,400, fields)
        self.assertEqual((await self.watch()).status,200)
        text = await (await self.client.get('/comatalarm/v1/state',headers=self.headers)).text()
        self.assertNotIn('private-api-token',text)
        self.assertNotIn('a'*64,text)
        self.assertIn('TK1729',text)

    async def test_foreground_takeover_background_events_and_no_duplicates(self):
        await self.watch(foreground=True)
        await self.comat.tick(self.now+1)
        self.comat.provider.lookup.assert_not_awaited()
        await self.watch()
        self.comat.provider.lookup.return_value = {'atd':self.now-10}
        await self.comat.tick(self.now+2)
        self.assertEqual(self.comat.sender.await_count,1)
        self.assertEqual(self.comat.sender.call_args.args[2]['title'],'Abgehoben')
        await self.comat.tick(self.now+70)
        self.assertEqual(self.comat.sender.await_count,1)
        self.comat.provider.lookup.return_value = {'ata':self.now+71,'aibt':self.now+73}
        await self.comat.tick(self.now+140)
        self.assertEqual(self.comat.sender.await_count,3)
        count = self.comat.provider.lookup.await_count
        await self.comat.tick(self.now+220)
        self.assertEqual(self.comat.provider.lookup.await_count,count)

    async def test_failed_delivery_retries_and_survives_reopen(self):
        await self.watch()
        self.comat.sender.return_value = False
        self.comat.provider.lookup.return_value = {'atd':self.now-10}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.relay.db.execute('select sent from comat_outbox').fetchone()[0],0)
        # Independent connection sees committed device credentials and outbox.
        other = Relay(str(Path(self.temp.name)/'relay.db'), 'TEAM.app', b'', AsyncMock())
        other.comatalarm.sender = AsyncMock(return_value=True)
        await other.comatalarm.tick(self.now+15)
        other.comatalarm.sender.assert_awaited_once()
        other.db.close()
        self.assertEqual(self.relay.db.execute('select sent from comat_outbox').fetchone()[0],1)

    async def test_remove_or_foreground_during_poll_cannot_deliver_stale_event(self):
        for change in (dict(flights=[]),dict(foreground=True),dict(preferences={'enabled':False})):
            await self.watch()
            async def lookup(*args):
                await self.watch(**change)
                return {'atd':self.now-10}
            self.comat.provider.lookup.side_effect = lookup
            await self.comat.tick(self.now+1)
            self.comat.sender.assert_not_awaited()
            self.assertEqual(self.relay.db.execute('select count(*) from comat_outbox').fetchone()[0],0)

    async def test_calendar_and_alarm_flags_suppress_without_replaying(self):
        await self.watch(preferences={'active_windows':[[self.now+300,self.now+600]]})
        self.comat.provider.lookup.return_value = {'atd':self.now-10}
        await self.comat.tick(self.now+1)
        self.comat.sender.assert_not_awaited()
        await self.comat.tick(self.now+400)
        self.comat.sender.assert_not_awaited()

    async def test_deleted_pending_delivery_is_not_retried(self):
        await self.watch()
        self.comat.provider.lookup.return_value = {'atd':self.now-10}
        self.comat.sender.return_value = False
        await self.comat.tick(self.now+1)
        await self.watch(flights=[])
        await self.comat.tick(self.now+20)
        self.assertEqual(self.comat.sender.await_count,1)
        self.assertEqual(self.relay.db.execute('select count(*) from comat_outbox').fetchone()[0],0)

    async def test_revoke_and_expiry_clear_private_tracking(self):
        await self.watch()
        await self.comat.tick(self.now+90000)
        row = self.relay.db.execute('select * from comat_devices').fetchone()
        self.assertEqual(row['api_token'],'')
        self.assertEqual(row['watch'],'[]')
        self.assertEqual((await self.client.delete('/comatalarm/v1/registration',headers=self.headers)).status,200)
        self.assertEqual((await self.client.get('/comatalarm/v1/state',headers=self.headers)).status,401)

    async def test_real_alert_test_reports_rejection(self):
        self.comat.sender.return_value = False
        self.assertEqual((await self.client.post('/comatalarm/v1/test',headers=self.headers)).status,502)
        self.comat.sender.return_value = True
        response=await self.client.post('/comatalarm/v1/test',headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertTrue((await response.json())['accepted'])

class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_arrival_events_and_ber_gate_mapping(self):
        now=time.time()
        responses=[{'data':[{'fr24_id':'abc','flight':'TK1729','dest_iata':'BER','reg':'D-AIDL','datetime_takeoff':now-300,'datetime_landed':now-100}]},
            {'data':[{'fr24_id':'abc','events':[{'type':'gate_arrival','timestamp':now-60,'details':{'gate_ident':'IGNORED','gate_lat':52.36507,'gate_lon':13.5053}}]}]},
            {'data':{'items':[{'flight_number':'TK 1729','gate':'A03'}]}}]
        class Response:
            status=200
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def json(self): return responses.pop(0)
        session=AsyncMock()
        session.get=lambda *args,**kwargs: Response()
        with patch('push_relay.comatalarm.ClientSession') as cls:
            cls.return_value.__aenter__.return_value=session
            provider=FlightProvider()
            result=await provider.lookup(dict(number='TK1729',reference=now-300,destination='BER'),'secret',now)
        self.assertEqual(result['registration'],'D-AIDL')
        self.assertEqual(result['aibt_position']['latitude'],52.36507)
        self.assertEqual(result['aibt'],now-60)
        self.assertEqual(result['gate'],'A03')
        self.assertNotIn('stand',result)
        self.assertEqual(result['atd'],now-300)

class RichParkingTests(ComatAlarmTests):
    async def test_master_only_testflight(self):
        self.comat.testflight.groups = AsyncMock(return_value=[{'id':'external','name':'Test'}])
        url = '/comatalarm/v1/testflight/groups'
        self.assertEqual((await self.client.get(url,headers=self.headers)).status,401)
        headers = dict(self.headers, **{'X-ComatAlarm-Setup-Key':'s'*64})
        response = await self.client.get(url,headers=headers)
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['groups'][0]['id'],'external')
        self.comat.testflight.add = AsyncMock()
        response = await self.client.post('/comatalarm/v1/testflight/testers',headers=self.headers,json={'email':'test@example.com','group_id':'external'})
        self.assertEqual(response.status,401)
        self.comat.testflight.add.assert_not_called()

    async def test_aibt_coordinate_matches_ber_and_rich_alert(self):
        self.flight.update(number='LH172',registration='D-AIDL',ata=self.now-50)
        positions = [{'name':'B05','latitude':52.36507,'longitude':13.5053}, {'name':'B06','latitude':52.367,'longitude':13.506}]
        response = await self.watch(stand_positions=positions)
        self.assertEqual(response.status,200)
        self.comat.provider.lookup.return_value = {'aibt':self.now,'gate':'C1','aibt_position':{'latitude':52.36507,'longitude':13.5053,'timestamp':self.now}}
        await self.comat.tick(self.now+1)
        bodies = [c.args[2]['body'] for c in self.comat.sender.call_args_list]
        self.assertIn('LH172 · D-AIDL · Stand B05',bodies)
        self.assertFalse(any('Stand C1' in x for x in bodies))
        state = await (await self.client.get('/comatalarm/v1/state',headers=self.headers)).json()
        self.assertEqual(state['flights'][0]['stand'],'B05')
        self.assertEqual(state['flights'][0]['aibt_position']['latitude'],52.36507)
        self.assertNotIn('stand_index',state)
        stored = self.comat.db.execute('SELECT stand_index FROM comat_devices').fetchone()[0]
        self.assertNotIn('B05',stored)
        self.comat.provider.lookup.reset_mock()
        await self.comat.tick(self.now+65)
        self.comat.provider.lookup.assert_not_called()
