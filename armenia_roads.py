"""Armenian Road Department notices at approximate numbered-road references."""

import concurrent.futures
import datetime as dt
from functools import lru_cache
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request

from armenia_outages import normalize
from armenia_reports import _GENITIVE_MONTHS
from scripts.update_armenia_road_locations import canonical, road_refs


BASE = 'https://armroad.am'
INDEX = BASE + '/am/press/urgentnews'
_ZONE = dt.timezone(dt.timedelta(hours=4))
_UTC = dt.timezone.utc
_PAGES = {}
_LOCK = threading.Lock()
_URL_PATH = re.compile(r'/am/urgent_news/inner/News_(\d{2})\.(\d{2})\.(\d{4})(?:_\d{1,3})?')
_WINDOW = re.compile(r'(' + '|'.join(_GENITIVE_MONTHS) + r')\s+(\d{1,2})-ին[՝,\s]+ժամը\s+'
                     r'(\d{1,2})[:։.](\d{2})-ից\s+մինչև\s+(\d{1,2})[:։.](\d{2})', re.I)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = set()
        self.title, self.body, self.dates = [], [], []
        self.depth = self.scope = self.articles = 0
        self.heading = self.date = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'a' and attrs.get('href'):
            self.links.add(urllib.parse.urljoin(BASE, attrs['href']))
        if tag == 'div':
            self.depth += 1
            if 'item-block-inner' in classes:
                self.scope = self.depth
                self.articles += 1
        if tag == 'h2' and 'inner-title' in classes:
            self.heading = True
        if self.scope and tag == 'span' and 'item-time' in classes:
            self.date = True
        if self.scope and tag in ('p', 'br'):
            self.body.append(' ')

    def handle_endtag(self, tag):
        if tag == 'div':
            if self.depth == self.scope:
                self.scope = 0
            self.depth -= 1
        if tag == 'h2':
            self.heading = False
        if tag == 'span':
            self.date = False

    def handle_data(self, value):
        if self.heading:
            self.title.append(value)
        if self.scope:
            (self.dates if self.date else self.body).append(value)


def _parse(page):
    if isinstance(page, bytes):
        page = page.decode('utf-8')
    parser = _Page()
    parser.feed(page)
    return parser


def _url_date(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'armroad.am' or parsed.query or parsed.fragment):
        return None
    match = _URL_PATH.fullmatch(parsed.path)
    try:
        return dt.date(int(match[3]), int(match[2]), int(match[1])) if match else None
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _locations():
    data = json.loads(Path(__file__).with_name('armenia-road-locations.json').read_text())
    if data.get('maxDistanceMetres') != 3000 or data.get('locationKind') != 'approximate_numbered_road_reference':
        raise ValueError('Armenia road reference catalog changed')
    places = data['locations']
    if not isinstance(places, list) or not 1 <= len(places) <= 1500:
        raise ValueError('Armenia road reference catalog size changed')
    for place in places:
        for road in place['roads'].values():
            point = road.get('coordinates')
            if (not isinstance(point, list) or len(point) != 2
                    or not all(type(x) in (int, float) and math.isfinite(x) for x in point)
                    or not (43.4 <= point[0] <= 46.7 and 38.8 <= point[1] <= 41.4)
                    or type(road.get('osmNode')) is not int or road['osmNode'] <= 0
                    or not 0 <= road.get('distanceMetres', 99999) <= 3000):
                raise ValueError('Invalid Armenia road reference')
    return places


def _located(body, ref, locations):
    matches = {}
    text = normalize(body)
    for place in locations:
        road = place.get('roads', {}).get(ref)
        if not road:
            continue
        for alias in place['aliases']:
            # Match the explicitly nearby settlement or named bridge, not route endpoints,
            # office addresses, or the streets used by the suggested detour.
            if re.search(r'(?<!\w)' + re.escape(alias) + r'(?:ի)?\s+(?:բնակավայրի\s+հարակից\s+տարածքում|կամուրջը)(?!\w)', text):
                matches[place['id']] = (place, road)
    return next(iter(matches.values())) if len(matches) == 1 else None


def _window(body, published, now):
    matches = list(_WINDOW.finditer(body.lower()))
    if len(matches) != 1:
        return None
    match = matches[0]
    month, day, sh, sm, eh, em = match.groups()
    candidates = []
    explicit = re.findall(r'(?<!\d)((?:19|20)\d{2})\s*թ', body[max(0, match.start() - 40):match.start()])
    for year in ([int(explicit[-1])] if explicit else (published.year, published.year + 1)):
        try:
            start = dt.datetime(year, _GENITIVE_MONTHS.index(month) + 1, int(day), int(sh), int(sm), tzinfo=_ZONE)
            end = start.replace(hour=int(eh), minute=int(em))
        except ValueError:
            continue
        if (published <= start.date() <= published + dt.timedelta(days=31)
                and dt.timedelta(0) < end - start <= dt.timedelta(hours=12)):
            candidates.append((start, end))
    if len(candidates) != 1 or now >= candidates[0][1]:
        return None
    return candidates[0]


def parse_notice(page, url, now=None, locations=None):
    now = now or dt.datetime.now(_UTC)
    published = _url_date(url)
    if published is None:
        raise ValueError('Unexpected Armenia road notice URL')
    if not 0 <= (now.astimezone(_ZONE).date() - published).days <= 14:
        return []
    page = _parse(page)
    if page.articles != 1 or ''.join(page.dates).strip() != published.strftime('%d-%m-%Y'):
        raise ValueError('Armenia road article structure or date changed')
    body = ' '.join(''.join(page.body).split())
    if not body:
        raise ValueError('Armenia road article body missing')
    refs = road_refs(body)
    if not refs:
        return []
    # The first numbered road in the source is the announced road; subsequent
    # numbers may be the road's endpoints or a suggested detour.
    first = re.search(r'[ՄՀՏMHTМ]\s*[- ]?\s*\d+(?:-\d+)*', body)
    ref = canonical(road_refs(first[0])[0]) if first else None
    located = _located(body, ref, locations if locations is not None else _locations())
    if not located:
        return []
    place, road = located
    display_ref = ref[0] + '-' + ref[1:]
    if any(term in body for term in ('երթևեկությունը վերականգնվել է', 'երթևեկությունը բացվել է', 'աշխատանքներն ավարտվել են')):
        return []
    window = _window(body, published, now)
    blasting = 'պայթեցման աշխատանքներ' in body
    if window and ('նորոգման' in body or blasting):
        start, end = window
        layer = 'construction'
        title = ('Planned blasting' if blasting else 'Scheduled roadworks') + ' · ' + display_ref + ' near ' + place['name']
        detail = f'{start:%d %b %Y} · {start:%H:%M}–{end:%H:%M} UTC+4'
        if blasting:
            detail += ' · Closure not stated by source'
        expiry = min(end.timestamp(), now.timestamp() + 900)
        schedule = {'starts_at': start.isoformat(), 'ends_at': end.isoformat()}
        kind = 'scheduled_roadworks_notice'
    elif not _WINDOW.search(body) and not blasting and 'կամուրջը' in body and 'երկկողմանի փակ է' in body:
        layer = 'incidents'
        title = 'Bridge closure report · ' + display_ref + ' near ' + place['name']
        detail = f'Reported closed {published:%d %b %Y} · Current status unverified'
        expiry = min(dt.datetime.combine(published + dt.timedelta(days=15), dt.time(), _ZONE).timestamp(),
                     now.timestamp() + 900)
        schedule, kind = {}, 'published_bridge_closure_report'
    else:
        return []
    chainage = re.search(r'կմ\s*(\d+\+\d+)(?:\s*[-–]\s*կմ\s*(\d+\+\d+))?', body)
    if chainage:
        detail += ' · Source km ' + chainage[1] + ('–' + chainage[2] if chainage[2] else '')
    detail += ' · Approximate road reference; exact section unverified'
    return [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': road['coordinates']},
             'properties': {'key': 'am:armroad:' + url.rsplit('/', 1)[-1], 'layer': layer,
                            'title': title, 'detail': detail, 'source_url': url,
                            'source': 'Armenia Road Department · OpenStreetMap (ODbL) / GeoNames (CC BY 4.0)',
                            'record_kind': kind, 'location_kind': 'approximate_numbered_road_reference',
                            'location_source_url': 'https://www.openstreetmap.org/node/' + str(road['osmNode']),
                            'valid_until': expiry, **schedule}}]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('Armenia road redirects are unsupported')


def _read(url):
    if url != INDEX and _url_date(url) is None:
        raise ValueError('Unexpected Armenia road URL')
    with _LOCK:
        cached = _PAGES.get(url)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public road notices)'})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
        data = response.read(250001)
    if len(data) > 250000:
        raise ValueError('Armenia road page exceeded size limit')
    with _LOCK:
        if len(_PAGES) >= 32:
            _PAGES.clear()
        _PAGES[url] = (time.monotonic() + 600, data)
    return data


def road_notices(now=None):
    now = now or dt.datetime.now(_UTC)
    page = _parse(_read(INDEX))
    all_links = {url for url in page.links if _url_date(url) is not None}
    if not all_links or len(all_links) > 64:
        raise ValueError('Armenia road listing structure changed')
    urls = sorted(url for url in all_links if 0 <= (now.astimezone(_ZONE).date() - _url_date(url)).days <= 14)
    if len(urls) > 12:
        raise ValueError('Armenia road recent notice limit exceeded')
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(_read, urls))
    return [item for page, url in zip(pages, urls) for item in parse_notice(page, url, now)]
