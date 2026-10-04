"""Current JMA warning/advisory areas from its public disaster XML feed."""

import concurrent.futures
import datetime as dt
import json
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET


FEED = 'https://www.data.jma.go.jp/developer/xml/feed/extra_l.xml'
AREAS = 'https://www.jma.go.jp/bosai/common/const/geojson/class10s.json'
_BULLETIN = re.compile(r'https://www\.data\.jma\.go\.jp/developer/xml/data/'
                       r'\d{14}_\d+_VPWW53_(\d{6})\.xml')
_ATOM = {'a': 'http://www.w3.org/2005/Atom'}
_HEAD = {'h': 'http://xml.kishou.go.jp/jmaxml1/informationBasis1/'}
_UTC = dt.timezone.utc
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'items': None}
_AREA_CACHE = {'until': 0, 'areas': None}


def _read(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (+https://globeview.app)'})
    with urllib.request.urlopen(request, timeout=18) as response:
        if response.url != url:
            raise ValueError('JMA bulletin redirected')
        body = response.read(limit + 1)
    if len(body) > limit or b'<!DOCTYPE' in body.upper():
        raise ValueError('JMA publication exceeded size limit or contains a DTD')
    return body


def _latest_bulletins(feed, now):
    root = ET.fromstring(feed)
    latest = {}
    for entry in root.findall('a:entry', _ATOM)[:10000]:
        link = entry.find('a:link', _ATOM)
        url = link.get('href', '') if link is not None else ''
        match = _BULLETIN.fullmatch(url)
        if not match:
            continue
        try:
            updated = dt.datetime.fromisoformat(
                entry.findtext('a:updated', '', _ATOM).replace('Z', '+00:00'))
        except ValueError:
            continue
        code = match[1]
        if code not in latest or latest[code][0] < updated:
            latest[code] = (updated, url)
    return {code: (issued, url) for code, (issued, url) in latest.items()
            if -dt.timedelta(minutes=5) <= now - issued <= dt.timedelta(hours=24)}


def _areas():
    now = time.monotonic()
    if _AREA_CACHE['areas'] is not None and now < _AREA_CACHE['until']:
        return _AREA_CACHE['areas']
    data = json.loads(_read(AREAS, 1_000_000))
    features = data.get('features') if isinstance(data, dict) else None
    if not isinstance(features, list) or not 100 <= len(features) <= 250:
        raise ValueError('JMA warning areas are invalid')
    areas = {}
    for feature in features:
        props = feature.get('properties') or {}
        code = props.get('code')
        geometry = feature.get('geometry') or {}
        if (isinstance(code, str) and re.fullmatch(r'\d{6}', code)
                and geometry.get('type') in ('Polygon', 'MultiPolygon')):
            polygons = ([geometry.get('coordinates')] if geometry['type'] == 'Polygon'
                        else geometry.get('coordinates'))
            if not isinstance(polygons, list):
                continue
            if code not in areas:
                areas[code] = (props.get('enName') or props.get('name') or code,
                               {'type': 'MultiPolygon', 'coordinates': []})
            areas[code][1]['coordinates'].extend(polygons)
    if len(areas) < 100:
        raise ValueError('JMA warning areas are incomplete')
    _AREA_CACHE.update({'until': now + 24 * 3600, 'areas': areas})
    return areas


def _point(geometry):
    polygons = ([geometry.get('coordinates')] if geometry.get('type') == 'Polygon'
                else geometry.get('coordinates') or [])
    try:
        ring = max((polygon[0] for polygon in polygons if polygon and polygon[0]), key=len)
        coords = [(float(lon), float(lat)) for lon, lat in ring]
    except (TypeError, ValueError, IndexError):
        return None
    if len(coords) < 3 or not all(122 <= lon <= 154 and 20 <= lat <= 47 for lon, lat in coords):
        return None
    if coords[0] == coords[-1]:
        coords.pop()
    return round(sum(p[0] for p in coords) / len(coords), 5), round(sum(p[1] for p in coords) / len(coords), 5)


def _severity(names):
    if any('特別警報' in name for name in names):
        return 'Extreme'
    if any('警報' in name and '注意報' not in name for name in names):
        return 'Severe'
    return 'Moderate'


def parse_bulletin(xml, office_code, issued, url, areas):
    root = ET.fromstring(xml)
    results = []
    for info in root.findall('.//h:Information', _HEAD):
        if '一次細分区域等' not in info.get('type', ''):
            continue
        for entry in info.findall('h:Item', _HEAD):
            area_code = entry.findtext('h:Areas/h:Area/h:Code', '', _HEAD)
            if area_code not in areas:
                continue
            names = list(dict.fromkeys(name.text.strip() for name in entry.findall('h:Kind/h:Name', _HEAD)
                                       if name.text and ('警報' in name.text or '注意報' in name.text)))
            if not names:
                continue
            area_name, geometry = areas[area_code]
            point = _point(geometry)
            if not point:
                continue
            results.append({
                'id': f'jp:jma:{office_code}:{area_code}',
                'title': f'{names[0]} · {area_name}'[:170],
                'lon': point[0], 'lat': point[1], 'geometry': geometry,
                'locationKind': 'polygon', 'country': 'Japan',
                'source': 'Japan Meteorological Agency', 'severity': _severity(names),
                'area': area_name, 'advice': ' / '.join(names)[:300],
                'observed': issued.isoformat().replace('+00:00', 'Z'),
                'sourceUrl': url,
            })
    return results


def alerts():
    """Return current published alert areas, reusing one server-side result for 10 minutes."""
    with _LOCK:
        now = time.monotonic()
        if _CACHE['items'] is not None and now < _CACHE['until']:
            return _CACHE['items']
        published = dt.datetime.now(_UTC)
        bulletins = _latest_bulletins(_read(FEED, 5_000_000), published)
        areas = _areas()
        items, failures = [], 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_read, url, 250_000): (code, issued, url)
                       for code, (issued, url) in bulletins.items()}
            for future in concurrent.futures.as_completed(futures):
                code, issued, url = futures[future]
                try:
                    items.extend(parse_bulletin(future.result(), code, issued, url, areas))
                except (OSError, ValueError, ET.ParseError):
                    failures += 1
        if failures and failures == len(bulletins):
            raise RuntimeError('JMA warning bulletins are unavailable')
        _CACHE.update({'until': now + 600, 'items': items})
        return items
