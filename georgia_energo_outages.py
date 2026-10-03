"""Anonymous public Energo-Pro notices at approximate named settlement references."""

import concurrent.futures
import datetime as dt
import functools
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request

from scripts.update_georgia_outage_locations import normalize

API_BASE = 'https://my.energo-pro.ge/owback'
SOURCE_URL = 'https://my.energo-pro.ge/ow/#/disconns'
TTL = 900
_ZONE = dt.timezone(dt.timedelta(hours=4))
_UTC = dt.timezone.utc
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'rows': []}
_FIELDS = ('taskId', 'taskType', 'scName', 'disconnectionArea', 'disconnectionDate', 'reconnectionDate')
_CITY_IDS = {'615532', '613607', '612287', '612366', '611717'}


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2}(?:\.\d{1,7})?)?', value):
        raise ValueError('Unexpected Energo-Pro clock')
    # Python 3.9 accepts only 3/6 fractional digits. Normalize the source's
    # variable precision to datetime's microseconds on every supported runtime.
    if '.' in value:
        clock, fraction = value.split('.')
        value = clock + '.' + fraction.ljust(6, '0')[:6]
    return dt.datetime.fromisoformat(value).replace(tzinfo=_ZONE)


@functools.lru_cache(maxsize=1)
def catalog():
    data = json.loads(Path(__file__).with_name('georgia-outage-locations.json').read_text(encoding='utf-8'))
    if data.get('source') != 'https://download.geonames.org/export/dump/GE.zip' or data.get('license') != 'CC BY 4.0':
        raise ValueError('Georgia place provenance changed')
    places = {p['id']: p for p in data['places']}
    if len(places) != len(data['places']) or not 1000 <= len(places) <= 10000:
        raise ValueError('Unexpected Georgia place catalog')
    for p in places.values():
        lon, lat = p['coordinates']
        if (not p['id'].isdigit() or not math.isfinite(lon) or not math.isfinite(lat)
                or not 39.8 <= lon <= 46.8 or not 41 <= lat <= 43.8):
            raise ValueError('Invalid Georgia settlement reference')
    return _index(data)


def _index(data):
    places = {p['id']: p for p in data['places']}
    scopes, names = {}, {}
    for scope in data['scopes']:
        if scope.get('centre') and scope['centre'] not in places:
            raise ValueError('Unknown Georgia municipality centre')
        for alias in scope['aliases']:
            scopes.setdefault(normalize(alias), []).append(scope)
    for place in places.values():
        for alias in place['aliases']:
            names.setdefault(normalize(alias), []).append(place)
            if ' ' in alias:
                names.setdefault(normalize(alias).replace(' ', ''), []).append(place)
    return {'places': places, 'scopes': scopes, 'names': names, 'queries': data['queries']}


def _name(value):
    return re.sub(r'^(?:(?:ქ\.|სოფ\.)\s*|(?:ქალაქი|სოფელი|დაბა)\s+)', '', normalize(value)).strip()


def _unique(name, refs, scopes=None, province=None):
    candidates = list({p['id']: p for p in refs['names'].get(_name(name), [])}.values())
    global_candidates = candidates
    if scopes:
        candidates = [p for p in candidates if any(p['admin2'] == s['id'] or p.get('reference_admin2') == s['id'] or p['id'] == s.get('centre')
                       or s['id'] == 'tbilisi' and p['admin1'] == '51' for s in scopes)]
        # The boundary reference is from 2007 and does not establish current
        # municipal limits. A nationwide-unique name with no contradictory
        # GeoNames ADM2 can still locate a published place in this province.
        if not candidates and len(global_candidates) == 1:
            p = global_candidates[0]
            if not p['admin2'] and any(p['admin1'] == s['admin1'] for s in scopes):
                candidates = [p]
    if province:
        candidates = [p for p in candidates if p['admin1'] == province]
    return candidates[0] if len(candidates) == 1 else None


def affected_places(area, refs):
    fragments = [a.strip() for a in area.split(',') if a.strip()]
    contexts = {}
    for fragment in fragments:
        choices = refs['scopes'].get(_name(fragment.split('/')[0]), [])
        if len(choices) == 1:
            contexts[choices[0]['id']] = choices[0]
    result = {}
    for fragment in fragments:
        parts = [p.strip() for p in fragment.split('/')]
        scope_matches = refs['scopes'].get(_name(parts[0]), [])
        place = None
        if len(scope_matches) == 1:
            scope = scope_matches[0]
            if len(parts) > 1:
                place = _unique(parts[1], refs, [scope])
                centre = refs['places'].get(scope.get('centre'))
                # Explicit urban/street addresses can use the named city as an
                # approximate reference. An unknown rural village is NOT
                # replaced with its municipality's administrative centre.
                if not place and centre and (_name(parts[0]) in centre['aliases']) and (
                        centre['id'] in _CITY_IDS or parts[0].startswith('ქ.')
                        or re.search(r'ქუჩა|ქ\.|გამზირი|ჩიხი|გზატკეცილი', parts[1])):
                    place = centre
            else:
                place = _unique(parts[0], refs, [scope])
        elif len(scope_matches) > 1:
            continue
        elif _name(parts[0]) == 'სამეგრელო' and len(parts) > 1:
            place = _unique(parts[1], refs, province='71')
        else:
            # A service centre is never the affected place. Bare settlement
            # names need uniqueness in the notice's explicit municipalities,
            # or nationwide uniqueness when it supplies no municipality.
            place = _unique(parts[0], refs, list(contexts.values()) or None)
        if place:
            entry = result.setdefault(place['id'], {'place': place, 'addresses': []})
            if fragment not in entry['addresses']:
                entry['addresses'].append(fragment)
    return list(result.values())


def parse_notices(rows, now=None, refs=None):
    now = now or dt.datetime.now(_UTC)
    if now.tzinfo is None:
        raise ValueError('Energo-Pro clock needs a time zone')
    refs = refs or catalog()
    groups = {}
    for row in rows:
        if (type(row.get('taskId')) is not int or row['taskId'] <= 0
                or row.get('taskType') not in {'1', '3'}):
            continue
        try:
            start, end = _clock(row.get('disconnectionDate')), _clock(row.get('reconnectionDate'))
        except ValueError:
            continue
        if end <= start or end <= now or start > now and row['taskType'] != '1':
            continue
        area = row.get('disconnectionArea')
        if not isinstance(area, str) or not 0 < len(area) <= 20000:
            continue
        key = (row['taskId'], row.get('scName'), row['disconnectionDate'])
        # Searches overlap. Without an update timestamp, contradictory copies
        # are omitted rather than choosing a possibly superseded end/area.
        groups.setdefault(key, {})[json.dumps({k: row.get(k) for k in _FIELDS}, sort_keys=True)] = (row, start, end)
    output, seen = [], {}
    for versions in groups.values():
        if len(versions) != 1:
            continue
        row, start, end = next(iter(versions.values()))
        planned = row['taskType'] == '1'
        for match in affected_places(row['disconnectionArea'], refs):
            place = match['place']
            key = f'ge:energo:{row["taskId"]}:{place["id"]}:{start.isoformat()}:{end.isoformat()}'
            if key in seen:
                existing = seen[key]['properties']
                fragments = list(dict.fromkeys(existing['source_address'].split(', ') + match['addresses']))
                existing['source_address'] = ', '.join(fragments)[:3000]
                continue
            phase = 'Scheduled' if now < start else 'Planned work window'
            end_label = end.strftime('%H:%M' if start.date() == end.date() else '%d %b · %H:%M')
            status = phase + f' · {start:%d %b · %H:%M}–{end_label} UTC+4' if planned else 'Reported unplanned outage'
            service = row.get('scName')
            source_url = SOURCE_URL + '/' + urllib.parse.quote(service, safe='') if isinstance(service, str) else SOURCE_URL
            output.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': place['coordinates']},
                           'properties': {
                               'key': key, 'provider': 'Energo-Pro Georgia',
                               'area_name': place['name'] + ' · Georgia · Approximate place reference',
                               'status': status,
                               'etr': '' if planned else f'{end:%d %b · %H:%M} UTC+4 (estimated)',
                               'planned': planned, 'customers_affected': None, 'location_kind': 'area',
                               'source_address': ', '.join(match['addresses'])[:3000],
                               'source_label': 'Energo-Pro Georgia · GeoNames / geoBoundaries (CC BY 4.0)',
                               'source_url': source_url,
                               'location_source_url': 'https://www.geonames.org/' + place['id'],
                               'starts_at': start.astimezone(_UTC).isoformat(),
                               'ends_at': end.astimezone(_UTC).isoformat(),
                               'valid_until': min(end.timestamp(), now.timestamp() + TTL),
                           }})
            seen[key] = output[-1]
    return output


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('Energo-Pro API redirects are unsupported')


def _read(path, search=None):
    if path not in {'/alerts', '/get/cities', '/searchAlerts'}:
        raise ValueError('Unknown public Energo-Pro endpoint')
    body = json.dumps({'search': search}, ensure_ascii=False).encode() if path == '/searchAlerts' else None
    request = urllib.request.Request(API_BASE + path, data=body, headers={
        'User-Agent': 'GlobeView/1.0 (public electricity notices)',
        'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json'})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
        if response.status != 200 or response.geturl() != API_BASE + path:
            raise ValueError('Unexpected Energo-Pro API response')
        raw = response.read(4000001)
    if len(raw) > 4000000:
        raise ValueError('Energo-Pro response exceeded size limit')
    data = json.loads(raw)
    rows = data.get('data')
    if data.get('status') != 200 or rows is not None and not isinstance(rows, list):
        raise ValueError('Energo-Pro public catalog changed')
    rows = rows or []
    if len(rows) > 100 or any(not isinstance(r, dict) for r in rows):
        raise ValueError('Energo-Pro public list limit changed')
    fields = ('nameGe', 'disabled') if path == '/get/cities' else _FIELDS
    return [{k: row.get(k) for k in fields} for row in rows]


def _collect(now):
    cities, default = _read('/get/cities'), _read('/alerts')
    queries = set(catalog()['queries'])
    queries.update(row['nameGe'] for row in cities if row.get('disabled') is False)
    queries.update(row['scName'] for row in default)
    if not 1 <= len(queries) <= 100 or any(not isinstance(q, str) or not re.fullmatch(r'[ა-ჰ\s-]{1,80}', q) for q in queries):
        raise ValueError('Unexpected Energo-Pro public city names')
    rows, searched = list(default), set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        while queries - searched:
            pending = queries - searched
            searched.update(pending)
            futures = {pool.submit(_read, '/searchAlerts', name): name for name in sorted(pending)}
            for future in concurrent.futures.as_completed(futures):
                batch = future.result()
                # A full list containing only upcoming work might omit earlier
                # current records. Fail this refresh rather than accept that scan.
                if len(batch) == 100 and all(_clock(r.get('disconnectionDate')) > now for r in batch):
                    raise ValueError('Energo-Pro city search exceeded current-record limit')
                rows.extend(batch)
                queries.update(row['scName'] for row in batch)
                if len(queries) > 100 or any(not isinstance(q, str) or not re.fullmatch(r'[ა-ჰ\s-]{1,80}', q) for q in queries):
                    raise ValueError('Unexpected Energo-Pro service-centre names')
    return rows


def energo_pro_outages(now=None):
    now = now or dt.datetime.now(_UTC)
    with _LOCK:
        if _CACHE['until'] <= time.monotonic():
            rows = _collect(now)
            _CACHE.update(until=time.monotonic() + TTL, rows=rows)
        return parse_notices(_CACHE['rows'], now)
