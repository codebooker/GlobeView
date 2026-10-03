"""Public Kyrgyzstan utility work schedules, with audited OSM references."""

import datetime as dt
import hashlib
import json
import pathlib
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser


# The national utility publishes these HTTP addresses in its regional directory.
# The HTTPS chains are incomplete; certificate validation is never disabled.
INDEX_URL = 'http://bipes.nesk.kg/ru/abonentam/perechen-uchastkov-rabot/'
_NOTICE_PATH = re.compile(r'/ru/abonentam/perechen-uchastkov-rabot/data-(\d{2})(\d{2})(\d{4})-g/')
_SERVICES = {
    'bishkek': {'index': INDEX_URL, 'path': _NOTICE_PATH,
                'name': 'Bishkek PES', 'key': 'bipes'},
    'issyk_kul': {
        'index': 'http://ipes.nesk.kg/ru/kardarlarga/plandalgan-ish-ajmaktardyn-tizmesi/',
        'path': re.compile(r'/ru/kardarlarga/plandalgan-ish-ajmaktardyn-tizmesi/data-(\d{2})(\d{2})(\d{4})-zh/'),
        'name': 'Issyk-Kul PES', 'key': 'ipes',
    },
}
_ZONE = dt.timezone(dt.timedelta(hours=6))
_UTC = dt.timezone.utc
_CLOCK = re.compile(r'^(\d{1,2})[:\-](\d{2})$')
_LOCATIONS = json.loads(pathlib.Path(__file__).with_name('kyrgyzstan-outage-locations.json')
                       .read_text(encoding='utf-8'))['locations']
_MATCHERS = [(entry, re.compile(entry['pattern'])) for entry in _LOCATIONS]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('Utility outage redirects are unsupported')


_OPENER = urllib.request.build_opener(_NoRedirect())


def _text(value):
    return ' '.join(value.split())


class _Page(HTMLParser):
    """Only visible headings, notice links, and bounded table cells are used."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings, self.links, self.tables = [], [], []
        self.heading = self.link = self.table = self.row = self.cell = None
        self.cell_span = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'title', 'h1', 'h2', 'h3'}:
            self.heading = []
        if tag == 'a':
            self.link = [attrs.get('href', ''), []]
        if tag == 'table':
            if self.table is not None:
                raise ValueError('Nested outage tables are unsupported')
            self.table = []
        if tag == 'tr' and self.table is not None:
            self.row = []
        if tag in {'td', 'th'} and self.row is not None:
            self.cell = []
            try:
                spans = [int(attrs.get(key, '1')) for key in ('rowspan', 'colspan')]
            except ValueError as error:
                raise ValueError('Invalid outage table span') from error
            if not (1 <= spans[0] <= 100 and 1 <= spans[1] <= 5):
                raise ValueError('Outage table span exceeded limit')
            self.cell_span = spans
        if tag == 'br' and self.cell is not None:
            self.cell.append(' ')

    def handle_data(self, data):
        for target in (self.heading, self.cell):
            if target is not None:
                target.append(data)
        if self.link is not None:
            self.link[1].append(data)

    def handle_endtag(self, tag):
        if tag in {'title', 'h1', 'h2', 'h3'} and self.heading is not None:
            self.headings.append(_text(''.join(self.heading)))
            self.heading = None
        if tag == 'a' and self.link is not None:
            self.links.append((self.link[0], _text(''.join(self.link[1]))))
            self.link = None
        if tag in {'td', 'th'} and self.cell is not None:
            value = _text(''.join(self.cell))
            if len(value) > 5000:
                raise ValueError('Outage cell exceeded limit')
            self.row.append((value, *self.cell_span))
            self.cell = None
        if tag == 'tr' and self.row is not None:
            if len(self.row) > 5 or len(self.table) >= 100:
                raise ValueError('Outage table exceeded limit')
            self.table.append(self.row)
            self.row = None
        if tag == 'table' and self.table is not None:
            if len(self.tables) >= 5:
                raise ValueError('Too many outage tables')
            self.tables.append(self.table)
            self.table = None


def _expanded_rows(rows):
    spans = {}
    for cells in rows:
        row, next_spans = [], {}
        column = 0
        for value, height, width in cells:
            while column in spans:
                previous, remaining = spans[column]
                row.append(previous)
                if remaining > 1:
                    next_spans[column] = (previous, remaining - 1)
                column += 1
            for _ in range(width):
                if column in spans or column >= 5:
                    raise ValueError('Inconsistent outage table columns')
                row.append(value)
                if height > 1:
                    next_spans[column] = (value, height - 1)
                column += 1
        while column in spans:
            previous, remaining = spans[column]
            row.append(previous)
            if remaining > 1:
                next_spans[column] = (previous, remaining - 1)
            column += 1
        spans = next_spans
        yield row


def _read_page(url, service='bishkek'):
    source = _SERVICES[service]
    if url != source['index']:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'http' or parsed.netloc != urllib.parse.urlsplit(source['index']).netloc
                or parsed.query or parsed.fragment or not source['path'].fullmatch(parsed.path)):
            raise ValueError('Unexpected utility outage URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (https://globeview.app)'})
    with _OPENER.open(request, timeout=15) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError('Unexpected utility outage response')
        body = response.read(250001)
    if len(body) > 250000:
        raise ValueError('Utility outage page exceeded size limit')
    page = _Page()
    page.feed(body.decode('utf-8'))
    return page


def _notice_links(page, now, service='bishkek'):
    source = _SERVICES[service]
    today = now.astimezone(_ZONE).date()
    links = {}
    for href, label in page.links:
        if not label.startswith('График плановых работ'):
            continue
        url = urllib.parse.urljoin(source['index'], href)
        parsed = urllib.parse.urlsplit(url)
        match = source['path'].fullmatch(parsed.path)
        if (not match or parsed.scheme != 'http' or parsed.netloc != urllib.parse.urlsplit(source['index']).netloc
                or parsed.query or parsed.fragment):
            continue
        day, month, year = map(int, match.groups())
        try:
            date = dt.date(year, month, day)
        except ValueError:
            continue
        if today <= date <= today + dt.timedelta(days=7):
            links[url] = date
    if len(links) > 8:
        raise ValueError('Utility outage catalog exceeded notice limit')
    return sorted(links.items())


def _locations_for(address, service='bishkek', district=''):
    normalized = _text(re.sub('[“”"\u201d]', '', address.lower())).replace('ё', 'е')
    district = _text(district).casefold()
    matches = [entry for entry, pattern in _MATCHERS
               if entry.get('service', 'bishkek') == service
               and (not entry.get('district') or entry['district'].casefold() == district)
               and pattern.search(normalized)]
    # A verified building is preferred to its street. Named areas are preferred
    # to incidental streets inside them; none are interpreted as outage extents.
    for kind in ('building', 'area', 'street'):
        selected = [entry for entry in matches if entry['kind'] == kind]
        if selected:
            return selected
    return []


def _parse_notice(page, date, url, now, service='bishkek'):
    source = _SERVICES[service]
    explicit_dates = set(re.findall(r'\b(\d{2}\.\d{2}\.\d{4})\b', ' '.join(page.headings)))
    if explicit_dates != {date.strftime('%d.%m.%Y')}:
        raise ValueError('Utility notice date is missing or inconsistent')
    features, seen = [], set()
    for table in page.tables:
        for row in _expanded_rows(table):
            if len(row) != 5:
                continue
            district, address, begin, finish, work = row
            clocks = [_CLOCK.fullmatch(clock) for clock in (begin, finish)]
            if not all(clocks):
                continue
            try:
                start, end = [dt.datetime.combine(date, dt.time(*map(int, clock.groups())), _ZONE)
                              for clock in clocks]
            except ValueError:
                continue
            if not start < end or not now < end or not address or not work:
                continue
            upcoming = now < start
            window = f'{date.day} {date.strftime("%b")} · {start:%H:%M}–{end:%H:%M} UTC+6'
            for location in _locations_for(address, service, district):
                identifier = hashlib.sha256(f'{url}\n{district}\n{address}\n{begin}\n{finish}'
                                            .encode()).hexdigest()[:16] + ':' + location['id']
                if identifier in seen:
                    continue
                seen.add(identifier)
                point_kind = {'building': 'Building reference', 'street': 'Approximate street reference',
                              'area': 'Approximate area reference'}[location['kind']]
                properties = {
                    'key': f'kg:{source["key"]}:planned:' + identifier,
                    'provider': source['name'] + ' · planned power work',
                    'area_name': f'{location["label"]} · {point_kind}',
                    'status': ('Scheduled' if upcoming else 'Planned work window') + ' · ' + window,
                    'reason': work[:160], 'etr': '', 'customers_affected': None,
                    'source_label': source['name'] + ' · OpenStreetMap reference locations', 'source_url': url,
                    'source_address': address, 'source_district': district,
                    'location_kind': location['kind'],
                    'location_source_url': 'https://www.openstreetmap.org/' + location['osm'],
                    'starts_at': start.astimezone(_UTC).isoformat(),
                    'ends_at': end.astimezone(_UTC).isoformat(),
                    'valid_until': min(end.timestamp(), now.timestamp() + 15 * 60),
                    'planned': True,
                }
                features.append({'type': 'Feature', 'geometry': {'type': 'Point',
                                 'coordinates': location['coordinates']}, 'properties': properties})
    return features


def _planned_outages(service, now=None):
    now = now or dt.datetime.now(_UTC)
    index = _read_page(_SERVICES[service]['index'], service)
    features = []
    # At most eight small daily notices, fetched once by the shared power loader.
    for url, date in _notice_links(index, now, service):
        features.extend(_parse_notice(_read_page(url, service), date, url, now, service))
    return features


def bishkek_planned_outages(now=None):
    return _planned_outages('bishkek', now)


def issyk_kul_planned_outages(now=None):
    return _planned_outages('issyk_kul', now)
