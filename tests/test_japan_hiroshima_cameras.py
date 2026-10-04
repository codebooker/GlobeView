import datetime as dt
import io
import unittest
from unittest import mock

from PIL import Image

import japan_hiroshima_cameras as cameras


class HiroshimaCameraTests(unittest.TestCase):
    def test_live_listing_joins_cc_by_locations_and_normalizes_ids(self):
        now = dt.datetime(2026, 10, 4, 5, 45, tzinfo=dt.timezone.utc)
        csv_body = ('観測地点ID,観測所名,設置場所,路線名,緯度,経度,路線番号,表示情報\n' +
                    ''.join(f'{ident},Station {ident},Hiroshima,Route {ident},34.5,132.5,R{ident},camera\n'
                            for ident in range(1, 101))).encode()
        listed = ''.join(
            f'<div class="content"><a href="camera_detail.php?id={ident:02d}">'
            f'<img src="snow_pic/{ident}.jpg?t=1"></a>'
            '<td class="time">2026/10/04 14:40:00</td>'
            for ident in range(1, 81))
        listed += ('<div class="content"><a href="camera_detail.php?id=81">'
                   '<img src="snow_pic/81.jpg?t=1"></a>'
                   '<td class="time">2026/10/03 14:40:00</td>')
        with mock.patch.object(cameras, '_read', side_effect=[csv_body, listed.encode()]), \
                mock.patch.object(cameras.dt, 'datetime', wraps=dt.datetime) as clock:
            clock.now.return_value = now
            features = cameras.camera_features()
        self.assertEqual(len(features), 80)
        self.assertEqual(features[0]['properties']['snapshot_url'], '/hiroshima-camera/1')
        self.assertEqual(features[0]['properties']['layer'], 'cameras')
        self.assertNotIn('81', cameras._LISTED)

    def test_snapshot_requires_current_listing_and_recent_valid_jpeg(self):
        image = io.BytesIO()
        Image.effect_noise((320, 180), 40).convert('RGB').save(image, 'JPEG')
        recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)

        class Response:
            url = f'{cameras.CAMERA_ORIGIN}/snow_pic/4.jpg'

            def __init__(self, modified):
                self.headers = mock.Mock()
                self.headers.get_content_type.return_value = 'image/jpeg'
                self.headers.get.return_value = modified

            def read(self, size):
                return image.getvalue()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        with mock.patch.dict(cameras._LISTED, {'4': dt.datetime.now().timestamp()}, clear=True), \
                mock.patch.object(cameras.urllib.request, 'urlopen',
                                  return_value=Response(recent.strftime('%a, %d %b %Y %H:%M:%S GMT'))):
            self.assertEqual(cameras.camera_snapshot('4'), (image.getvalue(), 'image/jpeg'))
        with mock.patch.dict(cameras._LISTED, {'4': dt.datetime.now().timestamp()}, clear=True), \
                mock.patch.object(cameras.urllib.request, 'urlopen',
                                  return_value=Response('Sat, 03 Oct 2026 05:00:00 GMT')):
            with self.assertRaisesRegex(FileNotFoundError, 'stale'):
                cameras.camera_snapshot('4')
        with self.assertRaises(ValueError):
            cameras.camera_snapshot('../4')


if __name__ == '__main__':
    unittest.main()
