import datetime as dt
import json
import unittest
from unittest import mock

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

    def test_nhc_master_index_resolves_named_storms_and_is_shared(self):
        year = dt.datetime.now(dt.timezone.utc).year
        index = '\n'.join([
            f' HANNA, AL, L, , , , , 08, {year}, TS, O, , , , , , , , WARNING, 3, AL08{year}',
            f' HANNA, AL, L, , , , , 08, {year - 6}, TS, O, , , , , , , , ARCHIVE, , AL08{year - 6}',
            f' RACHEL, EP, E, , , , , 18, {year}, HU, O, , , , , , , , WARNING, 2, EP18{year}',
        ])
        with mock.patch.dict(cyclone_guidance._index_cache, {}, clear=True), \
                mock.patch.object(cyclone_guidance, '_read', return_value=index) as read:
            self.assertEqual(cyclone_guidance._resolve_atcf_id({
                'title': 'Tropical Storm Hanna',
                'sourceUrl': f'https://www.nhc.noaa.gov/archive/{year}/HANNA.shtml',
            }), f'AL08{year}')
            self.assertEqual(cyclone_guidance._resolve_atcf_id({
                'title': 'Hurricane Rachel',
            }), f'EP18{year}')
            read.assert_called_once_with(f'{cyclone_guidance.NHC_BASE}/index/storm_list.txt', 1024 * 1024)

    def test_nhc_archive_year_does_not_match_a_new_season_or_ambiguous_name(self):
        index = ' EXAMPLE, AL, L, AL012025\n EXAMPLE, EP, E, EP012026\n DUPLICATE, AL, L, AL022026\n DUPLICATE, EP, E, EP022026'
        with mock.patch.dict(cyclone_guidance._index_cache, {}, clear=True), \
                mock.patch.object(cyclone_guidance, '_read', return_value=index):
            self.assertEqual(cyclone_guidance._resolve_atcf_id({
                'title': 'Hurricane Example', 'sourceUrl': 'https://www.nhc.noaa.gov/archive/2025/EXAMPLE.shtml',
            }), 'AL012025')
            self.assertIsNone(cyclone_guidance._resolve_atcf_id({
                'title': 'Hurricane Duplicate', 'sourceUrl': 'https://www.nhc.noaa.gov/archive/2026/DUPLICATE.shtml',
            }))

    def test_master_index_is_refreshed_after_cache_expiry(self):
        with mock.patch.dict(cyclone_guidance._index_cache, {'nhc': (0, {})}, clear=True), \
                mock.patch.object(cyclone_guidance, '_read', return_value=' NEW, AL, L, AL092026'):
            self.assertEqual(cyclone_guidance._nhc_storm_index()[('NEW', 2026)], 'AL092026')

    def test_ended_storms_do_not_display_archived_forecasts(self):
        storm = {'id': 'ended', 'title': 'Typhoon Example',
                 'sourceUrl': 'https://www.metoc.navy.mil/jtwc/products/wp2526.tcw'}
        rows = '\n'.join(f'WP, 25, 2020010100, 03, CMC, {tau}, 150N, 1400E' for tau in (0, 12, 24))
        with mock.patch.dict(cyclone_guidance._cache, {}, clear=True), \
                mock.patch.object(cyclone_guidance, 'hazard_snapshot', return_value=json.dumps({'items': [storm]})), \
                mock.patch.object(cyclone_guidance, '_read', return_value=rows):
            result = cyclone_guidance.guidance_snapshot('ended')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['tracks'], [])
        self.assertIn('No current model runs', result['message'])

    def test_indian_and_southern_hemisphere_guidance_uses_ucar(self):
        now = dt.datetime.now(dt.timezone.utc)
        cycle = now.strftime('%Y%m%d%H')
        for basin, directory in (('io', 'northindian'), ('sh', 'southernhemisphere')):
            with self.subTest(basin=basin):
                latitude = '150S' if basin == 'sh' else '150N'
                rows = [f'{basin.upper()}, 01, {cycle}, 03, {code}, {tau}, {latitude}, {lon}, 40, 1000'
                        for code in ('CMC', 'UKM')
                        for tau, lon in ((0, '850E'), (12, '860E'), (24, '870E'))]
                storm = {'id': 'storm-1', 'title': 'Tropical Cyclone Example',
                         'sourceUrl': f'https://www.metoc.navy.mil/jtwc/products/{basin}01{now:%y}.tcw'}
                with mock.patch.dict(cyclone_guidance._cache, {}, clear=True), \
                        mock.patch.object(cyclone_guidance, 'hazard_snapshot',
                                          return_value=json.dumps({'items': [storm]})), \
                        mock.patch.object(cyclone_guidance, '_read', return_value='\n'.join(rows)) as read:
                    result = cyclone_guidance.guidance_snapshot('storm-1')
                self.assertEqual(result['status'], 'available')
                self.assertEqual(len(result['tracks']), 2)
                self.assertEqual(read.call_args.args[0],
                                 f'{cyclone_guidance.UCAR_BASE}/{directory}/{now.year}/{basin}01{now.year}/a{basin}01{now.year}.dat')


if __name__ == '__main__':
    unittest.main()
