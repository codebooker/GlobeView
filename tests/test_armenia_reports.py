import datetime as dt
import json
import threading
import unittest
from unittest import mock

import armenia_reports as reports
import international_emergency


NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc)
SLUG = 'rockfall-aragacotn'
# Public 2 October report: retain the location clause and terminal status,
# rather than private personal details or the site's navigation/configuration.
OPENING = ('Հոկտեմբերի 2-ին՝ ժամը 09։41-ին, Արագածոտնի մարզային օպերատիվ կառավարման '
           'կենտրոն ահազանգ է ստացվել, որ Օշականի ոլորաններից դեպի Ձորակ թաղամաս '
           'տանող ճանապարհին ժայռաբեկորը պոկվել է և ընկել ճանապարհի երթևեկելի հատված։')
BODY = '<p>' + OPENING + '</p><p>Քարերը հեռացրել են ճանապարհահատվածից․ երթևեկությունը վերականգնվել է։</p>'


def page(value):
    """Encode the provider's devalue reference table, including undefined -1."""
    table = []

    def encode(value):
        index = len(table)
        table.append(None)
        if isinstance(value, dict):
            table[index] = {key: encode(item) for key, item in value.items()}
        elif isinstance(value, list):
            table[index] = [encode(item) for item in value]
        else:
            table[index] = value
        return index

    encode(value)
    return '<script id="__NUXT_DATA__" type="application/json">' + json.dumps(table) + '</script>'


def card(slug=SLUG, date='2026-10-02T11:34:36.000Z', category='Պատահարներ', path='news'):
    return {'slug': slug, 'dateCreated': date, 'contentType': {'path': path},
            'categories': [{'title': category}]}


def article(body=BODY, published='2026-10-02T11:34:36.000Z', status='published'):
    return page({'slug': SLUG, 'status': status, 'publication_date': published,
                 'content_blocks': [{'collection': 'block_richtext', 'item': {'content': body}}]})


class ArmeniaReportsTests(unittest.TestCase):
    def test_public_rockfall_report_is_located_and_resolved(self):
        items = reports.parse_report(article(), SLUG, NOW)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['id'], 'am:rescue:' + SLUG)
        self.assertEqual(item['title'], 'Rockfall response · Oshakan area')
        self.assertEqual((item['lon'], item['lat']), (44.31671, 40.26392))
        self.assertIn('Traffic restored', item['detail'])
        self.assertIn('not the exact incident location', item['detail'])
        self.assertEqual(item['record_kind'], 'published_rescue_report')
        self.assertEqual(item['observed'], '2026-10-02T11:34:36Z')
        self.assertEqual(item['sourceUrl'], reports.BASE + '/articles/news/' + SLUG)

    def test_old_future_unpublished_and_republished_old_incidents_are_omitted(self):
        for published in ('2026-09-29T00:00:00Z', '2026-10-04T00:00:00Z', '2025-10-02T11:00:00Z'):
            self.assertEqual(reports.parse_report(article(published=published), SLUG, NOW), [])
        self.assertEqual(reports.parse_report(article(status='draft'), SLUG, NOW), [])
        old_body = BODY.replace('Հոկտեմբերի 2-ին', 'Հոկտեմբերի 2-ին՝ 2025 թվականին')
        self.assertEqual(reports.parse_report(article(body=old_body), SLUG, NOW), [])
        self.assertEqual(reports.parse_report(article(body=BODY.replace('Հոկտեմբերի 2-ին', 'Սեպտեմբերի 20-ին')), SLUG, NOW), [])

    def test_month_inflections_and_year_rollover(self):
        body = BODY.replace('Հոկտեմբերի 2-ին', 'Դեկտեմբերի 31-ին')
        now = dt.datetime(2027, 1, 1, 12, tzinfo=dt.timezone.utc)
        self.assertEqual(len(reports.parse_report(article(body=body, published='2027-01-01T01:00:00Z'), SLUG, now)), 1)

    def test_responder_office_is_not_the_incident_location(self):
        body = BODY.replace('Օշականի ոլորաններից դեպի Ձորակ թաղամաս', 'անհայտ վայր')
        body = body.replace('Արագածոտնի մարզային', 'Օշականի Արագածոտնի մարզային')
        self.assertEqual(reports.parse_report(article(body=body), SLUG, NOW), [])

    def test_ambiguous_places_and_conflicting_provinces_are_omitted(self):
        for body in (BODY.replace('Օշականի', 'Օշականի և Աշտարակի'), BODY.replace('Արագածոտնի', 'Սյունիքի')):
            self.assertEqual(reports.parse_report(article(body=body), SLUG, NOW), [])

    def test_unverified_report_does_not_claim_active_emergency(self):
        body = '<p>' + OPENING.replace('ժայռաբեկորը պոկվել է', 'հրդեհ է բռնկվել') + '</p>'
        item = reports.parse_report(article(body=body), SLUG, NOW)[0]
        self.assertEqual(item['category'], 'fire')
        self.assertIn('current status unverified', item['detail'])

    def test_index_filters_categories_dates_and_unsafe_slugs(self):
        cards = [card(), card(category='Եղանակը Հայաստանում', slug='weather'),
                 card(date='2026-09-01T00:00:00Z', slug='old'), card(slug='../admin'),
                 card(slug='other-type', path='services')]
        self.assertEqual(reports.article_links(page(cards), NOW), {SLUG: reports.BASE + '/articles/news/' + SLUG})

    def test_index_or_article_structure_change_is_an_error_not_empty_success(self):
        for value in ('<html></html>', page({'foo': 'bar'})):
            with self.assertRaises(ValueError):
                reports.article_links(value, NOW)
        with self.assertRaises(ValueError):
            reports.parse_report(page({'slug': SLUG}), SLUG, NOW)
        with self.assertRaises(ValueError):
            reports.parse_report(page({'slug': SLUG, 'status': 'published',
                                       'publication_date': '2026-10-02T11:34:36Z', 'content_blocks': []}), SLUG, NOW)
        self.assertIsNone(reports._ref(['unsafe-last-entry'], -1))

    def test_reader_rejects_cross_host_redirects_and_plain_http(self):
        for url in ('http://rescue.mia.gov.am/', 'https://127.0.0.1/',
                    'https://rescue.mia.gov.am.evil.example/', 'https://u:p@rescue.mia.gov.am/',
                    'https://rescue.mia.gov.am:444/'):
            with self.assertRaises(ValueError):
                reports._safe_url(url)

    def test_listing_union_deduplicates_and_article_cache_is_bounded(self):
        calls = []

        def read(url):
            calls.append(url)
            return article() if '/articles/' in url else page([card()])

        with mock.patch.dict(reports._ARTICLE_CACHE, {}, clear=True), mock.patch.object(reports, '_read', side_effect=read):
            self.assertEqual(len(reports.rescue_reports(NOW)), 1)
            self.assertEqual(len(reports.rescue_reports(NOW)), 1)
        self.assertEqual(calls.count(reports.BASE + '/articles/news/' + SLUG), 1)

    def test_multiple_clients_share_one_international_refresh(self):
        cache = {'value': None, 'until': 0, 'sources': {}, 'source_times': {}}
        with mock.patch.dict(international_emergency._CACHE, cache, clear=True), \
                mock.patch.dict(international_emergency._LOADERS, {'am_rescue': lambda: reports.rescue_reports(NOW)}, clear=True), \
                mock.patch.dict(reports._ARTICLE_CACHE, {}, clear=True), \
                mock.patch.object(reports, '_read', side_effect=lambda url: article() if '/articles/' in url else page([card()])) as read:
            output = []
            threads = [threading.Thread(target=lambda: output.append(international_emergency.international_emergency_snapshot())) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertEqual(len(output), 8)
            self.assertEqual(read.call_count, 3)
            self.assertTrue(all(value['sourceCounts'] == {'am_rescue': 1} for value in output))
            self.assertTrue(all(not value['sourceErrors'] for value in output))


if __name__ == '__main__':
    unittest.main()
