import datetime as dt
import unittest
from unittest import mock

import armenia_roads as roads
import international_infrastructure as infrastructure
from scripts.update_armenia_named_road_locations import REPORTED_ROADS, build_references, build_reported_roads
from tests.test_armenia_roads import article


NOW = dt.datetime(2026, 10, 3, 9, tzinfo=dt.timezone.utc)
PASS_URL = roads.BASE + '/am/news/inner/News_25.09.2026'
GYUMRI_URL = roads.BASE + '/am/news/inner/News_28.09.2026_3'
PASS = ('Լոռու մարզում շարունակվում են Պուշկինյան լեռնանցքի հիմնանորոգման աշխատանքները։ '
        'Տեղադրվել են ստորգետնյա ջրահեռացման համակարգեր, կատարվում է ժայռային հանույթ՝ լայնացնելով ճանապարհը։')
GYUMRI = ('Գյումրու մի շարք փողոցներում իրականացվում են միջին նորոգման աշխատանքներ։ '
          'Հասմիկ Կիրակոսյան փողոցում ասֆալտբետոնե ծածկի տեղադրումն ավարտվել է։ '
          'Շինտեխնիկան այժմ Գյումրու Շահումյան փողոցում է։')


class ArmeniaConstructionTests(unittest.TestCase):
    def test_september_30_report_maps_all_verified_named_roads(self):
        body = ('Կոտայքի և Լոռու մարզերի ճանապարհաշինական տեղամասեր։ '
                'Եղվարդ-Արագյուղ-Հարթավան մոտ 12.5կմ ճանապարհին տեղադրվում է ասֆալտբետոնե ծածկը։ '
                'Քարաձորի, Ղուրսալ-Նոր Խաչակապ ճանապարհներին, Պուշկինյան լեռնանցքում (13.3կմ) '
                'ընթացքի մեջ է արհեստական կառույցների, հողային պաստառի կառուցումը։')
        rows = roads.parse_news_roadworks(article(body, '30-09-2026'),
                                         roads.BASE + '/am/news/inner/News_30.09.2026', NOW)
        self.assertEqual({r['properties']['key'].removeprefix('am:armroad:works:') for r in rows},
                         {'yeghvard-aragyugh-hartavan', 'karadzor-approach',
                          'ghursal-nor-khachakap', 'pushkin-pass'})
        self.assertTrue(all(r['properties']['reported_at'] == '2026-09-30' for r in rows))
        unrelated = 'Քարաձորի դպրոցը կառուցվում է։ Ճանապարհաշինական աշխատանքների այց։'
        self.assertEqual(roads.parse_news_roadworks(article(unrelated, '30-09-2026'),
                                                  roads.BASE + '/am/news/inner/News_30.09.2026', NOW), [])

    def test_numbered_road_references_require_exact_route_and_place(self):
        places, ways = [], []
        for index, spec in enumerate(REPORTED_ROADS):
            place = {'id': spec[3], 'name': spec[4], 'coordinates': [44.4, 40.8 + index * .1]}
            places.append(place)
            ways.append({'id': index + 10, 'tags': {'ref': spec[5][0] + '-' + spec[5][1:]},
                         'nodes': [{'id': index + 20, 'coordinates': place['coordinates']}]})
        references = build_reported_roads(ways, places)
        self.assertEqual([r['roadRef'] for r in references], ['H4', 'T5-77', 'T5-31'])
        self.assertEqual([r['osmNode'] for r in references], [20, 21, 22])
        for candidate_ways, candidate_places in ((ways[1:], places), (ways, places[1:]),
                                                (ways, places + [places[0]])):
            with self.assertRaises(ValueError):
                build_reported_roads(candidate_ways, candidate_places)
        far_ways = [{**w, 'nodes': [{'id': 80 + i, 'coordinates': [45.5, 39.5]}]}
                    for i, w in enumerate(ways)]
        with self.assertRaises(ValueError):
            build_reported_roads(far_ways, places)

    def test_current_reports_keep_source_dates_and_reference_points(self):
        for body, url, date, name, point in (
                (PASS, PASS_URL, '25-09-2026', 'Pushkin Pass', [44.4319955, 40.9107079]),
                (GYUMRI, GYUMRI_URL, '28-09-2026', 'Shahumyan Street · Gyumri', [43.843727, 40.7941569])):
            with self.subTest(name=name):
                rows = roads.parse_news_roadworks(article(body, date), url, NOW)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['geometry']['coordinates'], point)
                p = rows[0]['properties']
                self.assertEqual(p['layer'], 'construction')
                self.assertIn(name, p['title'])
                self.assertIn('Current restrictions unverified', p['detail'])
                self.assertEqual(p['valid_until'], NOW.timestamp() + 900)
                self.assertNotIn('ends_at', p)
                self.assertNotIn('road_segments', p)

    def test_old_future_completed_and_unscoped_reports_are_omitted(self):
        for body in (PASS.replace('շարունակվում են', 'չեն շարունակվում'),
                     'Պուշկինյան լեռնանցքի հիմնանորոգումը նախատեսվում է։',
                     'Պուշկինյան լեռնանցքի հիմնանորոգման աշխատանքներն ավարտվել են։',
                     GYUMRI.replace('Գյումրու', 'Երևանի')):
            self.assertEqual(roads.parse_news_roadworks(article(body, '25-09-2026'), PASS_URL, NOW), [])
        self.assertEqual(roads.parse_news_roadworks(article(PASS, '25-09-2026'), PASS_URL,
                                                  NOW + dt.timedelta(days=7)), [])

    def test_source_body_only_and_bad_article_date_is_an_error(self):
        page = article('Քննարկվել են ճանապարհների նորոգման խնդիրները։', '25-09-2026') + '<p>' + PASS + '</p>'
        self.assertEqual(roads.parse_news_roadworks(page, PASS_URL, NOW), [])
        with self.assertRaises(ValueError):
            roads.parse_news_roadworks(article(PASS, '24-09-2026'), PASS_URL, NOW)
        with self.assertRaises(ValueError):
            roads.parse_news_roadworks(article(PASS, '25-09-2026'), PASS_URL.replace('/news/', '/urgent_news/'), NOW)

    def test_completion_for_same_road_replaces_older_report(self):
        complete_url = roads.BASE + '/am/news/inner/News_30.09.2026'
        complete = 'Պուշկինյան լեռնանցքի հիմնանորոգման աշխատանքներն ավարտվել են։'
        listing = '<a href="' + PASS_URL + '">Work</a><a href="' + complete_url + '">Done</a>'
        pages = {roads.NEWS_INDEX: listing, PASS_URL: article(PASS, '25-09-2026'),
                 complete_url: article(complete, '30-09-2026')}
        with mock.patch.object(roads, '_read', side_effect=pages.__getitem__):
            self.assertEqual(roads.news_roadworks(NOW), [])
        pages[complete_url] = article('Լոռու մարզում շարունակվում են Պուշկինյան լեռնանցքի հիմնանորոգման աշխատանքները։', '30-09-2026')
        with mock.patch.object(roads, '_read', side_effect=pages.__getitem__):
            rows = roads.news_roadworks(NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['properties']['source_url'], complete_url)

    def test_other_completed_street_does_not_remove_shahumyan_work(self):
        rows = roads.parse_news_roadworks(article(GYUMRI, '28-09-2026'), GYUMRI_URL, NOW, include_resolved=True)
        self.assertEqual(len(rows), 1)
        self.assertNotIn('resolved', rows[0]['properties'])

    def test_news_and_urgent_indexes_are_kept_separate(self):
        self.assertIs(infrastructure._FETCHERS['roads']['am_armroad_notices'], roads.road_notices)
        self.assertIs(infrastructure._FETCHERS['roads']['am_armroad_construction'], roads.news_roadworks)
        listing = '<a href="' + PASS_URL + '">Other news</a><a href="' + GYUMRI_URL + '">Work</a>'
        pages = {roads.NEWS_INDEX: listing, PASS_URL: article(PASS, '25-09-2026'),
                 GYUMRI_URL: article(GYUMRI, '28-09-2026')}
        with mock.patch.object(roads, '_read', side_effect=pages.__getitem__):
            self.assertEqual(len(roads.news_roadworks(NOW)), 2)

    def test_builder_requires_city_scope_exact_street_and_pass_road_membership(self):
        places = [{'id': '616635', 'name': 'Gyumri', 'coordinates': [43.84635, 40.79305]}]
        street = {'id': 10, 'tags': {'name': 'Շահումյան փողոց'},
                  'nodes': [{'id': 11, 'coordinates': [43.843727, 40.7941569]}]}
        pass_node = {'id': 20, 'tags': {'mountain_pass': 'yes', 'name': 'Պուշկինի լեռնանցք'},
                     'coordinates': [44.4319955, 40.9107079]}
        way = {'id': 21, 'tags': {}, 'nodes': [pass_node]}
        refs = build_references([street, way], [pass_node], places)
        self.assertEqual(refs[1]['osmWays'], [21])
        for ways, landmarks in (([street], [pass_node]), ([street, way], [pass_node, pass_node]),
                                ([dict(street, tags={'name': 'Շահումյան 4-րդ փողոց'}), way], [pass_node])):
            with self.assertRaises(ValueError):
                build_references(ways, landmarks, places)


if __name__ == '__main__':
    unittest.main()
