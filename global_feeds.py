"""Worldwide aircraft snapshots and a viewport-scoped AISStream relay."""
import json
import math
import os
import random
import re
import threading
import time
import unicodedata
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

AIRCRAFT_LOCK = threading.Lock()
AIRCRAFT_CACHE = {}
AIRCRAFT_INFLIGHT = {}
AIRCRAFT_MAX_BYTES = 8_000_000
AIRCRAFT_PROVIDER_BACKOFF = {}
AIRCRAFT_ROUTE_CACHE = {}
AIRCRAFT_TRACK_CACHE = {}
AIRCRAFT_FALLBACK_CACHE = {}
AIRCRAFT_DAILY_REQUESTS = {}


class AircraftRateLimited(Exception):
    def __init__(self, retry_after=60):
        self.retry_after = retry_after
        super().__init__('Aircraft provider rate limited')
AIS_LOCK = threading.Lock()
AIS_STATE = {'thread': None, 'boxes': None, 'viewers': {}, 'status': 'needs_key',
             'updated_at': 0, 'vessels': {}}
AIS_VIEWER_TTL = 60
AIS_MAX_BOXES = max(1, min(64, int(os.getenv('AISSTREAM_MAX_BOXES', '32'))))
AIS_MAX_VIEWERS = max(32, min(5000, int(os.getenv('AISSTREAM_MAX_VIEWERS', '512'))))
AIS_MAX_VESSELS = 12000
AIS_PRUNE_THRESHOLD = 15000
AIS_KEY_LOCK = threading.Lock()
AIS_KEY_CACHE = {'mtime_ns': None, 'value': ''}
AIS_POSITION_TYPES = {'PositionReport', 'StandardClassBPositionReport', 'ExtendedClassBPositionReport', 'LongRangeAisBroadcastMessage'}
AIS_MESSAGE_TYPES = sorted(AIS_POSITION_TYPES | {'ShipStaticData', 'StaticDataReport'})


def _json_get(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    backoff_key = f'{host}/tracks' if host == 'opensky-network.org' and '/tracks/' in parsed.path else host
    with AIRCRAFT_LOCK:
        remaining = AIRCRAFT_PROVIDER_BACKOFF.get(backoff_key, 0) - time.time()
    if remaining > 0:
        raise AircraftRateLimited(max(1, int(remaining)))
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobalMap/1.0', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=22) as response:
            data = response.read(AIRCRAFT_MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code not in (420, 429, 502, 503, 504):
            raise
        value = error.headers.get('Retry-After') or error.headers.get('X-Rate-Limit-Retry-After-Seconds') or ''
        retry_after = min(3600, max(60, int(value))) if str(value).isdigit() else 60
        with AIRCRAFT_LOCK:
            AIRCRAFT_PROVIDER_BACKOFF[backoff_key] = max(AIRCRAFT_PROVIDER_BACKOFF.get(backoff_key, 0), time.time() + retry_after)
        raise AircraftRateLimited(retry_after) from error
    if len(data) > AIRCRAFT_MAX_BYTES:
        raise ValueError('Aircraft response exceeded size limit')
    return json.loads(data)


def _opensky_aircraft(icao24=None):
    url = 'https://opensky-network.org/api/states/all'
    if icao24:
        url += '?icao24=' + urllib.parse.quote(icao24)
    data = _json_get(url)
    result = []
    for state in data.get('states') or []:
        if len(state) < 11 or state[8] or state[5] is None or state[6] is None:
            continue
        barometric_altitude = state[7]
        geometric_altitude = state[13] if len(state) > 13 else None
        result.append({
            'hex': state[0], 'flight': (state[1] or '').strip(),
            'lon': state[5], 'lat': state[6],
            'alt_baro': round((barometric_altitude if barometric_altitude is not None else geometric_altitude) * 3.28084)
            if barometric_altitude is not None or geometric_altitude is not None else None,
            'alt_geom': round(geometric_altitude * 3.28084) if geometric_altitude is not None else None,
            'gs': round(state[9] * 1.94384) if state[9] is not None else None,
            'track': state[10], 'seen': state[4],
        })
    return result


def _daily_aircraft_budget(kind, limit):
    """Reserve one upstream request from a shared daily budget."""
    day = int(time.time() // 86400)
    with AIRCRAFT_LOCK:
        used_day, count = AIRCRAFT_DAILY_REQUESTS.get(kind, (day, 0))
        if used_day != day:
            count = 0
        if count >= limit:
            raise AircraftRateLimited(max(60, (day + 1) * 86400 - int(time.time())))
        AIRCRAFT_DAILY_REQUESTS[kind] = (day, count + 1)


def _opensky_hex_fallback(identifier):
    now = time.time()
    with AIRCRAFT_LOCK:
        cached = AIRCRAFT_FALLBACK_CACHE.get(identifier)
        if cached and now < cached[0]:
            return cached[1]
    # The anonymous states bucket is shared with the worldwide overview.
    _daily_aircraft_budget('opensky_hex', 144)
    items = _opensky_aircraft(identifier.lower())
    body = {'aircraft': items, 'source': 'OpenSky fallback', 'scope': 'hex',
            'updated_at': int(now), 'overview_refresh_seconds': 600}
    with AIRCRAFT_LOCK:
        AIRCRAFT_FALLBACK_CACHE[identifier] = (now + (600 if items else 180), body)
        if len(AIRCRAFT_FALLBACK_CACHE) > 256:
            AIRCRAFT_FALLBACK_CACHE.pop(next(iter(AIRCRAFT_FALLBACK_CACHE)))
    return body


def _world_aircraft():
    aircraft = {str(item['hex']).lower(): item for item in _opensky_aircraft() if item.get('hex')}
    military_count = 0
    try:
        data = _json_get('https://api.adsb.lol/v2/mil')
        for item in data.get('ac') or []:
            if item.get('hex') and item.get('lat') is not None and item.get('lon') is not None:
                item['dbFlags'] = int(item.get('dbFlags') or 0) | 1
                aircraft[str(item['hex']).lower()] = item
                military_count += 1
    except Exception:
        # OpenSky still provides the worldwide civil layer if this supplement fails.
        pass
    return {'aircraft': list(aircraft.values()), 'source': 'OpenSky / ADSB.lol',
            'scope': 'world', 'updated_at': int(time.time()), 'military_count': military_count,
            'overview_refresh_seconds': 1800}


def _local_aircraft(lat, lon, dist):
    data = _json_get(f'https://api.adsb.lol/v2/lat/{lat:.2f}/lon/{lon:.2f}/dist/{dist}')
    items = [item for item in (data.get('ac') or []) if item.get('lat') is not None and item.get('lon') is not None]
    return {'aircraft': items, 'source': 'ADSB.lol', 'scope': 'local',
            'updated_at': int(time.time()), 'overview_refresh_seconds': 45}


def _specific_aircraft(scope, identifier):
    try:
        data = _json_get(f'https://api.adsb.lol/v2/{scope}/{identifier}')
    except Exception:
        if scope == 'hex':
            return _opensky_hex_fallback(identifier)
        raise
    items = [item for item in (data.get('ac') or [])
             if item.get('lat') is not None and item.get('lon') is not None]
    if scope == 'hex' and not items:
        return _opensky_hex_fallback(identifier)
    return {'aircraft': items, 'source': 'ADSB.lol', 'scope': scope,
            'updated_at': int(time.time()), 'overview_refresh_seconds': 20}


def _stale_aircraft_body(body):
    data = json.loads(body)
    data['stale'] = True
    return json.dumps(data, separators=(',', ':')).encode()


def _cached_world_viewport(lat, lon, dist, now):
    with AIRCRAFT_LOCK:
        entry = AIRCRAFT_CACHE.get(('world',))
        if not entry or now >= entry['stale']:
            return None
        world = json.loads(entry['body'])
    radius = dist * 1.852
    rows = []
    for item in world.get('aircraft') or []:
        try:
            item_lat, item_lon = float(item['lat']), float(item['lon'])
            arc = math.sin(math.radians(lat)) * math.sin(math.radians(item_lat)) + math.cos(math.radians(lat)) * math.cos(math.radians(item_lat)) * math.cos(math.radians(item_lon - lon))
            if 6371 * math.acos(max(-1, min(1, arc))) <= radius:
                rows.append(item)
        except (KeyError, TypeError, ValueError):
            continue
    return json.dumps({'aircraft': rows, 'source': 'OpenSky cached overview', 'scope': 'local',
                       'updated_at': world.get('updated_at'), 'stale': True}, separators=(',', ':')).encode()


def aircraft_snapshot(scope='world', lat=None, lon=None, dist=None, identifier=None):
    if scope == 'local':
        lat, lon, dist = float(lat), float(lon), int(dist)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 100 <= dist <= 2000):
            raise ValueError('Invalid aircraft viewport')
        lat = round(lat * 4) / 4
        lon = round(lon * 4) / 4
        dist = min(2000, max(100, int(math.ceil(dist / 100) * 100)))
        key, ttl, stale = ('local', lat, lon, dist), 45, 900
        loader = lambda: _local_aircraft(lat, lon, dist)
    elif scope in ('hex', 'callsign', 'registration'):
        identifier = str(identifier or '').strip().upper()
        pattern = r'[0-9A-F]{6}' if scope == 'hex' else r'[A-Z0-9-]{2,12}'
        if not re.fullmatch(pattern, identifier):
            raise ValueError('Invalid aircraft identifier')
        key, ttl, stale = (scope, identifier), 15, 600
        loader = lambda: _specific_aircraft(scope, identifier)
    elif scope == 'world':
        key, ttl, stale = ('world',), 1800, 7200
        loader = _world_aircraft
    else:
        raise ValueError('Invalid aircraft scope')
    now = time.time()
    with AIRCRAFT_LOCK:
        entry = AIRCRAFT_CACHE.get(key)
        if entry and now < entry['expires']:
            return entry['body']
        pending = AIRCRAFT_INFLIGHT.get(key)
        if not pending:
            pending = threading.Event()
            AIRCRAFT_INFLIGHT[key] = pending
            fetch_here = True
        else:
            fetch_here = False
    if not fetch_here:
        pending.wait(25)
        with AIRCRAFT_LOCK:
            entry = AIRCRAFT_CACHE.get(key)
            if entry and time.time() < entry['stale']:
                return _stale_aircraft_body(entry['body'])
        raise RuntimeError('Aircraft refresh unavailable')
    try:
        body = json.dumps(loader(), separators=(',', ':')).encode()
    except Exception:
        with AIRCRAFT_LOCK:
            entry = AIRCRAFT_CACHE.get(key)
            if entry and now < entry['stale']:
                return _stale_aircraft_body(entry['body'])
        if scope == 'local':
            fallback = _cached_world_viewport(lat, lon, dist, now)
            if not fallback:
                try:
                    # A local view can be the first request after startup. Populate the
                    # shared world cache once so ADSB.lol outages have a fallback.
                    aircraft_snapshot('world')
                    fallback = _cached_world_viewport(lat, lon, dist, time.time())
                except Exception:
                    pass
            if fallback:
                with AIRCRAFT_LOCK:
                    AIRCRAFT_CACHE[key] = {'body': fallback, 'expires': time.time() + ttl,
                                           'stale': time.time() + stale}
                return fallback
        raise
    else:
        with AIRCRAFT_LOCK:
            AIRCRAFT_CACHE[key] = {'body': body, 'expires': now + ttl, 'stale': now + stale}
            if len(AIRCRAFT_CACHE) > 80:
                oldest_local = next((candidate for candidate in AIRCRAFT_CACHE if candidate != ('world',)), None)
                if oldest_local:
                    AIRCRAFT_CACHE.pop(oldest_local)
        return body
    finally:
        with AIRCRAFT_LOCK:
            AIRCRAFT_INFLIGHT.pop(key, None)
            pending.set()


def aircraft_track(identifier):
    """Return the current flown track from OpenSky, cached per transponder."""
    identifier = str(identifier or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{6}', identifier):
        raise ValueError('Invalid aircraft identifier')
    key = ('track', identifier)
    now = time.time()
    with AIRCRAFT_LOCK:
        cached = AIRCRAFT_TRACK_CACHE.get(identifier)
        if cached and now < cached[0]:
            return cached[1]
        pending = AIRCRAFT_INFLIGHT.get(key)
        if not pending:
            pending = threading.Event()
            AIRCRAFT_INFLIGHT[key] = pending
            fetch_here = True
        else:
            fetch_here = False
    if not fetch_here:
        pending.wait(25)
        with AIRCRAFT_LOCK:
            cached = AIRCRAFT_TRACK_CACHE.get(identifier)
            if cached and time.time() < cached[0]:
                return cached[1]
        raise RuntimeError('Aircraft track refresh unavailable')
    try:
        # /tracks/* has a separate OpenSky quota from /states/* (4 credits per live track).
        _daily_aircraft_budget('opensky_tracks', 75)
        try:
            data = _json_get(f'https://opensky-network.org/api/tracks/all?icao24={identifier}&time=0')
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            data = {}
        points = []
        for row in (data.get('path') or [])[-3000:]:
            if not isinstance(row, list) or len(row) < 3:
                continue
            try:
                timestamp, lat, lon = int(row[0]), float(row[1]), float(row[2])
            except (TypeError, ValueError):
                continue
            if -90 <= lat <= 90 and -180 <= lon <= 180 and timestamp > 0:
                points.append([lon, lat, timestamp])
        points.sort(key=lambda row: row[2])
        body = json.dumps({'status': 'available' if len(points) > 1 else 'unavailable',
                           'hex': identifier, 'points': points, 'source': 'OpenSky',
                           'updated_at': int(time.time())}, separators=(',', ':')).encode()
        with AIRCRAFT_LOCK:
            AIRCRAFT_TRACK_CACHE[identifier] = (time.time() + (600 if len(points) > 1 else 120), body)
            if len(AIRCRAFT_TRACK_CACHE) > 256:
                AIRCRAFT_TRACK_CACHE.pop(next(iter(AIRCRAFT_TRACK_CACHE)))
        return body
    finally:
        with AIRCRAFT_LOCK:
            AIRCRAFT_INFLIGHT.pop(key, None)
            pending.set()


def aircraft_route(callsign, lat, lon):
    """Look up a plausible airport pair for one selected flight, never a filed plan."""
    callsign = str(callsign or '').strip().upper()
    lat, lon = float(lat), float(lon)
    if not re.fullmatch(r'[A-Z0-9]{3,10}', callsign) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('Invalid route lookup')
    # A callsign can be reused on a later flight. Keep both hits and misses short lived.
    key = (callsign, round(lat), round(lon))
    now = time.time()
    with AIRCRAFT_LOCK:
        cached = AIRCRAFT_ROUTE_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
    request = urllib.request.Request(
        'https://adsb.im/api/0/routeset',
        data=json.dumps({'planes': [{'callsign': callsign, 'lat': lat, 'lng': lon}]}).encode(),
        headers={'User-Agent': 'GlobalMap/1.0', 'Accept': 'application/json',
                 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(100_001)
    if len(raw) > 100_000:
        raise ValueError('Route response exceeded size limit')
    rows = json.loads(raw)
    result = {'route': None, 'source': 'ADSB.im', 'kind': 'plausible callsign route'}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get('callsign') != callsign or row.get('plausible') is not True:
            continue
        airports = row.get('_airports') or []
        if len(airports) < 2:
            continue
        endpoints = []
        for airport in (airports[0], airports[-1]):
            try:
                airport_lat, airport_lon = float(airport['lat']), float(airport['lon'])
            except (TypeError, ValueError, KeyError):
                break
            if not (-90 <= airport_lat <= 90 and -180 <= airport_lon <= 180):
                break
            endpoints.append({'lat': airport_lat, 'lon': airport_lon,
                              'code': str(airport.get('iata') or airport.get('icao') or '')[:6],
                              'name': str(airport.get('name') or '')[:100]})
        if len(endpoints) == 2:
            result['route'] = {'origin': endpoints[0], 'destination': endpoints[1]}
            break
    with AIRCRAFT_LOCK:
        AIRCRAFT_ROUTE_CACHE[key] = (now + (600 if result['route'] else 180), result)
        if len(AIRCRAFT_ROUTE_CACHE) > 512:
            AIRCRAFT_ROUTE_CACHE.pop(next(iter(AIRCRAFT_ROUTE_CACHE)))
    return result


def _ais_key():
    key = os.getenv('AISSTREAM_API_KEY', '').strip()
    if key:
        return key
    path = Path(__file__).with_name('.env')
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        mtime_ns = None
    with AIS_KEY_LOCK:
        if AIS_KEY_CACHE['mtime_ns'] == mtime_ns:
            return AIS_KEY_CACHE['value']
        value = ''
        if mtime_ns is not None:
            for line in path.read_text().splitlines():
                if line.strip().startswith('AISSTREAM_API_KEY='):
                    value = line.split('=', 1)[1].strip().strip('"\'')
                    break
        AIS_KEY_CACHE.update(mtime_ns=mtime_ns, value=value)
        return value


def _vessel_message(message):
    kind = message.get('MessageType')
    meta = message.get('MetaData') or message.get('Metadata') or {}
    report = (message.get('Message') or {}).get(kind) or {}
    mmsi = str(meta.get('MMSI') or report.get('UserID') or report.get('UserId') or '')
    if not (mmsi.isdigit() and len(mmsi) == 9):
        return
    name = str(meta.get('ShipName') or report.get('Name') or '').strip()[:80]
    with AIS_LOCK:
        previous = AIS_STATE['vessels'].get(mmsi, {})
        if kind in AIS_POSITION_TYPES:
            if report.get('Valid') is False:
                return
            try:
                lat = float(meta.get('latitude', meta.get('Latitude', report.get('Latitude'))))
                lon = float(meta.get('longitude', meta.get('Longitude', report.get('Longitude'))))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    return
            except (ValueError, TypeError):
                return
            heading = report.get('TrueHeading')
            if not isinstance(heading, (int, float)) or heading >= 360:
                heading = report.get('Cog')
            try:
                heading = float(heading)
                heading = heading if math.isfinite(heading) and 0 <= heading < 360 else None
            except (ValueError, TypeError):
                heading = None
            AIS_STATE['vessels'][mmsi] = {
                'mmsi': mmsi, 'name': name or previous.get('name') or f'MMSI {mmsi}',
                'lat': lat, 'lon': lon, 'heading': heading,
                'speed': report.get('Sog'), 'updated_at': int(time.time()),
            }
            AIS_STATE['updated_at'] = int(time.time())
        elif previous and name:
            previous['name'] = name
        if len(AIS_STATE['vessels']) > AIS_PRUNE_THRESHOLD:
            cutoff = time.time() - 1800
            recent = {k: v for k, v in AIS_STATE['vessels'].items() if v['updated_at'] >= cutoff}
            if len(recent) > AIS_MAX_VESSELS:
                recent = dict(sorted(recent.items(), key=lambda row: row[1]['updated_at'], reverse=True)[:AIS_MAX_VESSELS])
            AIS_STATE['vessels'] = recent


def _ais_worker():
    try:
        from websockets.sync.client import connect
    except ImportError:
        with AIS_LOCK:
            AIS_STATE['status'] = 'dependency_missing'
        return
    delay = 3
    while True:
        key = _ais_key()
        if not key:
            with AIS_LOCK:
                AIS_STATE['status'] = 'needs_key'
            return
        connected_at = time.monotonic()
        try:
            with connect('wss://stream.aisstream.io/v0/stream', compression='deflate',
                         open_timeout=10, ping_interval=20, max_size=1_000_000) as socket:
                with AIS_LOCK:
                    boxes = _active_ais_boxes_locked(time.time())
                    AIS_STATE['boxes'] = boxes
                    AIS_STATE['status'] = 'connecting'
                if not boxes:
                    with AIS_LOCK:
                        AIS_STATE['status'] = 'idle'
                    return
                socket.send(json.dumps({'APIKey': key, 'BoundingBoxes': boxes,
                                        'FilterMessageTypes': AIS_MESSAGE_TYPES}))
                sent_at = time.monotonic()
                active_boxes = boxes
                while True:
                    with AIS_LOCK:
                        wanted = _active_ais_boxes_locked(time.time())
                        AIS_STATE['boxes'] = wanted
                    if not wanted:
                        with AIS_LOCK:
                            AIS_STATE['status'] = 'idle'
                        return
                    if wanted != active_boxes and time.monotonic() - sent_at >= 2:
                        socket.send(json.dumps({'APIKey': key, 'BoundingBoxes': wanted,
                                                'FilterMessageTypes': AIS_MESSAGE_TYPES}))
                        active_boxes, sent_at = wanted, time.monotonic()
                    try:
                        raw = socket.recv(timeout=1)
                    except TimeoutError:
                        continue
                    message = json.loads(raw)
                    if message.get('MessageType') == 'SubscriptionConfirmation':
                        if not message.get('Message', {}).get('CompressionEnabled'):
                            raise RuntimeError('AISStream connection did not negotiate compression')
                        with AIS_LOCK:
                            AIS_STATE['status'] = 'live'
                    else:
                        _vessel_message(message)
        except Exception:
            with AIS_LOCK:
                AIS_STATE['status'] = 'reconnecting'
            if time.monotonic() - connected_at > 120:
                delay = 3
            time.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(300, delay * 2)


def _boxes_for_view(south, west, north, east):
    vals = (south, west, north, east)
    if not all(math.isfinite(v) for v in vals) or not (-90 <= south < north <= 90):
        raise ValueError('Invalid vessel viewport')
    span = east - west
    if not (0 < span <= 120 and north - south <= 70):
        raise ValueError('Zoom closer to view vessel positions')
    south, north = math.floor(south), math.ceil(north)
    west = ((math.floor(west) + 180) % 360) - 180
    east = west + math.ceil(span) + 1
    if east <= 180:
        return [[[north, west], [south, east]]]
    return [[[north, west], [south, 180]], [[north, -180], [south, east - 360]]]


def _active_ais_boxes_locked(now):
    """Combine active viewer areas without letting one viewer replace another."""
    for viewer, entry in list(AIS_STATE['viewers'].items()):
        if now - entry['seen_at'] > AIS_VIEWER_TTL:
            del AIS_STATE['viewers'][viewer]
    unique = {tuple(value for corner in box for value in corner)
              for entry in AIS_STATE['viewers'].values() for box in entry['boxes']}
    return [[[north, west], [south, east]] for north, west, south, east in sorted(unique)]


def vessel_snapshot(south, west, north, east, client_id=None):
    south, west, north, east = map(float, (south, west, north, east))
    boxes = _boxes_for_view(south, west, north, east)
    client_id = str(client_id or 'legacy').lower()
    if client_id != 'legacy' and not re.fullmatch(r'[0-9a-f]{32}', client_id):
        raise ValueError('Invalid vessel client')
    key = _ais_key()
    if not key:
        return {'status': 'needs_key', 'vessels': [], 'source': 'AISStream'}
    with AIS_LOCK:
        now = time.time()
        _active_ais_boxes_locked(now)
        previous = AIS_STATE['viewers'].get(client_id)
        at_capacity = previous is None and len(AIS_STATE['viewers']) >= AIS_MAX_VIEWERS
        if not at_capacity:
            AIS_STATE['viewers'][client_id] = {'boxes': boxes, 'seen_at': now}
            active_boxes = _active_ais_boxes_locked(now)
            at_capacity = len(active_boxes) > AIS_MAX_BOXES
        if at_capacity:
            if previous:
                AIS_STATE['viewers'][client_id] = previous
            else:
                AIS_STATE['viewers'].pop(client_id, None)
        else:
            AIS_STATE['boxes'] = active_boxes
        if not at_capacity and (not AIS_STATE['thread'] or not AIS_STATE['thread'].is_alive()):
            AIS_STATE['status'] = 'connecting'
            AIS_STATE['thread'] = threading.Thread(target=_ais_worker, daemon=True, name='aisstream-relay')
            AIS_STATE['thread'].start()
        # The stream supplies new reports, so positions build up after each subscription.
        rows = [v.copy() for v in AIS_STATE['vessels'].values()
                if now - v['updated_at'] <= 1200 and south <= v['lat'] <= north
                and any(west <= v['lon'] + shift <= east for shift in (-360, 0, 360))]
        status = 'capacity' if at_capacity else AIS_STATE['status']
        updated_at = AIS_STATE['updated_at']
    rows.sort(key=lambda v: v['updated_at'], reverse=True)
    return {'status': status, 'vessels': rows[:5000], 'updated_at': updated_at, 'source': 'AISStream'}


def ais_health():
    with AIS_LOCK:
        boxes = _active_ais_boxes_locked(time.time())
        return {'status': AIS_STATE['status'], 'viewers': len(AIS_STATE['viewers']),
                'max_viewers': AIS_MAX_VIEWERS, 'boxes': len(boxes), 'max_boxes': AIS_MAX_BOXES,
                'cached_vessels': len(AIS_STATE['vessels'])}


def vessel_search(query):
    """Search recently observed AIS positions by reported ship name."""
    def fold(value):
        normalized = unicodedata.normalize('NFKD', str(value or ''))
        return ''.join(char for char in normalized if not unicodedata.combining(char)).casefold().strip()

    query = fold(query)
    if len(query) < 2 or len(query) > 80:
        raise ValueError('Enter at least two characters for vessel search')
    if not _ais_key():
        return {'status': 'needs_key', 'vessels': [], 'source': 'AISStream'}
    with AIS_LOCK:
        status = AIS_STATE['status']
        now = time.time()
        rows = [v.copy() for v in AIS_STATE['vessels'].values()
                if now - v.get('updated_at', 0) <= 1200 and query in fold(v.get('name', ''))]
    if status == 'needs_key':
        status = 'not_started'
    rows.sort(key=lambda vessel: (
        not fold(vessel.get('name', '')).startswith(query),
        -int(vessel.get('updated_at', 0)),
    ))
    return {'status': status, 'vessels': rows[:20], 'source': 'AISStream'}
