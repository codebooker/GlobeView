import datetime as dt
import json
import unittest
from unittest import mock

import japan_jma_alerts as jma


UTC = dt.timezone.utc
STAMP = dt.datetime(2026, 10, 4, 6, tzinfo=UTC)
URL = 'https://www.data.jma.go.jp/developer/xml/data/20261004060000_0_VPWW53_390000.xml'


class JapanJmaAlertsTests(unittest.TestCase):
    def test_feed_uses_latest_office_bulletin_and_drops_old_one(self):
        feed = f'''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><updated>2026-10-03T20:00:00Z</updated><link href="{URL}"/></entry>
          <entry><updated>2026-10-04T06:00:00Z</updated><link href="{URL}"/></entry>
          <entry><updated>2026-09-30T06:00:00Z</updated><link href="{URL.replace('390000', '400000')}"/></entry>
          <entry><updated>2026-10-04T06:00:00Z</updated><link href="https://untrusted.example/VPWW53_100000.xml"/></entry>
        </feed>'''.encode()
        latest = jma._latest_bulletins(feed, STAMP)
        self.assertEqual(list(latest), ['390000'])
        self.assertEqual(latest['390000'][0], STAMP)

    def test_current_area_alert_uses_polygon_and_severity(self):
        geometry = {'type': 'Polygon', 'coordinates': [
            [[133, 33], [134, 33], [134, 34], [133, 34], [133, 33]]
        ]}
        bulletin = '''<Report xmlns="http://xml.kishou.go.jp/jmaxml1/">
          <Head xmlns="http://xml.kishou.go.jp/jmaxml1/informationBasis1/">
            <Headline><Information type="気象警報・注意報（一次細分区域等）">
              <Item><Kind><Name>大雨特別警報</Name></Kind><Kind><Name>波浪注意報</Name></Kind>
                <Areas><Area><Code>390010</Code></Area></Areas></Item>
              <Item><Kind><Name>解除</Name></Kind><Areas><Area><Code>390020</Code></Area></Areas></Item>
            </Information></Headline>
          </Head></Report>'''.encode()
        results = jma.parse_bulletin(bulletin, '390000', STAMP, URL,
                                     {'390010': ('Kochi Central', geometry),
                                      '390020': ('Kochi East', geometry)})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['severity'], 'Extreme')
        self.assertEqual(results[0]['id'], 'jp:jma:390000:390010')
        self.assertEqual(results[0]['geometry'], geometry)
        self.assertEqual(results[0]['locationKind'], 'polygon')
        self.assertEqual(results[0]['ends'], '2026-10-05T06:00:00Z')

    def test_duplicate_area_parts_are_combined(self):
        feature = lambda lon, code: {'properties': {'code': code, 'enName': 'Kochi Central'},
                               'geometry': {'type': 'Polygon', 'coordinates': [
                                   [[lon, 33], [lon + 0.1, 33], [lon + 0.1, 34], [lon, 33]]
                               ]}}
        source = {'features': [feature(133, f'{390010 + number:06d}') for number in range(100)]
                              + [feature(133.2, '390010')]}
        with mock.patch.object(jma, '_read', return_value=json.dumps(source).encode()), \
                mock.patch.dict(jma._AREA_CACHE, {'until': 0, 'areas': None}):
            areas = jma._areas()
        self.assertEqual(len(areas['390010'][1]['coordinates']), 2)


if __name__ == '__main__':
    unittest.main()
