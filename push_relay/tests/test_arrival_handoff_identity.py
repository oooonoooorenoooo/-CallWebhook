"""Known aircraft identity survives authenticated handoff and shared recovery."""
import json
import unittest

from push_relay.arrival_sources import _airframe
from push_relay.tests import test_shared_monitoring as shared


class ArrivalHandoffIdentityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = shared.SharedMonitoringTests.asyncSetUp
    asyncTearDown = shared.SharedMonitoringTests.asyncTearDown
    register = shared.SharedMonitoringTests.register
    watch = shared.SharedMonitoringTests.watch
    add_peer = shared.SharedMonitoringTests.add_peer
    own = shared.SharedMonitoringTests.own
    colleague = shared.SharedMonitoringTests.colleague
    row = shared.SharedMonitoringTests.row
    watched = shared.SharedMonitoringTests.watched

    async def test_handoff_normalizes_identity_and_positions_can_use_address(self):
        row = dict(self.flight, icao24=' 3C6481 ', callsign=' dlh 6hf ')
        response = await self.own(flights=[row])
        saved = (await response.json())['flights'][0]
        self.assertEqual((saved['icao24'], saved['callsign']), ('3c6481', 'DLH6HF'))
        self.assertEqual(_airframe(saved), '3c6481')
        self.assertEqual(self.watched('reno')[0]['callsign'], 'DLH6HF')

    async def test_sparse_and_conflicting_phone_reposts_keep_existing_pi_identity(self):
        await self.own(flights=[dict(self.flight, icao24='3c6481', callsign='DLH6HF')])
        for metadata in ({}, dict(icao24=' ', callsign=' '), dict(icao24='3c6480', callsign='DLH7777')):
            with self.subTest(metadata=metadata):
                await self.own(flights=[dict(self.flight, **metadata)])
                saved = self.watched('reno')[0]
                self.assertEqual((saved['icao24'], saved['callsign']), ('3c6481', 'DLH6HF'))

    async def test_new_blank_identity_is_sparse_not_an_error_or_address(self):
        response = await self.own(flights=[dict(self.flight, icao24=' ', callsign=' ')])
        saved = (await response.json())['flights'][0]
        self.assertNotIn('icao24', saved)
        self.assertNotIn('callsign', saved)
        self.assertIsNone(_airframe(saved))

    async def test_invalid_metadata_is_rejected_before_saved_watch_changes(self):
        await self.own(flights=[dict(self.flight, icao24='3c6481', callsign='DLH6HF')])
        original = self.row('reno')['watch']
        for field, value in (('icao24', 123456), ('icao24', '3c648z'), ('icao24', '3c 6481'),
                             ('icao24', '3c6481\n'), ('callsign', ['DLH6HF']),
                             ('callsign', 'DLH/6HF'), ('callsign', 'DLH6HF\n'), ('callsign', 'DLHß'), ('callsign', 'A'*17)):
            with self.subTest(field=field, value=value):
                response = await self.watch(flights=[dict(self.flight, **{field: value})],
                    firebase_token='reno', server_polling=True, relay_alerts=True)
                self.assertEqual(response.status, 400)
                self.assertEqual(self.row('reno')['watch'], original)

    async def test_shared_peer_and_archived_evidence_preserve_aircraft_identity(self):
        await self.own(flights=[dict(self.flight, icao24='3c6481', callsign='DLH6HF')])
        await self.colleague(flights=[])
        await self.comat.tick(self.now+1)
        peer = self.watched('colleague')[0]
        self.assertEqual((peer['icao24'], peer['callsign']), ('3c6481', 'DLH6HF'))
        self.comat.history_sync.capture([dict(peer, ata=self.now-60, aibt=self.now-10,
                                              aibt_source='FlightStats')], self.now)
        saved = [json.loads(row['payload']) for row in self.comat.db.execute('SELECT payload FROM comat_app_history')]
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0]['icao24'], saved[0]['callsign']), ('3c6481', 'DLH6HF'))


if __name__ == '__main__':
    unittest.main()
