import json
import unittest
from unittest.mock import patch

import proxy


class GlobalPlateReaderTests(unittest.TestCase):
    def test_region_selection_uses_viewport(self):
        index = {'tile_size_degrees': 20, 'tile_url': 'https://example.test/{lat}/{lon}.json',
                 'regions': ['40/-20', '40/0', '20/-100']}
        self.assertEqual([key for key, _ in proxy.deflock_tiles_for_bbox(index, (-0.3, 51.3, 0.2, 51.7))],
                         ['40/-20', '40/0'])

    def test_global_viewport_filters_and_deduplicates(self):
        index = {'tile_size_degrees': 20, 'tile_url': 'https://example.test/{lat}/{lon}.json',
                 'regions': ['40/-20', '40/0'], 'expiration_utc': 1790449000}
        tiles = {
            '40/-20': [{'id': 1, 'lat': 51.5, 'lon': -0.1, 'tags': {'operator': 'A'}},
                       {'id': 2, 'lat': 52.0, 'lon': -0.1}],
            '40/0': [{'id': 1, 'lat': 51.5, 'lon': -0.1},
                     {'id': 3, 'lat': 51.6, 'lon': 0.1}],
        }

        def cached(url, key, ttl=900):
            if 'index' in key:
                return index
            return tiles[key.split(':')[2]]

        with patch.object(proxy, 'cached_deflock_json', side_effect=cached):
            payload = json.loads(proxy.fetch_deflock_lpr_content((-0.3, 51.3, 0.2, 51.7)))
        self.assertEqual({item['id'] for item in payload['elements']}, {1, 3})
        self.assertEqual(payload['total_elements'], 2)
        self.assertTrue(payload['viewport_filtered'])


if __name__ == '__main__':
    unittest.main()
