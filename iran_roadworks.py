"""Located, current public roadwork notices from Iran's 141 map."""

import datetime as dt
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


API_URL = 'https://api.141.ir/api/road_workshops/bbox'
SOURCE_URL = 'https://141.ir/'
_TEHRAN = ZoneInfo('Asia/Tehran')
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'rows': []}
_FRESH_SECONDS = 2 * 3600
_PASSAGE = {'امكان عبور': 'Passage possible', 'عدم امكان عبور': 'Road closed'}
_OPERATIONS = {'نصب علائم وحفاظ': 'Signs and barriers', 'روكش آسفالت': 'Asphalt resurfacing',
               'درزگیری آسفالت': 'Asphalt crack sealing', 'تعمیرات پل': 'Bridge repairs',
               'تعریض': 'Road widening', 'اصلاح و احداث تقاطع غیر همسطح': 'Interchange work'}


def _jalali_date(gregorian):
    """Convert a Gregorian date to the Solar Hijri date used in 141 notices."""
    year, month, day = gregorian.year - 1600, gregorian.month - 1, gregorian.day - 1
    month_days = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    days = 365 * year + (year + 3) // 4 - (year + 99) // 100 + (year + 399) // 400
    days += sum(month_days[:month]) + day
    actual_year = year + 1600
    if month > 1 and actual_year % 4 == 0 and (actual_year % 100 != 0 or actual_year % 400 == 0):
        days += 1
    cycles, remaining = divmod(days - 79, 12053)
    jalali_year = 979 + 33 * cycles + 4 * (remaining // 1461)
    remaining %= 1461
    if remaining >= 366:
        extra, remaining = divmod(remaining - 1, 365)
        jalali_year += extra
    if remaining < 186:
        return jalali_year, remaining // 31 + 1, remaining % 31 + 1
    remaining -= 186
    return jalali_year, remaining // 30 + 7, remaining % 30 + 1


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'1[34]\d{6}', value):
        raise ValueError('Invalid Solar Hijri date')
    year, month, day = int(value[:4]), int(value[4:6]), int(value[6:])
    if not 1 <= month <= 12 or not 1 <= day <= (31 if month <= 6 else 30 if month <= 11 else 29):
        raise ValueError('Invalid Solar Hijri date')
    return year, month, day


def parse_roadworks(rows, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Iran roadwork clock needs a time zone')
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('Iran roadwork collection changed')
    today = _jalali_date(now.astimezone(_TEHRAN).date())
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
            start, end = _date(meta['start_date']), _date(meta['end_date'])
            if not start <= today <= end:
                continue
            stamp = dt.datetime.fromisoformat(meta['updated_at'].replace('Z', '+00:00'))
            age = (now - stamp).total_seconds()
            if stamp.tzinfo is None or not -120 <= age <= _FRESH_SECONDS:
                continue
            title, province = meta['title'], meta['province_fa']
            if not all(isinstance(v, str) and 0 < len(v) <= 500 and '<' not in v for v in (title, province)):
                continue
            passage = _PASSAGE.get(meta['passing_situation_fa'])
            operation = meta['operation_type_fa']
            if passage is None or not isinstance(operation, str) or len(operation) > 100 or '<' in operation:
                continue
            start_time, end_time = meta['start_time'], meta['end_time']
            if not all(isinstance(v, str) and re.fullmatch(r'\d{4}', v) and
                       int(v[:2]) < 24 and int(v[2:]) < 60 for v in (start_time, end_time)):
                continue
            schedule = f'{start_time[:2]}:{start_time[2:]}–{end_time[:2]}:{end_time[2:]} Iran time'
            detail = (f'{passage} · {_OPERATIONS.get(operation, operation)} · {schedule}'
                      f' · Ends {end[0]:04d}/{end[1]:02d}/{end[2]:02d} (Persian calendar)')
            feature = {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                       'properties': {'key': f'ir:141:roadworks:{ref}', 'layer': 'construction',
                                      'title': title, 'detail': detail,
                                      'province_fa': province, 'operation_type_fa': operation,
                                      'source': 'Iran Road Management Center · 141',
                                      'source_url': SOURCE_URL, 'updated_at': stamp.timestamp(),
                                      'valid_until': (stamp + dt.timedelta(seconds=_FRESH_SECONDS)).timestamp()}}
            if ref in unique and unique[ref] != feature:
                conflicts.add(ref)
            unique[ref] = feature
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    return [feature for ref, feature in unique.items() if ref not in conflicts]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('141 roadwork API redirects are unsupported')


def _read():
    body = urllib.parse.urlencode({'min_lon': 44, 'min_lat': 25, 'max_lon': 64,
                                  'max_lat': 40, 'zoom': 16}).encode()
    request = urllib.request.Request(API_URL, data=body, headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': 'GlobeView/1.0 (public road data)',
        'Origin': SOURCE_URL.rstrip('/'), 'Referer': SOURCE_URL})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=25) as response:
        if response.status != 200 or response.geturl() != API_URL:
            raise ValueError('Unexpected 141 roadwork response')
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError('141 roadwork collection exceeded size limit')
    data = json.loads(payload)
    if type(data.get('error_code')) is not int or data['error_code'] != 0:
        raise ValueError('141 roadwork request failed')
    rows = data.get('data')
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('141 roadwork collection changed')
    fields = ('title', 'province_fa', 'start_date', 'end_date', 'start_time', 'end_time',
              'passing_situation_fa', 'operation_type_fa', 'updated_at')
    return [{'id': r.get('id'), 'lon': r.get('lon'), 'lat': r.get('lat'),
             'meta': {k: r['meta'].get(k) for k in fields}}
            for r in rows if isinstance(r, dict) and isinstance(r.get('meta'), dict)]


def roadworks(now=None):
    with _LOCK:
        if _CACHE['until'] <= time.monotonic():
            rows = _read()
            parse_roadworks(rows, now)
            _CACHE.update(until=time.monotonic() + 300, rows=rows)
        return parse_roadworks(_CACHE['rows'], now)
