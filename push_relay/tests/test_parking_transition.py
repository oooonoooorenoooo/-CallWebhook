"""A retained stand string must not substitute for the current local GPS match."""
import unittest

from push_relay.aibt_recovery import describe, position
from push_relay.comatalarm import match_stand, transitions


class ParkingCoordinateTransitionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.actual = self.now-120
        self.positions = [{'name': 'B06', 'latitude': 52.36507, 'longitude': 13.5053}]
        # Legacy handset handoffs could retain a stand while their coordinate
        # was absent from the Pi watch. This is deliberately synthetic data.
        self.old = dict(id='synthetic-stale-stand', number='LH9999', reference=self.now-3600,
                        origin='FRA', destination='BER', registration='D-TEST',
                        ata=self.now-300, stand='B13')

    def observed(self, latitude, longitude):
        return dict(latitude=latitude, longitude=longitude, timestamp=self.actual,
                    source='adsb_lol', on_ground=True, ground_speed=0)

    def test_new_valid_unmatched_coordinate_clears_legacy_stand_without_parking_push(self):
        for existing_aibt in (False, True):
            with self.subTest(existing_aibt=existing_aibt):
                old = dict(self.old)
                if existing_aibt:
                    old['aibt'] = self.actual
                updated = dict(old, aibt=self.actual, aibt_source='FlightStats',
                               aibt_position=self.observed(52.36, 13.49))
                self.assertIsNotNone(position(updated), 'The GPS observation itself is valid at AIBT')
                self.assertIsNone(match_stand(updated['aibt_position'], self.positions))
                self.assertEqual(updated['stand'], 'B13', 'Reproduce the retained pre-update field')
                updated.update(describe(updated, self.positions, self.now, ''))
                self.assertIsNone(updated.get('stand'), 'An unmatched current point must clear the old stand')
                self.assertEqual(updated['_parking_recovery']['state'], 'ambiguous')
                self.assertEqual(updated['aibt_position']['timestamp'], self.actual)
                self.assertFalse(any(event[1] == 'alarmParking' for event in transitions(old, updated)))

    def test_current_local_match_replaces_stale_stand_and_emits_only_one_complete_transition(self):
        old = dict(self.old, aibt=self.actual)
        updated = dict(old, aibt_position=self.observed(52.36507, 13.5053), aibt_source='FlightStats')
        updated.update(describe(updated, self.positions, self.now, ''))
        self.assertEqual(updated['stand'], 'B06')
        parking = [event for event in transitions(old, updated) if event[1] == 'alarmParking']
        self.assertEqual(len(parking), 1)
        self.assertIn('Stand B06', parking[0][3])
        self.assertNotIn('B13', parking[0][3])
        self.assertFalse(any(event[1] == 'alarmParking' for event in transitions(updated, dict(updated))))


if __name__ == '__main__':
    unittest.main()
