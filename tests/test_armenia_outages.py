import datetime as dt
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import armenia_outages as outages
import international_infrastructure as infrastructure


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 3, 8, tzinfo=UTC)


def header(date='հոկտեմբերի 3-ին'):
    return 'Ընկերությունը տեղեկացնում է, որ ' + date + ' պլանային նորոգման աշխատանքներ կիրականացվեն։'


def schedule(address='Արտաշատ քաղաք՝ Մխչյան փողոց մասնակի,',
             region='Արարատի մարզ՝', hours='11:00–14:00', date='հոկտեմբերի 3-ին'):
    return [header(date), region, hours + ' ' + address]


def names(features):
    return [p['properties']['area_name'].split(' · ')[0] for p in features]


class ArmeniaOutageTests(unittest.TestCase):
    def test_utc4_work_window_and_original_address_without_invented_impact(self):
        records = outages.parse_schedule(schedule(), NOW)
        self.assertEqual(names(records), ['Artashat'])
        p = records[0]['properties']
        self.assertEqual(p['starts_at'], '2026-10-03T07:00:00+00:00')
        self.assertEqual(p['ends_at'], '2026-10-03T10:00:00+00:00')
        self.assertEqual(p['valid_until'], NOW.timestamp() + 900)
        self.assertTrue(p['status'].startswith('Planned work window'))
        self.assertTrue(p['planned'])
        self.assertIsNone(p['customers_affected'])
        self.assertIn('Մխչյան', p['source_address'])
        self.assertIn('Approximate area reference', p['area_name'])
        self.assertEqual(p['source_url'], outages.SOURCE_URL)
        future = outages.parse_schedule(schedule(date='հոկտեմբերի 5-ին'), NOW)[0]['properties']
        self.assertTrue(future['status'].startswith('Scheduled'))

    def test_exact_end_removes_event_and_cache_validity_cannot_outlive_it(self):
        near_end = NOW.replace(hour=9, minute=58)
        p = outages.parse_schedule(schedule(), near_end)[0]['properties']
        self.assertEqual(p['valid_until'], NOW.replace(hour=10).timestamp())
        self.assertEqual(outages.parse_schedule(schedule(), NOW.replace(hour=10)), [])
        with self.assertRaisesRegex(ValueError, 'time zone'):
            outages.parse_schedule(schedule(), NOW.replace(tzinfo=None))

    def test_unknown_old_and_distant_dates_do_not_inherit_a_previous_day(self):
        for date in ('հոկտեմբերի 2-ին', '2025 թվականի հոկտեմբերի 3-ին',
                     'հոկտեմբերի 11-ին', 'հոկտեմբերի 99-ին', 'անհայտ օրը'):
            with self.subTest(date=date):
                blocks = schedule() + schedule(date=date)
                self.assertEqual(len(outages.parse_schedule(blocks, NOW)), 1)
        self.assertEqual(len(outages.parse_schedule(schedule(date='2026 թ. հոկտեմբերի 3-ին'), NOW)), 1)

    def test_year_boundary_resolves_only_the_upcoming_schedule(self):
        now = dt.datetime(2026, 12, 30, 8, tzinfo=UTC)
        record = outages.parse_schedule(schedule(date='հունվարի 2-ին'), now)[0]
        self.assertEqual(record['properties']['starts_at'], '2027-01-02T07:00:00+00:00')
        self.assertEqual(outages.parse_schedule(schedule(date='դեկտեմբերի 29-ին'), now), [])

    def test_clock_errors_duplicates_and_unknown_regions_are_rejected(self):
        for hours in ('25:00–26:00', '11:99–14:00', '14:00–11:00', '12:00–12:00'):
            self.assertEqual(outages.parse_schedule(schedule(hours=hours), NOW), [])
        self.assertEqual(len(outages.parse_schedule(schedule() * 2, NOW)), 1)
        blocks = schedule() + ['Անհայտ մարզ՝', '11:00–14:00 Արտաշատ քաղաք՝']
        self.assertEqual(len(outages.parse_schedule(blocks, NOW)), 1)

    def test_published_heading_punctuation_and_yerevan_ward_spellings(self):
        for ending in ('՝', '`', ':', '՛'):
            self.assertEqual(len(outages.parse_schedule(schedule(region='Արարատի մարզ' + ending), NOW)), 1)
        for region in ('Երևանի Մալաթիա-Սեբաստիա վարչական շրջան՝',
                       'Երեւանի Մալաթիա–Սեբաստիա վարչական շրջան՝'):
            records = outages.parse_schedule(schedule(address='Փողոց 27 շենք,', region=region), NOW)
            self.assertEqual(names(records), ['Malatia-Sebastia'])

    def test_street_names_do_not_create_village_points(self):
        records = outages.parse_schedule(schedule(address='Արտաշատ քաղաք՝ Ս․ Հակոբյան, Մխչյան, Օգոստոսի 23-ի փողոցներ մասնակի,'), NOW)
        self.assertEqual(names(records), ['Artashat'])
        self.assertEqual(outages.parse_schedule(schedule(address='Մխչյան փողոց 10,'), NOW), [])

    def test_shared_village_qualifier_and_ena_spellings_are_mapped(self):
        records = outages.parse_schedule(schedule(address='Ոսկետափ, Երասխ, Գոռավան գյուղեր մասնակի, Այգավան գյուղ մասնակի,'), NOW)
        self.assertEqual(set(names(records)), {'Vosketap', 'Yeraskh', 'Goravan', 'Aygavan'})
        records = outages.parse_schedule(schedule(address='Այնթափ, Նոր Խարբերդ գյուղեր մասնակի,'), NOW)
        self.assertEqual(set(names(records)), {'Ayntap', 'Nor Kharberd'})
        records = outages.parse_schedule(schedule(address='Հեր Հեր գյուղ մասնակի,', region='Վայոց ձորի մարզ՝'), NOW)
        self.assertEqual(names(records), ['Herher'])

    def test_city_village_homonyms_use_the_published_settlement_type(self):
        refs = outages.catalog()
        village = outages.locations_for('գեղարքունիքի', 'Մարտունի գյուղ՝ 1-ին փողոց', refs)
        city = outages.locations_for('գեղարքունիքի', 'Մարտունի քաղաք՝ 1-ին փողոց', refs)
        self.assertEqual([p['id'] for p in village], ['616437'])
        self.assertEqual([p['id'] for p in city], ['616438'])
        self.assertEqual(outages.locations_for('գեղարքունիքի', 'Մարտունի փողոց', refs), [])
        self.assertEqual(outages.locations_for('սյունիքի', 'Մարտունի գյուղ', refs), [])

    def test_yerevan_group_can_include_a_unique_named_external_village(self):
        records = outages.parse_schedule(schedule(
            region='Երևանի Աջափնյակ վարչական շրջան՝',
            address='Եղվարդի խճուղի 115 հասցե, Զովունի գյուղ՝ 1-ին փողոց,'), NOW)
        self.assertEqual(set(names(records)), {'Ajapnyak', 'Zovuni'})
        self.assertEqual(len({tuple(f['geometry']['coordinates']) for f in records}), 2)
        records = outages.parse_schedule(schedule(
            region='Երևանի Աջափնյակ վարչական շրջան՝', address='Զովունի փողոց 1,'), NOW)
        self.assertEqual(names(records), ['Ajapnyak'])

    def test_known_urban_neighbourhood_reference_does_not_map_rural_community_centres(self):
        records = outages.parse_schedule(schedule(region='Կոտայքի մարզ՝',
            address='Չարենցավան համայնք՝ 10-րդ թաղամաս 36 շենք, Սարալանջ թաղամաս,'), NOW)
        self.assertEqual(names(records), ['Charentsavan'])
        records = outages.parse_schedule(schedule(region='Շիրակի մարզ՝',
            address='Աշոցք համայնք՝ Սառնաղբյուր գյուղ,'), NOW)
        self.assertEqual(names(records), ['Sarnaghbyur'])
        self.assertEqual(outages.parse_schedule(schedule(address='Անհայտ գյուղ,'), NOW), [])

    def test_html_reads_only_current_planned_span_not_legacy_breakdowns(self):
        body = '<table><tr><td>Վթար 2025 11:00</td></tr></table>'
        body += '<span id="ctl00_ContentPlaceHolder1_attenbody"><p>' + header() + '</p>'
        body += '<p><span>Արարատի մարզ՝</span></p><p>11:00–14:00 <b>Արտաշատ քաղաք՝</b><br>Մխչյան փողոց</p></span>'
        body += '<p>' + header('2025 թվականի հոկտեմբերի 3-ին') + '</p>'
        blocks = outages.planned_blocks(body)
        self.assertEqual(blocks[0], header())
        self.assertNotIn('Վթար', ' '.join(blocks))
        self.assertNotIn('2025', ' '.join(blocks))
        self.assertEqual(len(outages.parse_schedule(blocks, NOW)), 1)
        self.assertIn('Մխչյան փողոց', outages.parse_schedule(blocks, NOW)[0]['properties']['source_address'])
        for bad in ('<p>' + header() + '</p>', '<span id="ctl00_ContentPlaceHolder1_attenbody">changed</span>'):
            with self.assertRaisesRegex(ValueError, 'section changed'):
                outages.planned_blocks(bad)
        with self.assertRaisesRegex(ValueError, 'row limit'):
            outages.planned_blocks('<span id="ctl00_ContentPlaceHolder1_attenbody">' + ('<p>' + header() + '</p>') * 2001 + '</span>')

    def test_html_wrapped_addresses_and_distinct_clock_rows_stay_separate(self):
        body = '<span id="ctl00_ContentPlaceHolder1_attenbody"><p>' + header() + '</p>'
        body += '<p>Արարատի մարզ՝<br>11:00–14:00<br>Արտաշատ քաղաք՝<br>Մխչյան փողոց'
        body += '<br>12:00–15:00 Այգավան գյուղ,<br>1-ին փողոց</p>'
        body += '<p>Սպառած էլեկտրաէներգիայի վերաբերյալ զանգահարեք։</p></span>'
        records = outages.parse_schedule(outages.planned_blocks(body), NOW)
        self.assertEqual(names(records), ['Artashat', 'Aygavan'])
        self.assertEqual(records[0]['properties']['source_address'], 'Արտաշատ քաղաք՝ Մխչյան փողոց')
        self.assertEqual(records[1]['properties']['source_address'], 'Այգավան գյուղ, 1-ին փողոց')

    def test_shared_hourly_fetch_still_rechecks_end_time_for_each_snapshot(self):
        with mock.patch.dict(outages._CACHE, {'until': 0, 'blocks': []}, clear=True), \
                mock.patch.object(outages, '_read_schedule', return_value=schedule()) as read:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: outages.ena_planned_outages(NOW), range(16)))
            self.assertTrue(all(len(r) == 1 for r in results))
            read.assert_called_once()
            self.assertEqual(outages.ena_planned_outages(NOW.replace(hour=10)), [])
            read.assert_called_once()
            outages._CACHE['until'] = 0
            with mock.patch.object(outages, '_read_schedule', side_effect=OSError('feed down')):
                with self.assertRaises(OSError):
                    outages.ena_planned_outages(NOW)

    def test_static_source_redirect_and_response_bounds(self):
        with self.assertRaisesRegex(ValueError, 'redirect'):
            outages._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://localhost/private')
        for status, url, body in ((500, outages.SOURCE_URL, b''),
                                  (200, 'http://localhost', b''),
                                  (200, outages.SOURCE_URL, b'x' * 2000001)):
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.status, response.geturl.return_value, response.read.return_value = status, url, body
            with mock.patch.object(outages.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(ValueError):
                    outages._read_schedule()
        self.assertIs(infrastructure._FETCHERS['power']['am_ena_planned'], outages.ena_planned_outages)


if __name__ == '__main__':
    unittest.main()
