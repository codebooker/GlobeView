import datetime as dt
import pathlib
import unittest
from unittest import mock

import kyrgyzstan_roads as roads


FIXTURES = pathlib.Path(__file__).with_name('fixtures')
NOW = dt.datetime(2026, 10, 3, 3, tzinfo=dt.timezone.utc)
ZH_URL = roads.INDEX_URL + '/34470'
AB_URL = roads.INDEX_URL + '/34481'


def page(text):
    result = roads._Page()
    result.feed(text)
    return result


def notice(name):
    return page((FIXTURES / name).read_text(encoding='utf-8'))


class KyrgyzstanRoadTests(unittest.TestCase):
    def test_real_repair_notice_maps_only_its_two_named_street_sections(self):
        rows = roads._parse_notice(notice('bishkek-road-zhukeev-20261003.html'), ZH_URL, NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['properties']['key'].rsplit(':', 1)[-1] for r in rows},
                         {'zhukeev-mederova-karasaeva', 'zhukeev-karasaeva-ankara'})
        for row in rows:
            props = row['properties']
            self.assertEqual(props['layer'], 'construction')
            self.assertEqual(props['starts_at'], '2026-10-03T07:00:00+06:00')
            self.assertEqual(props['ends_at'], '2026-10-03T23:00:00+06:00')
            self.assertEqual(props['source_url'], ZH_URL)
            self.assertEqual(props['valid_until'], NOW.timestamp() + 900)
            self.assertIn('Announced restriction', props['detail'])
            self.assertIn('Approximate street reference', props['detail'])
        # The road reference starts at the named Mederova intersection, rather
        # than including the unrelated southern part of this OSM way.
        self.assertEqual(rows[0]['properties']['road_segments'][0][0], [74.61628, 42.8519307])
        self.assertEqual(rows[0]['properties']['road_segments'][0][-1],
                         rows[1]['properties']['road_segments'][0][0])

    def test_real_ten_day_work_uses_only_the_verified_park_reference(self):
        rows = roads._parse_notice(notice('bishkek-road-abdrakhmanov-20261002.html'), AB_URL, NOW)
        self.assertEqual(len(rows), 1)
        props = rows[0]['properties']
        self.assertEqual(props['ends_at'], '2026-10-12T00:00:00+06:00')
        self.assertIn('Friendship Park reference', props['detail'])
        self.assertEqual(rows[0]['geometry']['coordinates'], [74.6065492, 42.8232226])
        self.assertEqual(props['road_segments'], [])
        # An unnamed mosque in the same notice has no verified location and is
        # not substituted with a random mosque or the street's city centroid.
        self.assertNotIn('mosque', props['detail'])

    def test_road_section_remains_visible_when_only_its_endpoint_is_in_view(self):
        from international_infrastructure import _road_feature_in_bbox
        row = roads._parse_notice(notice('bishkek-road-zhukeev-20261003.html'), ZH_URL, NOW)[0]
        bbox = [74.6162, 42.8518, 74.6164, 42.8521]
        self.assertGreater(row['geometry']['coordinates'][1], bbox[3])
        self.assertTrue(_road_feature_in_bbox(row, bbox))
        self.assertFalse(_road_feature_in_bbox(row, [74.5, 42.8, 74.6, 42.81]))

    def test_dates_expire_and_unsupported_or_restored_schedules_are_omitted(self):
        text = (FIXTURES / 'bishkek-road-zhukeev-20261003.html').read_text(encoding='utf-8')
        near_end = NOW.replace(hour=16, minute=55)
        rows = roads._parse_notice(page(text), ZH_URL, near_end)
        self.assertEqual(rows[0]['properties']['valid_until'], NOW.replace(hour=17).timestamp())
        self.assertEqual(roads._parse_notice(page(text), ZH_URL, NOW.replace(hour=17)), [])
        earlier = NOW - dt.timedelta(days=1)
        self.assertTrue(all('Scheduled restriction' in r['properties']['detail']
                            for r in roads._parse_notice(page(text), ZH_URL, earlier)))
        for replacement in (text.replace('2026 года', '2025 года'),
                            text.replace('с 07:00 до 23:00', ''),
                            text.replace('с 07:00 до 23:00', 'с 25:00 до 23:00'),
                            text.replace('</article>', 'Движение восстановлено.</article>'),
                            text.replace('заменой поврежденых участков асфальтового покрытия', 'мероприятием'),
                            text.replace('Карасаева', 'неизвестной дороги')):
            self.assertEqual(roads._parse_notice(page(replacement), ZH_URL, NOW), [])

    def test_discovery_validates_host_path_title_and_retains_unexpired_notices(self):
        index = page(''.join(f'<a href="{url}">Внимание! Временное ограничение движения по улице</a>'
                             for url in (ZH_URL, ZH_URL, AB_URL, 'https://evil.example/ru/post/34470',
                                         ZH_URL + '?redirect=elsewhere', ZH_URL + '#elsewhere')))
        zh = notice('bishkek-road-zhukeev-20261003.html')
        ab = notice('bishkek-road-abdrakhmanov-20261002.html')
        with mock.patch.dict(roads._ACTIVE_NOTICES, {}, clear=True):
            with mock.patch.object(roads, '_read_page', side_effect=[index, page(''), page(''), zh, ab]) as read:
                rows = roads.bishkek_roadworks(NOW)
            self.assertEqual(len(rows), 3)
            self.assertEqual(read.call_args_list,
                             [mock.call(roads.INDEX_URL), mock.call(roads.INDEX_URL + '?page=2'),
                              mock.call(roads.INDEX_URL + '?page=3'), mock.call(ZH_URL), mock.call(AB_URL)])
            self.assertEqual(set(roads._ACTIVE_NOTICES), {ZH_URL, AB_URL})
            # Pagination rolling past an active notice must not remove it. Its
            # source is revalidated, rather than reusing old feature data.
            with mock.patch.object(roads, '_read_page', side_effect=[page('')] * 3 + [zh, ab]):
                self.assertEqual(len(roads.bishkek_roadworks(NOW + dt.timedelta(minutes=5))), 3)
            with mock.patch.object(roads, '_read_page', return_value=page('')) as read:
                self.assertEqual(roads.bishkek_roadworks(NOW + dt.timedelta(days=10)), [])
                self.assertEqual(read.call_count, 3)
            self.assertEqual(roads._ACTIVE_NOTICES, {})

    def test_network_reader_rejects_other_hosts_redirects_and_large_pages(self):
        for url in ('https://evil.example/ru/post/34470', 'http://www.bishkek.gov.kg/ru/post/34470',
                    ZH_URL + '?next=elsewhere', 'https://www.bishkek.gov.kg/private',
                    roads.INDEX_URL + '?page=300'):
            with mock.patch.object(roads._OPENER, 'open') as reader:
                with self.assertRaisesRegex(ValueError, 'URL'):
                    roads._read_page(url)
                reader.assert_not_called()
        response = mock.MagicMock()
        response.status = 200
        response.geturl.return_value = ZH_URL
        response.read.return_value = b'x' * 250001
        response.__enter__.return_value = response
        with mock.patch.object(roads._OPENER, 'open', return_value=response):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                roads._read_page(ZH_URL)
        with self.assertRaisesRegex(ValueError, 'redirect'):
            roads._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.example/')


if __name__ == '__main__':
    unittest.main()
