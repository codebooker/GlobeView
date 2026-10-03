import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import tajikistan_alerts as feed


NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc)
TEXT = ('5 октября ожидаются сильные осадки. '
        '4 октября текущего года в Согдийской области ожидаются сели. '
        '5 и 6 октября в горных районах ожидаются сели. '
        'С второй половины дня 4 октября по 6 октября в Душанбе ожидается пыльная буря.')


def document(text=TEXT):
    return '<h2>Предупреждение!</h2><p>' + text + '</p>'


class TajikistanAlertsTests(unittest.TestCase):
    def test_current_regional_warning_uses_honest_capital_reference_and_source_dates(self):
        item, = feed.parse_alerts(document(), NOW)
        self.assertEqual(item['country'], 'Tajikistan')
        self.assertEqual(item['locationKind'], 'national advisory reference point')
        self.assertNotIn('geometry', item)
        self.assertNotIn('observed', item)  # No publication time in the source.
        self.assertEqual(item['starts'], '2026-10-04T00:00:00+05:00')
        self.assertEqual(item['ends'], '2026-10-07T00:00:00+05:00')
        self.assertEqual(item['sourceUrl'], feed.SOURCE)
        self.assertEqual(item['advice'], 'Bulletin covers precipitation, mudflows, dust.'
                         ' See source for affected areas and advice.')
        self.assertLess(len(item['advice']), 180)
        self.assertEqual(feed.parse_alerts(document(), NOW)[0]['id'], item['id'])

    def test_expiry_respects_tajikistan_midnight(self):
        self.assertTrue(feed.parse_alerts(document(), dt.datetime(2026, 10, 6, 18, 59,
                                                                  tzinfo=dt.timezone.utc)))
        self.assertEqual(feed.parse_alerts(document(), dt.datetime(2026, 10, 6, 19,
                                                                  tzinfo=dt.timezone.utc)), [])

    def test_old_undated_invalid_and_distant_notices_are_not_live_alerts(self):
        for text in [TEXT.replace('текущего года', '2025 года'),
                     TEXT.replace('текущего года', ''),
                     '31 февраля текущего года ожидаются осадки.',
                     '1 сентября текущего года ожидается ветер.',
                     '20 октября текущего года ожидается ветер.',
                     '1 октября и 15 октября текущего года ожидаются осадки.']:
            with self.subTest(text=text):
                self.assertEqual(feed.parse_alerts(document(text), NOW), [])
        self.assertTrue(feed.parse_alerts(document(TEXT.replace('текущего года', '2026 года')), NOW))

    def test_no_warning_and_changed_or_excessive_warning_layout(self):
        self.assertEqual(feed.parse_alerts('<h2>Новости</h2><p>' + TEXT + '</p>', NOW), [])
        with self.assertRaises(ValueError):
            feed.parse_alerts(document() + document(), NOW)
        with self.assertRaises(ValueError):
            feed.parse_alerts(document('x' * 5001), NOW)

    def test_fetch_bounds_content_type_and_fixed_source(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.url = feed.SOURCE
        response.headers = {'Content-Type': 'text/html; charset=utf-8'}
        response.read.return_value = document().encode()
        with patch.object(feed._OPENER, 'open', return_value=response) as opened, \
                patch.object(feed, 'parse_alerts', return_value=[]) as parsed:
            self.assertEqual(feed.alerts(), [])
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, feed.SOURCE)
            self.assertEqual(opened.call_args.kwargs['timeout'], 15)
            parsed.assert_called_once()
            response.read.assert_called_once_with(1_000_001)
            response.url = 'https://other.invalid/'
            with self.assertRaises(ValueError):
                feed.alerts()
            response.url = feed.SOURCE
            response.headers['Content-Type'] = 'application/json'
            with self.assertRaises(ValueError):
                feed.alerts()
            response.headers['Content-Type'] = 'text/html'
            response.read.return_value = b'x' * 1_000_001
            with self.assertRaises(ValueError):
                feed.alerts()
        with self.assertRaises(ValueError):
            feed._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid/')


if __name__ == '__main__':
    unittest.main()
