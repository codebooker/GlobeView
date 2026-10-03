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
