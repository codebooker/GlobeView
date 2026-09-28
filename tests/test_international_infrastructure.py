import unittest
import base64
import csv
import datetime as dt
import email.utils
import io
import threading
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

import international_infrastructure as feeds


NOW = 1790445600  # 2026-09-26 UTC


class InfrastructureTests(unittest.TestCase):
    def test_cyprus_road_events_require_fresh_feed_and_active_local_schedule(self):
        now = dt.datetime(2026, 9, 28, 9, tzinfo=dt.timezone.utc).timestamp()
        def record(identity, kind, start, end='', description='Road works'):
            return (f'<s:situationRecord id="{identity}"><c:validityStatus>definedByValidityTimeSpec</c:validityStatus>'
                    f'<c:overallStartTime>{start}</c:overallStartTime>'
                    f'<c:overallEndTime>{end}</c:overallEndTime>'
                    f'<l:latitude>35.17</l:latitude><l:longitude>33.38</l:longitude>'
                    f'<eventTypeId>{kind}</eventTypeId><subtype>construction work</subtype>'
                    f'<description>{description}</description></s:situationRecord>')
        xml = ('<d:payload xmlns:d="http://datex2.eu/schema/3/d2Payload" '
               'xmlns:s="http://datex2.eu/schema/3/situation" '
               'xmlns:c="http://datex2.eu/schema/3/common" '
               'xmlns:l="http://datex2.eu/schema/3/locationReferencing">'
               '<c:publicationTime>2026-09-28T12:03:00+03:00</c:publicationTime>'
               + record('active', '25', '2026-09-28T08:00:00', '2026-09-28T18:00:00')
               + record('future', '25', '2026-09-29T08:00:00', '2026-09-29T18:00:00')
               + record('expired', '25', '2026-09-27T08:00:00', '2026-09-27T18:00:00')
               + '</d:payload>')
        root = ET.fromstring(xml)
        rows = feeds._parse_cyprus_roads(root, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'construction')
        self.assertEqual(rows[0]['geometry']['coordinates'], [33.38, 35.17])
        self.assertEqual(rows[0]['properties']['detail'], 'construction work · Road works')
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_cyprus_roads(root, now + 3600)

    def test_cyprus_waze_alerts_exclude_old_crowdsourced_reports(self):
        now = dt.datetime(2026, 9, 27, 23, 50, tzinfo=dt.timezone.utc).timestamp()
        def alert(identity, reported):
            return (f'<t:trafficElement><c:id>{identity}</c:id><c:generalPublicComment>'
                    '<c:comment><c:commentType>type</c:commentType><c:value>HAZARD</c:value></c:comment>'
                    f'<c:comment><c:commentType>report_time</c:commentType><c:value>{reported}</c:value></c:comment>'
                    '<c:comment><c:commentType>street</c:commentType><c:value>A6</c:value></c:comment>'
                    '</c:generalPublicComment><l:latitude>35.1</l:latitude>'
                    '<l:longitude>33.4</l:longitude></t:trafficElement>')
        xml = ('<e:d2LogicalModel xmlns:e="https://datex2.eu/schema/3/exchangeInformation" '
               'xmlns:t="https://datex2.eu/schema/3/traffic" '
               'xmlns:c="https://datex2.eu/schema/3/common" '
               'xmlns:l="https://datex2.eu/schema/3/locationReferencing">'
               '<c:publicationTime>2026-09-28T02:52:00+03:00</c:publicationTime>'
               + alert('recent', '2026-09-28 02:20:00')
               + alert('old', '2026-09-26 12:00:00') + '</e:d2LogicalModel>')
        root = ET.fromstring(xml)
        rows = feeds._parse_cyprus_waze_alerts(root, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['detail'], 'A6')
        self.assertEqual(rows[0]['properties']['updated_at'], '2026-09-27T23:20:00+00:00')
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_cyprus_waze_alerts(root, now + 3600)
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_cyprus_waze_alerts(root, now - 9 * 60)

    def test_gdynia_signs_show_text_from_each_display_page(self):
        devices = [{'id': 5, 'location': {'type': 'Point', 'coordinates': [18.48, 54.52]}}]
        messages = [{'id': 12050184, 'vmsId': 5,
                     'contentUrl': '/ri/vms/messages/12050184',
                     'insertTime': '2026-09-27 08:13:31'},
                    {'id': 12050185, 'vmsId': 6,
                     'contentUrl': 'https://example.org/other',
                     'insertTime': '2026-09-27 08:13:31'}]
        pages = feeds._gdynia_sign_pages(
            '<DisplayValue><Text><Value>DROGA ZAMKNI&#280;TA</Value></Text></DisplayValue>'
            '<DisplayValue><Text><Value>OBJAZD MORSKA</Value></Text></DisplayValue>')
        self.assertEqual(pages, ['DROGA ZAMKNIĘTA', 'OBJAZD MORSKA'])
        self.assertEqual(feeds._gdynia_sign_pages(
            '<DisplayValue><Text><Value> </Value></Text></DisplayValue>'), [])
        features = feeds._parse_gdynia_signs(devices, messages, lambda _: pages)
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['properties']['detail'],
                         'DROGA ZAMKNIĘTA / OBJAZD MORSKA')
        self.assertEqual(features[0]['properties']['updated_at'],
                         '2026-09-27T06:13:31+00:00')

    def test_gdynia_sensors_join_segments_and_reject_stale_readings(self):
        now = dt.datetime(2026, 9, 27, 23, 55, tzinfo=ZoneInfo('Europe/Warsaw')).timestamp()
        segments = [{'id': 91, 'geometry': {'type': 'LineString', 'coordinates': [
            [18.5, 54.5], [18.51, 54.51], [18.52, 54.52]]}},
                    {'id': 92, 'geometry': {'type': 'Point', 'coordinates': [18.6, 54.4]}}]
        speeds = [{'roadSegmentId': 91, 'speed': 48, 'measureTime': '2026-09-27 23:50:00'},
                  {'roadSegmentId': 92, 'speed': 200, 'measureTime': '2026-09-27 23:50:00'}]
        intensities = [{'roadSegmentId': 91, 'intensity': 120,
                        'measureTime': '2026-09-27 23:50:00'},
                       {'roadSegmentId': 92, 'intensity': 80,
                        'measureTime': '2026-09-27 22:00:00'}]
        rows = feeds._parse_gdynia_sensors(segments, speeds, intensities, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['geometry']['coordinates'], [18.51, 54.51])
        self.assertIn('48 km/h', rows[0]['properties']['detail'])
        self.assertIn('120 vehicles/h', rows[0]['properties']['detail'])
        with self.assertRaisesRegex(ValueError, 'stale or empty'):
            feeds._parse_gdynia_sensors(segments, speeds, intensities, now + 3600)

    def test_bordeaux_flow_keeps_fresh_road_segments_only(self):
        now = dt.datetime(2026, 9, 27, 20, 40, tzinfo=dt.timezone.utc).timestamp()
        def road(gid, state='DENSE', modified='2026-09-27T20:35:00+00:00', lon=-0.60):
            return {'gid': gid, 'etat': state, 'mdate': modified, 'geo_shape': {
                'geometry': {'type': 'LineString', 'coordinates': [[lon, 44.82], [lon + 0.01, 44.83]]}}}
        data = feeds._parse_bordeaux_flow([
            road('17'), road('18', 'INCONNU'), road('19', modified='2026-09-27T19:00:00+00:00'),
            road('20', lon=2.35)], now)
        self.assertEqual(len(data['features']), 1)
        self.assertEqual(data['features'][0]['properties']['state'], 'DENSE')
        self.assertEqual(data['features'][0]['geometry']['coordinates'][0], [-0.6, 44.82])
        with self.assertRaisesRegex(ValueError, 'no current'):
            feeds._current_bordeaux_flow(data, now + 31 * 60)
        with self.assertRaisesRegex(ValueError, 'no current'):
            feeds._parse_bordeaux_flow([road('18', 'INCONNU')], now)

    def test_bordeaux_signs_show_only_readable_text_from_fresh_publication(self):
        now = dt.datetime(2026, 9, 28, 0, 40, tzinfo=dt.timezone.utc).timestamp()
        metadata = {'metas': {'default': {'data_processed': '2026-09-28T00:35:00+00:00'}}}
        def sign(identity, lon=-0.54, first='PONT FERME', second='SUIVRE DEVIATION'):
            return {'ident': identity, 'geo_point_2d': {'lon': lon, 'lat': 44.85},
                    'page1': first, 'page2': second, 'mdate': '2026-09-26T16:11:00+00:00'}
        publication = {'total_count': 4, 'results': [
            sign('Z40P107'), sign('Z40P108', first='', second=''),
            sign('../bad'), sign('Z40P109', lon=2.35)]}
        rows = feeds._parse_bordeaux_signs(metadata, publication, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['title'], 'PONT FERME / SUIVRE DEVIATION')
        self.assertEqual(rows[0]['properties']['updated_at'], '2026-09-28T00:35:00+00:00')
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_bordeaux_signs(metadata, publication, now + 21 * 60)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            feeds._parse_bordeaux_signs(metadata, {'total_count': 5, 'results': publication['results']}, now)

    def test_paris_roadworks_require_current_reported_work_and_fresh_dataset(self):
        now = dt.datetime(2026, 9, 28, 9, tzinfo=dt.timezone.utc).timestamp()
        metadata = {'metas': {'default': {'data_processed': '2026-09-27T08:25:05+00:00'}}}
        def work(identity, status=2, start='2026-09-20', end='2026-10-02', lon=2.35):
            return {'identifiant': identity, 'statut': status, 'date_debut': start,
                    'date_fin': end, 'voie': 'Rue de Rivoli',
                    'impact_circulation': 'BARRAGE_TOTAL',
                    'description': 'Road works', 'geo_point_2d': {'lon': lon, 'lat': 48.86}}
        rows = feeds._parse_paris_roadworks(metadata, [
            work('CP123456'), work('CP123457', status=4),
            work('CP123458', status=1), work('CP123459', start='2026-10-01'),
            work('CP123460', end='2026-09-27'), work('CP123461', lon=10),
            work('../bad')], now)
        self.assertEqual([row['properties']['key'] for row in rows],
                         ['fr:paris:works:CP123456', 'fr:paris:works:CP123457'])
        self.assertEqual(rows[0]['properties']['layer'], 'construction')
        self.assertIn('Road closed', rows[0]['properties']['detail'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_paris_roadworks(metadata, [work('CP123456')], now + 15 * 86400)

    def test_brussels_counters_only_map_recent_active_measurements(self):
        now = dt.datetime(2026, 9, 27, 19, tzinfo=dt.timezone.utc).timestamp()
        def counter(name, measured='2026-09-27T18:58:00Z', active=1, count=8, speed=42):
            return {'type': 'Feature', 'geometry': {'type': 'Point',
                    'coordinates': [4.35, 50.84]}, 'properties': {
                        'traverse_name': name, 'is_active': active,
                        'end_time_1m_a': measured, 'count_1m_a': count,
                        'speed_1m_a': speed, 'occupancy_1m_a': 16}}
        payload = {'type': 'FeatureCollection', 'totalFeatures': 5, 'features': [
            counter('ARL_103'), counter('ARL_203', speed=-1),
            counter('STALE', measured='2025-10-16T12:56:00Z'),
            counter('INACTIVE', active=0), counter('../BAD')]}
        rows = feeds._parse_brussels_counters(payload, now)
        self.assertEqual([row['properties']['key'] for row in rows],
                         ['be:brussels:counter:ARL_103', 'be:brussels:counter:ARL_203'])
        self.assertEqual(rows[0]['properties']['layer'], 'sensors')
        self.assertIn('8 vehicles/min · 42 km/h average', rows[0]['properties']['detail'])
        self.assertNotIn('km/h', rows[1]['properties']['detail'])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            feeds._parse_brussels_counters(dict(payload, totalFeatures=6), now)
        with patch.object(feeds, '_snapshot', return_value={
                'sources': {'be_brussels_counters': rows}, 'errors': []}):
            self.assertEqual(len(feeds.road_snapshot('sensors', (4.3, 50.8, 4.4, 50.9))['features']), 2)

    def test_vigo_cameras_reject_unavailable_stills_and_untrusted_urls(self):
        row = {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [-8.72, 42.23]},
               'properties': {'id': '05', 'nombre': 'Junction',
                              'url': 'http://camaras.vigo.org/webcam/camv2.php?id=05'}}
        payload = {'features': [row, dict(row, properties=dict(row['properties'], id='06')),
                                dict(row, properties=dict(row['properties'], id='../05'))]}
        rows = feeds._parse_vigo_cameras(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['snapshot_url'], '/vigo-camera/05')
        with self.assertRaises(ValueError):
            feeds.vigo_camera_snapshot('../05')
        class Response:
            url = 'https://camaras.vigo.org/webcam/camv2.php?id=05'
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, size): return b'\xff\xd8\xff' + b'camera image'
        with patch.object(feeds.urllib.request, 'urlopen', return_value=Response()):
            self.assertEqual(feeds.vigo_camera_snapshot('05')[1], 'image/jpeg')
        with patch.object(feeds.urllib.request, 'urlopen', return_value=Response()), \
                patch.object(feeds.hashlib, 'sha256') as digest:
            digest.return_value.hexdigest.return_value = feeds.VIGO_UNAVAILABLE_SHA256
            with self.assertRaises(FileNotFoundError):
                feeds.vigo_camera_snapshot('05')

    def test_vitoria_cameras_require_connected_image_and_valid_location(self):
        image_url = 'https://www.vitoria-gasteiz.org/c11-01w/cameras?action=get&id=CM03'
        row = {'type': 'Feature', 'id': 'CM03',
               'geometry': {'type': 'Point', 'coordinates': [-2.67, 42.85]},
               'properties': {'commStatusCode': 'Conectado', 'imagen': image_url,
                              'nombre': 'City centre'}}
        payload = {'type': 'FeatureCollection', 'features': [
            row, dict(row, id='CM04', properties=dict(row['properties'], commStatusCode='Desconectado')),
            dict(row, id='CM05', properties=dict(row['properties'], imagen='https://elsewhere.example/image')),
            dict(row, id='../CM03'),
            dict(row, id='CM06', geometry={'type': 'Point', 'coordinates': [0, 0]})]}
        rows = feeds._parse_vitoria_cameras(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['snapshot_url'], '/vitoria-camera/CM03')
        with self.assertRaises(ValueError):
            feeds._parse_vitoria_cameras({})
        with self.assertRaises(ValueError):
            feeds.vitoria_camera_snapshot('../CM03')

    def test_estonia_restrictions_use_active_dated_locations_and_strip_private_contacts(self):
        def event(event_id, cause='CONSTRUCTION', start=NOW - 3600, end=NOW + 86400,
                  coordinates=None):
            return {'type': 'Feature', 'geometry': {'type': 'Point',
                    'coordinates': coordinates or [25.6, 58.9]}, 'properties': {
                'objectid': event_id, 'road_name': 'Tallinn–Tartu', 'road_nr': 2,
                'cause': cause, 'effect': 'LANE_CLOSED',
                'extra_info': '<b>Bridge work</b>',
                'traffic_ctrl_contact_phone': '+372 5555 0000',
                'date_from': start * 1000, 'date_to': end * 1000}}
        payload = {'type': 'FeatureCollection', 'features': [
            event(1), event(2, cause='EVENT'), event(1),
            event(3, start=NOW + 3600), event(4, end=NOW - 1),
            event(5, coordinates=[8.8, 47.5])],
        }
        rows = feeds._parse_estonia_restrictions(payload, NOW)
        self.assertEqual([row['properties']['key'] for row in rows],
                         ['ee:tarktee:restriction:1', 'ee:tarktee:restriction:2'])
        self.assertEqual([row['properties']['layer'] for row in rows],
                         ['construction', 'incidents'])
        self.assertIn('Lane closed · Bridge work', rows[0]['properties']['detail'])
        self.assertNotIn('5555', str(rows))
        self.assertNotIn('<b>', str(rows))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            feeds._parse_estonia_restrictions(dict(payload, exceededTransferLimit=True), NOW)

    def test_czech_ndic_roads_use_current_records_and_complete_pages(self):
        now = dt.datetime(2026, 9, 27, 19, tzinfo=dt.timezone.utc).timestamp()
        def event(number, category='Práce na silnici', start='27.09.2026 19:00',
                  end='28.09.2026 08:00', coordinates=None):
            return {'type': 'Feature', 'geometry': {'type': 'Point',
                    'coordinates': coordinates or [14.42, 50.08]}, 'properties': {
                'msgid': f'00000000-0000-0000-0000-{number:012d}',
                'datum_aktualizace': '27.09.2026 20:30', 'zacatek': start, 'konec': end,
                'trida_popis1': category, 'event_popis1': 'uzavřeno',
                'txtmce': '<b>uzavřeno</b>', 'txpl_text': 'D1, Praha',
                'cislo_silnice': 'D1', 'private_contact': '+420 123 456 789'}}
        pages = [
            {'type': 'FeatureCollection', 'features': [event(1), event(2, category='Dopravní uzavírky a omezení'),
                event(3, start='28.09.2026 09:00'), event(4, end='27.09.2026 20:00')],
             'exceededTransferLimit': True},
            {'type': 'FeatureCollection', 'features': [event(1), event(5, coordinates=[0, 0])]},
        ]
        rows = feeds._parse_cz_ndic_roads(pages, now)
        self.assertEqual([row['properties']['layer'] for row in rows], ['construction', 'incidents'])
        self.assertEqual(len({row['properties']['key'] for row in rows}), 2)
        self.assertIn('uzavřeno · D1, Praha', rows[0]['properties']['detail'])
        self.assertNotIn('123 456 789', str(rows))
        with patch.object(feeds, '_snapshot', return_value={
                'sources': {'cz_ndic_roads': rows}, 'errors': []}), \
                patch.object(feeds, '_autobahn_service', return_value=[]):
            self.assertEqual(len(feeds.road_snapshot('construction', (14.3, 50, 14.5, 50.2))['features']), 1)
            self.assertEqual(len(feeds.road_snapshot('incidents', (14.3, 50, 14.5, 50.2))['features']), 1)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            feeds._parse_cz_ndic_roads(pages[:1], now)
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_cz_ndic_roads(pages, now + 7200)
        with patch.object(feeds, '_get_json', side_effect=pages) as fetch, \
                patch.object(feeds.time, 'time', return_value=now):
            self.assertEqual(len(feeds._cz_ndic_roads()), 2)
            self.assertEqual(fetch.call_count, 2)

    def test_prague_planned_works_require_active_valid_approved_records(self):
        now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc).timestamp()
        base = {'id': 2444, 'name': '<b>Bridge works</b>', 'street': 'Lipská',
                'subject': 'CONSTRUCTION', 'state': 'ACTIVE', 'approved': True,
                'hidden': False, 'start': '2026-09-28T08:00:00',
                'end': '2026-09-28T17:00:00', 'lon': 14.28, 'lat': 50.11,
                'investor_phone': 'private contact 5555'}
        payload = {'count': 6, 'data': [base,
            {**base, 'id': 2, 'state': 'DONE'},
            {**base, 'id': 3, 'start': '2026-09-29T08:00:00'},
            {**base, 'id': 4, 'approved': False},
            {**base, 'id': 5, 'hidden': True},
            {**base, 'id': 6, 'lon': 17.0}]}
        rows = feeds._parse_prague_roadworks(payload, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'construction')
        self.assertEqual(rows[0]['properties']['source_url'], 'https://opravujeme.to/action/2444/')
        self.assertNotIn('5555', str(rows))
        self.assertNotIn('<b>', str(rows))
        self.assertEqual(feeds._parse_prague_roadworks(payload, now + 4 * 3600), [])

    def test_brno_waze_reports_separate_works_and_reject_old_reports(self):
        now = dt.datetime(2026, 9, 27, 22, tzinfo=dt.timezone.utc).timestamp()
        def alert(number, kind, subtype='', age=600, coords=None):
            return {'type': 'Feature', 'geometry': {'type': 'Point',
                    'coordinates': coords or [16.61, 49.19]}, 'properties': {
                'uuid': f'00000000-0000-0000-0000-{number:012d}',
                'pubMillis': int((now - age) * 1000), 'type': kind, 'subtype': subtype,
                'street': '<b>Veveří</b>', 'city': 'Brno',
                'reportDescription': '<script>bad</script>Road blocked'}}
        payload = {'type': 'FeatureCollection', 'properties': {'exceededTransferLimit': False},
                   'features': [alert(1, 'ACCIDENT'),
                                alert(2, 'HAZARD', 'HAZARD_ON_ROAD_CONSTRUCTION'),
                                alert(3, 'JAM'), alert(4, 'ACCIDENT', age=7 * 3600),
                                alert(5, 'ACCIDENT', coords=[0, 0])]}
        rows = feeds._parse_brno_waze_alerts(payload, now)
        self.assertEqual([row['properties']['layer'] for row in rows], ['incidents', 'construction'])
        self.assertIn('Reported roadworks', rows[1]['properties']['title'])
        self.assertIn('unverified', rows[0]['properties']['detail'])
        self.assertNotIn('<script>', str(rows))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            feeds._parse_brno_waze_alerts({**payload, 'properties': {'exceededTransferLimit': True}}, now)

    def test_lithuania_cameras_require_recent_capture_and_official_image(self):
        url = 'https://eismoinfo.lt/eismoinfo-backend/image-provider/camera/last?id=72'
        row = {'id': 72, 'name': 'Vilnius A1 10,04', 'roadNr': 'A1', 'km': 10.04,
               'x': 576154, 'y': 6056867, 'image': url, 'date': int((NOW - 300) * 1000)}
        rows = feeds._parse_lithuania_cameras([row, dict(row, id=73),
                                               dict(row, id=74, image='https://elsewhere.example/74.jpg'),
                                               dict(row, id=75, image=url,
                                                    date=int((NOW - 3600) * 1000))], NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['snapshot_url'], '/lithuania-camera/72')
        self.assertAlmostEqual(rows[0]['geometry']['coordinates'][0], 25.1798, places=3)
        self.assertAlmostEqual(rows[0]['geometry']['coordinates'][1], 54.6426, places=3)

    def test_lithuania_camera_proxy_uses_fresh_catalog_and_jpeg(self):
        class Response(io.BytesIO):
            url = 'https://eismoinfo.lt/eismoinfo-backend/image-provider/camera/last?id=72'
            headers = {'Content-Type': 'image/jpeg'}
        feature = feeds._feature([25.18, 54.64], {
            'key': 'lt:eismoinfo:camera:72', 'layer': 'cameras'})
        with self.assertRaises(ValueError):
            feeds.lithuania_camera_snapshot('../72')
        with patch.object(feeds, '_lithuania_cameras', return_value=[]):
            with self.assertRaises(FileNotFoundError):
                feeds.lithuania_camera_snapshot('72')
        with patch.object(feeds, '_lithuania_cameras', return_value=[feature]), \
                patch.object(feeds.urllib.request, 'urlopen', return_value=Response(b'\xff\xd8\xffimage')):
            self.assertEqual(feeds.lithuania_camera_snapshot('72'),
                             (b'\xff\xd8\xffimage', 'image/jpeg'))

    def test_lithuania_road_weather_uses_recent_station_measurements(self):
        row = {'id': 68, 'name': 'Seirijai 132 15,56', 'x': 493361, 'y': 6017514,
               'date': int((NOW - 300) * 1000), 'surfaceCondition': 'Sausa',
               'roadTemperature': 17.3, 'airTemperature': 16.4}
        rows = feeds._parse_lithuania_road_weather([
            row, dict(row, id=69, date=int((NOW - 3600) * 1000)),
            dict(row, id=70, x=0), dict(row, id=68)], NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'sensors')
        self.assertIn('Surface Dry · Road 17.3°C · Air 16.4°C',
                      rows[0]['properties']['detail'])
        with self.assertRaises(ValueError):
            feeds._parse_lithuania_road_weather({'rows': [row]}, NOW)

    def test_lithuania_temporary_restrictions_use_valid_locations_and_types(self):
        payload = [{'layer': 'EAL', 'features': [
            {'id': 'MJ:3814', 'name': 'Kelio remontas',
             'points': [{'point': [422912, 6113682]}]},
            {'id': 'OB:5524', 'name': 'Kliūtis',
             'points': [{'point': [506619, 6086186]}]},
            {'id': 'MJ:9999', 'points': [{'point': [0, 0]}]},
        ]}]
        rows = feeds._parse_lithuania_restrictions(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['properties']['layer'] for row in rows],
                         ['construction', 'incidents'])
        self.assertEqual(rows[0]['properties']['lithuania_event_id'], 'MJ:3814')
        with self.assertRaises(ValueError):
            feeds._parse_lithuania_restrictions([{'layer': 'EA', 'features': []}])

    def test_lithuania_road_event_detail_checks_date_range(self):
        payload = {'name': 'Road construction', 'info': [{
            'keyValue': [{'key': 'Place', 'value': 'A1 10 - 12km'},
                         {'key': 'Date', 'value': '2026-09-26 00:00 - 2026-09-28 23:00'}],
            'text': 'Road resurfacing. Darbų vykdytojas: Contractor 12345.'}]}
        with patch.object(feeds, '_get_json', return_value=payload):
            detail = feeds.lithuania_event_detail('MJ:3814', NOW)
            self.assertEqual(detail['title'], 'Road construction')
            self.assertIn('A1 10 - 12km', detail['detail'])
            self.assertNotIn('Contractor', detail['detail'])
            with self.assertRaises(FileNotFoundError):
                feeds.lithuania_event_detail('MJ:3814', NOW + 10 * 86400)
        with self.assertRaises(ValueError):
            feeds.lithuania_event_detail('../3814')

    def test_iceland_cameras_group_views_and_require_verified_location(self):
        base = {'Maelist_nr': 7001, 'Myndavel': 'Hellisheiði', 'Vegheiti': 'Hringvegur',
                'Breidd': 64.018296, 'Lengd': -21.342636}
        rows = [
            {**base, 'Skyring': 'West',
             'Slod': 'https://www.vegagerdin.is/vgdata/vefmyndavelar/hellisheidi_1.jpg'},
            {**base, 'Skyring': 'East',
             'Slod': 'https://www.vegagerdin.is/vgdata/vefmyndavelar/hellisheidi_2.jpg'},
            {**base, 'Maelist_nr': 7002, 'Slod': 'https://elsewhere.invalid/camera.jpg'},
        ]
        checked = {'7001': (rows[1]['Slod'], NOW - 300)}
        features = feeds._parse_iceland_cameras(rows, checked)
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['geometry']['coordinates'], [-21.342636, 64.018296])
        self.assertEqual(features[0]['properties']['snapshot_url'], rows[1]['Slod'])
        self.assertEqual([view['label'] for view in features[0]['properties']['camera_views']],
                         ['East', 'West'])
        self.assertEqual(feeds._parse_iceland_cameras(rows, {}), [])

    def test_iceland_roads_require_fresh_publication_and_current_event(self):
        stamp = lambda offset: dt.datetime.fromtimestamp(NOW + offset, dt.timezone.utc).isoformat()
        root = ET.fromstring(f'''<messageContainer xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <publicationTime>{stamp(-60)}</publicationTime>
          <situationRecord xsi:type="MaintenanceWorks" id="IRCA_123.0_1">
            <validityStatus>definedByValidityTimeSpec</validityStatus>
            <overallStartTime>{stamp(-600)}</overallStartTime><overallEndTime>{stamp(3600)}</overallEndTime>
            <coordinatesForDisplay><latitude>64.02</latitude><longitude>-21.34</longitude></coordinatesForDisplay>
            <generalPublicComment><comment><values><value lang="is">Vegavinna</value></values></comment></generalPublicComment>
          </situationRecord>
          <situationRecord xsi:type="Accident" id="IRCA_124.0_1">
            <validityStatus>definedByValidityTimeSpec</validityStatus>
            <overallStartTime>{stamp(-3600)}</overallStartTime><overallEndTime>{stamp(-1)}</overallEndTime>
            <coordinatesForDisplay><latitude>64.03</latitude><longitude>-21.35</longitude></coordinatesForDisplay>
          </situationRecord>
        </messageContainer>''')
        rows = feeds._parse_iceland_roads(root, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'construction')
        self.assertEqual(rows[0]['properties']['detail'], 'Vegavinna')
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_iceland_roads(root, NOW + 3600)

    def test_iceland_sensors_join_locations_and_skip_faulty_or_old_readings(self):
        stamp = lambda offset: dt.datetime.fromtimestamp(NOW + offset, dt.timezone.utc).isoformat()
        sites = ET.fromstring('''<messageContainer><measurementSite id="IRCA_MP_1">
          <measurementSiteName><values><value>Hellisheiði</value></values></measurementSiteName>
          <coordinatesForDisplay><latitude>64.02</latitude><longitude>-21.34</longitude></coordinatesForDisplay>
          </measurementSite><measurementSite id="IRCA_MP_2"><coordinatesForDisplay>
          <latitude>64.03</latitude><longitude>-21.35</longitude></coordinatesForDisplay>
          </measurementSite></messageContainer>''')
        data = ET.fromstring(f'''<messageContainer><publicationTime>{stamp(-60)}</publicationTime>
          <siteMeasurements><measurementSiteReference id="IRCA_MP_1"/>
            <measurementTimeDefault><timeValue>{stamp(-300)}</timeValue></measurementTimeDefault>
            <physicalQuantity index="9"><vehicleFlowPer10Minute><vehicleFlowRate>12</vehicleFlowRate>
              </vehicleFlowPer10Minute></physicalQuantity>
            <physicalQuantity index="4"><airTemperature><temperature>6.2</temperature>
              </airTemperature></physicalQuantity>
            <physicalQuantity index="5"><physicalQuantityFault>sensorFault</physicalQuantityFault>
              <roadSurfaceTemperature><temperature>4.0</temperature></roadSurfaceTemperature>
              </physicalQuantity></siteMeasurements>
          <siteMeasurements><measurementSiteReference id="IRCA_MP_2"/>
            <measurementTimeDefault><timeValue>{stamp(-3600)}</timeValue></measurementTimeDefault>
            <physicalQuantity index="4"><airTemperature><temperature>3.0</temperature>
              </airTemperature></physicalQuantity></siteMeasurements>
          </messageContainer>''')
        rows = feeds._parse_iceland_sensors(sites, data, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['detail'], '12 vehicles / 10 min · Air 6.2°C')
        self.assertEqual(rows[0]['geometry']['coordinates'], [-21.34, 64.02])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_iceland_sensors(sites, data, NOW + 3600)

    def test_iceland_road_conditions_join_current_sections_only(self):
        stamp = lambda offset: dt.datetime.fromtimestamp(NOW + offset, dt.timezone.utc).isoformat()
        x, y = feeds.Transformer.from_crs('EPSG:4326', 'EPSG:3057', always_xy=True).transform(-21.34, 64.02)
        sections = ET.fromstring(f'''<messageContainer><predefinedLocationReference
          id="IRCA_PredefinedLocation_segments_123">
          <predefinedLocationGroupName><values><value>Hellisheiði</value></values>
          </predefinedLocationGroupName><gmlLineString srsName="http://www.opengis.net/gml/srs/epsg.xml#3057">
          <posList>{x} {y} {x + 100} {y + 100}</posList></gmlLineString>
          </predefinedLocationReference></messageContainer>''')
        conditions = ET.fromstring(f'''<messageContainer><publicationTime>{stamp(-60)}</publicationTime>
          <situationRecord id="IRCA_ROADCONDITIONS_123_1"><validityStatus>
          definedByValidityTimeSpec</validityStatus><overallStartTime>{stamp(-300)}</overallStartTime>
          <predefinedLocationReference id="IRCA_PredefinedLocation_segments_123"/>
          <roadOrCarriagewayOrLaneManagementType>roadClosed</roadOrCarriagewayOrLaneManagementType>
          <generalPublicComment><comment><values><value lang="is">Fært fjallabílum</value>
          <value lang="en">Mountain vehicles</value></values></comment></generalPublicComment>
          </situationRecord><situationRecord id="IRCA_ROADCONDITIONS_125_1">
          <validityStatus>active</validityStatus><overallStartTime>{stamp(-300)}</overallStartTime>
          <predefinedLocationReference id="IRCA_PredefinedLocation_segments_123"/>
          <roadOrCarriagewayOrLaneManagementType>roadClosed</roadOrCarriagewayOrLaneManagementType>
          <generalPublicComment><comment><values><value lang="en">Easily passable</value>
          </values></comment></generalPublicComment></situationRecord>
          <situationRecord id="IRCA_ROADCONDITIONS_124_1">
          <validityStatus>definedByValidityTimeSpec</validityStatus>
          <overallStartTime>{stamp(-600)}</overallStartTime><overallEndTime>{stamp(-1)}</overallEndTime>
          <predefinedLocationReference id="IRCA_PredefinedLocation_segments_123"/>
          <poorEnvironmentType>fog</poorEnvironmentType></situationRecord></messageContainer>''')
        rows = feeds._parse_iceland_road_conditions(sections, conditions, NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['properties']['title'], 'Mountain vehicles · Hellisheiði')
        self.assertEqual(rows[1]['properties']['title'], 'Easily passable · Hellisheiði')
        self.assertIn('Approximate section location', rows[0]['properties']['detail'])
        self.assertAlmostEqual(rows[0]['geometry']['coordinates'][0], -21.34, places=2)
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_iceland_road_conditions(sections, conditions, NOW + 3600)


    def test_tii_cameras_only_publish_active_public_stills(self):
        rows = [
            {'id': 91, 'active': True, 'public': True, 'name': 'Reaghstown',
             'location': {'latitude': 53.929722, 'longitude': -6.648056, 'routeId': 'M2/N2'},
             'views': [{'type': 'STILL_IMAGE', 'url': 'https://irecam.carsprogram.org/Vaisala/1681_cam1.jpeg'}]},
            {'id': 92, 'active': False, 'public': True, 'location': {'latitude': 53.9, 'longitude': -6.6},
             'views': [{'type': 'STILL_IMAGE', 'url': 'https://irecam.carsprogram.org/Vaisala/1682_cam1.jpeg'}]},
            {'id': 93, 'active': True, 'public': True, 'location': {'latitude': 53.9, 'longitude': -6.6},
             'views': [{'type': 'STILL_IMAGE', 'url': 'https://example.com/camera.jpeg'}]},
        ]
        previous = dict(feeds._TII_CAMERA_CACHE)
        try:
            feeds._TII_CAMERA_CACHE['until'] = 0
            with patch.object(feeds, '_get_json', return_value=rows), \
                    patch.object(feeds, '_tii_unavailable_cameras', return_value=set()):
                features = feeds._tii_cameras()
            self.assertEqual(len(features), 1)
            self.assertEqual(features[0]['properties']['snapshot_url'], '/ireland-camera/91')
        finally:
            feeds._TII_CAMERA_CACHE.update(previous)

    def test_tii_camera_health_omits_stale_stills(self):
        class Response:
            def __init__(self, url):
                self.url = url
                self.headers = {'Content-Type': 'image/jpeg',
                                'Last-Modified': ('Sun, 27 Sep 2026 12:55:00 GMT' if url.endswith('/91.jpeg')
                                                  else 'Sun, 27 Sep 2026 10:00:00 GMT')}
            def __enter__(self): return self
            def __exit__(self, *_): return False
        catalog = {str(number): ({'id': number}, f'https://irecam.carsprogram.org/{number}.jpeg')
                   for number in (91, 92)}
        previous = dict(feeds._TII_CAMERA_HEALTH)
        now = dt.datetime(2026, 9, 27, 13, tzinfo=dt.timezone.utc).timestamp()
        try:
            feeds._TII_CAMERA_HEALTH['until'] = 0
            with patch.object(feeds.time, 'time', return_value=now), \
                    patch.object(feeds.urllib.request, 'urlopen', side_effect=lambda req, timeout: Response(req.full_url)):
                self.assertEqual(feeds._tii_unavailable_cameras(catalog), {'92'})
        finally:
            feeds._TII_CAMERA_HEALTH.update(previous)

    def test_tii_camera_snapshot_rejects_stale_image(self):
        class Response(io.BytesIO):
            url = 'https://irecam.carsprogram.org/Vaisala/1681_cam1.jpeg'
            def __init__(self, last_modified):
                super().__init__(b'\xff\xd8\xffjpeg')
                self.headers = {'Last-Modified': last_modified}
        catalog = {'91': ({'id': 91}, 'https://irecam.carsprogram.org/Vaisala/1681_cam1.jpeg')}
        now = dt.datetime(2026, 9, 27, 13, tzinfo=dt.timezone.utc).timestamp()
        with patch.object(feeds, '_tii_camera_catalog', return_value=catalog), \
                patch.object(feeds.time, 'time', return_value=now), \
                patch.object(feeds.urllib.request, 'urlopen', return_value=Response('Sun, 27 Sep 2026 12:55:00 GMT')):
            self.assertEqual(feeds.tii_camera_snapshot('91')[1], 'image/jpeg')
        with patch.object(feeds, '_tii_camera_catalog', return_value=catalog), \
                patch.object(feeds.time, 'time', return_value=now), \
                patch.object(feeds.urllib.request, 'urlopen', return_value=Response('Sun, 27 Sep 2026 11:00:00 GMT')):
            with self.assertRaises(FileNotFoundError):
                feeds.tii_camera_snapshot('91')

    def test_tii_events_require_current_window(self):
        now = dt.datetime(2026, 9, 27, 13, tzinfo=dt.timezone.utc).timestamp()
        def row(event_id, title, start):
            return {'id': event_id, 'active': True,
                    'location': {'primaryPoint': {'lat': 53.3, 'lon': -6.2}},
                    'eventDescription': {'descriptionHeader': title,
                                         'headlinePhrase': 'Roadworks',
                                         'locationDescription': 'M50 near Dublin'},
                    'beginTime': {'time': start * 1000},
                    'endTime': {'time': (now + 3600) * 1000},
                    'updateTime': {'time': now * 1000}}
        features = feeds._parse_tii_events([
            row('IRE-26-09-1', 'Current roadworks', now - 600),
            row('IRE-26-09-2', 'Tomorrow roadworks', now + 86400),
        ], now)
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['properties']['layer'], 'construction')

    def test_tii_signs_use_only_recent_display_images(self):
        def row(sign_id, image_url):
            return {'id': f'irelanddot*{sign_id}', 'name': 'M50 sign',
                    'status': 'DISPLAYING_MESSAGE',
                    'properties': {'signType': 'VMS_IMAGE'},
                    'location': {'latitude': 53.3, 'longitude': -6.2},
                    'display': {'pages': [{'lines': [image_url]}]}}
        features = feeds._parse_tii_signs([
            row('M50-ONE', 'https://crc-public-eu-west-1-s3.s3.eu-west-1.amazonaws.com/ire/prod/signs/fresh.PNG'),
            row('M50-TWO', 'https://crc-public-eu-west-1-s3.s3.eu-west-1.amazonaws.com/ire/prod/signs/old.PNG'),
        ], image_loader=lambda url: ('YWJj', NOW) if url.endswith('fresh.PNG') else None)
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['properties']['image_data'], 'YWJj')

    def test_trafficwatch_ni_maps_cameras_current_works_and_readable_signs(self):
        now = dt.datetime(2026, 9, 27, 11, tzinfo=dt.timezone.utc).timestamp()
        data = {
            'CCTV_CAMERAS': [
                {'id': '155', 'latitude': 54.6, 'longitude': -5.93, 'summary': 'Peters Hill'},
                {'id': 'bad', 'latitude': 54.6, 'longitude': -5.93, 'summary': 'Invalid'},
            ],
            'ROAD_WORKS': [
                {'id': '123', 'latitude': 54.7, 'longitude': -6.1, 'summary': 'Lane closure',
                 'details': {'start': 'Sat, 26 Sep 2026', 'end': 'Mon, 28 Sep 2026',
                             'description': '<p>Bridge repairs</p>'}},
                {'id': '124', 'latitude': 54.7, 'longitude': -6.1, 'summary': 'Future works',
                 'details': {'start': 'Mon, 28 Sep 2026', 'end': 'Tue, 29 Sep 2026'}},
            ],
            'MESSAGE_SIGNS': [
                {'latitude': 54.6, 'longitude': -5.9, 'summary': 'M2/0101M',
                 'lastUpdated': 'Sun, 27 Sep 2026 11:55', 'details': {'message': 'SLOW DOWN\n'}},
                {'latitude': 54.6, 'longitude': -5.9, 'summary': 'M2/0102M',
                 'lastUpdated': 'Sun, 27 Sep 2026 11:55', 'details': {'message': 'Sign not set'}},
                {'latitude': 54.6, 'longitude': -5.9, 'summary': 'M2/0103M',
                 'lastUpdated': 'Sun, 27 Sep 2026 10:00', 'details': {'message': 'OLD TEXT'}},
                {'latitude': 53.3, 'longitude': -6.2, 'summary': 'M50 Dublin',
                 'lastUpdated': 'Sun, 27 Sep 2026 11:55', 'details': {'message': 'ROI SIGN'}},
            ],
        }
        rows = feeds._parse_trafficwatch(data, now)
        self.assertEqual([row['properties']['layer'] for row in rows],
                         ['cameras', 'construction', 'signs'])
        self.assertEqual(rows[0]['properties']['snapshot_url'], '/northern-ireland-camera/155')
        self.assertIn('Bridge repairs', rows[1]['properties']['detail'])
        self.assertEqual(rows[2]['properties']['detail'], 'SLOW DOWN')

    def test_trafficwatch_camera_id_is_validated_before_fetch(self):
        with self.assertRaises(ValueError):
            feeds.northern_ireland_camera_snapshot('https://example.org/')

    def test_south_tyrol_roads_require_current_publication_and_active_record(self):
        now = dt.datetime(2026, 9, 27, 11, 30, tzinfo=dt.timezone.utc).timestamp()
        root = ET.fromstring('''<d2LogicalModel xmlns="http://datex2.eu/schema/2/2_0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><payloadPublication>
          <publicationTime>2026-09-27T11:29:00Z</publicationTime>
          <situation id="work-1"><situationRecord xsi:type="RoadOrCarriagewayOrLaneManagement">
            <validity><validityStatus>active</validityStatus><validityTimeSpecification>
              <overallStartTime>2026-09-27T08:00:00Z</overallStartTime></validityTimeSpecification></validity>
            <generalPublicComment><comment><values><value lang="it">Cantiere stradale</value>
              </values></comment></generalPublicComment><groupOfLocations><locationContainedInGroup>
              <pointByCoordinates><pointCoordinates><latitude>46.5</latitude>
              <longitude>11.3</longitude></pointCoordinates></pointByCoordinates>
              </locationContainedInGroup></groupOfLocations></situationRecord></situation>
          <situation id="future-1"><situationRecord xsi:type="PublicEvent">
            <validity><validityStatus>definedByValidityTimeSpec</validityStatus>
              <validityTimeSpecification><overallStartTime>2026-09-28T08:00:00Z</overallStartTime>
              </validityTimeSpecification></validity>
            <pointByCoordinates><pointCoordinates><latitude>46.6</latitude><longitude>11.4</longitude>
              </pointCoordinates></pointByCoordinates></situationRecord></situation>
          </payloadPublication></d2LogicalModel>''')
        rows = feeds._parse_south_tyrol_roads(root, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'construction')
        self.assertEqual(rows[0]['geometry']['coordinates'], [11.3, 46.5])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_south_tyrol_roads(root, now + 21 * 60)

    def test_madrid_incidents_use_local_time_and_skip_future_work(self):
        now = dt.datetime(2026, 9, 27, 11, tzinfo=dt.timezone.utc).timestamp()
        root = ET.fromstring('''<Incidencias>
          <Incidencia><id_incidencia>51</id_incidencia><incid_estado>1</incid_estado>
            <es_obras>S</es_obras><nom_tipo_incidencia>Roadworks</nom_tipo_incidencia>
            <descripcion>Lane closed</descripcion><longitud>-3.7</longitud><latitud>40.4</latitud>
            <fh_inicio>2026-09-27T12:00:00.0000000</fh_inicio>
            <fh_final>2026-09-28T13:00:00.0000000</fh_final></Incidencia>
          <Incidencia><id_incidencia>52</id_incidencia><incid_estado>4</incid_estado>
            <es_obras>N</es_obras><longitud>-3.71</longitud><latitud>40.41</latitud>
            <fh_inicio>2026-09-28T12:00:00.0000000</fh_inicio>
            <fh_final>2026-09-29T13:00:00.0000000</fh_final></Incidencia>
        </Incidencias>''')
        rows = feeds._parse_madrid_incidents(root, now, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'construction')

    def test_bratislava_roadworks_include_only_current_vehicle_restrictions(self):
        now = dt.datetime(2026, 9, 27, 22, tzinfo=dt.timezone.utc).timestamp()
        def item(work_id, **overrides):
            props = {
                'OBJECTID': work_id, 'zobrazovanie': 'Zobrazovat',
                'uzavierka': 'čiastočná', 'vplyv_obmedzenia': 'Auta,Verejna_doprava',
                'datum_vzniku': (now - 86400) * 1000,
                'potvrdeny_termin_realizacie': None,
                'termin_finalnej_upravy': (now + 86400) * 1000,
                'adresa_rozkopavky': 'Košická ul.', 'predmet_nadpis': 'Utility works',
            }
            props.update(overrides)
            return {'type': 'Feature', 'geometry': {'type': 'Point',
                    'coordinates': [17.13, 48.15]}, 'properties': props}
        payload = {'type': 'FeatureCollection', 'features': [
            item(1), item(2, vplyv_obmedzenia='Chodci'),
            item(3, datum_vzniku=(now + 3600) * 1000),
            item(4, termin_finalnej_upravy=(now - 3600) * 1000),
            item(5, zobrazovanie='Nezobrazovat'), item(6, uzavierka='žiadna'),
        ]}
        rows = feeds._parse_bratislava_roadworks(payload, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['key'], 'sk:bratislava:works:1')
        self.assertIn('Scheduled permit; not confirmed live', rows[0]['properties']['detail'])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            feeds._parse_bratislava_roadworks({**payload, 'exceededTransferLimit': True}, now)

    def test_madrid_cameras_use_same_origin_image_proxy(self):
        root = ET.fromstring('''<kml xmlns="http://earth.google.com/kml/2.2"><Document>
          <Placemark><ExtendedData><Data name="Numero"><Value>06303</Value></Data>
            <Data name="Nombre"><Value>Plaza de Castilla</Value></Data></ExtendedData>
            <Point><coordinates>-3.68894,40.46606,10</coordinates></Point></Placemark>
        </Document></kml>''')
        rows = feeds._parse_madrid_cameras(root, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['snapshot_url'], '/madrid-camera/06303')
        self.assertEqual(rows[0]['properties']['snapshot_fallback_url'],
                         'https://informo.madrid.es/cameras/Camara06303.jpg')

    def test_madrid_camera_health_checks_image_body(self):
        class Response(io.BytesIO):
            def __init__(self, url, body):
                super().__init__(body)
                self.url = url
                self.headers = {'Content-Type': 'image/jpeg', 'Content-Length': '120000',
                                'Last-Modified': email.utils.formatdate(NOW - 120, usegmt=True)}

        cameras = [{'properties': {'key': f'es:madrid:camera:{camera_id}'}}
                   for camera_id in ('06303', '06304')]
        previous = dict(feeds._MADRID_CAMERA_HEALTH)
        try:
            feeds._MADRID_CAMERA_HEALTH.update(until=0, unavailable=set())
            def image(request, timeout):
                self.assertNotEqual(request.get_method(), 'HEAD')
                body = b'not a jpeg' if request.full_url.endswith('06304.jpg') else b'\xff\xd8\xffvalid'
                return Response(request.full_url, body)
            with patch.object(feeds.time, 'time', return_value=NOW), \
                    patch.object(feeds.urllib.request, 'urlopen', side_effect=image):
                self.assertEqual(feeds._madrid_unavailable_cameras(cameras),
                                 {'es:madrid:camera:06304'})
        finally:
            feeds._MADRID_CAMERA_HEALTH.clear()
            feeds._MADRID_CAMERA_HEALTH.update(previous)

    def test_lyon_cameras_require_fresh_official_stills(self):
        observed = dt.datetime.fromtimestamp(NOW - 60, dt.timezone.utc).isoformat()
        def camera(camera_id, url=None, updated=observed):
            return {'geometry': {'coordinates': [4.81, 45.77]}, 'properties': {
                'numeromaintenance': camera_id, 'nom': 'Lyon Centre',
                'libellelong': 'Pont Clemenceau', 'last_update': updated,
                'url': url or feeds._lyon_camera_url(camera_id)}}
        payload = {'features': [
            camera('CWL9018'),
            camera('CWL9019', 'https://other.example/CWL9019.JPG'),
            camera('CWL9020', updated='2020-01-01T00:00:00+00:00')]}
        rows = feeds._parse_lyon_cameras(payload, NOW)
        self.assertEqual([row['properties']['key'] for row in rows],
                         ['fr:lyon:camera:CWL9018'])
        self.assertEqual(rows[0]['properties']['snapshot_url'], '/lyon-camera/CWL9018')
        with self.assertRaisesRegex(ValueError, 'fresh'):
            feeds._parse_lyon_cameras({'features': payload['features'][1:]}, NOW)
        with self.assertRaises(ValueError):
            feeds.lyon_camera_snapshot('../CWL9018')

        class Response(io.BytesIO):
            url = feeds._lyon_camera_url('CWL9018')
            headers = {'Content-Type': 'image/jpeg'}
        with patch.object(feeds.urllib.request, 'urlopen', return_value=Response(b'\xff\xd8\xffimage')):
            self.assertEqual(feeds.lyon_camera_snapshot('CWL9018')[1], 'image/jpeg')

    def test_madrid_camera_proxy_validates_id_and_image(self):
        class Response(io.BytesIO):
            url = 'https://informo.madrid.es/cameras/Camara06303.jpg'
            headers = {'Content-Type': 'image/jpeg', 'Content-Length': '100000',
                       'Last-Modified': 'Sat, 26 Sep 2026 18:00:00 GMT'}
        with self.assertRaises(ValueError):
            feeds.madrid_camera_snapshot('../bad')
        with patch.object(feeds.time, 'time', return_value=NOW), \
                patch.object(feeds.urllib.request, 'urlopen', return_value=Response(b'\xff\xd8\xffimage')):
            self.assertEqual(feeds.madrid_camera_snapshot('06303')[1], 'image/jpeg')
        with patch.object(feeds.time, 'time', return_value=NOW), \
                patch.object(feeds.urllib.request, 'urlopen',
                             side_effect=[TimeoutError(), Response(b'\xff\xd8\xffimage')]) as open_image:
            self.assertEqual(feeds.madrid_camera_snapshot('06303')[1], 'image/jpeg')
            self.assertEqual(open_image.call_count, 2)
        with patch.object(feeds.time, 'time', return_value=NOW), \
                patch.object(feeds.urllib.request, 'urlopen', return_value=Response(b'not an image')):
            with self.assertRaises(ValueError):
                feeds.madrid_camera_snapshot('06303')
        placeholder = Response(b'\xff\xd8\xffimage')
        placeholder.headers = dict(Response.headers, **{'Content-Length': '17803'})
        with patch.object(feeds.time, 'time', return_value=NOW), \
                patch.object(feeds.urllib.request, 'urlopen', return_value=placeholder):
            with self.assertRaises(FileNotFoundError):
                feeds.madrid_camera_snapshot('09305')

    def test_madrid_camera_catalog_hides_offline_and_stale_stills(self):
        class Response:
            def __init__(self, url):
                self.url = url
                camera_id = url.rsplit('Camara', 1)[-1].split('.')[0]
                self.headers = {'Content-Type': 'image/jpeg',
                                'Content-Length': '17803' if camera_id == '09305' else '100000',
                                'Last-Modified': ('Fri, 25 Sep 2026 18:00:00 GMT' if camera_id == '04301'
                                                  else 'Sat, 26 Sep 2026 18:00:00 GMT')}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, size): return b'\xff\xd8\xff'[:size]

        cameras = [feeds._feature([-3.7, 40.4], {
            'key': f'es:madrid:camera:{camera_id}', 'layer': 'cameras',
        }) for camera_id in ('06303', '09305', '04301', '07313')]
        previous = dict(feeds._MADRID_CAMERA_HEALTH)
        def load(request, timeout):
            if '07313' in request.full_url:
                raise FileNotFoundError('missing camera')
            return Response(request.full_url)
        try:
            feeds._MADRID_CAMERA_HEALTH['until'] = 0
            with patch.object(feeds.time, 'time', return_value=NOW), \
                    patch.object(feeds.urllib.request, 'urlopen', side_effect=load):
                bad = feeds._madrid_unavailable_cameras(cameras)
            self.assertEqual(bad, {'es:madrid:camera:09305', 'es:madrid:camera:04301',
                                   'es:madrid:camera:07313'})
        finally:
            feeds._MADRID_CAMERA_HEALTH.update(previous)

    def test_failed_madrid_still_is_removed_from_next_map_response(self):
        camera = feeds._feature([-3.7, 40.42], {
            'key': 'es:madrid:camera:01315', 'layer': 'cameras',
            'snapshot_url': '/madrid-camera/01315',
        })
        previous = dict(feeds._MADRID_CAMERA_HEALTH)
        try:
            feeds._MADRID_CAMERA_HEALTH.update(until=NOW + 900, unavailable=set())
            with patch.object(feeds.urllib.request, 'urlopen', side_effect=FileNotFoundError):
                with self.assertRaises(FileNotFoundError):
                    feeds.madrid_camera_snapshot('01315')
            with patch.object(feeds, '_snapshot', return_value={
                    'sources': {'es_madrid_cameras': [camera]}, 'errors': []}), \
                    patch.object(feeds, '_dgt_unavailable_cameras', return_value=set()):
                self.assertEqual(feeds.road_snapshot('cameras', (-3.8, 40.3, -3.6, 40.5))['features'], [])
        finally:
            feeds._MADRID_CAMERA_HEALTH.clear()
            feeds._MADRID_CAMERA_HEALTH.update(previous)

    def test_madrid_signs_join_locations_and_preserve_alternating_phases(self):
        locations = [{'nombre': 'CPMV10051', 'longitud': '-3.7', 'latitud': '40.4'}]
        root = ET.fromstring('''<MESSAGE><HEAD><RESULT>OK</RESULT></HEAD><BODY>
          <DEVICES><VMS_ID>CPMV10051</VMS_ID><VMS_DESCRIPTION>M30 panel</VMS_DESCRIPTION></DEVICES>
          <LINES><VMS_ID>CPMV10051</VMS_ID><PHASE_NUMBER>1</PHASE_NUMBER>
            <LINE_NUMBER>2</LINE_NUMBER><LINE>CLOSED</LINE></LINES>
          <LINES><VMS_ID>CPMV10051</VMS_ID><PHASE_NUMBER>1</PHASE_NUMBER>
            <LINE_NUMBER>1</LINE_NUMBER><LINE>ROAD</LINE></LINES>
          <LINES><VMS_ID>CPMV10051</VMS_ID><PHASE_NUMBER>2</PHASE_NUMBER>
            <LINE_NUMBER>1</LINE_NUMBER><LINE>DETOUR</LINE></LINES>
        </BODY></MESSAGE>''')
        rows = feeds._parse_madrid_signs(locations, root, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['detail'], 'ROAD / CLOSED  •  DETOUR')
        self.assertEqual(rows[0]['geometry']['coordinates'], [-3.7, 40.4])

    def test_poland_road_events_require_fresh_publication_and_active_dates(self):
        now = dt.datetime(2026, 9, 27, 10, 50, tzinfo=dt.timezone.utc).timestamp()
        root = ET.fromstring('''<utrudnienia gen="2026-09-27T12:48:00+0200">
          <utr><typ>U</typ><nr_drogi>A1</nr_drogi><geo_lat>54.13</geo_lat><geo_long>18.67</geo_long>
            <nazwa_odcinka>Rusocin</nazwa_odcinka><objazd>Lane works</objazd><rodzaj><poz>U33</poz></rodzaj>
            <data_powstania>2026-09-27T08:00:00+0200</data_powstania>
            <data_likwidacji>2026-09-28T18:00:00+0200</data_likwidacji></utr>
          <utr><typ>W</typ><nr_drogi>A4</nr_drogi><geo_lat>50.11</geo_lat><geo_long>21.68</geo_long>
            <objazd>Collision</objazd><data_powstania>2026-09-27T12:15:00+0200</data_powstania>
            <data_likwidacji>2026-09-27T14:15:00+0200</data_likwidacji></utr>
          <utr><typ>U</typ><nr_drogi>S7</nr_drogi><geo_lat>52.1</geo_lat><geo_long>20.1</geo_long>
            <data_powstania>2026-09-28T08:00:00+0200</data_powstania>
            <data_likwidacji>2026-09-29T18:00:00+0200</data_likwidacji></utr>
          <utr><typ>I</typ><nr_drogi>S3</nr_drogi><geo_lat>52.1</geo_lat><geo_long>15.2</geo_long>
            <data_powstania>2026-09-26T08:00:00+0200</data_powstania>
            <data_likwidacji>2026-09-26T18:00:00+0200</data_likwidacji></utr>
        </utrudnienia>''')
        rows = feeds._parse_poland_roads(root, now)
        self.assertEqual([row['properties']['layer'] for row in rows], ['construction', 'incidents'])
        self.assertEqual(rows[0]['geometry']['coordinates'], [18.67, 54.13])
        self.assertIn('Collision', rows[1]['properties']['detail'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_poland_roads(root, now + 21 * 60)

    def test_sct_roadworks_and_incidents_are_separate(self):
        root = ET.fromstring('''<FeatureCollection><featureMember><event>
          <geom><Point><coordinates>2.16,41.67</coordinates></Point></geom>
          <identificador>151362101</identificador><carretera>C-59</carretera>
          <descripcio_tipus>Retenció</descripcio_tipus><descripcio>Traffic delay</descripcio>
          </event></featureMember><featureMember><event>
          <geom><Point><coordinates>1.42,41.36</coordinates></Point></geom>
          <identificador>151360903</identificador><carretera>C-51</carretera>
          <descripcio_tipus>Obres</descripcio_tipus><causa>Maintenance</causa>
          </event></featureMember></FeatureCollection>''')
        rows = feeds._parse_sct_incidents(root)
        self.assertEqual([row['properties']['layer'] for row in rows], ['incidents', 'construction'])
        self.assertEqual(rows[0]['properties']['key'], 'es:sct:incident:151362101')

    def test_sct_cameras_proxy_only_authority_images_and_deduplicate(self):
        root = ET.fromstring('''<FeatureCollection><featureMember><camera>
          <geom><Point><coordinates>2.18,41.46</coordinates></Point></geom>
          <carretera>C-58</carretera><link>http://mct.gencat.cat/mct2bo/RenderService?sctidcam=nc87.gif</link>
          </camera></featureMember><featureMember><camera>
          <geom><Point><coordinates>2.18,41.46</coordinates></Point></geom>
          <link>http://mct.gencat.cat/mct2bo/RenderService?sctidcam=nc87.gif</link>
          </camera></featureMember><featureMember><camera>
          <geom><Point><coordinates>2.20,41.47</coordinates></Point></geom>
          <link>https://example.com/camera.jpg</link>
          </camera></featureMember></FeatureCollection>''')
        rows = feeds._parse_sct_cameras(root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['snapshot_url'], '/catalonia-camera/nc87')

    def test_dgt_cameras_require_current_catalog_and_official_image(self):
        now = dt.datetime(2026, 9, 27, 9, 40, tzinfo=dt.timezone.utc).timestamp()
        root = ET.fromstring('''<payload><publicationTime>2026-09-27T09:00:00Z</publicationTime>
          <device id="62"><typeOfDevice>camera</typeOfDevice><pointCoordinates>
            <latitude>42.304092</latitude><longitude>-0.4282263</longitude>
          </pointCoordinates><roadName>A-23</roadName><province>HUESCA</province>
            <deviceUrl>https://etraffic.dgt.es/camarasEtraffic/168408.jpg</deviceUrl></device>
          <device id="63"><typeOfDevice>camera</typeOfDevice><pointCoordinates>
            <latitude>42.3</latitude><longitude>-0.4</longitude>
          </pointCoordinates><deviceUrl>https://other.example/camera.jpg</deviceUrl></device>
        </payload>''')
        rows = feeds._parse_dgt_cameras(root, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['snapshot_url'],
                         '/dgt-camera/168408')
        self.assertEqual(rows[0]['properties']['snapshot_fallback_url'],
                         'https://etraffic.dgt.es/camarasEtraffic/168408.jpg')
        self.assertEqual(rows[0]['geometry']['coordinates'], [-0.4282263, 42.304092])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_dgt_cameras(root, now + 4 * 3600)

    def test_dgt_visible_cameras_omit_placeholders_stale_and_invalid_images(self):
        class Response:
            def __init__(self, url):
                self.url = url
                camera_id = url.rsplit('/', 1)[-1]
                self.headers = {'Content-Type': 'image/jpeg',
                                'Content-Length': {'923.jpg': '32634', '1103.jpg': '9422'}.get(
                                    camera_id, '125668'),
                                'Last-Modified': email.utils.formatdate(
                                    NOW - (3600 if camera_id == '1104.jpg' else 300), usegmt=True)}
            def read(self, size):
                return b'bad' if self.url.endswith('/1105.jpg') else b'\xff\xd8\xff'
            def __enter__(self): return self
            def __exit__(self, *_): return False
        items = [feeds._feature([-4.13, 40.7], {
            'key': f'es:dgt:camera:{number + 10000}', 'layer': 'cameras',
            'snapshot_url': f'/dgt-camera/{number}',
        }) for number in (923, 1103, 1104, 1105, 929)]
        previous = dict(feeds._DGT_CAMERA_HEALTH)
        try:
            feeds._DGT_CAMERA_HEALTH.clear()
            with patch.object(feeds.time, 'time', return_value=NOW), \
                    patch.object(feeds.urllib.request, 'urlopen', side_effect=lambda req, timeout: Response(req.full_url)):
                self.assertEqual(feeds._dgt_unavailable_cameras(items),
                                 {item['properties']['key'] for item in items[:4]})
            # A second map request uses the health cache without another upstream request.
            with patch.object(feeds.time, 'time', return_value=NOW), \
                    patch.object(feeds.urllib.request, 'urlopen', side_effect=AssertionError):
                self.assertEqual(feeds._dgt_unavailable_cameras(items),
                                 {item['properties']['key'] for item in items[:4]})
            with patch.object(feeds, '_snapshot', return_value={
                    'sources': {'es_dgt_cameras': items}, 'errors': []}), \
                    patch.object(feeds.time, 'time', return_value=NOW):
                result = feeds.road_snapshot('cameras', (-4.2, 40.6, -4.0, 40.8))
                self.assertEqual([item['properties']['key'] for item in result['features']],
                                 [items[-1]['properties']['key']])
        finally:
            feeds._DGT_CAMERA_HEALTH.clear()
            feeds._DGT_CAMERA_HEALTH.update(previous)

    def test_dgt_camera_proxy_validates_image_and_freshness(self):
        class Response(io.BytesIO):
            url = 'https://etraffic.dgt.es/camarasEtraffic/597.jpg'
            headers = {'Content-Type': 'image/jpeg', 'Content-Length': '125380',
                       'Last-Modified': email.utils.formatdate(NOW - 300, usegmt=True)}
        with self.assertRaises(ValueError):
            feeds.dgt_camera_snapshot('../bad')
        with patch.object(feeds.time, 'time', return_value=NOW), \
                patch.object(feeds.urllib.request, 'urlopen', return_value=Response(b'\xff\xd8\xffimage')):
            self.assertEqual(feeds.dgt_camera_snapshot('597'), (b'\xff\xd8\xffimage', 'image/jpeg'))
        with patch.object(feeds.time, 'time', return_value=NOW), \
                patch.object(feeds.urllib.request, 'urlopen',
                             side_effect=[TimeoutError(), Response(b'\xff\xd8\xffimage')]) as open_image:
            self.assertEqual(feeds.dgt_camera_snapshot('597')[1], 'image/jpeg')
            self.assertEqual(open_image.call_count, 2)
        placeholder = Response(b'\xff\xd8\xffimage')
        placeholder.headers = dict(Response.headers, **{'Content-Length': '32634'})
        with patch.object(feeds.time, 'time', return_value=NOW), \
                patch.object(feeds.urllib.request, 'urlopen', return_value=placeholder):
            with self.assertRaises(FileNotFoundError):
                feeds.dgt_camera_snapshot('597')

    def test_dgt_incidents_separate_active_works_from_road_events(self):
        now = dt.datetime(2026, 9, 27, 9, 40, tzinfo=dt.timezone.utc).timestamp()
        root = ET.fromstring('''<payload><publicationTime>2026-09-27T09:39:00Z</publicationTime>
          <situation><situationRecord id="work-1"><validityStatus>active</validityStatus>
            <overallStartTime>2026-09-27T08:00:00Z</overallStartTime>
            <causeType>roadMaintenance</causeType><roadName>N-400</roadName>
            <pointCoordinates><latitude>39.99</latitude><longitude>-3.60</longitude></pointCoordinates>
          </situationRecord><situationRecord id="crash-1"><validityStatus>active</validityStatus>
            <causeType>accident</causeType><roadName>A-6</roadName>
            <pointCoordinates><latitude>40.2</latitude><longitude>-4.1</longitude></pointCoordinates>
          </situationRecord><situationRecord id="ended"><validityStatus>active</validityStatus>
            <overallEndTime>2026-09-27T09:00:00Z</overallEndTime>
            <causeType>accident</causeType><pointCoordinates>
              <latitude>40.3</latitude><longitude>-4.2</longitude>
            </pointCoordinates></situationRecord></situation></payload>''')
        rows = feeds._parse_dgt_incidents(root, now)
        self.assertEqual([row['properties']['layer'] for row in rows], ['construction', 'incidents'])
        self.assertEqual(rows[0]['properties']['key'], 'es:dgt:incident:work-1')
        self.assertIn('Crash', rows[1]['properties']['title'])

    def test_dgt_signs_join_current_display_with_device_location(self):
        now = dt.datetime(2026, 9, 27, 9, 40, tzinfo=dt.timezone.utc).timestamp()
        locations = ET.fromstring('''<payload><publicationTime>2026-09-27T09:00:00Z</publicationTime>
          <device id="61441"><typeOfDevice>vms</typeOfDevice><roadName>M-607</roadName>
            <pointCoordinates><latitude>40.5</latitude><longitude>-3.7</longitude></pointCoordinates>
          </device></payload>''')
        statuses = ET.fromstring('''<payload><publicationTime>2026-09-27T09:39:00Z</publicationTime>
          <vmsControllerStatus><vmsControllerReference id="61441"/><vmsMessage><vmsMessage>
            <timeLastSet>2026-09-27T09:35:00Z</timeLastSet>
            <textLine><textLine><textLine>VELOCIDAD CONTROLADA POR RADAR</textLine></textLine></textLine>
          </vmsMessage></vmsMessage></vmsControllerStatus></payload>''')
        rows = feeds._parse_dgt_signs(locations, statuses, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['key'], 'es:dgt:sign:61441')
        self.assertIn('VELOCIDAD CONTROLADA POR RADAR', rows[0]['properties']['detail'])
        stale = ET.fromstring(ET.tostring(statuses).replace(
            b'2026-09-27T09:35:00Z', b'2026-09-26T08:35:00Z'))
        self.assertEqual(feeds._parse_dgt_signs(locations, stale, now), [])

    def test_fintraffic_traffic_and_weather_sensors_require_recent_readings(self):
        now = dt.datetime(2026, 9, 27, 9, 30, tzinfo=dt.timezone.utc).timestamp()
        metadata = {'type': 'FeatureCollection', 'features': [
            {'geometry': {'type': 'Point', 'coordinates': [24.64, 60.22]}, 'properties': {
                'id': 20002, 'name': 'vt1_Espoo_Hirvisuo', 'collectionStatus': 'GATHERING'}},
            {'geometry': {'type': 'Point', 'coordinates': [25.0, 60.3]}, 'properties': {
                'id': 20003, 'collectionStatus': 'REMOVED_TEMPORARILY'}}]}
        readings = {'dataUpdatedTime': '2026-09-27T09:29:00Z', 'stations': [
            {'id': 20002, 'sensorValues': [
                {'id': 5122, 'value': 97, 'measuredTime': '2026-09-27T09:27:00Z'},
                {'id': 5116, 'value': 1848, 'measuredTime': '2026-09-27T09:27:00Z'},
                {'id': 5125, 'value': 88, 'measuredTime': '2026-09-27T09:26:00Z'},
                {'id': 5119, 'value': 936, 'measuredTime': '2026-09-27T09:26:00Z'}]},
            {'id': 20003, 'sensorValues': [
                {'id': 5122, 'value': 80, 'measuredTime': '2026-09-27T09:27:00Z'}]}]}
        rows = feeds._parse_fintraffic_sensors('tms', metadata, readings, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['key'], 'fi:tms:20002')
        self.assertIn('Direction 1: 97 km/h, 1,848 veh/h', rows[0]['properties']['detail'])
        self.assertIn('Direction 2: 88 km/h, 936 veh/h', rows[0]['properties']['detail'])
        weather = {'dataUpdatedTime': '2026-09-27T09:29:00Z', 'stations': [
            {'id': 20002, 'sensorValues': [
                {'id': 1, 'value': 13.8, 'measuredTime': '2026-09-27T09:25:00Z'},
                {'id': 3, 'value': 20.7, 'measuredTime': '2026-09-27T09:25:00Z'}]}]}
        rows = feeds._parse_fintraffic_sensors('weather', metadata, weather, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['detail'], 'Air 13.8°C · Road 20.7°C')
        weather['stations'][0]['sensorValues'][0]['measuredTime'] = '2026-09-27T08:00:00Z'
        weather['stations'][0]['sensorValues'][1]['measuredTime'] = '2026-09-27T08:00:00Z'
        self.assertEqual(feeds._parse_fintraffic_sensors('weather', metadata, weather, now), [])
        readings['dataUpdatedTime'] = '2026-09-27T08:00:00Z'
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_fintraffic_sensors('tms', metadata, readings, now)

    def test_fintraffic_cameras_use_recent_collected_preset_only(self):
        now = dt.datetime(2026, 9, 27, 9, 30, tzinfo=dt.timezone.utc).timestamp()
        metadata = {'type': 'FeatureCollection', 'features': [
            {'geometry': {'type': 'Point', 'coordinates': [24.95, 60.17]}, 'properties': {
                'id': 'C01503', 'name': 'kt51_Inkoo', 'collectionStatus': 'GATHERING',
                'presets': [{'id': 'C0150301', 'inCollection': True},
                            {'id': 'C0150302', 'inCollection': False}]}},
            {'geometry': {'type': 'Point', 'coordinates': [25.1, 60.2]}, 'properties': {
                'id': 'C01504', 'collectionStatus': 'REMOVED_TEMPORARILY',
                'presets': [{'id': 'C0150401', 'inCollection': True}]}}
        ]}
        observations = {'dataUpdatedTime': '2026-09-27T09:29:00Z', 'stations': [
            {'id': 'C01503', 'presets': [
                {'id': 'C0150301', 'measuredTime': '2026-09-27T09:25:00Z'},
                {'id': 'C0150302', 'measuredTime': '2026-09-27T09:27:00Z'}]},
            {'id': 'C01504', 'presets': [
                {'id': 'C0150401', 'measuredTime': '2026-09-27T09:25:00Z'}]}
        ]}
        rows = feeds._parse_fintraffic_cameras(metadata, observations, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['key'], 'fi:camera:C01503')
        self.assertEqual(rows[0]['properties']['snapshot_url'],
                         'https://weathercam.digitraffic.fi/C0150301.jpg')
        self.assertEqual(rows[0]['properties']['snapshot_refresh_ms'], 600000)
        self.assertEqual(rows[0]['geometry']['coordinates'], [24.95, 60.17])
        observations['dataUpdatedTime'] = '2026-09-27T08:30:00Z'
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_fintraffic_cameras(metadata, observations, now)

    def test_luxembourg_datex_maps_current_events_and_rejects_stale_feed(self):
        now = dt.datetime(2026, 9, 27, 8, 30, tzinfo=dt.timezone.utc).timestamp()
        document = '''<payload xmlns="http://datex2.eu/schema/3/d2Payload"
          xmlns:sit="http://datex2.eu/schema/3/situation"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <publicationTime>2026-09-27T08:25:00Z</publicationTime>
          <sit:situation><sit:situationRecord xsi:type="sit:MaintenanceWorks" id="work-1">
            <sit:validity><sit:overallStartTime>2026-09-27T07:00:00Z</sit:overallStartTime></sit:validity>
            <sit:generalPublicComment><sit:comment><sit:values><sit:value>Bridge repair</sit:value></sit:values></sit:comment></sit:generalPublicComment>
            <sit:locationReference><sit:roadName>A1</sit:roadName><sit:roadDestination>toward Trier</sit:roadDestination>
              <sit:pointCoordinates><sit:latitude>49.64</sit:latitude><sit:longitude>6.3</sit:longitude></sit:pointCoordinates>
            </sit:locationReference><sit:numberOfLanesRestricted>1</sit:numberOfLanesRestricted>
          </sit:situationRecord><sit:situationRecord xsi:type="sit:GeneralObstruction" id="obstruction-1">
            <sit:pointCoordinates><sit:latitude>49.5</sit:latitude><sit:longitude>6.1</sit:longitude></sit:pointCoordinates>
          </sit:situationRecord><sit:situationRecord xsi:type="sit:Accident" id="future">
            <sit:overallStartTime>2026-09-27T09:00:00Z</sit:overallStartTime>
            <sit:pointCoordinates><sit:latitude>49.6</sit:latitude><sit:longitude>6.1</sit:longitude></sit:pointCoordinates>
          </sit:situationRecord><sit:situationRecord xsi:type="sit:Accident" id="ended">
            <sit:overallEndTime>2026-09-27T08:00:00Z</sit:overallEndTime>
            <sit:pointCoordinates><sit:latitude>49.6</sit:latitude><sit:longitude>6.1</sit:longitude></sit:pointCoordinates>
          </sit:situationRecord><sit:situationRecord xsi:type="sit:Accident" id="no-location" />
          </sit:situation></payload>'''
        rows = feeds._parse_luxembourg_roads(ET.fromstring(document), now)
        self.assertEqual([row['properties']['layer'] for row in rows], ['construction', 'incidents'])
        self.assertEqual(rows[0]['geometry']['coordinates'], [6.3, 49.64])
        self.assertEqual(rows[0]['properties']['key'], 'lu:cita:work-1')
        self.assertIn('1 lane(s) restricted', rows[0]['properties']['detail'])
        stale = ET.fromstring(document.replace('2026-09-27T08:25:00Z', '2026-09-27T08:00:00Z'))
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_luxembourg_roads(stale, now)

    def test_luxembourg_retries_a_blank_xml_publication(self):
        root = ET.fromstring('<payload><publicationTime>2026-09-27T08:25:00Z</publicationTime></payload>')
        now = dt.datetime(2026, 9, 27, 8, 30, tzinfo=dt.timezone.utc).timestamp()
        with patch.object(feeds, '_get_xml', side_effect=[ET.ParseError('empty'), root]) as fetch, \
                patch.object(feeds.time, 'sleep') as sleep, patch.object(feeds.time, 'time', return_value=now):
            self.assertEqual(feeds._luxembourg_roads(), [])
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('www.cita.lu', fetch.call_args.args[0])
        sleep.assert_called_once_with(0.5)

    def test_luxembourg_camera_catalog_uses_geolocated_official_stills(self):
        root = ET.fromstring('''<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
          <Placemark id="camera_6006"><name>A6 - Camera 6006</name>
            <Point><coordinates>5.962785,49.636012,0</coordinates></Point></Placemark>
          <Placemark id="camera_bad"><Point><coordinates>5.9,49.6,0</coordinates></Point></Placemark>
          <Placemark id="camera_11"><Point><coordinates>9.9,49.6,0</coordinates></Point></Placemark>
        </Document></kml>''')
        rows = feeds._parse_luxembourg_cameras(root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['geometry']['coordinates'], [5.962785, 49.636012])
        self.assertEqual(rows[0]['properties']['snapshot_url'],
                         'https://www.cita.lu/info_trafic/cameras/images/cccam_6006.jpg')
        self.assertEqual(rows[0]['properties']['snapshot_refresh_ms'], 120000)
        with self.assertRaisesRegex(ValueError, 'no usable cameras'):
            feeds._parse_luxembourg_cameras(ET.fromstring('<kml xmlns="http://www.opengis.net/kml/2.2"/>'))

    def test_luxembourg_traffic_sensors_require_recent_measurements(self):
        now = dt.datetime(2026, 9, 27, 9, 15, tzinfo=dt.timezone.utc).timestamp()
        document = '''<d2LogicalModel xmlns="http://datex2.eu/schema/2/2_0">
          <publicationTime>2026-09-27T09:10:00Z</publicationTime>
          <siteMeasurements><measurementSiteReference id="A13.PS.6630"/>
            <measurementTimeDefault>2026-09-27T09:08:00Z</measurementTimeDefault>
            <measuredValue><basicData><pertinentLocation><locationForDisplay>
              <latitude>49.52</latitude><longitude>6.3</longitude>
            </locationForDisplay><roadNumber>A13</roadNumber></pertinentLocation>
              <averageVehicleSpeed><speed>85.5</speed></averageVehicleSpeed></basicData></measuredValue>
            <measuredValue><basicData><vehicleFlow><vehicleFlowRate>930</vehicleFlowRate></vehicleFlow></basicData></measuredValue>
          </siteMeasurements><siteMeasurements><measurementSiteReference id="old"/>
            <measurementTimeDefault>2026-09-27T08:00:00Z</measurementTimeDefault>
            <locationForDisplay><latitude>49.52</latitude><longitude>6.3</longitude></locationForDisplay>
            <vehicleFlow><vehicleFlowRate>50</vehicleFlowRate></vehicleFlow>
          </siteMeasurements></d2LogicalModel>'''
        rows = feeds._parse_luxembourg_traffic(ET.fromstring(document), 'a13', now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['key'], 'lu:cita:sensor:A13.PS.6630')
        self.assertEqual(rows[0]['geometry']['coordinates'], [6.3, 49.52])
        self.assertIn('85.5 km/h', rows[0]['properties']['detail'])
        self.assertIn('930 vehicles/hour', rows[0]['properties']['detail'])
        stale = ET.fromstring(document.replace('2026-09-27T09:10:00Z', '2026-09-27T08:00:00Z'))
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_luxembourg_traffic(stale, 'a13', now)

    def test_norway_roads_only_include_active_main_records(self):
        published = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+0000')
        base = {'geometry': {'type': 'Point', 'coordinates': [10.7, 59.9]},
                'properties': {'endJsonTime': published, 'situationId': 'event-1',
                               'situationType': 'MaintenanceWorks', 'isMainRecord': True,
                               'activePeriodAtLastUpdate': 1, 'locationDescription': 'E6 Oslo',
                               'description': 'Roadwork|One lane closed'}}
        rows = feeds._parse_norway_roads({'features': [base,
            {**base, 'properties': {**base['properties'], 'isMainRecord': False}},
            {**base, 'properties': {**base['properties'], 'activePeriodAtLastUpdate': 0}}]}, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'construction')
        self.assertEqual(rows[0]['properties']['key'], 'no:road:event-1')
        self.assertEqual(len(feeds._parse_norway_roads({'features': [base]}, NOW + 3600)), 1)
        with self.assertRaises(ValueError):
            feeds._parse_norway_roads({'features': [base]}, NOW + 2 * 3600)

    def test_norway_cameras_require_available_official_image(self):
        published = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+0000')
        base = {'geometry': {'type': 'Point', 'coordinates': [10.7, 59.9]},
                'properties': {'endJsonTime': published, 'cameraId': '3000063_1',
                               'status.stillImageAvailability': 'videoOrImagesAvailable',
                               'stillImageUrl': 'https://kamera.atlas.vegvesen.no/api/images/3000063_1'}}
        rows = feeds._parse_norway_cameras({'features': [base,
            {**base, 'properties': {**base['properties'], 'stillImageUrl': 'https://example.com/api/images/3000063_1'}},
            {**base, 'properties': {**base['properties'], 'status.stillImageAvailability': 'videoOrImagesUnavailableDueToCameraFault'}}]}, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'cameras')
        self.assertEqual(rows[0]['properties']['snapshot_url'], base['properties']['stillImageUrl'])

    def test_norway_weather_requires_recent_station_measurement(self):
        published = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+0000')
        base = {'geometry': {'type': 'Point', 'coordinates': [10.7, 59.9]},
                'properties': {'endJsonTime': published, 'measurementTime': published,
                               'referenceId': '100018', 'roadSurfaceTemperature': '2.5',
                               'windSpeed': '3.3'}}
        rows = feeds._parse_norway_weather({'features': [base]}, NOW)
        self.assertEqual(len(rows), 1)
        self.assertIn('Road 2.5°C', rows[0]['properties']['detail'])
        stale = {**base, 'properties': {**base['properties'], 'measurementTime':
                 dt.datetime.fromtimestamp(NOW - 3601, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+0000')}}
        self.assertEqual(feeds._parse_norway_weather({'features': [stale]}, NOW), [])

    def test_norway_travel_time_rejects_missing_data_and_uses_segment_midpoint(self):
        published = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+0000')
        base = {'geometry': {'type': 'LineString', 'coordinates': [[10.6, 59.8], [10.7, 59.9], [10.8, 60.0]]},
                'properties': {'endJsonTime': published, 'validAtTime': published,
                               'referenceId': '100289', 'missingData': False,
                               'actualTime': 120, 'expectedTime': 90, 'trafficStatusValue': 'freeFlow'}}
        rows = feeds._parse_norway_travel_times({'features': [base]}, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['geometry']['coordinates'], [10.7, 59.9])
        self.assertIn('Free flow', rows[0]['properties']['detail'])
        missing = {**base, 'properties': {**base['properties'], 'missingData': True}}
        self.assertEqual(feeds._parse_norway_travel_times({'features': [missing]}, NOW), [])

    def test_zurich_sensors_join_active_counters_by_station_number(self):
        locations = [
            {'geometry': {'type': 'Point', 'coordinates': [8.48, 47.45]},
             'properties': {'messst_nr': 113, 'dtv': 8068, 'dtv_bezugsjahr': 2025}},
            {'geometry': {'type': 'Point', 'coordinates': [8.49, 47.46]},
             'properties': {'messst_nr': 114}},
        ]
        collectors = [
            {'uID': {'id': 'M0113'}, 'name': 'Regensdorf: Niederhaslistrasse',
             'collectorStatus': 'ACTIVE'},
            {'uID': {'id': 'M0114'}, 'name': 'Inactive counter', 'collectorStatus': 'DISABLED'},
        ]
        rows = feeds._parse_zurich_sensors(locations, collectors)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['sensor_id'], 'M0113')
        self.assertIn('8,068 vehicles/day', rows[0]['properties']['detail'])

    def test_zurich_live_sensor_sample_is_recent_and_bounded(self):
        payload = {'uID': {'id': 'M0113', 'sub': {'id': '1'}},
                   'effectiveTime': str(NOW * 1000), 'swiss10Class': 'SWISS10_PW'}
        with patch.object(feeds.urllib.request, 'urlopen', return_value=io.BytesIO(
                (feeds.json.dumps(payload) + '\n').encode())), patch.object(feeds.time, 'time', return_value=NOW):
            sample = feeds.zurich_sensor_sample('M0113')
        self.assertEqual(sample['vehicle'], 'Passenger car')
        self.assertEqual(sample['lane'], '1')
        with self.assertRaises(ValueError):
            feeds.zurich_sensor_sample('../M0113')

    def test_zurich_roadworks_show_only_current_works_without_contact_details(self):
        current = {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [8.8, 47.5]},
                   'properties': {'strassenbez': '831', 'kmvon': '0.240',
                                  'strassenname': 'Pestalozzistrasse', 'gemeindename': 'Elsau',
                                  'beschreibung': 'Bridge repair', 'verkehrsfuehrung': 'Traffic lights',
                                  'status_baustelle': 'aktiv (Bauzeit)',
                                  'datum_baubeginn': '2026-03-02T00:00:00',
                                  'datum_bauende': '2026-09-30T00:00:00',
                                  'ansprechperson': 'Private contact', 'telefonnummer': '12345'}}
        future = {**current, 'properties': {**current['properties'], 'status_baustelle':
                  'zukünftig (Bauzeit in Zukunft)'}}
        expired = {**current, 'properties': {**current['properties'], 'datum_bauende':
                   '2026-09-25T00:00:00'}}
        bad_point = {**current, 'geometry': {'type': 'Point', 'coordinates': [0, 0]}}
        rows = feeds._parse_zurich_roadworks([current, future, expired, bad_point], NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['layer'], 'construction')
        self.assertEqual(rows[0]['geometry']['coordinates'], [8.8, 47.5])
        self.assertIn('Traffic lights', rows[0]['properties']['detail'])
        self.assertNotIn('Private contact', str(rows[0]))
        self.assertNotIn('12345', str(rows[0]))

    def test_autobahn_excludes_future_works_and_maps_current_closure(self):
        payload = {'closure': [
            {'identifier': 'current', 'future': False, 'title': 'A1 | Junction',
             'coordinate': {'lat': 51.2, 'long': 7.3}, 'description': ['Closed overnight']},
            {'identifier': 'planned', 'future': True, 'coordinate': {'lat': 51.3, 'long': 7.4}},
            {'identifier': 'later', 'future': False, 'startTimestamp': '2099-01-01T00:00:00Z',
             'coordinate': {'lat': 51.4, 'long': 7.5}},
        ]}
        rows = feeds._parse_autobahn_items('closure', 'A1', payload, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['geometry']['coordinates'], [7.3, 51.2])
        self.assertEqual(rows[0]['properties']['layer'], 'incidents')

    def test_hamburg_current_roads_reject_expired_future_and_old_open_incidents(self):
        now = dt.datetime(2026, 9, 28, 2, 30, tzinfo=dt.timezone.utc).timestamp()
        def item(identifier, kind, start, end='', point=None):
            return {'id': identifier, 'geometry': {'type': 'Point', 'coordinates': point or [10.0, 53.55]},
                    'properties': {'art': kind, 'description': 'Hamburg, road affected',
                                   'start': start, 'end': end}}
        payload = {'type': 'FeatureCollection', 'timeStamp': '2026-09-28T02:29:00Z',
                   'numberMatched': 5, 'features': [
                       item(1, 'ConstructionWorks', '2026-09-27 12:00:00', '2026-09-30 12:00:00'),
                       item(2, 'Accident', '2026-09-28 01:30:00'),
                       item(3, 'Accident', '2026-02-17 16:20:00'),
                       item(4, 'MaintenanceWorks', '2026-09-29 00:00:00'),
                       item(5, 'RoadOrCarriagewayOrLaneManagement', '2026-09-26 00:00:00',
                            '2026-09-27 00:00:00')]}
        rows = feeds._parse_hamburg_roads(payload, now)
        self.assertEqual([row['properties']['key'] for row in rows],
                         ['de:hamburg:road:1', 'de:hamburg:road:2'])
        self.assertEqual([row['properties']['layer'] for row in rows],
                         ['construction', 'incidents'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_hamburg_roads(payload, now + 3600)
        payload['numberMatched'] = 501
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            feeds._parse_hamburg_roads(payload, now)

    def test_berlin_roads_use_local_validity_and_reject_stale_publications(self):
        now = dt.datetime(2026, 9, 28, 3, tzinfo=dt.timezone.utc).timestamp()
        def item(identifier, kind, start, end, reported='2026-09-28T01:00:00Z'):
            return {'type': 'Feature', 'geometry': {'type': 'GeometryCollection', 'geometries': [
                {'type': 'Point', 'coordinates': [13.4, 52.52]},
                {'type': 'LineString', 'coordinates': [[13.4, 52.52], [13.41, 52.53]]}]},
                'properties': {'id': identifier, 'subtype': kind, 'objectState': 'modified',
                    'tstore': reported, 'validity': {'from': start, 'to': end},
                    'street': 'Berlin, Teststraße', 'content': 'Fahrbahn gesperrt'}}
        payload = {'type': 'FeatureCollection', 'features': [
            item('LMS/1', 'Baustelle', '28.09.2026 04:00', '29.09.2026 23:59'),
            item('LMS/2', 'Sperrung', '28.09.2026 04:00', '29.09.2026 23:59'),
            item('LMS/3', 'Gefahr', '28.09.2026 06:00', '29.09.2026 23:59'),
            item('LMS/4', 'Baustelle', '27.09.2026 04:00', '28.09.2026 03:00'),
            item('LMS/5', 'Sperrung', '', '', '2026-09-01T01:00:00Z')]}
        rows = feeds._parse_berlin_roads(payload, now - 60, now)
        self.assertEqual([row['properties']['layer'] for row in rows],
                         ['construction', 'incidents'])
        self.assertEqual(rows[0]['geometry']['coordinates'], [13.4, 52.52])
        self.assertIn('Berlin, Teststraße', rows[0]['properties']['detail'])
        self.assertNotEqual(rows[0]['properties']['key'], rows[1]['properties']['key'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_berlin_roads(payload, now - 4 * 3600, now)

    def test_autobahn_service_cache_is_shared_and_bounded(self):
        cache = {'until': 0, 'roads': {}, 'lock': threading.Lock()}
        calls = []
        def request(url):
            calls.append(url)
            if url == feeds.AUTOBAHN_BASE:
                return {'roads': ['A1', 'A2', 'bad-road']}
            return {'warning': [{'identifier': url.split('/')[-3], 'future': False,
                                 'coordinate': {'lat': 51.2, 'long': 7.3}}]}
        with patch.dict(feeds._AUTOBAHN_CACHE, {'warning': cache}), patch.object(feeds, '_get_json', side_effect=request):
            first = feeds._autobahn_service('warning')
            second = feeds._autobahn_service('warning')
        self.assertEqual(len(first), 2)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 3)

    def test_autobahn_requests_only_for_german_road_views(self):
        feature = feeds._feature([7.3, 51.2], {'layer': 'incidents', 'key': 'de:sample'})
        snapshot = {'sources': {}, 'errors': []}
        with patch.object(feeds, '_snapshot', return_value=snapshot), patch.object(
                feeds, '_autobahn_service', return_value=[feature]) as request:
            german = feeds.road_snapshot('incidents', (7, 51, 8, 52))
            french = feeds.road_snapshot('incidents', (-4, 44, -3, 45))
        self.assertEqual(len(german['features']), 2)
        self.assertEqual(len(french['features']), 0)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['warning', 'closure'])

    def test_ndw_current_road_situations_use_wgs84_and_validity(self):
        root = ET.fromstring('''<messageContainer xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <payload xsi:type="sit:SituationPublication">
            <publicationTime>2026-09-26T18:00:00.123456789Z</publicationTime>
            <situation id="open"><situationRecord xsi:type="sit:RoadOrCarriagewayOrLaneManagement">
              <validityStatus>definedByValidityTimeSpec</validityStatus>
              <overallStartTime>2026-09-26T17:00:00Z</overallStartTime>
              <causeType>roadMaintenance</causeType>
              <roadOrCarriagewayOrLaneManagementType>laneClosures</roadOrCarriagewayOrLaneManagementType>
              <gmlLineString srsName="WGS 84"><posList>52.0 5.0 52.1 5.1</posList></gmlLineString>
            </situationRecord></situation>
            <situation id="ended"><situationRecord xsi:type="sit:Accident">
              <validityStatus>active</validityStatus><overallEndTime>2026-09-26T17:00:00Z</overallEndTime>
              <pointCoordinates><latitude>52.2</latitude><longitude>5.2</longitude></pointCoordinates>
            </situationRecord></situation>
          </payload>
        </messageContainer>''')
        rows = feeds._parse_ndw_roads(root, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['geometry']['coordinates'], [5.1, 52.1])
        self.assertEqual(rows[0]['properties']['layer'], 'construction')
        self.assertIn('Lane closed', rows[0]['properties']['detail'])
        with self.assertRaises(ValueError):
            feeds._parse_ndw_roads(root, NOW + 3600)

    def test_ndw_bridge_schedule_only_shows_current_short_windows(self):
        root = ET.fromstring('''<messageContainer xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <payload xsi:type="sit:SituationPublication">
            <publicationTime>2026-09-26T18:00:00Z</publicationTime>
            <situation><situationRecord id="BMS01_bridge_1">
              <generalNetworkManagementType>bridgeSwingInOperation</generalNetworkManagementType>
              <overallStartTime>2026-09-26T17:58:00Z</overallStartTime>
              <overallEndTime>2026-09-26T18:04:00Z</overallEndTime>
              <pointCoordinates><latitude>52.35</latitude><longitude>4.85</longitude></pointCoordinates>
            </situationRecord></situation>
            <situation><situationRecord id="BMS01_future">
              <generalNetworkManagementType>bridgeSwingInOperation</generalNetworkManagementType>
              <overallStartTime>2026-09-26T19:00:00Z</overallStartTime>
              <overallEndTime>2026-09-26T19:06:00Z</overallEndTime>
              <pointCoordinates><latitude>52.36</latitude><longitude>4.86</longitude></pointCoordinates>
            </situationRecord></situation>
            <situation><situationRecord id="BMS01_long">
              <generalNetworkManagementType>bridgeSwingInOperation</generalNetworkManagementType>
              <overallStartTime>2026-09-26T16:00:00Z</overallStartTime>
              <overallEndTime>2026-09-26T20:00:00Z</overallEndTime>
              <pointCoordinates><latitude>52.37</latitude><longitude>4.87</longitude></pointCoordinates>
            </situationRecord></situation>
          </payload>
        </messageContainer>''')
        rows = feeds._parse_ndw_bridge_openings(root, NOW)
        self.assertEqual([item['properties']['key'] for item in rows],
                         ['nl:ndw:bridge:BMS01_bridge_1'])
        self.assertIn('schedule, not a confirmed closure', rows[0]['properties']['detail'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_ndw_bridge_openings(root, NOW + 3600)

    def test_ndw_signs_keep_only_working_displays(self):
        image = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 400).decode()
        root = ET.fromstring(f'''<messageContainer xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <payload xsi:type="vms:VmsTablePublication"><vmsControllerTable>
            <vmsController id="good"><value>A2 sign</value><pointCoordinates><latitude>52.0</latitude><longitude>5.0</longitude></pointCoordinates></vmsController>
            <vmsController id="broken"><pointCoordinates><latitude>52.1</latitude><longitude>5.1</longitude></pointCoordinates></vmsController>
            <vmsController id="blank"><pointCoordinates><latitude>52.2</latitude><longitude>5.2</longitude></pointCoordinates></vmsController>
          </vmsControllerTable></payload>
          <payload xsi:type="vms:VmsPublication"><publicationTime>2026-09-26T18:00:00.123456789Z</publicationTime>
            <vmsControllerStatus><vmsControllerReference id="good"/><workingStatus>working</workingStatus><imageFormat>png</imageFormat><imageData>{image}</imageData></vmsControllerStatus>
            <vmsControllerStatus><vmsControllerReference id="broken"/><workingStatus>notWorking</workingStatus><textLine>Closed</textLine></vmsControllerStatus>
            <vmsControllerStatus><vmsControllerReference id="blank"/><workingStatus>working</workingStatus></vmsControllerStatus>
          </payload>
        </messageContainer>''')
        rows = feeds._parse_ndw_signs(root, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['geometry']['coordinates'], [5.0, 52.0])
        self.assertEqual(rows[0]['properties']['image_data'], image)
        with self.assertRaises(ValueError):
            feeds._parse_ndw_signs(root, NOW + 3600)

    def test_opendatasoft_fetches_all_outage_pages(self):
        first = {'results': [{'id': i} for i in range(100)], 'total_count': 101}
        second = {'results': [{'id': 100}], 'total_count': 101}
        metadata = {'metas': {'default': {'data_processed': '2026-09-26T12:00:00Z'}}}
        with patch.object(feeds, '_get_json', side_effect=[first, second, metadata]) as request:
            rows, updated = feeds._ods('https://example.org', 'outages', 'isaffected = 1')
        self.assertEqual(len(rows), 101)
        self.assertEqual(updated, '2026-09-26T12:00:00Z')
        self.assertIn('offset=100', request.call_args_list[1].args[0])

    def test_roadworks_only_map_current_geolocated_events(self):
        def item(name, start, end, geometry):
            return {'geometry': geometry, 'properties': {
                'situationId': name, 'announcements': [{'title': name,
                    'timeAndDuration': {'startTime': start, 'endTime': end}}]}}
        payload = {'features': [
            item('active', '2026-09-25T00:00:00Z', '2026-09-28T00:00:00Z',
                 {'type': 'LineString', 'coordinates': [[24.0, 60.0], [25.0, 61.0]]}),
            item('future', '2026-09-28T00:00:00Z', None, {'type': 'Point', 'coordinates': [24, 60]}),
            item('missing', '2026-09-25T00:00:00Z', None, None),
        ]}
        with patch.object(feeds, '_get_json', return_value=payload), patch.object(feeds.time, 'time', return_value=NOW):
            result = feeds._fintraffic_messages('construction')
        self.assertEqual([x['properties']['key'] for x in result], ['fi:construction:active'])
        self.assertEqual(result[0]['geometry']['coordinates'], [25.0, 61.0])

    def test_signs_exclude_old_and_broken_devices(self):
        def sign(name, updated, reliability='NORMAL'):
            return {'geometry': {'type': 'Point', 'coordinates': [24, 60]},
                    'properties': {'id': name, 'type': 'SPEEDLIMIT', 'displayValue': '80',
                                   'effectDate': updated, 'reliability': reliability}}
        payload = {'features': [sign('fresh', '2026-09-26T12:00:00Z'),
                                sign('old', '2026-09-10T00:00:00Z'),
                                sign('broken', '2026-09-26T12:00:00Z', 'MALFUNCTION')]}
        with patch.object(feeds, '_get_json', return_value=payload), patch.object(feeds.time, 'time', return_value=NOW):
            result = feeds._fintraffic_signs()
        self.assertEqual([x['properties']['key'] for x in result], ['fi:sign:fresh'])
        self.assertIn('80 km/h', result[0]['properties']['title'])

    def test_london_works_and_incidents_are_separate(self):
        payload = [
            {'id': '1', 'category': 'Works', 'status': 'Active',
             'geography': {'coordinates': [-0.1, 51.5]}},
            {'id': '2', 'category': 'Breakdowns', 'status': 'Active',
             'geography': {'coordinates': [-0.2, 51.6]}},
            {'id': '3', 'category': 'Works', 'status': 'Inactive',
             'geography': {'coordinates': [-0.3, 51.7]}},
        ]
        with patch.object(feeds, '_get_json', return_value=payload):
            result = feeds._tfl_disruptions()
        self.assertEqual([x['properties']['layer'] for x in result], ['construction', 'incidents'])

    def test_london_geojson_catalog_resolves_disruption_details(self):
        catalog = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'id': 'TIMS-1', 'geometry': {'type': 'Point', 'coordinates': [-0.1, 51.5]}},
            {'type': 'Feature', 'id': 'TIMS-2', 'geometry': {'type': 'Point', 'coordinates': [-0.2, 51.6]}},
        ]}
        details = [
            {'id': 'TIMS-1', 'category': 'Works', 'status': 'Active',
             'geography': {'coordinates': [-0.1, 51.5]}},
            {'id': 'TIMS-2', 'category': 'Breakdowns', 'status': 'Active',
             'geography': {'coordinates': [-0.2, 51.6]}},
        ]
        with patch.object(feeds, '_get_json', side_effect=[catalog, details]) as fetch:
            result = feeds._tfl_disruptions()
        self.assertEqual(fetch.call_args_list[1].args[0],
                         f'{feeds.TFL_URL}/TIMS-1,TIMS-2')
        self.assertEqual([x['properties']['layer'] for x in result], ['construction', 'incidents'])

    def test_london_geojson_catalog_keeps_locations_when_details_fail(self):
        catalog = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'id': 'TIMS-1', 'geometry': {'type': 'Point', 'coordinates': [-0.1, 51.5]}}
        ]}
        with patch.object(feeds, '_get_json', side_effect=[catalog, OSError('upstream timeout')]):
            result = feeds._tfl_disruptions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['geometry']['coordinates'], [-0.1, 51.5])

    def test_national_highways_weekly_works_are_scheduled_and_windowed(self):
        source = b'''<Report xmlns="WebTeam"><HE_PLANNED_WORKS>
          <HE_PLANNED_WORKS_Collection>
            <HE_PLANNED_WORKS NEW_EVENT_NUMBER="00389408-001" STATUS="Published"
              SDATE="26-SEP-2026 06:00" EDATE="28-SEP-2026 05:00"
              DESCRIPTION="A38 lane restriction" EXPDEL="Slight (less than 10 mins)">
              <EASTNORTH><EASTNORTH CENTRE_EASTING="413949" CENTRE_NORTHING="309566"/></EASTNORTH>
              <ROADS><ROAD ROAD_NUMBER="A38"/></ROADS>
            </HE_PLANNED_WORKS>
            <HE_PLANNED_WORKS NEW_EVENT_NUMBER="00389409-001" STATUS="Published"
              SDATE="28-SEP-2026 06:00" EDATE="29-SEP-2026 05:00"
              DESCRIPTION="Future work"><EASTNORTH>
              <EASTNORTH CENTRE_EASTING="413949" CENTRE_NORTHING="309566"/>
              </EASTNORTH></HE_PLANNED_WORKS>
          </HE_PLANNED_WORKS_Collection></HE_PLANNED_WORKS></Report>'''
        previous = dict(feeds._NH_ROADWORKS_CACHE)
        catalog = {'success': True, 'result': {'resources': [{
            'url': ('https://s3.eu-west-2.amazonaws.com/webdata.nationalhighways.co.uk/'
                    'ha-roadworks/nh_roadworks_2026_21_9.xml'),
            'created': '2026-09-21T10:51:15',
        }]}}
        try:
            feeds._NH_ROADWORKS_CACHE.update(until=0, url='', activities=[])
            with patch.object(feeds.time, 'time', return_value=NOW), \
                    patch.object(feeds, '_get_json', return_value=catalog), \
                    patch.object(feeds.urllib.request, 'urlopen', return_value=io.BytesIO(source)):
                rows = feeds._national_highways_roadworks()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]['geometry']['coordinates'][0], -1.7951, places=3)
            self.assertAlmostEqual(rows[0]['geometry']['coordinates'][1], 52.6836, places=3)
            self.assertIn('not confirmed live', rows[0]['properties']['detail'])
            self.assertEqual(rows[0]['properties']['layer'], 'construction')
        finally:
            feeds._NH_ROADWORKS_CACHE.update(previous)

    def test_national_highways_rejects_old_catalog_file(self):
        previous = dict(feeds._NH_ROADWORKS_CACHE)
        try:
            feeds._NH_ROADWORKS_CACHE['until'] = 0
            catalog = {'success': True, 'result': {'resources': [{
                'url': ('https://s3.eu-west-2.amazonaws.com/webdata.nationalhighways.co.uk/'
                        'ha-roadworks/nh_roadworks_2026_1_1.xml'),
                'created': '2026-01-01T10:00:00',
            }]}}
            with patch.object(feeds.time, 'time', return_value=NOW), \
                    patch.object(feeds, '_get_json', return_value=catalog):
                with self.assertRaisesRegex(ValueError, 'stale'):
                    feeds._national_highways_roadworks()
        finally:
            feeds._NH_ROADWORKS_CACHE.update(previous)

    def test_wales_roadworks_only_include_current_geolocated_works(self):
        local_now = dt.datetime.now(ZoneInfo('Europe/London'))
        start = (local_now - dt.timedelta(days=1)).strftime('%d/%m/%Y %H:%M')
        end = (local_now + dt.timedelta(days=1)).strftime('%d/%m/%Y %H:%M')
        future = (local_now + dt.timedelta(days=2)).strftime('%d/%m/%Y %H:%M')
        feed = ET.fromstring(f'''<rss xmlns:georss="http://www.georss.org/georss"><channel>
          <item><guid>active</guid><title>A55 works</title><link>https://traffic.wales/road-traffic-alerts/1</link>
            <description>Start time: {start}, End Date: {end}</description><georss:point>53.2 -3.1</georss:point></item>
          <item><guid>future</guid><title>Future works</title>
            <description>Start time: {future}, End Date: {end}</description><georss:point>53.3 -3.2</georss:point></item>
          <item><guid>missing</guid><title>No location</title>
            <description>Start time: {start}, End Date: {end}</description></item>
        </channel></rss>''')
        with patch.object(feeds, '_get_xml', return_value=feed):
            result = feeds._wales_feed('construction')
        self.assertEqual([x['properties']['key'] for x in result], ['uk:wales:construction:active'])
        self.assertEqual(result[0]['geometry']['coordinates'], [-3.1, 53.2])

    def test_wales_incident_feed_maps_current_rss_items(self):
        feed = ET.fromstring('''<rss xmlns:georss="http://www.georss.org/georss"><channel>
          <item><guid>incident-1</guid><title>A55 collision</title><description>Lane blocked</description>
            <link>https://traffic.wales/road-traffic-alerts/2</link><georss:point>53.1 -3.0</georss:point></item>
        </channel></rss>''')
        with patch.object(feeds, '_get_xml', return_value=feed):
            result = feeds._wales_feed('incidents')
        self.assertEqual(result[0]['properties']['layer'], 'incidents')
        self.assertEqual(result[0]['properties']['source'], 'Traffic Wales')

    def test_france_datex_maps_only_current_geolocated_road_events(self):
        now = dt.datetime(2026, 9, 27, 12, 30, tzinfo=dt.timezone.utc).timestamp()
        document = '''<d2LogicalModel xmlns="http://datex2.eu/schema/2/2_0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <publicationTime>2026-09-27T12:00:00Z</publicationTime>
          <situation><situationRecord xsi:type="MaintenanceWorks" id="works-1">
            <situationRecordVersionTime>2026-09-27T11:50:00Z</situationRecordVersionTime>
            <validity><overallStartTime>2026-09-27T10:00:00Z</overallStartTime>
              <overallEndTime>2026-09-27T14:00:00Z</overallEndTime></validity>
            <generalPublicComment><comment><values><value>Travaux sur la chaussée</value></values></comment></generalPublicComment>
            <groupOfLocations><pointCoordinates><latitude>48.85</latitude><longitude>2.35</longitude></pointCoordinates>
              <roadNumber>N001</roadNumber></groupOfLocations>
          </situationRecord><situationRecord xsi:type="Accident" id="crash-1">
            <validity><overallStartTime>2026-09-27T11:00:00Z</overallStartTime></validity>
            <groupOfLocations><pointCoordinates><latitude>45.7</latitude><longitude>4.8</longitude></pointCoordinates></groupOfLocations>
          </situationRecord><situationRecord xsi:type="Accident" id="ended">
            <validity><overallEndTime>2026-09-27T11:00:00Z</overallEndTime></validity>
            <groupOfLocations><pointCoordinates><latitude>45.7</latitude><longitude>4.8</longitude></pointCoordinates></groupOfLocations>
          </situationRecord></situation></d2LogicalModel>'''
        root = ET.fromstring(document)
        result = feeds._parse_france_roads(root, now)
        self.assertEqual([item['properties']['layer'] for item in result], ['construction', 'incidents'])
        self.assertEqual(result[0]['geometry']['coordinates'], [2.35, 48.85])
        self.assertEqual(result[0]['properties']['title'], 'Roadworks · N001')
        stale = ET.fromstring(document.replace('2026-09-27T12:00:00Z', '2026-09-26T12:00:00Z'))
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_france_roads(stale, now)

    def test_france_sensor_reference_and_fresh_measurements(self):
        self.assertAlmostEqual(feeds._lambert93_to_lonlat(700000, 6600000)[0], 3, places=5)
        self.assertAlmostEqual(feeds._lambert93_to_lonlat(700000, 6600000)[1], 46.5, places=5)
        header = ';'.join(['code_pme', 'source', 'source_2', 'code_insee_commune', 'axe',
                           'pr_debut', 'abscisse_debut', 'pr_fin', 'abscisse_fin',
                           'sens_gestionnaire', 'sens_cardinal', 'sens_migratoire',
                           'sens_giratoire', 'longueur', 'nb_voies', 'x_deb', 'y_deb',
                           'x_fin', 'y_fin', 'code_traficolor'])
        row = ['station-1', 'DIR', '42', 'N7', 'marker', '0', 'marker', '0', '1',
               'NORD_SUD', 'Y', '', '0', '0', '700000', '6600000', '700000', '6600000', 'CODE']
        references = feeds._parse_france_sensor_references(header + '\n' + ';'.join(row))
        self.assertEqual(references['station-1'][1], 'N7')
        now = dt.datetime(2026, 9, 27, 12, 30, tzinfo=dt.timezone.utc).timestamp()
        xml = '''<d2LogicalModel xmlns="http://datex2.eu/schema/2/2_0">
          <payloadPublication><publicationTime>2026-09-27T12:20:00Z</publicationTime>
          <siteMeasurements><measurementSiteReference id="station-1"/>
            <measurementTimeDefault>2026-09-27T12:25:00Z</measurementTimeDefault>
            <measuredValue><basicData><vehicleFlow><vehicleFlowRate>320</vehicleFlowRate></vehicleFlow></basicData></measuredValue>
            <measuredValue><basicData><averageVehicleSpeed><speed>106</speed></averageVehicleSpeed></basicData></measuredValue>
          </siteMeasurements></payloadPublication></d2LogicalModel>'''
        result = feeds._parse_france_sensors(ET.fromstring(xml), references, now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['properties']['layer'], 'sensors')
        self.assertEqual(result[0]['properties']['detail'], '106 km/h · 320 vehicles/h')
        stale = ET.fromstring(xml.replace('2026-09-27T12:20:00Z', '2026-09-27T10:20:00Z'))
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_france_sensors(stale, references, now)

    def test_flemish_datex_uses_lambert72_and_current_event_windows(self):
        now = dt.datetime(2026, 9, 27, 12, 30, tzinfo=dt.timezone.utc).timestamp()
        lon, lat = feeds._lambert72_to_lonlat(235603.38, 203901.72)
        self.assertAlmostEqual(lon, 5.5919238, places=5)
        self.assertAlmostEqual(lat, 51.1388124, places=5)
        xml = '''<payload xmlns="http://datex2.eu/schema/3/d2Payload" xmlns:s="http://datex2.eu/schema/3/situation"
          xmlns:l="http://datex2.eu/schema/3/locationReferencing" xmlns:g="http://datex2.eu/schema/3/gml"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <publicationTime>2026-09-27T12:25:00Z</publicationTime>
          <s:situation id="EVT123"><s:situationVersionTime>2026-09-27T12:20:00Z</s:situationVersionTime>
            <s:situationRecord xsi:type="s:RoadOrCarriagewayOrLaneManagement">
              <s:validity><s:validityStatus>active</s:validityStatus><s:validityTimeSpecification>
                <s:overallStartTime>2026-09-27T12:00:00Z</s:overallStartTime>
                <s:overallEndTime>2026-09-27T14:00:00Z</s:overallEndTime>
              </s:validityTimeSpecification></s:validity>
              <s:locationReference><l:gmlLineString srsName="EPSG:31370"><g:posList>149000 170000 150000 170000 151000 170000</g:posList></l:gmlLineString></s:locationReference>
              <s:roadOrCarriagewayOrLaneManagementType>newRoadworksLayout</s:roadOrCarriagewayOrLaneManagementType>
            </s:situationRecord></s:situation>
          <s:situation id="EVT456"><s:situationRecord xsi:type="s:RoadOrCarriagewayOrLaneManagement">
            <s:validity><s:validityStatus>active</s:validityStatus><s:overallStartTime>2026-09-27T12:00:00Z</s:overallStartTime></s:validity>
            <s:locationReference><l:pointCoordinates><l:latitude>170000</l:latitude><l:longitude>150000</l:longitude></l:pointCoordinates></s:locationReference>
            <s:roadOrCarriagewayOrLaneManagementType>roadClosed</s:roadOrCarriagewayOrLaneManagementType>
          </s:situationRecord></s:situation>
          <s:situation id="EVT789"><s:situationRecord xsi:type="s:MaintenanceWorks">
            <s:validity><s:validityStatus>active</s:validityStatus><s:overallEndTime>2026-09-27T11:00:00Z</s:overallEndTime></s:validity>
            <s:locationReference><l:pointCoordinates><l:latitude>170000</l:latitude><l:longitude>150000</l:longitude></l:pointCoordinates></s:locationReference>
          </s:situationRecord></s:situation></payload>'''
        rows = feeds._parse_belgium_roads(ET.fromstring(xml), {'123': 'E40'}, now)
        self.assertEqual([row['properties']['layer'] for row in rows], ['construction', 'incidents'])
        self.assertEqual(rows[0]['properties']['title'], 'Roadworks · E40')
        self.assertAlmostEqual(rows[0]['geometry']['coordinates'][0], 4.368752, places=5)
        self.assertAlmostEqual(rows[0]['geometry']['coordinates'][1], 50.840411, places=5)
        self.assertEqual(rows[1]['properties']['detail'], 'Road closed')
        stale = ET.fromstring(xml.replace('2026-09-27T12:25:00Z', '2026-09-27T10:25:00Z'))
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_belgium_roads(stale, now=now)

    def test_flemish_otap_road_name_join(self):
        root = ET.fromstring('''<situationPublication><situation><key><situationReference>SIT123</situationReference></key>
          <situationElement><elementlocation><milestone><roadName>E411 - A4</roadName></milestone>
          </elementlocation></situationElement></situation></situationPublication>''')
        self.assertEqual(feeds._belgium_otap_road_names(root), {'123': 'E411 - A4'})

    def test_gipod_only_maps_active_road_impacts_caused_by_work(self):
        now = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.timezone.utc).timestamp()

        def item(key, effects, cause='/groundworks/123', start='2026-09-26T00:00:00Z',
                 end='2026-09-28T00:00:00Z', status='Gevalideerd'):
            return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [4.4, 50.85]},
                    'properties': {'ZoneId': key, 'Consequences': effects,
                                   'HindranceConsequenceOf': cause, 'HindranceStart': start,
                                   'HindranceEnd': end, 'HindranceStatus': status,
                                   'HindranceDescription': 'Brussels, Main Street: utility works'}}

        rows = feeds._parse_gipod_roadworks([
            item('road', 'Versmalde rijstroken;Parkeerverbod'),
            item('pedestrian', 'Beperkte doorgang voor voetgangers'),
            item('future', 'Versmalde rijstroken', start='2026-10-01T00:00:00Z'),
            item('expired', 'Versmalde rijstroken', end='2026-09-26T00:00:00Z'),
            item('event', 'Versmalde rijstroken', cause='/events/123'),
            item('draft', 'Versmalde rijstroken', status='Ontwerp'),
        ], now)
        self.assertEqual([row['properties']['key'] for row in rows], ['be:gipod:road'])
        self.assertEqual(rows[0]['properties']['title'], 'Road work · Brussels, Main Street')
        self.assertIn('Versmalde rijstroken', rows[0]['properties']['detail'])

    def test_power_filters_restored_and_future_outages_and_deduplicates(self):
        uk_rows = [
            {'incidentreference': 'a', 'geopoint': {'lon': 0.1, 'lat': 51.5},
             'powercuttype': 'Unplanned', 'nocustomeraffected': 10},
            {'incidentreference': 'b', 'geopoint': {'lon': 0.2, 'lat': 51.6},
             'powercuttype': 'Planned', 'planneddate': '2026-09-28T00:00:00'},
        ]
        npg_rows = [
            {'reference': 'x', 'lng': -1.5, 'lat': 54.5, 'totalconfirmedpowercut': 20},
            {'reference': 'x', 'lng': -1.6, 'lat': 54.6, 'totalconfirmedpowercut': 20},
        ]
        with patch.object(feeds, '_ods', side_effect=[(uk_rows, '2026-09-26T12:00:00Z'),
                                                     (npg_rows, '2026-09-26T12:00:00Z')]), patch.object(feeds.time, 'time', return_value=NOW):
            self.assertEqual([x['properties']['key'] for x in feeds._ukpn_outages()], ['uk:ukpn:a'])
            self.assertEqual([x['properties']['key'] for x in feeds._npg_outages()], ['uk:npg:x'])

    def test_ssen_powertrack_uses_current_geolocated_faults(self):
        payload = {'timestampUtc': '2026-09-27T12:00:00Z', 'faults': [
            {'reference': 'UA123', 'title': 'GU20 Area', 'location': {'longitude': -0.65, 'latitude': 51.36},
             'customerCount': 29, 'estimatedRestorationTimeUtc': '2026-09-27T18:00:00Z'},
            {'reference': 'UA123', 'location': {'longitude': -0.66, 'latitude': 51.35}},
            {'reference': 'UA124', 'location': None},
        ]}
        with patch.object(feeds, '_get_json', return_value=payload):
            result = feeds._ssen_outages()
        self.assertEqual([x['properties']['key'] for x in result], ['uk:ssen:UA123'])
        self.assertEqual(result[0]['properties']['customers_affected'], 29)
        self.assertEqual(result[0]['properties']['source_updated'], '2026-09-27T12:00:00Z')

    def test_nged_power_cuts_exclude_stale_restored_and_future_rows(self):
        now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
        base = {'Upload Date': '2026-09-27T12:30:00', 'Status': 'In Progress',
                'Planned': 'false', 'Region': 'South Wales', 'Category': 'HV OVERHEAD',
                'Confirmed Off': '7', 'Predicted Off': '3', 'Location Latitude': '51.5',
                'Location Longitude': '-3.2', 'ETR': '2026-09-27T14:00:00'}
        rows = [
            dict(base, **{'Incident ID': 'live'}),
            dict(base, **{'Incident ID': 'live', 'Location Latitude': '52.0'}),
            dict(base, **{'Incident ID': 'old', 'Upload Date': '2026-09-27T08:00:00'}),
            dict(base, **{'Incident ID': 'done', 'Status': 'Completed'}),
            dict(base, **{'Incident ID': 'future', 'Planned': 'true', 'Start Time': '2026-09-27T15:00:00'}),
            dict(base, **{'Incident ID': 'invalid', 'Location Latitude': ''}),
        ]
        with patch.object(feeds, '_get_csv', return_value=rows):
            result = feeds._nged_outages(now)
        self.assertEqual([item['properties']['key'] for item in result], ['uk:nged:live'])
        self.assertEqual(result[0]['properties']['customers_affected'], 10)
        self.assertEqual(result[0]['properties']['source_updated'], '2026-09-27T11:30:00Z')
        self.assertEqual(result[0]['properties']['etr'], '2026-09-27T13:00:00Z')
        self.assertEqual(result[0]['properties']['source_label'], 'Supported by NGED Open Data')

    def test_liander_power_cuts_exclude_resolved_and_stale_records(self):
        now = 1790542800
        fields = {'STORING_NUMMER': 8371446, 'STORING_TYPE': 'S',
                  'STORING_ENERGIESOORT': 'Elektriciteit',
                  'STORING_STATUS': 'monteur onderweg',
                  'STORING_DATUM_GEMELD': (now - 3600) * 1000,
                  'STORING_DATUM_EIND': None,
                  'STORING_SERVICE_UPDATE': (now - 300) * 1000,
                  'STORING_GETROFFEN_KLANTEN': '< 100',
                  'STORING_GETROFFEN_PLAATSEN': 'AMSTERDAM'}
        def outage(**changes):
            return {'attributes': dict(fields, **changes),
                    'centroid': {'x': 4.9, 'y': 52.37}}
        payload = {'features': [outage(), outage(),
                    outage(STORING_NUMMER=2, STORING_STATUS='opgelost'),
                    outage(STORING_NUMMER=3, STORING_DATUM_EIND=now * 1000),
                    outage(STORING_NUMMER=4, STORING_TYPE='P'),
                    outage(STORING_NUMMER=5, STORING_ENERGIESOORT='Gas'),
                    outage(STORING_NUMMER=6, STORING_SERVICE_UPDATE=(now - 90000) * 1000),
                    outage(STORING_NUMMER=7, STORING_DATUM_GEMELD=(now - 8 * 86400) * 1000),
                    dict(outage(STORING_NUMMER=8), centroid={'x': 0, 'y': 0})]}
        rows = feeds._parse_liander_outages(payload, now)
        self.assertEqual([row['properties']['key'] for row in rows], ['nl:liander:8371446'])
        self.assertEqual(rows[0]['geometry']['coordinates'], [4.9, 52.37])
        self.assertEqual(rows[0]['properties']['customers_affected'], '< 100')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            feeds._parse_liander_outages(dict(payload, exceededTransferLimit=True), now)

    def test_scottish_archive_maps_current_work_and_bounds_response(self):
        fields = ['ActivityStatus', 'Category', 'Longitude', 'Latitude', 'StartDateTimeUTC',
                  'EndDateTimeUTC', 'ActivityReference', 'Street', 'Town', 'TrafficManagement',
                  'TrafficImpact', 'Description', 'LastUpdatedDateTimeUTC']
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=fields)
        writer.writeheader()
        base = dict(ActivityStatus='In Progress', Category='Major', Longitude='-3.19', Latitude='55.95',
                    StartDateTimeUTC='2026-09-26T00:00:00Z', EndDateTimeUTC='2026-09-28T00:00:00Z',
                    ActivityReference='work-1', Street='North Bridge', Town='Edinburgh',
                    TrafficManagement='Lane Closure', TrafficImpact='High', Description='Bridge repairs',
                    LastUpdatedDateTimeUTC='2026-09-26T12:00:00Z')
        writer.writerow(base)
        writer.writerow(dict(base, ActivityReference='event', Category='Event'))
        writer.writerow(dict(base, ActivityReference='planned', ActivityStatus='Proposed'))
        writer.writerow(dict(base, ActivityReference='invalid', Latitude=''))
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as zipped:
            zipped.writestr('CurrentActivities.csv', csv_buffer.getvalue())
        activities = feeds._parse_scotland_archive(archive.getvalue())
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0][2]['properties']['title'], 'North Bridge')
        self.assertEqual(activities[0][2]['geometry']['coordinates'], [-3.19, 55.95])
        with patch.object(feeds, '_snapshot', return_value={'sources': {'scotland': [activities[0][2]]}, 'errors': []}):
            self.assertEqual(len(feeds.road_snapshot('construction', (-3.3, 55.9, -3.1, 56.0))['features']), 1)
            self.assertEqual(len(feeds.road_snapshot('construction', (-2, 55, -1, 56))['features']), 0)


if __name__ == '__main__':
    unittest.main()
