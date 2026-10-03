"""Dated Bishkek road-restriction notices with audited OSM references."""

import datetime as dt
import json
import pathlib
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser


INDEX_URL = 'https://www.bishkek.gov.kg/ru/post'
_ZONE = dt.timezone(dt.timedelta(hours=6))
_MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
           'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')
_DATE = re.compile(r'\b(\d{1,2})\s+(' + '|'.join(_MONTHS) + r')\s+(\d{4})\s+года\b')
_ROAD_TITLE = re.compile(r'(?:огранич\w*\s+движен|закры\w*.*(?:ремонт|участ|улиц)|реконструкц.*улиц)', re.I)
_RESTORED = re.compile(r'(?:движение.{0,35}(?:восстановлен|открыт)|ограничения.{0,35}снят|работы.{0,35}завершен)')
_REFERENCES = json.loads(pathlib.Path(__file__).with_name('kyrgyzstan-road-locations.json')
                         .read_text(encoding='utf-8'))['locations']
_MATCHERS = [(entry, re.compile(entry['pattern'])) for entry in _REFERENCES]
# Retain discovered, still-active notices after they roll off the first pages.
# All retained pages are re-read by the shared road loader; this is not a second
# feature cache. Unknown and expired schedules are not retained.
_ACTIVE_NOTICES = {}


def _text(value):
    return ' '.join(value.split())


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.body = []
        self.title = []
        self._href = None
        self._label = []
        self._article = False
        self._heading = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self._href, self._label = attrs.get('href'), []
        if tag == 'article' and attrs.get('id') == 'lightgallery':
            self._article = True
        if tag == 'h5':
            self._heading = True
        if tag == 'br' and self._article:
            self.body.append(' ')

    def handle_data(self, value):
        if self._href:
            self._label.append(value)
        if self._article:
            self.body.append(value)
        if self._heading:
            self.title.append(value)

    def handle_endtag(self, tag):
        if tag == 'a' and self._href:
            self.links.append((self._href, _text(''.join(self._label))))
            self._href = None
        if tag == 'article':
            self._article = False
        if tag == 'h5':
            self._heading = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('Bishkek road redirects are unsupported')


_OPENER = urllib.request.build_opener(_NoRedirect())


def _read_page(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.bishkek.gov.kg' or parsed.fragment
            or not ((parsed.path == '/ru/post' and parsed.query in ('', 'page=2', 'page=3'))
                    or (re.fullmatch(r'/ru/post/\d{1,8}', parsed.path) and not parsed.query))):
        raise ValueError('Unexpected Bishkek road URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (https://globeview.app)'})
    with _OPENER.open(request, timeout=15) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError('Unexpected Bishkek road response')
        body = response.read(250001)
    if len(body) > 250000:
        raise ValueError('Bishkek road page exceeded size limit')
    page = _Page()
    page.feed(body.decode('utf-8'))
    return page


def _notice_links(page):
    links = set()
    for href, title in page.links:
        if not _ROAD_TITLE.search(title):
            continue
        url = urllib.parse.urljoin(INDEX_URL, href)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme == 'https' and parsed.netloc == 'www.bishkek.gov.kg'
                and re.fullmatch(r'/ru/post/\d{1,8}', parsed.path)
                and not parsed.query and not parsed.fragment):
            links.add(url)
    return links


def _window(body, now):
    dates = set(_DATE.findall(body))
    if len(dates) != 1:
        return None
    day, month, year = next(iter(dates))
    try:
        date = dt.date(int(year), _MONTHS.index(month) + 1, int(day))
        today = now.astimezone(_ZONE).date()
        if not today - dt.timedelta(days=30) <= date <= today + dt.timedelta(days=7):
            return None
        hours = re.search(r'\bс\s+(\d{1,2}):(\d{2})\s+до\s+(\d{1,2}):(\d{2})\b', body)
        duration = re.search(r'сроком на\s+(\d{1,2})\s+дн', body)
        if hours:
            h1, m1, h2, m2 = map(int, hours.groups())
            start = dt.datetime.combine(date, dt.time(h1, m1, tzinfo=_ZONE))
            end = dt.datetime.combine(date, dt.time(h2, m2, tzinfo=_ZONE))
        elif duration and 1 <= int(duration[1]) <= 30:
            start = dt.datetime.combine(date, dt.time(tzinfo=_ZONE))
            end = start + dt.timedelta(days=int(duration[1]))
        else:
            return None
        if start >= end or now >= end:
            return None
        return start, end
    except ValueError:
        return None


def _parse_notice(page, url, now):
    body = _text(' '.join(page.body)).casefold().replace('ё', 'е')
    title = _text(' '.join(page.title))
    if (len(body) > 20000 or not _ROAD_TITLE.search(title) or _RESTORED.search(body)
            or not re.search(r'(?:строитель|ремонт|асфальт|реконструкц)', body)):
        return []
    window = _window(body, now)
    if window is None:
        return []
    start, end = window
    if end - start < dt.timedelta(days=1):
        schedule = f'{start:%d %b} · {start:%H:%M}–{end:%H:%M} UTC+6'
    else:
        schedule = f'{start:%d %b}–{end - dt.timedelta(seconds=1):%d %b %Y} · UTC+6'
    status = 'Scheduled restriction' if now < start else 'Announced restriction'
    features = []
    for entry, pattern in _MATCHERS:
        if not pattern.search(body) or not all(part in body for part in entry['required']):
            continue
        segments = entry.get('road_segments', [])
        points = [point for segment in segments for point in segment]
        bounds = ([min(point[0] for point in points), min(point[1] for point in points),
                   max(point[0] for point in points), max(point[1] for point in points)] if points else None)
        features.append({
            'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': entry['coordinates']},
            'properties': {
                'key': f'kg:bishkek:roadworks:{url.rsplit("/", 1)[-1]}:{entry["id"]}',
                'layer': 'construction', 'title': 'Roadworks · ' + entry['road'],
                'detail': f'{entry["section"]} · {status} · {schedule} · Approximate street reference',
                'source': 'City of Bishkek · OpenStreetMap reference locations', 'source_url': url,
                'starts_at': start.isoformat(), 'ends_at': end.isoformat(),
                'valid_until': min(end.timestamp(), now.timestamp() + 900),
                'location_source_url': 'https://www.openstreetmap.org/' + entry['osm'],
                'road_segments': segments, 'road_segment_bounds': bounds, 'segment_color': '#efbf68',
            },
        })
    return features


def bishkek_roadworks(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    for url, expires in list(_ACTIVE_NOTICES.items()):
        if expires <= now.timestamp():
            _ACTIVE_NOTICES.pop(url)
    urls = set(_ACTIVE_NOTICES)
    for index in (INDEX_URL, INDEX_URL + '?page=2', INDEX_URL + '?page=3'):
        urls.update(_notice_links(_read_page(index)))
    if len(urls) > 12:
        raise ValueError('Bishkek road catalog exceeded notice limit')
    features = []
    for url in sorted(urls):
        rows = _parse_notice(_read_page(url), url, now)
        if rows:
            _ACTIVE_NOTICES[url] = dt.datetime.fromisoformat(rows[0]['properties']['ends_at']).timestamp()
            features.extend(rows)
        else:
            _ACTIVE_NOTICES.pop(url, None)
    return features
