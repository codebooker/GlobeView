import unittest

from radio_catalog import normalize_stations, record_station_click


STATION_ID = '12345678-1234-1234-1234-123456789abc'


class RadioCatalogTests(unittest.TestCase):
    def test_catalog_keeps_browser_playable_geocoded_stations_and_deduplicates(self):
        good = {
            'stationuuid': STATION_ID, 'name': '  Test   Radio ',
            'url_resolved': 'https://example.org/live.mp3', 'geo_lat': '42.5',
            'geo_long': '-71.1', 'lastcheckok': 1, 'countrycode': 'US',
            'country': 'United States', 'clickcount': 20,
        }
        rows = [good, {**good, 'stationuuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'},
                {**good, 'stationuuid': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'url_resolved': 'http://example.org/live.mp3'},
                {**good, 'stationuuid': 'cccccccc-cccc-cccc-cccc-cccccccccccc', 'geo_lat': 'NaN'},
                {**good, 'stationuuid': 'dddddddd-dddd-dddd-dddd-dddddddddddd', 'hls': 1},
                {**good, 'stationuuid': 'ffffffff-ffff-ffff-ffff-ffffffffffff', 'url_resolved': 'https://127.0.0.1/live.mp3'},
                {**good, 'stationuuid': 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'lastcheckok': 0}]
        result = normalize_stations(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['name'], 'Test Radio')
        self.assertEqual(result[0]['streamUrl'], good['url_resolved'])

    def test_click_rejects_invalid_id_before_network(self):
        with self.assertRaises(ValueError):
            record_station_click('../private')


if __name__ == '__main__':
    unittest.main()
