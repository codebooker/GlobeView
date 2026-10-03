import copy
import datetime as dt
import io
import json
import unittest
from unittest import mock

from pyproj import Transformer

import georgia_roads as roads
import international_infrastructure as infrastructure
from scripts.update_georgia_road_locations import ANCHORS, build_reference

ZONE = dt.timezone(dt.timedelta(hours=4))
NOW = dt.datetime(2026, 10, 3, 12, tzinfo=ZONE)
# Public English listing record 5279 (28 September 2026), reduced to the
# announced schedule and road identity. Non-schedule prose is omitted.
BODY = ('<p>Installation works for electrical and mechanical tunnel systems on the Batumi Bypass Road.</p>'
        '<p>From September 28, traffic will be restricted during nighttime hours only: '
        'from Monday through Friday, between 00:00 and 07:00, '
        'and on weekends, between 02:00 and 07:00.</p>'
        '<p>This traffic regime will remain in effect for two months. '
        'During daytime hours, traffic on the Batumi Bypass Road will continue as usual.</p>')


def notice(record_id=5279, published='2026-09-28 17:25:32', status='3',
           title='Traffic on the Batumi Bypass Road to Be Temporarily Restricted during Nighttime Hours Due to Planned Works',
           body=BODY):
    return {'id': record_id, 'status': 1, 'deleted_at': None, 'restriction_status': status,
            'publish_date': published, 'translates': [{'language': 'en', 'title': title, 'content': body}]}


class GeorgiaRoadsTests(unittest.TestCase):
    def test_current_weekend_window_and_road_path(self):
        feature = roads.parse_restrictions([notice()], NOW)[0]
        p = feature['properties']
        self.assertFalse(p['restriction_active'])
        self.assertIn('Open now · Next restriction: 04 Oct · 02:00–07:00 UTC+4', p['detail'])
        self.assertEqual(p['window_starts_at'], '2026-10-04T02:00:00+04:00')
        self.assertEqual(p['starts_at'], '2026-09-28T00:00:00+04:00')
        self.assertEqual(p['ends_at'], '2026-11-28T00:00:00+04:00')
        self.assertEqual(p['layer'], 'construction')
        self.assertEqual(p['source_url'], 'https://georoad.gov.ge/en/restriction/5279')
        self.assertIn(feature['geometry']['coordinates'], p['road_segments'][0])
        self.assertEqual(p['valid_until'], NOW.timestamp() + 900)
        # The whole path remains available even when the representative marker is outside the viewport.
        endpoint = p['road_segments'][0][0]
        self.assertTrue(infrastructure._road_feature_in_bbox(feature, [endpoint[0] - .001, endpoint[1] - .001,
                                                                    endpoint[0] + .001, endpoint[1] + .001]))
        self.assertFalse(infrastructure._road_feature_in_bbox(feature, [44, 41, 45, 42]))

    def test_exact_weekday_weekend_transitions_and_expiry(self):
        for now, active, first in [
            (dt.datetime(2026, 10, 3, 1, 59, 59, tzinfo=ZONE), False, '2026-10-03T02:00:00+04:00'),
            (dt.datetime(2026, 10, 3, 2, tzinfo=ZONE), True, '2026-10-03T02:00:00+04:00'),
            (dt.datetime(2026, 10, 3, 7, tzinfo=ZONE), False, '2026-10-04T02:00:00+04:00'),
            (dt.datetime(2026, 10, 4, 8, tzinfo=ZONE), False, '2026-10-05T00:00:00+04:00'),
            (dt.datetime(2026, 10, 5, 0, tzinfo=ZONE), True, '2026-10-05T00:00:00+04:00')]:
            with self.subTest(now=now):
                p = roads.parse_restrictions([notice()], now)[0]['properties']
                self.assertEqual(p['restriction_active'], active)
                self.assertEqual(p['window_starts_at'], first)
                transition = p['window_ends_at'] if active else first
                self.assertEqual(p['valid_until'], min(now.timestamp() + 900,
                                                     dt.datetime.fromisoformat(transition).timestamp()))
        self.assertEqual(roads.parse_restrictions([notice()], dt.datetime(2026, 11, 28, tzinfo=ZONE)), [])
        self.assertEqual(roads.parse_restrictions([notice()], dt.datetime(2026, 11, 27, 8, tzinfo=ZONE)), [])

    def test_newer_reopening_or_changed_notice_suppresses_older_schedule(self):
        restored = notice(5282, '2026-10-02 13:00:00', '2', 'Traffic Restored on the Batumi Bypass Road')
        changed = notice(5282, '2026-10-02 13:00:00', '3', body='<p>Batumi Bypass Road: the traffic regime has changed.</p>')
        for latest in [restored, changed, dict(changed, restriction_status='4'),
                       notice(5282, '2026-10-02 13:00:00', body='')]:
            self.assertEqual(roads.parse_restrictions([notice(), latest], NOW), [])
            self.assertEqual(roads.parse_restrictions([latest, notice()], NOW), [])
        duplicate = notice()
        self.assertEqual(len(roads.parse_restrictions([notice(), duplicate], NOW)), 1)
        conflict = notice(5280)
        self.assertEqual(roads.parse_restrictions([notice(), conflict], NOW), [])
        unrelated = notice(5282, '2026-10-02 13:00:00', '2', 'Traffic Restored at km 11 of the Tsalenjikha–Obuji–Jikhaskari Road')
        self.assertEqual(len(roads.parse_restrictions([notice(), unrelated], NOW)), 1)

    def test_hidden_foreign_future_old_and_unrelated_records_are_not_mapped(self):
        variants = [dict(notice(), status=0), dict(notice(), status=True),
                    dict(notice(), deleted_at='2026-09-29'), dict(notice(), id=True),
                    dict(notice(), translates=None), notice(published='2027-09-28 17:25:32'),
                    notice(published='2025-09-28 17:25:32'), notice(published='bad'),
                    notice(title='Roads Department Reminder Regarding Temporary Water Supply Restrictions')]
        foreign = notice()
        foreign['translates'][0]['language'] = 'ka'
        variants.append(foreign)
        for row in variants:
            with self.subTest(row=row):
                self.assertEqual(roads.parse_restrictions([row], NOW), [])
        with self.assertRaises(ValueError):
            roads.parse_restrictions([notice()], NOW.replace(tzinfo=None))

    def test_ambiguous_invalid_and_overnight_windows_are_rejected(self):
        for body in [BODY.replace('00:00 and 07:00', '25:00 and 07:00'),
                     BODY.replace('02:00 and 07:00', '22:00 and 07:00'),
                     BODY.replace('02:00 and 07:00', '07:00 and 07:00'),
                     BODY + '<p>On weekends, between 03:00 and 08:00.</p>',
                     BODY.replace('for two months', 'until further notice'),
                     BODY.replace('September 28,', 'September 31,'),
                     BODY.replace('September 28,', 'September 28, 2025,'),
                     BODY.replace('will continue as usual', 'will also be restricted'),
                     BODY.replace('September 28,', 'September 28, traffic. From September 29,')]:
            self.assertEqual(roads.parse_restrictions([notice(body=body)], NOW), [])

    def test_calendar_month_duration_year_rollover_and_long_running_schedule(self):
        body = BODY.replace('September 28', 'December 28')
        row = notice(published='2026-12-28 10:00:00', body=body)
        p = roads.parse_restrictions([row], dt.datetime(2027, 1, 2, 3, tzinfo=ZONE))[0]['properties']
        self.assertEqual(p['ends_at'], '2027-02-28T00:00:00+04:00')
        row = notice(published='2026-08-31 10:00:00', body=BODY.replace('September 28', 'August 31'))
        p = roads.parse_restrictions([row], dt.datetime(2026, 10, 30, 2, tzinfo=ZONE))[0]['properties']
        self.assertEqual(p['ends_at'], '2026-10-31T00:00:00+04:00')
        row = notice(published='2026-08-31 10:00:00', body=BODY.replace('September 28', 'August 31').replace('two months', 'three months'))
        p = roads.parse_restrictions([row], dt.datetime(2026, 11, 29, 3, tzinfo=ZONE))[0]['properties']
        self.assertTrue(p['restriction_active'])
        self.assertEqual(p['ends_at'], '2026-11-30T00:00:00+04:00')

    def test_shared_catalog_reinterprets_cached_windows_without_network_reads(self):
        with mock.patch.dict(roads._CACHE, {'until': 0, 'rows': []}, clear=True), \
                mock.patch.object(roads, '_read_page', return_value=([notice()], 1)) as read:
            first = roads.road_restrictions(dt.datetime(2026, 10, 3, 1, 59, tzinfo=ZONE))
            second = roads.road_restrictions(dt.datetime(2026, 10, 3, 2, tzinfo=ZONE))
            self.assertFalse(first[0]['properties']['restriction_active'])
            self.assertTrue(second[0]['properties']['restriction_active'])
            self.assertEqual(roads.road_restrictions(dt.datetime(2026, 11, 28, tzinfo=ZONE)), [])
            read.assert_called_once_with(1)
        self.assertIs(infrastructure._FETCHERS['roads']['ge_georoad_restrictions'], roads.road_restrictions)

    def test_pagination_stops_at_lookback_and_does_not_cache_partial_or_unordered_scan(self):
        old = notice(4000, '2026-05-01 00:00:00')
        with mock.patch.dict(roads._CACHE, {'until': 0, 'rows': []}, clear=True), \
                mock.patch.object(roads, '_read_page', side_effect=[([notice()], 511), ([old], 511)]) as read:
            self.assertEqual(len(roads.road_restrictions(NOW)), 1)
            self.assertEqual(read.call_count, 2)
        for batches in [[([old, notice()], 511)], [([notice()], 511)] * 8]:
            with mock.patch.dict(roads._CACHE, {'until': 0, 'rows': []}, clear=True), \
                    mock.patch.object(roads, '_read_page', side_effect=batches):
                with self.assertRaises(ValueError):
                    roads.road_restrictions(NOW)
                self.assertEqual(roads._CACHE['until'], 0)

    def test_reader_uses_public_english_endpoint_and_limits_schema(self):
        value = notice()
        value['user_id'] = 'not imported'
        value['translates'][0]['editor_id'] = 'not imported'
        payload = {'message': 'Success', 'items': {'current_page': 1, 'last_page': 511, 'data': [value]}}
        response = io.BytesIO(json.dumps(payload).encode())
        response.status, response.geturl = 200, lambda: roads.API_URL
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(roads.urllib.request, 'build_opener', return_value=opener):
            rows, last = roads._read_page(1)
        self.assertEqual(last, 511)
        self.assertNotIn('user_id', rows[0])
        self.assertNotIn('editor_id', rows[0]['translates'][0])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, roads.API_URL)
        self.assertEqual(request.get_header('Locale'), 'en')
        self.assertEqual(request.get_method(), 'GET')
        for wrong in [dict(payload, message='Error'), {'message': 'Success', 'items': {'current_page': 2, 'last_page': 2, 'data': []}}]:
            response = io.BytesIO(json.dumps(wrong).encode())
            response.status, response.geturl = 200, lambda: roads.API_URL
            opener.open.return_value = response
            with mock.patch.object(roads.urllib.request, 'build_opener', return_value=opener), self.assertRaises(ValueError):
                roads._read_page(1)
        with self.assertRaises(ValueError):
            roads._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.example')

    def test_builder_rejects_wrong_road_disconnected_graph_and_changed_projection(self):
        location = roads.catalog()['batumi-bypass']
        points = location['roadSegments'][0]
        ids = [ANCHORS[0]] + list(range(90000000001, 90000000000 + len(points) - 1)) + [ANCHORS[1]]
        osm = {'elements': [{'type': 'way', 'id': 5, 'tags': {'highway': 'trunk'}, 'nodes': ids,
                             'geometry': [{'lon': p[0], 'lat': p[1]} for p in points]}]}
        transform = Transformer.from_crs(4326, 32638, always_xy=True)
        gis = {'spatialReference': {'wkid': 32638}, 'features': [{'attributes': {'Section_Na': 'Batumi bypass', 'FID': 1},
                                                               'geometry': {'paths': [[list(transform.transform(*p)) for p in points]]}}]}
        roadmap = {'roadmap': {'data': [{'id': 88, 'status': 1, 'json_data': json.dumps(gis), 'publish_date': '2026-08-27 14:36:23'}]}}
        result = build_reference(osm, roadmap)
        self.assertEqual(result['osmEndNodes'], list(ANCHORS))
        self.assertEqual(result['roadSegments'][0], points)
        self.assertTrue(11500 < result['lengthMetres'] < 15500)
        bad = copy.deepcopy(osm)
        bad['elements'][0]['nodes'][0] = 7
        with self.assertRaises(ValueError):
            build_reference(bad, roadmap)
        bad = copy.deepcopy(osm)
        bad['elements'][0]['tags']['highway'] = 'residential'
        with self.assertRaises(ValueError):
            build_reference(bad, roadmap)
        with self.assertRaises(ValueError):
            build_reference({'elements': osm['elements'] * 2}, roadmap)
        # Both end references exist but there is no connecting edge across the middle.
        split = len(points) // 2
        disconnected = {'elements': [dict(osm['elements'][0], nodes=ids[:split],
                                          geometry=osm['elements'][0]['geometry'][:split]),
                                     dict(osm['elements'][0], id=6, nodes=ids[split:],
                                          geometry=osm['elements'][0]['geometry'][split:])]}
        with self.assertRaises(ValueError):
            build_reference(disconnected, roadmap)
        for field, value in [('Section_Na', 'Different road'), ('FID', 2)]:
            bad_gis = copy.deepcopy(gis)
            bad_gis['features'][0]['attributes'][field] = value
            bad_roadmap = copy.deepcopy(roadmap)
            bad_roadmap['roadmap']['data'][0]['json_data'] = json.dumps(bad_gis)
            with self.assertRaises(ValueError):
                build_reference(osm, bad_roadmap)
        bad_gis = copy.deepcopy(gis)
        bad_gis['spatialReference']['wkid'] = 4326
        bad_roadmap = copy.deepcopy(roadmap)
        bad_roadmap['roadmap']['data'][0]['json_data'] = json.dumps(bad_gis)
        with self.assertRaises(ValueError):
            build_reference(osm, bad_roadmap)


if __name__ == '__main__':
    unittest.main()
