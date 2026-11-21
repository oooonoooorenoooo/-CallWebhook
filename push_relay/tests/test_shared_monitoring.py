"""Closed-app subscriptions, authorization and per-recipient delivery regressions."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from push_relay.tests import test_comatalarm as fixtures


class SharedMonitoringTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch
    add_peer = fixtures.ComatAlarmTests.add_peer

    async def asyncSetUp(self):
        await fixtures.ComatAlarmTests.asyncSetUp(self)
        self.allowed = {'reno', 'colleague', 'muted'}
        async def access(uid):
            return {'uid': uid, 'administrator': uid == 'reno'} if uid in self.allowed else None
        self.comat.stand_access = AsyncMock(side_effect=access)
        self.comat.apple_access.store.device_uid = Mock(side_effect=lambda token: token)
        self.flight.update(origin='FRA', destination='BER', created_at=self.now-120)

    async def own(self, **fields):
        args = dict(flights=[], firebase_token='reno', server_polling=True, relay_alerts=True)
        args.update(fields)
        response = await self.watch(**args)
        self.assertEqual(response.status, 200, await response.text())
        return response

    async def colleague(self, **fields):
        args = dict(firebase_token='colleague', server_polling=True, relay_alerts=True)
        args.update(fields)
        return await self.add_peer(self.flight, **args)

    def rows(self):
        return self.comat.db.execute('SELECT * FROM comat_devices ORDER BY id').fetchall()

    def row(self, uid):
        return self.comat.db.execute('SELECT * FROM comat_devices WHERE firebase_uid=?', (uid,)).fetchone()

    def watched(self, uid):
        return json.loads(self.row(uid)['watch'])

    async def test_new_colleague_flight_reaches_closed_peer_without_app_read(self):
        positions = [dict(name='B05', latitude=52.36507, longitude=13.5053)]
        await self.own(stand_positions=positions)
        await self.colleague(stand_positions=positions)
        self.comat.provider.lookup.return_value = {'atd': self.now, 'fr24ID': 'actual-leg'}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.provider.lookup.await_count, 1)
        self.assertEqual(self.comat.sender.await_count, 2)
        self.assertEqual(self.watched('reno')[0]['number'], self.flight['number'])
        self.assertTrue(self.watched('reno')[0]['_shared_subscription'])
        self.comat.provider.lookup.return_value = {'ata': self.now+62, 'fr24ID': 'actual-leg'}
        await self.comat.tick(self.now+63)
        self.comat.provider.lookup.return_value = {'aibt': self.now+124, 'fr24ID': 'actual-leg',
            'aibt_source': 'FlightStats', 'aibt_position': dict(latitude=52.36507, longitude=13.5053,
                                                             timestamp=self.now+124, source='adsb_lol')}
        await self.comat.tick(self.now+125)
        flags = [call.args[2]['flag'] for call in self.comat.sender.call_args_list]
        self.assertEqual(flags.count('alarmTakeoff'), 2)
        self.assertEqual(flags.count('alarmLanded'), 2)
        self.assertEqual(flags.count('alarmParking'), 2)
        await self.comat.tick(self.now+140)
        self.assertEqual(self.comat.sender.await_count, 6)

    async def test_each_recipient_keeps_individual_alarm_and_calendar_rules(self):
        await self.own(preferences={'alarmTakeoff': False})
        await self.colleague()
        await self.add_peer(self.flight, firebase_token='muted', server_polling=True, relay_alerts=True,
                            preferences={'active_windows': []})
        self.comat.provider.lookup.return_value = {'atd': self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.assertEqual(self.comat.sender.call_args.args[0], self.row('colleague')['token'])
        self.assertEqual(self.comat.provider.lookup.await_count, 1)

    async def test_group_token_is_used_server_side_without_copying_secret(self):
        await self.own(fr24_token='')
        await self.colleague()
        self.comat.provider.lookup.return_value = {'atd': self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.row('reno')['api_token'], '')
        self.assertEqual(self.comat.provider.lookup.await_count, 1)
        self.assertEqual(self.comat.sender.await_count, 2)
        response = await self.client.get('/comatalarm/v1/state', headers=self.headers)
        result = await response.json()
        self.assertTrue(result['background_ready'])
        self.assertNotIn('private-api-token', json.dumps(result))

    async def test_approved_watch_survives_24_hours_without_phones(self):
        self.flight['reference'] = self.now+26*3600
        await self.own()
        await self.colleague()
        self.comat.provider.lookup.return_value = {'atd': self.now+25*3600}
        await self.comat.tick(self.now+25*3600+1)
        self.assertEqual(self.comat.sender.await_count, 2)
        for row in self.rows():
            self.assertGreater(row['expires'], self.now+25*3600)
            self.assertTrue(json.loads(row['watch']))

    async def test_obsolete_flights_do_not_poll_forever_after_renewal(self):
        await self.own(flights=[self.flight])
        await self.comat.tick(self.now+3*86400)
        self.comat.provider.lookup.assert_not_awaited()
        self.assertEqual(self.watched('reno'), [])

    async def test_approval_failure_does_not_erase_and_cannot_poll_or_push(self):
        self.flight['reference'] = self.now+26*3600
        await self.own(flights=[self.flight])
        self.allowed.remove('reno')
        await self.comat.tick(self.now+25*3600)
        self.comat.provider.lookup.assert_not_awaited()
        self.comat.sender.assert_not_awaited()
        self.assertEqual(len(self.watched('reno')), 1)
        self.assertEqual(self.row('reno')['api_token'], 'private-api-token')
        self.allowed.add('reno')
        self.comat.provider.lookup.return_value = {'atd': self.now+25*3600}
        await self.comat.tick(self.now+25*3600+1)
        self.assertEqual(self.comat.sender.await_count, 1)

    async def test_legacy_and_blocked_devices_do_not_join_shared_group(self):
        await self.own()
        await self.colleague()
        await self.add_peer(self.flight, flights=[], server_polling=True, relay_alerts=True)
        await self.add_peer(self.flight, flights=[], firebase_token='muted', server_polling=True, relay_alerts=True)
        self.allowed.remove('muted')
        self.comat.provider.lookup.return_value = {'atd': self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.sender.await_count, 2)
        self.assertEqual(self.watched('muted'), [])
        legacy = next(r for r in self.rows() if not r['firebase_uid'])
        self.assertEqual(json.loads(legacy['watch']), [])

    async def test_revoke_during_approval_cannot_recreate_registration(self):
        await self.own()
        await self.colleague()
        original = self.comat.stand_access.side_effect
        async def revoke(uid):
            if uid == 'reno':
                self.comat.db.execute('DELETE FROM comat_devices WHERE id=?', (self.id,))
                self.comat.db.commit()
            return await original(uid)
        self.comat.stand_access.side_effect = revoke
        self.comat.provider.lookup.return_value = {'atd': self.now}
        await self.comat.tick(self.now+1)
        self.assertIsNone(self.row('reno'))
        self.assertEqual(self.comat.sender.await_count, 1)

    async def test_stale_empty_phone_handoff_preserves_shared_subscription(self):
        await self.own()
        await self.colleague()
        await self.comat.tick(self.now+1)
        await self.own(flights=[])
        self.assertEqual(len(self.watched('reno')), 1)

    async def test_explicit_deletion_blocks_stale_reposts_and_all_pending_pushes(self):
        await self.own()
        peer = await self.colleague()
        self.comat.provider.lookup.return_value = {'atd': self.now}
        self.comat.sender.return_value = False
        await self.comat.tick(self.now+1)
        removal = dict(self.flight, deleted_at=self.now+2)
        await self.own(removed_flights=[removal])
        self.assertTrue(all(json.loads(r['watch']) == [] for r in self.rows()))
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_outbox WHERE sent=0').fetchone()[0], 0)
        response = await self.client.post('/comatalarm/v1/watch', headers=peer, json=dict(
            flights=[self.flight], preferences={}, foreground=True, server_polling=True,
            relay_alerts=True, firebase_token='colleague'))
        self.assertEqual(response.status, 200)
        await self.comat.tick(self.now+63)
        self.assertTrue(all(json.loads(r['watch']) == [] for r in self.rows()))

    async def test_explicit_readd_newer_than_tombstone_is_permitted(self):
        await self.own(flights=[self.flight])
        await self.colleague()
        await self.own(removed_flights=[dict(self.flight, deleted_at=self.now+1)])
        renewed = dict(self.flight, id='recreated-flight', created_at=self.now+2)
        await self.own(flights=[renewed])
        self.comat.provider.lookup.return_value = {'atd': self.now+3}
        await self.comat.tick(self.now+4)
        self.assertEqual(len(self.watched('reno')), 1)
        self.assertEqual(len(self.watched('colleague')), 1)
        self.assertEqual(self.comat.sender.await_count, 2)

    async def test_old_and_unknown_deletions_do_not_block_valid_handoff(self):
        await self.own(flights=[self.flight], removed_flights=[
            dict(self.flight, reference=self.now-60*86400, deleted_at=self.now),
            {'id': 'unknown-document', 'deleted_at': self.now},
            dict(self.flight, deleted_at=self.now-8*86400)])
        self.assertEqual(len(self.watched('reno')), 1)

    async def test_id_only_deletion_accepts_canonical_case_and_stays_scoped(self):
        await self.own(flights=[self.flight])
        await self.colleague()
        await self.own(removed_flights=[{'id': self.flight['id'].upper(), 'deleted_at': self.now+1}])
        self.assertTrue(all(json.loads(r['watch']) == [] for r in self.rows()))

    async def test_token_rotation_needs_existing_approval_but_not_master_phone(self):
        await self.own(flights=[self.flight])
        response = await self.own(flights=[self.flight], push_token='b'*64, push_environment='production')
        self.assertTrue((await response.json())['token_refreshed'])
        self.assertEqual(self.row('reno')['token'], 'b'*64)
        self.comat.provider.lookup.return_value = {'atd': self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.sender.call_args.args[0], 'b'*64)
        self.allowed.remove('reno')
        response = await self.watch(push_token='c'*64, push_environment='production', firebase_token='reno')
        self.assertEqual(response.status, 403)
        self.assertEqual(self.row('reno')['token'], 'b'*64)

    async def test_token_rotation_cannot_take_another_device_identity(self):
        await self.own()
        await self.colleague()
        other_token = self.row('colleague')['token']
        response = await self.watch(firebase_token='reno', server_polling=True,
                                    push_token=other_token, push_environment='production')
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.rows()), 2)
        self.assertNotEqual(self.row('reno')['token'], other_token)

    async def test_long_calendar_window_is_valid_and_expiry_remains_muted(self):
        response = await self.own(flights=[self.flight], preferences={
            'active_windows': [[self.now+12*86400, self.now+13*86400]],
            'calendar_valid_until': self.now+14*86400})
        self.assertEqual(response.status, 200)
        prefs = json.loads(self.row('reno')['preferences'])
        self.assertTrue(self.comat.enabled(prefs, self.now+12*86400+1))
        self.assertFalse(self.comat.enabled(prefs, self.now+15*86400))
        self.comat.db.execute('UPDATE comat_devices SET preferences=? WHERE id=?',
            (json.dumps({'enabled': True, 'active_windows': [[self.now-100, self.now+100]],
                        'calendar_valid_until': self.now-1}), self.id))
        self.comat.db.commit()
        self.comat.provider.lookup.return_value = {'atd': self.now}
        await self.comat.tick(self.now+1)
        self.comat.sender.assert_not_awaited()
        self.assertIn('Kalenderdaten veraltet', self.row('reno')['error'])

    async def test_new_watch_wakes_worker_and_resets_poll_due_time(self):
        await self.own()
        await self.comat.tick(self.now+1)
        self.comat.poll_wakeup.clear()
        await self.own(flights=[self.flight])
        self.assertTrue(self.comat.poll_wakeup.is_set())
        self.assertEqual(self.row('reno')['last_poll'], 0)
        await self.comat.tick(self.now+2)
        self.comat.provider.lookup.assert_awaited_once()

    async def test_damaged_device_snapshot_does_not_stop_other_subscribers(self):
        await self.own()
        await self.colleague()
        self.comat.db.execute('UPDATE comat_devices SET watch=? WHERE id=?', ('not-json', self.id))
        self.comat.db.commit()
        self.comat.provider.lookup.return_value = {'atd': self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.assertEqual(self.comat.sender.call_args.args[0], self.row('colleague')['token'])

    async def test_provider_milestone_during_authorization_survives_stale_post(self):
        await self.own(flights=[self.flight])
        original = self.comat.bind_stand_access
        async def while_authorizing(device, body):
            access = await original(device, body)
            current = dict(self.flight, ata=self.now-10,
                           _landing_alert={'triggered_at': self.now-200},
                           _prelanding={'triggered_at': self.now-200})
            self.comat.db.execute('UPDATE comat_devices SET watch=? WHERE id=?',
                                  (json.dumps([current]), self.id))
            self.comat.db.commit()
            return access
        self.comat.bind_stand_access = while_authorizing
        await self.own(flights=[self.flight])
        current = self.watched('reno')[0]
        self.assertEqual(current['ata'], self.now-10)
        self.assertEqual(current['_landing_alert']['triggered_at'], self.now-200)
        self.assertEqual(current['_prelanding']['triggered_at'], self.now-200)

    async def queued_push(self):
        await self.own(flights=[self.flight])
        self.comat.sender.return_value = False
        self.comat.provider.lookup.return_value = {'atd': self.now}
        await self.comat.tick(self.now+1)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.comat.sender.reset_mock()
        self.comat.sender.return_value = True

    async def test_revoke_during_delivery_authorization_prevents_send(self):
        await self.queued_push()
        async def revoked(uid):
            self.comat.db.execute('DELETE FROM comat_devices WHERE id=?', (self.id,))
            self.comat.db.commit()
            return {'uid': uid, 'administrator': True}
        self.comat.stand_access.side_effect = revoked
        await self.comat.deliver(self.id, self.now+2)
        self.comat.sender.assert_not_awaited()

    async def test_delete_during_delivery_authorization_prevents_send(self):
        await self.queued_push()
        async def removed(uid):
            self.comat.db.execute("UPDATE comat_devices SET watch='[]' WHERE id=?", (self.id,))
            self.comat.db.commit()
            return {'uid': uid, 'administrator': True}
        self.comat.stand_access.side_effect = removed
        await self.comat.deliver(self.id, self.now+2)
        self.comat.sender.assert_not_awaited()

    async def test_token_rotation_during_delivery_uses_current_destination(self):
        await self.queued_push()
        async def rotated(uid):
            self.comat.db.execute('UPDATE comat_devices SET token=? WHERE id=?', ('b'*64, self.id))
            self.comat.db.commit()
            return {'uid': uid, 'administrator': True}
        self.comat.stand_access.side_effect = rotated
        await self.comat.deliver(self.id, self.now+2)
        self.comat.sender.assert_awaited_once()
        self.assertEqual(self.comat.sender.call_args.args[0], 'b'*64)


if __name__ == '__main__':
    unittest.main()
