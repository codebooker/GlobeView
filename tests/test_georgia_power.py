import copy
import datetime as dt
import json
import unittest
from unittest import mock

import georgia_power as power
import international_infrastructure as infrastructure
from scripts.update_tbilisi_outage_locations import DISTRICTS, build_references

NOW = dt.datetime(2026, 10, 3, 9, tzinfo=dt.timezone.utc)
TITLE = 'სხვადასხვა სამუშაოების ჩატარების გამო 6 ოქტომბერს ელექტრომომარაგება დროებით შეიზღუდება'
BODY = ('<p><strong>გლდანის რაიონი</strong></p>'
        '<p>გადაუდებელი სამუშაოების გამო 11:00 საათიდან 18:00 საათამდე შეზღუდვა შეეხება:&nbsp;ლერი ლაგურაშვილის ქუჩის მოსახლეობას.</p>'
        '<p>საბურთალოს რაიონი</p>'
        '<p>ქსელში გადართვების გამო 01:00 საათიდან 04:00 საათამდე შეზღუდვა შეეხება: შარტავას ქუჩის მოსახლეობას.</p>')


def notice(**kwargs):
    return {'id': 5903, 'status': 'published', 'content_type': 'poweroutage', 'localisedto': 'ka',
            'taxonomy': {'content_poweroutage': [2769]}, 'date': '2026-10-02 17:05:58',
            'title': TITLE, 'editor': BODY, **kwargs}


class GeorgiaPowerTests(unittest.TestCase):
    def test_published_work_keeps_windows_addresses_and_approximate_references(self):
        rows = power.parse_planned([notice()], NOW)
        self.assertEqual(len(rows), 2)
        first, second = [r['properties'] for r in rows]
        self.assertIn('Gldani', first['area_name'])
        self.assertIn('Approximate district reference', first['area_name'])
        self.assertEqual(first['starts_at'], '2026-10-06T07:00:00+00:00')
        self.assertEqual(first['ends_at'], '2026-10-06T14:00:00+00:00')
        self.assertEqual(second['starts_at'], '2026-10-05T21:00:00+00:00')
        self.assertEqual(first['valid_until'], NOW.timestamp() + 900)
        self.assertTrue(first['planned'])
        self.assertIsNone(first['customers_affected'])
        self.assertIn('content=5903', first['source_url'])
        self.assertIn('ლერი ლაგურაშვილის', first['source_address'])
        self.assertNotIn('&nbsp;', first['source_address'])
        self.assertNotIn('<', first['source_address'])
        self.assertEqual(rows[0]['geometry']['coordinates'], power.catalog()['გლდანის რაიონი']['coordinates'])
        self.assertEqual(power.parse_planned([notice(), notice()], NOW), rows)

    def test_expired_work_and_unpublished_or_unplanned_records_are_omitted(self):
        for changed in ({'status': 'hidden'}, {'taxonomy': {'content_poweroutage': [2770]}},
                        {'localisedto': 'en'}, {'content_type': 'news'}, {'id': '5903'},
                        {'date': '2026-09-01 17:00:00'}, {'date': '2026-10-04 12:00:00'},
                        {'title': TITLE.replace('6 ოქტომბერს', '2025 წლის 6 ოქტომბერს')},
                        {'title': TITLE.replace('6 ოქტომბერს', '25 ოქტომბერს')}):
            with self.subTest(changed=changed):
                self.assertEqual(power.parse_planned([notice(**changed)], NOW), [])
        self.assertEqual(power.parse_planned([notice()], NOW + dt.timedelta(days=3, hours=6)), [])
        active = power.parse_planned([notice()], dt.datetime(2026, 10, 6, 10, tzinfo=dt.timezone.utc))
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]['properties']['status'].startswith('Planned work window'))

    def test_unknown_district_resets_scope_and_invalid_clocks_are_rejected(self):
        body = BODY.replace('საბურთალოს რაიონი', 'უცნობი რაიონი')
        self.assertEqual(len(power.parse_planned([notice(editor=body)], NOW)), 1)
        for body in (BODY.replace('11:00', '25:00').replace('01:00', '27:00'),
                     BODY.replace('18:00', '10:00').replace('04:00', '00:30'),
                     BODY.replace('საათამდე', 'საათამდე და 15:00 საათიდან 18:00 საათამდე')):
            self.assertEqual(power.parse_planned([notice(editor=body)], NOW), [])
        with self.assertRaises(ValueError):
            power.parse_planned([notice(editor='<div>No source paragraphs</div>')], NOW)
        with self.assertRaises(ValueError):
            power.parse_planned([notice()], NOW.replace(tzinfo=None))

    def test_yearless_dates_resolve_across_new_year_only_with_recent_publication(self):
        now = dt.datetime(2026, 12, 30, 8, tzinfo=dt.timezone.utc)
        row = notice(date='2026-12-29 12:00:00', title=TITLE.replace('6 ოქტომბერს', '2 იანვარს'))
        rows = power.parse_planned([row], now)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]['properties']['starts_at'].startswith('2027-01-02'))
        self.assertEqual(power.parse_planned([row], now + dt.timedelta(days=5)), [])

    def test_one_shared_source_read_and_independent_window_expiry(self):
        with mock.patch.dict(power._CACHE, {'until': 0, 'rows': []}, clear=True), \
                mock.patch.object(power, '_read_page', return_value=([notice()], 1)) as read:
            self.assertEqual(len(power.telasi_planned_outages(NOW)), 2)
            self.assertEqual(len(power.telasi_planned_outages(NOW)), 2)
            self.assertEqual(power.telasi_planned_outages(NOW + dt.timedelta(days=4)), [])
            read.assert_called_once_with(1)
        self.assertIs(infrastructure._FETCHERS['power']['ge_telasi_planned'], power.telasi_planned_outages)

    def test_reader_uses_public_planned_content_and_discards_device_and_editor_metadata(self):
        response = mock.MagicMock()
        response.status = 200
        response.geturl.return_value = power.API_URL
        response.read.return_value = json.dumps({'content': {'list': [notice(user_id=402)], 'page': 1, 'listCount': 1},
                                                'api': {'list': [{'status': 'hidden', 'code': 'PRIVATE'}]}}).encode()
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with mock.patch.object(power.urllib.request, 'build_opener', return_value=opener):
            rows, count = power._read_page(1)
        self.assertEqual(count, 1)
        self.assertNotIn('user_id', rows[0])
        self.assertNotIn('code', rows[0])
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)['taxonomy'], {'content_poweroutage': [2769]})
        with self.assertRaises(ValueError):
            power._NoRedirect().redirect_request(request, response, 302, '', {}, 'https://other.example')

    def test_offline_catalog_requires_exact_district_and_its_named_label_member(self):
        relations, nodes = [], []
        refs = power.catalog()
        for name, relation_id in DISTRICTS.items():
            place = next(p for p in refs.values() if p['id'] == str(relation_id))
            tags = {'name': place['heading'], 'name:en': name, 'boundary': 'administrative', 'admin_level': '10'}
            relations.append({'type': 'relation', 'id': relation_id, 'tags': tags,
                              'members': [{'type': 'node', 'ref': place['osmNode'], 'role': 'label'}]})
            nodes.append({'type': 'node', 'id': place['osmNode'], 'tags': tags,
                          'lon': place['coordinates'][0], 'lat': place['coordinates'][1]})
        self.assertEqual(len(build_references(relations, nodes)), 10)
        for rels, labels in ((relations[1:], nodes), (relations, nodes[1:]),
                             (relations + [relations[0]], nodes), (relations, nodes + [nodes[0]])):
            with self.assertRaises(ValueError):
                build_references(rels, labels)
        wrong = copy.deepcopy(relations)
        wrong[0]['members'][0]['role'] = 'admin_centre'
        with self.assertRaises(ValueError):
            build_references(wrong, nodes)
        wrong_nodes = copy.deepcopy(nodes)
        wrong_nodes[0]['lon'] = 45.8
        with self.assertRaises(ValueError):
            build_references(relations, wrong_nodes)


if __name__ == '__main__':
    unittest.main()
