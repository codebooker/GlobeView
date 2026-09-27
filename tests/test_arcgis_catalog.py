import unittest
from unittest.mock import patch

import arcgis_catalog


class ArcgisCatalogTests(unittest.TestCase):
    def test_bbox_and_layer_validation(self):
        for bbox in ('', 'nan,0,1,1', '1,0,-1,1', '-181,0,1,1', '-170,0,170,1'):
            with self.assertRaises(ValueError):
                arcgis_catalog.parse_bbox(bbox)
        with self.assertRaises(ValueError):
            arcgis_catalog.arcgis_viewport('https://example.com/', '0,0,1,1')

    @patch.object(arcgis_catalog, '_query')
    def test_dense_view_does_not_return_partial_geometry(self, query):
        query.return_value = {'count': 2001}
        result = arcgis_catalog.arcgis_viewport('active_faults', '0,0,1,1')
        self.assertTrue(result['too_many'])
        self.assertEqual(result['features'], [])
        self.assertEqual(query.call_count, 1)

    @patch.object(arcgis_catalog, '_query')
    def test_respects_smaller_provider_record_limit(self, query):
        query.return_value = {'count': 1001}
        result = arcgis_catalog.arcgis_viewport('power_plants', '0,0,1,1')
        self.assertTrue(result['too_many'])
        self.assertEqual(query.call_count, 1)

    @patch.object(arcgis_catalog, '_query')
    def test_small_view_returns_geometry(self, query):
        feature = {'type': 'Feature', 'properties': {}, 'geometry': {'type': 'Point', 'coordinates': [0.5, 0.5]}}
        query.side_effect = [{'count': 1}, {'type': 'FeatureCollection', 'features': [feature]}]
        result = arcgis_catalog.arcgis_viewport('power_plants', '0,0,1,1')
        self.assertEqual(result['features'], [feature])
        self.assertFalse(result['too_many'])
        self.assertEqual(query.call_args.args[1]['outFields'], arcgis_catalog.LAYERS['power_plants']['fields'])

    @patch.object(arcgis_catalog, '_query')
    def test_shipping_routes_are_fetched_as_one_global_snapshot(self, query):
        feature = {'type': 'Feature', 'properties': {'Type': 'Major'}, 'geometry': {'type': 'MultiLineString', 'coordinates': []}}
        query.side_effect = [{'count': 1}, {'type': 'FeatureCollection', 'features': [feature]}]
        result = arcgis_catalog.arcgis_viewport('shipping_routes', '')
        self.assertEqual(result['features'], [feature])
        self.assertNotIn('geometry', query.call_args_list[0].args[1])


if __name__ == '__main__':
    unittest.main()
