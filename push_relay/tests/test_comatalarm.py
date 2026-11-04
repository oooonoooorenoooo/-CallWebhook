import asyncio
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
from push_relay.comatalarm import FlightProvider, SharedAPIStatus

class ComatAlarmTests(unittest.IsolatedAsyncioTestCase):

    async def test_provider_gate_survives_stale_phone_reposts_and_is_shared(self):
        self.flight.update(number='LH206', origin='FRA', destination='BER', gate='OLD')
        await self.watch(server_polling=True)
        peer = await self.add_peer(dict(self.flight, id='peer-flight'), server_polling=True)
        self.comat.provider.lookup.return_value = {'gate':'C1', 'gate_source':'FlightStats'}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count, 1)
        response = await self.watch(server_polling=True)
        self.assertEqual(response.status, 200)
        for headers in (self.headers, peer):
            state = await (await self.client.get('/comatalarm/v1/state', headers=headers)).json()
            self.assertEqual(state['flights'][0]['gate'], 'C1')
            self.assertEqual(state['flights'][0]['gate_source'], 'FlightStats')
        # A later provider gate change still replaces the preserved value.
        self.comat.provider.lookup.return_value = {'gate':'A2', 'gate_source':'BER'}
        await self.comat.tick(self.now+62)
        state = await (await self.client.get('/comatalarm/v1/state', headers=self.headers)).json()
        self.assertEqual(state['flights'][0]['gate'], 'A2')
        self.assertEqual(state['flights'][0]['gate_source'], 'BER')

    async def test_old_finalized_departure_recovers_partial_previous_arrival_after_takeoff(self):
        self.flight.update(number='LH207', origin='BER', destination='FRA', registration='D-AIZD', atd=self.now-300)
        await self.watch(server_polling=True, relay_alerts=True,
                         stand_positions=[dict(name='A12',latitude=52.36507,longitude=13.5053)])
        row = self.comat.db.execute('SELECT id,watch FROM comat_devices').fetchone()
        flights = json.loads(row['watch'])
        flights[0]['_departure_finalized'] = True  # State left by 1.2.14.
        self.comat.db.execute('UPDATE comat_devices SET watch=? WHERE id=?',(json.dumps(flights),row['id']))
        self.comat.db.commit()
        inbound = dict(id='206', number='LH206', registration='D-AIZD', destination='BER', ata=self.now-800)
        self.comat.provider.lookup.return_value = {'_inbound':inbound}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count, 1)
        await self.watch(server_polling=True, relay_alerts=True)
        await self.comat.tick(self.now+62)
        self.assertEqual(self.comat.provider.lookup.await_count, 1, 'no minute-by-minute recovery')
        inbound = dict(inbound, aibt=self.now-600, aibt_position=dict(latitude=52.36507,longitude=13.5053,timestamp=self.now-600))
        self.comat.provider.lookup.return_value = {'_inbound':inbound}
        await self.comat.tick(self.now+902)
        self.assertEqual(self.comat.provider.lookup.await_count, 2)
        state = await (await self.client.get('/comatalarm/v1/state',headers=self.headers)).json()
        self.assertEqual(state['flights'][0]['stand'],'A12')
        self.assertNotIn('aibt',state['flights'][0])
        self.assertTrue(state['flights'][0]['_departure_finalized'])
        await self.comat.tick(self.now+1803)
        self.assertEqual(self.comat.provider.lookup.await_count, 2)

    async def test_postdeparture_recovery_is_bounded_and_stops_after_24h(self):
        self.flight.update(number='LH207', origin='BER', destination='FRA', registration='D-AIZD', atd=self.now-300)
        await self.watch(server_polling=True)
        self.comat.provider.lookup.return_value = {'_inbound':{}}
        for offset in (1, 902, 1803, 2704, 3605):
            await self.comat.tick(self.now+offset)
        self.assertEqual(self.comat.provider.lookup.await_count,4)
        row = self.comat.db.execute('SELECT id,watch FROM comat_devices').fetchone()
        flights = json.loads(row['watch'])
        flights[0]['_departure_repair_attempts'] = 0
        flights[0]['atd'] = self.now-86400
        self.comat.db.execute('UPDATE comat_devices SET watch=? WHERE id=?',(json.dumps(flights),row['id']))
        self.comat.db.commit()
        await self.comat.tick(self.now+4506)
        self.assertEqual(self.comat.provider.lookup.await_count,4)

    async def test_departure_previous_arrival_shared_persisted_without_false_arrival(self):
        self.flight.update(number='LH207', origin='BER', destination='FRA', registration='D-AIZD')
        await self.watch(foreground=True, server_polling=True, relay_alerts=True,
                         stand_positions=[dict(name='A12',latitude=52.36507,longitude=13.5053)])
        peer = await self.add_peer(dict(self.flight, id='peer-departure'), foreground=True, server_polling=True, relay_alerts=True,
                                   stand_positions=[dict(name='A12',latitude=52.36507,longitude=13.5053)])
        inbound = dict(id='206', number='LH206', registration='D-AIZD', destination='BER', ata=self.now-800, aibt=self.now-600,
                       aibt_position=dict(latitude=52.36507,longitude=13.5053,timestamp=self.now-600))
        self.comat.provider.lookup.return_value = {'_inbound':inbound, '_inbound_check_at':self.now, '_inbound_registration':'DAIZD'}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count, 1)
        self.assertEqual(self.comat.sender.await_count, 2)
        payload = self.comat.sender.call_args.args[2]['flight_state']
        self.assertEqual(payload['parking_aibt'], self.now-600)
        self.assertNotIn('aibt', payload)
        await self.watch(foreground=True, server_polling=True, relay_alerts=True)
        state = await (await self.client.get('/comatalarm/v1/state', headers=self.headers)).json()
        self.assertEqual(state['flights'][0]['_inbound']['id'], '206')
        self.comat.provider.lookup.return_value = {'atd':self.now+61}
        await self.comat.tick(self.now+62)
        calls = self.comat.provider.lookup.await_count
        await self.comat.tick(self.now+123)
        self.assertEqual(self.comat.provider.lookup.await_count, calls)

    async def test_departure_invalidated_stand_cannot_be_restored_by_client(self):
        self.flight.update(number='LH207', origin='BER', destination='FRA', registration='D-AIZD', stand='A12')
        await self.watch(server_polling=True)
        self.comat.provider.lookup.return_value = {'_inbound':{}}
        await self.comat.tick(self.now+1)
        await self.watch(server_polling=True)
        state = await (await self.client.get('/comatalarm/v1/state', headers=self.headers)).json()
        self.assertIsNone(state['flights'][0].get('stand'))

    async def test_pi_polls_with_all_five_apps_open_and_preserves_server_snapshot(self):
        await self.watch(foreground=True, api_foreground=True, server_polling=True, relay_alerts=True)
        peers = [self.headers]
        for index in range(4):
            peers.append(await self.add_peer(dict(self.flight, id=f"open-{index}"), foreground=True, server_polling=True, relay_alerts=True))
        self.comat.provider.lookup.return_value = {"atd":self.now-5, "_schedule":{"sta":self.now+2000}, "_live":{"lat":52.4,"lon":13.5,"timestamp":self.now}}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count, 1)
        self.assertTrue(self.comat.provider.lookup.call_args.args[0]["_central"])
        self.assertEqual(self.comat.sender.await_count, 5)
        for headers in peers:
            state = await (await self.client.get('/comatalarm/v1/state', headers=headers)).json()
            self.assertTrue(state['server_polling_supported'])
            self.assertEqual(state['flights'][0]['_schedule']['sta'], self.now+2000)
        # A phone posting its older local snapshot cannot erase Pi-owned values.
        await self.watch(foreground=True, server_polling=True, relay_alerts=True)
        state = await (await self.client.get('/comatalarm/v1/state', headers=self.headers)).json()
        self.assertIn('_live',state['flights'][0])
        self.assertEqual(state['flights'][0]['atd'],self.now-5)
        await self.comat.tick(self.now+62)
        self.assertEqual(self.comat.sender.await_count, 5)

    async def test_pi_keeps_polling_when_all_apps_close_without_handoff(self):
        await self.watch(foreground=True, server_polling=True, relay_alerts=True)
        self.comat.provider.lookup.return_value = {'atd':self.now}
        await self.comat.tick(self.now+1)
        self.comat.provider.lookup.return_value = {'ata':self.now+61}
        await self.comat.tick(self.now+62)
        self.assertEqual(self.comat.provider.lookup.await_count,2)
        self.assertEqual(self.comat.sender.await_count,2)
        await self.comat.tick(self.now+86401)
        self.assertEqual(self.comat.provider.lookup.await_count,2)

    async def test_pi_repairs_archived_aibt_once_without_replaying_alert(self):
        self.flight.update(aibt=self.now-3600, ata=self.now-3800, reference=self.now-7200, repair_arrival=True)
        await self.watch(foreground=True, server_polling=True, relay_alerts=True)
        self.comat.provider.lookup.return_value = {'aibt_position':{'latitude':52.36507,'longitude':13.5053,'timestamp':self.now-3600}}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count,1)
        self.comat.sender.assert_not_awaited()
        await self.watch(foreground=True, server_polling=True, relay_alerts=True)
        await self.comat.tick(self.now+62)
        self.assertEqual(self.comat.provider.lookup.await_count,1)

    async def test_invalid_server_ownership_is_rejected(self):
        self.assertEqual((await self.watch(server_polling='yes')).status,400)

    async def test_pi_parking_recovery_is_bounded_and_outbound_continues(self):
        self.flight.update(aibt=self.now-3600, ata=self.now-3800, reference=self.now-7200, repair_arrival=True)
        await self.watch(foreground=True, server_polling=True)
        for offset in (1, 62, 902, 1803):
            await self.comat.tick(self.now+offset)
        self.assertEqual(self.comat.provider.lookup.await_count,2)
        # A completed inbound flight can still carry outgoing packages.
        self.flight.update(repair_arrival=False, track_outbound=True, outbound_number='LH187')
        await self.watch(foreground=True, server_polling=True)
        self.comat.provider.lookup.return_value = {'_outbound':{'number':'LH187','atd':self.now+1804,'destination':'FRA'}}
        await self.comat.tick(self.now+1864)
        self.assertEqual(self.comat.provider.lookup.await_count,3)
        await self.comat.tick(self.now+1925)
        self.assertEqual(self.comat.provider.lookup.await_count,3)

    async def test_background_handoff_preserves_token_and_polls_without_app(self):
        await self.watch(foreground=True, api_foreground=True, relay_alerts=True)
        response = await self.watch(foreground=False, fr24_token="", relay_alerts=True)
        self.assertTrue((await response.json())["background_ready"])
        self.comat.provider.lookup.return_value = {"atd": self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.call_args.args[1], "private-api-token")
        self.assertEqual(self.comat.sender.await_count, 1)
        self.assertIn("atd", self.comat.sender.call_args.args[2]["flight_state"])
        await self.comat.tick(self.now+62)
        self.assertEqual(self.comat.sender.await_count, 1)

    async def test_force_quit_without_handoff_expires_foreground_lease(self):
        await self.watch(foreground=True, api_foreground=True, relay_alerts=True)
        self.comat.provider.lookup.return_value = {"atd": self.now}
        await self.comat.tick(self.now+30)
        self.comat.provider.lookup.assert_not_awaited()
        await self.comat.tick(self.now+91)
        self.assertEqual(self.comat.provider.lookup.await_count, 1)
        self.assertEqual(self.comat.sender.await_count, 1)

    async def test_omitted_token_preserves_existing_but_foreground_clear_works(self):
        await self.watch()
        body = dict(flights=[self.flight], preferences={}, foreground=False)
        response = await self.client.post("/comatalarm/v1/watch", headers=self.headers, json=body)
        self.assertTrue((await response.json())["background_ready"])
        response = await self.watch(foreground=True, fr24_token="")
        self.assertFalse((await response.json())["background_ready"])


    async def test_shared_api_check_is_coalesced_and_visible_to_all_devices(self):
        peer = await self.add_peer(self.flight)
        await asyncio.gather(*[self.watch(api_status_request=True) for _ in range(5)])
        if self.comat.api_status.task: await self.comat.api_status.task
        self.assertEqual(self.comat.api_status.probe.await_count, 1)
        left = await (await self.client.get("/comatalarm/v1/state", headers=self.headers)).json()
        right = await (await self.client.get("/comatalarm/v1/state", headers=peer)).json()
        self.assertEqual(left["api_status"], right["api_status"])
        self.assertEqual(left["api_status"]["results"]["FR24"], "OK")
        self.assertTrue(left["api_status_supported"])
        self.assertNotIn("credentials", left["api_status"])

    async def test_shared_api_check_automatic_ttl_and_manual_throttle(self):
        await self.watch(api_status_request=True, api_credentials={"FLIGHTAWARE_API_KEY":"test-private"})
        if self.comat.api_status.task: await self.comat.api_status.task
        checked = self.comat.api_status.snapshot()["checked_at"]
        await self.comat.api_status.refresh("private", now=checked+61)
        self.assertEqual(self.comat.api_status.probe.await_count, 1)
        await self.comat.api_status.refresh("private", manual=True, now=checked+61)
        self.assertEqual(self.comat.api_status.probe.await_count, 2)
        await self.comat.api_status.refresh("private", now=checked+962)
        self.assertEqual(self.comat.api_status.probe.await_count, 3)
        restored = SharedAPIStatus(self.relay.db, FlightProvider())
        self.assertEqual(restored.snapshot(), self.comat.api_status.snapshot())
        self.assertNotIn("test-private", json.dumps(restored.snapshot()))

    async def test_invalid_api_status_inputs_rejected(self):
        self.assertEqual((await self.watch(api_status_request="yes")).status, 400)
        self.assertEqual((await self.watch(api_credentials={"URL":"https://example.com"})).status, 400)
        self.comat.api_status.probe.assert_not_awaited()

    async def test_relay_owns_foreground_transitions_exactly_once(self):
        response = await self.watch(foreground=True, relay_alerts=True)
        self.assertTrue((await response.json())["relay_alerts"])
        updated = dict(self.flight, atd=self.now-10)
        await self.watch(foreground=True, relay_alerts=True, flights=[updated])
        self.assertEqual(self.comat.sender.await_count, 1)
        await self.watch(foreground=True, relay_alerts=True, flights=[updated])
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.comat.provider.lookup.assert_not_awaited()
        updated.update(ata=self.now-5, aibt=self.now-1, stand="B05")
        await self.watch(foreground=True, relay_alerts=True, flights=[updated])
        self.assertEqual(self.comat.sender.await_count, 3)
        self.assertEqual(self.comat.sender.call_args.args[2]["flight_state"]["stand"], "B05")

    async def test_no_historical_alerts_on_first_foreground_subscription(self):
        await self.watch(foreground=True, relay_alerts=True, flights=[dict(self.flight, atd=self.now-100, ata=self.now-10)])
        self.comat.sender.assert_not_awaited()

    async def test_same_phone_different_local_ids_gets_one_alert(self):
        await self.watch()
        await self.add_peer(dict(self.flight, id="other-local-id"))
        # Simulate two pre-existing grants before the new registration safeguard.
        self.relay.db.execute("UPDATE comat_devices SET token=?", ('a'*64,))
        self.relay.db.commit()
        self.comat.provider.lookup.return_value = {"atd":self.now-10}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.assertEqual(self.relay.db.execute("SELECT count(*) FROM comat_outbox WHERE sent=1").fetchone()[0], 2)

    async def test_reprovisioning_same_phone_replaces_old_grant(self):
        await self.watch()
        self.id = str(uuid4())
        response = await self.register()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.relay.db.execute("SELECT count(*) FROM comat_devices").fetchone()[0], 1)
        self.assertEqual((await self.client.get('/comatalarm/v1/state', headers=self.headers)).status, 401)

    async def test_watch_and_tick_cannot_double_send_pending_event(self):
        await self.watch(relay_alerts=True)
        self.comat.sender.return_value = False
        self.comat.provider.lookup.return_value = {"atd":self.now-10}
        await self.comat.tick(self.now+1)
        self.comat.sender.reset_mock()
        self.comat.sender.return_value = True
        await asyncio.gather(self.comat.deliver(self.id, self.now+2), self.comat.deliver(self.id, self.now+2))
        self.assertEqual(self.comat.sender.await_count, 1)

    async def test_legacy_foreground_still_uses_local_alerts(self):
        await self.watch(foreground=True)
        await self.watch(foreground=True, flights=[dict(self.flight, atd=self.now-10)])
        self.comat.sender.assert_not_awaited()

    async def test_handoff_accepts_all_active_and_recent_completed_flights(self):
        completed = [dict(self.flight, id=f"done-{i}", aibt=self.now-5) for i in range(12)]
        active = [dict(self.flight, id=f"active-{i}", number=f"LH{180+i}") for i in range(20)]
        self.assertEqual((await self.watch(relay_alerts=True, flights=active+completed)).status, 200)
        self.assertEqual((await self.watch(relay_alerts=True, flights=active+[dict(self.flight, id="extra", number="EW999")])).status, 200)


    async def test_twelve_shared_flights_five_closed_devices_poll_once_each(self):
        flights = [dict(self.flight, id=f"flight-{i}", number=f"LH{180+i}") for i in range(12)]
        await self.watch(flights=flights, relay_alerts=True)
        for peer in range(4):
            await self.add_peer(self.flight, flights=[dict(f, id=f"peer-{peer}-{i}") for i, f in enumerate(flights)], relay_alerts=True)
        self.comat.provider.lookup.return_value = {"atd": self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count, 12)
        self.assertEqual(self.comat.sender.await_count, 60)
        await self.comat.tick(self.now+62)
        self.assertEqual(self.comat.provider.lookup.await_count, 24)
        self.assertEqual(self.comat.sender.await_count, 60)

    async def test_api_budget_deferred_flights_get_next_cycle_priority(self):
        flights = [dict(self.flight, id=f"flight-{i}", number=f"LH{180+i}") for i in range(12)]
        await self.watch(flights=flights, relay_alerts=True)
        attempts = []
        async def limited(flight, token, now):
            attempts.append(flight["number"])
            return {} if len(attempts) <= 8 else {"_error": "FR24: Minutenschutz aktiv"}
        self.comat.provider.lookup.side_effect = limited
        await self.comat.tick(self.now+1)
        attempts.clear()
        await self.comat.tick(self.now+62)
        self.assertEqual(attempts[:4], ["LH188", "LH189", "LH190", "LH191"])

    async def test_server_tracks_union_not_four_global_flights(self):
        flights = [dict(self.flight, id=f"flight-{i}", number=f"LH{180+i}") for i in range(4)]
        await self.watch(flights=flights, relay_alerts=True)
        await self.add_peer(dict(self.flight, id="extra", number="EW123"))
        self.comat.provider.lookup.return_value = {"atd": self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count, 5)
        self.assertEqual(self.comat.sender.await_count, 5)

    async def test_push_contains_confirmed_widget_milestones(self):
        await self.watch()
        for index, field in enumerate(("atd", "ata", "aibt")):
            self.comat.provider.lookup.return_value = {field:self.now+index*61}
            await self.comat.tick(self.now+index*61+1)
            payload = self.comat.sender.call_args.args[2]
            self.assertEqual(payload["flight_state"]["id"], self.flight["id"])
            self.assertEqual(payload["flight_state"][field], self.now+index*61)
            self.assertNotIn("api_token", payload["flight_state"])
            self.assertNotIn("aibt_position", payload["flight_state"])

    async def test_all_apps_closed_for_hours_still_poll_and_deliver_events(self):
        self.flight.update(number='LH186',origin='FRA',destination='BER')
        await self.watch(foreground=True, server_polling=True, relay_alerts=True)
        await self.add_peer(dict(self.flight,id='peer-186'),foreground=True,server_polling=True,relay_alerts=True)
        self.comat.sender.reset_mock()
        self.comat.stand_access = AsyncMock(return_value={'uid':'approved-device','administrator':False})
        self.comat.db.execute("UPDATE comat_devices SET firebase_uid='approved-device'")
        self.comat.db.commit()
        # No state/watch calls and no background handoff after this point.
        # The phones can be suspended or killed without sending another request.
        for elapsed in range(1, 4*3600, 61):
            evidence = {'atd':self.now+3600} if elapsed >= 3600 else {}
            if elapsed >= 7200:
                evidence.update(ata=self.now+7200)
            if elapsed >= 7500:
                evidence.update(aibt=self.now+7500)
            self.comat.provider.lookup.return_value=evidence
            await self.comat.tick(self.now+elapsed)
            if 7200 <= elapsed < 7261:
                self.assertEqual(sum(c.args[2]['flag']=='alarmLanded' for c in self.comat.sender.call_args_list),2)
        flags=[c.args[2]['flag'] for c in self.comat.sender.call_args_list]
        self.assertEqual(flags.count('alarmTakeoff'),2)
        self.assertEqual(flags.count('alarmLanded'),2)
        self.assertEqual(flags.count('alarmParking'),2)
        self.assertGreater(self.comat.provider.lookup.await_count,100)

    async def test_monitor_requires_operator_and_does_not_query_providers(self):
        self.flight.update(number='LH186', origin='FRA', destination='BER', packets='3/2')
        await self.watch(server_polling=True)
        await self.add_peer(dict(self.flight,id='peer-flight'),server_polling=True)
        for headers in ({}, self.headers, {'X-ComatAlarm-Setup-Key':'wrong'}):
            response = await self.client.get('/comatalarm/v1/monitor',headers=headers)
            self.assertEqual(response.status,401)
        response = await self.client.get('/comatalarm/v1/monitor',headers={'X-ComatAlarm-Setup-Key':'s'*64})
        self.assertEqual(response.status,200)
        body = await response.json()
        self.assertEqual(len(body['flights']),1)
        self.assertEqual(len(body['flights'][0]['subscribers']),2)
        self.assertEqual(body['flights'][0]['packets'],{'in':3,'out':2})
        self.comat.provider.lookup.assert_not_called()
        self.comat.api_status.probe.assert_not_called()
        for secret in ('private-api-token', 's'*64, 'a'*64, self.id, self.headers['Authorization'][7:]):
            self.assertNotIn(secret,json.dumps(body))

    async def test_monitor_push_failure_success_and_app_read_are_distinct(self):
        await self.watch(server_polling=True,relay_alerts=True)
        self.comat.provider.lookup.return_value = {'atd':self.now-10,'_live':{'lat':52.0,'lon':12.0,'alt':30000,'gspeed':400,'timestamp':self.now-300,'private':'do-not-export'}}
        self.comat.sender.return_value = False
        await self.comat.tick(self.now+1)
        body = self.comat.monitor.snapshot(self.now+1)
        self.assertTrue(body['flights'][0]['position_stale'])
        self.assertEqual(body['pushes']['pending'],1)
        self.assertIsNone(body['devices'][0]['last_app_read'])
        self.assertTrue(any(x['kind']=='push_failed' for x in body['events']))
        self.assertNotIn('do-not-export',json.dumps(body))
        self.comat.sender.return_value = True
        await self.comat.deliver(self.id,self.now+20)
        await self.client.get('/comatalarm/v1/state',headers=self.headers)
        body = self.comat.monitor.snapshot(self.now+21)
        self.assertEqual(body['pushes']['accepted'],1)
        self.assertEqual(body['pushes']['pending'],0)
        self.assertIsNotNone(body['devices'][0]['last_app_read'])
        self.assertTrue(any(x['kind']=='push_accepted' for x in body['events']))
        self.assertEqual(self.comat.monitor.cycle_finished,self.now+1)

    async def test_monitor_logs_are_allowlisted_and_retained_after_restart(self):
        import logging
        handler = self.comat.monitor.handler
        handler.handle(logging.LogRecord('root',logging.WARNING,'',0,'unknown secret %s',('secret-value',),None))
        handler.handle(logging.LogRecord('root',logging.WARNING,'',0,'ComatAlarm provider=%s HTTP=%s response=%s pause_seconds=%s%s',('BER',403,'HTML',60,' gateway=CloudFront'),None))
        from push_relay.monitor import Monitor
        restored = Monitor(self.comat)
        events = restored.snapshot()['events']
        self.assertTrue(any('BER: HTTP 403' in x['message'] for x in events))
        self.assertNotIn('secret-value',json.dumps(events))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, COMATALARM_SETUP_KEY='s'*64)
        self.env.start()
        self.relay = Relay(str(Path(self.temp.name)/'relay.db'), 'TEAM.app', b'', AsyncMock())
        self.comat = self.relay.comatalarm
        self.comat.provider = AsyncMock()
        self.comat.provider.lookup.return_value = {}
        self.comat.api_status.probe = AsyncMock(return_value={name: "OK" for name in SharedAPIStatus.names})
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

    async def add_peer(self, flight, **fields):
        response = await self.client.post('/comatalarm/v1/register', headers={'X-ComatAlarm-Setup-Key':'s'*64},
            json={'device_id':str(uuid4()), 'token':uuid4().hex*2, 'environment':'production'})
        self.assertEqual(response.status, 200)
        headers = {'Authorization':'Bearer '+(await response.json())['credential']}
        body = dict(flights=[flight], preferences={}, foreground=False, fr24_token='private-api-token')
        body.update(fields)
        response = await self.client.post('/comatalarm/v1/watch', headers=headers, json=body)
        self.assertEqual(response.status, 200)
        return headers

    async def test_five_devices_share_one_request_and_each_receive_event(self):
        await self.watch()
        for offset in range(1, 5):
            await self.add_peer(dict(self.flight, id=f'phone-{offset}', reference=self.flight['reference']+offset))
        self.comat.provider.lookup.return_value = {'atd':self.now-10, 'fr24ID':'real-leg'}
        await asyncio.gather(self.comat.tick(self.now+1), self.comat.tick(self.now+1))
        self.assertEqual(self.comat.provider.lookup.await_count, 1)
        self.assertEqual(self.comat.sender.await_count, 5)
        await self.comat.tick(self.now+62)
        self.assertEqual(self.comat.provider.lookup.await_count, 2)
        self.assertEqual(self.comat.sender.await_count, 5)

    async def test_staggered_device_uses_shared_result(self):
        await self.watch()
        await self.comat.tick(self.now+1)
        await self.add_peer(dict(self.flight, id='late-phone', reference=self.flight['reference']+3))
        await self.comat.tick(self.now+30)
        self.assertEqual(self.comat.provider.lookup.await_count, 1)

    async def test_shared_lookup_keeps_dates_provider_ids_and_tokens_separate(self):
        await self.watch(flights=[dict(self.flight, fr24ID='leg-a')])
        await self.add_peer(dict(self.flight, id='leg-b', fr24ID='leg-b'))
        await self.add_peer(dict(self.flight, id='tomorrow', reference=self.now+86400))
        await self.add_peer(dict(self.flight, id='other-account', fr24ID='leg-a'), fr24_token='different-token')
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count, 4)

    async def test_foreground_leader_supplies_background_without_provider(self):
        await self.watch(foreground=True, api_foreground=True, flights=[dict(self.flight, atd=self.now-10)])
        await self.add_peer(dict(self.flight, id='background-copy'))
        await self.comat.tick(self.now+1)
        self.comat.provider.lookup.assert_not_awaited()
        self.assertEqual(self.comat.sender.await_count, 1)
        lease = self.comat.db.execute("SELECT MAX(api_foreground_until) FROM comat_devices").fetchone()[0]
        await self.comat.tick(max(self.now+100, lease+1))
        self.assertEqual(self.comat.provider.lookup.await_count, 1)

    async def test_foreground_follower_does_not_block_background_provider(self):
        await self.watch(foreground=True, api_foreground=False)
        await self.add_peer(dict(self.flight, id='background-copy'))
        await self.comat.tick(self.now+1)
        self.comat.provider.lookup.assert_awaited_once()

    async def test_failed_provider_request_is_also_shared(self):
        await self.watch()
        await self.add_peer(dict(self.flight, id='another-copy'))
        self.comat.provider.lookup.side_effect = RuntimeError('unavailable')
        await self.comat.tick(self.now+1)
        self.comat.provider.lookup.assert_awaited_once()
        self.assertEqual(self.relay.db.execute("select count(*) from comat_devices where error<>''").fetchone()[0], 2)

    async def test_authentication_environment_and_credential_isolation(self):
        for key in ('', 'x'*64): self.assertEqual((await self.register(key)).status,401)
        with patch.dict(os.environ, COMATALARM_SETUP_KEY=''):
            self.assertEqual((await self.register('')).status,401)
        self.assertEqual((await self.register(environment='development')).status,400)
        self.assertEqual((await self.client.get('/v1/registration',headers=self.headers)).status,401)
        self.assertEqual((await self.client.get('/comatalarm/v1/state')).status,401)

    async def test_watch_validation_and_no_secret_in_state(self):
        for fields in (dict(flights=[self.flight]*3), dict(flights=[self.flight]*2), dict(preferences={'enabled':'true'}), dict(foreground=1), dict(api_foreground=1), dict(flights=[dict(self.flight, reference=0)]),dict(fr24_token='x\ny')):
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
            self.comat.shared_results.clear()
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
        self.comat.testflight.groups = AsyncMock(return_value=[{'id':'internal','name':'Test'}])
        url = '/comatalarm/v1/testflight/groups'
        self.assertEqual((await self.client.get(url,headers=self.headers)).status,401)
        headers = dict(self.headers, **{'X-ComatAlarm-Setup-Key':'s'*64})
        response = await self.client.get(url,headers=headers)
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['groups'][0]['id'],'internal')
        self.comat.testflight.add = AsyncMock()
        response = await self.client.post('/comatalarm/v1/testflight/testers',headers=self.headers,json={'email':'test@example.com','group_id':'external'})
        self.assertEqual(response.status,401)
        self.comat.testflight.add.assert_not_called()
        response = await self.client.post('/comatalarm/v1/testflight/testers',headers=headers,json={'email':'test@example.com','group_id':'internal'})
        self.assertEqual(response.status,400)  # Old app must not silently grant App Store Connect access.
        self.comat.testflight.add.assert_not_called()
        self.comat.testflight.add.return_value = {'state':'pending','message':'Einladung ausstehend'}
        response = await self.client.post('/comatalarm/v1/testflight/testers',headers=headers,json={'mode':'internal','email':'test@example.com','group_id':'internal','first_name':'Test','last_name':'Person'})
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['state'],'pending')

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


class ArrivalStopTests(ComatAlarmTests):
    async def test_overdue_arrival_is_not_polled_or_announced_as_parked(self):
        self.flight.update(ata=self.now-9900, fr24ID='overdue')
        await self.watch()
        await self.comat.tick(self.now+1)
        self.comat.provider.lookup.assert_not_awaited()
        self.comat.sender.assert_not_awaited()

class ArrivalProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_overdue_provider_has_no_network_even_if_called_directly(self):
        now = time.time()
        with patch('push_relay.comatalarm.ClientSession') as session:
            self.assertEqual(await FlightProvider().lookup(dict(number='LH286', reference=now-12000, ata=now-9900), 'secret', now), {})
            session.assert_not_called()

    async def test_known_arrival_prioritizes_event_and_applies_retry_after(self):
        now = time.time()
        calls = []
        class Response:
            status = 429
            headers = {'Retry-After': '900'}
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
        def get(url, **kw):
            calls.append(url)
            return Response()
        session = AsyncMock(); session.get = get
        provider = FlightProvider()
        flight = dict(number='LH286',reference=now-3600,ata=now-60,fr24ID='real')
        with patch('push_relay.comatalarm.ClientSession') as cls:
            cls.return_value.__aenter__.return_value = session
            await provider.lookup(flight, 'secret', now)
            self.assertTrue(calls[0].endswith('historic/flight-events/light'))
            self.assertFalse(any('flight-summary' in url for url in calls))
            count = len(calls)
            await provider.lookup(flight, 'secret', now+301)
            self.assertEqual(len(calls), count)
            await provider.lookup(flight, 'secret', now+901)
            self.assertGreater(len(calls), count)

class APIStatusProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_hosts_auth_and_invalid_payloads(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        status = SharedAPIStatus(db, FlightProvider())
        requests = []
        class Response:
            status = 200
            headers = {}
            def __init__(self, url): self.url = url
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self):
                if "fr24api" in self.url: return {"data":[]}
                if "aeroapi" in self.url: return {"flights":[]}
                if "adsb.lol" in self.url: return {"unexpected":True}
                if "auth.opensky" in self.url: return {"access_token":"access"}
                if "opensky" in self.url: return {"time":123,"states":None}
                if "ber.berlin" in self.url: return {"data":{"items":[]}}
                return {}
        session = AsyncMock()
        def request(method, url, **kwargs):
            requests.append((method,url,kwargs))
            return Response(url)
        session.request = request
        with patch("push_relay.comatalarm.ClientSession") as cls:
            cls.return_value.__aenter__.return_value = session
            result = await status.probe("fr24-private", {"FLIGHTAWARE_API_KEY":"fa-private","OPENSKY_CLIENT_ID":"id","OPENSKY_CLIENT_SECRET":"os-private"}, time.time())
            import hashlib
            status.provider.requests[hashlib.sha256(b"fr24-private").hexdigest()] = [time.time()]*8
            first_requests = list(requests)
            requests.clear()
            limited = await status.probe("fr24-private", {}, time.time())
            self.assertIn("Minutenschutz", limited["FR24"])
            self.assertFalse(any("fr24api" in url for _,url,_ in requests))
        self.assertTrue(result["FR24"].startswith("Erreichbar"))
        self.assertEqual(result["ADSB.lol"], "Ungültige Antwort")
        self.assertEqual(result["OpenSky"], "OK")
        self.assertEqual(result["BER"], "OK")
        self.assertNotIn("private", json.dumps(result))
        self.assertTrue(all(not kw["allow_redirects"] for _,_,kw in requests))
        self.assertEqual(next(kw for _,url,kw in first_requests if "aeroapi" in url)["headers"]["x-apikey"], "fa-private")
        db.close()

