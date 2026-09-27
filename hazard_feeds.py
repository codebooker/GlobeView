"""Cached, normalized global natural-hazard feeds for the globe client."""
import datetime as dt
import csv
import io
import json
import math
import threading
import time
import urllib.request
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path


FEEDS = {
    'cyclones': {'ttl': 900, 'stale': 3600, 'loader': None},
    'earthquakes': {'ttl': 120, 'stale': 900, 'loader': None},
    'fires': {'ttl': 1800, 'stale': 7200, 'loader': None},
    'floods': {'ttl': 1800, 'stale': 7200, 'loader': None},
    'volcanoes': {'ttl': 1800, 'stale': 7200, 'loader': None},
    'nws_alerts': {'ttl': 120, 'stale': 900, 'loader': None},
    'world_alerts': {'ttl': 180, 'stale': 600, 'loader': None},
    'gdelt_events': {'ttl': 900, 'stale': 3600, 'loader': None},
}
_CACHE = {}
_INFLIGHT = {}
_RETRY_AFTER = {}
_LOCK = threading.Lock()
_UTC = dt.timezone.utc


def _get_json(url, max_bytes=5_000_000, accept='application/json'):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobalMap/1.0 (natural hazard map)',
        'Accept': accept,
    })
    with urllib.request.urlopen(request, timeout=25) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('Hazard feed exceeded size limit')
    return json.loads(body)


def _get_xml(url, max_bytes=2_000_000):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobalMap/1.0 (weather alert map)',
        'Accept': 'application/xml, application/rss+xml',
    })
    with urllib.request.urlopen(request, timeout=25) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('Weather alert feed exceeded size limit')
    return ET.fromstring(body)


def _point(coordinates):
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        return None
    try:
        lon, lat = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def _cyclones():
    data = _get_json('https://eonet.gsfc.nasa.gov/api/v3/events?category=severeStorms&status=open&limit=100')
    cutoff = dt.datetime.now(_UTC) - dt.timedelta(days=7)
    items = []
    for event in data.get('events', []):
        title = event.get('title') or ''
        if not any(word in title.lower() for word in ('hurricane', 'typhoon', 'cyclone', 'tropical storm', 'tropical depression')):
            continue
        positions = [g for g in event.get('geometry', []) if g.get('type') == 'Point' and _point(g.get('coordinates'))]
        if not positions:
            continue
        latest = max(positions, key=lambda g: g.get('date') or '')
        try:
            observed = dt.datetime.fromisoformat(latest['date'].replace('Z', '+00:00'))
        except (KeyError, ValueError):
            continue
        if observed < cutoff:
            continue
        lon, lat = _point(latest['coordinates'])
        source = next((s.get('url') for s in event.get('sources', []) if s.get('url')), event.get('link'))
        items.append({
            'id': str(event.get('id') or title), 'title': title, 'lon': lon, 'lat': lat,
            'observed': latest['date'], 'windKt': latest.get('magnitudeValue') if latest.get('magnitudeUnit') == 'kts' else None,
            'sourceUrl': source, 'track': [_point(g['coordinates']) for g in positions],
        })
    return {'source': 'NASA EONET', 'items': items}


def _earthquakes():
    data = _get_json('https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_week.geojson')
    items = []
    for feature in data.get('features', []):
        point = _point((feature.get('geometry') or {}).get('coordinates'))
        props = feature.get('properties') or {}
        if not point or props.get('mag') is None:
            continue
        lon, lat = point
        items.append({
            'id': str(feature.get('id') or len(items)), 'title': props.get('place') or 'Earthquake',
            'lon': lon, 'lat': lat, 'magnitude': props['mag'],
            'depthKm': (feature['geometry']['coordinates'] + [None, None, None])[2],
            'observed': props.get('time'), 'sourceUrl': props.get('url'),
        })
    return {'source': 'USGS Earthquake Hazards Program', 'items': items}


def _fires():
    today = dt.datetime.now(_UTC).date()
    since = today - dt.timedelta(days=90)
    base = ('https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?'
            f'eventlist=WF&fromdate={since}&todate={today}&alertlevel=red;orange;green')
    by_id = {}
    for page in range(1, 7):
        data = _get_json(f'{base}&pagenumber={page}')
        features = data.get('features', [])
        current = [feature for feature in features if str((feature.get('properties') or {}).get('iscurrent', '')).lower() == 'true']
        for feature in current:
            point = _point((feature.get('geometry') or {}).get('coordinates'))
            props = feature.get('properties') or {}
            if not point or not props.get('eventid'):
                continue
            lon, lat = point
            severity = props.get('severitydata') or {}
            link = props.get('url') or {}
            event_id = str(props['eventid'])
            by_id[event_id] = {
                'id': event_id, 'title': props.get('name') or 'Wildfire',
                'lon': lon, 'lat': lat, 'country': props.get('country'),
                'alert': props.get('alertlevel'), 'areaHa': severity.get('severity') if severity.get('severityunit') == 'ha' else None,
                'observed': props.get('todate'), 'sourceUrl': link.get('report'),
            }
        if len(features) < 100 or not current:
            break
    return {'source': 'Global Disaster Alert and Coordination System, GDACS', 'items': list(by_id.values())}


def _gdacs_events(event_type, days, current_only=False):
    today = dt.datetime.now(_UTC).date()
    since = today - dt.timedelta(days=days)
    base = ('https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?'
            f'eventlist={event_type}&fromdate={since}&todate={today}&alertlevel=red;orange;green')
    by_id = {}
    for page in range(1, 7):
        data = _get_json(f'{base}&pagenumber={page}')
        features = data.get('features', [])
        for item in features:
            props = item.get('properties') or {}
            if current_only and str(props.get('iscurrent', '')).lower() != 'true':
                continue
            point = _point((item.get('geometry') or {}).get('coordinates'))
            if not point or not props.get('eventid'):
                continue
            event_id = str(props['eventid'])
            link = props.get('url') or {}
            by_id[event_id] = {
                'id': event_id, 'title': props.get('name') or props.get('description') or event_type,
                'lon': point[0], 'lat': point[1], 'country': props.get('country'),
                'alert': props.get('alertlevel'), 'observed': props.get('datemodified') or props.get('todate'),
                'sourceUrl': link.get('report'), 'active': str(props.get('iscurrent', '')).lower() == 'true',
            }
        if len(features) < 100:
            break
    return {'source': 'Global Disaster Alert and Coordination System, GDACS', 'items': list(by_id.values())}


@lru_cache(maxsize=1)
def _nws_zones():
    path = Path(__file__).with_name('nws-zone-centroids.json')
    return json.loads(path.read_text())['zones']


def _polygon_point(geometry):
    if not isinstance(geometry, dict) or geometry.get('type') not in ('Polygon', 'MultiPolygon'):
        return None
    coordinates = geometry.get('coordinates') or []
    try:
        polygons = [coordinates] if geometry['type'] == 'Polygon' else coordinates
        ring = max((polygon[0] for polygon in polygons if polygon and polygon[0]), key=len)
        points = [_point(coordinate) for coordinate in ring]
        points = [point for point in points if point]
        if len(points) < 3:
            return None
        if points[0] == points[-1]:
            points.pop()
        longitude = math.degrees(math.atan2(
            sum(math.sin(math.radians(point[0])) for point in points),
            sum(math.cos(math.radians(point[0])) for point in points)))
        latitude = sum(point[1] for point in points) / len(points)
        return longitude, latitude
    except (IndexError, TypeError, ValueError):
        return None


def _nws_alerts():
    data = _get_json('https://api.weather.gov/alerts/active', max_bytes=15_000_000,
                     accept='application/geo+json')
    zones = _nws_zones()
    items = []
    unmapped = 0
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        if props.get('status') != 'Actual' or props.get('messageType') not in ('Alert', 'Update'):
            continue
        geometry = feature.get('geometry')
        point = _polygon_point(geometry)
        location_kind = 'polygon' if point else 'zone centroid'
        zone_name = ''
        if not point:
            for url in props.get('affectedZones') or []:
                code = str(url).rsplit('/', 1)[-1]
                zone = zones.get(code)
                if zone:
                    point = _point(zone)
                    zone_name = zone[2]
                    break
        if not point:
            unmapped += 1
            continue
        source_url = props.get('@id') or feature.get('id')
        items.append({
            'id': str(props.get('id') or source_url or len(items)),
            'title': props.get('event') or props.get('headline') or 'NWS alert',
            'lon': round(point[0], 5), 'lat': round(point[1], 5),
            'severity': props.get('severity'), 'area': props.get('areaDesc'),
            'advice': ' '.join(str(props.get('instruction') or props.get('description') or '').split())[:360],
            'zoneName': zone_name, 'locationKind': location_kind,
            'observed': props.get('sent'), 'ends': props.get('ends') or props.get('expires'),
            'sourceUrl': source_url if str(source_url).startswith('https://api.weather.gov/alerts/') else None,
            'geometry': geometry if location_kind == 'polygon' else None,
        })
    return {'source': 'National Weather Service', 'items': items, 'unmapped': unmapped}


def _future_timestamp(value, now):
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')) > now
    except (TypeError, ValueError):
        return False


def _canada_alerts():
    url = 'https://api.weather.gc.ca/collections/weather-alerts/items?f=json&limit=1000'
    data = _get_json(url, max_bytes=25_000_000, accept='application/geo+json')
    features = data.get('features') or []
    if any(link.get('rel') == 'next' for link in data.get('links') or []):
        raise ValueError('Canadian weather alerts exceed the supported page size')
    now = dt.datetime.now(_UTC)
    items = []
    for feature in features:
        props = feature.get('properties') or {}
        if not _future_timestamp(props.get('expiration_datetime'), now):
            continue
        geometry = feature.get('geometry')
        point = _polygon_point(geometry)
        if not point:
            continue
        identifier = str(feature.get('id') or props.get('feature_id') or '')
        if not identifier:
            continue
        items.append({
            'id': 'ca:' + identifier,
            'title': props.get('alert_short_name_en') or props.get('alert_name_en') or 'Weather alert',
            'lon': round(point[0], 5), 'lat': round(point[1], 5),
            'geometry': geometry, 'locationKind': 'polygon',
            'country': 'Canada', 'source': 'Environment and Climate Change Canada',
            'severity': props.get('risk_colour_en'),
            'area': ', '.join(part for part in (props.get('feature_name_en'), props.get('province')) if part),
            'advice': 'Open the original notice for full details and instructions.',
            'observed': props.get('publication_datetime'), 'ends': props.get('expiration_datetime'),
            'sourceUrl': 'https://api.weather.gc.ca/collections/weather-alerts/items/' + urllib.parse.quote(identifier, safe='') + '?f=html',
        })
    return items


_CAP_NS = {'cap': 'urn:oasis:names:tc:emergency:cap:1.2'}


def _cap_polygon(value):
    points = []
    for pair in str(value or '').split():
        try:
            lat, lon = map(float, pair.split(','))
        except (TypeError, ValueError):
            return None
        point = _point([lon, lat])
        if not point:
            return None
        points.append([point[0], point[1]])
        if len(points) > 5000:
            return None
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    return points


@lru_cache(maxsize=512)
def _nz_cap_alert(url):
    if not url.startswith('https://alerts.metservice.com/cap/alert?id='):
        raise ValueError('Unexpected MetService CAP URL')
    return _get_xml(url, max_bytes=250_000)


def _new_zealand_alerts():
    feed = _get_xml('https://alerts.metservice.com/cap/rss')
    now = dt.datetime.now(_UTC)
    items = []
    seen = set()
    failed = 0
    for entry in feed.findall('./channel/item')[:250]:
        url = entry.findtext('link') or ''
        if not url.startswith('https://alerts.metservice.com/cap/alert?id='):
            continue
        try:
            cap = _nz_cap_alert(url)
        except Exception:
            failed += 1
            continue
        if cap.findtext('cap:status', namespaces=_CAP_NS) != 'Actual' or cap.findtext('cap:msgType', namespaces=_CAP_NS) not in ('Alert', 'Update'):
            continue
        identifier = cap.findtext('cap:identifier', namespaces=_CAP_NS) or url
        for index, info in enumerate(cap.findall('cap:info', _CAP_NS)):
            if not _future_timestamp(info.findtext('cap:expires', namespaces=_CAP_NS), now):
                continue
            polygons = []
            areas = []
            for area in info.findall('cap:area', _CAP_NS):
                for polygon in area.findall('cap:polygon', _CAP_NS):
                    ring = _cap_polygon(polygon.text)
                    if ring:
                        polygons.append([ring])
                if area.findtext('cap:areaDesc', namespaces=_CAP_NS):
                    areas.append(area.findtext('cap:areaDesc', namespaces=_CAP_NS))
            if not polygons:
                continue
            geometry = {'type': 'MultiPolygon', 'coordinates': polygons}
            point = _polygon_point(geometry)
            if not point:
                continue
            key = 'nz:' + identifier + ':' + str(index)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                'id': key, 'title': info.findtext('cap:headline', namespaces=_CAP_NS) or entry.findtext('title') or 'Weather alert',
                'lon': round(point[0], 5), 'lat': round(point[1], 5),
                'geometry': geometry, 'locationKind': 'polygon',
                'country': 'New Zealand', 'source': 'MetService New Zealand',
                'severity': info.findtext('cap:severity', namespaces=_CAP_NS),
                'area': '; '.join(areas)[:250],
                'advice': ' '.join((info.findtext('cap:instruction', namespaces=_CAP_NS) or info.findtext('cap:description', namespaces=_CAP_NS) or '').split())[:480],
                'observed': cap.findtext('cap:sent', namespaces=_CAP_NS),
                'ends': info.findtext('cap:expires', namespaces=_CAP_NS),
                'sourceUrl': url,
            })
    if failed and not items:
        raise RuntimeError('MetService CAP notices are unavailable')
    return items


def _world_alerts():
    items = []
    unavailable = []
    for country, loader in [('Canada', _canada_alerts), ('New Zealand', _new_zealand_alerts)]:
        try:
            items.extend(loader())
        except Exception:
            unavailable.append(country)
    if len(unavailable) == 2:
        raise RuntimeError('International weather alert feeds are unavailable')
    return {'source': 'National meteorological services', 'items': items,
            'countries': ['Canada', 'New Zealand'], 'unavailable': unavailable}


def _gdelt_events():
    """Recent media-coded conflict/protest events; locations may be broad."""
    index = 'https://data.gdeltproject.org/gdeltv2/lastupdate.txt'
    with urllib.request.urlopen(index, timeout=15) as response:
        lines = response.read(20000).decode('utf-8', 'replace').splitlines()
    match = next((line.split() for line in lines if '.export.CSV.zip' in line), None)
    if not match or len(match) < 3:
        raise RuntimeError('GDELT update index unavailable')
    base_url = match[2].replace('http://', 'https://')
    name = base_url.rsplit('/', 1)[-1]
    stamp = name.split('.', 1)[0]
    try:
        latest = dt.datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=_UTC)
    except ValueError as error:
        raise RuntimeError('Invalid GDELT update timestamp') from error
    items = []
    errors = 0
    for minutes_ago in range(0, 181, 15):
        file_time = latest - dt.timedelta(minutes=minutes_ago)
        url = base_url if minutes_ago == 0 else f'https://data.gdeltproject.org/gdeltv2/{file_time:%Y%m%d%H%M%S}.export.CSV.zip'
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'GlobalMap/1.0 (GDELT public event map)'})
            with urllib.request.urlopen(request, timeout=12) as response:
                archive_bytes = response.read(1_000_000)
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                member = archive.namelist()[0]
                with archive.open(member) as payload:
                    text = io.TextIOWrapper(payload, encoding='utf-8', errors='replace')
                    for row in csv.reader(text, delimiter='\t'):
                        if len(row) < 61 or row[28] not in {'14', '15', '16', '17', '18', '19', '20'}:
                            continue
                        try:
                            lon, lat = float(row[57]), float(row[56])
                        except (ValueError, IndexError):
                            continue
                        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                            continue
                        try:
                            observed = dt.datetime.strptime(row[59], '%Y%m%d%H%M%S').replace(tzinfo=_UTC).isoformat()
                        except ValueError:
                            observed = file_time.isoformat()
                        precision = {'1': 'Country-level location', '2': 'State-level location', '3': 'City-level location', '4': 'City-level location', '5': 'Administrative-region location'}.get(row[51], 'Approximate reported location')
                        actor = row[6] or row[16] or 'Reported event'
                        event_id = row[0] or f'{stamp}-{row[57]}-{row[56]}-{len(items)}'
                        categories = {'14': 'Protest', '15': 'Demonstration with force', '16': 'Administrative sanction',
                                      '17': 'Coercive action', '18': 'Assault', '19': 'Armed conflict', '20': 'Mass violence'}
                        items.append({'id': f'gdelt:{event_id}', 'title': f'{actor}: {row[52] or "media-coded event"}'[:170],
                                      'lon': lon, 'lat': lat, 'category': categories[row[28]],
                                      'geoPrecision': precision, 'observed': observed,
                                      'source': 'GDELT Project · automatically coded from news coverage',
                                      'sourceUrl': row[60] if row[60].startswith(('https://', 'http://')) else 'https://www.gdeltproject.org/data.html'})
                        if len(items) >= 250:
                            break
        except Exception:
            errors += 1
        if len(items) >= 250:
            break
    if not items:
        raise RuntimeError(f'GDELT exports unavailable ({errors} files failed)')
    unique = {}
    for item in items:
        unique[item['id']] = item
    return {'source': 'GDELT 2.0 Event Database · 15-minute updates', 'items': list(unique.values())[:250]}


FEEDS['cyclones']['loader'] = _cyclones
FEEDS['earthquakes']['loader'] = _earthquakes
FEEDS['fires']['loader'] = _fires
FEEDS['floods']['loader'] = lambda: _gdacs_events('FL', 90, current_only=True)
FEEDS['volcanoes']['loader'] = lambda: _gdacs_events('VO', 30)
FEEDS['nws_alerts']['loader'] = _nws_alerts
FEEDS['world_alerts']['loader'] = _world_alerts
FEEDS['gdelt_events']['loader'] = _gdelt_events


def hazard_snapshot(layer):
    if layer not in FEEDS:
        raise ValueError('Unknown hazard layer')
    config = FEEDS[layer]
    while True:
        with _LOCK:
            now = time.monotonic()
            cached = _CACHE.get(layer)
            if cached and now < cached['expires']:
                return cached['body']
            if now < _RETRY_AFTER.get(layer, 0):
                if cached and now < cached['stale']:
                    return cached['body']
                raise RuntimeError('Hazard provider is temporarily unavailable')
            event = _INFLIGHT.get(layer)
            if event is None:
                event = threading.Event()
                _INFLIGHT[layer] = event
                break
        event.wait(timeout=30)
    try:
        body = json.dumps(config['loader'](), separators=(',', ':')).encode()
    except Exception:
        with _LOCK:
            _RETRY_AFTER[layer] = time.monotonic() + 60
            cached = _CACHE.get(layer)
            if cached and time.monotonic() < cached['stale']:
                return cached['body']
        raise
    else:
        now = time.monotonic()
        with _LOCK:
            _CACHE[layer] = {'body': body, 'expires': now + config['ttl'], 'stale': now + config['stale']}
            _RETRY_AFTER.pop(layer, None)
        return body
    finally:
        with _LOCK:
            _INFLIGHT.pop(layer, None)
            event.set()
