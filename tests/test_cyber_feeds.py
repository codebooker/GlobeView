import json
import unittest
from unittest.mock import patch

import cyber_feeds


class CyberFeedTests(unittest.TestCase):
    def test_scans_only_map_public_unique_sources(self):
        feed = b'23.94.68.19\thost.example\n10.0.0.1\tprivate\n23.94.68.19\tduplicate\n89.248.163.109\tsecond.example\n'
        location = {'lat': 42.0, 'lon': -78.0, 'country': 'US', 'city': 'Buffalo'}
        with patch.object(cyber_feeds, '_get', return_value=feed), patch.object(cyber_feeds, '_geolocate', return_value=location):
            snapshot = cyber_feeds._scans()
            items = snapshot['items']
        self.assertEqual([item['ip'] for item in items], ['23.94.68.19', '89.248.163.109'])
        self.assertEqual([item['rank'] for item in items], [1, 2])
        self.assertEqual(snapshot['listed'], 2)

    def test_scans_use_published_top_100_limit(self):
        feed = '\n'.join(f'11.0.0.{index}\thost-{index}' for index in range(1, 102)).encode()
        location = {'lat': 48.0, 'lon': 2.0, 'country': 'FR', 'city': 'Paris'}
        with patch.object(cyber_feeds, '_get', return_value=feed), patch.object(cyber_feeds, '_geolocate', return_value=location):
            snapshot = cyber_feeds._scans()
        self.assertEqual(snapshot['listed'], 100)
        self.assertEqual(len(snapshot['items']), 100)

    def test_sparse_static_feed_uses_fuller_sans_api(self):
        static = b'23.94.68.19\thost.example\n'
        api = json.dumps([
            {'source': '89.248.163.109', 'reports': 120, 'targets': 3},
            {'source': '23.94.68.19', 'reports': 80, 'targets': 2},
        ]).encode()
        location = {'lat': 42.0, 'lon': -78.0, 'country': 'US', 'city': 'Buffalo'}
        with patch.object(cyber_feeds, '_get', side_effect=[static, api]), patch.object(cyber_feeds, '_geolocate', return_value=location):
            snapshot = cyber_feeds._scans()
        self.assertEqual(snapshot['feedType'], 'api')
        self.assertEqual(snapshot['listed'], 2)
        self.assertEqual([item['reports'] for item in snapshot['items']], [120, 80])

    def test_kev_sorts_by_catalog_date(self):
        payload = {'dateReleased': '2026-09-26', 'vulnerabilities': [
            {'cveID': 'CVE-2025-10000', 'dateAdded': '2025-01-01', 'vendorProject': 'Old'},
            {'cveID': 'CVE-2026-20000', 'dateAdded': '2026-09-25', 'vendorProject': 'New'},
        ]}
        with patch.object(cyber_feeds, '_get', return_value=json.dumps(payload).encode()):
            items = cyber_feeds._kev()['items']
        self.assertEqual([item['cve'] for item in items], ['CVE-2026-20000', 'CVE-2025-10000'])
        self.assertEqual(items[0]['vendor'], 'New')

    def test_outbreaks_keep_only_official_alert_links_and_sort_by_date(self):
        feed = b'''<rss><channel>
          <item><title>Older alert</title><link>https://fortiguard.fortinet.com/outbreak-alert/older</link><pubDate>Mon, 01 Jun 2026 00:00:00 +0000</pubDate></item>
          <item><title>Impostor</title><link>https://fortiguard.fortinet.com.evil.example/alert</link><pubDate>Fri, 25 Sep 2026 00:00:00 +0000</pubDate></item>
          <item><title>Newer alert</title><link>https://fortiguard.fortinet.com/outbreak-alert/newer</link><pubDate>Tue, 22 Sep 2026 00:00:00 +0000</pubDate></item>
        </channel></rss>'''
        with patch.object(cyber_feeds, '_get', return_value=feed):
            items = cyber_feeds._outbreaks()['items']
        self.assertEqual([item['title'] for item in items], ['Newer alert', 'Older alert'])
        self.assertEqual(items[0]['published'], '2026-09-22T00:00:00Z')


if __name__ == '__main__':
    unittest.main()
