import copy
import datetime as dt
import json
import unittest
from unittest import mock

import georgia_energo_outages as power
import international_infrastructure as infrastructure
from scripts.update_georgia_outage_locations import add_boundary_scopes, _polygon_contains

NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc)  # 16:00 in Georgia


def notice(**changes):
    row = {'taskId': 12345, 'taskType': '1', 'scName': 'თბილისი',
           'disconnectionArea': 'ცხვარიჭამია, მცხეთა/ცხვარიჭამია',
           'disconnectionDate': '2026-10-05 10:00', 'reconnectionDate': '2026-10-05 18:30'}
    row.update(changes)
    return row


class EnergoProOutageTests(unittest.TestCase):
    def test_affected_places_are_not_service_centres_and_customer_counts_are_not_distributed(self):
        rows = power.parse_notices([notice(scEffectedCustomers='1000')], NOW)
        self.assertEqual(len(rows), 1)
        p = rows[0]['properties']
        self.assertTrue(p['area_name'].startswith('Tskhvarich’amia'))
        self.assertEqual(rows[0]['geometry']['coordinates'], [44.9284, 41.88333])
        self.assertNotEqual(rows[0]['geometry']['coordinates'], power.catalog()['places']['611717']['coordinates'])
        self.assertIsNone(p['customers_affected'])
        self.assertEqual(p['starts_at'], '2026-10-05T06:00:00+00:00')
        self.assertIn('მცხეთა/ცხვარიჭამია', p['source_address'])
        area = 'მცხეთა/მუხათწყარო, მცხეთა/ლისი, მცხეთა/მუხათწყარო/VI, ლისი'
        self.assertEqual(len(power.parse_notices([notice(disconnectionArea=area)], NOW)), 2)

    def test_municipality_scope_disambiguates_duplicates_and_does_not_guess_missing_villages(self):
        def ids(area):
            return {r['place']['id'] for r in power.affected_places(area, power.catalog())}
        self.assertEqual(ids('ახალციხე/აგარა'), {'615944'})
        self.assertEqual(ids('კასპის რაიონი/აღაიანი'), {'615937'})
        self.assertEqual(ids('მცხეთა/უცნობი სოფელი'), set())
        self.assertEqual(ids('უცნობი მუნიციპალიტეტი/ცხვარიჭამია'), set())
        self.assertEqual(ids('სალხინო'), set())  # Multiple places share this name.
        self.assertEqual(ids('ბორჯომი/ბაკურიანი/წაქაძეს ქუჩა'), {'615583'})
        self.assertEqual(ids('სამეგრელო/ფოთი/ბარათაშვილის'), {'612366'})
        self.assertEqual(ids('თელავის რ-ნი/თელავი/თბილისის გზატკეცილი'), {'611694'})

    def test_source_spelling_aliases_and_urban_streets_keep_named_place_coordinates(self):
        def names(area):
            return {r['place']['name'] for r in power.affected_places(area, power.catalog())}
        self.assertEqual(names('ბაღდათის რაიონი/I ობჩა, ბაღდათის რაიონი/II ობჩა'), {'P’irveli Obcha', 'Meore Obcha'})
        self.assertEqual(names('ახმეტის რ-ნი/ქვ.ალვანი/უცნობი, ახმეტის რ-ნი/ზ.ალვანი'), {'Kvemo Alvani', 'Zemo Alvani'})
        self.assertEqual(names('დიდიჭყონი'), {'Didi Ch’q’oni'})
        self.assertEqual(names('ბათუმი/გენ.გ.კვინიტაძე, ხელვაჩაურის რაიონი/მახვილაური'), {'Batumi', 'Makhvilauri'})

    def test_unplanned_fractional_restoration_clock_and_exact_end_expiry(self):
        row = notice(taskType='3', scName='კასპი', disconnectionArea='კავთისხევი, ბოძი 14/1',
                     disconnectionDate='2026-10-03 16:06', reconnectionDate='2026-10-03 19:36:10.0000000')
        now = NOW + dt.timedelta(minutes=10)
        out = power.parse_notices([row], now)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]['properties']['planned'])
        self.assertTrue(out[0]['properties']['status'].startswith('Reported unplanned outage'))
        self.assertEqual(out[0]['properties']['ends_at'], '2026-10-03T15:36:10+00:00')
        self.assertEqual(out[0]['properties']['valid_until'], now.timestamp() + 900)
        for fraction, microseconds in [('1', 100000), ('12', 120000), ('1234567', 123456)]:
            self.assertEqual(power._clock('2026-10-03 19:36:10.' + fraction).microsecond, microseconds)
        self.assertEqual(power.parse_notices([row], power._clock(row['reconnectionDate'])), [])
        self.assertEqual(power.parse_notices([row], NOW), [])  # Future unplanned record.
        self.assertEqual(power.parse_notices([notice(disconnectionDate='2026-10-03 23:00',
                         reconnectionDate='2026-10-04 02:00')], NOW)[0]['properties']['ends_at'], '2026-10-03T22:00:00+00:00')

    def test_invalid_dates_unknown_types_and_conflicting_copies_are_excluded(self):
        for change in ({'taskId': True}, {'taskType': 'unknown'}, {'disconnectionDate': '2026-02-30 10:00'},
                       {'reconnectionDate': None}, {'reconnectionDate': '2026-10-05 09:00'},
                       {'disconnectionDate': '2026-10-05 25:00'}, {'disconnectionArea': ''}):
            with self.subTest(change=change):
                self.assertEqual(power.parse_notices([notice(**change)], NOW), [])
        a = notice()
        self.assertEqual(power.parse_notices([a, a], NOW), power.parse_notices([a], NOW))
        self.assertEqual(power.parse_notices([a, notice(reconnectionDate='2026-10-05 19:00')], NOW), [])
        copies = [notice(scName='თბილისი', disconnectionArea='მცხეთა/ცხვარიჭამია'),
                  notice(scName='მცხეთა', disconnectionArea='მცხეთა/ცხვარიჭამია/ქუჩა 1')]
        merged = power.parse_notices(copies, NOW)
        self.assertEqual(len(merged), 1)
        self.assertIn('ქუჩა 1', merged[0]['properties']['source_address'])
        with self.assertRaises(ValueError):
            power.parse_notices([a], NOW.replace(tzinfo=None))

    def test_public_reader_whitelists_fields_and_rejects_redirects_and_schema_changes(self):
        response = mock.MagicMock()
        response.status = 200
        response.geturl.return_value = power.API_BASE + '/searchAlerts'
        response.read.return_value = json.dumps({'status': 200, 'data': [notice(account='PRIVATE', taskNote='unused')]}).encode()
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with mock.patch.object(power.urllib.request, 'build_opener', return_value=opener):
            rows = power._read('/searchAlerts', 'თბილისი')
        self.assertEqual(set(rows[0]), set(power._FIELDS))
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {'search': 'თბილისი'})
        self.assertNotIn('Authorization', request.headers)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 20)
        with self.assertRaises(ValueError):
            power._NoRedirect().redirect_request(request, response, 302, '', {}, 'https://other.example')
        for payload in ({'status': 403, 'data': []}, {'status': 200, 'data': {}}, {'status': 200, 'data': [notice()] * 101}):
            response.read.return_value = json.dumps(payload).encode()
            with mock.patch.object(power.urllib.request, 'build_opener', return_value=opener), self.assertRaises(ValueError):
                power._read('/searchAlerts', 'თბილისი')
        response.read.return_value = b'x' * 4000001
        with mock.patch.object(power.urllib.request, 'build_opener', return_value=opener), self.assertRaises(ValueError):
            power._read('/searchAlerts', 'თბილისი')

    def test_all_public_cities_and_service_centres_are_searched_without_using_default_cap_alone(self):
        def read(path, search=None):
            if path == '/get/cities':
                return [{'nameGe': 'ბათუმი', 'disabled': False}]
            if path == '/alerts':
                return [notice(scName='კასპი')]
            return [notice(taskId={'ბათუმი': 1, 'თბილისი': 2, 'კასპი': 3}[search])]
        with mock.patch.object(power, 'catalog', return_value={'queries': ['თბილისი']}), \
                mock.patch.object(power, '_read', side_effect=read) as loader:
            rows = power._collect(NOW)
        self.assertEqual(len(rows), 4)
        self.assertEqual({call.args[1] for call in loader.call_args_list if len(call.args) > 1},
                         {'ბათუმი', 'თბილისი', 'კასპი'})
        with mock.patch.object(power, 'catalog', return_value={'queries': ['თბილისი']}), \
                mock.patch.object(power, '_read', side_effect=lambda path, search=None:
                                  [] if path in {'/get/cities', '/alerts'} else [notice()] * 100), self.assertRaises(ValueError):
            power._collect(NOW)

    def test_shared_cache_recalculates_windows_and_failed_refresh_never_replaces_snapshot(self):
        with mock.patch.dict(power._CACHE, {'until': 0, 'rows': []}, clear=True), \
                mock.patch.object(power, '_collect', return_value=[notice()]) as collect, \
                mock.patch.object(power.time, 'monotonic', return_value=100):
            self.assertEqual(len(power.energo_pro_outages(NOW)), 1)
            active = power.energo_pro_outages(dt.datetime(2026, 10, 5, 10, tzinfo=dt.timezone.utc))
            self.assertTrue(active[0]['properties']['status'].startswith('Planned work window'))
            self.assertEqual(power.energo_pro_outages(dt.datetime(2026, 10, 5, 14, 30, tzinfo=dt.timezone.utc)), [])
            collect.assert_called_once()
            previous = copy.deepcopy(power._CACHE)
            with mock.patch.object(power.time, 'monotonic', return_value=1001), \
                    mock.patch.object(power, '_collect', side_effect=ValueError('incomplete')), self.assertRaises(ValueError):
                power.energo_pro_outages(NOW)
            self.assertEqual(power._CACHE, previous)
        self.assertIs(infrastructure._FETCHERS['power']['ge_energo_pro'], power.energo_pro_outages)

    def test_offline_scope_join_rejects_holes_edges_ambiguity_and_changed_provenance(self):
        outer = [[41, 41], [43, 41], [43, 43], [41, 43], [41, 41]]
        hole = [[41.4, 41.4], [41.8, 41.4], [41.8, 41.8], [41.4, 41.8], [41.4, 41.4]]
        self.assertTrue(_polygon_contains([42, 42], [outer, hole]))
        for point in ([41.6, 41.6], [41.4, 41.6], [41, 42], [44, 42]):
            self.assertFalse(_polygon_contains(point, [outer, hole]))
        metadata = {'boundaryID': 'GEO-ADM2-92138335', 'boundaryISO': 'GEO', 'boundaryType': 'ADM2',
                    'boundaryYearRepresented': '2007', 'boundaryLicense': 'Public Domain'}
        features = [{'properties': {'shapeName': 'Example' if i == 0 else str(i),
                                    'shapeGroup': 'GEO', 'shapeType': 'ADM2'},
                     'geometry': {'type': 'Polygon', 'coordinates': [outer, hole]}} for i in range(68)]
        places = [{'admin1': '66', 'admin2': '', 'coordinates': [42, 42]},
                  {'admin1': '67', 'admin2': '', 'coordinates': [42, 42]},
                  {'admin1': '66', 'admin2': '', 'coordinates': [41.6, 41.6]}]
        scopes = {'one': {'id': 'one', 'name': 'Example Municipality', 'admin1': '66'}}
        add_boundary_scopes(places, scopes, {'type': 'FeatureCollection', 'features': features}, metadata)
        self.assertEqual(places[0]['reference_admin2'], 'one')
        self.assertNotIn('reference_admin2', places[1])
        self.assertNotIn('reference_admin2', places[2])
        with self.assertRaises(ValueError):
            add_boundary_scopes(places, scopes, {'type': 'FeatureCollection', 'features': features},
                                dict(metadata, boundaryISO='OTHER'))
        malformed = copy.deepcopy(features)
        malformed[1]['geometry']['coordinates'] = []
        with self.assertRaises(ValueError):
            add_boundary_scopes(places, scopes, {'type': 'FeatureCollection', 'features': malformed}, metadata)
        features[1]['properties']['shapeName'] = 'Example'
        with self.assertRaises(ValueError):
            add_boundary_scopes(places, scopes, {'type': 'FeatureCollection', 'features': features}, metadata)


if __name__ == '__main__':
    unittest.main()
