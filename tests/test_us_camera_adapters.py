import unittest
from unittest import mock

import proxy


class USCameraAdapterTests(unittest.TestCase):
    def test_virginia_uses_current_camera_route(self):
        camera = {'type': 'Feature', 'properties': {
            'id': '3958', 'description': 'I-66 camera',
            'https_url': 'https://media-sfs7.vdotcameras.com/rtplive/camera/playlist.m3u8',
            'image_url': 'https://snapshot.vdotcameras.com/thumbs/camera.flv.png',
        }, 'geometry': {'type': 'Point', 'coordinates': [-77.3, 38.9]}}
        with mock.patch.dict(proxy.VDOT_TRAFFIC_CACHE, {}, clear=True), \
                mock.patch.object(proxy, 'fetch_json_url', return_value={'data': [camera]}) as fetch:
            items = proxy.fetch_vdot_dataset('Cameras')
        self.assertEqual(items, [camera])
        self.assertEqual(fetch.call_args.args[0],
                         'https://511.vdot.virginia.gov/services/511/map/array/cameras')

    def test_texas_camera_query_uses_current_maplarge_fields(self):
        row = {
            'id': 'camera-1', 'description': 'I-10 camera', 'name': 'TX_TEST_1',
            'active': 1, 'problemstream': 0, 'lastUpdated': 1790916123476,
            'httpsurl': 'https://s69.us-east-1.skyvdn.com/rtplive/TX_TEST_1/playlist.m3u8',
            'imageurl': '', 'prerollurl': '', 'XY': 'POINT (-97.7 30.3)',
        }
        with mock.patch.dict(proxy.DRIVETEXAS_CACHE, {}, clear=True), \
                mock.patch.object(proxy, 'drivetexas_query', return_value=[row]) as query:
            cameras = proxy.fetch_drivetexas_cameras()
        self.assertEqual(len(cameras), 1)
        self.assertTrue(cameras[0]['video_url'])
        fields = query.call_args.args[1]
        self.assertIn('id', fields)
        self.assertNotIn('guid', fields)
        self.assertNotIn('route', fields)
        self.assertNotIn('jurisdiction', fields)
        self.assertNotIn('direction', fields)

    def test_nebraska_camera_uses_public_image_directly(self):
        camera = {'id': '617', 'lat': 41.2, 'lon': -96.0, 'name': 'Fort Street',
                  'route': '', 'city': '', 'owner': '', 'updated_at': None,
                  'snapshot_url': 'https://dot511.nebraska.gov/images/camera.jpg'}
        with mock.patch.object(proxy, 'fetch_cars511_cameras', return_value=[camera]):
            item = proxy.cars511_layer_payload('NE', 'Cameras')['item2'][0]
            detail = proxy.cars511_camera_detail('NE', '617')
        self.assertEqual(item['expando']['snapshotUrl'], camera['snapshot_url'])
        self.assertEqual(detail['snapshot_url'], camera['snapshot_url'])

    def test_hawaii_camera_keeps_direct_image_fallback(self):
        camera = {'id': '123', 'device_id': 'TL-0095', 'name': 'Kam Hwy',
                  'lat': 21.4, 'lon': -157.9, 'last_update': None,
                  'snapshot_url': 'https://cctv.cdn.goakamai.org/SnapShot/320x240/TL-0095.jpg'}
        with mock.patch.object(proxy, 'goakamai_camera_records', return_value=[camera]):
            item = proxy.goakamai_layer_payload('Cameras')['item2'][0]
            detail = proxy.goakamai_camera_detail('123')
        self.assertEqual(item['expando']['snapshotFallbackUrl'], camera['snapshot_url'])
        self.assertEqual(detail['snapshot_fallback_url'], camera['snapshot_url'])

    def test_snapshot_rejects_html_challenge(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'<html>Access denied</html>'
        response.headers.get_content_type.return_value = 'image/jpeg'
        detail = {'upstream_snapshot_url': 'https://cctv.cdn.goakamai.org/camera.jpg'}
        handler = object.__new__(proxy.Handler)
        with mock.patch.object(proxy, 'goakamai_camera_detail', return_value=detail), \
                mock.patch.object(proxy.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(FileNotFoundError):
                handler._load_camera_snapshot('HI', '123')


if __name__ == '__main__':
    unittest.main()
