import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from push_relay.stand_model import StandModel, bucket
from push_relay.comatalarm import protect_index, unprotect_index, match_stand
import test_comatalarm as fixtures
from push_relay.apple_access import AccessDenied

POSITIONS = [dict(name='B05',latitude=52.362,longitude=13.502,kind='stand'),
             dict(name='M7',latitude=52.368,longitude=13.512,kind='stop_bar_cat_i'),
             dict(name='25R',latitude=52.369,longitude=13.512,kind='runway')]

def observation(now, **extra):
    return dict(dict(id='fr24_id:seed', stand='B05', paxGate='C1', exit='STOP_BAR_CAT_I/M7', corridor='STOP_BAR_CAT_I/M7', runway='25R', timeBucket=bucket(now), airline='LH',aircraft='A320',observedAt=now-100),**extra)

def payload(kind, rows, offset=0, total=None, digest=None):
    return dict(airport='BER', kind=kind, rows=rows, offset=offset,total=total or len(rows),digest=digest or ('a' if kind=='package' else 'b')*64)

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,COMATALARM_SETUP_KEY='s'*64); self.env.start()
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'relay.sqlite3'
        self.db=sqlite3.connect(self.path); self.model=StandModel(self.db,protect_index,unprotect_index)
        self.now=time.time()
    def tearDown(self):
        self.db.close(); self.tmp.cleanup(); self.env.stop()
    def seed(self):
        self.model.upload(payload('package',POSITIONS), 'admin', self.now)
        self.model.upload(payload('observations',[observation(self.now)]),'admin',self.now)
    def test_chunk_resume_atomic_package_restart_and_encrypted_backup(self):
        self.model.upload(payload('package',POSITIONS[:1],total=3), 'admin',self.now)
        self.assertFalse(self.model.ready)
        restarted=StandModel(self.db,protect_index,unprotect_index)
        receipt=restarted.upload(payload('package',POSITIONS[:1],total=3),'admin',self.now)
        self.assertEqual(receipt['offset'],1)
        restarted.upload(payload('package',POSITIONS[1:],offset=1,total=3),'admin',self.now)
        self.assertTrue(restarted.ready)
        self.assertEqual(len(restarted.positions()),1)
        self.assertNotIn('B05',self.path.with_suffix('.stands-backup').read_text())
        self.db.execute('DELETE FROM comat_stand_models'); self.db.commit()
        restored=StandModel(self.db,protect_index,unprotect_index)
        self.assertEqual(restored.positions()[0]['name'],'B05')
    def test_legacy_import_dedup_and_exact_weighting(self):
        self.seed()
        # Another device has a different local ID for exactly the same arrival.
        self.model.learn(observation(self.now,id='id:another-local-id'), self.now)
        self.model.learn(observation(self.now,id='FR24_ID:SEED',paxGate='CORRECTED'), self.now)
        self.assertEqual(len(self.model.data['observations']),1)
        context=observation(self.now)
        result=self.model.predict(context,self.now)
        self.assertEqual(result['candidates'],[dict(stand='B05',percent=28)])
        for i in range(1,4): self.model.learn(observation(self.now,id=f'FR24_ID:{i}',observedAt=self.now-100-i),self.now)
        self.assertEqual(self.model.predict(context,self.now)['candidates'][0]['percent'],74)
        context.update(paxGate='',corridor='',exit='')
        self.assertEqual(self.model.predict(context,self.now)['candidates'],[])
    def test_confirmation_only_and_reference_features(self):
        self.seed()
        flight=dict(id='phone-1',number='LH206',reference=self.now,origin='FRA',destination='BER',fr24ID='real',gate='C1',ata=self.now-80,_checked_at=self.now,
            _live=dict(lat=52.368,lon=13.512,timestamp=self.now-60,type='A320'))
        before=self.model.apply(flight,self.now,match_stand)
        self.assertEqual(before['candidates'][0]['stand'],'B05')
        self.assertEqual(len(self.model.data['observations']),1, 'forecast is not training evidence')
        flight['aibt']=self.now-20
        self.model.apply(flight,self.now,match_stand)
        self.assertEqual(len(self.model.data['observations']),1, 'AIBT without GPS is not confirmation')
        flight['aibt_position']=dict(latitude=52.362,longitude=13.502,timestamp=self.now-20)
        self.model.apply(flight,self.now,match_stand)
        self.model.apply(dict(flight,id='phone-2'),self.now+1,match_stand)
        self.assertEqual(len(self.model.data['observations']),2)
        learned=self.model.data['observations'][-1]
        self.assertEqual(learned['corridor'],'STOP_BAR_CAT_I/M7')
        self.assertEqual(learned['runway'],'25R')
        self.assertIsNone(match_stand(dict(latitude=52.368,longitude=13.512),self.model.positions()))
        self.model.save()
        restarted=StandModel(self.db,protect_index,unprotect_index)
        self.assertEqual(len(restarted.data['observations']),2)
    def test_outbound_and_unrelated_airports_never_learn(self):
        self.seed()
        for origin,destination in [('BER','FRA'),('FRA','MUC')]:
            self.model.apply(dict(number='LH207',reference=self.now,origin=origin,destination=destination,fr24ID='other',aibt=self.now-10,ata=self.now-20,aibt_position=dict(latitude=52.362,longitude=13.502,timestamp=self.now-10)),self.now,match_stand)
        self.assertEqual(len(self.model.data['observations']),1)
    def test_stale_client_does_not_replace_central_forecast(self):
        self.seed()
        f=dict(number='LH206',reference=self.now,origin='FRA',destination='BER',fr24ID='real',gate='C1',_checked_at=self.now)
        first=self.model.apply(f,self.now,match_stand)
        stale=dict(f,gate='OTHER',_checked_at=0)
        self.assertEqual(self.model.apply(stale,self.now+10,match_stand),first)
    def test_invalid_chunk_does_not_replace_active_package(self):
        self.seed()
        with self.assertRaises(ValueError): self.model.upload(payload('package',[dict(name='bad',latitude=0,longitude=0)],digest='c'*64),'admin',self.now)
        self.assertEqual(self.model.positions()[0]['name'],'B05')

# Reuse the HTTP fixtures, not the inherited tests.
class CentralRelayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ComatAlarmTests.asyncSetUp
    asyncTearDown=fixtures.ComatAlarmTests.asyncTearDown
    register=fixtures.ComatAlarmTests.register
    watch=fixtures.ComatAlarmTests.watch
    add_peer=fixtures.ComatAlarmTests.add_peer
    def access(self):
        self.comat.apple_access.store.device_uid=Mock(side_effect=lambda token:token)
        self.comat.apple_access.store.device_access=Mock(side_effect=lambda uid:dict(uid=uid,administrator=uid=='admin'))
    async def seed(self):
        self.access()
        response=await self.watch(firebase_token='admin',server_polling=True)
        self.assertEqual(response.status,200)
        for kind,rows in [('package',POSITIONS),('observations',[observation(self.now)])]:
            response=await self.client.post('/comatalarm/v1/watch',headers=self.headers,json={'stand_sync':True,'stand_import':payload(kind,rows)})
            self.assertEqual(response.status,200)
    async def test_closed_apps_share_prediction_and_one_provider_call(self):
        self.flight.update(number='LH206',origin='FRA',destination='BER',fr24ID='real',gate='C1')
        await self.seed()
        peer=await self.add_peer(dict(self.flight,id='peer'),firebase_token='peer',server_polling=True,relay_alerts=True)
        await self.watch(firebase_token='admin',server_polling=True,relay_alerts=True)
        self.comat.provider.lookup.return_value={'atd':self.now-10,'gate':'C1','gate_source':'FlightStats','_checked_at':self.now+1}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count,1)
        forecasts=[]
        for headers in (self.headers,peer):
            response=await self.client.get('/comatalarm/v1/state',headers=headers)
            forecasts.append((await response.json())['flights'][0]['_stand_prediction'])
        self.assertEqual(forecasts[0],forecasts[1])
        self.assertEqual(forecasts[0]['candidates'][0]['stand'],'B05')
        self.assertEqual(self.comat.sender.await_count,2)
        self.assertIn('B05',self.comat.sender.call_args.args[2]['flight_state']['prediction'])
        # Expiry of registrations cannot delete the airport's training history.
        await self.comat.tick(self.now+86401)
        self.assertEqual(len(self.comat.stand_model.data['observations']),1)
    async def test_only_approved_firebase_and_admin_can_publish(self):
        await self.seed()
        peer=await self.add_peer(self.flight,firebase_token='peer',server_polling=True)
        response=await self.client.post('/comatalarm/v1/watch',headers=peer,json={'stand_sync':True,'stand_import':payload('package',POSITIONS)})
        self.assertEqual(response.status,403)
        response=await self.watch(firebase_token='someone-else')
        self.assertEqual(response.status,403, 'cannot rebind relay grant to a different Firebase UID')
        self.comat.stand_access_cache.clear()
        self.comat.apple_access.store.device_access.side_effect=AccessDenied('blocked')
        response=await self.client.get('/comatalarm/v1/state',headers=peer)
        self.assertEqual(response.status,403)
        await self.comat.tick(self.now+1)
        self.comat.provider.lookup.assert_not_awaited()
    async def test_unbound_legacy_device_receives_no_shared_model(self):
        await self.seed()
        peer=await self.add_peer(self.flight,server_polling=True)
        result=await (await self.client.get('/comatalarm/v1/state',headers=peer)).json()
        self.assertFalse(result['stand_model_authorized'])
        self.assertNotIn('stand_model',result)
        self.assertNotIn('_stand_prediction',result['flights'][0])
