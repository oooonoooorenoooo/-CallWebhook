"""Authenticated monthly backfill, immutable evidence and deletion regressions."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from push_relay.history_sync import HistorySync, day_of
from push_relay.monitor_actions import identity, key_of
from push_relay.tests import test_comatalarm as fixtures
from push_relay.tests import test_shared_monitoring as shared_fixtures
from push_relay.tests import test_monitor_actions as operator_fixtures


class HistorySyncTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.ComatAlarmTests.asyncTearDown
    register = fixtures.ComatAlarmTests.register
    watch = fixtures.ComatAlarmTests.watch
    add_peer = fixtures.ComatAlarmTests.add_peer
    own = shared_fixtures.SharedMonitoringTests.own
    colleague = shared_fixtures.SharedMonitoringTests.colleague
    row = shared_fixtures.SharedMonitoringTests.row
    watched = shared_fixtures.SharedMonitoringTests.watched
    command = operator_fixtures.OperatorTests.command
    seed_arrival = operator_fixtures.OperatorTests.seed_arrival

    async def asyncSetUp(self):
        await shared_fixtures.SharedMonitoringTests.asyncSetUp(self)
        self.now = datetime(2026, 11, 18, 12, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        self.clock = patch('push_relay.comatalarm.time.time', return_value=self.now)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.flight.update(reference=self.now-100, created_at=self.now-120, fr24ID='abc123')

    def completed(self, **fields):
        value = dict(self.flight, atd=self.now-3600, ata=self.now-150, aibt=self.now-90,
                     stand='B06', gate='C03', gate_source='BER', packets='3/2',
                     aibt_position=dict(latitude=52.36507, longitude=13.5053, timestamp=self.now-90))
        value.update(fields)
        return value

    async def read(self, **fields):
        body = dict(history_sync=True, offset=0, revision='')
        body.update(fields)
        response = await self.client.post('/comatalarm/v1/watch', headers=self.headers, json=body)
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())['history']

    def save_watch(self, rows):
        with self.comat.db:
            self.comat.db.execute('UPDATE comat_devices SET watch=? WHERE id=?', (json.dumps(rows), self.id))

    def archive(self, flight, **extra):
        value = {k: v for k, v in flight.items() if k not in ('id', 'created_at', 'aibt_position')}
        value.update(day=day_of(flight['reference']), category='completed', packets={'in': 3, 'out': 2})
        if '_schedule' in value:
            value['schedule'] = value.pop('_schedule')
        value.update(extra)
        value['key'] = key_of(value)
        with self.comat.db:
            self.comat.db.execute('INSERT OR REPLACE INTO comat_monitor_history VALUES(?,?,?,?)',
                                 (identity(value), value['day'], self.now, json.dumps(value)))
        return value

    async def test_pi_tracked_completed_flight_syncs_after_watch_disappears_without_io(self):
        await self.own(stand_positions=[dict(name='B06', latitude=52.36507, longitude=13.5053)])
        flight = self.seed_arrival()
        response = await self.command('track', flight['key'])
        self.assertEqual(response.status, 200)
        original = next(f for f in self.watched('reno') if f['number'] == flight['number'])
        self.comat.provider.lookup.return_value = dict(fr24ID='feed123', atd=self.now-3600,
            ata=self.now-120, aibt=self.now-30, stand='B06',
            aibt_position=dict(latitude=52.36507, longitude=13.5053, timestamp=self.now-30))
        await self.comat.tick(self.now+1)
        self.save_watch([])
        self.comat.provider.lookup.reset_mock()
        self.comat.sender.reset_mock()
        before = dict(self.row('reno'))
        history = await self.read()
        item = history['flights'][0]
        self.assertRegex(item['id'], r'^PI_HISTORY_[a-f0-9]{64}$')
        self.assertIn(original['id'], item['entry_aliases'])
        self.assertEqual(item['aibt_position']['timestamp'], self.now-30)
        self.assertEqual(item['stand'], 'B06')
        self.assertTrue(item['archived'])
        self.assertEqual(dict(self.row('reno')), before, 'History reads cannot alter watches, preferences or expiry')
        self.comat.provider.lookup.assert_not_awaited()
        self.comat.sender.assert_not_awaited()

    async def test_approved_old_watch_is_captured_before_48h_filter_after_restart(self):
        await self.own()
        old = self.completed(reference=self.now-3*86400, created_at=self.now-3*86400-3600,
                             atd=self.now-3*86400, ata=self.now-3*86400+3600,
                             aibt=self.now-3*86400+3900)
        self.save_watch([old])
        self.comat.history_sync = HistorySync(self.comat)
        await self.comat.tick(self.now)
        self.assertEqual(self.watched('reno'), [])
        self.assertEqual((await self.read())['flights'][0]['number'], old['number'])
        self.comat.provider.lookup.assert_not_awaited()
        self.comat.sender.assert_not_awaited()

    async def test_handoff_captures_completed_old_watch_before_removing_it(self):
        await self.own()
        old = self.completed(reference=self.now-3*86400, atd=self.now-3*86400,
                             ata=self.now-3*86400+3600, aibt=self.now-3*86400+3900)
        self.save_watch([old])
        await self.own(flights=[])
        self.assertEqual(self.watched('reno'), [])
        self.assertEqual((await self.read())['flights'][0]['aibt'], old['aibt'])

    async def test_private_legacy_admin_projection_is_not_shared(self):
        await self.own()
        self.archive(self.completed(number='LH999', fr24ID=None), subscribers=[{'device': 'private-device'}],
                     credential='private-credential', token='secret-token')
        history = await self.read()
        self.assertEqual(history['flights'], [])
        response = await self.client.get('/comatalarm/v1/state', headers=self.headers)
        self.assertTrue((await response.json())['history_sync_supported'])
        self.assertNotIn('private', await response.text())

    async def test_legacy_manual_operator_provenance_restores_projection_and_original_pi_alias(self):
        await self.own()
        f = self.archive(self.completed(), schedule={'sta': self.now-100, 'gate': 'C03'})
        manual = dict(id='PI_'+key_of(f)[:32], number=f['number'], reference=f['reference'],
                      origin=f['origin'], destination=f['destination'])
        with self.comat.db:
            self.comat.db.execute('INSERT INTO comat_manual VALUES(?,?,?)',
                (identity(manual), json.dumps(manual), self.now+80000))
        result = (await self.read())['flights'][0]
        self.assertIn(manual['id'], result['entry_aliases'])
        self.assertEqual(result['created_at'], self.now+80000-86400)
        self.assertEqual(result['packets'], '3/2')
        self.assertEqual(result['_schedule']['sta'], self.now-100)
        self.assertEqual(result['stand'], 'B06')
        self.assertNotIn('aibt_position', result, 'Absent legacy coordinates must not be invented')

    async def test_unique_operator_audit_restores_old_pi_only_archive(self):
        await self.own()
        f = self.archive(self.completed(fr24ID=None))
        self.comat.monitor.record('service', 'Über Ankunftstabelle zur Pi-Überwachung hinzugefügt',
                                  f['number'], now=self.now-200)
        item = (await self.read())['flights'][0]
        self.assertIn('PI_'+key_of(f)[:32], item['entry_aliases'])
        self.assertEqual(item['created_at'], self.now-200)
        self.assertNotIn('fr24ID', item)

    async def test_ambiguous_or_wrong_day_operator_evidence_does_not_authorize_archive(self):
        await self.own()
        first = self.archive(self.completed())
        self.archive(self.completed(origin='HAM'))
        self.comat.monitor.record('service', 'Über Ankunftstabelle zur Pi-Überwachung hinzugefügt',
                                  first['number'], now=self.now-200)
        other = self.archive(self.completed(number='LH999'))
        self.comat.monitor.record('service', 'Über Ankunftstabelle zur Pi-Überwachung hinzugefügt',
                                  other['number'], now=self.now-86400)
        self.assertEqual((await self.read())['flights'], [])

    async def test_unbound_and_blocked_devices_cannot_read_shared_history(self):
        self.comat.history_sync.capture([self.completed()], self.now)
        response = await self.client.post('/comatalarm/v1/watch', headers=self.headers, json={'history_sync': True})
        self.assertEqual(response.status, 403)
        await self.own()
        self.allowed.remove('reno')
        response = await self.client.post('/comatalarm/v1/watch', headers=self.headers, json={'history_sync': True})
        self.assertEqual(response.status, 403)
        self.comat.sender.assert_not_awaited()

    async def test_revoke_or_identity_change_during_authorization_blocks_history_and_state(self):
        for path in ('history', 'state'):
            for mutation in ('delete', 'credential', 'uid'):
                with self.subTest(path=path, mutation=mutation):
                    with self.comat.db:
                        self.comat.db.execute('DELETE FROM comat_devices WHERE id=?', (self.id,))
                    response = await self.register()
                    self.headers = {'Authorization': 'Bearer '+(await response.json())['credential']}
                    self.allowed.add('reno')
                    self.comat.stand_access = AsyncMock(return_value={'uid': 'reno', 'administrator': True})
                    await self.own()
                    async def changed(uid):
                        with self.comat.db:
                            if mutation == 'delete':
                                self.comat.db.execute('DELETE FROM comat_devices WHERE id=?', (self.id,))
                            elif mutation == 'credential':
                                self.comat.db.execute("UPDATE comat_devices SET credential='rotated' WHERE id=?", (self.id,))
                            else:
                                self.comat.db.execute("UPDATE comat_devices SET firebase_uid='another' WHERE id=?", (self.id,))
                        return {'uid': uid, 'administrator': True}
                    self.comat.stand_access = AsyncMock(side_effect=changed)
                    if path == 'history':
                        result = await self.client.post('/comatalarm/v1/watch', headers=self.headers, json={'history_sync': True})
                    else:
                        result = await self.client.get('/comatalarm/v1/state', headers=self.headers)
                    self.assertIn(result.status, (401, 403))
                    self.assertNotIn('PI_HISTORY_', await result.text())

    async def test_revocation_during_poll_authorization_cannot_capture_history(self):
        await self.own()
        self.save_watch([self.completed()])
        device = self.row('reno')
        async def revoke(uid):
            with self.comat.db:
                self.comat.db.execute('DELETE FROM comat_devices WHERE id=?', (self.id,))
            return {'uid': uid, 'administrator': True}
        self.comat.stand_access = AsyncMock(side_effect=revoke)
        await self.comat.poll_device(device, self.now)
        self.assertEqual(self.comat.history_sync.rows(self.now), [])
        self.comat.provider.lookup.assert_not_awaited()

    async def test_page_revision_is_stable_and_changes_atomically_with_deleted_rows(self):
        await self.own()
        self.comat.history_sync.capture([self.completed(id=f'flight-{i}', number=f'LH{1000+i}', fr24ID=f'{i:08x}')
                                        for i in range(105)], self.now)
        first = await self.read()
        self.assertTrue(0 < len(first['flights']) <= 100)
        second = await self.read(offset=first['next_offset'], revision=first['revision'])
        self.assertEqual(len(second['flights']), 105-len(first['flights']))
        self.assertIsNone(second['next_offset'])
        self.assertEqual(len({f['id'] for f in first['flights']+second['flights']}), 105)
        unchanged = await self.read(revision=first['revision'])
        self.assertTrue(unchanged['unchanged'])
        self.assertEqual(unchanged['flights'], [])
        deleted = first['flights'][0]
        await self.own(removed_flights=[{'id': deleted['id'], 'deleted_at': self.now}])
        response = await self.client.post('/comatalarm/v1/watch', headers=self.headers,
            json={'history_sync': True, 'offset': first['next_offset'], 'revision': first['revision']})
        self.assertEqual(response.status, 409)
        self.assertEqual((await self.read())['count'], 104)

    async def test_page_size_limit_and_bad_offsets(self):
        await self.own()
        large = self.completed(id='big')
        large['entry_aliases'] = [f'{i:03d}'+'a'*150 for i in range(128)]
        self.comat.history_sync.capture([dict(large, number=f'LH{i+100}', fr24ID=f'{i:08x}') for i in range(5)], self.now)
        first = await self.read()
        self.assertLess(len(first['flights']), 5)
        self.assertIsNotNone(first['next_offset'])
        for offset in (-1, True, 50001, 0.5):
            response = await self.client.post('/comatalarm/v1/watch', headers=self.headers,
                                             json={'history_sync': True, 'offset': offset})
            self.assertEqual(response.status, 400)

    async def test_fifty_history_page_reads_have_a_separate_budget_from_watch_and_providers(self):
        await self.own()
        self.comat.history_sync.capture([self.completed()], self.now)
        first = await self.read()
        for _ in range(49):
            result = await self.read(revision=first['revision'])
            self.assertTrue(result['unchanged'])
        await self.own()
        self.comat.provider.lookup.assert_not_awaited()
        self.comat.sender.assert_not_awaited()

    async def test_monthly_retention_removes_previous_month_without_provider_calls(self):
        await self.own()
        self.comat.history_sync.capture([self.completed()], self.now)
        december = datetime(2026, 12, 1, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        result = self.comat.history_sync.read({}, december)['history']
        self.assertEqual(result['month'], '2026-12')
        self.assertEqual(result['flights'], [])
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_app_history').fetchone()[0], 0)
        self.comat.provider.lookup.assert_not_awaited()

    async def test_month_boundary_uses_actual_history_day_without_closing_active_flights(self):
        await self.own()
        reference = datetime(2026, 11, 30, 23, 55, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        midnight = datetime(2026, 12, 1, 0, 10, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        f = dict(self.flight, reference=reference, created_at=reference-3600, atd=reference-7200)
        self.comat.history_sync.capture([f], reference)
        self.comat.history_sync.capture([f], midnight)
        self.assertEqual(self.comat.history_sync.read({}, midnight)['history']['flights'], [])
        f.update(ata=midnight+300, aibt=midnight+600)
        self.comat.history_sync.capture([f], midnight+601)
        item = self.comat.history_sync.read({}, midnight+601)['history']['flights'][0]
        self.assertEqual(item['reference'], reference)
        self.assertEqual(item['history_day'], '2026-12-01')
        self.assertEqual(self.comat.history_sync.metadata(midnight+601)['month'], '2026-12')

    async def test_month_boundary_tombstone_retains_current_month_completion(self):
        await self.own()
        reference = datetime(2026, 11, 30, 23, 55, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        landed = datetime(2026, 12, 1, 0, 20, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        later = landed+10*86400
        f = self.completed(reference=reference, created_at=reference-3600, atd=reference-7200,
                           ata=landed-300, aibt=landed)
        self.comat.history_sync.capture([f], landed+1)
        self.comat.shared_monitoring.remove([dict(f, deleted_at=landed+2)], landed+2)
        self.comat.shared_monitoring.remove([], later)
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_shared_removed').fetchone()[0], 1)
        self.assertEqual(self.comat.history_sync.read({}, later)['history']['flights'], [])

    async def test_month_old_history_can_be_deleted_by_stable_id_and_original_alias(self):
        await self.own()
        for use_alias in (False, True):
            f = self.completed(id='Original-UUID-'+str(use_alias), number='LH'+('188' if use_alias else '186'),
                               fr24ID='a' if use_alias else 'b', reference=self.now-14*86400,
                               created_at=self.now-14*86400-120, atd=self.now-14*86400,
                               ata=self.now-14*86400+600, aibt=self.now-14*86400+900)
            self.comat.history_sync.capture([f], self.now)
            item = next(x for x in (await self.read())['flights'] if x['number'] == f['number'])
            identifier = f['id'].lower() if use_alias else item['id']
            await self.own(removed_flights=[{'id': identifier, 'deleted_at': self.now}])
            self.assertFalse(any(x['number'] == f['number'] for x in (await self.read())['flights']))

    async def test_tombstone_survives_seven_days_and_blocks_legacy_backfill(self):
        await self.own()
        f = self.completed(reference=self.now-12*86400, created_at=self.now-12*86400-300,
                           atd=self.now-12*86400, ata=self.now-12*86400+600, aibt=self.now-12*86400+900)
        self.archive(f)
        self.comat.monitor.record('service', 'Über Ankunftstabelle zur Pi-Überwachung hinzugefügt',
                                  f['number'], now=f['reference']-300)
        await self.own(removed_flights=[dict(f, deleted_at=self.now-10*86400)])
        self.comat.shared_monitoring.remove([], self.now)
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_shared_removed').fetchone()[0], 1)
        self.assertEqual((await self.read())['flights'], [])

    async def test_monotone_facts_and_late_id_preserve_history_identity(self):
        await self.own()
        f = self.completed(fr24ID=None)
        self.comat.history_sync.capture([f], self.now)
        first = (await self.read())['flights'][0]
        self.comat.history_sync.capture([dict(self.flight, id='second-device', fr24ID='actual-id')], self.now+10)
        second = (await self.read())['flights'][0]
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['aibt'], f['aibt'])
        self.assertEqual(second['stand'], f['stand'])
        self.assertEqual(second['fr24ID'], 'actual-id')
        self.assertIn('second-device', second['entry_aliases'])

    async def test_distinct_real_legs_with_same_alias_are_separate_and_ambiguous_delete_is_ignored(self):
        await self.own()
        one, two = self.completed(fr24ID='one'), self.completed(fr24ID='two')
        self.comat.history_sync.capture([one, two], self.now)
        self.assertEqual((await self.read())['count'], 2)
        self.assertIsNone(self.comat.history_sync.resolve(one['id'], self.now))
        await self.own(removed_flights=[{'id': one['id'], 'deleted_at': self.now}])
        self.assertEqual((await self.read())['count'], 2)

    async def test_unknown_creation_time_never_claims_later_readdition(self):
        await self.own()
        f = self.completed()
        f.pop('created_at')
        self.comat.history_sync.capture([f], self.now)
        self.assertEqual((await self.read())['flights'][0]['created_at'], 0)
        await self.own(removed_flights=[dict(f, deleted_at=self.now-200)])
        self.assertEqual((await self.read())['flights'], [])

    async def test_explicit_new_creation_after_deletion_can_be_captured_without_old_evidence(self):
        await self.own()
        f = self.completed(created_at=self.now-300)
        self.comat.history_sync.capture([f], self.now)
        await self.own(removed_flights=[dict(f, deleted_at=self.now-200)])
        new = dict(self.flight, id='new-instance', created_at=self.now-100)
        self.comat.history_sync.capture([new], self.now)
        self.assertEqual((await self.read())['flights'], [], 'Re-add does not inherit old completion')
        self.comat.history_sync.capture([dict(new, aibt=self.now-1, ata=self.now-10)], self.now)
        restored = (await self.read())['flights'][0]
        self.assertEqual(restored['created_at'], self.now-100)
        self.assertEqual(restored['entry_aliases'], ['new-instance'])

    async def test_open_outbound_and_delayed_previous_day_flight_are_not_archived(self):
        await self.own()
        f = self.completed(track_outbound=True, _outbound={'id': 'outgoing', 'number': 'LH187'})
        yesterday = dict(self.flight, id='late', number='LH999', fr24ID='late-leg',
                         reference=self.now-86400, atd=self.now-3600)
        self.comat.history_sync.capture([f, yesterday], self.now)
        self.assertEqual((await self.read())['flights'], [])
        f['_outbound']['atd'] = self.now-1
        self.comat.history_sync.capture([f], self.now)
        restored = (await self.read())['flights']
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['completed_at'], self.now-1)

    async def test_replacing_nested_leg_cannot_inherit_other_legs_actuals_or_gps(self):
        await self.own()
        inbound = dict(id='old-in', number='LH100', registration='D-OLD', ata=self.now-300,
                       aibt=self.now-200, aibt_position=self.completed()['aibt_position'])
        f = self.completed(origin='BER', destination='FRA', _inbound=inbound)
        self.comat.history_sync.capture([f], self.now)
        new = dict(self.flight, origin='BER', destination='FRA', _inbound={'id': 'new-in', 'registration': 'D-NEW'})
        self.comat.history_sync.capture([new], self.now)
        item = (await self.read())['flights'][0]
        self.assertEqual(item['_inbound'], new['_inbound'])
        self.assertNotIn('stand', item)
        outward = self.completed(number='LH199', fr24ID='out-parent', track_outbound=True,
                                _outbound={'id': 'old-out', 'number': 'LH200', 'atd': self.now-10})
        self.comat.history_sync.capture([outward], self.now)
        self.comat.history_sync.capture([dict(outward, _outbound={'id': 'new-out', 'number': 'LH202'})], self.now)
        self.assertFalse(any(x['number'] == 'LH199' for x in (await self.read())['flights']))

    async def test_history_rows_cannot_be_reintroduced_as_active_watches(self):
        await self.own()
        self.comat.history_sync.capture([self.completed()], self.now)
        item = (await self.read())['flights'][0]
        await self.own(flights=[item])
        self.assertEqual(self.watched('reno'), [])
        await self.comat.tick(self.now)
        self.comat.provider.lookup.assert_not_awaited()
        self.comat.sender.assert_not_awaited()

    async def test_history_import_handoff_preserves_only_existing_bounded_coordinate_repair(self):
        f = self.completed()
        f.pop('aibt_position')
        await self.own(flights=[f],
                       stand_positions=[dict(name='B06', latitude=52.36507, longitude=13.5053)])
        self.assertEqual((await self.read())['count'], 1)
        await self.own(flights=[])
        self.assertEqual(len(self.watched('reno')), 1)
        self.assertTrue(self.watched('reno')[0].get('_arrival_recovery_only'))
        await self.add_peer(f, flights=[], firebase_token='colleague',
                            server_polling=True, relay_alerts=True)
        self.comat.provider.lookup.return_value = {'aibt_position': self.completed()['aibt_position']}
        await self.comat.tick(self.now)
        self.comat.sender.assert_not_awaited()
        self.assertEqual(self.watched('reno')[0]['stand'], 'B06',
                         'A real local match remains a silent history repair')
        self.assertTrue(self.watched('reno')[0].get('_arrival_recovery_only'))
        self.assertEqual(self.watched('colleague'), [],
                         'A repair-only history row cannot create a new peer subscription')
        self.assertEqual((await self.read())['flights'][0]['aibt_position']['timestamp'], self.now-90)
        await self.own(flights=[])
        self.assertEqual(self.watched('reno'), [])
        before = self.comat.provider.lookup.await_count
        await self.comat.tick(self.now+61)
        self.assertEqual(self.comat.provider.lookup.await_count, before)

    async def test_gate_only_and_legacy_stand_never_fabricate_parking_coordinates(self):
        await self.own()
        f = self.completed()
        f.pop('aibt_position')
        self.comat.history_sync.capture([f], self.now)
        item = (await self.read())['flights'][0]
        self.assertEqual(item['stand'], 'B06')
        self.assertEqual(item['gate'], 'C03')
        self.assertNotIn('aibt_position', item)
        self.assertNotIn('parking_confirmed', item)

    async def test_operator_delete_removes_both_archives_and_prevents_reimport(self):
        await self.own(flights=[self.completed()])
        key = self.comat.monitor.snapshot(self.now)['flights'][0]['key']
        self.assertEqual((await self.command('delete', key)).status, 200)
        self.assertEqual((await self.read())['flights'], [])
        self.assertEqual(self.comat.db.execute('SELECT count(*) FROM comat_app_history').fetchone()[0], 0)
        self.comat.sender.assert_not_awaited()

    async def test_operator_delete_finds_retained_app_history_after_old_dashboard_projection_is_gone(self):
        await self.own()
        f = self.completed()
        self.comat.history_sync.capture([f], self.now)
        self.assertEqual((await self.command('delete', key_of(f))).status, 200)
        self.assertEqual((await self.read())['flights'], [])

    async def test_history_keeps_real_actuals_for_large_schedule_delays(self):
        await self.own()
        f = self.completed(reference=self.now-36*3600)
        self.comat.history_sync.capture([f], self.now)
        result = (await self.read())['flights'][0]
        self.assertEqual(result['aibt'], f['aibt'])
        self.assertEqual(result['reference'], f['reference'])
        self.assertEqual(result['history_day'], day_of(f['aibt']))

    async def test_same_nested_leg_keeps_actuals_across_operating_number_alias(self):
        await self.own()
        inbound = dict(id='same-in', number='LH100', registration='D-OLD', ata=self.now-300,
                       aibt=self.now-200, aibt_position=self.completed()['aibt_position'])
        f = self.completed(origin='BER', destination='FRA', _inbound=inbound)
        self.comat.history_sync.capture([f], self.now)
        new = dict(self.flight, origin='BER', destination='FRA',
                   _inbound={'id': 'same-in', 'number': 'UA123', 'registration': 'DOLD'})
        self.comat.history_sync.capture([new], self.now)
        item = (await self.read())['flights'][0]
        self.assertEqual(item['_inbound']['aibt'], inbound['aibt'])
        self.assertEqual(item['_inbound']['aibt_position'], inbound['aibt_position'])


if __name__ == '__main__':
    unittest.main()
