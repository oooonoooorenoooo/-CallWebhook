import asyncio
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from push_relay.tests import test_monitor_actions as base
from push_relay.arrivals_sync import ArrivalSync
from push_relay.monitor_actions import BERLIN, day_of
from push_relay.server import arrival_notification


def epoch(value):
    return datetime.fromisoformat(value).replace(tzinfo=BERLIN).timestamp()


class ArrivalSyncTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.OperatorTests.asyncSetUp
    asyncTearDown = base.OperatorTests.asyncTearDown
    register = base.OperatorTests.register
    watch = base.OperatorTests.watch
    seed_arrival = base.OperatorTests.seed_arrival

    async def read(self, **body):
        return await self.client.post('/comatalarm/v1/watch', json=dict(arrivals_sync=True, **body), headers=self.headers)

    async def test_0500_berlin_winter_summer_and_dst_days(self):
        op = self.comat.monitor.operator
        for day, utc_hour in [('2026-11-14',4),('2026-06-14',3),('2026-03-29',3),('2026-10-25',4)]:
            with self.subTest(day=day):
                due = epoch(day+'T05:00:00')
                self.assertEqual(datetime.fromtimestamp(due, __import__('datetime').timezone.utc).hour,utc_hour)
                op.fetch_arrivals = AsyncMock(return_value=[])
                op.arrival_sync.schedule(due-1)
                op.fetch_arrivals.assert_not_awaited()
                with patch('push_relay.monitor_actions.time.time', return_value=due):
                    op.arrival_sync.schedule(due); task=op.task
                    op.arrival_sync.schedule(due+1); self.assertIs(task,op.task)
                    await task
                op.fetch_arrivals.assert_awaited_once()
                # Fresh process state and repeated ticks must not fetch again.
                ArrivalSync(op).schedule(due+300)
                op.fetch_arrivals.assert_awaited_once()

    async def test_startup_catchup_retry_backoff_and_pre0500_cache(self):
        op=self.comat.monitor.operator
        early=epoch('2026-11-14T04:00:00'); due=early+3600
        self.comat.db.execute('INSERT OR REPLACE INTO comat_arrivals VALUES(1,?,?,?,?,?)',
            (day_of(early),early,early,'[]',''));self.comat.db.commit()
        op.fetch_arrivals=AsyncMock(side_effect=ValueError('BER-Ankunftstabelle: HTTP 403'))
        with patch('push_relay.monitor_actions.time.time', return_value=due+600):
            op.arrival_sync.schedule(due+600);await op.task
        self.assertEqual(op.arrival_sync.metadata(due+600)['fetched_at'],early)
        op.arrival_sync.schedule(due+601)
        self.assertEqual(op.fetch_arrivals.await_count,1)
        self.comat.api_status.cooldown['ber']=due+3600
        op.arrival_sync.schedule(due+1000)
        self.assertEqual(op.fetch_arrivals.await_count,1)
        op.fetch_arrivals.side_effect=None;op.fetch_arrivals.return_value=[]
        with patch('push_relay.monitor_actions.time.time', return_value=due+3601):
            op.arrival_sync.schedule(due+3601);await op.task
        self.assertEqual(op.fetch_arrivals.await_count,2)
        self.assertTrue(op.arrival_sync.metadata(due+3601)['ready'])

    async def test_cache_sync_leaves_watches_preferences_and_expiry_unchanged(self):
        await self.watch(server_polling=True)
        self.seed_arrival()
        before=dict(self.comat.db.execute('SELECT * FROM comat_devices WHERE id=?',(self.id,)).fetchone())
        page=await (await self.read()).json()
        self.assertEqual(page['arrivals']['flights'][0]['number'],'EW207')
        self.assertEqual(page['arrivals']['scheduled_time'],'05:00')
        self.assertEqual(dict(self.comat.db.execute('SELECT * FROM comat_devices WHERE id=?',(self.id,)).fetchone()),before)
        self.comat.provider.lookup.assert_not_awaited()
        revision=page['arrivals']['revision']
        again=await (await self.read(revision=revision)).json()
        self.assertTrue(again['arrivals']['unchanged']);self.assertEqual(again['arrivals']['flights'],[])
        self.assertEqual((await self.read(offset=100,revision='older')).status,409)
        self.assertEqual((await self.read(offset=-1)).status,400)
        self.assertEqual((await self.client.post('/comatalarm/v1/watch',json={'arrivals_sync':True})).status,401)

    async def test_paging_complete_bounded_and_revision_consistent(self):
        f=self.seed_arrival()
        rows=[dict(f,number='EW'+str(i),aliases=['EW'+str(i)]) for i in range(250)]
        self.comat.db.execute('UPDATE comat_arrivals SET payload=?',(json.dumps(rows),));self.comat.db.commit()
        all_rows=[];offset=0;revision=''
        for _ in range(3):
            response=await self.read(offset=offset,revision=revision)
            raw=await response.read();self.assertLess(len(raw),65536)
            page=json.loads(raw)['arrivals'];all_rows+=page['flights']
            revision=page['revision'];offset=page['next_offset']
        self.assertIsNone(offset);self.assertEqual(len(all_rows),250)
        self.assertEqual(len({r['number'] for r in all_rows}),250)
        self.seed_arrival('LH194')
        self.assertEqual((await self.read(offset=100,revision=revision)).status,409)

    async def test_background_push_without_open_app_or_active_flights_is_durable(self):
        self.seed_arrival()
        await self.read()
        self.comat.db.execute("UPDATE comat_devices SET watch='[]',expires=?,api_token='',foreground_until=0",(self.now-86400,));self.comat.db.commit()
        sync=self.comat.monitor.operator.arrival_sync
        await sync.deliver(self.now)
        self.comat.sender.assert_awaited_once()
        payload=self.comat.sender.call_args.args[2]
        self.assertEqual(payload['sync_kind'],'arrivals')
        self.assertNotIn('flight_id',payload)
        note=arrival_notification('a'*64,payload)
        self.assertEqual(note.priority,5)
        self.assertEqual(note.message['aps'],{'content-available':1})
        self.assertLess(len(json.dumps(note.message).encode()),4096)
        await ArrivalSync(self.comat.monitor.operator).deliver(self.now+300)
        self.assertEqual(self.comat.sender.await_count,1)
        # Next day's outdated cache is not pushed under today's date.
        await sync.deliver(self.now+86400)
        self.assertEqual(self.comat.sender.await_count,1)

    async def test_push_retries_and_revocation_without_erasing_other_devices(self):
        self.seed_arrival();await self.read()
        sync=self.comat.monitor.operator.arrival_sync
        self.comat.sender.return_value=False
        await sync.deliver(self.now)
        await sync.deliver(self.now+10)
        self.assertEqual(self.comat.sender.await_count,1)
        self.comat.sender.return_value=True
        await ArrivalSync(self.comat.monitor.operator).deliver(self.now+121)
        self.assertEqual(self.comat.sender.await_count,2)
        self.seed_arrival('LH194')
        self.comat.db.execute('UPDATE comat_devices SET firebase_uid=?',('revoked',));self.comat.db.commit()
        self.comat.stand_access=AsyncMock(return_value=None)
        await sync.deliver(self.now+300)
        self.assertEqual(self.comat.sender.await_count,2)
        self.assertEqual((await self.read()).status,403)
        self.comat.db.execute('DELETE FROM comat_devices');self.comat.db.commit()
        await sync.deliver(self.now+600)
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_arrival_subscribers').fetchone()[0],0)

    async def test_no_push_for_failed_or_unavailable_snapshot(self):
        await self.read()
        sync=self.comat.monitor.operator.arrival_sync
        await sync.deliver(self.now)
        self.comat.sender.assert_not_awaited()
        self.seed_arrival();await sync.deliver(self.now)
        self.comat.db.execute("UPDATE comat_arrivals SET error='BER-Abfrage pausiert'");self.comat.db.commit()
        await sync.deliver(self.now+300)
        self.assertEqual(self.comat.sender.await_count,1)
