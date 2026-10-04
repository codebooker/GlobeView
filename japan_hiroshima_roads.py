"""Current road restrictions published by Hiroshima Prefecture road navigation."""

import datetime as dt
import json
import math
import re
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


SOURCE = 'https://www.roadnavi.pref.hiroshima.lg.jp/'
_JST = ZoneInfo('Asia/Tokyo')
_FLAGS = ('blocked', 'big_blocked', 'one_side', 'narrows', 'saigai', 'jizen',
          'controll_complex', 'walker_bike', 'chain')


def _source_url():
    query = urllib.parse.urlencode({**{flag: 'true' for flag in _FLAGS}, 'mode': 'honjitu'})
    return SOURCE + 'readKisei.php?' + query


def _read():
    url = _source_url()
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public road-feed reader)',
                                                   'Referer': SOURCE})
    with urllib.request.urlopen(request, timeout=18) as response:
        if response.url != url:
            raise ValueError('Hiroshima road feed redirected')
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError('Hiroshima road feed exceeded size limit')
    return json.loads(body)


def _when(value):
    try:
        return dt.datetime.strptime(value, '%Y/%m/%d %H:%M').replace(tzinfo=_JST)
    except (ValueError, TypeError):
        return None


def _segments(wkt):
    if not isinstance(wkt, str) or len(wkt) > 50000:
        return []
    match = re.fullmatch(r'LINESTRING\s*\(([^()]+)\)', wkt.strip(), re.I)
    if not match:
        return []
    pairs = match[1].split(',')
    if not 2 <= len(pairs) <= 2000:
        return []
    points = []
    for pair in pairs:
        try:
            lon, lat = (float(value) for value in pair.split())
        except ValueError:
            return []
        if not (math.isfinite(lon) and math.isfinite(lat)
                and 131.5 <= lon <= 134.5 and 33.5 <= lat <= 35.3):
            return []
        point = [round(lon, 6), round(lat, 6)]
        if not points or points[-1] != point:
            points.append(point)
    if len(points) < 2:
        return []
    if len(points) > 300:
        step = math.ceil((len(points) - 1) / 299)
        points = points[::step] + ([points[-1]] if points[-1] != points[::step][-1] else [])
    return [points]


def parse_restrictions(payload, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = payload.get('results') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError('Hiroshima road-restriction publication is empty or invalid')
    features, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = str(row.get('id') or '')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,30}', ident) or ident in seen:
            continue
        try:
            lat, lon = float(row['lat']), float(row['lon'])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)
                and 33.5 <= lat <= 35.3 and 131.5 <= lon <= 134.5):
            continue
        start, end = _when(row.get('start_date')), _when(row.get('end_date'))
        if (start and start > now + dt.timedelta(minutes=5)) or (end and end <= now):
            continue
        road = str(row.get('rosenname') or '').strip()[:140]
        reason = str(row.get('kiseireason') or '').strip()[:180]
        restriction = str(row.get('kiseinaiyo') or '').strip()[:180]
        if not road or not (reason or restriction):
            continue
        work = '工事' in reason or '工事' in restriction
        closed = '通行止' in restriction
        layer = 'construction' if work else 'incidents'
        prefix = 'Roadwork' if work else 'Road closure' if closed else 'Road restriction'
        schedule = str(row.get('kisei_hour') or '').strip()
        display_start = start if start and start.year > 2000 else None
        detail = ' · '.join(part for part in (restriction, reason,
                          f'{schedule} JST' if schedule and schedule not in {'−', '-'} else '',
                          f"{display_start:%d %b %Y}–{end:%d %b %Y}" if display_start and end
                          else f"Since {display_start:%d %b %Y}" if display_start else '') if part)
        segments = _segments(row.get('kukanroot'))
        props = {
            'key': f'jp:hiroshima:restriction:{ident}', 'layer': layer,
            'title': f'{prefix} · {road}', 'detail': detail,
            'source': 'Hiroshima Prefecture road navigation', 'source_url': SOURCE,
            'valid_until': now.timestamp() + 600,
        }
        if segments:
            points = segments[0]
            props.update({'road_segments': segments,
                          'road_segment_bounds': [min(p[0] for p in points), min(p[1] for p in points),
                                                  max(p[0] for p in points), max(p[1] for p in points)],
                          'segment_color': '#d97a72' if closed else '#ddb16d'})
        features.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                         'properties': props})
        seen.add(ident)
    if len(features) < 10:
        raise ValueError('Hiroshima road-restriction feed has too few current records')
    return features


def road_restrictions():
    return parse_restrictions(_read())
