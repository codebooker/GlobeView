import unittest
import csv
import datetime as dt
import io
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

import international_infrastructure as feeds


NOW = 1790445600  # 2026-09-26 UTC


class InfrastructureTests(unittest.TestCase):
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
