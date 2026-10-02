import datetime as dt
import io
import unittest
import urllib.error
import zipfile
import xml.etree.ElementTree as ET
from unittest.mock import patch

import hazard_feeds


class HazardFeedTests(unittest.TestCase):
    def test_gdelt_export_maps_action_geo_and_filters_event_codes(self):
        row = [''] * 61
        row[0], row[27], row[28], row[30] = 'event-1', '141', '14', '-5.0'
        row[6], row[52], row[56], row[57], row[59], row[60] = 'Example group', 'Example city', '12.5', '44.25', '20260926210000', 'https://example.org/report'
        relevant = '\t'.join(row)
        row[0], row[27], row[28] = 'ignored-event', '010', '01'
        irrelevant = '\t'.join(row)
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('sample.export.CSV', relevant + '\n' + irrelevant + '\n')
        index = b'12345 e78df41c6102cf8d2cb77c1c7aaad23d http://data.gdeltproject.org/gdeltv2/20260926210000.export.CSV.zip\n'
        responses = [io.BytesIO(index), *[io.BytesIO(zipped.getvalue()) for _ in range(13)]]
        with patch.object(hazard_feeds.urllib.request, 'urlopen', side_effect=responses):
            items = hazard_feeds._gdelt_events()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['lon'], items[0]['lat']), (44.25, 12.5))
        self.assertIn('Example group', items[0]['title'])
        self.assertEqual(items[0]['category'], 'Protest')
        self.assertEqual(items[0]['eventType'], 'protest')

    def test_gdelt_unrest_excludes_broad_sanctions_and_assaults(self):
        rows = []
        for code in ('15', '16', '17', '18', '19', '20'):
            row = [''] * 61
            row[0], row[28], row[6] = f'event-{code}', code, 'Example group'
            row[51], row[52], row[56], row[57] = '4', 'Example city', '12.5', '44.25'
            row[59], row[60] = '20260926210000', 'https://example.org/report'
            rows.append('\t'.join(row))
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('sample.export.CSV', '\n'.join(rows) + '\n')
        index = b'12345 abc http://data.gdeltproject.org/gdeltv2/20260926210000.export.CSV.zip\n'
        responses = [io.BytesIO(index), *[io.BytesIO(zipped.getvalue()) for _ in range(13)]]
        with patch.object(hazard_feeds.urllib.request, 'urlopen', side_effect=responses):
            items = hazard_feeds._gdelt_events()['items']
        self.assertEqual({item['eventType'] for item in items}, {'military', 'conflict'})
        self.assertEqual({item['id'] for item in items}, {'gdelt:event-15', 'gdelt:event-19', 'gdelt:event-20'})

    def test_cyclones_use_latest_observed_position_and_track(self):
        recent = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        payload = {'events': [{
            'id': 'storm-1', 'title': 'Typhoon Example', 'sources': [
                {'id': 'EO', 'url': 'https://science.nasa.gov/example'},
                {'id': 'JTWC', 'url': 'https://www.metoc.navy.mil/jtwc/products/wp2526.tcw'},
            ],
            'geometry': [
                {'type': 'Point', 'coordinates': [130, 10], 'date': '2026-01-01T00:00:00Z'},
                {'type': 'Point', 'coordinates': [131, 11], 'date': recent, 'magnitudeValue': 65, 'magnitudeUnit': 'kts'},
            ],
        }]}
        with patch.object(hazard_feeds, '_get_json', return_value=payload):
            items = hazard_feeds._cyclones()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['lon'], items[0]['lat']), (131, 11))
        self.assertEqual(items[0]['windKt'], 65)
        self.assertEqual(items[0]['track'], [(130, 10), (131, 11)])
        self.assertEqual(items[0]['sourceUrl'], 'https://www.metoc.navy.mil/jtwc/products/wp2526.tcw')

    def test_earthquakes_reject_invalid_coordinates(self):
        payload = {'features': [
            {'id': 'ok', 'geometry': {'coordinates': [170, -20, 12]}, 'properties': {'mag': 5.1, 'time': 123}},
            {'id': 'bad', 'geometry': {'coordinates': [190, -20, 12]}, 'properties': {'mag': 5.1}},
        ]}
        with patch.object(hazard_feeds, '_get_json', return_value=payload):
            items = hazard_feeds._earthquakes()['items']
        self.assertEqual([item['id'] for item in items], ['ok'])
        self.assertEqual(items[0]['depthKm'], 12)

    def test_fires_include_only_current_events(self):
        def event(event_id, current):
            return {'geometry': {'coordinates': [33, -18]}, 'properties': {
                'eventid': event_id, 'name': 'Forest fires in Mozambique',
                'iscurrent': current, 'severitydata': {'severity': 50, 'severityunit': 'ha'},
                'url': {'report': 'https://www.gdacs.org/report.aspx'},
            }}
        with patch.object(hazard_feeds, '_get_json', return_value={'features': [event(1, 'true'), event(2, 'false')]}):
            items = hazard_feeds._fires()['items']
        self.assertEqual([item['id'] for item in items], ['1'])
        self.assertEqual(items[0]['areaHa'], 50)

    def test_gdacs_floods_and_volcanoes_keep_distinct_recency_rules(self):
        def event(event_id, current):
            return {'geometry': {'coordinates': [125, 12]}, 'properties': {
                'eventid': event_id, 'name': f'Event {event_id}', 'iscurrent': current,
                'country': 'Philippines', 'alertlevel': 'Orange',
                'datemodified': '2026-09-25T12:00:00',
                'url': {'report': 'https://www.gdacs.org/report.aspx'},
            }}
        payload = {'features': [event(1, 'true'), event(2, 'false')]}
        with patch.object(hazard_feeds, '_get_json', return_value=payload):
            floods = hazard_feeds._gdacs_events('FL', 90, current_only=True)['items']
            volcanoes = hazard_feeds._gdacs_events('VO', 30)['items']
        self.assertEqual([item['id'] for item in floods], ['1'])
        self.assertEqual([item['id'] for item in volcanoes], ['1', '2'])
        self.assertEqual(volcanoes[1]['active'], False)
        self.assertEqual(floods[0]['alert'], 'Orange')

    def test_nws_alerts_map_polygons_and_zone_only_advisories(self):
        polygon = {'type': 'Polygon', 'coordinates': [[[-76, 36], [-75, 36], [-75, 37], [-76, 37], [-76, 36]]]}
        def alert(identifier, event, geometry, status='Actual', zones=None):
            return {'id': identifier, 'geometry': geometry, 'properties': {
                'id': identifier, '@id': f'https://api.weather.gov/alerts/{identifier}',
                'event': event, 'status': status, 'messageType': 'Alert', 'severity': 'Moderate',
                'affectedZones': zones or [], 'areaDesc': 'Test coast',
                'ends': '2026-09-27T00:00:00-04:00',
            }}
        payload = {'features': [
            alert('polygon', 'Flood Warning', polygon),
            alert('zone', 'Small Craft Advisory', None, zones=['https://api.weather.gov/zones/forecast/ANZ533']),
            alert('test', 'Test Message', None, status='Test', zones=['https://api.weather.gov/zones/forecast/ANZ533']),
        ]}
        with patch.object(hazard_feeds, '_get_json', return_value=payload), patch.object(
            hazard_feeds, '_nws_zones', return_value={'ANZ533': [-76.3, 38.4, 'Chesapeake Bay']}
        ):
            result = hazard_feeds._nws_alerts()
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['unmapped'], 0)
        self.assertEqual(result['items'][0]['geometry'], polygon)
        self.assertEqual(result['items'][1]['locationKind'], 'zone centroid')
        self.assertEqual((result['items'][1]['lon'], result['items'][1]['lat']), (-76.3, 38.4))

    def test_canadian_alerts_keep_current_published_polygons(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat()
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat()
        polygon = {'type': 'Polygon', 'coordinates': [[[-64, 45], [-63, 45], [-63, 46], [-64, 46], [-64, 45]]]}
        def alert(identifier, expires):
            return {'id': identifier, 'geometry': polygon, 'properties': {
                'alert_short_name_en': 'Rainfall warning', 'expiration_datetime': expires,
                'feature_name_en': 'Test county', 'province': 'NS', 'risk_colour_en': 'red',
            }}
        with patch.object(hazard_feeds, '_get_json', return_value={'features': [alert('live', future), alert('old', past)], 'links': []}):
            items = hazard_feeds._canada_alerts()
        self.assertEqual([item['id'] for item in items], ['ca:live'])
        self.assertEqual(items[0]['geometry'], polygon)
        self.assertEqual(items[0]['source'], 'Environment and Climate Change Canada')

    def test_new_zealand_cap_polygon_uses_latitude_longitude_order(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat()
        feed = ET.fromstring('<rss><channel><item><link>https://alerts.metservice.com/cap/alert?id=live</link></item></channel></rss>')
        cap = ET.fromstring(f'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
            <identifier>live</identifier><status>Actual</status><msgType>Alert</msgType>
            <info><headline>Strong wind</headline><severity>Moderate</severity><expires>{future}</expires>
            <area><areaDesc>Test region</areaDesc><polygon>-41,174 -41,175 -40,175 -40,174 -41,174</polygon></area>
            </info></alert>''')
        with patch.object(hazard_feeds, '_get_xml', return_value=feed), patch.object(hazard_feeds, '_nz_cap_alert', return_value=cap):
            items = hazard_feeds._new_zealand_alerts()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['geometry']['coordinates'][0][0][0], [174, -41])
        self.assertEqual(items[0]['country'], 'New Zealand')

    def test_world_alerts_keep_available_country_when_other_feed_fails(self):
        with patch.object(hazard_feeds, '_canada_alerts', return_value=[{'id': 'ca:1'}]), patch.object(
            hazard_feeds, '_new_zealand_alerts', side_effect=RuntimeError('offline')
        ), patch.object(hazard_feeds, '_norway_alerts', return_value=[]
        ), patch.object(hazard_feeds, '_ireland_alerts', return_value=[]
        ), patch.object(hazard_feeds, '_germany_alerts', return_value=[]
        ), patch.object(hazard_feeds, '_azores_alerts', return_value=[]
        ), patch.object(hazard_feeds, '_pagasa_alerts', return_value=[]
        ), patch.object(hazard_feeds, '_sachet_alerts', return_value=[]
        ):
            result = hazard_feeds._world_alerts()
        self.assertEqual(result['items'], [{'id': 'ca:1'}])
        self.assertEqual(result['unavailable'], ['New Zealand'])

    def test_sachet_cap_filters_expired_and_superseded_alerts_and_maps_area(self):
        now = dt.datetime(2026, 10, 2, 20, tzinfo=dt.timezone.utc)
        base = hazard_feeds._SACHET_PATH
        def cap(rss_id, identifier, expires, references=''):
            return ET.fromstring(f'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
              <identifier>{identifier}</identifier><status>Actual</status><msgType>Update</msgType>
              <scope>Public</scope><sent>2026-10-02T19:00:00Z</sent><references>{references}</references>
              <info><language>en-IN</language><category>Met</category><event>Flood</event>
                <expires>{expires}</expires><headline>River flood warning</headline>
                <severity>Severe</severity><area><areaDesc>Test district</areaDesc></area>
                <parameter><valueName>Polygon URL</valueName>
                  <value>{base}FetchPolygonXMLFile?identifier={rss_id}</value></parameter>
              </info></alert>''')
        old = cap('1790970564204007', 'IN-old', '2026-10-03T00:00:00Z')
        current = cap('1790970564204008', 'IN-new', '2026-10-03T00:00:00Z',
                      'agency,IN-old,2026-10-02T18:00:00Z')
        expired = cap('1790970564204009', 'IN-expired', '2026-10-02T19:00:00Z')
        candidates = hazard_feeds._parse_sachet_caps([
            ('1790970564204007', old), ('1790970564204008', current),
            ('1790970564204009', expired)], now)
        self.assertEqual([item['id'] for item in candidates], ['in:sachet:IN-new'])
        area = ET.fromstring('''<alert><polygon>10,75 11,75 11,76 10,76 10,75</polygon>
          <polygon>13,79 14,79 14,81 13,81 13,79</polygon></alert>''')
        self.assertEqual(hazard_feeds._sachet_polygon_point(area), [80, 13.5])

    def test_sachet_cap_revalidates_with_etag_and_uses_cached_xml_for_304(self):
        identifier = '1790970564204007'
        body = b'<alert><identifier>cached</identifier></alert>'
        class Response:
            headers = {'ETag': '"version-1"'}
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return body
        requests = []
        def urlopen(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                return Response()
            raise urllib.error.HTTPError(request.full_url, 304, 'Not Modified', {}, None)
        with patch.dict(hazard_feeds._SACHET_XML_CACHE, {}, clear=True), \
                patch.object(hazard_feeds.urllib.request, 'urlopen', side_effect=urlopen):
            first = hazard_feeds._sachet_xml(identifier)
            hazard_feeds._SACHET_XML_CACHE[(identifier, False)]['checked'] = -1000
            second = hazard_feeds._sachet_xml(identifier)
        self.assertEqual(first.findtext('identifier'), second.findtext('identifier'))
        self.assertEqual(requests[1].get_header('If-none-match'), '"version-1"')

    def test_pagasa_cap_updates_clear_prior_warning_and_keep_current_polygon(self):
        now = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc)
        old_id = '11111111-1111-1111-1111-111111111111'
        new_id = '22222222-2222-2222-2222-222222222222'
        cancel_id = '33333333-3333-3333-3333-333333333333'

        def cap(identifier, response='', references='', expires='2026-10-03T00:00:00Z'):
            return ET.fromstring(f'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
              <identifier>{identifier}</identifier><sent>2026-10-02T11:00:00Z</sent>
              <status>Actual</status><msgType>Update</msgType><scope>Public</scope>
              <references>{references}</references><info><event>Flood Advisory</event>
              <responseType>{response}</responseType><expires>{expires}</expires>
              <area><areaDesc>Test province</areaDesc>
              <polygon>8,123 8,124 9,124 9,123 8,123</polygon></area>
              </info></alert>''')

        old = cap(old_id)
        update = cap(new_id, references=f'PAGASA-DOST,{old_id},2026-10-02T10:00:00Z')
        rows = hazard_feeds._parse_pagasa_caps([('https://example.test/old', old),
                                                 ('https://example.test/new', update)], now)
        self.assertEqual([row['id'] for row in rows], [f'ph:pagasa:{new_id}:0'])
        self.assertEqual(rows[0]['geometry']['coordinates'][0][0][0], [123.0, 8.0])
        final = cap(cancel_id, response='AllClear',
                    references=f'PAGASA-DOST,{new_id},2026-10-02T11:00:00Z')
        self.assertEqual(hazard_feeds._parse_pagasa_caps([
            ('https://example.test/old', old), ('https://example.test/new', update),
            ('https://example.test/final', final)], now), [])
        self.assertEqual(hazard_feeds._parse_pagasa_caps([
            ('https://example.test/old', cap(old_id, expires='2026-10-01T00:00:00Z'))], now), [])
        self.assertEqual(hazard_feeds._pagasa_cap_url(
            'https://121.58.193.10/output/gfa/11111111-1111-1111-1111-111111111111.cap'),
            'https://publicalert.pagasa.dost.gov.ph/output/gfa/11111111-1111-1111-1111-111111111111.cap')
        self.assertIsNone(hazard_feeds._pagasa_cap_url('http://localhost/private'))

    def test_azores_alerts_show_only_current_island_group_windows(self):
        now = dt.datetime(2026, 9, 28, 4, tzinfo=dt.timezone.utc)
        def window(start, end, color='2'):
            return {'dia_inicio': start[:10], 'hora_inicio': start[11:],
                    'dia_fim': end[:10], 'hora_fim': end[11:],
                    'codigo_cor': color, 'categoria': 'Rain', 'texto': 'Heavy rain'}
        alerts = [{'idalerta': 3587, 'codigo_tipo': 1, 'titulo_aviso': 'Weather warning',
                   'g_ocidental': {'precipitacao': [window('2026-09-27T18:00', '2026-09-28T06:00')]},
                   'g_central': {'precipitacao': [window('2026-09-27T21:00', '2026-09-28T12:00')]},
                   'g_oriental': {'precipitacao': [window('2026-09-28T05:00', '2026-09-28T15:00')]}},
                  {'idalerta': 3586, 'codigo_tipo': 2, 'g_central': {
                      'precipitacao': [window('2026-09-27T21:00', '2026-09-28T12:00')]}},
                  {'idalerta': 3570, 'codigo_tipo': 1, 'g_central': {
                      'precipitacao': [window('2026-09-27T21:00', '2026-09-28T12:00', '1')]}}]
        items = hazard_feeds._parse_azores_alerts(alerts, now)
        self.assertEqual([item['id'] for item in items],
                         ['pt:azores:3587:ocidental', 'pt:azores:3587:central'])
        self.assertEqual(items[1]['severity'], 'Severe')
        self.assertEqual(items[1]['locationKind'], 'island-group representative point')
        self.assertEqual(items[1]['ends'], '2026-09-28T12:00:00Z')
        self.assertEqual(hazard_feeds._parse_azores_alerts(alerts, now + dt.timedelta(days=2)), [])
        winter = [{'idalerta': 100, 'codigo_tipo': 1, 'g_central': {'vento': [
            window('2026-01-27T11:30', '2026-01-27T13:00')]}}]
        self.assertEqual(hazard_feeds._parse_azores_alerts(
            winter, dt.datetime(2026, 1, 27, 12, tzinfo=dt.timezone.utc)), [])

    def test_dwd_status_archive_maps_current_polygons_and_accepts_empty_zip(self):
        now = dt.datetime.now(dt.timezone.utc)
        future = (now + dt.timedelta(hours=2)).isoformat()
        xml = f'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
          <identifier>test-123</identifier><sent>{now.isoformat()}</sent>
          <status>Actual</status><msgType>Update</msgType>
          <info><language>en</language><headline>Official WARNING of FOG</headline>
          <severity>Minor</severity><expires>{future}</expires>
          <instruction>Drive carefully.</instruction><area><areaDesc>Kreis Test</areaDesc>
          <polygon>48.0,11.0 48.1,11.0 48.1,11.1 48.0,11.0</polygon>
          </area></info></alert>'''
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as zipped:
            zipped.writestr('warning.ENG.xml', xml)
        items = hazard_feeds._parse_dwd_alerts_zip(archive.getvalue(), now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['country'], 'Germany')
        self.assertEqual(items[0]['geometry']['coordinates'][0][0][0], [11, 48])
        self.assertEqual(items[0]['area'], 'Kreis Test')
        self.assertEqual(hazard_feeds._parse_dwd_alerts_zip(
            archive.getvalue(), now + dt.timedelta(hours=3)), [])
        empty = io.BytesIO()
        with zipfile.ZipFile(empty, 'w'):
            pass
        self.assertEqual(hazard_feeds._parse_dwd_alerts_zip(empty.getvalue(), now), [])

    def test_ireland_alerts_use_current_county_regions_and_original_text(self):
        now = dt.datetime.now(dt.timezone.utc)
        future = (now + dt.timedelta(hours=2)).isoformat()
        past = (now - dt.timedelta(hours=2)).isoformat()
        warning = {'capId': 'cap.1', 'headline': 'Wind warning',
                   'description': 'Keep clear of exposed areas.', 'regions': ['EI07', 'EI12'],
                   'severity': 'Moderate', 'issued': now.isoformat(), 'expiry': future}
        with patch.object(hazard_feeds, '_get_json', return_value=[
            warning, {**warning, 'capId': 'cap.2', 'expiry': past},
            {**warning, 'capId': 'cap.3', 'regions': ['EI07', 'UNKNOWN']},
        ]):
            items = hazard_feeds._ireland_alerts()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['regions'], ['EI07', 'EI12'])
        self.assertEqual(items[0]['area'], 'Dublin, Kildare')
        self.assertEqual(items[0]['title'], warning['headline'])
        self.assertEqual(items[0]['advice'], warning['description'])
        self.assertEqual(items[0]['sourceUrl'], 'https://cap.met.ie//cap.1.xml')
        self.assertEqual(items[0]['locationKind'], 'county point')

    def test_norway_alerts_map_current_english_cap_polygon(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat()
        feed = ET.fromstring('''<rss><channel><item><guid>safe.123</guid></item>
            <item><guid>../../unsafe</guid></item></channel></rss>''')
        cap = ET.fromstring(f'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
            <identifier>safe.123</identifier><status>Actual</status><msgType>Alert</msgType>
            <info><language>no</language><headline>Norsk tekst</headline></info>
            <info><language>en-GB</language><headline>Strong wind</headline>
              <severity>Moderate</severity><expires>{future}</expires>
              <area><areaDesc>Test region</areaDesc>
                <polygon>62,5 62,6 63,6 63,5 62,5</polygon></area></info></alert>''')
        with patch.object(hazard_feeds, '_get_xml', return_value=feed), patch.object(
            hazard_feeds, '_norway_cap_alert', return_value=cap) as fetch:
            items = hazard_feeds._norway_alerts()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'Strong wind')
        self.assertEqual(items[0]['geometry']['coordinates'][0][0][0], [5, 62])
        self.assertEqual(items[0]['country'], 'Norway')
        fetch.assert_called_once_with('safe.123')


if __name__ == '__main__':
    unittest.main()
