import datetime as dt
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import international_emergency as feeds


class InternationalEmergencyTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
