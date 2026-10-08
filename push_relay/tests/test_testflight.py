import unittest
from unittest.mock import AsyncMock
from push_relay.testflight import TestFlight

class TestFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_external_groups_of_exact_app(self):
        client = TestFlight()
        client.request = AsyncMock(side_effect=[{'data':[{'id':'app'}]}, {'data':[{'id':'external','attributes':{'name':'Test','isInternalGroup':False}}, {'id':'internal','attributes':{'name':'Team','isInternalGroup':True}}]}])
        self.assertEqual(await client.groups(), [{'id':'external','name':'Test'}])
        self.assertEqual(client.request.call_args_list[0].kwargs['params']['filter[bundleId]'], 'de.comatalarm.app.ios.U98PKCA4W7')

    async def test_new_and_existing_tester_membership(self):
        for existing in (False, True):
            client = TestFlight()
            client.groups = AsyncMock(return_value=[{'id':'external'}])
            client.request = AsyncMock(side_effect=[{'data':[{'id':'tester','attributes':{'email':'test@example.com'}}] if existing else []}, {}])
            await client.add('test@example.com','external')
            call = client.request.call_args
            self.assertEqual(call.args[0], 'POST')
            self.assertEqual(call.args[1], 'betaGroups/external/relationships/betaTesters' if existing else 'betaTesters')
            self.assertIn('data', call.args[2])

    async def test_no_invite_for_invalid_input_or_other_app_group(self):
        client = TestFlight()
        client.groups = AsyncMock(return_value=[{'id':'external'}])
        client.request = AsyncMock()
        for email, group in [('bad','external'), ('test@example.com','other'), ('x\ny@example.com','external')]:
            with self.assertRaises(ValueError):
                await client.add(email,group)
        client.request.assert_not_called()
