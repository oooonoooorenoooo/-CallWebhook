"""Exercise the manual operator path without real FR24 traffic or credentials."""
import asyncio
import hashlib
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientConnectionError

from push_relay.comatalarm import FlightProvider
from push_relay.fr24_diagnostics import ENDPOINT, MAX_BODY, REDACTED
from push_relay.tests import test_comatalarm as base


class HistoricEventsDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = base.ComatAlarmTests.asyncTearDown
    register = base.ComatAlarmTests.register
    watch = base.ComatAlarmTests.watch
    add_peer = base.ComatAlarmTests.add_peer

    async def asyncSetUp(self):
        await base.ComatAlarmTests.asyncSetUp(self)
        self.comat.provider = FlightProvider()
        await self.watch(server_polling=True)
        self.diagnostic = self.comat.monitor.operator.fr24_diagnostics
        self.calls = []
        self.raw = json.dumps({'data': [{'fr24_id': '42a83e5a', 'events': [{
            'type': 'gate_arrival', 'timestamp': '2023-01-27T06:16:18Z',
            'details': {'gate_ident': None, 'gate_lat': 55.61253, 'gate_lon': 12.63116}}]}]}).encode()
        self.status = 200
        self.response_headers = {'Content-Type': 'application/json'}
        self.failure = None
        self.started = None
        self.release = None
        outer = self

        class Response:
            @property
            def status(self): return outer.status
            @property
            def headers(self): return outer.response_headers
            @property
            def content(self): return self
            async def __aenter__(self):
                if outer.failure:
                    raise outer.failure
                if outer.started:
                    outer.started.set()
                if outer.release:
                    await outer.release.wait()
                return self
            async def __aexit__(self, *args): pass
            async def iter_chunked(self, size):
                for offset in range(0, len(outer.raw), size):
                    yield outer.raw[offset:offset+size]

        class Session:
            def __init__(self, **kwargs):
                outer.session_options = kwargs
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                outer.calls.append((url, kwargs))
                return Response()

        self.http_patch = patch('push_relay.fr24_diagnostics.ClientSession', Session)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)

    async def command(self, identifier='42a83e5a', event_types='gate_arrival', headers=None, **extra):
        return await self.client.post('/comatalarm/v1/monitor',
            json={'action': 'fr24_historic_events', 'fr24_id': identifier, 'event_types': event_types, **extra},
            headers={'X-ComatAlarm-Setup-Key': 's'*64} if headers is None else headers)

    async def result(self, **kwargs):
        response = await self.command(**kwargs)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return await response.json()

    def database_state(self):
        return {table: [tuple(row) for row in self.comat.db.execute('SELECT * FROM '+table)]
                for table in ('comat_devices', 'comat_outbox', 'comat_app_history', 'comat_monitor_history', 'comat_delivered')}

    async def test_fixed_endpoint_saved_token_and_unfiltered_documentation_event(self):
        before = self.database_state()
        result = await self.result()
        self.assertTrue(result['ok'])
        diagnostic = result['diagnostic']
        self.assertEqual(diagnostic['response'], json.loads(self.raw))
        self.assertEqual(diagnostic['http_status'], 200)
        self.assertTrue(diagnostic['response_is_json'])
        self.assertIsNone(diagnostic['response_text'])
        self.assertFalse(diagnostic['redacted'])
        self.assertFalse(diagnostic['response_truncated'])
        self.assertEqual(len(self.calls), 1)
        url, options = self.calls[0]
        self.assertEqual(url, ENDPOINT)
        self.assertEqual(options['params'], {'flight_ids': '42a83e5a', 'event_types': 'gate_arrival'})
        self.assertEqual(options['headers']['Authorization'], 'Bearer private-api-token')
        self.assertEqual(options['headers']['Accept-Version'], 'v1')
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(self.session_options['timeout'].total, 10)
        self.assertNotIn('private-api-token', json.dumps(result))
        self.assertEqual(before, self.database_state())
        self.comat.sender.assert_not_awaited()

    async def test_all_events_and_opaque_identifier_are_forwarded_exactly(self):
        result = await self.result(identifier='_CaseSensitive_Leg-42', event_types='all')
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls[0][1]['params'], {'flight_ids': '_CaseSensitive_Leg-42', 'event_types': 'all'})

    async def test_empty_filter_is_explicit_all_not_omitted(self):
        await self.result(event_types='')
        self.assertEqual(self.calls[0][1]['params']['event_types'], 'all')

    async def test_operator_authorization_required_before_provider_request(self):
        for headers in ({}, self.headers, {'X-ComatAlarm-Setup-Key': 'wrong'}):
            response = await self.command(headers=headers)
            self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, [])

    async def test_invalid_identifiers_and_filters_do_not_send_requests(self):
        for identifier in ('', '../flight', 'id,other', 'id?x=1', ' x ', 'x\n', 'a'*65, 42, None, ['id']):
            self.assertEqual((await self.command(identifier=identifier)).status, 400)
        for event_types in ('takeoff', 'gate_arrival,all', None, ['all']):
            self.assertEqual((await self.command(event_types=event_types)).status, 400)
        self.assertEqual(self.calls, [])

    async def test_request_cannot_supply_token_or_override_upstream(self):
        result = await self.result(fr24_token='attacker-token', url='http://localhost/', params={'flight_ids': 'other'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls[0][0], ENDPOINT)
        self.assertEqual(self.calls[0][1]['headers']['Authorization'], 'Bearer private-api-token')

    async def test_expired_registration_and_absent_token_do_not_query(self):
        self.comat.db.execute('UPDATE comat_devices SET expires=?', (self.now-1,))
        self.comat.db.commit()
        result = await self.result()
        self.assertEqual(result['diagnostic']['error'], 'missing_token')
        self.assertIsNone(result['diagnostic']['http_status'])
        self.assertEqual(self.calls, [])

    async def test_saved_empty_token_does_not_query(self):
        self.comat.db.execute("UPDATE comat_devices SET api_token=''")
        self.comat.db.commit()
        self.assertEqual((await self.result())['diagnostic']['error'], 'missing_token')
        self.assertEqual(self.calls, [])

    async def test_revoked_firebase_approval_does_not_query(self):
        self.comat.db.execute("UPDATE comat_devices SET firebase_uid='approved-user'")
        self.comat.db.commit()
        self.comat.stand_access = AsyncMock(return_value=None)
        result = await self.result()
        self.assertEqual(result['diagnostic']['error'], 'missing_token')
        self.comat.stand_access.assert_awaited_once_with('approved-user')
        self.assertEqual(self.calls, [])

    async def test_approved_stored_token_is_used_without_exporting_identity(self):
        self.comat.db.execute("UPDATE comat_devices SET firebase_uid='approved-user'")
        self.comat.db.commit()
        self.comat.stand_access = AsyncMock(return_value={'uid': 'approved-user'})
        result = await self.result()
        self.assertTrue(result['ok'])
        self.assertNotIn('approved-user', json.dumps(result))

    async def test_token_rebound_while_approval_is_pending_is_not_used(self):
        self.comat.db.execute("UPDATE comat_devices SET firebase_uid='approved-user'")
        self.comat.db.commit()
        async def access(uid):
            self.comat.db.execute("UPDATE comat_devices SET api_token='replacement-token'")
            self.comat.db.commit()
            return {'uid': uid}
        self.comat.stand_access = access
        self.assertEqual((await self.result())['diagnostic']['error'], 'missing_token')
        self.assertEqual(self.calls, [])

    async def test_existing_shared_cooldown_is_not_bypassed(self):
        identity = hashlib.sha256(b'private-api-token').hexdigest()
        self.comat.provider.cooldown[identity] = time.time()+60
        result = await self.result()
        self.assertEqual(result['diagnostic']['error'], 'provider_cooldown')
        self.assertGreater(result['diagnostic']['retry_after_seconds'], 0)
        self.assertEqual(self.calls, [])

    async def test_shared_minute_budget_blocks_and_counts_manual_requests(self):
        identity = hashlib.sha256(b'private-api-token').hexdigest()
        self.comat.provider.requests[identity] = [time.time()-5]*8
        result = await self.result()
        self.assertEqual(result['diagnostic']['error'], 'minute_limit')
        self.assertEqual(self.calls, [])
        self.comat.provider.requests[identity] = [time.time()-5]*7
        self.assertTrue((await self.result())['ok'])
        self.assertEqual(len(self.comat.provider.requests[identity]), 8)

    async def test_repeated_click_is_not_another_paid_provider_request(self):
        self.assertTrue((await self.result())['ok'])
        result = await self.result()
        self.assertEqual(result['diagnostic']['error'], 'manual_limit')
        self.assertEqual(len(self.calls), 1)

    async def test_concurrent_click_has_one_inflight_request(self):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self.result())
        await self.started.wait()
        result = await self.result()
        self.assertEqual(result['diagnostic']['error'], 'request_busy')
        self.release.set()
        self.assertTrue((await first)['ok'])
        self.assertEqual(len(self.calls), 1)

    async def test_http_429_original_status_body_and_shared_retry_after(self):
        self.status = 429
        self.response_headers['Retry-After'] = '120'
        self.raw = json.dumps({'error': 'Limit exceeded', 'data': [], 'detail': 'contact support'}).encode()
        result = await self.result()
        diagnostic = result['diagnostic']
        self.assertFalse(result['ok'])
        self.assertEqual(diagnostic['http_status'], 429)
        self.assertEqual(diagnostic['error'], 'http_error')
        self.assertEqual(diagnostic['response'], json.loads(self.raw))
        self.assertGreaterEqual(diagnostic['retry_after_seconds'], 119)
        identity = hashlib.sha256(b'private-api-token').hexdigest()
        self.assertGreater(self.comat.provider.cooldown[identity], time.time()+118)
        self.assertEqual(len(self.calls), 1)

    async def test_http_403_original_status_and_safe_body_are_visible(self):
        self.status = 403
        self.raw = b'{"message":"Forbidden","details":{"reason":"subscription"}}'
        diagnostic = (await self.result())['diagnostic']
        self.assertEqual(diagnostic['http_status'], 403)
        self.assertEqual(diagnostic['response']['details']['reason'], 'subscription')

    async def test_non_json_html_is_text_without_executing_or_following_redirect(self):
        self.status = 302
        self.response_headers = {'Content-Type': 'text/html', 'Location': 'https://other.example/'}
        self.raw = b'<html><script>alert(1)</script>upstream redirect</html>'
        diagnostic = (await self.result())['diagnostic']
        self.assertEqual(diagnostic['http_status'], 302)
        self.assertEqual(diagnostic['response_text'], self.raw.decode())
        self.assertEqual(diagnostic['error'], 'http_error')
        self.assertIsNone(diagnostic['response'])
        self.assertFalse(self.calls[0][1]['allow_redirects'])

    async def test_invalid_json_with_http_200_is_reported_precisely(self):
        self.raw = b'not a JSON response'
        diagnostic = (await self.result())['diagnostic']
        self.assertEqual(diagnostic['http_status'], 200)
        self.assertEqual(diagnostic['error'], 'non_json_response')
        self.assertEqual(diagnostic['response_text'], self.raw.decode())
        self.assertFalse(diagnostic['response_is_json'])

    async def test_json_null_is_explicit_json_not_missing_body(self):
        self.raw = b'null'
        result = await self.result()
        self.assertTrue(result['ok'])
        diagnostic = result['diagnostic']
        self.assertTrue(diagnostic['response_is_json'])
        self.assertIsNone(diagnostic['response'])
        self.assertIsNone(diagnostic['response_text'])
        self.assertIsNone(diagnostic['error'])

    async def test_nested_json_credentials_and_echoed_bearer_are_redacted(self):
        self.raw = json.dumps({'data': [{'events': [], 'details': {'gate_ident': None, 'gate_lat': 55.61253}}],
            'FR24_API_TOKEN': 'different-secret', 'nested': [{'Authorization': 'Bearer a-new-secret'},
                {'message': 'echo private-api-token and Bearer third-secret'}, {'password': 'p4ss'}]}).encode()
        result = await self.result()
        encoded = json.dumps(result)
        for secret in ('private-api-token', 'different-secret', 'a-new-secret', 'third-secret', 'p4ss'):
            self.assertNotIn(secret, encoded)
        self.assertTrue(result['diagnostic']['redacted'])
        self.assertIsNone(result['diagnostic']['response']['data'][0]['details']['gate_ident'])

    async def test_non_json_echoes_are_redacted_without_exposing_secrets(self):
        self.status = 500
        self.raw = b'Bearer private-api-token; api_key="other-secret"; message: failure'
        result = await self.result()
        self.assertNotIn('private-api-token', json.dumps(result))
        self.assertNotIn('other-secret', json.dumps(result))
        self.assertTrue(result['diagnostic']['redacted'])

    async def test_timeout_and_connection_errors_do_not_leak_exception_or_retry(self):
        for failure, expected in ((asyncio.TimeoutError('private-api-token'), 'timeout'),
                                  (ClientConnectionError('Bearer private-api-token'), 'connection_error')):
            self.failure = failure
            self.diagnostic.next_request_at = 0
            result = await self.result()
            self.assertEqual(result['diagnostic']['error'], expected)
            self.assertNotIn('private-api-token', json.dumps(result))
            self.assertFalse(self.diagnostic.busy)
        self.assertEqual(len(self.calls), 2)

    async def test_response_limit_is_explicit_and_fits_proxy_even_after_escaping(self):
        self.raw = b'\x00'*(MAX_BODY+1)
        result = await self.result()
        diagnostic = result['diagnostic']
        self.assertTrue(diagnostic['response_truncated'])
        self.assertEqual(diagnostic['error'], 'response_too_large')
        self.assertEqual(len(diagnostic['response_text']), MAX_BODY)
        self.assertLess(len(json.dumps(result).encode()), 2_000_000)

    async def test_cutoff_does_not_reveal_partially_echoed_saved_token(self):
        fragment = 'private-api-to'
        self.raw = b'x'*(MAX_BODY-len(fragment)) + fragment.encode() + b'ken, rest of oversized response'
        diagnostic = (await self.result())['diagnostic']
        self.assertTrue(diagnostic['response_truncated'])
        self.assertTrue(diagnostic['redacted'])
        self.assertNotIn(fragment, diagnostic['response_text'])
        self.assertTrue(diagnostic['response_text'].endswith(REDACTED))

    async def test_redaction_expansion_stays_below_proxy_limit_for_short_saved_token(self):
        self.comat.db.execute("UPDATE comat_devices SET api_token='x'")
        self.comat.db.commit()
        for raw in (b'x'*MAX_BODY, json.dumps({'message': 'x'*(MAX_BODY-100)}).encode()):
            self.raw = raw
            self.diagnostic.next_request_at = 0
            result = await self.result()
            diagnostic = result['diagnostic']
            self.assertEqual(diagnostic['http_status'], 200)
            self.assertTrue(diagnostic['redacted'])
            self.assertTrue(diagnostic['response_truncated'])
            self.assertIsNone(diagnostic['response'])
            self.assertFalse(diagnostic['response_is_json'])
            self.assertLess(len(json.dumps(result).encode()), 2_000_000)
            self.assertNotIn('x', diagnostic['response_text'])

    async def test_monitor_get_and_snapshot_do_not_repeat_or_store_raw_diagnostic(self):
        await self.result()
        response = await self.client.get('/comatalarm/v1/monitor', headers={'X-ComatAlarm-Setup-Key': 's'*64})
        snapshot = await response.json()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('55.61253', json.dumps(snapshot))
        self.assertNotIn('private-api-token', json.dumps(snapshot))


if __name__ == '__main__':
    unittest.main()
