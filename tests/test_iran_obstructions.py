import datetime as dt
import json
import unittest
from unittest import mock

import international_infrastructure as infrastructure
import iran_obstructions as notices


NOW = dt.datetime(2026, 10, 4, 2, 20, tzinfo=dt.timezone.utc)


def closure(ref=37774):
    return {'id': ref, 'lat': '36.04028823', 'lon': '51.43138987', 'meta': {
        'title': 'انسداد در شمشك-دیزین', 'province_fa': 'البرز',
        'obstruction_reason_fa': 'نبود ایمنی كافی', 'direction_fa': 'هر دو مسیر',
        'start_date': '13980326', 'start_time': '1321',
        'updated_at': '2026-10-04T02:10:03Z'}}


class IranClosureTests(unittest.TestCase):
    def test_current_official_closure_with_old_start_date_and_duplicate_ids(self):
        features = notices.parse_obstructions([closure(445680), closure(37774)], NOW)
        self.assertEqual(len(features), 1)
        feature = features[0]
        self.assertEqual(feature['properties']['key'], 'ir:141:obstruction:37774')
        self.assertEqual(feature['properties']['layer'], 'incidents')
        self.assertEqual(feature['geometry']['coordinates'], [51.43138987, 36.04028823])
        self.assertIn('Reported closure · both directions', feature['properties']['detail'])
        self.assertIn('Insufficient road safety', feature['properties']['detail'])
        self.assertEqual(feature['properties']['source_url'], 'https://141.ir/')
        self.assertIs(infrastructure._FETCHERS['roads']['ir_141_obstructions'], notices.obstructions)
        self.assertTrue(infrastructure._road_feature_in_bbox(feature, [51, 36, 52, 37]))
        self.assertFalse(infrastructure._road_feature_in_bbox(feature, [44, 36, 45, 37]))

    def test_worksite_restriction_is_construction_without_claiming_full_closure(self):
        row = closure(451147)
        row['meta'].update(title='كارگاه جاده ای در لوشان - امام زاده هاشم',
                           obstruction_reason_fa='احداث و تعمیر روشنایی',
                           start_date='14050704', start_time='0001')
        feature = notices.parse_obstructions([row], NOW)[0]
        self.assertEqual(feature['properties']['layer'], 'construction')
        self.assertIn('Road restriction · both directions', feature['properties']['detail'])
        self.assertIn('Lighting work', feature['properties']['detail'])

    def test_stale_future_invalid_and_conflicting_notices_are_omitted(self):
        for field, value in [('start_date', '14050713'), ('start_date', '14051301'),
                             ('updated_at', '2026-10-03T23:00:00Z'),
                             ('updated_at', '2026-10-04T02:30:00Z'),
                             ('direction_fa', 'unknown'), ('title', '<script>'),
                             ('start_time', '2500')]:
            row = closure(); row['meta'][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(notices.parse_obstructions([row], NOW), [])
        for field, value in [('lon', '0'), ('lat', 'nan'), ('lat', True), ('id', True)]:
            row = closure(); row[field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(notices.parse_obstructions([row], NOW), [])
        conflict = closure(); conflict['lon'] = '51.5'
        self.assertEqual(notices.parse_obstructions([closure(), conflict], NOW), [])
        with self.assertRaises(ValueError):
            notices.parse_obstructions([closure()], NOW.replace(tzinfo=None))

    def test_public_request_is_bounded_and_cached_once(self):
        row = closure(); row['account'] = 'discard'; row['meta']['vehicle'] = 'discard'
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200; response.geturl.return_value = notices.API_URL
        response.read.return_value = json.dumps({'data': [row], 'error_code': 0}).encode()
        opener = mock.MagicMock(); opener.open.return_value = response
        with mock.patch.object(notices.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(notices._read(), [closure()])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, notices.API_URL)
            self.assertIn(b'zoom=16', request.data)
            self.assertNotIn('Authorization', request.headers)
            response.read.assert_called_with(1_000_001)
            for payload in [{'data': [], 'error_code': True}, {'data': {}, 'error_code': 0}]:
                response.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(ValueError): notices._read()
        with mock.patch.dict(notices._CACHE, {'until': 0, 'rows': []}, clear=True), \
                mock.patch.object(notices, '_read', return_value=[closure()]) as read:
            self.assertEqual(len(notices.obstructions(NOW)), 1)
            self.assertEqual(len(notices.obstructions(NOW)), 1)
            self.assertEqual(notices.obstructions(NOW + dt.timedelta(hours=3)), [])
            read.assert_called_once()


if __name__ == '__main__':
    unittest.main()
