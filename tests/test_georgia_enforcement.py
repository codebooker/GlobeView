import copy
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import georgia_enforcement as feed
import proxy
from scripts.update_georgia_enforcement import enforcement_record


class GeorgiaEnforcementTests(unittest.TestCase):
    def tearDown(self):
        feed.enforcement_catalog.cache_clear()

    def test_only_explicit_camera_capabilities_are_mapped(self):
        node = {'id': 1, 'lat': 42.62, 'lon': 44.60}
        for tags, expected in [
            ({'highway': 'speed_camera'}, ['speed_camera']),
            ({'highway': 'speed_camera', 'surveillance:type': 'ALPR;camera'},
             ['speed_camera', 'plate_reader']),
            ({'man_made': 'surveillance', 'surveillance:type': 'anpr'}, ['plate_reader']),
            ({'man_made': 'surveillance', 'surveillance:type': 'camera'}, None),
            ({'highway': 'speed_display'}, None),
            ({'surveillance:type': 'ALPR'}, None),
        ]:
            with self.subTest(tags=tags):
                row, _ = enforcement_record({**node, 'tags': tags})
                self.assertEqual(row['mappedKinds'] if row else None, expected)

    def test_flagged_disused_and_invalid_nodes_are_excluded(self):
        node = {'id': 1, 'lat': 42.62, 'lon': 44.60, 'tags': {'highway': 'speed_camera'}}
        for flag in [{'fixme': 'position'}, {'name': 'fixme location'}, {'removed': 'yes'},
                     {'abandoned': 'yes'}, {'disused:highway': 'speed_camera'}]:
            row, reason = enforcement_record({**node, 'tags': {**node['tags'], **flag}})
            self.assertIsNone(row)
            self.assertTrue(reason)
        for position in [{'lat': float('nan')}, {'lon': 47}, {'id': True}, {'lat': None}]:
            self.assertIsNone(enforcement_record({**node, **position})[0])

    def test_unknown_units_and_directions_are_not_invented(self):
        node = {'id': 1, 'lat': 42.62, 'lon': 44.60,
                'tags': {'highway': 'speed_camera', 'maxspeed': '60 mph', 'direction': '-20'}}
        row, _ = enforcement_record(node)
        self.assertIsNone(row['mappedLimitKmh'])
        self.assertFalse(row['directions'])
        node['tags'].update({'maxspeed': '60', 'direction': '360;0;270'})
        row, _ = enforcement_record(node)
        self.assertEqual(row['mappedLimitKmh'], 60)
        self.assertEqual(row['directions'], [0, 270])

    def test_catalog_retains_source_points_and_does_not_claim_live_video(self):
        catalog = feed.enforcement_catalog()
        raw = catalog['locations']
        self.assertEqual(len(raw), 259)
        self.assertEqual(catalog['sourceNodes'], 259)
        self.assertEqual(catalog['omitted'], [])
        self.assertEqual(catalog['osmDataThrough'], '2026-10-02T20:21:34Z')
        self.assertEqual({r['osmNode'] for r in raw if 'plate_reader' in r['mappedKinds']},
                         {1202894052, 6602323088})
        visible = feed.enforcement_for_bbox(feed.GEORGIA_BOUNDS)
        self.assertEqual(len(visible), 259)
        by_id = {r['osmNode']: r for r in raw}
        for row in visible:
            original = by_id[row['id']]
            self.assertEqual((row['lon'], row['lat']), (original['lon'], original['lat']))
            self.assertIn('status unverified', row['source'])
            self.assertEqual(row['source_url'], f"https://www.openstreetmap.org/node/{row['id']}")
            self.assertFalse({'video_url', 'image_url', 'stream_url'} & set(row))
        self.assertFalse(feed.enforcement_for_bbox((-80, 35, -75, 40)))

    def test_catalog_rejects_bad_provenance_capabilities_or_coordinates(self):
        original = copy.deepcopy(feed.enforcement_catalog())
        cases = []
        bad = copy.deepcopy(original); bad['sourceUrl'] = 'https://example.test/'; cases.append(bad)
        bad = copy.deepcopy(original); bad['boundaryInputSha256'] = 'bad'; cases.append(bad)
        bad = copy.deepcopy(original); bad['osmDataThrough'] = '2026-10-02'; cases.append(bad)
        bad = copy.deepcopy(original); bad['locations'].append(bad['locations'][0]); cases.append(bad)
        bad = copy.deepcopy(original); bad['locations'][0]['lat'] = 40; cases.append(bad)
        bad = copy.deepcopy(original); bad['locations'][0]['mappedKinds'] = ['live_camera']; cases.append(bad)
        bad = copy.deepcopy(original); bad['locations'][0]['directions'] = [360]; cases.append(bad)
        for candidate in cases:
            with self.subTest(candidate=candidate['locations'][0]['osmNode']):
                feed.enforcement_catalog.cache_clear()
                path = MagicMock()
                path.open.return_value = io.StringIO(json.dumps(candidate))
                with patch.object(feed, 'CATALOG_PATH', path), self.assertRaises(ValueError):
                    feed.enforcement_catalog()

    def test_catalog_is_shared_across_viewport_requests(self):
        catalog = feed.enforcement_catalog()
        path = MagicMock()
        path.open.return_value = io.StringIO(json.dumps(catalog))
        feed.enforcement_catalog.cache_clear()
        with patch.object(feed, 'CATALOG_PATH', path):
            feed.enforcement_for_bbox(feed.GEORGIA_BOUNDS)
            feed.enforcement_for_bbox((44.59, 42.61, 44.61, 42.63))
        path.open.assert_called_once()

    def test_georgia_inventory_survives_deflock_failure(self):
        bbox = (44.59, 42.61, 44.61, 42.63)
        expected = feed.enforcement_for_bbox(bbox)
        self.assertEqual(len(expected), 2)
        with patch.object(proxy, 'cached_deflock_json', side_effect=OSError('unavailable')):
            result = json.loads(proxy.fetch_deflock_lpr_content(bbox))
        self.assertEqual({r['id'] for r in result['elements']}, {r['id'] for r in expected})
        self.assertIn('DeFlock:', result['sourceErrors'][0])

    def test_duplicate_nodes_are_shown_once_and_other_countries_skip_georgia(self):
        bbox = (44.59, 42.61, 44.61, 42.63)
        node = feed.enforcement_for_bbox(bbox)[0]
        index = {'tile_size_degrees': 20, 'regions': [],
                 'tile_url': 'https://example.test/{lat}/{lon}.json'}
        with patch.object(proxy, 'cached_deflock_json', side_effect=[index, [node]]):
            result = json.loads(proxy.fetch_deflock_lpr_content(bbox))
        self.assertEqual(sum(r['id'] == node['id'] for r in result['elements']), 1)
        with patch.object(proxy, 'cached_deflock_json', return_value=index), \
                patch.object(proxy, 'georgia_enforcement_for_bbox') as read:
            proxy.fetch_deflock_lpr_content((-80, 35, -75, 40))
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
