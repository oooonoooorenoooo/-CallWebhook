import sqlite3
import time
import unittest
from unittest.mock import AsyncMock
from push_relay.testflight import TestFlight, AppleError

class TestFlightTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client = TestFlight()
        client.app_id = AsyncMock(return_value='app')
        client.groups = AsyncMock(return_value=[{'id':'internal','name':'Team'}])
        client.in_group = AsyncMock(return_value=False)
        return client

    async def test_only_internal_groups_of_exact_app(self):
        client = TestFlight()
        client.request = AsyncMock(side_effect=[{'data':[{'id':'app'}]}, {'data':[{'id':'external','attributes':{'name':'Test','isInternalGroup':False}}, {'id':'internal','attributes':{'name':'Team','isInternalGroup':True}}]}])
        self.assertEqual(await client.groups(), [{'id':'internal','name':'Team'}])
        self.assertEqual(client.request.call_args_list[0].kwargs['params']['filter[bundleId]'], 'de.comatalarm.app.ios.U98PKCA4W7')

    async def test_new_invitation_is_app_scoped_marketing_without_provisioning(self):
        client = self.client()
        client.request = AsyncMock(side_effect=[{'data':[]}, {'data':[]}, {}])
        result = await client.add('test@example.com','internal','Test','Person')
        self.assertEqual(result['state'],'pending')
        method, path, body = client.request.call_args.args
        self.assertEqual((method,path),('POST','userInvitations'))
        self.assertEqual(body['data']['attributes'],dict(email='test@example.com',firstName='Test',lastName='Person',roles=['MARKETING'],allAppsVisible=False,provisioningAllowed=False))
        self.assertEqual(body['data']['relationships']['visibleApps']['data'],[{'type':'apps','id':'app'}])

    async def test_pending_invite_is_not_duplicated(self):
        client = self.client()
        client.request = AsyncMock(side_effect=[{'data':[]}, {'data':[{'attributes':{'email':'TEST@example.com'}}]}])
        self.assertEqual((await client.add('test@example.com','internal','Test','Person'))['state'],'pending')
        self.assertTrue(all(c.args[0]=='GET' for c in client.request.call_args_list))

    async def test_accepted_user_gets_latest_valid_build_and_membership(self):
        for existing in (False,True):
            client=self.client()
            client.request=AsyncMock(side_effect=[{'data':[{'id':'user','attributes':{'username':'test@example.com','roles':['MARKETING'],'allAppsVisible':False}}]},
                {'data':[{'id':'app'}]}, {'data':[{'id':'latest','attributes':{'version':'1201','expired':False,'processingState':'VALID'}}]}, {},
                {'data':[{'id':'tester','attributes':{'email':'test@example.com'}}] if existing else []}, {}])
            result=await client.add('test@example.com','internal','Test','Person')
            self.assertEqual(result['state'],'assigned')
            self.assertIn('1201',result['message'])
            self.assertEqual(client.request.call_args_list[2].kwargs['params']['sort'],'-uploadedDate')
            self.assertEqual(client.request.call_args_list[3].args[1], 'betaGroups/internal/relationships/builds')
            self.assertEqual(client.request.call_args_list[-1].args[1], 'betaGroups/internal/relationships/betaTesters' if existing else 'betaTesters')
            self.assertFalse(any(c.args[0]=='PATCH' for c in client.request.call_args_list))

    async def test_existing_user_permissions_are_not_escalated(self):
        for roles, apps in [(['CUSTOMER_SUPPORT'],['app']), (['MARKETING'],['other'])]:
            client=self.client()
            client.request=AsyncMock(side_effect=[{'data':[{'id':'user','attributes':{'username':'test@example.com','roles':roles,'allAppsVisible':False}}]}, {'data':[{'id':a} for a in apps]}])
            with self.assertRaises(AppleError): await client.add('test@example.com','internal','Test','Person')
            self.assertTrue(all(c.args[0]=='GET' for c in client.request.call_args_list))

    async def test_existing_membership_is_success_without_duplicate_mutations(self):
        client = self.client()
        client.in_group.return_value = True
        client.request = AsyncMock(side_effect=[
            {'data':[{'id':'user','attributes':{'username':'test@example.com','roles':['MARKETING'],'allAppsVisible':True}}]},
            {'data':[{'id':'latest','attributes':{'version':'1201','expired':False,'processingState':'VALID'}}]},
            {'data':[{'id':'tester','attributes':{'email':'test@example.com'}}]}])
        self.assertEqual((await client.add('test@example.com','internal','Test','Person'))['state'], 'assigned')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))
        self.assertEqual([c.args for c in client.in_group.call_args_list], [('builds','latest','internal'), ('betaTesters','tester','internal')])

    async def test_membership_lookup_fails_closed_on_incomplete_page(self):
        client = TestFlight()
        client.request = AsyncMock(return_value={'data':[{'id':'other'}], 'links':{'next':'more'}})
        with self.assertRaises(AppleError):
            await client.in_group('builds','latest','internal')
        self.assertEqual(client.request.call_args.args[1], 'betaGroups/internal/relationships/builds')
        client.request.return_value = {'data':[{'id':'latest'}], 'links':{'next':'more'}}
        self.assertTrue(await client.in_group('builds','latest','internal'))

    async def test_failed_apple_assignment_never_reports_success(self):
        client = self.client()
        client.request = AsyncMock(side_effect=[
            {'data':[{'id':'user','attributes':{'username':'test@example.com','roles':['MARKETING'],'allAppsVisible':True}}]},
            {'data':[{'id':'latest','attributes':{'version':'1201','expired':False,'processingState':'VALID'}}]},
            AppleError('Apple refuses this build')])
        with self.assertRaises(AppleError):
            await client.add('test@example.com','internal','Test','Person')

    async def test_queue_records_apple_failure_for_later_retry(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        client = TestFlight(db)
        client.configure = AsyncMock(side_effect=AppleError('Apple unavailable'))
        with self.assertRaises(AppleError):
            await client.add('test@example.com','internal','Test','Person')
        self.assertEqual(client.statuses()[0]['state'], 'pending')
        self.assertEqual(client.statuses()[0]['message'], 'Apple unavailable')
        db.close()

    async def test_no_invite_for_invalid_input_or_external_group(self):
        client=self.client(); client.request=AsyncMock()
        for email,group,first in [('bad','internal','Test'),('test@example.com','external','Test'),('x\ny@example.com','internal','Test'),('test@example.com','internal','')]:
            with self.assertRaises(ValueError): await client.add(email,group,first,'Person')
        client.request.assert_not_called()

    async def test_missing_build_or_failed_assignment_never_reports_success(self):
        for builds in ([],[{'id':'old','attributes':{'expired':True,'processingState':'VALID'}}]):
            client=self.client()
            client.request=AsyncMock(side_effect=[{'data':[{'id':'user','attributes':{'username':'test@example.com','roles':['ADMIN'],'allAppsVisible':True}}]}, {'data':builds}])
            with self.assertRaises(AppleError): await client.add('test@example.com','internal','Test','Person')
            self.assertTrue(all(c.args[0]=='GET' for c in client.request.call_args_list))

    async def test_queue_survives_restart_and_retries_without_new_invitation(self):
        db=sqlite3.connect(':memory:'); db.row_factory=sqlite3.Row
        client=TestFlight(db)
        client.configure=AsyncMock(return_value={'state':'pending','message':'pending'})
        await client.add('test@example.com','internal','Test','Person')
        db.execute('UPDATE comat_testflight_requests SET updated=?',(time.time()-400,)); db.commit()
        restarted=TestFlight(db)
        restarted.configure=AsyncMock(return_value={'state':'assigned','message':'Build 1201'})
        await restarted.reconcile()
        self.assertFalse(restarted.configure.call_args.kwargs['allow_invite'])
        self.assertEqual(restarted.statuses()[0]['state'],'assigned')
        restarted.configure.reset_mock(); await restarted.reconcile(); restarted.configure.assert_not_called()
        db.close()

    async def test_background_retry_never_invites_if_invitation_disappeared(self):
        client=self.client(); client.request=AsyncMock(side_effect=[{'data':[]},{'data':[]}])
        await client.configure('test@example.com','internal','Test','Person',allow_invite=False)
        self.assertTrue(all(c.args[0]=='GET' for c in client.request.call_args_list))
