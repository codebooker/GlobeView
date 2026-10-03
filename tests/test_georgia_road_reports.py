import copy
import datetime as dt
import json
import unittest
from unittest import mock

import georgia_roads as roads
from scripts.update_georgia_road_report_locations import build_reference, NODE, WAY, RELATION

NOW = dt.datetime(2026, 10, 3, 18, tzinfo=roads._ZONE)
# Reduced public notice 5282. Georgian text is also returned in the CMS's en field.
TITLE = 'ფშაველი–აბანო–ომალოს გზის კმ39-კმ45 მონაკვეთზე ავტოტრანსპორტის მოძრაობის შეზღუდვა დაწესდა'
BODY = ('ინტენსიური თოვის და მოსალოდნელი ლიპყინულის წარმოქმნის გამო, '
        'ფშაველი–აბანო–ომალოს გზის კმ39-კმ45 მონაკვეთზე დასაშვებია მაღალი გამავლობის '
        'ავტოტრანსპორტის მოძრაობა მხოლოდ მოცურების საწინააღმდეგო ჯაჭვების გამოყენებით, '
        'ხოლო კმ12-კმ38 და კმ46-კმ72 მონაკვეთებზე დასაშვებია მხოლოდ მაღალი გამავლობის '
        'ავტოტრანსპორტის მოძრაობა. კმ1-კმ12 მონაკვეთზე ავტოტრანსპორტის მოძრაობა თავისუფალია.')


def notice(record_id=5282, published='2026-10-03 16:12:57', status='1', title=TITLE, body=BODY):
    return {'id': record_id, 'status': 1, 'deleted_at': None, 'restriction_status': status,
            'publish_date': published, 'translates': [{'language': 'en', 'title': title, 'content': body}]}


class GeorgiaRoadReportTests(unittest.TestCase):
    def tearDown(self):
        roads.report_catalog.cache_clear()

    def test_current_snow_report_preserves_chainages_and_approximate_point(self):
        feature = roads.parse_restrictions([notice()], NOW)[0]
        p = feature['properties']
        self.assertEqual(feature['geometry']['coordinates'], [45.5084719, 42.2780088])
        self.assertEqual(p['chains_km'], [39, 45])
        self.assertEqual(p['high_clearance_km'], [[12, 38], [46, 72]])
        self.assertEqual(p['unrestricted_km'], [1, 12])
        self.assertEqual(p['layer'], 'incidents')
        self.assertEqual(p['source_language'], 'ka')
        self.assertIn('translated from Georgian', p['source'])
        self.assertIn('Reported 03 Oct, 16:12 UTC+4', p['detail'])
        self.assertEqual(p['record_kind'], 'published_road_restriction')
        self.assertEqual(p['source_url'], 'https://georoad.gov.ge/en/restriction/5282')
        self.assertNotIn('road_segments', p)
        self.assertNotIn('restriction_active', p)
        self.assertEqual(p['valid_until'], NOW.timestamp() + 900)

    def test_newer_reopening_unsupported_change_and_conflict_remove_old_report(self):
        for newer in [notice(5283, '2026-10-03 17:30:00', '2', 'Pshaveli–Abano–Omalo: Traffic Restored'),
                      notice(5283, '2026-10-03 17:30:00', body='The traffic regime has changed.'),
                      notice(5283, '2026-10-03 17:30:00', status='4'),
                      notice(5283)]:
            self.assertFalse(roads.parse_restrictions([notice(), newer], NOW))
            self.assertFalse(roads.parse_restrictions([newer, notice()], NOW))
        self.assertEqual(len(roads.parse_restrictions([notice(), notice()], NOW)), 1)

    def test_future_and_old_reports_expire_without_inventing_restoration(self):
        published = dt.datetime(2026, 10, 3, 16, 12, 57, tzinfo=roads._ZONE)
        self.assertFalse(roads.parse_restrictions([notice()], published - dt.timedelta(seconds=1)))
        expiry = published + dt.timedelta(days=roads.REPORT_MAX_AGE_DAYS)
        p = roads.parse_restrictions([notice()], expiry - dt.timedelta(seconds=1))[0]['properties']
        self.assertEqual(p['valid_until'], expiry.timestamp())
        self.assertNotIn('ends_at', p)
        self.assertFalse(roads.parse_restrictions([notice()], expiry))
        with self.assertRaises(ValueError):
            roads.parse_restrictions([notice()], NOW.replace(tzinfo=None))

    def test_unsupported_conditions_and_other_roads_are_not_guessed(self):
        for body in [BODY.replace('ჯაჭვების გამოყენებით', 'სხვა პირობებით'),
                     BODY.replace('კმ39-კმ45', 'კმ39-კმ11'),
                     BODY.replace('კმ12-კმ38', 'კმ12-კმ44'),
                     BODY + ' მოძრაობა აკრძალულია.',
                     BODY.replace('კმ1-კმ12', 'კმ2-კმ150')]:
            with self.subTest(body=body):
                self.assertFalse(roads.parse_restrictions([notice(body=body)], NOW))
        self.assertFalse(roads.parse_restrictions([notice(title='Snow on a different road')], NOW))
        self.assertFalse(roads.parse_restrictions([dict(notice(), deleted_at='2026-10-03')], NOW))

    def test_named_pass_builder_checks_road_membership_and_original_coordinates(self):
        osm = {'osm3s': {'timestamp_osm_base': '2026-10-02T20:21:34Z'}, 'elements': [
            {'type': 'node', 'id': NODE, 'lon': 45.5084719, 'lat': 42.2780088,
             'tags': {'name:en': 'Abano Pass', 'mountain_pass': 'yes', 'natural': 'saddle'}},
            {'type': 'way', 'id': WAY, 'nodes': [1, NODE, 2],
             'tags': {'highway': 'tertiary', 'ref': 'შ 44'}},
            {'type': 'relation', 'id': RELATION, 'members': [{'type': 'way', 'ref': WAY}],
             'tags': {'route': 'road', 'name:en': 'Pshaveli-Abano-Omalo'}},
        ]}
        self.assertEqual(build_reference(osm)['coordinates'], [45.5084719, 42.2780088])
        for element, key, value in [(0, 'lon', 44), (0, 'lat', float('nan')),
                                    (1, 'nodes', [1, 2]), (2, 'members', [])]:
            invalid = copy.deepcopy(osm)
            invalid['elements'][element][key] = value
            with self.assertRaises(ValueError):
                build_reference(invalid)
        invalid = copy.deepcopy(osm)
        invalid['elements'][0]['tags']['name:en'] = 'Different pass'
        with self.assertRaises(ValueError):
            build_reference(invalid)
        with self.assertRaises(ValueError):
            build_reference(dict(osm, elements=osm['elements'] + [osm['elements'][0]]))

    def test_report_catalog_is_cached_and_rejects_moved_points(self):
        raw = roads.REPORT_LOCATION_PATH.read_text()
        with mock.patch.object(roads, 'REPORT_LOCATION_PATH') as path:
            path.read_text.return_value = raw
            roads.report_catalog()
            roads.report_catalog()
            path.read_text.assert_called_once()
        roads.report_catalog.cache_clear()
        data = json.loads(raw)
        data['locations'][0]['coordinates'] = [45, 42]
        with mock.patch.object(roads, 'REPORT_LOCATION_PATH') as path:
            path.read_text.return_value = json.dumps(data)
            with self.assertRaises(ValueError):
                roads.report_catalog()


if __name__ == '__main__':
    unittest.main()
