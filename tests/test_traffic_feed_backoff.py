import unittest
from unittest import mock

import proxy


class TrafficFeedBackoffTests(unittest.TestCase):
    def test_failed_feed_waits_before_retrying_upstream(self):
        cache = proxy.SharedResponseCache(db_path=None)
        calls = []

        def load():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError('provider denied request')
            return b'{"item2":[]}', 'application/json'

        try:
            with mock.patch.object(proxy.time, 'time', return_value=100):
                with self.assertRaises(ValueError):
                    cache.get_or_load('traffic', load, ttl=30, stale_ttl=30, failure_ttl=300)
            with mock.patch.object(proxy.time, 'time', return_value=101):
                with self.assertRaises(proxy.UpstreamCoolingDown):
                    cache.get_or_load('traffic', load, ttl=30, stale_ttl=30, failure_ttl=300)
            self.assertEqual(len(calls), 1)
            with mock.patch.object(proxy.time, 'time', return_value=401):
                body, _, status = cache.get_or_load('traffic', load, ttl=30, stale_ttl=30, failure_ttl=300)
            self.assertEqual((body, status), (b'{"item2":[]}', 'MISS'))
            self.assertEqual(len(calls), 2)
        finally:
            cache.close()


if __name__ == '__main__':
    unittest.main()
