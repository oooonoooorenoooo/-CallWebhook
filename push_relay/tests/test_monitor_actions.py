import asyncio
import json
import time
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from push_relay.tests import test_comatalarm as base
from push_relay.monitor_actions import Operator, parse_arrivals, day_of, key_of


class OperatorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ComatAlarmTests.asyncSetUp
    asyncTearDown = base.ComatAlarmTests.asyncTearDown
    watch = base.ComatAlarmTests.watch
    register = base.ComatAlarmTests.register
    add_peer = base.ComatAlarmTests.add_peer

    async def command(self, action, key=None, headers=None):
        return await self.client.post('/comatalarm/v1/monitor',json={'action':action,'key':key},
            headers=headers if headers is not None else {'X-ComatAlarm-Setup-Key':'s'*64})

    def seed_arrival(self, number='EW207'):
        f = dict(number=number,origin='CGN',destination='BER',reference=self.now+3600,
            day=day_of(self.now),airline='Eurowings',carrier='EW',origin_name='Köln',
            schedule={'sta':self.now+3600,'eta':self.now+3700},aliases=[number,'LH9999'])
        f['key'] = key_of(f)
        self.comat.db.execute('INSERT OR REPLACE INTO comat_arrivals VALUES(1,?,?,?,?,?)',
            (day_of(self.now),self.now,self.now,json.dumps([f]),''))
        self.comat.db.commit()
        return f

    async def test_arrival_pagination_and_shared_provider_backoff(self):
        pages=[{'data':{'items':[{'flight_number':'EW207','arr_scheduled_time':day_of(self.now)+'T23:55:00','dep_airport_iata':'CGN'}],'pagination':{'total_pages':2}}},
               {'data':{'items':[{'flight_number':'LH194','arr_scheduled_time':day_of(self.now)+'T23:56:00','dep_airport_iata':'FRA'}],'pagination':{'total_pages':2}}}]
        calls=[]
        class Response:
            status=200
            headers={}
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            @property
            def content(self):return self
            async def iter_chunked(self,size):
                raw=json.dumps(pages[len(calls)-1]).encode()
                yield raw[:10];yield raw[10:]
        class Session:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def get(self,url,**kwargs):
                calls.append(dict(kwargs['params']));return Response()
        with patch('push_relay.monitor_actions.ClientSession',Session):
            flights=await self.comat.monitor.operator.fetch_arrivals(self.now)
            self.assertEqual({f['number'] for f in flights},{'EW207','LH194'})
            self.assertEqual([p['page'] for p in calls],['1','2'])
            self.assertTrue(all(p['arrivalDeparture']=='A' and p['search']=='' for p in calls))
            self.comat.api_status.cooldown['ber']=self.now+600
            with self.assertRaisesRegex(ValueError,'Sperrfrist'):
                await self.comat.monitor.operator.fetch_arrivals(self.now)
            self.assertEqual(len(calls),2)

    async def test_commands_require_operator_and_reject_unknown_actions(self):
        for headers in ({},self.headers,{'X-ComatAlarm-Setup-Key':'bad'}):
            self.assertEqual((await self.command('delete','a'*64,headers)).status,401)
        for action,key in [('delete','../anything'),('drop_database','a'*64)]:
            self.assertEqual((await self.command(action,key)).status,400)

    async def test_delete_removes_all_device_copies_pending_push_history_and_blocks_reposts(self):
        self.flight.update(number='LH207',origin='BER',destination='FRA')
        await self.watch(server_polling=True)
        peer = await self.add_peer(dict(self.flight,id='peer-flight'),server_polling=True)
        self.comat.db.execute('INSERT INTO comat_outbox VALUES(?,?,?,?,0)',
            (self.id,'pending',json.dumps({'flight_id':'test-flight','flag':'alarmLanded'}),self.now))
        self.comat.db.commit()
        key = self.comat.monitor.snapshot()['flights'][0]['key']
        self.assertEqual((await self.command('delete',key)).status,200)
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_outbox').fetchone()[0],0)
        self.assertEqual(self.comat.monitor.snapshot()['flights'],[])
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_monitor_history').fetchone()[0],0)
        await self.watch(server_polling=True)
        self.assertEqual(self.comat.monitor.snapshot()['flights'],[])
        restored = Operator(self.comat.monitor)
        self.assertEqual(restored.merge([self.flight],[],self.now),[])
        tomorrow = dict(self.flight,reference=self.now+86400,id='tomorrow')
        self.assertEqual(len(restored.merge([tomorrow],[],self.now)),1)
        self.assertEqual((await self.command('delete',key)).status,200)
        self.comat.sender.assert_not_awaited()

    async def test_delete_wins_race_with_in_flight_provider_request(self):
        await self.watch(server_polling=True)
        start, release = asyncio.Event(),asyncio.Event()
        async def lookup(*args,**kwargs):
            start.set();await release.wait();return {'atd':self.now-1}
        self.comat.provider.lookup.side_effect=lookup
        task=asyncio.create_task(self.comat.tick(self.now+1))
        await start.wait()
        key=self.comat.monitor.snapshot()['flights'][0]['key']
        await self.command('delete',key)
        release.set();await task
        self.assertEqual(self.comat.monitor.snapshot()['flights'],[])
        self.comat.sender.assert_not_awaited()

    async def test_track_survives_app_handoff_restart_and_polls_without_open_app(self):
        await self.watch(server_polling=True,relay_alerts=True)
        f=self.seed_arrival()
        response=await self.command('track',f['key'])
        self.assertEqual(response.status,200,await response.text())
        # User can close all apps; the timer polls the new watch using existing credentials.
        self.comat.provider.lookup.return_value={'atd':self.now-1}
        await self.comat.tick(self.now+1)
        queried=[call.args[0]['number'] for call in self.comat.provider.lookup.call_args_list]
        self.assertIn('EW207',queried)
        before=self.comat.db.execute('SELECT watch FROM comat_devices WHERE id=?',(self.id,)).fetchone()[0]
        response=await self.watch(server_polling=True,relay_alerts=True)
        result=await response.json()
        added=next(x for x in result['flights'] if x['number']=='EW207')
        self.assertEqual(added['atd'],self.now-1)
        self.assertTrue(any(c.args[2]['flight_id']==added['id'] for c in self.comat.sender.call_args_list))
        self.assertEqual((await self.command('track',f['key'])).status,200)
        self.assertEqual(len(self.comat.monitor.snapshot()['flights']),2)
        restored=Operator(self.comat.monitor)
        self.assertEqual(len(restored.merge([],json.loads(before),self.now)),1)
        self.assertEqual(restored.merge([],[],self.now+86401),[])
        # No raw key/device credentials in the board response.
        body=json.dumps(self.comat.monitor.snapshot())
        for secret in ('private-api-token',self.id,'s'*64):self.assertNotIn(secret,body)

    async def test_track_requires_connected_approved_device_and_available_row(self):
        f=self.seed_arrival()
        self.assertEqual((await self.command('track',f['key'])).status,409)
        await self.watch(server_polling=True)
        self.comat.db.execute('UPDATE comat_devices SET firebase_uid=?',('revoked-user',));self.comat.db.commit()
        self.comat.stand_access=AsyncMock(return_value=None)
        self.assertEqual((await self.command('track',f['key'])).status,409)
        self.assertEqual((await self.command('track','f'*64)).status,409)
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_manual').fetchone()[0],0)

    async def test_history_exposes_only_archive_facts_after_registration_expiry(self):
        self.flight.update(number='LH194',origin='FRA',destination='BER',aibt=self.now-1,stand='B06')
        await self.watch(server_polling=True)
        self.comat.db.execute('UPDATE comat_devices SET expires=?',(self.now-1,));self.comat.db.commit()
        result=self.comat.monitor.snapshot()
        f=result['history']['flights'][0]
        self.assertEqual(f['category'],'completed');self.assertEqual(f['stand'],'B06')
        for key in ('live','position_stale','position_age','subscribers','distance_km'):
            self.assertNotIn(key,f)

    async def test_arrivals_shared_task_daily_cache_retry_backoff_and_error_preserves_cache(self):
        op=self.comat.monitor.operator
        op.fetch_arrivals=AsyncMock(return_value=[])
        op.request_arrivals(); first=op.task;op.request_arrivals();self.assertIs(op.task,first)
        await first
        op.request_arrivals();op.request_arrivals(force=True)
        self.assertEqual(op.fetch_arrivals.await_count,1)
        f=self.seed_arrival()
        self.comat.db.execute('UPDATE comat_arrivals SET attempted=?',(self.now-301,));self.comat.db.commit()
        op.fetch_arrivals.side_effect=ValueError('BER-Ankunftstabelle: HTTP 403')
        op.request_arrivals(force=True);await op.task
        b=op.board([],self.now)
        self.assertIn('403',b['error']);self.assertEqual(b['flights'][0]['number'],f['number'])


class ArrivalParserTests(unittest.TestCase):
    def test_app_contract_eurowings_codeshares_operators_and_actual_arrival(self):
        def row(number,**kw):return dict(flight_number=number,arr_scheduled_time='2026-11-06T18:00:00',
            dep_airport_iata='FRA',arr_airport_iata='BER',**kw)
        rows=[row('LH 194',code_shares=['UA 1234']),row('LH194',code_shares=['AC 999']),row('EW 207'),
              row('FR1234',code_shares=['LH999']),row('LH196',arr_actual_time='2026-11-06T18:01:00'),
              row('TK1730',status_text='Gelandet'),row('LH200',is_departure=True)]
        result=parse_arrivals(rows,'2026-11-06')
        self.assertEqual({f['number'] for f in result},{'LH194','EW207'})
        self.assertIn('AC999',next(f for f in result if f['number']=='LH194')['aliases'])
        self.assertEqual(parse_arrivals(rows,'2026-11-07'),[])
