import datetime as dt
import json
import unittest
from unittest import mock

import iran_roads as roads
import international_infrastructure as infrastructure

NOW = dt.datetime(2026, 10, 3, 16, 52, tzinfo=dt.timezone.utc)


def sensor():
    return {'id': 3731369, 'lat': '35.7888890', 'lon': '51.0188890',
            'meta': {'axis_name_fa': 'آزادراه تهران - کرج (پل کلاک)', 'province_fa': 'البرز',
                     'avg_of_speed': '28', 'tarffic_status': 'سنگین',
                     'updated_at': '2026-10-03T16:50:17.000000Z'}}


class IranTrafficTests(unittest.TestCase):
    def test_located_reading_units_identity_and_expiry(self):
        f = roads.parse_sensors([sensor()], NOW)[0]
        self.assertEqual(f['geometry']['coordinates'], [51.018889, 35.788889])
        self.assertEqual(f['properties']['key'], 'ir:141:sensor:3731369')
        self.assertIn('28 km/h · Heavy traffic', f['properties']['detail'])
        self.assertEqual(f['properties']['valid_until'],
                         dt.datetime(2026, 10, 3, 17, 5, 17, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(f['properties']['source_url'], 'https://141.ir/')
        self.assertIs(infrastructure._FETCHERS['roads']['ir_141_traffic_sensors'], roads.traffic_sensors)
        self.assertTrue(infrastructure._road_feature_in_bbox(f, [51, 35, 52, 36]))
        self.assertFalse(infrastructure._road_feature_in_bbox(f, [44, 35, 45, 36]))

    def test_bad_stale_future_and_unlocated_readings_are_omitted(self):
        for field, value in [('updated_at', '2026-10-03T16:30:00Z'),
                             ('updated_at', '2026-10-03T17:00:00Z'),
                             ('updated_at', '2026-10-03T16:50:00'),
                             ('avg_of_speed', 'nan'), ('avg_of_speed', '-1'),
                             ('avg_of_speed', '300'), ('avg_of_speed', True),
                             ('axis_name_fa', '<script>'), ('tarffic_status', 'unsupported')]:
            row = sensor(); row['meta'][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(roads.parse_sensors([row], NOW), [])
        for field, value in [('lat', 'nan'), ('lon', '0'), ('lat', True), ('id', True)]:
            row = sensor(); row[field] = value
            self.assertEqual(roads.parse_sensors([row], NOW), [])
        with self.assertRaises(ValueError):
            roads.parse_sensors([sensor()], NOW.replace(tzinfo=None))

    def test_unknown_is_not_reported_as_zero_and_duplicate_conflicts_are_suppressed(self):
        row = sensor(); row['meta'].update(avg_of_speed='', tarffic_status='نامشخص')
        self.assertEqual(roads.parse_sensors([row], NOW), [])
        row['meta']['avg_of_speed'] = '0'
        self.assertIn('0 km/h', roads.parse_sensors([row], NOW)[0]['properties']['detail'])
        self.assertEqual(len(roads.parse_sensors([sensor(), sensor()], NOW)), 1)
        different = sensor(); different['lon'] = '51.5'
        self.assertEqual(roads.parse_sensors([sensor(), different], NOW), [])

    def test_shared_cache_rechecks_freshness_without_per_viewer_requests(self):
        with mock.patch.dict(roads._CACHE, {'until': 0, 'rows': []}, clear=True), \
                mock.patch.object(roads, '_read', return_value=[sensor()]) as read:
            self.assertEqual(len(roads.traffic_sensors(NOW)), 1)
            self.assertEqual(len(roads.traffic_sensors(NOW)), 1)
            self.assertEqual(roads.traffic_sensors(NOW + dt.timedelta(minutes=20)), [])
            read.assert_called_once()

    def test_read_bounds_body_errors_and_retains_only_public_fields(self):
        row = sensor(); row['account'] = 'discard'; row['meta']['vehicle'] = 'discard'
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200; response.geturl.return_value = roads.API_URL
        response.read.return_value = json.dumps({'data': [row], 'error_code': 0}).encode()
        opener = mock.MagicMock(); opener.open.return_value = response
        with mock.patch.object(roads.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(roads._read(), [sensor()])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, roads.API_URL)
            self.assertIn(b'zoom=16', request.data)
            self.assertNotIn('Authorization', request.headers)
            response.read.assert_called_with(2_000_001)
            for payload in [{'data': [], 'error_code': True}, {'data': {}, 'error_code': 0},
                            {'data': [], 'error_code': 1}]:
                response.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(ValueError): roads._read()
            response.read.return_value = b'x' * 2_000_001
            with self.assertRaises(ValueError): roads._read()


if __name__ == '__main__':
    unittest.main()
