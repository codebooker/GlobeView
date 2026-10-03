import csv
import datetime as dt
import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import azerbaijan_outages as outages


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 3, 8, tzinfo=UTC)
DOWNLOAD = (f'https://admin.opendata.az/dataset/{outages.DATASET_ID}/resource/'
            f'{outages.RESOURCE_ID}/download/schedule.csv')


def row(network='Quba', address='Quba şəhəri', date='03.10.2026', hours='10:00-13:00'):
    return ['1', network, date, hours, 'Xəttin təmiri', address]


def table(rows):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(outages._FIELDS)
    writer.writerows(rows)
    return stream.getvalue()


def metadata(**kwargs):
    package = {'id': outages.DATASET_ID, 'license_id': 'cc-zero',
               'organization': {'name': 'azerisiq-asc'},
               'resources': [{'id': outages.RESOURCE_ID, 'format': 'CSV', 'state': 'active',
                              'url': DOWNLOAD, 'last_modified': '2026-10-01T07:03:30'}]}
    package.update(kwargs)
    return json.dumps({'success': True, 'result': package})


class AzerbaijanOutageTests(unittest.TestCase):
    def test_published_place_and_utc4_window_without_invented_impact(self):
        rows = [row(), row('Ağcabədi', 'Ağcabədi rayonu, Salmanbəyli kəndi (470 nəfər)',
                           date='05.10.2026', hours='10:00-10:30')]
        result = outages.parse_schedule(rows, NOW)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['geometry']['coordinates'], [48.51341, 41.36108])
        current, future = [p['properties'] for p in result]
        self.assertEqual(current['starts_at'], '2026-10-03T06:00:00+00:00')
        self.assertEqual(current['ends_at'], '2026-10-03T09:00:00+00:00')
        self.assertEqual(current['valid_until'], NOW.timestamp() + 900)
        self.assertTrue(current['status'].startswith('Planned work window'))
        self.assertTrue(future['status'].startswith('Scheduled'))
        self.assertIsNone(future['customers_affected'])
        self.assertIn('470 nəfər', future['source_address'])
        self.assertIn('Approximate place reference', future['area_name'])
        self.assertIn('147274', future['location_source_url'])
        self.assertEqual(current['source_url'], outages.SOURCE_URL)

    def test_homonyms_are_scoped_and_ambiguous_locations_are_omitted(self):
        self.assertEqual([p['id'] for p in outages.locations_for('Quba', 'Quba şəhəri')], ['585221'])
        self.assertEqual([p['id'] for p in outages.locations_for('Xaçmaz', 'Xaçmaz şəhəri')], ['584717'])
        self.assertEqual([p['id'] for p in outages.locations_for('Şəki', 'Şəki şəhəri')], ['585170'])
        self.assertEqual(outages.locations_for('Şəki', 'Şəki'), [])
        self.assertEqual(outages.locations_for('Nərimanov', 'Nərimanov rayonu, Ağa Neymətulla küçəsi'), [])
        self.assertEqual(outages.locations_for('Kürqırağı', 'Quba'), [])
        self.assertEqual(outages.locations_for('Quba', 'Bilgəh qəsəbəsi'), [])

    def test_administrative_and_street_prefixes_do_not_create_extra_points(self):
        places = outages.locations_for('Ağcabədi', 'Ağcabədi rayonu, Salmanbəyli kəndi')
        self.assertEqual([p['id'] for p in places], ['147274'])
        self.assertEqual(outages.locations_for('Abşeron', 'Xırdalan küçəsi 10'), [])
        self.assertEqual([p['id'] for p in outages.locations_for('Maştağa', 'Bakı şəhəri, Bilgəh qəsəbəsi')],
                         ['586971'])
        self.assertEqual(outages.locations_for('Quba', 'Naməlum kənd'), [])

    def test_expired_old_ambiguous_and_distant_schedules_are_not_live(self):
        values = [row(hours='10:00-12:00'), row(date='03.10.2025'), row(date='11.10.2026'),
                  row(date='03-05.10.2026'), row(date='03.10.2026\n04.10.2026'),
                  row(hours='3 saat'), row(hours='25:00-26:00'), row(hours='13:00-10:00')]
        self.assertEqual(outages.parse_schedule(values, NOW), [])
        # At the exact end, the current schedule is removed, not cached all month.
        self.assertEqual(outages.parse_schedule([row()], NOW.replace(hour=9)), [])
        with self.assertRaisesRegex(ValueError, 'time zone'):
            outages.parse_schedule([row()], NOW.replace(tzinfo=None))

    def test_published_date_and_clock_separators_are_supported(self):
        for hours in ('10:00-13:00', '10.00–13.00', '1000-1300', '10:00-13;00'):
            with self.subTest(hours=hours):
                self.assertEqual(len(outages.parse_schedule([row(date='03,10,2026.', hours=hours)], NOW)), 1)
        self.assertEqual(len(outages.parse_schedule([row(), row()], NOW)), 1)

    def test_csv_schema_and_size_are_bounded(self):
        self.assertEqual(outages._rows(table([row()])), [row()])
        self.assertEqual(outages._rows(table([['bad']])), [])
        with self.assertRaisesRegex(ValueError, 'columns'):
            outages._rows('unexpected;csv\n')
        with self.assertRaisesRegex(ValueError, 'row limit'):
            outages._rows(table([row()] * 5001))

    def test_only_the_published_dataset_and_storage_redirects_are_allowed(self):
        storage = f'https://data-storage.opendata.az/ckan-prod-storage/ckan/resources/{outages.RESOURCE_ID}/schedule.csv?signature=example'
        for url in (outages.API_URL, DOWNLOAD, storage):
            self.assertTrue(outages._allowed_url(url))
        for url in ('http://admin.opendata.az/', 'https://localhost/schedule.csv',
                    'https://admin.opendata.az.evil.example/schedule.csv',
                    DOWNLOAD.replace(outages.RESOURCE_ID, 'other-resource'),
                    DOWNLOAD + '#fragment', DOWNLOAD.replace('https://', 'https://user:pass@'),
                    DOWNLOAD.replace('schedule.csv', '../../private.csv'),
                    DOWNLOAD.replace('schedule.csv', '%2e%2e/private.csv'),
                    storage.replace('data-storage.opendata.az', '169.254.169.254')):
            self.assertFalse(outages._allowed_url(url))
        with self.assertRaisesRegex(ValueError, 'redirect'):
            outages._Redirect().redirect_request(None, None, 302, '', {}, 'https://localhost/private.csv')

    def test_dataset_identity_license_and_resource_are_checked(self):
        for changes in ({'id': 'other'}, {'license_id': 'restricted'},
                        {'organization': {'name': 'other'}}, {'resources': []}):
            with self.subTest(changes=changes), mock.patch.dict(outages._CACHE, until=0), \
                    mock.patch.object(outages, '_read', return_value=metadata(**changes)) as read:
                with self.assertRaises(ValueError):
                    outages._schedule()
                self.assertEqual(read.call_count, 1)

    def test_concurrent_users_share_downloads_but_expiry_is_checked_each_time(self):
        def read(url, limit):
            return metadata() if url == outages.API_URL else table([row()])
        with mock.patch.dict(outages._CACHE, until=0, rows=[], modified=''), \
                mock.patch.object(outages, '_read', side_effect=read) as fetch:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: outages.azerishiq_planned_outages(NOW), range(8)))
            self.assertTrue(all(len(items) == 1 for items in results))
            self.assertEqual(fetch.call_count, 2)  # One metadata + one CSV, total.
            self.assertEqual(outages.azerishiq_planned_outages(NOW.replace(hour=9)), [])
            self.assertEqual(fetch.call_count, 2)
            with mock.patch.object(outages.time, 'time', return_value=outages._CACHE['until'] + 1):
                outages.azerishiq_planned_outages(NOW)
            self.assertEqual(fetch.call_count, 4)
