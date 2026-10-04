import datetime as dt
import json
import unittest
from unittest import mock

import international_infrastructure as infrastructure
import iran_roadworks as works


NOW = dt.datetime(2026, 10, 4, 2, tzinfo=dt.timezone.utc)


def notice():
    return {'id': 174589, 'lat': '25.43260519', 'lon': '60.63190618', 'meta': {
        'title': 'كارگاه جاده ای كنارك - چابهار', 'province_fa': 'سيستان و بلوچستان',
        'start_date': '14050120', 'end_date': '14051201', 'start_time': '0500',
        'end_time': '2300', 'passing_situation_fa': 'امكان عبور',
        'operation_type_fa': 'نصب علائم وحفاظ', 'updated_at': '2026-10-04T01:50:04Z'}}


class IranRoadworksTests(unittest.TestCase):
    def test_current_official_notice_keeps_publisher_point_schedule_and_identity(self):
        self.assertEqual(works._jalali_date(NOW.astimezone(works._TEHRAN).date()), (1405, 7, 12))
        self.assertEqual(works._date('14031230'), (1403, 12, 30))
        with self.assertRaises(ValueError): works._date('14051230')
        row = notice()
        feature = works.parse_roadworks([row], NOW)[0]
        self.assertEqual(feature['geometry']['coordinates'], [60.63190618, 25.43260519])
        self.assertEqual(feature['properties']['key'], 'ir:141:roadworks:174589')
        self.assertEqual(feature['properties']['layer'], 'construction')
        self.assertIn('05:00–23:00 Iran time', feature['properties']['detail'])
        self.assertIn('Passage possible', feature['properties']['detail'])
        self.assertIn('Signs and barriers', feature['properties']['detail'])
        self.assertIn('1405/12/01 (Persian calendar)', feature['properties']['detail'])
        self.assertEqual(feature['properties']['source_url'], 'https://141.ir/')
        self.assertIs(infrastructure._FETCHERS['roads']['ir_141_roadworks'], works.roadworks)
        self.assertTrue(infrastructure._road_feature_in_bbox(feature, [60, 25, 61, 26]))
        self.assertFalse(infrastructure._road_feature_in_bbox(feature, [51, 35, 52, 36]))

    def test_expired_future_stale_and_unlocated_notices_do_not_render(self):
        for field, value in [('start_date', '14050713'), ('end_date', '14050711'),
                             ('end_date', '14051301'), ('updated_at', '2026-10-03T23:00:00Z'),
                             ('updated_at', '2026-10-04T02:10:00Z'),
                             ('passing_situation_fa', 'unknown'), ('title', '<script>'),
                             ('start_time', '2500')]:
            row = notice(); row['meta'][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(works.parse_roadworks([row], NOW), [])
        for field, value in [('lon', '0'), ('lat', 'nan'), ('lat', True), ('id', True)]:
            row = notice(); row[field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(works.parse_roadworks([row], NOW), [])
        with self.assertRaises(ValueError):
            works.parse_roadworks([notice()], NOW.replace(tzinfo=None))

    def test_duplicate_conflicts_and_shared_cache(self):
        self.assertEqual(len(works.parse_roadworks([notice(), notice()], NOW)), 1)
        conflict = notice(); conflict['lon'] = '61.5'
        self.assertEqual(works.parse_roadworks([notice(), conflict], NOW), [])
        with mock.patch.dict(works._CACHE, {'until': 0, 'rows': []}, clear=True), \
                mock.patch.object(works, '_read', return_value=[notice()]) as read:
            self.assertEqual(len(works.roadworks(NOW)), 1)
            self.assertEqual(len(works.roadworks(NOW)), 1)
            self.assertEqual(works.roadworks(NOW + dt.timedelta(hours=3)), [])
            read.assert_called_once()

    def test_anonymous_bounded_public_request_drops_extra_fields(self):
        row = notice(); row['account'] = 'discard'; row['meta']['private'] = 'discard'
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200; response.geturl.return_value = works.API_URL
        response.read.return_value = json.dumps({'data': [row], 'error_code': 0}).encode()
        opener = mock.MagicMock(); opener.open.return_value = response
        with mock.patch.object(works.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(works._read(), [notice()])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, works.API_URL)
            self.assertIn(b'zoom=16', request.data)
            self.assertNotIn('Authorization', request.headers)
            response.read.assert_called_with(1_000_001)
            for payload in [{'data': [], 'error_code': True}, {'data': {}, 'error_code': 0}]:
                response.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(ValueError): works._read()


if __name__ == '__main__':
    unittest.main()
