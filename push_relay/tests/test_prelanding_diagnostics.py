"""Operator-visible reasons for an absent warning, with commit and redaction gates."""
import json
import unittest

from push_relay.prelanding_diagnostics import receipt
from push_relay.tests.test_landing_alert import NOW, approaching, sample
from push_relay.tests import test_landing_pipeline as pipeline


class DiagnosticPolicyTests(unittest.TestCase):
    def test_current_alarm_and_calendar_reasons_are_explicit(self):
        for preferences, word in (({'alarmPrelanding': False}, 'deaktiviert'),
                                  ({'enabled': False}, 'deaktiviert'),
                                  ({'calendar_valid_until': NOW}, 'abgelaufen'),
                                  ({'active_windows': []}, 'Schichtfensters')):
            with self.subTest(preferences=preferences):
                previous, current = approaching()
                message = receipt(previous, current, preferences, NOW+60)
                self.assertIn(word, message)
                self.assertNotIn('Voralarm erkannt', message)

    def test_invalid_provider_time_cannot_break_diagnostics(self):
        flight = sample(eta=1e300)
        message = receipt({}, flight, {}, NOW)
        self.assertIn('außerhalb des Anflugzeitraums', message)
        self.assertLessEqual(len(message), 400)

    def test_untrusted_provider_text_is_never_echoed(self):
        flight = sample(source='Bearer private-provider-text')
        message = receipt({}, flight, {}, NOW)
        self.assertIn('keine bestätigte ADS-B-/MLAT', message)
        self.assertNotIn('private-provider-text', message)
        self.assertNotIn('Bearer', message)

    def test_terminal_reason_is_logged_once_even_outside_shift(self):
        previous = sample()
        receipt({}, previous, {'active_windows': []}, NOW)
        current = dict(previous, ata=NOW+1)
        message = receipt(previous, current, {'active_windows': []}, NOW+1)
        self.assertIn('Landung oder Abschluss bereits bestätigt', message)
        self.assertIn('zuvor: außerhalb', message)
        self.assertIsNone(receipt(current, dict(current), {'active_windows': []}, NOW+400))

    def test_live_identity_failure_has_its_own_reason(self):
        flight = sample()
        flight['_live'] = {}
        flight['_live_lookup_status'] = 'route_conflict'
        message = receipt({}, flight, {}, NOW)
        self.assertIn('widerspricht der überwachten Route', message)

    def test_normal_freshness_oscillation_cannot_flood_journal(self):
        previous, messages = {}, []
        for offset in range(0, 121, 15):
            observed = NOW+(offset//30)*30-40
            current = dict(previous, **sample(at=observed, eta=NOW+360))
            message = receipt(previous, current, {}, NOW+offset)
            if message:
                messages.append(message)
            previous = current
        self.assertLessEqual(len(messages), 3)

    def test_log_uses_actual_live_values_instead_of_schedule_eta(self):
        flight = sample()
        flight['_schedule'] = {'eta': NOW+9999}
        message = receipt({}, flight, {}, NOW)
        self.assertIn('Vorlauf 240 s', message)
        self.assertIn('Höhe über Flughafen 3000 ft', message)
        self.assertIn('Geschwindigkeit 170 kt', message)
        self.assertIn('Sinkrate -700 ft/min', message)
        self.assertIn('Kursabweichung', message)


class DiagnosticPipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.LandingPipelineTests.asyncSetUp
    asyncTearDown = pipeline.LandingPipelineTests.asyncTearDown
    register = pipeline.LandingPipelineTests.register
    watch = pipeline.LandingPipelineTests.watch
    prepare = pipeline.LandingPipelineTests.prepare
    live = pipeline.LandingPipelineTests.live

    def messages(self):
        return [r[0] for r in self.comat.db.execute(
            "SELECT message FROM comat_monitor_events WHERE kind='prelanding' ORDER BY id")]

    async def test_missing_eta_is_visible_throttled_and_explains_arrival(self):
        await self.prepare()
        live = self.live()
        live.pop('eta')
        self.comat.provider.lookup.return_value = {'_live': live}
        await self.comat.tick(self.now)
        self.assertIn('keine verwendbare ETA', self.messages()[-1])
        for offset in (15, 30):
            await self.comat.tick(self.now+offset)
        self.assertEqual(len(self.messages()), 1)
        live = self.live(at=60)
        live.pop('eta')
        self.comat.provider.lookup.return_value = {'_live': live}
        await self.comat.tick(self.now+60)
        self.assertEqual(len(self.messages()), 2)
        self.comat.provider.lookup.return_value = {'ata': self.now+120}
        await self.comat.tick(self.now+120)
        self.assertIn('Landung oder Abschluss bereits bestätigt', self.messages()[-1])
        self.assertIn('zuvor: Live-Beobachtung enthält keine verwendbare ETA', self.messages()[-1])
        snapshot = self.comat.monitor.snapshot(self.now+120)
        self.assertTrue(any(e['kind'] == 'prelanding' and 'zuvor:' in e['message'] for e in snapshot['events']))
        self.assertEqual([c.args[2]['flag'] for c in self.comat.sender.call_args_list], ['alarmLanded'])

    async def test_failed_watch_commit_does_not_publish_a_diagnostic(self):
        await self.prepare()
        device = self.comat.db.execute('SELECT * FROM comat_devices WHERE id=?', (self.id,)).fetchone()

        async def changed(*args, **kwargs):
            with self.comat.db:
                self.comat.db.execute('UPDATE comat_devices SET watch=? WHERE id=?', ('[]', self.id))
            return {'_live': self.live()}

        self.comat.provider.lookup.side_effect = changed
        await self.comat.poll_device(device, self.now)
        self.assertEqual(self.messages(), [])
        self.comat.sender.assert_not_awaited()

    async def test_app_handoff_preserves_receipt_throttle(self):
        await self.prepare()
        self.comat.provider.lookup.return_value = {'_live': self.live()}
        await self.comat.tick(self.now)
        count = len(self.messages())
        response = await self.watch(server_polling=True, relay_alerts=True)
        self.assertEqual(response.status, 200)
        flight = json.loads(self.comat.db.execute('SELECT watch FROM comat_devices WHERE id=?', (self.id,)).fetchone()[0])[0]
        self.assertIn('_prelanding_report', flight)
        await self.comat.tick(self.now+15)
        self.assertEqual(len(self.messages()), count)


if __name__ == '__main__':
    unittest.main()
