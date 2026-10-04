"""Cached, normalized global natural-hazard feeds for the globe client."""
import datetime as dt
import concurrent.futures
import csv
import email.utils
import io
import json
import math
import re
import ssl
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import zipfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
from tajikistan_alerts import alerts as _tajikistan_alerts


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
    # EONET can leave ended storms marked open for days. Keep only storms with
    # a recent position so selecting a marker is likely to find current guidance.
    cutoff = dt.datetime.now(_UTC) - dt.timedelta(hours=72)
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


_SRI_LANKA_CAP_FEED = 'https://was.meteo.gov.lk/cap/en/rss.xml'
_SRI_LANKA_CAP_URL = re.compile(
    r'https://was\.meteo\.gov\.lk/cap/(?:en/)?[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}'
)


@lru_cache(maxsize=1)
def _sri_lanka_district_points():
    path = Path(__file__).with_name('sri-lanka-district-points.json')
    return json.loads(path.read_text())['districts']


def _sri_lanka_alerts():
    """Map public CAP polygons or approximate points for named districts."""
    feed = _get_xml(_SRI_LANKA_CAP_FEED, max_bytes=500_000)
    now = dt.datetime.now(_UTC)
    districts = _sri_lanka_district_points()
    items = []
    seen = set()
    failed = 0
    for entry in feed.findall('./channel/item')[:200]:
        url = (entry.findtext('link') or '').strip()
        if not _SRI_LANKA_CAP_URL.fullmatch(url):
            continue
        try:
            cap = _get_xml(url, max_bytes=300_000)
        except Exception:
            failed += 1
            continue
        if (cap.findtext('cap:status', namespaces=_CAP_NS) != 'Actual'
                or cap.findtext('cap:msgType', namespaces=_CAP_NS) not in ('Alert', 'Update')
                or cap.findtext('cap:scope', namespaces=_CAP_NS) not in (None, 'Public')):
            continue
        identifier = cap.findtext('cap:identifier', namespaces=_CAP_NS) or url.rsplit('/', 1)[-1]
        sent = cap.findtext('cap:sent', namespaces=_CAP_NS)
        for index, info in enumerate(cap.findall('cap:info', _CAP_NS)):
            language = info.findtext('cap:language', namespaces=_CAP_NS) or 'en'
            if not language.lower().startswith('en') or not _future_timestamp(info.findtext('cap:expires', namespaces=_CAP_NS), now):
                continue
            areas = info.findall('cap:area', _CAP_NS)
            polygons = []
            area_text = []
            for area in areas:
                description = (area.findtext('cap:areaDesc', namespaces=_CAP_NS) or '').strip()
                if description:
                    area_text.append(description)
                for polygon in area.findall('cap:polygon', _CAP_NS):
                    ring = _cap_polygon(polygon.text)
                    if ring:
                        polygons.append([ring])
            base = {
                'title': info.findtext('cap:headline', namespaces=_CAP_NS) or entry.findtext('title') or 'Weather alert',
                'country': 'Sri Lanka', 'source': 'Sri Lanka Department of Meteorology',
                'severity': info.findtext('cap:severity', namespaces=_CAP_NS),
                'advice': ' '.join((info.findtext('cap:instruction', namespaces=_CAP_NS)
                                    or info.findtext('cap:description', namespaces=_CAP_NS) or '').split())[:480],
                'observed': sent, 'ends': info.findtext('cap:expires', namespaces=_CAP_NS),
                'sourceUrl': url,
            }
            if polygons:
                geometry = {'type': 'MultiPolygon', 'coordinates': polygons}
                point = _polygon_point(geometry)
                if point and 78 <= point[0] <= 83 and 5 <= point[1] <= 11:
                    key = f'lk:{identifier}:{index}'
                    if key not in seen:
                        seen.add(key)
                        items.append({**base, 'id': key, 'lon': round(point[0], 5), 'lat': round(point[1], 5),
                                      'geometry': geometry, 'locationKind': 'polygon',
                                      'area': '; '.join(area_text)[:250]})
                continue
            descriptions = ' '.join(area_text)
            for district, point in districts.items():
                if not re.search(r'(?<![A-Za-z])' + re.escape(district) + r'(?: District)?(?![A-Za-z])',
                                 descriptions, re.I):
                    continue
                key = f'lk:{identifier}:{index}:{district.lower().replace(" ", "-")}'
                if key in seen:
                    continue
                seen.add(key)
                items.append({**base, 'id': key, 'lon': point[0], 'lat': point[1],
                              'locationKind': 'published area representative point',
                              'area': district + ' District'})
    if failed and not items:
        raise RuntimeError('Sri Lanka CAP notices are unavailable')
    return items


_MALDIVES_CAP_FEED = 'https://cap.meteorology.gov.mv/rss/alerts/'
_MALDIVES_CAP_URL = re.compile(r'https://cap\.meteorology\.gov\.mv/rss/alerts/\d{1,8}')


@lru_cache(maxsize=256)
def _maldives_cap_alert(url):
    if not _MALDIVES_CAP_URL.fullmatch(url):
        raise ValueError('Unexpected Maldives CAP URL')
    return _get_xml(url, max_bytes=300_000)


def _maldives_alerts():
    feed = _get_xml(_MALDIVES_CAP_FEED, max_bytes=500_000)
    now = dt.datetime.now(_UTC)
    caps = []
    failed = 0
    for entry in feed.findall('./channel/item')[:100]:
        url = (entry.findtext('link') or '').strip()
        if not _MALDIVES_CAP_URL.fullmatch(url):
            continue
        try:
            published = email.utils.parsedate_to_datetime(entry.findtext('pubDate') or '')
            if not dt.timedelta(minutes=-5) <= now - published <= dt.timedelta(days=2):
                continue
            caps.append((url, _maldives_cap_alert(url)))
        except (TypeError, ValueError):
            continue
        except Exception:
            failed += 1
    if failed and not caps:
        raise RuntimeError('Maldives CAP notices are unavailable')
    referenced = set()
    for _, cap in caps:
        for reference in (cap.findtext('cap:references', namespaces=_CAP_NS) or '').split():
            parts = reference.split(',')
            if len(parts) == 3:
                referenced.add(parts[1])
    items = []
    for url, cap in caps:
        identifier = cap.findtext('cap:identifier', namespaces=_CAP_NS) or ''
        if not identifier or len(identifier) > 160 or identifier in referenced:
            continue
        if (cap.findtext('cap:status', namespaces=_CAP_NS) != 'Actual'
                or cap.findtext('cap:scope', namespaces=_CAP_NS) != 'Public'
                or cap.findtext('cap:msgType', namespaces=_CAP_NS) not in ('Alert', 'Update')):
            continue
        sent = cap.findtext('cap:sent', namespaces=_CAP_NS)
        try:
            sent_at = dt.datetime.fromisoformat(sent.replace('Z', '+00:00'))
            if not dt.timedelta(minutes=-5) <= now - sent_at <= dt.timedelta(days=2):
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        for index, info in enumerate(cap.findall('cap:info', _CAP_NS)):
            if not _future_timestamp(info.findtext('cap:expires', namespaces=_CAP_NS), now):
                continue
            polygons = []
            names = []
            for area in info.findall('cap:area', _CAP_NS):
                description = area.findtext('cap:areaDesc', namespaces=_CAP_NS)
                if description:
                    names.append(description)
                for polygon in area.findall('cap:polygon', _CAP_NS):
                    ring = _cap_polygon(polygon.text)
                    if ring and all(71 <= lon <= 75 and -2 <= lat <= 9 for lon, lat in ring):
                        polygons.append([ring])
            if not polygons or len(polygons) > 30 or sum(len(polygon[0]) for polygon in polygons) > 20_000:
                continue
            geometry = {'type': 'MultiPolygon', 'coordinates': polygons}
            point = _polygon_point(geometry)
            if not point:
                continue
            advice = info.findtext('cap:instruction', namespaces=_CAP_NS) or info.findtext('cap:description', namespaces=_CAP_NS) or ''
            items.append({
                'id': f'mv:{url.rsplit("/", 1)[-1]}:{index}',
                'title': info.findtext('cap:headline', namespaces=_CAP_NS) or
                         info.findtext('cap:event', namespaces=_CAP_NS) or 'Weather alert',
                'lon': round(point[0], 5), 'lat': round(point[1], 5),
                'geometry': geometry, 'locationKind': 'polygon',
                'country': 'Maldives', 'source': 'Maldives Meteorological Service',
                'severity': info.findtext('cap:severity', namespaces=_CAP_NS),
                'area': '; '.join(names)[:250], 'advice': ' '.join(advice.split())[:480],
                'observed': sent, 'ends': info.findtext('cap:expires', namespaces=_CAP_NS),
                'sourceUrl': url,
            })
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


_PAGASA_FEED = 'https://publicalert.pagasa.dost.gov.ph/feeds/'
_PAGASA_HOST = 'publicalert.pagasa.dost.gov.ph'
_ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom'}


def _pagasa_cap_url(link):
    """Move the agency's IP-address links to its matching public HTTPS hostname."""
    path = urllib.parse.urlsplit(link).path
    if re.fullmatch(r'/output/[a-z0-9_-]+/[0-9a-f-]{36}\.cap', path):
        return f'https://{_PAGASA_HOST}{path}'
    return None


def _parse_pagasa_caps(caps, now=None):
    now = now or dt.datetime.now(_UTC)
    referenced = set()
    for _, cap in caps:
        for reference in (cap.findtext('cap:references', namespaces=_CAP_NS) or '').split():
            parts = reference.split(',')
            if len(parts) == 3:
                referenced.add(parts[1])
    items = []
    for url, cap in caps:
        identifier = cap.findtext('cap:identifier', namespaces=_CAP_NS) or ''
        if not re.fullmatch(r'[0-9a-f-]{36}', identifier) or identifier in referenced:
            continue
        if (cap.findtext('cap:status', namespaces=_CAP_NS) != 'Actual' or
                cap.findtext('cap:scope', namespaces=_CAP_NS) != 'Public' or
                cap.findtext('cap:msgType', namespaces=_CAP_NS) not in ('Alert', 'Update')):
            continue
        sent = cap.findtext('cap:sent', namespaces=_CAP_NS)
        try:
            sent_at = dt.datetime.fromisoformat(sent.replace('Z', '+00:00'))
        except (AttributeError, ValueError):
            continue
        if not dt.timedelta(minutes=-5) <= now - sent_at <= dt.timedelta(days=2):
            continue
        for index, info in enumerate(cap.findall('cap:info', _CAP_NS)):
            if (not _future_timestamp(info.findtext('cap:expires', namespaces=_CAP_NS), now) or
                    info.findtext('cap:responseType', namespaces=_CAP_NS) == 'AllClear' or
                    info.findtext('cap:urgency', namespaces=_CAP_NS) == 'Past'):
                continue
            polygons, names = [], []
            for area in info.findall('cap:area', _CAP_NS):
                name = area.findtext('cap:areaDesc', namespaces=_CAP_NS)
                if name:
                    names.append(name)
                for polygon in area.findall('cap:polygon', _CAP_NS):
                    ring = _cap_polygon(polygon.text)
                    if ring and all(116 <= lon <= 128 and 4 <= lat <= 22 for lon, lat in ring):
                        polygons.append([ring])
            if not polygons or len(polygons) > 50 or sum(len(polygon[0]) for polygon in polygons) > 20_000:
                continue
            geometry = {'type': 'MultiPolygon', 'coordinates': polygons}
            point = _polygon_point(geometry)
            if not point:
                continue
            advice = (info.findtext('cap:instruction', namespaces=_CAP_NS) or
                      info.findtext('cap:description', namespaces=_CAP_NS) or '')
            items.append({
                'id': f'ph:pagasa:{identifier}:{index}',
                'title': info.findtext('cap:headline', namespaces=_CAP_NS) or
                         info.findtext('cap:event', namespaces=_CAP_NS) or 'Weather advisory',
                'lon': round(point[0], 5), 'lat': round(point[1], 5),
                'geometry': geometry, 'locationKind': 'polygon',
                'country': 'Philippines', 'source': 'PAGASA · CC BY 4.0',
                'severity': info.findtext('cap:severity', namespaces=_CAP_NS),
                'area': '; '.join(names)[:250], 'advice': ' '.join(advice.split())[:480],
                'observed': sent, 'ends': info.findtext('cap:expires', namespaces=_CAP_NS),
                'sourceUrl': url,
            })
    return items[:100]


def _pagasa_alerts():
    # PAGASA omits its GlobalSign intermediate; keep full TLS verification.
    context = ssl.create_default_context()
    cert_dir = Path(__file__).with_name('certs')
    for name in ('globalsign-root-r46.pem', 'globalsign-gcc-r46-ov-tls-ca-2025.pem'):
        context.load_verify_locations(cafile=str(cert_dir / name))

    def fetch(url, limit):
        request = urllib.request.Request(url, headers={
            'User-Agent': 'GlobeView/1.0 (https://globeview.app)',
            'Accept': 'application/xml, application/atom+xml',
        })
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('PAGASA alert exceeded size limit')
        return ET.fromstring(body)

    feed = fetch(_PAGASA_FEED, 500_000)
    if 'CC BY 4.0' not in (feed.findtext('atom:rights', namespaces=_ATOM_NS) or ''):
        raise ValueError('PAGASA feed reuse terms changed')
    now = dt.datetime.now(_UTC)
    urls = []
    for entry in feed.findall('atom:entry', _ATOM_NS)[:100]:
        updated = entry.findtext('atom:updated', namespaces=_ATOM_NS)
        try:
            updated_at = dt.datetime.fromisoformat(updated.replace('Z', '+00:00'))
        except (AttributeError, ValueError):
            continue
        if not dt.timedelta(minutes=-5) <= now - updated_at <= dt.timedelta(days=2):
            continue
        link = next((item.get('href') for item in entry.findall('atom:link', _ATOM_NS)
                     if item.get('type') == 'application/cap+xml'), None)
        url = _pagasa_cap_url(link or '')
        if url:
            urls.append(url)
    caps, failed = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch, url, 300_000) for url in urls]
        for url, future in zip(urls, futures):
            try:
                caps.append((url, future.result()))
            except Exception:
                failed += 1
    if failed and not caps:
        raise RuntimeError('PAGASA CAP notices are unavailable')
    return _parse_pagasa_caps(caps, now)


_SACHET_FEED = 'https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml'
_SACHET_PATH = 'https://sachet.ndma.gov.in/cap_public_website/'
_SACHET_XML_CACHE = {}
_SACHET_RESULT_CACHE = {'expires': 0, 'items': []}
_SACHET_LOCK = threading.Lock()


def _sachet_xml(identifier, polygon=False):
    """Fetch official CAP XML with the ETag/304 behavior required by NDMA."""
    if not re.fullmatch(r'\d{10,20}', identifier):
        raise ValueError('Invalid SACHET identifier')
    key = (identifier, polygon)
    now = time.monotonic()
    with _SACHET_LOCK:
        cached = _SACHET_XML_CACHE.get(key)
    if cached and now - cached['checked'] < (3600 if polygon else 600):
        return ET.fromstring(cached['body'])
    endpoint = 'FetchPolygonXMLFile' if polygon else 'FetchXMLFile'
    request = urllib.request.Request(_SACHET_PATH + endpoint + '?identifier=' + identifier,
                                     headers={'User-Agent': 'GlobeView/1.0 (public alert map)',
                                              'Accept': 'application/xml',
                                              **({'If-None-Match': cached['etag']} if cached else {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            etag = response.headers.get('ETag')
            body = response.read((2_000_000 if polygon else 100_000) + 1)
    except urllib.error.HTTPError as error:
        if error.code != 304 or not cached:
            raise
        with _SACHET_LOCK:
            cached['checked'] = now
        return ET.fromstring(cached['body'])
    if not etag or len(body) > (2_000_000 if polygon else 100_000) or b'<!DOCTYPE' in body.upper():
        raise ValueError('Invalid SACHET XML response')
    root = ET.fromstring(body)
    with _SACHET_LOCK:
        _SACHET_XML_CACHE[key] = {'etag': etag, 'body': body, 'checked': now}
        while (len(_SACHET_XML_CACHE) > 300 or
               sum(len(entry['body']) for entry in _SACHET_XML_CACHE.values()) > 50_000_000):
            oldest = min(_SACHET_XML_CACHE, key=lambda item: _SACHET_XML_CACHE[item]['checked'])
            del _SACHET_XML_CACHE[oldest]
    return root


def _sachet_polygon_point(root):
    """Use the center of the largest published area as an approximate marker."""
    best = None
    for polygon in root.iter():
        if polygon.tag.rsplit('}', 1)[-1] != 'polygon':
            continue
        lons, lats = [], []
        for pair in (polygon.text or '').split():
            try:
                lat, lon = map(float, pair.split(','))
            except ValueError:
                lons = []
                break
            if not (math.isfinite(lat) and math.isfinite(lon) and 6 <= lat <= 38 and 68 <= lon <= 98):
                lons = []
                break
            lons.append(lon)
            lats.append(lat)
        if len(lons) < 3:
            continue
        west, east, south, north = min(lons), max(lons), min(lats), max(lats)
        candidate = ((east - west) * (north - south), [(west + east) / 2, (south + north) / 2])
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def _parse_sachet_caps(caps, now):
    referenced = set()
    for _, cap in caps:
        for entry in (cap.findtext('cap:references', namespaces=_CAP_NS) or '').split():
            parts = entry.split(',')
            if len(parts) >= 2:
                referenced.add(parts[1])
    candidates = []
    for rss_id, cap in caps:
        identifier = cap.findtext('cap:identifier', namespaces=_CAP_NS) or ''
        if (identifier in referenced or cap.findtext('cap:status', namespaces=_CAP_NS) != 'Actual' or
                cap.findtext('cap:scope', namespaces=_CAP_NS) != 'Public' or
                cap.findtext('cap:msgType', namespaces=_CAP_NS) not in ('Alert', 'Update')):
            continue
        infos = cap.findall('cap:info', _CAP_NS)
        info = next((value for value in infos if (value.findtext('cap:language', namespaces=_CAP_NS) or '').lower().startswith('en')), None)
        if info is None or info.findtext('cap:category', namespaces=_CAP_NS) != 'Met':
            continue
        expires = info.findtext('cap:expires', namespaces=_CAP_NS)
        if not _future_timestamp(expires, now) or info.findtext('cap:responseType', namespaces=_CAP_NS) == 'AllClear':
            continue
        polygon_url = next((value.findtext('cap:value', namespaces=_CAP_NS) for value in info.findall('cap:parameter', _CAP_NS)
                            if value.findtext('cap:valueName', namespaces=_CAP_NS) == 'Polygon URL'), None)
        if polygon_url != _SACHET_PATH + 'FetchPolygonXMLFile?identifier=' + rss_id:
            continue
        area = '; '.join(filter(None, (value.findtext('cap:areaDesc', namespaces=_CAP_NS)
                                       for value in info.findall('cap:area', _CAP_NS))))
        candidates.append({
            'id': 'in:sachet:' + identifier, 'polygonId': rss_id,
            'title': (info.findtext('cap:headline', namespaces=_CAP_NS) or
                      info.findtext('cap:event', namespaces=_CAP_NS) or 'Public alert')[:180],
            'country': 'India', 'source': 'NDMA SACHET',
            'severity': info.findtext('cap:severity', namespaces=_CAP_NS),
            'area': area[:250],
            'advice': ' '.join((info.findtext('cap:instruction', namespaces=_CAP_NS) or
                                info.findtext('cap:description', namespaces=_CAP_NS) or '').split())[:300],
            'observed': cap.findtext('cap:sent', namespaces=_CAP_NS), 'ends': expires,
            'sourceUrl': _SACHET_PATH + 'FetchXMLFile?identifier=' + rss_id,
        })
    return candidates


def _sachet_alerts():
    now = time.monotonic()
    with _SACHET_LOCK:
        if now < _SACHET_RESULT_CACHE['expires']:
            return [item for item in _SACHET_RESULT_CACHE['items']
                    if _future_timestamp(item['ends'], dt.datetime.now(_UTC))]
    feed = _get_xml(_SACHET_FEED, max_bytes=750_000)
    entries = feed.findall('./channel/item')
    if len(entries) > 250:
        raise ValueError('SACHET RSS exceeds supported size')
    identifiers = list(dict.fromkeys(entry.findtext('guid') or '' for entry in entries
                                     if re.fullmatch(r'\d{10,20}', entry.findtext('guid') or '')))
    caps, failed = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_sachet_xml, identifier): identifier for identifier in identifiers}
        for future in concurrent.futures.as_completed(futures):
            try:
                caps.append((futures[future], future.result()))
            except Exception:
                failed += 1
    if failed and not caps:
        raise RuntimeError('SACHET CAP notices are unavailable')
    candidates = _parse_sachet_caps(caps, dt.datetime.now(_UTC))
    items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_sachet_xml, item['polygonId'], True): item for item in candidates}
        for future in concurrent.futures.as_completed(futures):
            try:
                point = _sachet_polygon_point(future.result())
            except Exception:
                point = None
            if not point:
                continue
            item = futures[future].copy()
            del item['polygonId']
            item.update({'lon': round(point[0], 5), 'lat': round(point[1], 5),
                         'locationKind': 'published area representative point'})
            items.append(item)
    if candidates and not items:
        raise RuntimeError('SACHET alert areas are unavailable')
    items.sort(key=lambda item: item['observed'] or '', reverse=True)
    with _SACHET_LOCK:
        _SACHET_RESULT_CACHE.update({'items': items, 'expires': time.monotonic() + 600})
    return items


_MALAYSIA_WARNINGS_URL = 'https://api.data.gov.my/weather/warning?limit=100'
_MALAYSIA_STATE_ALIASES = {
    'Malacca': ('Malacca', 'Melaka'),
    'Negeri Sembilan': ('Negeri Sembilan', 'N. Sembilan'),
    'Penang': ('Penang', 'Pulau Pinang'),
    'Putrajaya': ('Putrajaya',),
    **{name: (name,) for name in ('Johor', 'Kedah', 'Kelantan', 'Kuala Lumpur',
                                  'Labuan', 'Pahang', 'Perak', 'Perlis', 'Sabah',
                                  'Sarawak', 'Selangor', 'Terengganu')},
}


@lru_cache(maxsize=1)
def _malaysia_state_points():
    path = Path(__file__).with_name('malaysia-state-points.json')
    return json.loads(path.read_text(encoding='utf-8'))['states']


def _malaysia_local_time(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo('Asia/Kuala_Lumpur'))
        return parsed.astimezone(_UTC)
    except (TypeError, ValueError):
        return None


def _malaysia_alerts(now=None):
    records = _get_json(_MALAYSIA_WARNINGS_URL, max_bytes=1_000_000)
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError('Unexpected MET Malaysia warning feed')
    states = _malaysia_state_points()
    now = now or dt.datetime.now(_UTC)
    candidates = []
    for record in records:
        if not isinstance(record, dict):
            continue
        issue = record.get('warning_issue') or {}
        if not isinstance(issue, dict):
            continue
        issued = _malaysia_local_time(issue.get('issued'))
        starts = _malaysia_local_time(record.get('valid_from'))
        ends = _malaysia_local_time(record.get('valid_to'))
        if not issued or not starts or not ends or starts > now or ends <= now:
            continue
        title = str(issue.get('title_en') or record.get('heading_en') or '').strip()[:150]
        body = str(record.get('text_en') or '')[:20_000]
        if not title or title.lower() == 'no advisory' or not body:
            continue
        # The API gives prose, not geometry. Locate only explicitly named land
        # states; sea forecasts must never look like a warning on land.
        land_sections = []
        for match in re.finditer(r'\bover the states? of\s+', body, re.I):
            section = body[match.end():match.end() + 1600]
            land_sections.append(re.split(r'\buntil\b|\bwithin the period\b', section, maxsplit=1,
                                            flags=re.I)[0])
        if not land_sections:
            continue
        for state, aliases in _MALAYSIA_STATE_ALIASES.items():
            if state not in states or not any(re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', section, re.I)
                                              for section in land_sections for alias in aliases):
                continue
            candidates.append((issued, title, state, ends, record))
    candidates.sort(key=lambda row: row[0], reverse=True)
    items = []
    seen = set()
    for issued, title, state, ends, record in candidates:
        key = (title.casefold(), state)
        if key in seen:
            continue
        seen.add(key)
        lon, lat = states[state]
        if not _point((lon, lat)) or not (99 <= lon <= 120 and 0 <= lat <= 8):
            continue
        instruction = ' '.join(str(record.get('instruction_en') or '').split())[:250]
        items.append({
            'id': f'my:met:{issued:%Y%m%d%H%M}:{re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")}:{state.lower().replace(" ", "-")}',
            'title': title, 'country': 'Malaysia', 'source': 'MET Malaysia · state point: geoBoundaries / OpenStreetMap',
            'lon': lon, 'lat': lat, 'area': f'{state} · approximate state point; warning may name specific districts',
            'locationKind': 'published area representative point', 'severity': 'Unknown', 'advice': instruction,
            'observed': issued.isoformat(), 'ends': ends.isoformat(),
            'sourceUrl': 'https://api.data.gov.my/weather/warning',
        })
    return items


_KAZAKHSTAN_CAP_FEED = 'https://meteoalert.meteoinfo.ru/kazakhstan/cap-feed/en/atom.xml'
_KAZAKHSTAN_CAP_URL = re.compile(
    r'https://meteoalert\.meteoinfo\.ru/kazakhstan/cap-feed/en/'
    r'2\.49\.0\.0\.398\.0-[A-Za-z0-9-]{1,100}\.xml')
_KAZAKHSTAN_LOCK = threading.Lock()
_KAZAKHSTAN_STATE = {'caps': None, 'fetched_at': 0, 'refresh_after': 0, 'inflight': False}


@lru_cache(maxsize=2048)
def _kazakhstan_cap_alert(url):
    if not _KAZAKHSTAN_CAP_URL.fullmatch(url):
        raise ValueError('Unexpected Kazakhstan CAP URL')
    return _get_xml(url, max_bytes=150_000)


def _parse_meteoalert_caps(caps, now, *, country, source, country_code, identifier_prefix,
                           bounds, max_age=dt.timedelta(days=2)):
    referenced = set()
    for _, cap in caps:
        for reference in (cap.findtext('cap:references', namespaces=_CAP_NS) or '').split():
            parts = reference.split(',')
            if len(parts) == 3 and cap.findtext('cap:status', namespaces=_CAP_NS) == 'Actual':
                referenced.add(parts[1])
    items, seen = [], set()
    for url, cap in caps:
        identifier = cap.findtext('cap:identifier', namespaces=_CAP_NS) or ''
        if (not identifier.startswith(identifier_prefix) or len(identifier) > 160
                or identifier in referenced or identifier in seen
                or cap.findtext('cap:status', namespaces=_CAP_NS) != 'Actual'
                or cap.findtext('cap:scope', namespaces=_CAP_NS) != 'Public'
                or cap.findtext('cap:msgType', namespaces=_CAP_NS) not in ('Alert', 'Update')):
            continue
        seen.add(identifier)
        sent = cap.findtext('cap:sent', namespaces=_CAP_NS)
        try:
            issued = dt.datetime.fromisoformat(sent.replace('Z', '+00:00'))
            if not dt.timedelta(minutes=-5) <= now - issued <= max_age:
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        for index, info in enumerate(cap.findall('cap:info', _CAP_NS)):
            if (not (info.findtext('cap:language', namespaces=_CAP_NS) or '').lower().startswith('en')
                    or not _future_timestamp(info.findtext('cap:expires', namespaces=_CAP_NS), now)
                    or info.findtext('cap:urgency', namespaces=_CAP_NS) == 'Past'
                    or info.findtext('cap:responseType', namespaces=_CAP_NS) == 'AllClear'):
                continue
            polygons, names = [], []
            for area in info.findall('cap:area', _CAP_NS):
                name = area.findtext('cap:areaDesc', namespaces=_CAP_NS)
                if name:
                    names.append(name)
                for polygon in area.findall('cap:polygon', _CAP_NS):
                    ring = _cap_polygon(polygon.text)
                    if ring and all(bounds[0] <= lon <= bounds[2] and bounds[1] <= lat <= bounds[3]
                                    for lon, lat in ring):
                        polygons.append([ring])
            if not polygons or len(polygons) > 50 or sum(len(p[0]) for p in polygons) > 20_000:
                continue
            geometry = {'type': 'MultiPolygon', 'coordinates': polygons}
            point = _polygon_point(geometry)
            if not point:
                continue
            advice = (info.findtext('cap:instruction', namespaces=_CAP_NS) or
                      info.findtext('cap:description', namespaces=_CAP_NS) or '')
            items.append({
                'id': f'{country_code}:{identifier}:{index}',
                'title': info.findtext('cap:headline', namespaces=_CAP_NS) or
                         info.findtext('cap:event', namespaces=_CAP_NS) or 'Weather warning',
                'lon': round(point[0], 5), 'lat': round(point[1], 5), 'geometry': geometry,
                'locationKind': 'polygon', 'country': country, 'source': source,
                'severity': info.findtext('cap:severity', namespaces=_CAP_NS),
                'area': '; '.join(names)[:250], 'advice': ' '.join(advice.split())[:480],
                'observed': sent, 'ends': info.findtext('cap:expires', namespaces=_CAP_NS),
                'sourceUrl': url,
            })
    return items


def _parse_kazakhstan_caps(caps, now):
    return _parse_meteoalert_caps(caps, now, country='Kazakhstan', source='Kazhydromet',
                                 country_code='kz', identifier_prefix='2.49.0.0.398.0-',
                                 bounds=(45, 40, 88, 56))


def _load_meteoalert_caps(feed_url, url_pattern, fetch_cap, country):
    feed = _get_xml(feed_url, max_bytes=1_000_000)
    if (feed.findtext('atom:rights', namespaces=_ATOM_NS) or '').strip().lower() != 'public domain':
        raise ValueError(f'{country} CAP feed reuse terms changed')
    entries = feed.findall('atom:entry', _ATOM_NS)
    if len(entries) > 1000:
        raise ValueError(f'{country} CAP feed exceeded entry limit')
    now = dt.datetime.now(_UTC)
    urls = set()
    for entry in entries:
        try:
            updated = dt.datetime.fromisoformat(entry.findtext('atom:updated', namespaces=_ATOM_NS).replace('Z', '+00:00'))
            if not dt.timedelta(minutes=-5) <= now - updated <= dt.timedelta(days=2):
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        for link in entry.findall('atom:link', _ATOM_NS):
            url = link.get('href') or ''
            if link.get('type') == 'application/cap+xml' and url_pattern.fullmatch(url):
                urls.add(url)
                break
    # Immutable CAP documents are cached once across users and feed refreshes.
    # Bounded concurrency keeps the first national catalog fetch considerate.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        caps = list(zip(sorted(urls), executor.map(fetch_cap, sorted(urls))))
    return caps


def _load_kazakhstan_caps():
    return _load_meteoalert_caps(_KAZAKHSTAN_CAP_FEED, _KAZAKHSTAN_CAP_URL,
                                 _kazakhstan_cap_alert, 'Kazakhstan')


def _refresh_kazakhstan_caps():
    try:
        caps = _load_kazakhstan_caps()
    except Exception:
        with _KAZAKHSTAN_LOCK:
            _KAZAKHSTAN_STATE.update(refresh_after=time.monotonic() + 60, inflight=False)
    else:
        with _KAZAKHSTAN_LOCK:
            fetched_at = time.monotonic()
            _KAZAKHSTAN_STATE.update(caps=caps, fetched_at=fetched_at,
                                     refresh_after=fetched_at + 300, inflight=False)


def _kazakhstan_alerts():
    # A first download can contain hundreds of immutable CAP documents. Keep it
    # off the request path so all other countries remain responsive while warming.
    with _KAZAKHSTAN_LOCK:
        if (time.monotonic() >= _KAZAKHSTAN_STATE['refresh_after']
                and not _KAZAKHSTAN_STATE['inflight']):
            _KAZAKHSTAN_STATE['inflight'] = True
            threading.Thread(target=_refresh_kazakhstan_caps,
                             name='kazakhstan-cap-refresh', daemon=True).start()
        caps = _KAZAKHSTAN_STATE['caps']
        fetched_at = _KAZAKHSTAN_STATE['fetched_at']
    if caps is None or time.monotonic() - fetched_at > 900:
        raise RuntimeError('Kazakhstan warning catalog is warming or unavailable')
    return _parse_kazakhstan_caps(caps, dt.datetime.now(_UTC))


# WMO authority 2.49.0.0.417.0 publishes these public-domain English CAP alerts.
# Filenames differ from Kazakhstan; keep each national URL/identifier restricted.
_KYRGYZSTAN_CAP_FEED = 'https://meteoalert.meteoinfo.ru/kyrgyzstan/cap-feed/en/atom.xml'
_KYRGYZSTAN_CAP_URL = re.compile(
    r'https://meteoalert\.meteoinfo\.ru/kyrgyzstan/cap-feed/en/\d{14}-\d{7}\.xml')
_KYRGYZSTAN_LOCK = threading.Lock()
_KYRGYZSTAN_STATE = {'caps': None, 'fetched_at': 0, 'refresh_after': 0, 'inflight': False}


@lru_cache(maxsize=1024)
def _kyrgyzstan_cap_alert(url):
    if not _KYRGYZSTAN_CAP_URL.fullmatch(url):
        raise ValueError('Unexpected Kyrgyzstan CAP URL')
    return _get_xml(url, max_bytes=150_000)


def _parse_kyrgyzstan_caps(caps, now):
    # Published 72-hour outlooks can still be valid more than two days after issue.
    return _parse_meteoalert_caps(caps, now, country='Kyrgyzstan', source='Kyrgyzhydromet',
                                 country_code='kg', identifier_prefix='2.49.0.0.417.0.',
                                 bounds=(69, 39, 81, 44), max_age=dt.timedelta(days=7))


def _load_kyrgyzstan_caps():
    return _load_meteoalert_caps(_KYRGYZSTAN_CAP_FEED, _KYRGYZSTAN_CAP_URL,
                                 _kyrgyzstan_cap_alert, 'Kyrgyzstan')


def _refresh_kyrgyzstan_caps():
    try:
        caps = _load_kyrgyzstan_caps()
    except Exception:
        with _KYRGYZSTAN_LOCK:
            _KYRGYZSTAN_STATE.update(refresh_after=time.monotonic() + 60, inflight=False)
    else:
        with _KYRGYZSTAN_LOCK:
            fetched_at = time.monotonic()
            _KYRGYZSTAN_STATE.update(caps=caps, fetched_at=fetched_at,
                                    refresh_after=fetched_at + 300, inflight=False)


def _kyrgyzstan_alerts():
    with _KYRGYZSTAN_LOCK:
        if (time.monotonic() >= _KYRGYZSTAN_STATE['refresh_after']
                and not _KYRGYZSTAN_STATE['inflight']):
            _KYRGYZSTAN_STATE['inflight'] = True
            threading.Thread(target=_refresh_kyrgyzstan_caps,
                             name='kyrgyzstan-cap-refresh', daemon=True).start()
        caps = _KYRGYZSTAN_STATE['caps']
        fetched_at = _KYRGYZSTAN_STATE['fetched_at']
    if caps is None or time.monotonic() - fetched_at > 900:
        raise RuntimeError('Kyrgyzstan warning catalog is warming or unavailable')
    return _parse_kyrgyzstan_caps(caps, dt.datetime.now(_UTC))


def _world_alerts():
    items = []
    unavailable = []
    loaders = [('Canada', _canada_alerts), ('New Zealand', _new_zealand_alerts),
               ('Norway', _norway_alerts), ('Ireland', _ireland_alerts),
               ('Germany', _germany_alerts), ('Portugal · Azores', _azores_alerts),
               ('Philippines', _pagasa_alerts), ('India', _sachet_alerts),
               ('Sri Lanka', _sri_lanka_alerts), ('Maldives', _maldives_alerts),
               ('Malaysia', _malaysia_alerts), ('Kazakhstan', _kazakhstan_alerts),
               ('Kyrgyzstan', _kyrgyzstan_alerts), ('Tajikistan', _tajikistan_alerts)]
    # Each provider is independent; a slow national service should not delay
    # every other country's current alerts.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(loaders)) as executor:
        futures = [executor.submit(loader) for _, loader in loaders]
        for (country, _), future in zip(loaders, futures):
            try:
                items.extend(future.result())
            except Exception:
                unavailable.append(country)
    if len(unavailable) == len(loaders):
        raise RuntimeError('International weather alert feeds are unavailable')
    return {'source': 'National weather and disaster alert services', 'items': items,
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


def _current_weather_body(layer, body):
    """Drop alerts that expire while their shared snapshot is cached."""
    if layer not in ('world_alerts', 'nws_alerts'):
        return body
    snapshot = json.loads(body)
    now = dt.datetime.now(_UTC)
    items = snapshot['items']
    active = [item for item in items
              if not item.get('ends') or _future_timestamp(item['ends'], now)]
    if len(active) == len(items):
        return body
    snapshot['items'] = active
    return json.dumps(snapshot, separators=(',', ':')).encode()


def hazard_snapshot(layer):
    if layer not in FEEDS:
        raise ValueError('Unknown hazard layer')
    config = FEEDS[layer]
    while True:
        with _LOCK:
            now = time.monotonic()
            cached = _CACHE.get(layer)
            if cached and now < cached['expires']:
                return _current_weather_body(layer, cached['body'])
            if now < _RETRY_AFTER.get(layer, 0):
                if cached and now < cached['stale']:
                    return _current_weather_body(layer, cached['body'])
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
                return _current_weather_body(layer, cached['body'])
        raise
    else:
        now = time.monotonic()
        with _LOCK:
            _CACHE[layer] = {'body': body, 'expires': now + config['ttl'], 'stale': now + config['stale']}
            _RETRY_AFTER.pop(layer, None)
        return _current_weather_body(layer, body)
    finally:
        with _LOCK:
            _INFLIGHT.pop(layer, None)
            event.set()
