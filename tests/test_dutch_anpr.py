import unittest

import dutch_anpr


class DutchAnprTests(unittest.TestCase):
    def test_latest_official_plan_from_search_results(self):
        search = '''<searchRetrieveResponse xmlns="http://docs.oasis-open.org/ns/search-ws/sruResponse"
          xmlns:d="http://purl.org/dc/terms/"><records><record><recordData>
          <d:title>Cameraplan ANPR Politie Q3-2026</d:title>
          <d:identifier>stcrt-2026-23725</d:identifier>
          </recordData></record></records></searchRetrieveResponse>'''
        self.assertEqual(dutch_anpr.latest_plan(search),
                         ('https://zoek.officielebekendmakingen.nl/stcrt-2026-23725.html', 'Q3 2026'))

    def test_plan_only_maps_geolocated_published_camera_rows(self):
        def row(name, lat, lon):
            return f'<tr><td><p>{name}</p></td><td><p>Amsterdam</p></td><td>Amsterdam</td>' \
                   f'<td>{lat}</td><td>{lon}</td><td>x</td><td>x</td><td></td><td></td><td>x</td></tr>'
        page = '<table class="zebra portrait"><tbody>' + ''.join([
            row('A10 West', '52.4', '4.8'), row('A10 West', '52.4', '4.8'),
            row('Outside country', '45', '4.8'), row('Invalid', 'none', '4.8')
        ]) + '</tbody></table>'
        source = 'https://zoek.officielebekendmakingen.nl/stcrt-2026-23725.html'
        items = dutch_anpr.parse_plan(page, source, 'Q3 2026', minimum_rows=1)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['lat'], items[0]['lon']), (52.4, 4.8))
        self.assertIn('status unverified', items[0]['detail'])
        self.assertEqual(dutch_anpr.plan_for_bbox(items, (4.7, 52.3, 4.9, 52.5)), items)
        self.assertEqual(dutch_anpr.plan_for_bbox(items, (5, 52, 6, 53)), [])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            dutch_anpr.parse_plan('<html>No camera table</html>', source, 'Q3 2026', minimum_rows=1)


if __name__ == '__main__':
    unittest.main()
