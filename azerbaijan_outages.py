"""Azerishiq's public monthly work schedule, with named-place references."""

import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

DATASET_ID = '3fddc12a-9e87-482b-90d6-a22435582258'
RESOURCE_ID = '24d595c9-fae8-4f70-9651-ac82772505f1'
SOURCE_URL = 'https://opendata.az/en/@azerisiq-asc/elektrik-techizatinda-planli-fasileler'
API_URL = 'https://admin.opendata.az/api/3/action/package_show?id=elektrik-techizatinda-planli-fasileler'
_UTC = dt.timezone.utc
_ZONE = dt.timezone(dt.timedelta(hours=4))
_FIELDS = ['Sıra sayı', 'Elektrik Şəbəkə', 'Tarix', 'Fasilə saatı', 'Görüləcək iş',
           'Elektrik enerjisi təchizatında fasilə yaranacaq ərazilər']
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'rows': [], 'modified': ''}


def normalize(text):
    value = unicodedata.normalize('NFKD', text.casefold().replace('ə', 'e').replace('ı', 'i'))
    return ' '.join(''.join(c for c in value if not unicodedata.combining(c)).split())


_CATALOG = json.loads(pathlib.Path(__file__).with_name('azerbaijan-outage-locations.json')
                      .read_text(encoding='utf-8'))
_PLACES = _CATALOG['locations']
_NETWORKS = _CATALOG['networks']
_ALIASES = {}
for _place in _PLACES:
    for _alias in _place['aliases']:
        _ALIASES.setdefault((_place['admin1'], _alias), []).append(_place)

_MATCHERS = {}
_SCOPES = {}
for _network, _codes in _NETWORKS.items():
    _scope = tuple(_codes)
    if _scope in _SCOPES:
        _MATCHERS[_network] = _SCOPES[_scope]
        continue
    _names = {}
    for (_code, _alias), _places in _ALIASES.items():
        if _code in _codes:
            _names.setdefault(_alias, {}).update({p['id']: p for p in _places})
    if _names:
        _pattern = r'(?<!\w)(?:' + '|'.join(re.escape(a) for a in sorted(_names, key=len, reverse=True)) + r')(?!\w)'
        _MATCHERS[_network] = (re.compile(_pattern), _names)
        _SCOPES[_scope] = _MATCHERS[_network]


def locations_for(network, address):
    key = normalize(network)
    codes = _NETWORKS.get(key, [])
    matcher = _MATCHERS.get(key)
    if not matcher:
        return []
    text = normalize(address)
    text = re.sub(r'^baki\s+seheri\s*[,;]\s*', '', text)
    # A district prefix names the utility area, not an additional affected city.
    text = re.sub(r'^' + re.escape(normalize(network)) + r'\s+rayon(?:u)?\b', '', text)
    matches = []
    pattern, names = matcher
    for match in pattern.finditer(text):
        candidates = list(names[match.group()].values())
        suffix = text[match.end():match.end() + 12]
        if re.match(r'\s+(?:kuc|prospekt|dalan|pr\.)', suffix):
            continue  # A street named after a town is not that affected town.
        if re.match(r'\s+seh(?:eri|er|\.)', suffix):
            candidates = [p for p in candidates if p['kind'] in {'PPLA', 'PPLC'}]
        unique = {p['id']: p for p in candidates}
        if len(unique) == 1:
            matches.append((match.start(), match.end(), next(iter(unique.values()))))
    selected = {}
    for start, end, place in matches:
        if any(other_start <= start and end <= other_end and other_end - other_start > end - start
               for other_start, other_end, _ in matches):
            continue
        selected[place['id']] = place
    # Explicit generic city descriptions can use the named utility's city only.
    if not selected and re.fullmatch(r'seher\s+erazisi(?:nin)?\s*(?:bir\s*hissesi)?', text):
        candidates = {p['id']: p for code in codes
                      for p in _ALIASES.get((code, normalize(network)), [])
                      if p['kind'] in {'PPLA', 'PPLC'}}
        if len(candidates) == 1:
            selected.update(candidates)
    return list(selected.values())


def _allowed_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        return False
    path = urllib.parse.unquote(parsed.path)
    if '\\' in path or any(segment in {'.', '..'} for segment in path.split('/')):
        return False
    if parsed.hostname == 'admin.opendata.az':
        return url == API_URL or parsed.path.startswith(
            f'/dataset/{DATASET_ID}/resource/{RESOURCE_ID}/download/') and parsed.path.endswith('.csv')
    return parsed.hostname == 'data-storage.opendata.az' and parsed.path.startswith(
        f'/ckan-prod-storage/ckan/resources/{RESOURCE_ID}/') and parsed.path.endswith('.csv')


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if not _allowed_url(url):
            raise ValueError('Unexpected outage download redirect')
        return super().redirect_request(request, response, code, message, headers, url)


_OPENER = urllib.request.build_opener(_Redirect())


def _read(url, limit):
    if not _allowed_url(url):
        raise ValueError('Unexpected outage source URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (https://globeview.app)'})
    with _OPENER.open(request, timeout=20) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('Outage source exceeded size limit')
    return body.decode('utf-8-sig')


def _rows(text):
    reader = csv.reader(io.StringIO(text), delimiter=';')
    if [cell.strip() for cell in next(reader, [])] != _FIELDS:
        raise ValueError('Unexpected outage CSV columns')
    rows = list(reader)
    if len(rows) > 5000:
        raise ValueError('Outage schedule exceeded row limit')
    return [row for row in rows if len(row) == 6]


def _schedule():
    with _LOCK:
        if time.time() < _CACHE['until']:
            return _CACHE['rows'], _CACHE['modified']
        package = json.loads(_read(API_URL, 300000)).get('result', {})
        if (package.get('id') != DATASET_ID or package.get('license_id') != 'cc-zero'
                or package.get('organization', {}).get('name') != 'azerisiq-asc'):
            raise ValueError('Unexpected outage dataset publisher or license')
        resources = [r for r in package.get('resources', []) if r.get('id') == RESOURCE_ID
                     and r.get('format') == 'CSV' and r.get('state') == 'active']
        if len(resources) != 1:
            raise ValueError('Published outage CSV is unavailable')
        resource = resources[0]
        rows = _rows(_read(resource['url'], 1000000))
        modified = resource.get('last_modified', '')
        _CACHE.update(until=time.time() + 3600, rows=rows, modified=modified)
        return rows, modified


def parse_schedule(rows, now=None, modified=''):
    now = now or dt.datetime.now(_UTC)
    if now.tzinfo is None:
        raise ValueError('Outage clock must have a time zone')
    features, seen = [], set()
    for number, network, date, hours, work, address in rows:
        # Multiple dates, ranges, durations and multiline tables need separate
        # source verification; do not invent daily outages or start times.
        day = re.fullmatch(r'\s*(\d{2})\s*[.,]\s*(\d{2})\s*[.,]\s*(\d{4})\s*\.?\s*', date)
        clock = re.fullmatch(r'\s*(\d{1,2})[:.;]?(\d{2})\s*[-–]\s*(\d{1,2})[:.;]?(\d{2})\s*', hours)
        if not day or not clock or not work.strip() or not address.strip():
            continue
        try:
            start = dt.datetime(int(day[3]), int(day[2]), int(day[1]), int(clock[1]), int(clock[2]), tzinfo=_ZONE)
            end = start.replace(hour=int(clock[3]), minute=int(clock[4]))
        except ValueError:
            continue
        if not start < end or now >= end or start > now + dt.timedelta(days=7):
            continue
        for place in locations_for(network, address):
            key = hashlib.sha256('\n'.join([network, date, hours, work, address, place['id']])
                                 .encode()).hexdigest()[:20]
            if key in seen:
                continue
            seen.add(key)
            window = f'{start:%d %b} · {start:%H:%M}–{end:%H:%M} UTC+4'
            features.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': place['coordinates']},
                             'properties': {
                                 'key': f'az:azerishiq:planned:{key}',
                                 'provider': 'Azerishiq · planned power work',
                                 'area_name': place['name'] + ' · Approximate place reference',
                                 'status': ('Scheduled' if now < start else 'Planned work window') + ' · ' + window,
                                 'reason': ' '.join(work.split())[:400], 'etr': '', 'customers_affected': None,
                                 'source_label': 'Azerishiq / IDDA (CC0) · GeoNames (CC BY 4.0)',
                                 'source_url': SOURCE_URL, 'source_updated': modified,
                                 'source_address': ' '.join(address.split())[:600], 'source_district': network.strip(),
                                 'location_source_url': 'https://www.geonames.org/' + place['id'] + '/',
                                 'location_kind': 'area', 'planned': True,
                                 'starts_at': start.astimezone(_UTC).isoformat(),
                                 'ends_at': end.astimezone(_UTC).isoformat(),
                                 'valid_until': min(end.timestamp(), now.timestamp() + 900),
                             }})
    return features


def azerishiq_planned_outages(now=None):
    rows, modified = _schedule()
    return parse_schedule(rows, now, modified)
