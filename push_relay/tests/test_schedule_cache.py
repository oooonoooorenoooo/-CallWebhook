"""Supplemental provider jobs cannot hold the closed-app alarm worker hostage."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from push_relay.flight_schedule import ScheduleCache
from push_relay.tests import test_fast_alert_polling as fast_fixtures
from push_relay.tests import test_fr24_batch as batch_fixtures


class ScheduleIsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fast_fixtures.FastAlertPollingTests.asyncSetUp
    asyncTearDown = fast_fixtures.FastAlertPollingTests.asyncTearDown
    register = fast_fixtures.FastAlertPollingTests.register
    watch = fast_fixtures.FastAlertPollingTests.watch
    add_peer = fast_fixtures.FastAlertPollingTests.add_peer
    own = fast_fixtures.FastAlertPollingTests.own
    row = fast_fixtures.FastAlertPollingTests.row
    watched = fast_fixtures.FastAlertPollingTests.watched
    tick = fast_fixtures.FastAlertPollingTests.tick
    positions = staticmethod(fast_fixtures.FastAlertPollingTests.positions)
    event = fast_fixtures.FastAlertPollingTests.event

    def configure(self, flights):
        fast_fixtures.FastAlertPollingTests.configure(self, flights)
        self.provider = self.comat.provider
        self.addAsyncCleanup(self.provider.schedules.close)
        self.addAsyncCleanup(self.provider.close_arrivals)

    async def finish_jobs(self):
        await asyncio.gather(*list(self.provider.schedules.tasks.values()))
        await asyncio.sleep(0)  # Let completed-task cleanup callbacks run.

    async def finish_arrivals(self):
        await asyncio.gather(*list(self.provider.arrival_tasks.values()))
        await asyncio.sleep(0)

    def alternative_after_first_tick(self):
        async def lookup(session, flight, now, **kwargs):
            if now <= self.now:
                return {'_arrival_sources_status': 'no_aibt'}
            return {'aibt': self.now+1, 'aibt_source': 'FlightStats',
                    'aibt_position': dict(latitude=52.36507, longitude=13.5053, timestamp=self.now+1,
                                          source='adsb_lol', on_ground=True, ground_speed=0),
                    '_arrival_sources_status': 'complete'}
        self.provider.arrivals.lookup = AsyncMock(side_effect=lookup)

    async def test_alternative_appearing_during_blocked_schedule_reaches_both_closed_apps(self):
        air, taxi = batch_fixtures.flights(2, self.now)
        taxi['ata'] = self.now-120
        self.configure([air, taxi])
        self.alternative_after_first_tick()
        await self.own(flights=[air, taxi], stand_positions=self.positions())
        await self.add_peer(air, flights=[], firebase_token='colleague', server_polling=True,
                            relay_alerts=True, stand_positions=self.positions())
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            entered.set()
            await release.wait()
            return {'sta': self.now+600}
        self.schedule.side_effect = blocked
        try:
            await asyncio.wait_for(self.tick(0), 1)
            await self.finish_arrivals()
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(self.provider.schedules.tasks)
            self.assertFalse(release.is_set())
            self.comat.sender.assert_not_awaited()
            # FR24 was empty and must never be tried again for this occurrence.
            # An independent actual/position pair appears while schedule work
            # remains blocked; the wakeup must commit it to both closed apps.
            actual = self.now+1
            fr24_count = len(self.http.calls)
            await asyncio.wait_for(self.tick(15), 1)
            await self.finish_arrivals()
            await asyncio.wait_for(self.tick(15), 1)
            self.assertFalse(release.is_set())
            self.assertTrue(any(not t.done() for t in self.provider.schedules.tasks.values()))
            for uid in ('reno', 'colleague'):
                saved = next(f for f in self.watched(uid) if f['id'] == taxi['id'])
                self.assertEqual(saved['aibt'], actual)
                self.assertEqual(saved['aibt_position']['timestamp'], actual)
                self.assertEqual(saved['stand'], 'B06')
            self.assertEqual(self.comat.sender.await_count, 2)
            self.assertTrue(all(c.args[2]['flag'] == 'alarmParking' for c in self.comat.sender.call_args_list))
            self.assertEqual(len(self.http.calls), fr24_count, 'Alternative completion must not retry FR24')
        finally:
            release.set()
            await self.finish_jobs()

    async def test_completed_schedule_is_applied_next_pass_without_another_fr24_call(self):
        f = batch_fixtures.flights(1, self.now)[0]
        self.configure([f])
        self.schedule.return_value = {'gate': 'A21', 'gate_source': 'FlightStats', 'sta': self.now+600}
        await self.own(flights=[f])
        await self.tick(0)
        await self.finish_jobs()
        count = len(self.http.calls)
        await self.tick(15)
        saved = self.watched('reno')[0]
        self.assertEqual(saved['gate'], 'A21')
        self.assertEqual(saved['_schedule']['sta'], self.now+600)
        self.assertEqual(saved['_schedule_checked_at'], self.now)
        self.assertNotIn('aibt', saved)
        self.assertEqual(len(self.http.calls), count)
        self.assertEqual(self.comat.sender.await_count, 1)
        self.assertEqual(self.comat.sender.call_args.args[2]['flag'], 'alarmGate')

    async def test_two_job_limit_has_no_queue_and_oldest_unstarted_flights_get_next_slots(self):
        tracked = batch_fixtures.flights(4, self.now)
        self.configure(tracked)
        await self.own(flights=tracked)
        release, two_started = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            if self.schedule.await_count == 2:
                two_started.set()
            await release.wait()
            return {'std': self.now-100}
        self.schedule.side_effect = blocked
        await self.tick(0)
        await asyncio.wait_for(two_started.wait(), 1)
        self.assertEqual(len(self.provider.schedules.tasks), 2)
        self.assertEqual(len(self.provider.schedules.started), 2)
        await self.tick(15)
        self.assertEqual(len(self.provider.schedules.tasks), 2)
        self.assertEqual(self.schedule.await_count, 2)
        release.set()
        await self.finish_jobs()
        await self.tick(30)
        await self.finish_jobs()
        self.assertEqual(len(self.provider.schedules.started), 4)
        self.assertEqual(self.schedule.await_count, 4)
        self.assertLessEqual(len(self.provider.schedules.tasks), 2)

    async def test_regular_sixty_second_schedule_and_existing_aware_cache_intervals_remain(self):
        f = batch_fixtures.flights(1, self.now)[0]
        self.configure([f])
        self.schedule.return_value = {'std': self.now-100}
        await self.own(flights=[f])
        for offset in (0, 15, 30, 45, 60):
            await self.tick(offset)
            await self.finish_jobs()
        self.assertEqual(self.schedule.await_count, 2)
        self.assertEqual(self.aware.await_count, 1, 'Existing planned flights keep the30-minute Aware cache')

    async def test_provider_credential_rotation_separates_outer_inner_and_aware_caches(self):
        f = dict(batch_fixtures.flights(1, self.now)[0], _central=True)
        self.configure([f])
        self.provider.background_schedules = True
        async def stats(session, flight, credentials, cooldown, now):
            return {'std': self.now-100, 'gate': credentials['version'], 'gate_source': 'FlightStats'}
        self.schedule.side_effect = stats
        for offset, version in ((0, 'OLD'), (1, 'NEW')):
            self.provider.credentials = {'version': version}
            await self.provider.lookup(f, 'test-token', self.now+offset)
            await self.finish_jobs()
            self.assertEqual(self.provider.schedules.get(f, 'test-token', self.now+offset)['gate'], version)
        self.assertEqual(self.schedule.await_count, 2)
        self.assertEqual(self.aware.await_count, 2)
        # The ComatAlarm cache above FlightProvider must honor the same version.
        self.comat.shared_results.clear()
        self.provider.lookup = AsyncMock(side_effect=[{'_schedule': {'gate': 'OLD'}}, {'_schedule': {'gate': 'NEW'}}])
        self.provider.credentials = {'version': 'OLD'}
        await self.comat.shared_lookup(f, 'test-token', self.now, central=True)
        self.provider.credentials = {'version': 'NEW'}
        result = await self.comat.shared_lookup(f, 'test-token', self.now+1, central=True)
        self.assertEqual(result['_schedule']['gate'], 'NEW')
        self.assertEqual(self.provider.lookup.await_count, 2)

    async def test_noncentral_jobs_keep_ber_only_and_do_not_share_central_results(self):
        f = batch_fixtures.flights(1, self.now)[0]
        self.configure([f])
        self.provider.background_schedules = True
        self.schedule.return_value = {'gate': 'A21', 'gate_source': 'FlightStats'}
        await self.provider.lookup(f, 'test-token', self.now)
        await self.finish_jobs()
        self.schedule.assert_not_awaited(); self.aware.assert_not_awaited()
        self.assertNotIn('gate', self.provider.schedules.get(f, 'test-token', self.now))
        central = dict(f, _central=True)
        await self.provider.lookup(central, 'test-token', self.now+1)
        await self.finish_jobs()
        self.assertEqual(self.schedule.await_count, 1)
        self.assertEqual(self.aware.await_count, 1)
        self.assertEqual(self.provider.schedules.get(central, 'test-token', self.now+1)['gate'], 'A21')
        self.assertNotIn('gate', self.provider.schedules.get(f, 'test-token', self.now+1))

    async def test_schedule_only_departure_job_cannot_query_previous_arrival_or_fr24(self):
        f = dict(batch_fixtures.flights(1, self.now)[0], origin='BER', destination='FRA', _central=True)
        self.configure([f])
        self.provider.schedules.request(f, 'test-token', self.now)
        await self.finish_jobs()
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.schedule.await_count, 1)

    async def test_finished_private_job_cannot_reanimate_deleted_or_revoked_watches(self):
        for mutation in ('delete', 'approval', 'api_token', 'credential'):
            with self.subTest(mutation=mutation):
                self.allowed.add('reno')
                for table in ('comat_devices', 'comat_outbox', 'comat_app_history', 'comat_shared_removed'):
                    self.comat.db.execute('DELETE FROM '+table)
                self.comat.db.commit()
                self.comat.shared_results.clear()
                response = await self.register()
                self.headers = {'Authorization': 'Bearer '+(await response.json())['credential']}
                f = batch_fixtures.flights(1, self.now)[0]
                self.configure([f])
                release, entered = asyncio.Event(), asyncio.Event()
                async def blocked(*args):
                    entered.set()
                    await release.wait()
                    return {'gate': 'OLD', 'gate_source': 'FlightStats'}
                self.schedule.side_effect = blocked
                await self.own(flights=[f])
                await self.tick(0)
                await asyncio.wait_for(entered.wait(), 1)
                if mutation == 'delete':
                    self.comat.shared_monitoring.remove([dict(f, deleted_at=self.now+1)], self.now+1)
                elif mutation == 'approval':
                    self.allowed.remove('reno')
                elif mutation == 'credential':
                    self.comat.db.execute("UPDATE comat_devices SET credential='rotated',watch='[]' WHERE id=?", (self.id,))
                    self.comat.db.commit()
                else:
                    self.comat.db.execute("UPDATE comat_devices SET api_token='rotated-provider-token' WHERE id=?", (self.id,))
                    self.comat.db.commit()
                release.set()
                await self.finish_jobs()
                await self.tick(15)
                row = self.comat.db.execute('SELECT watch FROM comat_devices WHERE id=?', (self.id,)).fetchone()
                saved = json.loads(row['watch'])
                self.assertFalse(any(f.get('gate') == 'OLD' for f in saved))
                if mutation in ('delete', 'credential'):
                    self.assertEqual(saved, [])
                self.comat.sender.assert_not_awaited()
                await self.provider.schedules.close()

    async def test_slow_periodic_healthcheck_never_blocks_the_next_gate_pass(self):
        taxi = dict(batch_fixtures.flights(1, self.now)[0], ata=self.now-120)
        self.configure([taxi])
        self.alternative_after_first_tick()
        await self.own(flights=[taxi], stand_positions=self.positions())
        entered, release = asyncio.Event(), asyncio.Event()
        async def health(*args, **kwargs):
            entered.set()
            self.assertIs(kwargs.get('manual'), False)
            await release.wait()
            return {}
        self.comat.api_status.refresh = AsyncMock(side_effect=health)
        try:
            await asyncio.wait_for(self.tick(0), 1)
            await self.finish_arrivals()
            await asyncio.wait_for(entered.wait(), 1)
            fr24_count = len(self.http.calls)
            await asyncio.wait_for(self.tick(15), 1)
            await self.finish_arrivals()
            await asyncio.wait_for(self.tick(15), 1)
            self.assertEqual(self.comat.sender.await_count, 1)
            self.assertEqual(len(self.http.calls), fr24_count)
            self.assertFalse(release.is_set())
            self.assertEqual(self.comat.api_status.refresh.await_count, 1)
        finally:
            release.set()
            await self.comat.api_status.task


class ScheduleTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_arrival_provenance_survives_private_schedule_allowlist(self):
        now = 1_800_000_000
        evidence = dict(aibt=now-1, aibt_source='FlightAware', flightawareID='DLH194-real-leg',
                        icao24='3c6424', origin='FRA', destination='BER', registration='D-AIDA')
        provider = SimpleNamespace(credentials={}, lookup=AsyncMock(return_value=dict(evidence, stand='UNTRUSTED')))
        cache = ScheduleCache(provider)
        flight = dict(number='LH194', reference=now, origin='FRA', destination='BER')
        cache.request(flight, 'test', now)
        await asyncio.gather(*list(cache.tasks.values()))
        result = cache.get(flight, 'test', now)
        for key, value in evidence.items():
            self.assertEqual(result[key], value)
        self.assertNotIn('stand', result)
        await cache.close()

    async def test_close_cancels_running_work_and_consumes_task_failures(self):
        stopped, entered = asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        provider = SimpleNamespace(credentials={}, lookup=AsyncMock(side_effect=blocked))
        cache = ScheduleCache(provider)
        f = dict(number='LH194', reference=1_800_000_000, origin='FRA', destination='BER', _central=True)
        cache.request(f, 'test-token', 1_800_000_000)
        await entered.wait()
        await cache.close()
        self.assertTrue(stopped.is_set())
        self.assertEqual(cache.tasks, {})
        cache.request(f, 'test-token', 1_800_000_060)
        self.assertEqual(cache.tasks, {})
        failing = ScheduleCache(SimpleNamespace(credentials={}, lookup=AsyncMock(side_effect=RuntimeError('offline failure'))))
        failing.request(f, 'test-token', 1_800_000_000)
        await asyncio.gather(*list(failing.tasks.values()), return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(failing.tasks, {})
        self.assertEqual(failing.get(f, 'test-token', 1_800_000_001), {})
        await failing.close()


if __name__ == '__main__':
    unittest.main()
