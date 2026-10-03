"""Public Telasi planned work windows, at approximate Tbilisi district references."""

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

API_URL = 'https://app.telasi.ge/api/view/telasi/getPoweroutages'
SOURCE_URL = 'https://www.telasi.ge/ka/company-news/power-outage'
_ZONE = dt.timezone(dt.timedelta(hours=4))
_UTC = dt.timezone.utc
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'rows': []}
_MONTHS = dict(zip(('იანვარს', 'თებერვალს', 'მარტს', 'აპრილს', 'მაისს', 'ივნისს',
                    'ივლისს', 'აგვისტოს', 'სექტემბერს', 'ოქტომბერს', 'ნოემბერს', 'დეკემბერს'), range(1, 13)))
_DATE = re.compile(r'\b(\d{1,2})\s+(' + '|'.join(_MONTHS) + r')\b')
_CLOCK = re.compile(r'\b(\d{1,2}):(\d{2})\s+საათიდან\s+(\d{1,2}):(\d{2})\s+საათამდე\b')


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.blocks, self.depth = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag == 'p':
            self.flush()
            self.depth = 1
        elif tag == 'br' and self.depth:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag == 'p':
            self.flush()
            self.depth = 0

    def handle_data(self, value):
        if self.depth:
            self.parts.append(value)

    def flush(self):
        text = ' '.join(''.join(self.parts).split())
        self.parts = []
        if text:
            self.blocks.append(text)


@functools.lru_cache(maxsize=1)
def catalog():
    data = json.loads(Path(__file__).with_name('tbilisi-outage-locations.json').read_text(encoding='utf-8'))
    if data.get('licence') != 'ODbL 1.0' or data.get('locationKind') != 'approximate_district_reference':
        raise ValueError('Tbilisi district provenance changed')
    rows = data['locations']
    if len(rows) != 10 or len({p['heading'] for p in rows}) != 10:
        raise ValueError('Tbilisi district catalog changed')
    for p in rows:
        lon, lat = p['coordinates']
        if (not all(math.isfinite(v) for v in (lon, lat)) or not 44.6 <= lon <= 45.05
                or not 41.55 <= lat <= 41.9 or type(p['osmNode']) is not int or p['osmNode'] <= 0
                or not p['id'].isdigit() or not p['heading'].endswith(' რაიონი')):
            raise ValueError('Invalid Tbilisi district reference')
    return {p['heading']: p for p in rows}


def _publication(row):
    return dt.datetime.strptime(row['date'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=_ZONE)


def parse_planned(rows, now=None, refs=None):
    now = now or dt.datetime.now(_UTC)
    if now.tzinfo is None:
        raise ValueError('Telasi clock needs a time zone')
    refs = refs if refs is not None else catalog()
    local_day, output, seen = now.astimezone(_ZONE).date(), [], set()
    for row in rows:
        # The same endpoint also returns an API/device list; that list is never
        # imported. Only published planned editorial notices are processed.
        if (row.get('status') != 'published' or row.get('content_type') != 'poweroutage'
                or row.get('localisedto') != 'ka' or 2769 not in row.get('taxonomy', {}).get('content_poweroutage', [])
                or type(row.get('id')) is not int or row['id'] <= 0):
            continue
        publication = _publication(row)
        if not 0 <= (local_day - publication.date()).days <= 14 or publication > now:
            continue
        title = row.get('title', '')
        dates = list(_DATE.finditer(title))
        if len(dates) != 1 or 'ელექტრომომარაგება დროებით შეიზღუდება' not in title:
            continue
        match, candidates = dates[0], []
        explicit_years = set(re.findall(r'\b(?:19|20)\d{2}\b', title))
        for year in range(publication.year - 1, publication.year + 2):
            try:
                day = dt.date(year, _MONTHS[match[2]], int(match[1]))
            except ValueError:
                continue
            if (publication.date() <= day <= publication.date() + dt.timedelta(days=7)
                    and local_day <= day <= local_day + dt.timedelta(days=7)
                    and (not explicit_years or explicit_years == {str(year)})):
                candidates.append(day)
        if len(candidates) != 1:
            continue
        body = row.get('editor')
        if not isinstance(body, str) or not 0 < len(body) <= 50000:
            raise ValueError('Telasi notice body changed')
        page = _Paragraphs()
        page.feed(body)
        page.flush()
        if not page.blocks:
            raise ValueError('Telasi notice paragraphs missing')
        scope = None
        for block in page.blocks:
            heading = block.rstrip(':.')
            if heading.endswith(' რაიონი'):
                scope = refs.get(heading)  # An unknown district must reset scope.
                continue
            clocks = list(_CLOCK.finditer(block))
            if scope is None or len(clocks) != 1 or 'შეზღუდვა შეეხება:' not in block:
                continue
            clock = clocks[0]
            try:
                start = dt.datetime.combine(candidates[0], dt.time(int(clock[1]), int(clock[2])), _ZONE)
                end = start.replace(hour=int(clock[3]), minute=int(clock[4]))
            except ValueError:
                continue
            if end <= start or end <= now:
                continue
            key = f'ge:telasi:planned:{row["id"]}:{scope["id"]}:{start:%H%M}:{end:%H%M}'
            if key in seen:
                continue
            seen.add(key)
            address = block.split('შეზღუდვა შეეხება:', 1)[1].strip()
            if not address:
                continue
            output.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': scope['coordinates']},
                           'properties': {
                               'key': key, 'provider': 'Telasi · planned power work',
                               'area_name': scope['name'] + ' · Tbilisi · Approximate district reference',
                               'status': ('Scheduled' if now < start else 'Planned work window')
                                         + f' · {start:%d %b · %H:%M}–{end:%H:%M} UTC+4',
                               'customers_affected': None, 'planned': True, 'location_kind': 'area',
                               'source_address': address[:1500], 'source_district': scope['heading'],
                               'source_label': 'Telasi · OpenStreetMap (ODbL)',
                               'source_url': 'https://www.telasi.ge/company-news/power-outage?content=' + str(row['id']),
                               'location_source_url': 'https://www.openstreetmap.org/relation/' + scope['id'],
                               'published_at': publication.astimezone(_UTC).isoformat(),
                               'starts_at': start.astimezone(_UTC).isoformat(),
                               'ends_at': end.astimezone(_UTC).isoformat(),
                               'valid_until': min(end.timestamp(), now.timestamp() + 900),
                           }})
    return output


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('Telasi API redirects are unsupported')


def _read_page(page):
    body = {'pageNumber': page, 'perPage': 12, 'selectedlan': 'ka',
            'taxonomy': {'content_poweroutage': [2769]}}
    request = urllib.request.Request(API_URL, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'lang': 'ka',
                                              'User-Agent': 'GlobeView/1.0 (https://globeview.app)'})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
        if response.status != 200 or response.geturl() != API_URL:
            raise ValueError('Unexpected Telasi API response')
        data = response.read(2000001)
    if len(data) > 2000000:
        raise ValueError('Telasi page exceeded size limit')
    content = json.loads(data)['content']
    if (not isinstance(content.get('list'), list) or len(content['list']) > 12
            or type(content.get('listCount')) is not int or content['listCount'] < len(content['list'])
            or content.get('page') != page):
        raise ValueError('Telasi planned list structure changed')
    fields = ('id', 'status', 'content_type', 'localisedto', 'taxonomy', 'date', 'title', 'editor')
    return [{k: row.get(k) for k in fields} for row in content['list']], content['listCount']


def telasi_planned_outages(now=None):
    now = now or dt.datetime.now(_UTC)
    with _LOCK:
        if _CACHE['until'] <= time.monotonic():
            rows = []
            for page in range(1, 5):
                batch, count = _read_page(page)
                rows.extend(batch)
                if (len(rows) >= count or not batch
                        or min(_publication(r).date() for r in batch) < now.astimezone(_ZONE).date() - dt.timedelta(days=14)):
                    break
            else:
                raise ValueError('Telasi recent planned notices exceeded page limit')
            _CACHE.update(until=time.monotonic() + 3600, rows=rows)
        return parse_planned(_CACHE['rows'], now)
