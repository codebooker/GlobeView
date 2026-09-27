import datetime as dt
import unittest

import cyclone_guidance


class CycloneGuidanceTests(unittest.TestCase):
    def test_atcf_parser_keeps_latest_cycle_and_distinct_track_models(self):
        now = dt.datetime(2026, 9, 26, 21, tzinfo=dt.timezone.utc)
        rows = []
        for cycle in ('2026092600', '2026092618'):
            for code in ('AVNI', 'CMCI', 'OFCL'):
                for tau, lon in ((0, '500W'), (12, '510W'), (24, '520W')):
                    rows.append(f'AL, 07, {cycle}, 03, {code}, {tau}, 150N, {lon}, 40, 1000')
        rows.append('AL, 07, 2026092618, 03, AVNI, 24, 520W, 150N')  # invalid lat/lon order
        result = cyclone_guidance.parse_adeck('\n'.join(rows), now)
        self.assertEqual(result['cycle'], '2026-09-26T18:00:00Z')
        self.assertEqual([track['model'] for track in result['tracks']],
                         ['GFS', 'Canadian', 'NHC official forecast'])
        self.assertEqual(result['tracks'][0]['points'], [[-50, 15], [-51, 15], [-52, 15]])

    def test_stale_forecasts_are_not_shown(self):
        row = 'AL, 07, 2026092400, 03, AVNI, 24, 150N, 500W'
        now = dt.datetime(2026, 9, 26, 21, tzinfo=dt.timezone.utc)
        self.assertIsNone(cyclone_guidance.parse_adeck('\n'.join([row] * 3), now))

    def test_western_pacific_prefers_cycle_with_distinct_models_and_ensemble(self):
        now = dt.datetime(2026, 9, 26, 21, tzinfo=dt.timezone.utc)
        rows = []
        for cycle, codes in (('2026092618', ('AEMN', 'AP01', 'AP02', 'AP03', 'AP04', 'AP05')),
                             ('2026092612', ('CMC', 'UKM', 'NGX', 'AEMN', 'AP01', 'AP02'))):
            for code in codes:
                for tau, lon in ((0, '1384E'), (12, '1400E'), (24, '1420E')):
                    rows.append(f'WP, 25, {cycle}, 03, {code}, {tau}, 160N, {lon}, 40, 1000')
        result = cyclone_guidance.parse_adeck('\n'.join(rows), now, basin='wp')
        self.assertEqual(result['cycle'], '2026-09-26T12:00:00Z')
        self.assertEqual([track['model'] for track in result['tracks'] if track['kind'] == 'model'],
                         ['Canadian', 'UKMET', 'NAVGEM'])
        self.assertEqual(len([track for track in result['tracks'] if track['kind'] == 'ensemble']), 2)
        self.assertIn('GEFS mean', [track['model'] for track in result['tracks']])

    def test_navy_storm_ids_resolve_for_nhc_and_western_pacific_basins(self):
        self.assertEqual(cyclone_guidance._resolve_atcf_id({
            'title': 'Hurricane Example', 'sourceUrl': 'https://www.metoc.navy.mil/jtwc/products/ep1726.tcw',
        }), 'EP172026')
        self.assertEqual(cyclone_guidance._resolve_atcf_id({
            'title': 'Typhoon Example', 'sourceUrl': 'https://www.metoc.navy.mil/jtwc/products/wp2526.tcw',
        }), 'WP252026')


if __name__ == '__main__':
    unittest.main()
