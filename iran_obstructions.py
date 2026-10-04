"""Current closure and restriction notices on Iran's public 141 road map."""

import datetime as dt
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request

from iran_roadworks import _TEHRAN, _date, _jalali_date


API_URL = 'https://api.141.ir/api/obstructions/bbox'
SOURCE_URL = 'https://141.ir/'
_FRESH_SECONDS = 2 * 3600
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'rows': []}
_DIRECTIONS = {'هر دو مسیر': 'both directions', 'مسیر رفت': 'outbound',
               'مسیر برگشت': 'return direction'}
_REASONS = {'نبود ایمنی كافی': 'Insufficient road safety',
            'تعمیرات پل': 'Bridge repairs',
            'احداث و تعمیر روشنایی': 'Lighting work'}


def parse_obstructions(rows, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Iran closure clock needs a time zone')
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('Iran closure collection changed')
    today = _jalali_date(now.astimezone(_TEHRAN).date())
    by_id, conflicts = {}, set()
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
            start = _date(meta['start_date'])
            if start > today:
                continue
            start_time = meta['start_time']
            if not isinstance(start_time, str) or not re.fullmatch(r'\d{4}', start_time) or \
                    int(start_time[:2]) > 23 or int(start_time[2:]) > 59:
                continue
            stamp = dt.datetime.fromisoformat(meta['updated_at'].replace('Z', '+00:00'))
            age = (now - stamp).total_seconds()
            if stamp.tzinfo is None or not -120 <= age <= _FRESH_SECONDS:
                continue
            title, province, reason, direction = (meta[k] for k in
                                                  ('title', 'province_fa', 'obstruction_reason_fa', 'direction_fa'))
            if not all(isinstance(v, str) and 0 < len(v) <= 500 and '<' not in v
                       for v in (title, province, reason, direction)):
                continue
            direction_en = _DIRECTIONS.get(direction)
            if direction_en is None:
                continue
            is_work = title.startswith(('كارگاه جاده ای', 'کارگاه جاده ای'))
            layer = 'construction' if is_work else 'incidents'
            label = 'Road restriction' if is_work else 'Reported closure' if title.startswith('انسداد') else 'Traffic restriction'
            reason_en = _REASONS.get(reason, '')
            detail = ' · '.join(part for part in (f'{label} · {direction_en}', reason_en,
                                                  f'Since {start[0]:04d}/{start[1]:02d}/{start[2]:02d} (Persian calendar)') if part)
            feature = {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                       'properties': {'key': f'ir:141:obstruction:{ref}', 'layer': layer,
                                      'title': title, 'detail': detail,
                                      'province_fa': province, 'reason_fa': reason,
                                      'direction_fa': direction,
                                      'source': 'Iran Road Management Center · 141',
                                      'source_url': SOURCE_URL, 'updated_at': stamp.timestamp(),
                                      'valid_until': (stamp + dt.timedelta(seconds=_FRESH_SECONDS)).timestamp()}}
            signature = (round(lon, 6), round(lat, 6), title, reason, direction, start, start_time)
            if ref in by_id and by_id[ref] != (signature, feature):
                conflicts.add(ref)
            by_id[ref] = (signature, feature)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    # The publisher sometimes returns the same notice under two IDs.
    unique = {}
    for ref in sorted(by_id):
        if ref in conflicts:
            continue
        signature, feature = by_id[ref]
        unique.setdefault(signature, feature)
    return list(unique.values())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('141 closure API redirects are unsupported')


def _read():
    body = urllib.parse.urlencode({'min_lon': 44, 'min_lat': 25, 'max_lon': 64,
                                  'max_lat': 40, 'zoom': 16}).encode()
    request = urllib.request.Request(API_URL, data=body, headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': 'GlobeView/1.0 (public road data)',
        'Origin': SOURCE_URL.rstrip('/'), 'Referer': SOURCE_URL})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=25) as response:
        if response.status != 200 or response.geturl() != API_URL:
            raise ValueError('Unexpected 141 closure response')
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError('141 closure collection exceeded size limit')
    data = json.loads(payload)
    if type(data.get('error_code')) is not int or data['error_code'] != 0:
        raise ValueError('141 closure request failed')
    rows = data.get('data')
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('141 closure collection changed')
    fields = ('title', 'province_fa', 'obstruction_reason_fa', 'direction_fa',
              'start_date', 'start_time', 'updated_at')
    return [{'id': r.get('id'), 'lon': r.get('lon'), 'lat': r.get('lat'),
             'meta': {k: r['meta'].get(k) for k in fields}}
            for r in rows if isinstance(r, dict) and isinstance(r.get('meta'), dict)]


def obstructions(now=None):
    with _LOCK:
        if _CACHE['until'] <= time.monotonic():
            rows = _read()
            parse_obstructions(rows, now)
            _CACHE.update(until=time.monotonic() + 300, rows=rows)
        return parse_obstructions(_CACHE['rows'], now)
