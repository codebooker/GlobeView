import json
import unittest
from unittest.mock import patch

import proxy
import uzbek_enforcement


HEADER = '<tr>' + ''.join(f'<th>{value}</th>' for value in (
    'Viloyat', 'Tuman', 'Joylashgan joyi', 'Turi', 'Kenglik', 'Uzunlik')) + '</tr>'


def row(values):
    return '<tr>' + ''.join(f'<td><p>{value}</p></td>' for value in values) + '</tr>'


class UzbekEnforcementTests(unittest.TestCase):
    def test_decimal_commas_split_latitudes_and_duplicate_directions(self):
        first = ['Toshkent shahri', 'District', 'Intersection', 'Стационар камера', '41,31', '69,28']
        second = ['Toshkent shahri', 'District', 'Road', 'Стационар радар', '', '69.29']
        html = '<table>' + HEADER + row(first) + row(first) + row(second) + row(['41.32']) + '</table>'
        locations, rejected = uzbek_enforcement.parse_inventory(html)
        self.assertEqual(len(locations), 2)
        self.assertEqual(rejected, 0)
        self.assertEqual([(x['kind'], x['lat'], x['lon']) for x in locations],
                         [('camera', 41.31, 69.28), ('radar', 41.32, 69.29)])

    def test_missing_coordinates_unknown_devices_and_swapped_axes_are_omitted(self):
        rows = [
            ['Region', 'District', 'Good', 'Стационар камера', '41', '69'],
            ['Region', 'District', 'Unknown', 'CCTV', '41', '69'],
            ['Region', 'District', 'Missing', 'Стационар камера', '', '69'],
            ['Region', 'District', 'Swapped', 'Стационар камера', '69', '41'],
            ['Region', 'District', 'Infinite', 'Стационар камера', 'nan', '69'],
        ]
        locations, rejected = uzbek_enforcement.parse_inventory(
            '<table>' + HEADER + ''.join(row(values) for values in rows) + '</table>')
        self.assertEqual([x['address'] for x in locations], ['Good'])
        self.assertEqual(rejected, 4)

    def test_changed_columns_fail_instead_of_misplacing_devices(self):
        with self.assertRaises(ValueError):
            uzbek_enforcement.parse_inventory('<table><tr><th>Longitude</th></tr></table>')

    def test_stored_inventory_is_dated_and_viewport_filtered_without_video_or_alpr_claim(self):
        locations = uzbek_enforcement.enforcement_for_bbox((59.5, 42.4, 59.8, 42.6))
        self.assertTrue(locations)
        self.assertTrue(all(59.5 <= x['lon'] <= 59.8 and 42.4 <= x['lat'] <= 42.6
                            for x in locations))
        self.assertTrue(all(x['inventory_date'] == '2025-01-01' for x in locations))
        self.assertTrue(all('status unverified' in x['source'] for x in locations))
        self.assertTrue(all('url' not in x and 'tags' not in x for x in locations))
        self.assertEqual(uzbek_enforcement.enforcement_for_bbox((-81, 28, -80, 29)), [])

    def test_official_locations_survive_community_source_failure_and_respect_limit(self):
        with patch.object(proxy, 'cached_deflock_json', side_effect=OSError('unavailable')):
            content = json.loads(proxy.fetch_deflock_lpr_content((55.99, 37.18, 73.22, 45.60), limit=100))
        self.assertEqual(len(content['elements']), 100)
        self.assertGreater(content['total_elements'], 100)
        self.assertTrue(content['elements_limited'])
        self.assertTrue(all(x['id'].startswith('uz:iiv:') for x in content['elements']))
        self.assertTrue(content['sourceErrors'])

    def test_unrelated_view_does_not_load_uzbek_inventory(self):
        index = {'tile_size_degrees': 20, 'tile_url': 'https://example.test/{lat}/{lon}.json', 'regions': []}
        with patch.object(proxy, 'cached_deflock_json', return_value=index), \
                patch.object(proxy, 'uzbek_enforcement_for_bbox') as load:
            proxy.fetch_deflock_lpr_content((-81, 28, -80, 29))
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
