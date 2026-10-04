import datetime as dt
import unittest

from japan_hiroshima_roads import _segments, parse_restrictions


UTC = dt.timezone.utc


def restriction(ident, **changes):
    row = {
        'id': ident, 'lat': '34.5', 'lon': '132.5',
        'rosenname': '県道 テスト線', 'kiseinaiyo': '通行止め',
        'kiseireason': '道路改良工事', 'kisei_hour': '終日',
        'start_date': '2026/10/01 00:00', 'end_date': '2026/10/31 23:59',
        'kukanroot': 'LINESTRING(132.49 34.49, 132.5 34.5, 132.51 34.51)',
    }
    row.update(changes)
    return row


class HiroshimaRoadTests(unittest.TestCase):
    def test_filters_dates_classifies_and_maps_segments(self):
        rows = [restriction(f'work-{n}') for n in range(10)]
        rows.append(restriction('incident', kiseireason='土砂崩れ'))
        rows.append(restriction('expired', end_date='2026/10/02 00:00'))
        rows.append(restriction('future', start_date='2026/10/10 00:00'))
        rows.append(restriction('elsewhere', lon='140.0'))
        rows.append(restriction('work-0'))
        now = dt.datetime(2026, 10, 4, 12, tzinfo=UTC)
        features = parse_restrictions({'results': rows}, now)
        self.assertEqual(len(features), 11)
        self.assertEqual(sum(f['properties']['layer'] == 'construction' for f in features), 10)
        self.assertEqual(sum(f['properties']['layer'] == 'incidents' for f in features), 1)
        self.assertEqual(features[0]['properties']['road_segments'],
                         [[[132.49, 34.49], [132.5, 34.5], [132.51, 34.51]]])
        self.assertEqual(features[0]['properties']['road_segment_bounds'],
                         [132.49, 34.49, 132.51, 34.51])

    def test_rejects_unexpected_feed_and_bad_geometry(self):
        now = dt.datetime(2026, 10, 4, 12, tzinfo=UTC)
        with self.assertRaises(ValueError):
            parse_restrictions({'results': []}, now)
        with self.assertRaises(ValueError):
            parse_restrictions({'results': [restriction('one')]}, now)
        self.assertEqual(_segments('LINESTRING(132.5 34.5, 999 34.6)'), [])
        self.assertEqual(_segments('MULTILINESTRING((132.5 34.5, 132.6 34.6))'), [])

    def test_sentinel_start_date_is_not_shown_as_1900(self):
        rows = [restriction(f'open-{n}', start_date='1900/01/01 00:00', end_date='未定')
                for n in range(10)]
        features = parse_restrictions({'results': rows}, dt.datetime(2026, 10, 4, 12, tzinfo=UTC))
        self.assertNotIn('1900', features[0]['properties']['detail'])


if __name__ == '__main__':
    unittest.main()
