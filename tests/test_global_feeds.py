import unittest
import urllib.error
import json
from unittest.mock import patch

import global_feeds


class GlobalFeedTests(unittest.TestCase):
    def test_route_requires_plausible_matching_callsign_and_caches_result(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, count):
                return json.dumps([{'callsign': 'FFT3469', 'plausible': True, '_airports': [
                    {'lat': 28.43, 'lon': -81.31, 'iata': 'MCO', 'name': 'Orlando'},
                    {'lat': 29.98, 'lon': -95.34, 'iata': 'IAH', 'name': 'Houston'}]}]).encode()
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_ROUTE_CACHE.clear()
        with patch.object(global_feeds.urllib.request, 'urlopen', return_value=Response()) as fetch:
            first = global_feeds.aircraft_route('FFT3469', 28.1, -82.8)
            second = global_feeds.aircraft_route('FFT3469', 28.1, -82.8)
        self.assertEqual(first, second)
        self.assertEqual(first['route']['origin']['code'], 'MCO')
        self.assertEqual(first['route']['destination']['code'], 'IAH')
        self.assertEqual(first['kind'], 'plausible callsign route')
        fetch.assert_called_once()

    def test_route_rejects_invalid_input_and_implausible_result(self):
        for callsign, lat, lon in [('N12345', 91, 0), ('ABC/DEF', 0, 0)]:
            with self.assertRaises(ValueError):
                global_feeds.aircraft_route(callsign, lat, lon)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, count):
                return json.dumps([{'callsign': 'FFT3469', 'plausible': False, '_airports': [
                    {'lat': 28, 'lon': -81}, {'lat': 29, 'lon': -95}]}]).encode()
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_ROUTE_CACHE.clear()
        with patch.object(global_feeds.urllib.request, 'urlopen', return_value=Response()):
            self.assertIsNone(global_feeds.aircraft_route('FFT3469', 28.1, -82.8)['route'])

    def test_provider_rate_limit_pauses_followup_requests(self):
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_PROVIDER_BACKOFF.clear()
        error = urllib.error.HTTPError('https://api.adsb.lol/v2/hex/abc123', 429,
                                       'Too Many Requests', {'Retry-After': '75'}, None)
        with patch.object(global_feeds.urllib.request, 'urlopen', side_effect=error) as fetch:
            with self.assertRaises(global_feeds.AircraftRateLimited) as first:
                global_feeds._json_get('https://api.adsb.lol/v2/hex/abc123')
            with self.assertRaises(global_feeds.AircraftRateLimited):
                global_feeds._json_get('https://api.adsb.lol/v2/hex/def456')
        self.assertEqual(first.exception.retry_after, 75)
        fetch.assert_called_once()
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_PROVIDER_BACKOFF.clear()

    def test_opensky_420_uses_provider_retry_header(self):
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_PROVIDER_BACKOFF.clear()
        error = urllib.error.HTTPError('https://opensky-network.org/api/states/all', 420,
                                       'Rate limited', {'X-Rate-Limit-Retry-After-Seconds': '120'}, None)
        with patch.object(global_feeds.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(global_feeds.AircraftRateLimited) as result:
                global_feeds._json_get('https://opensky-network.org/api/states/all')
        self.assertEqual(result.exception.retry_after, 120)
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_PROVIDER_BACKOFF.clear()

    def test_opensky_normalizes_global_aircraft(self):
        states = [
            ['abc123', 'TEST1 ', 'GB', 0, 0, -0.1, 51.5, 1000, False, 100, 90],
            ['ground', 'PARKED', 'GB', 0, 0, -0.2, 51.6, 0, True, 0, 0],
        ]
        with patch.object(global_feeds, '_json_get', return_value={'states': states}):
            rows = global_feeds._opensky_aircraft()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['flight'], 'TEST1')
        self.assertEqual(rows[0]['alt_baro'], 3281)
        self.assertEqual(rows[0]['gs'], 194)

    def test_aircraft_cache_avoids_extra_provider_calls(self):
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_CACHE.clear()
        with patch.object(global_feeds, '_world_aircraft', return_value={'aircraft': [], 'source': 'test'}) as load:
            first = global_feeds.aircraft_snapshot()
            second = global_feeds.aircraft_snapshot()
        self.assertEqual(first, second)
        load.assert_called_once()

    def test_specific_aircraft_validates_identifier_and_reuses_cache(self):
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_CACHE.clear()
        expected = {'aircraft': [{'hex': 'abc123', 'lat': 51.5, 'lon': -0.1}], 'source': 'test'}
        with patch.object(global_feeds, '_specific_aircraft', return_value=expected) as load:
            first = global_feeds.aircraft_snapshot('hex', identifier='ABC123')
            second = global_feeds.aircraft_snapshot('hex', identifier='abc123')
        self.assertEqual(first, second)
        load.assert_called_once_with('hex', 'ABC123')
        for value in ('', 'abc123/../../world', 'too-long-identifier'):
            with self.assertRaises(ValueError):
                global_feeds.aircraft_snapshot('hex', identifier=value)
        with self.assertRaises(ValueError):
            global_feeds.aircraft_snapshot('unknown')

    def test_registration_lookup_uses_specific_aircraft_source(self):
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_CACHE.clear()
        with patch.object(global_feeds, '_specific_aircraft', return_value={'aircraft': []}) as load:
            global_feeds.aircraft_snapshot('registration', identifier='tc-son')
        load.assert_called_once_with('registration', 'TC-SON')

    def test_current_flight_track_is_normalized_and_shared_from_cache(self):
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_TRACK_CACHE.clear()
            global_feeds.AIRCRAFT_DAILY_REQUESTS.clear()
        track = {'path': [[100, 28.1, -81.3, 1000, 90, False],
                          [120, 28.2, -81.2, 1100, 90, False],
                          [130, None, -81.1, 1100, 90, False]]}
        with patch.object(global_feeds, '_json_get', return_value=track) as fetch:
            first = json.loads(global_feeds.aircraft_track('A4B065'))
            second = json.loads(global_feeds.aircraft_track('a4b065'))
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'available')
        self.assertEqual(first['points'], [[-81.3, 28.1, 100], [-81.2, 28.2, 120]])
        fetch.assert_called_once_with('https://opensky-network.org/api/tracks/all?icao24=a4b065&time=0')
        with self.assertRaises(ValueError):
            global_feeds.aircraft_track('../bad')

    def test_hex_lookup_falls_back_to_opensky_when_adsb_lol_fails(self):
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_FALLBACK_CACHE.clear()
            global_feeds.AIRCRAFT_DAILY_REQUESTS.clear()
        state = ['a4b065', 'TEST1', 'US', 100, 100, -81.3, 28.1, 1000, False, 100, 90]
        with patch.object(global_feeds, '_json_get', side_effect=[
            global_feeds.AircraftRateLimited(60), {'states': [state]}]):
            result = global_feeds._specific_aircraft('hex', 'A4B065')
        self.assertEqual(result['source'], 'OpenSky fallback')
        self.assertEqual(result['aircraft'][0]['hex'], 'a4b065')

    def test_failed_refresh_keeps_last_positions_marked_stale(self):
        body = json.dumps({'aircraft': [{'hex': 'a4b065', 'lat': 28.1, 'lon': -81.3}],
                           'source': 'test', 'updated_at': 1000}).encode()
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_CACHE.clear()
            global_feeds.AIRCRAFT_CACHE[('world',)] = {'body': body, 'expires': 999, 'stale': 1800}
        with patch.object(global_feeds.time, 'time', return_value=1100), \
             patch.object(global_feeds, '_world_aircraft', side_effect=RuntimeError('offline')):
            result = json.loads(global_feeds.aircraft_snapshot('world'))
        self.assertTrue(result['stale'])
        self.assertEqual(result['aircraft'][0]['hex'], 'a4b065')

    def test_failed_local_feed_uses_labeled_cached_world_positions(self):
        world = json.dumps({'aircraft': [
            {'hex': 'near', 'lat': 28.1, 'lon': -81.3},
            {'hex': 'far', 'lat': 40.7, 'lon': -74.0}], 'updated_at': 1000}).encode()
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_CACHE.clear()
            global_feeds.AIRCRAFT_CACHE[('world',)] = {'body': world, 'expires': 1200, 'stale': 1800}
        with patch.object(global_feeds.time, 'time', return_value=1100), \
             patch.object(global_feeds, '_local_aircraft', side_effect=RuntimeError('offline')):
            result = json.loads(global_feeds.aircraft_snapshot('local', 28.6, -81.3, 300))
        self.assertTrue(result['stale'])
        self.assertEqual(result['source'], 'OpenSky cached overview')
        self.assertEqual([row['hex'] for row in result['aircraft']], ['near'])

    def test_failed_local_feed_populates_world_fallback_on_cold_start(self):
        with global_feeds.AIRCRAFT_LOCK:
            global_feeds.AIRCRAFT_CACHE.clear()
        world = {'aircraft': [
            {'hex': 'near', 'lat': 28.1, 'lon': -81.3},
            {'hex': 'far', 'lat': 40.7, 'lon': -74.0}],
            'source': 'OpenSky', 'updated_at': int(global_feeds.time.time())}
        with patch.object(global_feeds, '_local_aircraft', side_effect=global_feeds.AircraftRateLimited(60)) as local, \
             patch.object(global_feeds, '_world_aircraft', return_value=world) as overview:
            first = json.loads(global_feeds.aircraft_snapshot('local', 28.6, -81.3, 300))
            second = json.loads(global_feeds.aircraft_snapshot('local', 28.6, -81.3, 300))
        self.assertEqual(first, second)
        self.assertTrue(first['stale'])
        self.assertEqual([row['hex'] for row in first['aircraft']], ['near'])
        overview.assert_called_once()
        local.assert_called_once()

    def test_ais_position_uses_lowercase_metadata_coordinates(self):
        with global_feeds.AIS_LOCK:
            global_feeds.AIS_STATE['vessels'].clear()
        global_feeds._vessel_message({
            'MessageType': 'PositionReport',
            'MetaData': {'MMSI': 368207620, 'ShipName': 'TEST VESSEL', 'latitude': 51.9, 'longitude': 1.2},
            'Message': {'PositionReport': {'Sog': 12.4, 'Cog': 86.7, 'TrueHeading': 87}},
        })
        vessel = global_feeds.AIS_STATE['vessels']['368207620']
        self.assertEqual((vessel['lat'], vessel['lon']), (51.9, 1.2))
        self.assertEqual(vessel['heading'], 87)

    def test_ais_vessel_cache_has_hard_limit(self):
        with global_feeds.AIS_LOCK:
            saved = global_feeds.AIS_STATE['vessels']
            global_feeds.AIS_STATE['vessels'] = {}
        try:
            with patch.object(global_feeds, 'AIS_MAX_VESSELS', 2), \
                 patch.object(global_feeds, 'AIS_PRUNE_THRESHOLD', 3):
                for mmsi in range(100000001, 100000005):
                    global_feeds._vessel_message({
                        'MessageType': 'PositionReport',
                        'MetaData': {'MMSI': mmsi, 'latitude': 28.0, 'longitude': -80.0},
                        'Message': {'PositionReport': {'Sog': 10.0}},
                    })
            with global_feeds.AIS_LOCK:
                self.assertEqual(len(global_feeds.AIS_STATE['vessels']), 2)
        finally:
            with global_feeds.AIS_LOCK:
                global_feeds.AIS_STATE['vessels'] = saved

    def test_vessel_search_matches_recent_names_and_ignores_old_reports(self):
        now = int(global_feeds.time.time())
        with global_feeds.AIS_LOCK:
            previous_status = global_feeds.AIS_STATE['status']
            global_feeds.AIS_STATE['status'] = 'needs_key'
            global_feeds.AIS_STATE['vessels'] = {
                '123456789': {'mmsi': '123456789', 'name': 'Ever Given', 'lat': 30.0, 'lon': 32.0, 'updated_at': now},
                '987654321': {'mmsi': '987654321', 'name': 'EVER BRIGHT', 'lat': 31.0, 'lon': 32.0, 'updated_at': now - 1300},
                '111222333': {'mmsi': '111222333', 'name': 'Maersk Alabama', 'lat': 30.0, 'lon': 32.0, 'updated_at': now},
            }
        with patch.object(global_feeds, '_ais_key', return_value='configured'):
            result = global_feeds.vessel_search('  EVER  ')
        self.assertEqual([row['mmsi'] for row in result['vessels']], ['123456789'])
        self.assertEqual(result['status'], 'not_started')
        with self.assertRaises(ValueError):
            global_feeds.vessel_search('x')
        with global_feeds.AIS_LOCK:
            global_feeds.AIS_STATE['vessels'].clear()
            global_feeds.AIS_STATE['status'] = previous_status

    def test_ais_boxes_split_across_dateline(self):
        boxes = global_feeds._boxes_for_view(-10, 170, 10, 190)
        self.assertEqual(len(boxes), 2)
        self.assertEqual(boxes[0][0], [10, 170])
        self.assertEqual(boxes[1][1][1], -169)

    def test_ais_subscription_combines_viewers_and_expires_idle_areas(self):
        class LiveThread:
            def is_alive(self): return True
        with global_feeds.AIS_LOCK:
            saved = global_feeds.AIS_STATE.copy()
            global_feeds.AIS_STATE.update({'thread': LiveThread(), 'boxes': [], 'viewers': {},
                                           'status': 'live', 'vessels': {}})
        try:
            with patch.object(global_feeds, '_ais_key', return_value='test-key'):
                global_feeds.vessel_snapshot(0, 0, 1, 1, client_id='a' * 32)
                global_feeds.vessel_snapshot(2, 2, 3, 3, client_id='b' * 32)
                global_feeds.vessel_snapshot(0, 0, 1, 1, client_id='c' * 32)
                with global_feeds.AIS_LOCK:
                    self.assertEqual(len(global_feeds.AIS_STATE['boxes']), 2)
                    global_feeds.AIS_STATE['viewers']['a' * 32]['seen_at'] -= 61
                    global_feeds.AIS_STATE['viewers']['c' * 32]['seen_at'] -= 61
                    self.assertEqual(len(global_feeds._active_ais_boxes_locked(global_feeds.time.time())), 1)
        finally:
            with global_feeds.AIS_LOCK:
                global_feeds.AIS_STATE.clear()
                global_feeds.AIS_STATE.update(saved)

    def test_ais_subscription_reports_capacity_without_evicting_viewer(self):
        class LiveThread:
            def is_alive(self): return True
        with global_feeds.AIS_LOCK:
            saved = global_feeds.AIS_STATE.copy()
            global_feeds.AIS_STATE.update({'thread': LiveThread(), 'boxes': [], 'viewers': {},
                                           'status': 'live', 'vessels': {}})
        try:
            with patch.object(global_feeds, '_ais_key', return_value='test-key'), \
                 patch.object(global_feeds, 'AIS_MAX_BOXES', 1):
                first = global_feeds.vessel_snapshot(0, 0, 1, 1, client_id='a' * 32)
                second = global_feeds.vessel_snapshot(2, 2, 3, 3, client_id='b' * 32)
            with patch.object(global_feeds, '_ais_key', return_value='test-key'), \
                 patch.object(global_feeds, 'AIS_MAX_VIEWERS', 1):
                third = global_feeds.vessel_snapshot(0, 0, 1, 1, client_id='c' * 32)
            self.assertEqual(first['status'], 'live')
            self.assertEqual(second['status'], 'capacity')
            self.assertEqual(third['status'], 'capacity')
            with global_feeds.AIS_LOCK:
                self.assertEqual(list(global_feeds.AIS_STATE['viewers']), ['a' * 32])
                self.assertEqual(len(global_feeds.AIS_STATE['boxes']), 1)
        finally:
            with global_feeds.AIS_LOCK:
                global_feeds.AIS_STATE.clear()
                global_feeds.AIS_STATE.update(saved)


if __name__ == '__main__':
    unittest.main()
