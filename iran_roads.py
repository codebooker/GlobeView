"""Anonymous public traffic-counter readings from Iran's official 141 map."""

import datetime as dt
import json
import math
import threading
import time
import urllib.parse
import urllib.request

API_URL = 'https://api.141.ir/api/otfs/bbox'
SOURCE_URL = 'https://141.ir/'
MAX_AGE = 15 * 60
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'rows': []}
_STATUS = {'روان': 'Free flowing', 'نیمه سنگین': 'Moderate traffic',
           'سنگین': 'Heavy traffic', 'راه بندان': 'Congested', 'نامشخص': 'Status unknown'}


def parse_sensors(rows, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Iran sensor clock needs a time zone')
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('Iran traffic-counter collection changed')
    unique, conflicts = {}, set()
    for row in rows:
        try:
            ref, meta = row['id'], row['meta']
            if type(ref) is not int or ref <= 0 or not isinstance(meta, dict):
                continue
            if isinstance(row['lon'], bool) or isinstance(row['lat'], bool):
                continue
            lon, lat = float(row['lon']), float(row['lat'])
            if not all(math.isfinite(v) for v in (lon, lat)) or not (44 <= lon <= 64 and 25 <= lat <= 40):
                continue
            title, province = meta['axis_name_fa'], meta['province_fa']
            if not all(isinstance(v, str) and 0 < len(v) <= 500 and '<' not in v for v in (title, province)):
                continue
            stamp = dt.datetime.fromisoformat(meta['updated_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None or not -120 <= (now - stamp).total_seconds() <= MAX_AGE:
                continue
            status = _STATUS.get(meta['tarffic_status'])
            speed = meta.get('avg_of_speed')
            if speed == '' or speed is None:
                speed = None
            elif isinstance(speed, bool):
                continue
            else:
                speed = float(speed)
                if not math.isfinite(speed) or not 0 <= speed <= 250:
                    continue
            if status is None or (speed is None and status == 'Status unknown'):
                continue
            detail = ' · '.join([v for v in [f'Average speed {speed:g} km/h' if speed is not None else '',
                                           status, province] if v])
            feature = {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                       'properties': {'key': f'ir:141:sensor:{ref}', 'layer': 'sensors', 'title': title,
                                      'detail': detail, 'source': 'Iran Road Management Center · 141',
                                      'source_url': SOURCE_URL, 'updated_at': stamp.isoformat(),
                                      'valid_until': (stamp + dt.timedelta(seconds=MAX_AGE)).timestamp()}}
            if ref in unique and unique[ref] != feature:
                conflicts.add(ref)
            unique[ref] = feature
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    return [feature for ref, feature in unique.items() if ref not in conflicts]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('141 traffic API redirects are unsupported')


def _read():
    # One bounded national query shared by every viewport and viewer.
    body = urllib.parse.urlencode({'min_lon': 44, 'min_lat': 25, 'max_lon': 64,
                                  'max_lat': 40, 'zoom': 16}).encode()
    request = urllib.request.Request(API_URL, data=body, headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': 'GlobeView/1.0 (public road data)',
        'Origin': SOURCE_URL.rstrip('/'), 'Referer': SOURCE_URL})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=25) as response:
        if response.status != 200 or response.geturl() != API_URL:
            raise ValueError('Unexpected 141 traffic response')
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError('141 traffic collection exceeded size limit')
    data = json.loads(body)
    if type(data.get('error_code')) is not int or data['error_code'] != 0:
        raise ValueError('141 traffic request failed')
    rows = data.get('data')
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('141 traffic collection changed')
    # Keep only public measurement fields; no user, vehicle or account records.
    fields = ('axis_name_fa', 'province_fa', 'avg_of_speed', 'tarffic_status', 'updated_at')
    return [{'id': r.get('id'), 'lon': r.get('lon'), 'lat': r.get('lat'),
             'meta': {k: r['meta'].get(k) for k in fields}}
            for r in rows if isinstance(r, dict) and isinstance(r.get('meta'), dict)]


def traffic_sensors(now=None):
    with _LOCK:
        if _CACHE['until'] <= time.monotonic():
            rows = _read()
            parse_sensors(rows, now)  # Validate before replacing the shared collection.
            _CACHE.update(until=time.monotonic() + 300, rows=rows)
        return parse_sensors(_CACHE['rows'], now)
