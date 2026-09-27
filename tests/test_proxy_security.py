import email.message
import io
import urllib.error
import urllib.request
import unittest
from unittest import mock

import proxy


class ProxySecurityTests(unittest.TestCase):
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
