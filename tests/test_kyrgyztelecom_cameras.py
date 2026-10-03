import email.utils
import json
import unittest
from unittest import mock

import kyrgyztelecom_cameras as cameras


class KyrgyztelecomTests(unittest.TestCase):
    def setUp(self):
        self.now = 1791000000
        self.headers = {'last-modified': email.utils.formatdate(self.now, usegmt=True),
                        'content-type': 'video/mp2t', 'content-length': '188'}
        self.catalog = {'cameras': [
            {'name': name, 'stream': cameras.STREAM_BASE + camera_id + '.m3u8'}
            for camera_id, (name, _) in cameras.CAMERAS.items()
        ] + [{'name': 'Unlocated city', 'stream': cameras.STREAM_BASE + 'camera99.m3u8'}]}

    def playlist(self, camera_id):
        return ('#EXTM3U\n#EXTINF:4,\n' + camera_id + '-100.ts\n#EXTINF:4,\n' + camera_id + '-101.ts\n').encode()

    def read(self, url, limit, method='GET'):
        if url == cameras.CATALOG:
            return json.dumps(self.catalog).encode(), {}
        camera_id = url.rsplit('/', 1)[-1].split('.')[0].split('-')[0]
        if url.endswith('.m3u8'):
            return self.playlist(camera_id), self.headers
        return (b'' if method == 'HEAD' else b'\x47' + bytes(187)), self.headers

    def test_only_audited_views_are_mapped_at_a_disclosed_landmark_reference(self):
        with mock.patch.object(cameras.time, 'time', return_value=self.now), \
                mock.patch.object(cameras, '_read', side_effect=self.read) as read:
            feature = cameras.camera_features()[0]
        self.assertEqual(feature['geometry']['coordinates'], cameras.SQUARE_REFERENCE)
        props = feature['properties']
        self.assertEqual(len(props['camera_views']), 3)
        self.assertIn('approximate square reference', props['detail'])
        self.assertEqual(props['location_source_url'], 'https://www.openstreetmap.org/way/181568920')
        self.assertEqual(props['source_url'], cameras.SOURCE)
        self.assertEqual(props['valid_until'], self.now + 600)
        self.assertFalse(any('camera99' in call.args[0] for call in read.call_args_list))

    def test_changed_public_name_and_dead_primary_view_are_not_advertised(self):
        self.catalog['cameras'][2]['name'] = 'Moved camera'
        def read(url, limit, method='GET'):
            if 'camera25-' in url:
                raise FileNotFoundError('offline')
            return self.read(url, limit, method)
        with mock.patch.object(cameras.time, 'time', return_value=self.now), \
                mock.patch.object(cameras, '_read', side_effect=read):
            feature = cameras.camera_features()[0]
        self.assertEqual(len(feature['properties']['camera_views']), 1)
        self.assertTrue(feature['properties']['video_url'].endswith('camera27.m3u8'))

    def test_stale_or_ended_playlists_and_foreign_assets_are_rejected(self):
        bad = [b'not a playlist', self.playlist('camera25') + b'#EXT-X-ENDLIST\n',
               self.playlist('camera25') + b'https://private.example/secret.ts\n',
               self.playlist('camera25') + b'../camera27-1.ts\n',
               self.playlist('camera27'), self.playlist('camera25') + b'#EXT-X-KEY:URI="key"\n']
        with mock.patch.object(cameras.time, 'time', return_value=self.now):
            for body in bad:
                with self.subTest(body=body), mock.patch.object(cameras, '_read', return_value=(body, self.headers)), \
                        self.assertRaises((ValueError, FileNotFoundError)):
                    cameras._playlist('camera25')
            stale = dict(self.headers, **{'last-modified': email.utils.formatdate(self.now - 181, usegmt=True)})
            with mock.patch.object(cameras, '_read', return_value=(self.playlist('camera25'), stale)), \
                    self.assertRaises(FileNotFoundError):
                cameras._playlist('camera25')
            with mock.patch.object(cameras, '_read') as read, self.assertRaises(ValueError):
                cameras._playlist('../private')
            read.assert_not_called()

    def test_segment_preview_requires_complete_fresh_bounded_transport_stream(self):
        with mock.patch.object(cameras.time, 'time', return_value=self.now), \
                mock.patch.object(cameras, '_read', side_effect=self.read):
            self.assertEqual(cameras.camera_segment('camera25'), b'\x47' + bytes(187))
        for headers, body in [(dict(self.headers, **{'content-length': '99999999'}), b'\x47'),
                              (dict(self.headers, **{'content-type': 'text/html'}), b'\x47' + bytes(187)),
                              (self.headers, b'\x47'), (self.headers, bytes(188)),
                              ({}, b'\x47' + bytes(187))]:
            with self.subTest(headers=headers, size=len(body)), \
                    mock.patch.object(cameras, '_playlist', return_value=('', cameras.STREAM_BASE + 'camera25-1.ts', self.now)), \
                    mock.patch.object(cameras, '_read', return_value=(body, headers)), \
                    mock.patch.object(cameras.time, 'time', return_value=self.now), \
                    self.assertRaises((ValueError, FileNotFoundError)):
                cameras.camera_segment('camera25')

    def test_response_limits_and_redirects_are_enforced(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status, response.url = 200, cameras.CATALOG
        response.headers = {}
        response.read.return_value = b'12345'
        with mock.patch.object(cameras._OPENER, 'open', return_value=response), self.assertRaises(ValueError):
            cameras._read(cameras.CATALOG, 4)
        response.url = 'https://private.example/'
        response.read.reset_mock()
        with mock.patch.object(cameras._OPENER, 'open', return_value=response), self.assertRaises(ValueError):
            cameras._read(cameras.CATALOG, 4)
        response.read.assert_not_called()
        self.assertIsNone(cameras._NoRedirect().redirect_request(None, None, 302, '', {}, response.url))


if __name__ == '__main__':
    unittest.main()
