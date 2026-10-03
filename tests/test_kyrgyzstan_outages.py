import datetime as dt
import pathlib
import unittest
from unittest import mock

import kyrgyzstan_outages as outages


UTC = dt.timezone.utc
NOTICE_DATE = dt.date(2026, 10, 5)
NOTICE_URL = outages.INDEX_URL + 'data-05102026-g/'
NOW = dt.datetime(2026, 10, 3, 2, tzinfo=UTC)
FIXTURE = pathlib.Path(__file__).with_name('fixtures') / 'bishkek-planned-work-20261005.html'


def page(text):
    result = outages._Page()
    result.feed(text)
    return result


class KyrgyzstanOutageTests(unittest.TestCase):
    def test_issyk_kul_published_schedule_maps_each_verified_district(self):
        notice = page(FIXTURE.with_name('issyk-kul-planned-work-20261007.html').read_text(encoding='utf-8'))
        url = outages._SERVICES['issyk_kul']['index'] + 'data-07102026-zh/'
        points = outages._parse_notice(notice, dt.date(2026, 10, 7), url, NOW, 'issyk_kul')
        self.assertEqual(len(points), 13)
        self.assertEqual({p['properties']['source_district'] for p in points},
                         {'Тюпский', 'Каракольский', 'Жети-Огузский', 'Тонский'})
        for point in points:
            lon, lat = point['geometry']['coordinates']
            self.assertTrue(77 < lon < 79 and 42 < lat < 43)
            properties = point['properties']
            self.assertTrue(properties['key'].startswith('kg:ipes:planned:'))
            self.assertEqual(properties['provider'], 'Issyk-Kul PES · planned power work')
            self.assertEqual(properties['starts_at'], '2026-10-07T03:00:00+00:00')
            self.assertEqual(properties['ends_at'], '2026-10-07T11:00:00+00:00')
            self.assertTrue(properties['status'].startswith('Scheduled'))
            self.assertEqual(properties['source_url'], url)
        names = [p['properties']['area_name'] for p in points]
        self.assertIn('Darkhan · Approximate area reference', names)
        self.assertIn('Jalgyz-Oruk · Approximate area reference', names)
        self.assertIn('Korgondu-Bulak · Approximate area reference', names)

    def test_homonyms_are_scoped_to_the_utility_and_published_district(self):
        # Bishkek's Ak-Ordo row includes a Kyzyl-Suu street. It must not place an
        # outage 350 km away at the identically named Issyk-Kul village.
        self.assertEqual([p['id'] for p in outages._locations_for('ж/м Ак-Ордо (ул. Тунук, Кызыл-Суу)')],
                         ['tunuk'])
        self.assertEqual([p['id'] for p in outages._locations_for('Кызыл-Суу', 'issyk_kul', 'Жети-Огузский')],
                         ['kyzyl-suu-jeti-oguz'])
        self.assertEqual(outages._locations_for('Кызыл-Суу', 'issyk_kul', 'Тонский'), [])
        # The OSM Kainar in Tyup is not the unverified Kainar in the Ton notice.
        self.assertEqual(outages._locations_for('Кайнар', 'issyk_kul', 'Тонский'), [])
        self.assertEqual(outages._locations_for('Курмонту Пристань', 'issyk_kul', 'Тюпский'), [])
        self.assertEqual(outages._locations_for('Тасма', 'bishkek', 'Тюпский'), [])

    def test_issyk_kul_adapter_only_follows_its_published_current_year_links(self):
        index = outages._SERVICES['issyk_kul']['index']
        url = index + 'data-07102026-zh/'
        catalog = page(''.join(f'<a href="{href}">График плановых работ и техобслуживания</a>'
                               for href in (url, index + 'data-02102026-zh/',
                                            index + 'data-07102025-zh/', NOTICE_URL)))
        notice = page(FIXTURE.with_name('issyk-kul-planned-work-20261007.html').read_text(encoding='utf-8'))
        with mock.patch.object(outages, '_read_page', side_effect=[catalog, notice]) as read:
            points = outages.issyk_kul_planned_outages(NOW)
        self.assertEqual(len(points), 13)
        self.assertEqual(read.call_args_list, [mock.call(index, 'issyk_kul'), mock.call(url, 'issyk_kul')])
        with mock.patch.object(outages._OPENER, 'open') as read:
            with self.assertRaisesRegex(ValueError, 'URL'):
                outages._read_page(NOTICE_URL, 'issyk_kul')
            read.assert_not_called()

    def test_issyk_kul_schedules_expire_and_do_not_accept_a_wrong_heading_year(self):
        text = FIXTURE.with_name('issyk-kul-planned-work-20261007.html').read_text(encoding='utf-8')
        url = outages._SERVICES['issyk_kul']['index'] + 'data-07102026-zh/'
        date = dt.date(2026, 10, 7)
        active = outages._parse_notice(page(text), date, url,
                                        dt.datetime(2026, 10, 7, 10, 55, tzinfo=UTC), 'issyk_kul')
        self.assertEqual(len(active), 13)
        self.assertTrue(all(p['properties']['status'].startswith('Planned work window') for p in active))
        self.assertTrue(all(p['properties']['valid_until'] == dt.datetime(2026, 10, 7, 11, tzinfo=UTC).timestamp()
                            for p in active))
        self.assertEqual(outages._parse_notice(page(text), date, url,
                                              dt.datetime(2026, 10, 7, 11, tzinfo=UTC), 'issyk_kul'), [])
        with self.assertRaisesRegex(ValueError, 'date'):
            outages._parse_notice(page(text.replace('07.10.2026', '07.10.2025')), date, url, NOW, 'issyk_kul')

    def test_published_table_rowspans_and_audited_reference_locations(self):
        points = outages._parse_notice(page(FIXTURE.read_text(encoding='utf-8')),
                                       NOTICE_DATE, NOTICE_URL, NOW)
        self.assertEqual(len(points), 15)
        self.assertEqual(len({p['properties']['key'] for p in points}), 15)
        by_name = {p['properties']['area_name']: p for p in points}
        building = by_name['Chokmorova 207 · Building reference']['properties']
        self.assertEqual(building['source_district'], 'Западный РЭС')
        self.assertEqual(building['starts_at'], '2026-10-05T03:00:00+00:00')
        self.assertEqual(building['ends_at'], '2026-10-05T11:00:00+00:00')
        self.assertEqual(building['location_source_url'], 'https://www.openstreetmap.org/way/37081106')
        self.assertIn('Baytik village · Approximate area reference', by_name)
        self.assertIn('Kashka-Suu · Approximate area reference', by_name)
        self.assertIn('Kokchetavskaya street · Approximate street reference', by_name)
        for point in points:
            lon, lat = point['geometry']['coordinates']
            self.assertTrue(74.4 < lon < 74.7 and 42.6 < lat < 43)
            properties = point['properties']
            self.assertTrue(properties['planned'])
            self.assertIsNone(properties['customers_affected'])
            self.assertTrue(properties['status'].startswith('Scheduled'))
            self.assertEqual(properties['valid_until'], NOW.timestamp() + 900)

    def test_schedule_windows_use_kyrgyz_time_and_expire_at_the_published_end(self):
        notice = page(FIXTURE.read_text(encoding='utf-8'))
        during = dt.datetime(2026, 10, 5, 6, 55, tzinfo=UTC)
        points = outages._parse_notice(notice, NOTICE_DATE, NOTICE_URL, during)
        maevka = next(p['properties'] for p in points if 'Maevka' in p['properties']['area_name'])
        self.assertTrue(maevka['status'].startswith('Planned work window'))
        self.assertEqual(maevka['valid_until'], dt.datetime(2026, 10, 5, 7, tzinfo=UTC).timestamp())
        at_end = outages._parse_notice(notice, NOTICE_DATE, NOTICE_URL,
                                       dt.datetime(2026, 10, 5, 7, tzinfo=UTC))
        self.assertFalse(any('Maevka' in p['properties']['area_name'] for p in at_end))
        self.assertEqual(outages._parse_notice(notice, NOTICE_DATE, NOTICE_URL,
                                              dt.datetime(2026, 10, 5, 13, tzinfo=UTC)), [])

    def test_missing_or_conflicting_year_never_becomes_a_current_notice(self):
        fixture = FIXTURE.read_text(encoding='utf-8')
        for text in (fixture.replace('05.10.2026', '5 октября'),
                     fixture.replace('05.10.2026', '05.10.2025'),
                     '<h2>06.10.2026</h2>' + fixture):
            with self.subTest(text=text[:70]), self.assertRaisesRegex(ValueError, 'date'):
                outages._parse_notice(page(text), NOTICE_DATE, NOTICE_URL, NOW)

    def test_unresolved_places_and_different_house_numbers_are_not_building_points(self):
        self.assertEqual(outages._locations_for('ж/м Хабитат'), [])
        self.assertEqual(outages._locations_for('ж/м Жениш'), [])
        for address in ('ул. Ибраимова, ж/д №146А', 'ул. Ибраимова, ж/д №146/1',
                        'ул. Чокморова, ж/д №207/2'):
            self.assertTrue(all(p['kind'] == 'street' for p in outages._locations_for(address)))
        self.assertEqual([p['id'] for p in outages._locations_for('ул. Ибраимова, ж/д №146, 146А')],
                         ['ibraimova-146'])

    def test_catalog_only_selects_published_near_term_same_host_daily_notices(self):
        urls = [NOTICE_URL, outages.INDEX_URL + 'data-02102026-g/',
                outages.INDEX_URL + 'data-15102026-g/',
                'http://example.com/ru/abonentam/perechen-uchastkov-rabot/data-05102026-g/',
                NOTICE_URL + '?other=true', NOTICE_URL + '#old',
                outages.INDEX_URL + 'data-32102026-g/']
        catalog = page(''.join(f'<a href="{url}">График плановых работ</a>' for url in urls)
                       + f'<a href="{NOTICE_URL}">Unrelated article</a>')
        self.assertEqual(outages._notice_links(catalog, NOW), [(NOTICE_URL, NOTICE_DATE)])
        # UTC midnight is not the utility's date boundary.
        self.assertEqual(outages._notice_links(catalog, dt.datetime(2026, 10, 5, 20, tzinfo=UTC)), [])

    def test_invalid_times_and_excessive_spans_are_rejected(self):
        text = ('<h1>Дата: 05.10.2026</h1><table><tr>'
                '<td>РЭС</td><td>с. Маевка</td><td>{begin}</td><td>{end}</td><td>Работы</td>'
                '</tr></table>')
        for begin, end in (('25:00', '26:00'), ('17:00', '09:00'), ('09:30', '09:30')):
            self.assertEqual(outages._parse_notice(page(text.format(begin=begin, end=end)),
                                                  NOTICE_DATE, NOTICE_URL, NOW), [])
        with self.assertRaisesRegex(ValueError, 'span'):
            page('<table><tr><td rowspan="999999">work</td></tr></table>')

    def test_transport_is_bounded_and_rejects_redirects_and_foreign_urls(self):
        with mock.patch.object(outages._OPENER, 'open') as open_url:
            with self.assertRaisesRegex(ValueError, 'URL'):
                outages._read_page('http://127.0.0.1/private')
            open_url.assert_not_called()
            response = open_url.return_value.__enter__.return_value
            response.status = 200
            response.geturl.return_value = NOTICE_URL
            response.read.return_value = b'x' * 250001
            with self.assertRaisesRegex(ValueError, 'size'):
                outages._read_page(NOTICE_URL)
            response.read.assert_called_once_with(250001)
        with self.assertRaisesRegex(ValueError, 'redirect'):
            outages._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')

    def test_shared_power_snapshot_drops_old_schedule_cache_even_on_source_failure(self):
        import international_infrastructure as infrastructure
        points = outages._parse_notice(page(FIXTURE.read_text(encoding='utf-8')),
                                       NOTICE_DATE, NOTICE_URL, NOW)
        snapshot = {'sources': {'kg_bipes_planned': points}, 'errors': ['kg_bipes_planned: unavailable']}
        with mock.patch.object(infrastructure, '_snapshot', return_value=snapshot), \
                mock.patch.object(infrastructure.time, 'time', return_value=NOW.timestamp() + 900):
            self.assertEqual(infrastructure.power_snapshot()['features'], [])
        self.assertIs(infrastructure._FETCHERS['power']['kg_bipes_planned'], outages.bishkek_planned_outages)


if __name__ == '__main__':
    unittest.main()
