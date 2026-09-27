import unittest
import base64
import csv
import datetime as dt
import io
import threading
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

import international_infrastructure as feeds


NOW = 1790445600  # 2026-09-26 UTC


class InfrastructureTests(unittest.TestCase):
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
                         'https://etraffic.dgt.es/camarasEtraffic/168408.jpg')
        self.assertEqual(rows[0]['geometry']['coordinates'], [-0.4282263, 42.304092])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds._parse_dgt_cameras(root, now + 4 * 3600)

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
