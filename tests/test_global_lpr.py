import json
import unittest
from unittest.mock import patch

import proxy
from france_toll_gantries import _catalog as french_gantry_catalog, gantries_for_bbox as french_gantries_for_bbox


class GlobalPlateReaderTests(unittest.TestCase):
    def test_nottingham_anpr_inventory_is_bounded_and_dated(self):
        def row(camera_id, kind=4, lon=-1.15, lat=52.95, location='Goldsmith Street'):
            return {'attributes': {'OBJECTID': camera_id, 'TYPE': kind, 'LONG': lon,
                                   'LAT': lat, 'LOCATION': location, 'CREATE_DAT': '20240528'}}
        catalog = {'features': [row(24), row(24), row(25, kind=2),
                                row(26, lon=-3), row(27, location=' '), row(28, lat=53.5)]}
        rows = proxy.nottingham_anpr_plate_readers(catalog, (-1.3, 52.8, -1.0, 53.1))
        self.assertEqual([item['id'] for item in rows],
                         ['uk:nottingham:anpr:24', 'uk:nottingham:anpr:27'])
        self.assertIn('Goldsmith Street', rows[0]['title'])
        self.assertIn('2024-05-28', rows[0]['detail'])
        self.assertIn('current operation unverified', rows[0]['detail'])

    def test_nottingham_anpr_survives_deflock_outage(self):
        catalog = {'features': [{'attributes': {'OBJECTID': 24, 'TYPE': 4,
                                               'LAT': 52.95, 'LONG': -1.15,
                                               'LOCATION': 'Goldsmith Street'}}]}
        def cached(url, key, ttl=900):
            if key == 'deflock-index:v1': raise OSError('index unavailable')
            if key == 'uk-nottingham-anpr:v1': return catalog
            raise AssertionError(key)
        with patch.object(proxy, 'cached_deflock_json', side_effect=cached):
            payload = json.loads(proxy.fetch_deflock_lpr_content((-1.2, 52.9, -1.1, 53.0)))
        self.assertEqual([item['id'] for item in payload['elements']],
                         ['uk:nottingham:anpr:24'])
        self.assertTrue(payload['sourceErrors'])

    def test_french_toll_gantries_are_bounded_published_locations(self):
        self.assertEqual(len(french_gantry_catalog()), 32)
        rows = french_gantries_for_bbox((2.9, 46.2, 4.0, 46.7))
        self.assertTrue(rows)
        self.assertTrue(all(row['id'].startswith('fr:freeflow:') for row in rows))
        self.assertTrue(all('October 2025' in row['detail'] for row in rows))
        self.assertEqual(french_gantries_for_bbox((-0.3, 51.3, 0.2, 51.7)), [])

    def test_french_gantries_survive_deflock_outage(self):
        with patch.object(proxy, 'cached_deflock_json', side_effect=OSError('index unavailable')):
            payload = json.loads(proxy.fetch_deflock_lpr_content((2.9, 46.2, 4.0, 46.7)))
        self.assertTrue(payload['elements'])
        self.assertTrue(all(row['id'].startswith('fr:freeflow:') for row in payload['elements']))
        self.assertTrue(payload['sourceErrors'])

    def test_region_selection_uses_viewport(self):
        index = {'tile_size_degrees': 20, 'tile_url': 'https://example.test/{lat}/{lon}.json',
                 'regions': ['40/-20', '40/0', '20/-100']}
        self.assertEqual([key for key, _ in proxy.deflock_tiles_for_bbox(index, (-0.3, 51.3, 0.2, 51.7))],
                         ['40/-20', '40/0'])

    def test_global_viewport_filters_and_deduplicates(self):
        index = {'tile_size_degrees': 20, 'tile_url': 'https://example.test/{lat}/{lon}.json',
                 'regions': ['40/-20', '40/0'], 'expiration_utc': 1790449000}
        tiles = {
            '40/-20': [{'id': 1, 'lat': 51.5, 'lon': -0.1, 'tags': {'operator': 'A'}},
                       {'id': 2, 'lat': 52.0, 'lon': -0.1}],
            '40/0': [{'id': 1, 'lat': 51.5, 'lon': -0.1},
                     {'id': 3, 'lat': 51.6, 'lon': 0.1}],
        }

        def cached(url, key, ttl=900):
            if 'index' in key:
                return index
            return tiles[key.split(':')[2]]

        with patch.object(proxy, 'cached_deflock_json', side_effect=cached):
            payload = json.loads(proxy.fetch_deflock_lpr_content((-0.3, 51.3, 0.2, 51.7)))
        self.assertEqual({item['id'] for item in payload['elements']}, {1, 3})
        self.assertEqual(payload['total_elements'], 2)
        self.assertTrue(payload['viewport_filtered'])

    def test_lithuanian_toll_equipment_maps_only_current_plate_readers(self):
        now = 1790520000
        def site(site_id, kind, lon=23.2, start=1675296000000, end=None):
            return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lon, 55.9]},
                    'properties': {'objectid': site_id, 'tipas': kind, 'kelionumeris': 'A18',
                                   'km': 8.171, 'galiojimopradzia': start,
                                   'galiojimopabaiga': end}}
        catalog = {'features': [
            site(1, 'ANAK'), site(2, 'AKNAĮ'), site(2, 'AKNAĮ'),
            site(3, 'ANAK', lon=26.5), site(4, 'ANAK', end=(now - 1) * 1000),
            site(5, 'ANAK', start=(now + 1) * 1000), site(6, 'unknown')],
        }
        rows = proxy.lithuania_toll_plate_readers(catalog, (22, 55, 24, 56), now)
        self.assertEqual([row['id'] for row in rows], ['lt:via:toll:1', 'lt:via:toll:2'])
        self.assertEqual(rows[0]['title'], 'Plate reader · toll enforcement')
        self.assertEqual(rows[1]['title'], 'Vehicle classifier and plate reader')
        self.assertIn('operating status unverified', rows[0]['detail'])
        self.assertEqual(rows[0]['source_url'], proxy.LITHUANIA_TOLL_EQUIPMENT_SOURCE)

    def test_lithuanian_inventory_joins_lpr_view_without_replacing_deflock(self):
        catalog = {'features': [{'geometry': {'type': 'Point', 'coordinates': [23.2, 55.9]},
                                 'properties': {'objectid': 60, 'tipas': 'ANAK',
                                                'galiojimopradzia': 1675296000000}}]}
        index = {'tile_size_degrees': 20, 'tile_url': 'https://example.test/{lat}/{lon}.json',
                 'regions': ['40/20']}
        def cached(url, key, ttl=900):
            if key == 'deflock-index:v1': return index
            if key.startswith('deflock-tile:'): return [
                {'id': 7, 'lat': 55.8, 'lon': 23.1, 'tags': {'operator': 'community'}}]
            if key == 'lt-via-toll-equipment:v1': return catalog
            raise AssertionError(key)
        with patch.object(proxy, 'cached_deflock_json', side_effect=cached):
            payload = json.loads(proxy.fetch_deflock_lpr_content((22, 55, 24, 56)))
        self.assertEqual({row['id'] for row in payload['elements']}, {7, 'lt:via:toll:60'})
        self.assertEqual(payload['total_elements'], 2)

    def test_official_lithuanian_locations_survive_deflock_outage(self):
        catalog = {'features': [{'geometry': {'type': 'Point', 'coordinates': [23.2, 55.9]},
                                 'properties': {'objectid': 60, 'tipas': 'ANAK',
                                                'galiojimopradzia': 1675296000000}}]}
        def cached(url, key, ttl=900):
            if key == 'deflock-index:v1': raise OSError('index unavailable')
            if key == 'lt-via-toll-equipment:v1': return catalog
            raise AssertionError(key)
        with patch.object(proxy, 'cached_deflock_json', side_effect=cached):
            payload = json.loads(proxy.fetch_deflock_lpr_content((22, 55, 24, 56)))
        self.assertEqual([row['id'] for row in payload['elements']], ['lt:via:toll:60'])
        self.assertTrue(payload['sourceErrors'])

    def test_dutch_police_plan_survives_deflock_outage(self):
        official = [{'type': 'node', 'id': 'nl:politie:anpr:1234', 'lat': 52.4, 'lon': 4.8,
                     'title': 'A10 West', 'detail': 'Q3 2026 camera plan · status unverified',
                     'source': 'Dutch Police', 'source_url': 'https://zoek.officielebekendmakingen.nl/stcrt-2026-23725.html'}]
        with patch.object(proxy, 'cached_deflock_json', side_effect=OSError('index unavailable')), \
                patch.object(proxy, 'cached_dutch_anpr_catalog', return_value=official):
            payload = json.loads(proxy.fetch_deflock_lpr_content((4.7, 52.3, 4.9, 52.5)))
        self.assertEqual(payload['elements'], official)
        self.assertTrue(payload['sourceErrors'])

    def test_milan_area_b_maps_only_listed_active_entry_plate_readers(self):
        def gate(identifier, status='ATTIVI E SANZIONANTI', point=(9.2, 45.47)):
            return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': list(point)},
                    'properties': {'id_amat': identifier, 'nome': '001 - CORELLI', 'stato': status}}
        catalog = {'type': 'FeatureCollection', 'features': [
            gate(1), gate(2, 'IN PRE-ESERCIZIO'), gate(3, point=(10, 45.47)), gate(1)]}
        rows = proxy.milan_area_b_plate_readers(catalog, (9.1, 45.4, 9.3, 45.5))
        self.assertEqual([row['id'] for row in rows], ['it:milano:areab:1'])
        self.assertIn('CORELLI', rows[0]['title'])
        self.assertIn('current operation unverified', rows[0]['detail'])
        self.assertEqual(rows[0]['source_url'], proxy.MILAN_AREA_B_GATES_SOURCE)
        with self.assertRaisesRegex(ValueError, 'invalid'):
            proxy.milan_area_b_plate_readers({'features': []}, (9.1, 45.4, 9.3, 45.5))

    def test_milan_official_gates_survive_deflock_outage(self):
        catalog = {'type': 'FeatureCollection', 'features': [
            {'geometry': {'type': 'Point', 'coordinates': [9.2, 45.47]},
             'properties': {'id_amat': 1, 'nome': 'CORELLI',
                            'stato': 'ATTIVI E SANZIONANTI'}}]}
        area_c = {'type': 'FeatureCollection', 'features': [
            {'geometry': {'type': 'Point', 'coordinates': [9.19, 45.46]},
             'properties': {'id_amat': 57, 'label': 'PORTA TENAGLIA'}}]}
        def cached(url, key, ttl=900):
            if key == 'deflock-index:v1': raise OSError('index unavailable')
            if key == 'it-milan-area-b-gates:v1': return catalog
            if key == 'it-milan-area-c-gates:v1': return area_c
            raise AssertionError(key)
        with patch.object(proxy, 'cached_deflock_json', side_effect=cached):
            payload = json.loads(proxy.fetch_deflock_lpr_content((9.1, 45.4, 9.3, 45.5)))
        self.assertEqual({row['id'] for row in payload['elements']},
                         {'it:milano:areab:1', 'it:milano:areac:57'})
        self.assertTrue(payload['sourceErrors'])

    def test_milan_area_c_gate_inventory_is_bounded_and_labeled(self):
        catalog = {'type': 'FeatureCollection', 'features': [
            {'geometry': {'type': 'Point', 'coordinates': [9.19, 45.46]},
             'properties': {'id_amat': 57, 'label': 'PORTA TENAGLIA'}},
            {'geometry': {'type': 'Point', 'coordinates': [9.19, 45.46]},
             'properties': {'id_amat': 57, 'label': 'duplicate'}},
            {'geometry': {'type': 'Point', 'coordinates': [10, 45.46]},
             'properties': {'id_amat': 58, 'label': 'out of Milan'}}]}
        rows = proxy.milan_area_c_plate_readers(catalog, (9.1, 45.4, 9.3, 45.5))
        self.assertEqual([row['id'] for row in rows], ['it:milano:areac:57'])
        self.assertIn('PORTA TENAGLIA', rows[0]['title'])
        self.assertIn('operation unverified', rows[0]['detail'])
        self.assertEqual(rows[0]['source_url'], proxy.MILAN_AREA_C_GATES_SOURCE)

    def test_bologna_maps_only_active_unique_gates_in_city_bounds(self):
        def gate(gate_id, active='S', lon=11.34):
            return {'identificativo_varco': gate_id, 'attivo': active,
                    'descrizione': 'via Marconi', 'tipologia_varco': 'ZTL',
                    'coordinate': {'lon': lon, 'lat': 44.5}}
        catalog = {'total_count': 4, 'results': [gate(4), gate(4), gate(5, 'N'), gate(6, lon=12)]}
        rows = proxy.bologna_plate_readers(catalog, (11.3, 44.45, 11.4, 44.55))
        self.assertEqual([row['id'] for row in rows], ['it:bologna:gate:4'])
        self.assertIn('operation unverified', rows[0]['detail'])
        self.assertEqual(rows[0]['source_url'], proxy.BOLOGNA_GATES_SOURCE)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            proxy.bologna_plate_readers({'total_count': 101, 'results': []}, (11.3, 44.45, 11.4, 44.55))

    def test_florence_telematic_gates_survive_deflock_outage(self):
        catalog = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {'id': 105, 'titolo': 'Via dei Bardi (Area Pedonale)'},
             'geometry': {'type': 'Point', 'coordinates': [11.25355, 43.76717]}},
            {'type': 'Feature', 'properties': {'id': 105, 'titolo': 'duplicate'},
             'geometry': {'type': 'Point', 'coordinates': [11.25355, 43.76717]}},
            {'type': 'Feature', 'properties': {'id': 106},
             'geometry': {'type': 'Point', 'coordinates': [12, 43.76717]}}]}
        def cached(url, key, ttl=900):
            if key == 'deflock-index:v1': raise OSError('index unavailable')
            if key == 'it-florence-gates:v1': return catalog
            raise AssertionError(key)
        with patch.object(proxy, 'cached_deflock_json', side_effect=cached):
            payload = json.loads(proxy.fetch_deflock_lpr_content((11.2, 43.74, 11.3, 43.8)))
        self.assertEqual([row['id'] for row in payload['elements']], ['it:firenze:gate:105'])
        self.assertIn('Via dei Bardi', payload['elements'][0]['title'])
        self.assertTrue(payload['sourceErrors'])


if __name__ == '__main__':
    unittest.main()
