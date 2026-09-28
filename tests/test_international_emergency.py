import datetime as dt
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import international_emergency as feeds


class InternationalEmergencyTests(unittest.TestCase):
    def test_poland_rso_maps_only_current_geocoded_advisories(self):
        now = dt.datetime(2026, 9, 28, 6, tzinfo=dt.timezone.utc)
        active = {'id': 23344126, 'title': 'Jakość wody przeznaczonej do spożycia',
                  'shortcut': 'Water notice for Nowe Miasto Lubawskie',
                  'longitude': 19.5923, 'latitude': 53.4239,
                  'valid_from': '2026-09-18 11:40:00',
                  'valid_to': '2026-10-02 23:59:00',
                  'updated_at': '2026-09-18 11:42:38'}
        rows = feeds.parse_poland_rso({'newses': [active, {**active, 'id': 2, 'latitude': None},
                                                 {**active, 'id': 3, 'valid_to': '2026-09-27 00:00:00'},
                                                 {**active, 'id': 4, 'longitude': 10}]}, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'pl:rso:23344126')
        self.assertEqual(rows[0]['category'], 'warning')
        self.assertIn('Advisory', rows[0]['title'])
        self.assertEqual(rows[0]['observed'], '2026-09-18T09:42:38Z')
        self.assertEqual(feeds.parse_poland_rso({'newses': [active]},
                         now + dt.timedelta(days=31)), [])
        with self.assertRaisesRegex(ValueError, 'invalid'):
            feeds.parse_poland_rso({'newses': 'bad'}, now)

    def test_usti_fire_reports_join_recent_status_and_map_coordinates(self):
        now = dt.datetime(2026, 9, 27, 22, 30, tzinfo=dt.timezone.utc)
        url = feeds.USTI_EMERGENCY_URL
        rss = ET.fromstring(f'''<rss><channel><lastBuildDate>Sun, 27 Sep 2026 22:20:00 +0000</lastBuildDate>
          <item><title>požár - Ústí nad Labem</title><link>{url}15685/</link>
            <description>stav: probíhající&lt;br&gt;Ústí nad Labem</description>
            <pubDate>Sun, 27 Sep 2026 22:10:00 +0000</pubDate></item>
          <item><title>dopravní nehoda - Děčín</title><link>{url}15684/</link>
            <description>stav: ukončená</description>
            <pubDate>Sun, 27 Sep 2026 22:00:00 +0000</pubDate></item>
          <item><title>old event</title><link>{url}15683/</link>
            <pubDate>Sun, 27 Sep 2026 12:00:00 +0000</pubDate></item>
        </channel></rss>''')
        payload = {'result': {'total_items': 3, 'batch_start': 0},
                   'result_items': [{'ret': [
                       {'id': 15685, 'geom': {'lon': '-760726', 'lat': '-975193'}},
                       {'id': 15684, 'geom': {'lon': '-790632', 'lat': '-990678'}},
                       {'id': 15683, 'geom': {'lon': '-760726', 'lat': '-975193'}}]}]}
        rows = feeds.parse_usti_emergencies(payload, rss, now)
        self.assertEqual([row['id'] for row in rows], ['cz:usti:fire:15685', 'cz:usti:fire:15684'])
        self.assertAlmostEqual(rows[0]['lon'], 14.0394, places=3)
        self.assertAlmostEqual(rows[0]['lat'], 50.6689, places=3)
        self.assertEqual([row['category'] for row in rows], ['fire', 'traffic'])
        self.assertIn('Ongoing report', rows[0]['detail'])
        self.assertIn('Completed report', rows[1]['detail'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds.parse_usti_emergencies(payload, rss, now + dt.timedelta(days=2))

    def test_sweden_vma_maps_only_current_public_alerts_to_municipalities(self):
        now = dt.datetime(2026, 9, 27, 18, tzinfo=dt.timezone.utc)
        alert = {'identifier': 'SRCAP20260927170000I', 'sent': '2026-09-27T19:00:00+02:00',
                 'status': 'Actual', 'scope': 'Public', 'msgType': 'Alert',
                 'info': [
                     {'language': 'sv-SE', 'event': 'Viktigt meddelande',
                      'expires': '2026-09-28T19:00:00+02:00',
                      'area': [{'areaDesc': 'Dorotea kommun', 'geocode': [
                          {'valueName': 'Kommun', 'value': '2425'}]}]},
                     {'language': 'en-US', 'event': 'Important Public Announcement',
                      'description': 'Stay indoors.', 'expires': '2026-09-28T19:00:00+02:00',
                      'web': 'https://sverigesradio.se/artikel/vma-vad-ar-det',
                      'area': [{'areaDesc': 'Dorotea kommun', 'geocode': [
                          {'valueName': 'Kommun', 'value': '2425'},
                          {'valueName': 'Kommun', 'value': '2425'},
                          {'valueName': 'Kommun', 'value': '9999'}]}]}]}
        payload = {'alerts': [alert, {**alert, 'identifier': 'cancel', 'msgType': 'Cancel'},
                              {**alert, 'identifier': 'exercise', 'status': 'Exercise'},
                              {**alert, 'identifier': 'test', 'status': 'Test'}]}
        rows = feeds.parse_sweden_vma(payload, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'se:vma:SRCAP20260927170000I:2425')
        self.assertIn('Dorotea · Important Public Announcement', rows[0]['title'])
        self.assertIn('approximate area marker', rows[0]['detail'])
        self.assertIn('Stay indoors.', rows[0]['detail'])
        self.assertEqual(rows[0]['sourceUrl'], 'https://sverigesradio.se/artikel/vma-vad-ar-det')
        self.assertEqual(feeds.parse_sweden_vma(payload, now + dt.timedelta(days=2)), [])
        self.assertEqual(len(feeds._sweden_areas()['points']), 290)
        self.assertEqual(len(feeds._sweden_areas()['counties']), 21)

    def test_sweden_vma_uses_county_or_country_when_no_municipality_given(self):
        now = dt.datetime(2026, 9, 27, 18, tzinfo=dt.timezone.utc)
        base = {'sent': '2026-09-27T17:00:00Z', 'status': 'Actual',
                'scope': 'Public', 'msgType': 'Update',
                'info': [{'language': 'sv-SE', 'event': 'VMA',
                          'expires': '2026-09-28T17:00:00Z',
                          'area': [{'geocode': [{'valueName': 'Län', 'value': '01'},
                                                {'valueName': 'Sverige', 'value': '00'}]}]}]}
        rows = feeds.parse_sweden_vma({'alerts': [{**base, 'identifier': 'county'}]}, now)
        self.assertEqual([row['id'] for row in rows], ['se:vma:county:01'])
        self.assertIn('county · approximate', rows[0]['detail'])
        national = {**base, 'identifier': 'national', 'info': [{**base['info'][0],
                    'area': [{'geocode': [{'valueName': 'Sverige', 'value': '00'}]}]}]}
        rows = feeds.parse_sweden_vma({'alerts': [national]}, now)
        self.assertEqual([row['id'] for row in rows], ['se:vma:national:00'])

    def test_nsw_uses_incident_point_and_skips_planned_burns(self):
        payload = {'features': [
            {'geometry': {'type': 'GeometryCollection', 'geometries': [
                {'type': 'Point', 'coordinates': [151.2, -33.8]}]},
             'properties': {'guid': 'https://incidents.rfs.nsw.gov.au/api/v1/incidents/123',
                            'title': 'Road incident', 'category': 'Advice', 'pubDate': '26/09/2026 3:05:00 PM',
                            'description': 'TYPE: MVA/Transport <br />FIRE: No'}},
            {'geometry': {'type': 'Point', 'coordinates': [151.3, -33.9]},
             'properties': {'guid': 'https://incidents.rfs.nsw.gov.au/api/v1/incidents/124',
                            'category': 'Planned Burn'}}]}
        items = feeds.parse_nsw(payload)
        self.assertEqual([item['id'] for item in items], ['nsw:123'])
        self.assertEqual((items[0]['lon'], items[0]['lat']), (151.2, -33.8))
        self.assertEqual(items[0]['category'], 'traffic')

    def test_victoria_rejects_old_and_planned_reports(self):
        now = dt.datetime(2026, 9, 26, 17, tzinfo=dt.timezone.utc)
        base = {'incidentNo': 1, 'longitude': 145, 'latitude': -38, 'feedType': 'incident',
                'lastUpdatedDt': now.timestamp() * 1000, 'incidentType': 'GRASS FIRE'}
        items = feeds.parse_victoria({'results': [base, {**base, 'incidentNo': 2,
            'lastUpdatedDt': (now - dt.timedelta(days=1)).timestamp() * 1000},
            {**base, 'incidentNo': 3, 'feedType': 'plannedBurn'}]}, now)
        self.assertEqual([item['id'] for item in items], ['vic:1'])

    def test_nz_cap_requires_public_current_alert_with_geometry(self):
        xml = '''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>a</identifier>
          <sent>2026-09-26T16:00:00Z</sent><status>Actual</status><msgType>Alert</msgType><scope>Public</scope>
          <info><headline>Evacuation</headline><expires>2026-09-27T00:00:00Z</expires>
            <area><polygon>-41.0,174.0 -41.2,174.2 -41.1,174.3 -41.0,174.0</polygon></area>
          </info></alert>'''
        now = dt.datetime(2026, 9, 26, 17, tzinfo=dt.timezone.utc)
        root = ET.fromstring(xml)
        self.assertEqual(len(feeds.parse_nz_cap(root, now=now)), 1)
        root.find('{urn:oasis:names:tc:emergency:cap:1.2}msgType').text = 'Cancel'
        self.assertEqual(feeds.parse_nz_cap(root, now=now), [])
        root.find('{urn:oasis:names:tc:emergency:cap:1.2}msgType').text = 'Alert'
        root.find('.//{urn:oasis:names:tc:emergency:cap:1.2}expires').clear()
        self.assertEqual(feeds.parse_nz_cap(root, now=now + dt.timedelta(days=3)), [])

    def test_england_uses_official_area_centroid(self):
        payload = {'items': [{'floodAreaID': 'ABC12', 'severity': 'Flood warning',
                              'description': 'River area', 'timeRaised': '2026-09-26T12:00:00'}]}
        with patch.object(feeds, '_england_area', return_value={'long': 0.8, 'lat': 51.7}):
            items = feeds.parse_england(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['lon'], items[0]['lat']), (0.8, 51.7))

    def test_queensland_edxl_keeps_only_unexpired_public_alerts(self):
        xml = '''<EDXLDistribution xmlns="urn:oasis:names:tc:emergency:EDXL:DE:1.0"
                 xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2"><contentObject><xmlContent>
          <embeddedXMLContent><cap:alert><cap:identifier>WARN-1</cap:identifier>
            <cap:status>Actual</cap:status><cap:scope>Public</cap:scope>
            <cap:info><cap:headline>Fire warning</cap:headline><cap:expires>2026-09-27T00:00:00Z</cap:expires>
              <cap:area><cap:circle>-27.5,152.0 5</cap:circle></cap:area>
            </cap:info></cap:alert></embeddedXMLContent></xmlContent></contentObject></EDXLDistribution>'''
        root = ET.fromstring(xml)
        now = dt.datetime(2026, 9, 26, 17, tzinfo=dt.timezone.utc)
        self.assertEqual(len(feeds.parse_queensland(root, now)), 1)
        self.assertEqual(feeds.parse_queensland(root, now + dt.timedelta(days=1)), [])

    def test_burgenland_maps_only_current_exact_municipalities(self):
        def operation(place, code='B0'):
            return (f'<div class="row operation"><div class="avatar">{code}</div>'
                    f'<div class="small"><i class="fa-location-dot"></i> {place}</div>'
                    '<div class="small"><i class="fa-alarm-clock"></i> 09:14</div></div>')
        page = ('<html><head><meta charset="utf-8"></head><body>'
                '<div id="current-pane"><div class="district-operations">'
                '<div class="col fw-bold">Mattersburg</div>'
                + operation('Forchtenstein') + operation('Unknown hamlet') + '</div></div>'
                '<div id="twelve-hours-pane">' + operation('Eisenstadt') + '</div>'
                'Zuletzt aktualisiert am 27.09.2026, 09:20 Uhr</body></html>')
        now = dt.datetime(2026, 9, 27, 7, 21, tzinfo=dt.timezone.utc)
        items = feeds.parse_burgenland(page, now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'B0 · Forchtenstein')
        self.assertEqual((items[0]['lon'], items[0]['lat']), (16.3431, 47.7111))
        self.assertEqual(items[0]['observed'], '2026-09-27T07:14:00Z')
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds.parse_burgenland(page, now + dt.timedelta(minutes=30))

    def test_upper_austria_maps_current_dispatches_without_exercises_or_addresses(self):
        now = dt.datetime(2026, 9, 27, 21, 30, tzinfo=dt.timezone.utc)
        base = {'num1': 'E260905494', 'status': 'offen', 'einsatzart': 'BRAND',
                'startzeit': 'Sun, 27 Sep 2026 23:10:00 +0200',
                'wgs84': {'lng': '14.4761', 'lat': '48.2137'},
                'einsatztyp': {'text': 'Brandmeldealarm'},
                'adresse': {'emun': 'Enns', 'default': 'Private address 123'},
                'bezirk': {'text': 'Linz-Land'}}
        payload = {'webext2': True, 'title': 'laufend',
                   'pubDate': 'Sun, 27 Sep 2026 23:29:00 +0200',
                   'einsaetze': {'0': {'einsatz': base},
                                '1': {'einsatz': {**base, 'num1': 'E260905495', 'einsatzart': 'SELBST'}},
                                '2': {'einsatz': {**base, 'num1': 'E260905496', 'status': 'abgeschlossen'}},
                                '3': {'einsatz': {**base, 'num1': 'E260905497',
                                                  'wgs84': {'lng': '20', 'lat': '48'}}}}}
        items = feeds.parse_upper_austria(payload, now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'upper-austria:E260905494')
        self.assertEqual(items[0]['category'], 'fire')
        self.assertEqual(items[0]['observed'], '2026-09-27T21:10:00Z')
        self.assertIn('Enns', items[0]['detail'])
        self.assertNotIn('Private address', str(items[0]))
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds.parse_upper_austria(payload, now + dt.timedelta(minutes=30))

    def test_iceland_cap_maps_active_polygon_and_excludes_cleared_alerts(self):
        now = dt.datetime(2026, 9, 27, 8, tzinfo=dt.timezone.utc)
        row = {'identifier': 'imo-1', 'area_id': 14, 'msgtype': 'Alert',
               'sent': '2026-09-27T07:30:00Z', 'expires': '2026-09-28T00:00:00Z',
               'headline_en': 'Landslide warning', 'description_en': 'Avoid steep slopes.',
               'polygon': ['66.0,-23.0 66.0,-22.0 65.0,-22.0 66.0,-23.0']}
        items = feeds.parse_iceland([row, {**row, 'identifier': 'imo-2', 'msgtype': 'Cancel'}], now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'iceland:imo-1:14')
        self.assertAlmostEqual(items[0]['lon'], -22.333333, places=5)
        self.assertIn('downloaded 2026-09-27', items[0]['source'])
        self.assertEqual(feeds.parse_iceland([{**row, 'expires': '2026-09-27T07:00:00Z'}], now), [])

    def test_portugal_active_incidents_check_freshness_and_use_public_fields(self):
        now = dt.datetime(2026, 9, 27, 10, 6, tzinfo=dt.timezone.utc)
        record = {'geometry': {'x': -8.61, 'y': 41.15}, 'attributes': {
            'ID_oc': 123, 'Numero': '2026000123', 'Natureza': '3103 - Mato',
            'Concelho': 'Porto', 'EstadoAgrupado': 'Em Curso', 'Operacionais': 12,
            'MeiosTerrestres': 3, 'DataDosDados': int((now + dt.timedelta(minutes=58)).timestamp() * 1000)}}
        payload = {'features': [record]}
        items = feeds.parse_portugal(payload, now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'portugal:2026000123')
        self.assertEqual((items[0]['lon'], items[0]['lat']), (-8.61, 41.15))
        self.assertEqual(items[0]['category'], 'fire')
        self.assertIn('Mato · Porto', items[0]['title'])
        self.assertNotIn('Endereco', items[0]['detail'])
        self.assertEqual(items[0]['observed'], '2026-09-27T10:04:00Z')
        record['attributes']['DataDosDados'] = int((now - dt.timedelta(hours=2)).timestamp() * 1000)
        with self.assertRaisesRegex(ValueError, 'stale'):
            feeds.parse_portugal(payload, now)

    def test_catalonia_fire_map_keeps_only_recent_non_extinguished_reports(self):
        now = dt.datetime(2026, 9, 28, 2, 45, tzinfo=dt.timezone.utc)
        props = {'GlobalID': '04b07e5c-923c-4a57-9eae-b078fb0fd9f3',
                 'DATA_ACT': int((now - dt.timedelta(hours=2)).timestamp() * 1000),
                 'ACT_DAT_FI': None, 'ACT_URGENT': 'S', 'COM_FASE': 'Controlat',
                 'MUNICIPI_DPX': 'Sant Bartomeu del Grau',
                 'TAL_DESC_ALARMA2': 'Incendi vegetació agrícola'}
        row = {'geometry': {'type': 'Point', 'coordinates': [2.1366, 41.9858]},
               'properties': props}
        payload = {'features': [row,
            {**row, 'properties': {**props, 'COM_FASE': 'Extingit'}},
            {**row, 'properties': {**props, 'DATA_ACT': int((now - dt.timedelta(days=2)).timestamp() * 1000)}},
            {**row, 'geometry': {'type': 'Point', 'coordinates': [25, 41.9858]}}]}
        items = feeds.parse_catalonia_fires(payload, now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'es:catalonia:fire:04b07e5c-923c-4a57-9eae-b078fb0fd9f3')
        self.assertEqual(items[0]['category'], 'fire')
        self.assertIn('Controlat', items[0]['detail'])
        self.assertEqual(items[0]['sourceUrl'], feeds.CATALONIA_FIRE_SOURCE)
        with self.assertRaisesRegex(ValueError, 'truncated'):
            feeds.parse_catalonia_fires({**payload, 'properties': {'exceededTransferLimit': True}}, now)


    def test_sweden_police_maps_recent_reports_at_area_centers(self):
        now = dt.datetime(2026, 9, 27, 19, tzinfo=dt.timezone.utc)
        event = {'id': 654486, 'datetime': '2026-09-27 9:00:24 +02:00',
                 'type': 'Trafikolycka, vilt', 'summary': 'Do not copy this summary',
                 'url': '/aktuellt/handelser/2026/september/27/traffic-report/',
                 'location': {'name': 'Kronobergs län', 'gps': '56.71834,14.411467'}}
        rows = feeds.parse_sweden_police([event, event,
            {**event, 'id': 2, 'type': 'Sammanfattning natt'},
            {**event, 'id': 3, 'datetime': '2026-09-25 20:00:24 +02:00'},
            {**event, 'id': 4, 'url': 'https://example.com/private'},
            {**event, 'id': 5, 'type': 'Brand', 'location': {'name': 'Skåne län', 'gps': '55.990257,13.595769'}}], now)
        self.assertEqual([row['id'] for row in rows], ['se:police:654486', 'se:police:5'])
        self.assertEqual((rows[0]['lat'], rows[0]['lon']), (56.71834, 14.411467))
        self.assertEqual([row['category'] for row in rows], ['traffic', 'fire'])
        self.assertIn('approximate area center', rows[0]['detail'])
        self.assertNotIn(event['summary'], str(rows))
        self.assertEqual(rows[0]['sourceUrl'], 'https://polisen.se' + event['url'])

    def test_sweden_police_uses_one_request_during_cooldown(self):
        previous = dict(feeds._SWEDEN_POLICE_CACHE)
        try:
            feeds._SWEDEN_POLICE_CACHE.update(until=0, items=None)
            with patch.object(feeds, '_json', return_value=[]) as fetch, \
                    patch.object(feeds.time, 'time', return_value=1000):
                self.assertEqual(feeds._sweden_police(), [])
                self.assertEqual(feeds._sweden_police(), [])
                fetch.assert_called_once_with(feeds.SWEDEN_POLICE_URL)
        finally:
            feeds._SWEDEN_POLICE_CACHE.update(previous)


if __name__ == '__main__':
    unittest.main()
