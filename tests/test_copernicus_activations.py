import datetime as dt
import unittest
from unittest import mock

import international_emergency as feeds


class CopernicusActivationTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 4, 15, tzinfo=dt.timezone.utc)

    def row(self, **changes):
        # Public EMSR933 listing sampled on 4 October 2026; no incident prose retained.
        return {
            'code': 'EMSR933', 'name': 'Flood in Crete, Greece',
            'centroid': 'POINT (24.978601792058495 35.22506250241993)',
            'activationTime': '2026-10-04T11:02:00',
            'lastUpdate': '2026-10-04T14:21:41.576139',
            'drmPhase': 'response', 'closed': False,
            'n_aois': 8, 'n_products': 0,
            'category': {'slug': 'flood', 'name': 'Flood'}, **changes,
        }

    def parse(self, rows):
        return feeds.parse_copernicus_activations(
            {'count': len(rows), 'next': None, 'results': rows}, self.now)

    def test_live_listing_uses_published_centroid_and_activation_credit(self):
        item, = self.parse([self.row()])
        self.assertAlmostEqual(item['lon'], 24.978601792058495)
        self.assertAlmostEqual(item['lat'], 35.22506250241993)
        self.assertEqual(item['id'], 'copernicus:EMSR933')
        self.assertEqual(item['sourceUrl'], 'https://mapping.emergency.copernicus.eu/activations/EMSR933')
        self.assertEqual(item['observed'], '2026-10-04T14:21:41.576139Z')
        self.assertIn('© 2026 European Union', item['source'])
        self.assertIn('0 products', item['detail'])
        self.assertIn('not a dispatch location', item['detail'])

    def test_closed_recovery_preparedness_and_sensitive_records_are_omitted(self):
        for changes in ({'closed': True}, {'closed': 'false'}, {'closed': None},
                        {'drmPhase': 'recovery'}, {'drmPhase': 'preparedness'},
                        {'sensitive': True}, {'sensitive': 'false'}, {'sensitive': None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.parse([self.row(**changes)]), [])

    def test_stale_future_missing_and_inconsistent_dates_are_omitted(self):
        for changes in (
            {'lastUpdate': '2026-09-26T12:00:00'},
            {'activationTime': '2026-09-01T12:00:00'},
            {'lastUpdate': '2026-10-04T16:00:00'},
            {'activationTime': '2026-10-04T16:00:00'},
            {'lastUpdate': None}, {'activationTime': 'bad date'},
            {'lastUpdate': '2026-10-04T10:00:00'},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.parse([self.row(**changes)]), [])

    def test_bad_coordinates_and_nonpoint_geometry_are_omitted(self):
        for point in ('POINT (200 35)', 'POINT (24 95)', 'POINT (NaN 35)',
                      'POINT (1e999 35)', 'POINT (35)', 'POINT (24 35 5)',
                      'POLYGON ((24 35, 25 36, 24 35))', None):
            with self.subTest(point=point):
                self.assertEqual(self.parse([self.row(centroid=point)]), [])
        item, = self.parse([self.row(centroid='POINT (2.49786e1 +35.225)')])
        self.assertAlmostEqual(item['lon'], 24.9786)

    def test_duplicate_activation_conflicts_do_not_pick_an_arbitrary_location(self):
        self.assertEqual(len(self.parse([self.row(), self.row()])), 1)
        self.assertEqual(self.parse([self.row(), self.row(centroid='POINT (25 35)')]), [])

    def test_incomplete_or_unbounded_catalog_raises_instead_of_silent_truncation(self):
        for payload in ({}, {'count': 1, 'results': []},
                        {'count': 1, 'next': 'http://example.com/page', 'results': [self.row()]},
                        {'count': True, 'results': [self.row()]},
                        {'count': 101, 'next': None, 'results': [self.row()] * 101}):
            with self.subTest(payload=str(payload)[:100]):
                with self.assertRaises(ValueError):
                    feeds.parse_copernicus_activations(payload, self.now)

    def test_untrusted_ids_counts_and_text_do_not_become_links_or_markup(self):
        for changes in ({'code': '../private'}, {'code': 'EMSR933?token=bad'},
                        {'n_aois': True}, {'n_products': -1}, {'n_products': '0'}, {'name': ''}):
            with self.subTest(changes=changes):
                self.assertEqual(self.parse([self.row(**changes)]), [])
        item, = self.parse([self.row(name='<script>bad</script>Flood',
                                    sourceUrl='https://evil.example/')])
        self.assertNotIn('<script>', item['title'])
        self.assertTrue(item['sourceUrl'].startswith(feeds.COPERNICUS_ACTIVATIONS_SOURCE))
        fire, = self.parse([self.row(category={'slug': 'wildfire'})])
        self.assertEqual(fire['category'], 'fire')

    def test_multiple_viewers_share_one_filtered_catalog_request(self):
        loader = feeds._LOADERS['copernicus_mapping']
        payload = {'count': 0, 'next': None, 'results': []}
        with mock.patch.dict(feeds._CACHE, {'until': 0, 'value': None, 'sources': {}, 'source_times': {}}, clear=True), \
                mock.patch.dict(feeds._LOADERS, {'copernicus_mapping': loader}, clear=True), \
                mock.patch.object(feeds, '_json', return_value=payload) as fetch:
            for _ in range(10):
                self.assertEqual(feeds.international_emergency_snapshot()['sourceCounts']['copernicus_mapping'], 0)
            fetch.assert_called_once_with(feeds.COPERNICUS_ACTIVATIONS_URL)
        self.assertIn('closed=false', feeds.COPERNICUS_ACTIVATIONS_URL)
        self.assertIn('drmPhase=response', feeds.COPERNICUS_ACTIVATIONS_URL)


if __name__ == '__main__':
    unittest.main()
