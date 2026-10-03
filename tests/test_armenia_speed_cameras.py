import copy
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import armenia_enforcement as feed
import proxy
from scripts.update_armenia_speed_cameras import speed_camera_record


class ArmeniaSpeedCameraTests(unittest.TestCase):
    def tearDown(self):
        feed.speed_camera_catalog.cache_clear()

    def test_only_explicit_speed_cameras_with_unflagged_positions_are_mapped(self):
        node = {'id': 10, 'lat': 40.7, 'lon': 44.7,
                'tags': {'highway': 'speed_camera', 'maxspeed': '60', 'direction': '90;270'}}
        row, reason = speed_camera_record(node)
        self.assertIsNone(reason)
        self.assertEqual(row['osmNode'], 10)
        self.assertEqual(row['mappedLimitKmh'], 60)
        self.assertEqual(row['directions'], [90, 270])
        for tags in ({'highway': 'speed_camera', 'fixme': 'position'},
                     {'highway': 'speed_camera', 'name': 'fixme position'},
                     {'highway': 'speed_camera', 'disused': 'yes'},
                     {'man_made': 'surveillance'}, {'highway': 'speed_display'}):
            with self.subTest(tags=tags):
                self.assertIsNone(speed_camera_record({**node, 'tags': tags})[0])
        for position in ({'lat': float('nan')}, {'lon': 80}, {'id': True}):
            self.assertIsNone(speed_camera_record({**node, **position})[0])

    def test_units_and_unknown_directions_are_not_invented(self):
        node = {'id': 10, 'lat': 40.7, 'lon': 44.7,
                'tags': {'highway': 'speed_camera', 'maxspeed': '60 mph', 'direction': 'invalid'}}
        row, _ = speed_camera_record(node)
        self.assertIsNone(row['mappedLimitKmh'])
        self.assertEqual(row['directions'], [])
        node['tags']['direction'] = '360;0;270'
        self.assertEqual(speed_camera_record(node)[0]['directions'], [0, 270])

    def test_committed_catalog_has_source_nodes_without_public_video_or_plate_reader_claims(self):
        catalog = feed.speed_camera_catalog()
        self.assertEqual(len(catalog['locations']), 276)
        self.assertEqual(catalog['sourceNodes'], 281)
        self.assertEqual(len(catalog['omitted']), 5)
        excluded = {130041119, 597695089, 1884452947, 1972401132, 1972401134}
        self.assertFalse(excluded & {r['osmNode'] for r in catalog['locations']})
        visible = feed.speed_cameras_for_bbox(feed.ARMENIA_BOUNDS)
        self.assertEqual(len(visible), 276)
        self.assertFalse(feed.speed_cameras_for_bbox((-80, 35, -75, 40)))
        for row in visible:
            self.assertEqual(row['title'], 'Mapped speed camera')
            self.assertIn('status unverified', row['source'])
            self.assertIn('2026-10-02 snapshot', row['source'])
            self.assertEqual(row['source_url'], f"https://www.openstreetmap.org/node/{row['id']}")
            self.assertFalse({'image_url', 'video_url', 'stream_url'} & set(row))

    def test_catalog_rejects_duplicate_bad_position_and_wrong_source(self):
        original = json.loads(feed.SPEED_CATALOG_PATH.read_text())
        for kind in ('duplicate', 'nan', 'outside', 'bool', 'source', 'date', 'limit', 'direction'):
            with self.subTest(kind=kind):
                candidate = copy.deepcopy(original)
                if kind == 'duplicate':
                    candidate['locations'][1]['osmNode'] = candidate['locations'][0]['osmNode']
                elif kind == 'nan':
                    candidate['locations'][0]['lat'] = float('nan')
                elif kind == 'outside':
                    candidate['locations'][0]['lon'] = 80
                elif kind == 'bool':
                    candidate['locations'][0]['osmNode'] = True
                elif kind == 'source':
                    candidate['sourceUrl'] = 'https://example.test/'
                elif kind == 'date':
                    candidate['osmDataThrough'] = 'invalid'
                elif kind == 'limit':
                    candidate['locations'][0]['mappedLimitKmh'] = 999
                else:
                    candidate['locations'][0]['directions'] = [float('inf')]
                feed.speed_camera_catalog.cache_clear()
                path = MagicMock()
                path.open.return_value = io.StringIO(json.dumps(candidate))
                with patch.object(feed, 'SPEED_CATALOG_PATH', path), self.assertRaises(ValueError):
                    feed.speed_camera_catalog()

    def test_one_catalog_read_is_shared_across_viewports(self):
        feed.speed_camera_catalog.cache_clear()
        path = MagicMock()
        path.open.return_value = io.StringIO(feed.SPEED_CATALOG_PATH.read_text())
        with patch.object(feed, 'SPEED_CATALOG_PATH', path):
            feed.speed_cameras_for_bbox(feed.ARMENIA_BOUNDS)
            feed.speed_cameras_for_bbox((44.6, 40.7, 44.8, 40.8))
        path.open.assert_called_once()

    def test_nationwide_view_survives_deflock_failure_without_loading_yerevan_inventory(self):
        bbox = (44.9, 39.75, 45.0, 39.85)
        expected = feed.speed_cameras_for_bbox(bbox)
        self.assertTrue(expected)
        with patch.object(proxy, 'cached_deflock_json', side_effect=OSError('unavailable')), \
                patch.object(proxy, 'armenia_enforcement_for_bbox') as yerevan:
            result = json.loads(proxy.fetch_deflock_lpr_content(bbox))
        self.assertEqual({r['id'] for r in result['elements']}, {r['id'] for r in expected})
        self.assertIn('DeFlock:', result['sourceErrors'][0])
        yerevan.assert_not_called()

    def test_independent_inventory_failures_do_not_hide_the_other_source(self):
        bbox = (44.5, 40.17, 44.52, 40.19)
        for failed, working in [('armenia_enforcement_for_bbox', 'armenia_speed_cameras_for_bbox'),
                                ('armenia_speed_cameras_for_bbox', 'armenia_enforcement_for_bbox')]:
            with self.subTest(failed=failed), \
                    patch.object(proxy, 'cached_deflock_json', side_effect=OSError('unavailable')), \
                    patch.object(proxy, failed, side_effect=ValueError('bad catalog')), \
                    patch.object(proxy, working, return_value=[{'id': 'good', 'lat': 40.18, 'lon': 44.51}]):
                result = json.loads(proxy.fetch_deflock_lpr_content(bbox))
            self.assertEqual([r['id'] for r in result['elements']], ['good'])

    def test_duplicate_osm_node_is_shown_once_and_views_elsewhere_skip_catalog(self):
        bbox = (44.9, 39.75, 45.0, 39.85)
        node = feed.speed_cameras_for_bbox(bbox)[0]
        index = {'tile_size_degrees': 20, 'regions': [],
                 'tile_url': 'https://example.test/{lat}/{lon}.json'}
        with patch.object(proxy, 'cached_deflock_json', side_effect=[index, [node]]):
            result = json.loads(proxy.fetch_deflock_lpr_content(bbox))
        self.assertEqual(sum(r['id'] == node['id'] for r in result['elements']), 1)
        with patch.object(proxy, 'cached_deflock_json', return_value=index), \
                patch.object(proxy, 'armenia_speed_cameras_for_bbox') as read:
            proxy.fetch_deflock_lpr_content((-80, 35, -75, 40))
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
