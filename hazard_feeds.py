"""Cached, normalized global natural-hazard feeds for the globe client."""
import datetime as dt
import csv
import email.utils
import io
import json
import math
import re
import threading
import time
import urllib.request
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


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
        'User-Agent': 'GlobeView/1.0 (https://globeview.app)',
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
        sources = [s for s in event.get('sources', []) if isinstance(s, dict) and s.get('url')]
        source = next((s['url'] for agency in ('JTWC', 'NOAA_NHC') for s in sources
                       if s.get('id') == agency), None)
        source = source or next((s['url'] for s in sources), event.get('link'))
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
_DWD_CAP_URL = ('https://opendata.dwd.de/weather/alerts/cap/DISTRICT_DWD_STAT/'
                'Z_CAP_C_EDZW_LATEST_PVW_STATUS_PREMIUMDWD_DISTRICT_EN.zip')


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


def _parse_dwd_alerts_zip(body, now=None):
    """The status archive contains all current district notices, or an empty ZIP."""
    now = dt.datetime.now(_UTC) if now is None else now
    items = []
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = archive.infolist()
        if len(members) > 500 or sum(member.file_size for member in members) > 30_000_000:
            raise ValueError('DWD warning archive exceeded size limit')
        if any(not member.filename.endswith('.xml') for member in members):
            raise ValueError('Unexpected DWD warning archive member')
        for member in members:
            cap = ET.fromstring(archive.read(member))
            if (cap.findtext('cap:status', namespaces=_CAP_NS) != 'Actual'
                    or cap.findtext('cap:msgType', namespaces=_CAP_NS) not in ('Alert', 'Update')):
                continue
            identifier = cap.findtext('cap:identifier', namespaces=_CAP_NS) or member.filename
            for index, info in enumerate(cap.findall('cap:info', _CAP_NS)):
                if (info.findtext('cap:language', namespaces=_CAP_NS) != 'en'
                        or not _future_timestamp(info.findtext('cap:expires', namespaces=_CAP_NS), now)):
                    continue
                polygons = []
                areas = []
                for area in info.findall('cap:area', _CAP_NS):
                    for polygon in area.findall('cap:polygon', _CAP_NS):
                        ring = _cap_polygon(polygon.text)
                        if ring:
                            polygons.append([ring])
                    description = area.findtext('cap:areaDesc', namespaces=_CAP_NS)
                    if description:
                        areas.append(description)
                if not polygons:
                    continue
                geometry = {'type': 'MultiPolygon', 'coordinates': polygons}
                point = _polygon_point(geometry)
                if not point:
                    continue
                advice = (info.findtext('cap:instruction', namespaces=_CAP_NS)
                          or info.findtext('cap:description', namespaces=_CAP_NS) or '')
                items.append({
                    'id': 'de:' + identifier + ':' + str(index),
                    'title': info.findtext('cap:headline', namespaces=_CAP_NS) or 'Weather warning',
                    'lon': round(point[0], 5), 'lat': round(point[1], 5),
                    'geometry': geometry, 'locationKind': 'polygon',
                    'country': 'Germany', 'source': 'Deutscher Wetterdienst · GeoBasis-DE / BKG',
                    'severity': info.findtext('cap:severity', namespaces=_CAP_NS),
                    'area': '; '.join(areas)[:250],
                    'advice': ' '.join(advice.split())[:480],
                    'observed': cap.findtext('cap:sent', namespaces=_CAP_NS),
                    'ends': info.findtext('cap:expires', namespaces=_CAP_NS),
                    'sourceUrl': 'https://www.dwd.de/DE/wetter/warnungen/warnWetter_node.html',
                })
    return items


def _germany_alerts():
    request = urllib.request.Request(_DWD_CAP_URL, headers={'User-Agent': 'GlobeView/1.0'})
    with urllib.request.urlopen(request, timeout=20) as response:
        if urllib.parse.urlsplit(response.url).hostname != 'opendata.dwd.de':
            raise ValueError('Unexpected DWD warning redirect')
        modified = response.headers.get('Last-Modified')
        if not modified:
            raise ValueError('DWD warning archive has no publication time')
        age = time.time() - email.utils.parsedate_to_datetime(modified).timestamp()
        if not -300 <= age <= 30 * 60:
            raise ValueError('DWD warning archive is stale')
        body = response.read(4_000_001)
    if len(body) > 4_000_000:
        raise ValueError('DWD warning archive exceeded size limit')
    return _parse_dwd_alerts_zip(body)


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


@lru_cache(maxsize=512)
def _norway_cap_alert(identifier):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,120}', identifier):
        raise ValueError('Invalid Norway CAP identifier')
    url = ('https://api.met.no/weatherapi/metalerts/2.0/current?cap='
           + urllib.parse.quote(identifier, safe='') + '&lang=en')
    return _get_xml(url, max_bytes=250_000)


def _norway_alerts():
    feed = _get_xml('https://api.met.no/weatherapi/metalerts/2.0/current.rss?lang=en&geographicDomain=land')
    now = dt.datetime.now(_UTC)
    items = []
    failed = 0
    for entry in feed.findall('./channel/item')[:250]:
        identifier = (entry.findtext('guid') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,120}', identifier):
            continue
        try:
            cap = _norway_cap_alert(identifier)
        except Exception:
            failed += 1
            continue
        if (cap.findtext('cap:status', namespaces=_CAP_NS) != 'Actual'
                or cap.findtext('cap:msgType', namespaces=_CAP_NS) not in ('Alert', 'Update')):
            continue
        info = next((node for node in cap.findall('cap:info', _CAP_NS)
                     if (node.findtext('cap:language', namespaces=_CAP_NS) or '').startswith('en')), None)
        if info is None or not _future_timestamp(info.findtext('cap:expires', namespaces=_CAP_NS), now):
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
        items.append({
            'id': 'no:' + identifier,
            'title': info.findtext('cap:headline', namespaces=_CAP_NS) or entry.findtext('title') or 'Weather alert',
            'lon': round(point[0], 5), 'lat': round(point[1], 5),
            'geometry': geometry, 'locationKind': 'polygon',
            'country': 'Norway', 'source': 'Norwegian Meteorological Institute',
            'severity': info.findtext('cap:severity', namespaces=_CAP_NS),
            'area': '; '.join(areas)[:250],
            'advice': ' '.join((info.findtext('cap:instruction', namespaces=_CAP_NS)
                                or info.findtext('cap:description', namespaces=_CAP_NS) or '').split())[:480],
            'observed': cap.findtext('cap:sent', namespaces=_CAP_NS),
            'ends': info.findtext('cap:expires', namespaces=_CAP_NS),
            'sourceUrl': 'https://api.met.no/weatherapi/metalerts/2.0/current?cap='
                         + urllib.parse.quote(identifier, safe='') + '&lang=en',
        })
    if failed and not items:
        raise RuntimeError('Norwegian weather notices are unavailable')
    return items


@lru_cache(maxsize=1)
def _ireland_counties():
    path = Path(__file__).with_name('ireland-counties-2019.json')
    return json.loads(path.read_text(encoding='utf-8'))['counties']


def _ireland_alerts():
    warnings = _get_json('https://www.met.ie/Open_Data/json/warning_IRELAND.json', max_bytes=500_000)
    if not isinstance(warnings, list) or len(warnings) > 250:
        raise ValueError('Unexpected Met Éireann warning feed')
    counties = _ireland_counties()
    now = dt.datetime.now(_UTC)
    items = []
    seen = set()
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        identifier = str(warning.get('capId') or '')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,120}', identifier) or identifier in seen:
            continue
        if not _future_timestamp(warning.get('expiry'), now):
            continue
        regions = warning.get('regions')
        if not isinstance(regions, list) or not regions or any(code not in counties for code in regions):
            continue  # Marine or unknown regions have no county boundary here.
        seen.add(identifier)
        regions = list(dict.fromkeys(regions))
        centers = [counties[code]['center'] for code in regions]
        mean_lon = sum(center[0] for center in centers) / len(centers)
        mean_lat = sum(center[1] for center in centers) / len(centers)
        lon, lat = min(centers, key=lambda center:
                       (center[0] - mean_lon) ** 2 + (center[1] - mean_lat) ** 2)
        area = ('Ireland' if len(regions) == len(counties) else
                ', '.join(counties[code]['name'] for code in regions))
        items.append({
            'id': 'ie:' + identifier,
            'title': warning.get('headline') or 'Weather warning',
            'lon': lon, 'lat': lat, 'regions': regions,
            'locationKind': 'county point', 'country': 'Ireland',
            'source': 'Met Éireann', 'severity': warning.get('severity'),
            'area': area, 'advice': warning.get('description') or '',
            'observed': warning.get('issued'), 'ends': warning.get('expiry'),
            'sourceUrl': 'https://cap.met.ie//' + identifier + '.xml',
        })
    return items


_AZORES_ALERT_URL = 'https://www.prociv.azores.gov.pt/alertas/api/?lang=en&limit_last_alerts=30'
_AZORES_GROUPS = {
    'g_ocidental': ('Western Azores', -31.2, 39.46),
    'g_central': ('Central Azores', -27.2, 38.72),
    'g_oriental': ('Eastern Azores', -25.5, 37.75),
}
_AZORES_SEVERITY = {'1': 'Minor', '2': 'Severe', '3': 'Extreme'}


def _parse_azores_alerts(alerts, now=None):
    if not isinstance(alerts, list) or len(alerts) > 30:
        raise ValueError('Unexpected Azores alert feed')
    now = now or dt.datetime.now(_UTC)
    local_now = now.astimezone(ZoneInfo('Atlantic/Azores'))
    items = {}
    for alert in alerts:
        if not isinstance(alert, dict) or str(alert.get('codigo_tipo')) != '1':
            continue
        identifier = str(alert.get('idalerta') or '')
        if not re.fullmatch(r'\d{1,8}', identifier):
            continue
        for group_key, (area, lon, lat) in _AZORES_GROUPS.items():
            group = alert.get(group_key)
            if not isinstance(group, dict):
                continue
            active = []
            for hazard in ('precipitacao', 'vento', 'trovoada', 'agitacao'):
                windows = group.get(hazard)
                if not isinstance(windows, list):
                    continue
                for window in windows:
                    if not isinstance(window, dict):
                        continue
                    try:
                        start = dt.datetime.fromisoformat(f"{window['dia_inicio']}T{window['hora_inicio']}").replace(tzinfo=local_now.tzinfo)
                        end = dt.datetime.fromisoformat(f"{window['dia_fim']}T{window['hora_fim']}").replace(tzinfo=local_now.tzinfo)
                    except (KeyError, TypeError, ValueError):
                        continue
                    if start <= local_now < end and end - start <= dt.timedelta(days=7):
                        active.append((hazard, window, end))
            if not active:
                continue
            previous = items.get(group_key)
            if previous and int(previous['id'].split(':')[2]) > int(identifier):
                continue
            severity = max(active, key=lambda entry:
                           {'1': 1, '2': 2, '3': 3}.get(str(entry[1].get('codigo_cor')), 0))[1]
            descriptions = [f"{window.get('categoria') or hazard}: {window.get('texto') or window.get('cor') or 'warning'}"
                            for hazard, window, _ in active]
            items[group_key] = {
                'id': f'pt:azores:{identifier}:{group_key[2:]}',
                'title': f'{alert.get("titulo_aviso") or "Weather warning"} · {area}',
                'lon': lon, 'lat': lat, 'locationKind': 'island-group representative point',
                'country': 'Portugal', 'source': 'Azores Civil Protection',
                'severity': _AZORES_SEVERITY.get(str(severity.get('codigo_cor')), 'Unknown'),
                'area': area, 'advice': '; '.join(descriptions)[:480],
                'ends': max(entry[2] for entry in active).astimezone(_UTC).isoformat().replace('+00:00', 'Z'),
                'sourceUrl': f'https://www.prociv.azores.gov.pt/alertas/ver.php?id={identifier}',
            }
    return list(items.values())


def _azores_alerts():
    return _parse_azores_alerts(_get_json(_AZORES_ALERT_URL, max_bytes=250_000))


def _world_alerts():
    items = []
    unavailable = []
    loaders = [('Canada', _canada_alerts), ('New Zealand', _new_zealand_alerts),
               ('Norway', _norway_alerts), ('Ireland', _ireland_alerts),
               ('Germany', _germany_alerts), ('Portugal · Azores', _azores_alerts)]
    for country, loader in loaders:
        try:
            items.extend(loader())
        except Exception:
            unavailable.append(country)
    if len(unavailable) == len(loaders):
        raise RuntimeError('International weather alert feeds are unavailable')
    return {'source': 'National meteorological services', 'items': items,
            'countries': [country for country, _ in loaders], 'unavailable': unavailable}


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
                        if len(row) < 61 or row[28] not in {'14', '15', '19', '20'}:
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
                        categories = {'14': ('Protest', 'protest'),
                                      '15': ('Military force posture', 'military'),
                                      '19': ('Fighting', 'conflict'),
                                      '20': ('Mass violence', 'conflict')}
                        items.append({'id': f'gdelt:{event_id}', 'title': f'{actor}: {row[52] or "media-coded event"}'[:170],
                                      'lon': lon, 'lat': lat, 'category': categories[row[28]][0],
                                      'eventType': categories[row[28]][1],
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
