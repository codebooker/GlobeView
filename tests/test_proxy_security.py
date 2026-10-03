import email.message
import io
import urllib.error
import urllib.request
import urllib.parse
import unittest
from unittest import mock

import proxy


class ProxySecurityTests(unittest.TestCase):
    def test_cached_camera_clip_byte_ranges_cover_browser_requests(self):
        self.assertIsNone(proxy.parse_media_range(None, 100))
        for header, expected in [('bytes=0-31', (0, 31)), ('bytes=50-', (50, 99)),
                                 ('bytes=-20', (80, 99)), ('bytes=90-200', (90, 99)),
                                 ('bytes=-200', (0, 99))]:
            with self.subTest(header=header):
                self.assertEqual(proxy.parse_media_range(header, 100), expected)
        for header in ('bytes=100-', 'bytes=20-10', 'bytes=-0', 'bytes=-',
                       'bytes=0-20,30-40', 'bytes=' + '9' * 1000 + '-', 'invalid'):
            with self.subTest(header=header), self.assertRaises(ValueError):
                proxy.parse_media_range(header, 100)

    def test_kaztoll_clip_handler_shares_cache_and_serves_native_video_ranges(self):
        handler = object.__new__(proxy.Handler)
        handler.headers = {'Range': 'bytes=2-5'}
        handler._write_bytes = mock.Mock()
        handler.send_error = mock.Mock()
        with mock.patch.object(proxy.MEDIA_RESPONSE_CACHE, 'get_or_load',
                               return_value=(b'0123456789', 'video/mp4', 'HIT')) as cache:
            handler._handle_kaztoll_camera(urllib.parse.urlsplit('/kaztoll-camera/jjvezd'))
            args, kwargs = handler._write_bytes.call_args
            self.assertEqual(args, (206, b'2345', 'video/mp4'))
            self.assertEqual(kwargs['extra_headers']['Content-Range'], 'bytes 2-5/10')
            self.assertEqual(cache.call_args.kwargs['ttl'], 30)
            self.assertEqual(cache.call_args.kwargs['stale_ttl'], 0)
            handler.headers = {'Range': 'bytes=100-'}
            handler._handle_kaztoll_camera(urllib.parse.urlsplit('/kaztoll-camera/jjvezd'))
            self.assertEqual(handler._write_bytes.call_args.args[0], 416)
            self.assertEqual(handler._write_bytes.call_args.kwargs['extra_headers']['Content-Range'], 'bytes */10')
            cache.reset_mock()
            handler._handle_kaztoll_camera(urllib.parse.urlsplit('/kaztoll-camera/anything-else'))
            handler.send_error.assert_called_once_with(404, 'Unknown camera')
            cache.assert_not_called()
        self.assertEqual(proxy.rate_limit_bucket('/kaztoll-camera/jjvezd'),
                         proxy.rate_limit_bucket('/kaztoll-camera/ttvezd'))

    def test_european_road_camera_hosts_are_allowed_by_image_policy(self):
        image_policy = proxy.SECURITY_HEADERS['Content-Security-Policy'].split('img-src ', 1)[1].split(';', 1)[0]
        for host in ('weathercam.digitraffic.fi', 'etraffic.dgt.es', 'informo.madrid.es',
                     'www.cita.lu', 'kamera.atlas.vegvesen.no'):
            with self.subTest(host=host):
                self.assertIn('https://' + host, image_policy.split())

    def test_elcat_preview_handler_uses_shared_cache_and_a_fixed_camera_allowlist(self):
        handler = object.__new__(proxy.Handler)
        handler._write_bytes = mock.Mock()
        handler.send_error = mock.Mock()
        with mock.patch.object(proxy.MEDIA_RESPONSE_CACHE, 'get_or_load',
                               return_value=(b'jpeg', 'image/jpeg', 'HIT')) as cache:
            handler._handle_elcat_camera(urllib.parse.urlsplit('/elcat-camera/Kemin?v=123'))
            self.assertEqual(handler._write_bytes.call_args.args, (200, b'jpeg', 'image/jpeg'))
            self.assertEqual(cache.call_args.args[0], 'elcat-camera:v1:Kemin')
            self.assertEqual(cache.call_args.kwargs['ttl'], 60)
            self.assertEqual(cache.call_args.kwargs['stale_ttl'], 0)
            handler._handle_elcat_camera(urllib.parse.urlsplit('/elcat-camera/../private'))
            handler.send_error.assert_called_with(404, 'Unknown camera')
            self.assertEqual(cache.call_count, 1)
        self.assertEqual(proxy.rate_limit_bucket('/elcat-camera/Kemin'),
                         proxy.rate_limit_bucket('/elcat-camera/Balykchi'))
        policy = proxy.SECURITY_HEADERS['Content-Security-Policy']
        self.assertIn('https://webcam.elcat.kg', policy.split('connect-src ', 1)[1].split(';', 1)[0].split())
        self.assertNotIn('https://*.elcat.kg', policy)

    def test_stream_subpaths_share_a_rate_limit_bucket(self):
        self.assertEqual(
            proxy.rate_limit_bucket('/stream/camera-a/segment-1.ts'),
            proxy.rate_limit_bucket('/stream/camera-b/segment-2.ts'),
        )

    def test_stream_redirects_are_rejected(self):
        handler = proxy.NoStreamRedirectHandler()
        request = urllib.request.Request('https://video.example/playlist.m3u8')
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, 'Found', {}, 'http://127.0.0.1/')

    def test_oversized_stream_body_is_rejected_before_headers(self):
        handler = object.__new__(proxy.Handler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        headers = email.message.Message()
        headers['Content-Length'] = str(proxy.MAX_STREAM_MEDIA_BYTES + 1)
        response = mock.Mock(headers=headers)

        with self.assertRaises(ValueError):
            handler._write_streamed_upstream(response, 'video/mp2t')

        handler.send_response.assert_not_called()

    def test_stream_body_without_length_is_cut_off_at_limit(self):
        handler = object.__new__(proxy.Handler)
        handler.command = 'GET'
        handler.close_connection = False
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        sent_headers = {}
        handler.send_header = lambda key, value: sent_headers.setdefault(key, value)
        handler.end_headers = mock.Mock()
        headers = email.message.Message()
        response = mock.Mock(headers=headers)
        response.read.side_effect = [b'12345', b'']

        with mock.patch.object(proxy, 'MAX_STREAM_MEDIA_BYTES', 8):
            handler._write_streamed_upstream(response, 'video/mp2t', prefix=b'1234')

        self.assertEqual(handler.wfile.getvalue(), b'1234')
        self.assertTrue(handler.close_connection)
        self.assertEqual(sent_headers['Connection'], 'close')


if __name__ == '__main__':
    unittest.main()
