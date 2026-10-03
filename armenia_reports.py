"""Dated Armenian Rescue Service reports, with approximate settlement references."""

import concurrent.futures
import datetime as dt
from html.parser import HTMLParser
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from armenia_outages import catalog, normalize


BASE = 'https://rescue.mia.gov.am'
CATEGORY = BASE + '/' + urllib.parse.quote('պատահարներ')
_UTC = dt.timezone.utc
_MAX_AGE = dt.timedelta(days=3)
_ARTICLE_CACHE = {}
_LOCK = threading.Lock()
_MONTHS = ('հունվար', 'փետրվար', 'մարտ', 'ապրիլ', 'մայիս', 'հունիս',
           'հուլիս', 'օգոստոս', 'սեպտեմբեր', 'հոկտեմբեր', 'նոյեմբեր', 'դեկտեմբեր')
_GENITIVE_MONTHS = ('հունվարի', 'փետրվարի', 'մարտի', 'ապրիլի', 'մայիսի', 'հունիսի',
                    'հուլիսի', 'օգոստոսի', 'սեպտեմբերի', 'հոկտեմբերի', 'նոյեմբերի', 'դեկտեմբերի')


class _Nuxt(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.capture = dict(attrs).get('id') == '__NUXT_DATA__'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.capture = False

    def handle_data(self, value):
        if self.capture:
            self.parts.append(value)


def _table(page):
    if isinstance(page, bytes):
        page = page.decode('utf-8')
    if len(page) > 2 * 1024 * 1024:
        raise ValueError('Armenia rescue page exceeded size limit')
    parser = _Nuxt()
    parser.feed(page)
    data = json.loads(''.join(parser.parts))
    if not isinstance(data, list) or len(data) > 50000:
        raise ValueError('Armenia rescue page data changed')
    return data


def _ref(table, index, default=None):
    # Nuxt's -1 sentinel is undefined, not the final array entry.
    if type(index) is int and 0 <= index < len(table):
        return table[index]
    return default


def _field(table, row, key, default=None):
    return _ref(table, row.get(key), default) if isinstance(row, dict) else default


def _date(value):
    try:
        value = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.astimezone(_UTC) if value.tzinfo else None
    except ValueError:
        return None


def _recent(published, now):
    return published is not None and dt.timedelta(minutes=-10) <= now - published <= _MAX_AGE


def article_links(page, now=None):
    """Read only published incident cards; exclude weather, drills and road bulletins."""
    now = now or dt.datetime.now(_UTC)
    table = _table(page)
    cards = [row for row in table if isinstance(row, dict) and 'dateCreated' in row]
    if not cards:
        raise ValueError('Armenia rescue listing structure changed')
    output = {}
    for row in cards:
        categories = _field(table, row, 'categories', [])
        if not isinstance(categories, list) or not any(
                _field(table, _ref(table, index), 'title') == 'Պատահարներ' for index in categories):
            continue
        slug = _field(table, row, 'slug', '')
        content = _field(table, row, 'contentType')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,159}', slug):
            continue
        if _field(table, content, 'path') != 'news':
            continue
        if _recent(_date(_field(table, row, 'dateCreated')), now):
            output[slug] = BASE + '/articles/news/' + slug
    if len(output) > 12:
        raise ValueError('Armenia rescue recent article limit exceeded')
    return output


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.paragraphs = [], []
        self.capture = False

    def handle_starttag(self, tag, attrs):
        if tag == 'p':
            self.parts = []
            self.capture = True

    def handle_endtag(self, tag):
        if tag == 'p' and self.capture:
            self.paragraphs.append(' '.join(''.join(self.parts).split()))
            self.capture = False

    def handle_data(self, value):
        if self.capture:
            self.parts.append(value)


def _location(opening, refs):
    # Match the incident clause, not the location of the responding rescue office.
    clause = re.search(r'ահազանգ է ստացվել[՝,\s]+որ\s+(.+)', opening)
    if not clause:
        return None
    place_text = normalize(clause[1])
    prefix = normalize(opening[:clause.start()])
    provinces = {code for name, code in refs['regions'].items()
                 if re.search(r'(?<!\w)' + re.escape(name) + r'\s+մարզ', prefix)}
    if len(provinces) > 1:
        return None
    selected = {}
    for place in refs['locations']:
        if provinces and place['admin1'] not in provinces:
            continue
        for alias in place['aliases']:
            if re.search(r'(?<!\w)' + re.escape(alias) + r'(?:ի|ում|ից)?(?!\w)', place_text):
                selected[place['id']] = place
    # Two settlements or a duplicated name without a province cannot become a guessed point.
    return next(iter(selected.values())) if len(selected) == 1 else None


def parse_report(page, slug, now=None, refs=None):
    now = now or dt.datetime.now(_UTC)
    refs = refs if refs is not None else catalog()
    table = _table(page)
    rows = [row for row in table if isinstance(row, dict) and 'publication_date' in row
            and _field(table, row, 'slug') == slug]
    if len(rows) != 1:
        raise ValueError('Armenia rescue article structure changed')
    row = rows[0]
    if _field(table, row, 'status') != 'published':
        return []
    published = _date(_field(table, row, 'publication_date'))
    if not _recent(published, now):
        return []
    paragraphs = _Paragraphs()
    blocks = _field(table, row, 'content_blocks', [])
    if not isinstance(blocks, list) or len(blocks) > 50:
        raise ValueError('Armenia rescue content structure changed')
    for index in blocks:
        block = _ref(table, index)
        if _field(table, block, 'collection') == 'block_richtext':
            content = _field(table, _field(table, block, 'item'), 'content', '')
            if isinstance(content, str):
                paragraphs.feed(content)
    if not paragraphs.paragraphs:
        raise ValueError('Armenia rescue article body missing')
    opening = paragraphs.paragraphs[0]
    forms = {name: month for month, pair in enumerate(zip(_MONTHS, _GENITIVE_MONTHS), 1) for name in pair}
    event_date = re.match(r'(' + '|'.join(forms) + r')\s+(\d{1,2})(?:-ին)?', opening.lower())
    if not event_date:
        return []
    years = re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', opening)
    if len(set(years)) > 1:
        return []
    event_days = []
    for year in ([int(years[0])] if years else [published.year - 1, published.year]):
        try:
            day = dt.date(year, forms[event_date[1]], int(event_date[2]))
        except ValueError:
            continue
        if 0 <= (published.date() - day).days <= 2:
            event_days.append(day)
    if len(event_days) != 1:
        return []
    place = _location(opening, refs)
    if not place:
        return []
    body = ' '.join(paragraphs.paragraphs)
    rockfall = any(term in opening for term in ('ժայռաբեկոր', 'քարաթափ'))
    fire = any(term in opening for term in ('հրդեհ', 'բռնկվել'))
    traffic = any(term in opening for term in ('վթար', 'բախվել', 'կողաշրջվել'))
    category = 'fire' if fire else 'traffic' if rockfall or traffic else 'warning'
    kind = 'Rockfall response' if rockfall else 'Fire report' if fire else 'Road incident report' if traffic else 'Rescue report'
    status = ('Traffic restored' if 'երթևեկությունը վերականգնվել է' in body else
              'Fire extinguished' if 'հրդեհը մարվել է' in body else 'Published report · current status unverified')
    lon, lat = place['coordinates']
    return [{'id': 'am:rescue:' + slug, 'lon': lon, 'lat': lat,
             'title': kind + ' · ' + place['name'] + ' area', 'category': category,
             'detail': status + ' · Approximate settlement reference, not the exact incident location',
             'source': 'Armenia MIA Rescue Service · GeoNames (CC BY 4.0)',
             'sourceUrl': BASE + '/articles/news/' + slug,
             'observed': published.isoformat().replace('+00:00', 'Z'),
             'record_kind': 'published_rescue_report', 'location_kind': 'settlement_reference'}]


def _safe_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'rescue.mia.gov.am'
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Unexpected Armenia rescue URL')


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read(url):
    _safe_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public rescue reports)'})
    with urllib.request.build_opener(_Redirect()).open(request, timeout=15) as response:
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError('Armenia rescue page exceeded size limit')
    return body


def _article(slug, url, now):
    with _LOCK:
        cached = _ARTICLE_CACHE.get(slug)
        if cached and time.monotonic() - cached[0] < 300:
            return parse_report(cached[1], slug, now)
    page = _read(url)
    result = parse_report(page, slug, now)
    with _LOCK:
        if len(_ARTICLE_CACHE) >= 64:
            _ARTICLE_CACHE.clear()
        _ARTICLE_CACHE[slug] = (time.monotonic(), page)
    return result


def rescue_reports(now=None):
    """Called by the shared five-minute international emergency snapshot."""
    now = now or dt.datetime.now(_UTC)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        pages = list(executor.map(_read, (BASE + '/', CATEGORY)))
        links = {}
        for page in pages:
            links.update(article_links(page, now))
        if len(links) > 12:
            raise ValueError('Armenia rescue recent article limit exceeded')
        futures = [executor.submit(_article, slug, url, now) for slug, url in links.items()]
        return [item for future in futures for item in future.result()]
