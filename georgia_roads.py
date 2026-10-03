"""Public Georgian Roads Department notices with verified road references."""

import calendar
import datetime as dt
import functools
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.request

API_URL = 'https://api.georoad.gov.ge/api/restrictions'
SOURCE_URL = 'https://georoad.gov.ge/en/restriction'
_ZONE = dt.timezone(dt.timedelta(hours=4))
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'rows': []}
MAX_AGE_DAYS = 130  # Up to three calendar months, starting within a month of publication.
_MONTHS = {calendar.month_name[i].lower(): i for i in range(1, 13)}
_START = re.compile(r'\bfrom (' + '|'.join(_MONTHS) + r') (\d{1,2})(?:, (20\d{2}))?, traffic\b', re.I)
_WEEKDAY = re.compile(r'\bfrom Monday through Friday, between (\d{2}):(\d{2}) and (\d{2}):(\d{2})\b', re.I)
_WEEKEND = re.compile(r'\bon weekends, between (\d{2}):(\d{2}) and (\d{2}):(\d{2})\b', re.I)
_DURATION = re.compile(r'\b(?:This traffic regime will remain in effect|for) (?:for )?(one|two|three) months?\b', re.I)
REPORT_LOCATION_PATH = Path(__file__).with_name('georgia-road-report-locations.json')
REPORT_MAX_AGE_DAYS = 7
_TUSHETI_ROAD = re.compile(r'(?:Pshaveli\s*[-–—]\s*Abano\s*[-–—]\s*Omalo|'
                          r'ფშაველი\s*[-–—]\s*აბანო\s*[-–—]\s*ომალო)', re.I)
_KM_RANGE = re.compile(r'კმ\s*(\d{1,3})\s*[-–—]\s*კმ\s*(\d{1,3})')


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.skip += 1
        if tag in {'p', 'br', 'div'}:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.skip:
            self.skip -= 1
        if tag in {'p', 'div'}:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(value):
    parser = _Text()
    parser.feed(value)
    return ' '.join(''.join(parser.parts).split())


@functools.lru_cache(maxsize=1)
def catalog():
    data = json.loads(Path(__file__).with_name('georgia-road-locations.json').read_text(encoding='utf-8'))
    if (data.get('licence') != 'ODbL 1.0'
            or data.get('locationKind') != 'approximate_named_road_reference'):
        raise ValueError('Georgia road-reference provenance changed')
    rows = data['locations']
    if len(rows) != 1 or rows[0]['id'] != 'batumi-bypass':
        raise ValueError('Georgia verified-road catalog changed')
    row = rows[0]
    paths = row['roadSegments']
    if (len(paths) != 1 or not 20 <= len(paths[0]) <= 5000
            or row['coordinates'] not in paths[0] or not isinstance(row['osmNode'], int)
            or not all(math.isfinite(v) for p in paths[0] for v in p)
            or not all(len(p) == 2 and 41.6 <= p[0] <= 41.75 and 41.57 <= p[1] <= 41.71 for p in paths[0])):
        raise ValueError('Invalid Georgia road reference')
    return {row['id']: row for row in rows}


def _publication(row):
    return dt.datetime.strptime(row['publish_date'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=_ZONE)


def _record(row):
    if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
            or type(row.get('status')) is not int or row['status'] != 1 or row.get('deleted_at') is not None):
        return None
    if not isinstance(row.get('translates'), list):
        return None
    translations = [t for t in row['translates'] if isinstance(t, dict) and t.get('language') == 'en']
    if len(translations) != 1:
        return None
    translation = translations[0]
    title, content = translation.get('title'), translation.get('content')
    if not isinstance(title, str) or not isinstance(content, str) or len(title) > 1000 or len(content) > 50000:
        return None
    try:
        published = _publication(row)
    except (KeyError, TypeError, ValueError):
        return None
    return published, plain_text(title), plain_text(content)


def _window(matches):
    if len(matches) != 1:
        return None
    try:
        first, last = dt.time(int(matches[0][0]), int(matches[0][1])), dt.time(int(matches[0][2]), int(matches[0][3]))
    except ValueError:
        return None
    # Overnight windows need an explicit source date rule; do not infer one.
    return (first, last) if first < last else None


def _schedule(text, published):
    starts, durations = list(_START.finditer(text)), list(_DURATION.finditer(text))
    weekdays, weekends = _window(_WEEKDAY.findall(text)), _window(_WEEKEND.findall(text))
    if (len(starts) != 1 or len(durations) != 1 or not weekdays or not weekends
            or not re.search(r'During daytime hours, traffic on the Batumi Bypass Road will continue as usual', text, re.I)):
        return None
    start_match = starts[0]
    month, day = _MONTHS[start_match[1].lower()], int(start_match[2])
    candidates = []
    for year in range(published.year - 1, published.year + 2):
        if start_match[3] and int(start_match[3]) != year:
            continue
        try:
            value = dt.datetime(year, month, day, tzinfo=_ZONE)
        except ValueError:
            continue
        if -1 <= (value.date() - published.date()).days <= 31:
            candidates.append(value)
    if len(candidates) != 1:
        return None
    start = candidates[0]
    months = {'one': 1, 'two': 2, 'three': 3}[durations[0][1].lower()]
    month_index = start.year * 12 + start.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    end = start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))
    return start, end, weekdays, weekends


def _parse_batumi_restrictions(rows, now=None, refs=None):
    now = now or dt.datetime.now(_ZONE)
    if now.tzinfo is None:
        raise ValueError('Georgia road clock must be timezone aware')
    now = now.astimezone(_ZONE)
    refs = catalog() if refs is None else refs
    # Select the newest notice before interpreting its schedule. A newer reopening
    # or unsupported change must not leave an older restriction visible.
    notices = []
    for row in rows:
        record = _record(row)
        if record is None:
            continue
        published, title, text = record
        if not 0 <= (now - published).total_seconds() <= MAX_AGE_DAYS * 86400:
            continue
        if re.search(r'\bBatumi (?:Bypass )?Road\b', title, re.I) and 'bypass' in title.lower():
            notices.append((published, row['id'], str(row.get('restriction_status')), title, text))
    if not notices:
        return []
    latest = max(notice[0] for notice in notices)
    candidates = {n for n in notices if n[0] == latest}
    if len(candidates) != 1:
        return []
    published, record_id, status, title, text = candidates.pop()
    if status not in {'1', '3'} or re.search(r'\b(?:restored|reopened|completed)\b', title, re.I):
        return []
    schedule = _schedule(text, published)
    if schedule is None or not re.search(r'\b(?:installation|works)\b', text, re.I):
        return []
    start, end, weekdays, weekends = schedule
    if now >= end:
        return []
    # Determine this work window or the next one; Saturday/Sunday use their own hours.
    window = None
    for offset in range(34):
        day = now.date() + dt.timedelta(days=offset)
        times = weekends if day.weekday() >= 5 else weekdays
        first, last = [dt.datetime.combine(day, clock, _ZONE) for clock in times]
        first, last = max(first, start), min(last, end)
        if first < last and last > now:
            window = first, last
            break
    if window is None:
        return []
    first, last = window
    active = first <= now < last
    phase = 'Night restriction' if active else 'Open now · Next restriction' if now >= start else 'Scheduled night restriction'
    times = f'{first:%d %b} · {first:%H:%M}–{last:%H:%M} UTC+4'
    detail = (f'{phase}: {times}. Weekdays {weekdays[0]:%H:%M}–{weekdays[1]:%H:%M}; '
              f'weekends {weekends[0]:%H:%M}–{weekends[1]:%H:%M}. '
              'Detour via Batumi city. Approximate bypass path.')
    location = refs.get('batumi-bypass')
    if location is None:
        return []
    path = location['roadSegments'][0]
    bounds = [min(p[0] for p in path), min(p[1] for p in path), max(p[0] for p in path), max(p[1] for p in path)]
    transition = last if active else first
    return [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': location['coordinates']},
             'properties': {'key': 'ge:georoad:batumi-bypass', 'layer': 'construction',
                            'title': 'Batumi bypass · Tunnel works', 'detail': detail,
                            'source': 'Georgia Roads Department · OpenStreetMap (ODbL)',
                            'source_url': SOURCE_URL + '/' + str(record_id),
                            'location_kind': 'approximate_named_road_reference',
                            'location_source_url': 'https://www.openstreetmap.org/node/' + str(location['osmNode']),
                            'record_kind': 'scheduled_recurring_road_restriction',
                            'reported_at': published.isoformat(), 'starts_at': start.isoformat(),
                            'ends_at': end.isoformat(), 'window_starts_at': first.isoformat(),
                            'window_ends_at': last.isoformat(), 'restriction_active': active,
                            'road_segments': location['roadSegments'], 'road_segment_bounds': bounds,
                            'segment_color': '#ed7770' if active else '#edb965',
                            'valid_until': min(now.timestamp() + 900, transition.timestamp(), end.timestamp())}}]


@functools.lru_cache(maxsize=1)
def report_catalog():
    data = json.loads(REPORT_LOCATION_PATH.read_text(encoding='utf-8'))
    if (data.get('licence') != 'ODbL 1.0'
            or data.get('source') != 'https://www.openstreetmap.org/copyright'
            or data.get('locationKind') != 'approximate_named_road_reference'
            or not re.fullmatch(r'[0-9a-f]{64}', str(data.get('osmSha256', '')))):
        raise ValueError('Georgia road-report provenance changed')
    rows = data.get('locations')
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Georgia road-report reference missing')
    row = rows[0]
    point = row.get('coordinates')
    if (row.get('id') != 'pshaveli-abano-omalo' or row.get('osmNode') != 824101262
            or row.get('osmRoadWay') != 779659168 or row.get('osmRoadRelation') != 19687627
            or not isinstance(point, list) or len(point) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point)
            or not 45.49 <= point[0] <= 45.53 or not 42.26 <= point[1] <= 42.30):
        raise ValueError('Invalid Abano Pass road reference')
    return {row['id']: row}


def _parse_tusheti_restrictions(rows, now, refs):
    location = refs.get('pshaveli-abano-omalo')
    if location is None:
        return []
    notices = []
    for row in rows:
        record = _record(row)
        if record is None:
            continue
        published, title, text = record
        if (0 <= (now - published).total_seconds() < REPORT_MAX_AGE_DAYS * 86400
                and _TUSHETI_ROAD.search(title)):
            notices.append((published, row['id'], str(row.get('restriction_status')), title, text))
    if not notices:
        return []
    latest = max(n[0] for n in notices)
    candidates = {n for n in notices if n[0] == latest}
    if len(candidates) != 1:
        return []
    published, record_id, status, title, text = candidates.pop()
    # A newer reopening or unsupported change invalidates the previous report.
    if (status not in {'1', '3'} or re.search(r'აღდგა|აღდგენილია|restored|reopened', title, re.I)
            or not _TUSHETI_ROAD.search(text)
            or not all(phrase in text for phrase in ('ინტენსიური თოვის', 'ლიპყინულის',
                       'მაღალი გამავლობის', 'მოცურების საწინააღმდეგო ჯაჭვების გამოყენებით',
                       'მოძრაობა თავისუფალია'))
            or re.search(r'აკრძალულ|prohibited|closed', text, re.I)):
        return []
    clauses = re.split(r'(?<!\w)ხოლო(?!\w)', text)
    if len(clauses) != 2 or text.count('მაღალი გამავლობის') != 2:
        return []
    first_ranges = _KM_RANGE.findall(clauses[0])
    last_ranges = _KM_RANGE.findall(clauses[1])
    if len(first_ranges) != 1 or len(last_ranges) != 3:
        return []
    chains, high_first, high_last, unrestricted = [tuple(map(int, pair))
                                                for pair in first_ranges + last_ranges]
    if (not all(1 <= a < b <= 150 for a, b in (chains, high_first, high_last, unrestricted))
            or not unrestricted[1] <= high_first[0] < high_first[1] < chains[0] < chains[1] < high_last[0]):
        return []
    km = lambda pair: f'{pair[0]}–{pair[1]}'
    detail = (f'High clearance + chains: km {km(chains)}. High clearance only: '
              f'km {km(high_first)} and {km(high_last)}. Unrestricted: km {km(unrestricted)}. '
              f'Reported {published:%d %b, %H:%M} UTC+4. Approximate Abano Pass road reference.')
    expiry = published + dt.timedelta(days=REPORT_MAX_AGE_DAYS)
    return [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': location['coordinates']},
             'properties': {'key': 'ge:georoad:pshaveli-abano-omalo', 'layer': 'incidents',
                            'title': 'Abano Pass road · Snow & ice restrictions', 'detail': detail,
                            'source': 'Georgia Roads Department · translated from Georgian · OSM (ODbL)',
                            'source_url': SOURCE_URL + '/' + str(record_id),
                            'location_kind': 'approximate_named_road_reference',
                            'location_source_url': 'https://www.openstreetmap.org/node/' + str(location['osmNode']),
                            'record_kind': 'published_road_restriction', 'reported_at': published.isoformat(),
                            'source_language': 'ka', 'chains_km': list(chains),
                            'high_clearance_km': [list(high_first), list(high_last)],
                            'unrestricted_km': list(unrestricted),
                            'valid_until': min(now.timestamp() + 900, expiry.timestamp())}}]


def parse_restrictions(rows, now=None, refs=None):
    now = now or dt.datetime.now(_ZONE)
    if now.tzinfo is None:
        raise ValueError('Georgia road clock must be timezone aware')
    now = now.astimezone(_ZONE)
    return (_parse_batumi_restrictions(rows, now, refs)
            + _parse_tusheti_restrictions(rows, now, report_catalog() if refs is None else refs))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected Roads Department redirect')


def _read_page(page):
    url = API_URL + ('?page=' + str(page) if page > 1 else '')
    request = urllib.request.Request(url, headers={'locale': 'en', 'Accept': 'application/json',
                                                 'User-Agent': 'GlobeView/1.0 public road notices'})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError('Unexpected Roads Department response')
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Roads Department response too large')
    payload = json.loads(raw)
    items = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(items, dict) or payload.get('message') != 'Success':
        raise ValueError('Roads Department catalog missing')
    rows = items.get('data')
    if (not isinstance(rows, list) or len(rows) > 50 or type(items.get('current_page')) is not int or items['current_page'] != page
            or type(items.get('last_page')) is not int or items['last_page'] < page):
        raise ValueError('Roads Department pagination changed')
    # Retain only published notice fields, not unrelated CMS metadata.
    names = ('id', 'status', 'deleted_at', 'restriction_status', 'publish_date')
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid Roads Department record')
        if not isinstance(row.get('translates'), list):
            raise ValueError('Roads Department translations missing')
        value = {key: row.get(key) for key in names}
        value['translates'] = [{key: t.get(key) for key in ('language', 'title', 'content')}
                               for t in row.get('translates', []) if isinstance(t, dict)]
        clean.append(value)
    return clean, items['last_page']


def road_restrictions(now=None):
    now = now or dt.datetime.now(_ZONE)
    if now.tzinfo is None:
        raise ValueError('Georgia road clock must be timezone aware')
    with _LOCK:
        if _CACHE['until'] <= time.monotonic():
            rows, previous = [], None
            for page in range(1, 9):
                batch, last_page = _read_page(page)
                published = [_publication(row) for row in batch]
                if published != sorted(published, reverse=True) or (previous and published and published[0] > previous):
                    raise ValueError('Roads Department publication order changed')
                rows.extend(batch)
                if not batch or page == last_page or (now - published[-1]).total_seconds() > MAX_AGE_DAYS * 86400:
                    break
                previous = published[-1]
            else:
                raise ValueError('Recent Roads Department catalog exceeds page limit')
            _CACHE.update({'until': time.monotonic() + 600, 'rows': rows})
        return parse_restrictions(_CACHE['rows'], now)
