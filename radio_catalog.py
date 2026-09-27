"""Small, persistent Radio Browser catalog for the globe's optional radio layer."""

import json
import ipaddress
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(os.getenv('AMERICAMAP_CACHE_DIR', os.path.join(BASE_DIR, '.cache')), 'radio-stations.json')
API_HOSTS = ('https://all.api.radio-browser.info', 'https://de1.api.radio-browser.info')
USER_AGENT = 'GlobalMap/1.0 (radio directory; https://www.radio-browser.info/)'
CATALOG_TTL = 12 * 3600
UUID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_lock = threading.RLock()
_catalog = None


def normalize_stations(rows):
    stations = []
    seen_ids = set()
    seen_names = set()
    for row in rows:
        station_id = str(row.get('stationuuid') or '').lower()
        name = ' '.join(str(row.get('name') or '').split())[:140]
        stream = str(row.get('url_resolved') or row.get('url') or '').strip()
        if not UUID_PATTERN.fullmatch(station_id) or not name or station_id in seen_ids:
            continue
        try:
            parsed_stream = urllib.parse.urlsplit(stream)
            host = parsed_stream.hostname or ''
        except ValueError:
            continue
        try:
            address = ipaddress.ip_address(host)
            private_host = not address.is_global
        except ValueError:
            private_host = host in {'localhost', 'localhost.localdomain'} or host.endswith(('.local', '.localhost', '.internal'))
        if parsed_stream.scheme != 'https' or not host or parsed_stream.username or private_host or row.get('hls') in (1, '1', True):
            continue
        if parsed_stream.path.lower().endswith(('.m3u', '.m3u8', '.pls', '.asx')):
            continue
        if row.get('lastcheckok') in (0, '0', False):
            continue
        try:
            lat, lon = float(row['geo_lat']), float(row['geo_long'])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        country_code = str(row.get('countrycode') or '').upper()[:2]
        # Radio Browser has duplicate UUIDs and duplicate station listings.
        name_key = (name.casefold(), country_code, round(lat, 2), round(lon, 2))
        if name_key in seen_names:
            continue
        seen_ids.add(station_id)
        seen_names.add(name_key)
        homepage = str(row.get('homepage') or '').strip()
        try:
            bitrate = int(row.get('bitrate') or 0)
            click_count = int(row.get('clickcount') or 0)
        except (TypeError, ValueError):
            bitrate, click_count = 0, 0
        stations.append({
            'id': station_id, 'name': name, 'lat': lat, 'lon': lon,
            'country': str(row.get('country') or '')[:80], 'countryCode': country_code,
            'state': str(row.get('state') or '')[:80], 'streamUrl': stream,
            'homepage': homepage if homepage.startswith('https://') else '',
            'codec': str(row.get('codec') or '')[:24],
            'bitrate': bitrate,
            'clickCount': click_count,
            'lastSuccessfulCheck': str(row.get('lastcheckoktime_iso8601') or '')[:32],
            'source': 'Radio Browser',
        })
    return stations


def _get_json(url, timeout=25):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError('Radio directory response is too large')
    return json.loads(raw)


def _load_cache():
    try:
        with open(CACHE_PATH, encoding='utf-8') as handle:
            cached = json.load(handle)
        if isinstance(cached.get('stations'), list) and cached.get('updatedAt'):
            return cached
    except (OSError, ValueError, TypeError):
        pass
    return None


def catalog_snapshot():
    global _catalog
    with _lock:
        if _catalog is None:
            _catalog = _load_cache()
        if _catalog and time.time() - _catalog['updatedAt'] < CATALOG_TTL:
            return {**_catalog, 'stale': False}
        error = None
        query = urllib.parse.urlencode({
            'has_geo_info': 'true', 'hidebroken': 'true', 'order': 'clickcount',
            'reverse': 'true', 'limit': 8000,
        })
        for host in API_HOSTS:
            try:
                rows = _get_json(f'{host}/json/stations/search?{query}')
                stations = normalize_stations(rows)
                if len(stations) < 100:
                    raise ValueError('Radio directory returned too few usable stations')
                _catalog = {'updatedAt': time.time(), 'stations': stations, 'source': 'Radio Browser'}
                os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
                temporary = f'{CACHE_PATH}.tmp'
                with open(temporary, 'w', encoding='utf-8') as handle:
                    json.dump(_catalog, handle, separators=(',', ':'), ensure_ascii=False)
                os.replace(temporary, CACHE_PATH)
                return {**_catalog, 'stale': False}
            except Exception as exc:
                error = exc
        if _catalog:
            return {**_catalog, 'stale': True}
        raise RuntimeError('Radio directory unavailable') from error


def record_station_click(station_id):
    if not UUID_PATTERN.fullmatch(station_id or ''):
        raise ValueError('Invalid station ID')
    for host in API_HOSTS:
        try:
            result = _get_json(f'{host}/json/url/{station_id}', timeout=8)
            return bool(result.get('ok'))
        except Exception:
            continue
    return False
