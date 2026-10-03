import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest import mock

import armenia_roads as roads
import international_infrastructure as infrastructure
from scripts.update_armenia_road_locations import canonical, join_locations, road_refs


NOW = dt.datetime(2026, 10, 3, 9, tzinfo=dt.timezone.utc)
BLAST_URL = roads.BASE + '/am/urgent_news/inner/News_02.10.2026_2'
BRIDGE_URL = roads.BASE + '/am/urgent_news/inner/News_25.09.2026_1'
BLAST = ('Հ-26, /Մ-4/-Ենոքավան-Կարմիրգյուղ (գյուղատեղի)- /Հ-58/ ավտոճանապարհի '
         'հիմնանորոգման աշխատանքներով պայմանավորված՝ Ենոքավան բնակավայրի հարակից '
         'տարածքում՝ կմ7+360-կմ7+400 հատվածում, հոկտեմբերի 4-ին՝ ժամը 14։00-ից մինչև '
         '15։00, նախատեսվում է կատարել պայթեցման աշխատանքներ։')
BRIDGE = ('Մ-3, Վաղարշապատի շրջանցիկ ավտոճանապարհի՝ Զվարթնոց թաղամասից դեպի Ոսկեհատ '
          'և Մարգարա տանող հատվածում գտնվող Ոսկեհատի կամուրջը (կմ2+100) վթարային '
          'վիճակում է։ Կամուրջը երկկողմանի փակ է երթևեկության համար։ '
          'Շրջանցել Մ-5 ավտոճանապարհից դեպի Մ-3 ավտոճանապարհ։')


def article(body, date):
    return ('<h2 class="inner-title">Public notice</h2><div class="item-block item-block-inner">'
            '<div class="block-info"><span class="item-time">' + date + '</span><p>' + body +
            '</p></div></div><div class="latest-info"><p>Other notices and office addresses</p></div>')


class ArmeniaRoadTests(unittest.TestCase):
    def test_real_blasting_notice_keeps_local_window_and_approximate_location(self):
        row = roads.parse_notice(article(BLAST, '02-10-2026'), BLAST_URL, NOW)[0]
        p = row['properties']
        self.assertEqual(p['layer'], 'construction')
        self.assertEqual(p['title'], 'Planned blasting · H-26 near Yenokavan')
        self.assertEqual(p['starts_at'], '2026-10-04T14:00:00+04:00')
        self.assertEqual(p['ends_at'], '2026-10-04T15:00:00+04:00')
        self.assertIn('Closure not stated', p['detail'])
        self.assertIn('Source km 7+360–7+400', p['detail'])
        self.assertIn('exact section unverified', p['detail'])
        self.assertEqual(row['geometry']['coordinates'], [45.1073152, 40.9125761])
        self.assertLessEqual(p['valid_until'], NOW.timestamp() + 900)
        self.assertNotIn('road_segments', p)

    def test_blasting_expiry_uses_armenia_time_not_utc(self):
        page = article(BLAST, '02-10-2026')
        before = dt.datetime(2026, 10, 4, 10, 59, tzinfo=dt.timezone.utc)
        self.assertEqual(roads.parse_notice(page, BLAST_URL, before)[0]['properties']['valid_until'],
                         dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(roads.parse_notice(page, BLAST_URL, before + dt.timedelta(minutes=1)), [])

    def test_bridge_report_uses_numbered_road_to_disambiguate_voskehat(self):
        row = roads.parse_notice(article(BRIDGE, '25-09-2026'), BRIDGE_URL, NOW)[0]
        self.assertEqual(row['properties']['layer'], 'incidents')
        self.assertIn('Current status unverified', row['properties']['detail'])
        self.assertEqual(row['geometry']['coordinates'], [44.3230281, 40.1590403])
        self.assertNotIn('ends_at', row['properties'])
        self.assertEqual(roads.parse_notice(article(BRIDGE, '25-09-2026'), BRIDGE_URL,
                                            NOW + dt.timedelta(days=8)), [])

    def test_expired_restored_wrong_year_and_invalid_windows_are_omitted(self):
        for body in (BLAST.replace('հոկտեմբերի 4-ին', 'հոկտեմբերի 2-ին'),
                     BLAST.replace('հոկտեմբերի 4-ին', '2025 թվականի հոկտեմբերի 4-ին'),
                     BLAST.replace('15։00', '13։00'), BLAST.replace('14։00', '25։00'),
                     BLAST + ' երթևեկությունը վերականգնվել է։'):
            self.assertEqual(roads.parse_notice(article(body, '02-10-2026'), BLAST_URL, NOW), [])

    def test_missing_schedule_does_not_become_an_active_construction_claim(self):
        body = BLAST.replace('հոկտեմբերի 4-ին՝ ժամը 14։00-ից մինչև 15։00,', '')
        self.assertEqual(roads.parse_notice(article(body, '02-10-2026'), BLAST_URL, NOW), [])

    def test_two_settlements_or_wrong_road_are_not_guessed(self):
        body = BLAST.replace('Հ-26', 'Մ-3', 1)
        self.assertEqual(roads.parse_notice(article(body, '02-10-2026'), BLAST_URL, NOW), [])
        places = copy.deepcopy(roads._locations())
        other = copy.deepcopy(next(p for p in places if p['name'] == 'Yenokavan'))
        other['id'] = 'ambiguous'
        places.append(other)
        self.assertEqual(roads.parse_notice(article(BLAST, '02-10-2026'), BLAST_URL, NOW, places), [])

    def test_primary_article_only_and_structure_change_is_an_error(self):
        page = article(BLAST, '02-10-2026') + '<span class="item-time">01-01-1999</span>'
        self.assertEqual(len(roads.parse_notice(page, BLAST_URL, NOW)), 1)
        for page in ('<html></html>', article(BLAST, '01-10-2026')):
            with self.assertRaises(ValueError):
                roads.parse_notice(page, BLAST_URL, NOW)

    def test_reader_rejects_arbitrary_urls_and_redirects(self):
        for url in ('http://armroad.am/am/press/urgentnews', 'https://127.0.0.1/',
                    BLAST_URL + '?next=http://localhost', BLAST_URL + '#x',
                    BLAST_URL.replace('armroad.am', 'armroad.am.evil.example')):
            with self.assertRaises(ValueError):
                roads._read(url)
        with self.assertRaises(ValueError):
            roads._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')

    def test_listing_filters_old_links_and_source_pages_share_cache(self):
        listing = ('<a href="' + BLAST_URL + '">Work</a><a href="' + BRIDGE_URL + '">Closure</a>'
                   '<a href="https://armroad.am/am/urgent_news/inner/News_01.01.2025">Old</a>')
        pages = {roads.INDEX: listing, BLAST_URL: article(BLAST, '02-10-2026'),
                 BRIDGE_URL: article(BRIDGE, '25-09-2026')}

        class Response:
            def __init__(self, body): self.body = body.encode()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return self.body[:limit]

        opener = mock.Mock()
        opener.open.side_effect = lambda request, timeout: Response(pages[request.full_url])
        with mock.patch.dict(roads._PAGES, {}, clear=True), mock.patch.object(roads.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(len(roads.road_notices(NOW)), 2)
            self.assertEqual(len(roads.road_notices(NOW)), 2)
            self.assertEqual(opener.open.call_count, 3)

    def test_numbered_road_join_selects_actual_nodes_with_distance_limit(self):
        p = {'id': '1', 'name': 'Town', 'coordinates': [45.1, 40.9], 'aliases': ['town']}
        nodes = [{'id': 10, 'way': 20, 'coordinates': [45.101, 40.9], 'refs': ['H26']},
                 {'id': 11, 'way': 21, 'coordinates': [45.15, 40.9], 'refs': ['M3']}]
        joined = join_locations([p], nodes)[0]['roads']
        self.assertEqual(set(joined), {'H26'})
        self.assertEqual(joined['H26']['osmNode'], 10)
        self.assertEqual(joined['H26']['coordinates'], nodes[0]['coordinates'])
        self.assertEqual(canonical(road_refs('Հ-26; Մ-3; Տ-10-24')[0]), 'H26')

    def test_committed_catalog_has_provenance_and_bounded_references(self):
        data = json.loads(Path(roads.__file__).with_name('armenia-road-locations.json').read_text())
        self.assertEqual(data['osmDataThrough'], '2026-10-02T20:21:34Z')
        self.assertEqual(data['osmLicence'], 'ODbL 1.0')
        self.assertEqual(data['gazetteerLicence'], 'CC BY 4.0')
        self.assertEqual(len(data['locations']), 1091)
        self.assertEqual(sum(len(p['roads']) for p in data['locations']), 4206)
        self.assertTrue(all(0 <= r['distanceMetres'] <= 3000 for p in roads._locations() for r in p['roads'].values()))

    def test_road_snapshot_filters_layer_bbox_and_expired_records(self):
        rows = roads.parse_notice(article(BLAST, '02-10-2026'), BLAST_URL, NOW)
        snapshot = {'sources': {'am_armroad_notices': rows}, 'errors': [], 'loading': False}
        with mock.patch.object(infrastructure, '_snapshot', return_value=snapshot), \
                mock.patch.object(infrastructure.time, 'time', return_value=NOW.timestamp()):
            visible = infrastructure.road_snapshot('construction', (45.10, 40.90, 45.12, 40.93))
            self.assertEqual(len(visible['features']), 1)
            self.assertEqual(infrastructure.road_snapshot('incidents', (45.10, 40.90, 45.12, 40.93))['features'], [])
            self.assertEqual(infrastructure.road_snapshot('construction', (44.1, 40.1, 44.2, 40.2))['features'], [])
        with mock.patch.object(infrastructure, '_snapshot', return_value=snapshot), \
                mock.patch.object(infrastructure.time, 'time', return_value=NOW.timestamp() + 901):
            self.assertEqual(infrastructure.road_snapshot('construction', (45.10, 40.90, 45.12, 40.93))['features'], [])


if __name__ == '__main__':
    unittest.main()
