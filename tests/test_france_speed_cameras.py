import json
import unittest
from unittest.mock import patch

import france_speed_cameras as radars
import proxy


class FranceSpeedCameraTests(unittest.TestCase):
    def test_official_csv_filters_bad_records_and_keeps_camera_types(self):
        rows = [
            'Numéro de radar;Type de radar;Date de mise en service;VMA ;Latitude; Longitude',
            'F1;ETF;31/10/2011 00:00;80;+45.361;+4.2526',
            'F1;ETF;31/10/2011 00:00;80;+45.361;+4.2526',
            'F2;ETFR;15/09/2005 17:40;NA;+16.25425;-61.56169',
            'F3;ETF;31/12/2026 00:00;90;+45.361;+4.2526',
            'F4;ETF;31/10/2011 00:00;80;+35;+4.2526',
            'F5;UNKNOWN;31/10/2011 00:00;80;+45.361;+4.2526',
        ]
        with patch.object(radars, '_catalog', return_value=radars.parse_radars(('\r\n'.join(rows)).encode('cp1252'))):
            mainland = radars.radars_for_bbox((4, 45, 5, 46))
            antilles = radars.radars_for_bbox((-62, 16, -61, 17))
        self.assertEqual([item['id'] for item in mainland], ['fr:interior:radar:F1'])
        self.assertEqual(mainland[0]['title'], 'Fixed speed camera')
        self.assertIn('80 km/h', mainland[0]['detail'])
        self.assertIn('plate reading unverified', mainland[0]['detail'])
        self.assertEqual([item['title'] for item in antilles], ['Red-light camera'])

    def test_current_official_file_includes_metropolitan_and_overseas_locations(self):
        catalog = radars._catalog()
        self.assertEqual(len(catalog), 3309)
        self.assertTrue(any(lat > 40 for _, _, lat, _, _, _ in catalog))
        self.assertTrue(any(lat < 20 for _, _, lat, _, _, _ in catalog))
        self.assertFalse(radars.region_visible((100, 0, 101, 1)))

    def test_overseas_inventory_survives_deflock_outage(self):
        with patch.object(proxy, 'cached_deflock_json', side_effect=OSError('index unavailable')):
            payload = json.loads(proxy.fetch_deflock_lpr_content((-62, 16, -61, 17)))
        ids = [row['id'] for row in payload['elements']]
        self.assertTrue(any(str(identifier).startswith('fr:interior:radar:') for identifier in ids))
        self.assertTrue(payload['sourceErrors'])


if __name__ == '__main__':
    unittest.main()
