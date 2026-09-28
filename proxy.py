#!/usr/bin/env python3
"""
Proxy server:
  GET /video-token?id={cameraId}  → state 511 auth, returns proxied m3u8 URL
  GET /stream/{host}/{path}?{qs}  → forwards an allowlisted 511 stream
  GET /*                          → static file serving
"""
import ast, base64, concurrent.futures, datetime, gzip, hashlib, html, http, http.server, ipaddress, json, math, os, re, sqlite3, struct, threading, time, urllib.error, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from collections import OrderedDict

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from zoneinfo import ZoneInfo
from global_feeds import AircraftRateLimited, aircraft_route, aircraft_snapshot, aircraft_track, ais_health, vessel_search, vessel_snapshot
from hazard_feeds import hazard_snapshot
from cyber_feeds import cyber_snapshot
from international_emergency import international_emergency_snapshot
from international_infrastructure import (road_snapshot as international_road_snapshot,
                                          power_snapshot as international_power_snapshot,
                                          international_traffic_snapshot,
                                          zurich_sensor_sample, northern_ireland_camera_snapshot,
                                          madrid_camera_snapshot, dgt_camera_snapshot,
                                          tfl_camera_snapshot,
                                          estonia_camera_snapshot,
                                          lyon_camera_snapshot,
                                          vitoria_camera_snapshot,
                                          vigo_camera_snapshot,
                                          luxembourg_camera_snapshot,
                                          lithuania_camera_snapshot,
                                          lithuania_event_detail,
                                          tii_camera_snapshot)
from radio_catalog import catalog_snapshot as radio_catalog_snapshot, record_station_click
from cyclone_guidance import guidance_snapshot as cyclone_guidance_snapshot
from trip_routing import RouteBusy, RouteNotFound, RouteTooLong, RouteUnavailable, parse_point as parse_route_point, route_snapshot
from arcgis_catalog import arcgis_viewport
from dutch_anpr import SEARCH_URL as DUTCH_ANPR_SEARCH_URL, latest_plan as dutch_anpr_latest_plan, parse_plan as parse_dutch_anpr_plan, plan_for_bbox as dutch_anpr_for_bbox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = os.getenv('AMERICAMAP_HOST', os.getenv('FLORIDAMAP_HOST', '127.0.0.1'))
PORT = int(os.getenv('AMERICAMAP_PORT', os.getenv('FLORIDAMAP_PORT', os.getenv('PORT', '8765'))))
DEBUG = os.getenv('AMERICAMAP_DEBUG', os.getenv('FLORIDAMAP_DEBUG', '')).strip() == '1'
CACHE_ENABLED = os.getenv('AMERICAMAP_CACHE_ENABLED', '1').strip().lower() not in {'0', 'false', 'no'}
CACHE_DIR = os.path.abspath(os.getenv('AMERICAMAP_CACHE_DIR', os.path.join(BASE_DIR, '.cache')))
CACHE_DB_PATH = os.path.join(CACHE_DIR, 'shared-responses.sqlite3')
SERVER_MAX_CONCURRENT_REQUESTS = max(4, int(os.getenv('AMERICAMAP_MAX_REQUESTS', '128')))
SERVER_REQUEST_QUEUE = max(16, int(os.getenv('AMERICAMAP_REQUEST_QUEUE', '256')))
SERVER_IDLE_TIMEOUT = max(5, int(os.getenv('AMERICAMAP_IDLE_TIMEOUT', '20')))
SOURCE_FETCH_WORKERS = max(1, int(os.getenv('AMERICAMAP_SOURCE_FETCH_WORKERS', '16')))
API_UPSTREAM_CONCURRENCY = max(1, int(os.getenv('AMERICAMAP_API_UPSTREAM_CONCURRENCY', '16')))
MEDIA_UPSTREAM_CONCURRENCY = max(1, int(os.getenv('AMERICAMAP_MEDIA_UPSTREAM_CONCURRENCY', '32')))
MAX_STREAM_REQUESTS = max(1, int(os.getenv('AMERICAMAP_MAX_STREAMS', '48')))
MAX_STREAM_PLAYLIST_BYTES = 512 * 1024
MAX_STREAM_MEDIA_BYTES = 32 * 1024 * 1024
API_UPSTREAM_SEMAPHORE = threading.BoundedSemaphore(API_UPSTREAM_CONCURRENCY)
MEDIA_UPSTREAM_SEMAPHORE = threading.BoundedSemaphore(MEDIA_UPSTREAM_CONCURRENCY)
STREAM_REQUEST_SEMAPHORE = threading.BoundedSemaphore(MAX_STREAM_REQUESTS)


class NoStreamRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise urllib.error.HTTPError(request.full_url, code, 'Stream redirect blocked', headers, response)


STREAM_HTTP_OPENER = urllib.request.build_opener(NoStreamRedirectHandler)


def rate_limit_bucket(path):
    for prefix in ('/stream/', '/camera-snapshot/', '/511/', '/fl511/'):
        if path.startswith(prefix):
            return prefix
    return path


def parse_trusted_proxy_networks(value):
    networks = []
    for token in str(value or '').split(','):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            print(f'[server] ignoring invalid trusted proxy network: {token}', flush=True)
    return tuple(networks)


TRUSTED_PROXY_NETWORKS = parse_trusted_proxy_networks(
    os.getenv('AMERICAMAP_TRUSTED_PROXIES', '127.0.0.1,::1')
)


class SharedResponseCache:
    """Thread-safe response cache with persistence, single-flight, and stale fallback."""

    def __init__(
        self,
        db_path=None,
        max_entries=2048,
        max_bytes=256 * 1024 * 1024,
        load_semaphore=None,
        load_wait_timeout=120,
    ):
        self.db_path = db_path
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._entries = OrderedDict()
        self._memory_bytes = 0
        self._inflight = {}
        self._lock = threading.RLock()
        self._db = None
        self._load_semaphore = load_semaphore
        self._load_wait_timeout = max(1, float(load_wait_timeout))
        self._stats = {
            'hits': 0,
            'misses': 0,
            'stale': 0,
            'waits': 0,
            'refreshes': 0,
            'errors': 0,
        }
        if CACHE_ENABLED and db_path:
            try:
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                self._db = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
                self._db.execute('PRAGMA journal_mode=WAL')
                self._db.execute('PRAGMA synchronous=NORMAL')
                self._db.execute('PRAGMA busy_timeout=10000')
                self._db.execute('''
                    CREATE TABLE IF NOT EXISTS response_cache (
                        cache_key TEXT PRIMARY KEY,
                        body BLOB NOT NULL,
                        content_type TEXT NOT NULL,
                        fresh_until REAL NOT NULL,
                        stale_until REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        size INTEGER NOT NULL
                    )
                ''')
                self._db.execute(
                    'CREATE INDEX IF NOT EXISTS response_cache_updated_idx '
                    'ON response_cache(updated_at)'
                )
                self._db.execute('DELETE FROM response_cache WHERE stale_until <= ?', (time.time(),))
                self._db.commit()
            except Exception as exc:
                if self._db is not None:
                    self._db.close()
                self._db = None
                print(f'[cache] persistent cache disabled: {exc}', flush=True)

    def _remember_locked(self, key, entry):
        previous = self._entries.pop(key, None)
        if previous:
            self._memory_bytes -= previous['size']
        self._entries[key] = entry
        self._memory_bytes += entry['size']
        while self._entries and (
            len(self._entries) > self.max_entries or self._memory_bytes > self.max_bytes
        ):
            _, evicted = self._entries.popitem(last=False)
            self._memory_bytes -= evicted['size']

    def _read_locked(self, key, persist, include_expired=False):
        now = time.time()
        entry = self._entries.get(key)
        if entry:
            if include_expired or entry['stale_until'] > now:
                self._entries.move_to_end(key)
                return entry
            self._memory_bytes -= entry['size']
            self._entries.pop(key, None)
        if not persist or self._db is None:
            return None
        row = self._db.execute(
            'SELECT body, content_type, fresh_until, stale_until, updated_at, size '
            'FROM response_cache WHERE cache_key = ?',
            (key,),
        ).fetchone()
        if not row:
            return None
        entry = {
            'body': bytes(row[0]),
            'content_type': row[1],
            'fresh_until': float(row[2]),
            'stale_until': float(row[3]),
            'updated_at': float(row[4]),
            'size': int(row[5]),
        }
        if not include_expired and entry['stale_until'] <= now:
            self._db.execute('DELETE FROM response_cache WHERE cache_key = ?', (key,))
            self._db.commit()
            return None
        self._remember_locked(key, entry)
        return entry

    def _store_locked(self, key, body, content_type, ttl, stale_ttl, persist):
        if isinstance(body, str):
            body = body.encode('utf-8')
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError('Cached response body must be bytes')
        body = bytes(body)
        now = time.time()
        entry = {
            'body': body,
            'content_type': str(content_type or 'application/octet-stream'),
            'fresh_until': now + max(1, float(ttl)),
            'stale_until': now + max(float(ttl), float(stale_ttl)),
            'updated_at': now,
            'size': len(body),
        }
        self._remember_locked(key, entry)
        if persist and self._db is not None:
            self._db.execute('''
                INSERT INTO response_cache
                    (cache_key, body, content_type, fresh_until, stale_until, updated_at, size)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    body=excluded.body,
                    content_type=excluded.content_type,
                    fresh_until=excluded.fresh_until,
                    stale_until=excluded.stale_until,
                    updated_at=excluded.updated_at,
                    size=excluded.size
            ''', (
                key, sqlite3.Binary(body), entry['content_type'], entry['fresh_until'],
                entry['stale_until'], entry['updated_at'], entry['size'],
            ))
            self._db.execute('DELETE FROM response_cache WHERE stale_until <= ?', (now,))
            self._db.execute('''
                DELETE FROM response_cache WHERE cache_key IN (
                    SELECT cache_key FROM response_cache
                    ORDER BY updated_at DESC
                    LIMIT -1 OFFSET ?
                )
            ''', (self.max_entries,))
            disk_bytes = int(self._db.execute(
                'SELECT COALESCE(SUM(size), 0) FROM response_cache'
            ).fetchone()[0])
            if disk_bytes > self.max_bytes:
                delete_keys = []
                for old_key, old_size in self._db.execute(
                    'SELECT cache_key, size FROM response_cache ORDER BY updated_at ASC'
                ):
                    delete_keys.append((old_key,))
                    disk_bytes -= int(old_size)
                    if disk_bytes <= self.max_bytes:
                        break
                self._db.executemany(
                    'DELETE FROM response_cache WHERE cache_key = ?',
                    delete_keys,
                )
            self._db.commit()
        return entry

    def _finish_inflight_locked(self, key):
        event = self._inflight.pop(key, None)
        if event:
            event.set()

    def _call_loader(self, loader):
        if self._load_semaphore is None:
            return loader()
        if not self._load_semaphore.acquire(timeout=self._load_wait_timeout):
            raise TimeoutError('Timed out waiting for an upstream fetch slot')
        try:
            return loader()
        finally:
            self._load_semaphore.release()

    def _refresh(self, key, loader, ttl, stale_ttl, persist):
        try:
            body, content_type = self._call_loader(loader)
            with self._lock:
                self._store_locked(key, body, content_type, ttl, stale_ttl, persist)
                self._stats['refreshes'] += 1
        except Exception as exc:
            with self._lock:
                self._stats['errors'] += 1
            print(f'[cache] background refresh failed for {key}: {exc}', flush=True)
        finally:
            with self._lock:
                self._finish_inflight_locked(key)

    def get_or_load(self, key, loader, ttl, stale_ttl, persist=True, wait_timeout=45):
        if not CACHE_ENABLED:
            body, content_type = self._call_loader(loader)
            return body, content_type, 'BYPASS'

        owner = False
        stale_entry = None
        with self._lock:
            now = time.time()
            entry = self._read_locked(key, persist)
            if entry and entry['fresh_until'] > now:
                self._stats['hits'] += 1
                return entry['body'], entry['content_type'], 'HIT'
            if entry and entry['stale_until'] > now:
                stale_entry = entry
                self._stats['stale'] += 1
                if key not in self._inflight:
                    event = threading.Event()
                    self._inflight[key] = event
                    CACHE_REFRESH_EXECUTOR.submit(
                        self._refresh,
                        key,
                        loader,
                        ttl,
                        stale_ttl,
                        persist,
                    )
                return entry['body'], entry['content_type'], 'STALE'

            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                self._stats['misses'] += 1
                owner = True
            else:
                self._stats['waits'] += 1

        if not owner:
            if not event.wait(timeout=max(1, wait_timeout)):
                raise TimeoutError(f'Cache refresh timed out for {key}')
            with self._lock:
                entry = self._read_locked(key, persist)
                if entry:
                    return entry['body'], entry['content_type'], 'WAIT'
            raise RuntimeError(f'Cache refresh failed for {key}')

        try:
            body, content_type = self._call_loader(loader)
            with self._lock:
                self._store_locked(key, body, content_type, ttl, stale_ttl, persist)
            return body, content_type, 'MISS'
        except Exception:
            with self._lock:
                self._stats['errors'] += 1
                stale_entry = self._read_locked(key, persist, include_expired=True)
            if stale_entry:
                return stale_entry['body'], stale_entry['content_type'], 'STALE-ERROR'
            raise
        finally:
            with self._lock:
                self._finish_inflight_locked(key)

    def snapshot(self):
        with self._lock:
            disk_entries = 0
            disk_bytes = 0
            if self._db is not None:
                row = self._db.execute(
                    'SELECT COUNT(*), COALESCE(SUM(size), 0) FROM response_cache'
                ).fetchone()
                disk_entries, disk_bytes = int(row[0]), int(row[1])
            return {
                'enabled': CACHE_ENABLED,
                'persistent': self._db is not None,
                'memory_entries': len(self._entries),
                'memory_bytes': self._memory_bytes,
                'disk_entries': disk_entries,
                'disk_bytes': disk_bytes,
                'inflight': len(self._inflight),
                **self._stats,
            }

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


CACHE_REFRESH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(2, int(os.getenv('AMERICAMAP_CACHE_REFRESH_WORKERS', '12'))),
    thread_name_prefix='cache-refresh',
)

API_RESPONSE_CACHE = SharedResponseCache(
    CACHE_DB_PATH,
    max_entries=int(os.getenv('AMERICAMAP_CACHE_MAX_ENTRIES', '4096')),
    max_bytes=int(os.getenv('AMERICAMAP_CACHE_MAX_BYTES', str(256 * 1024 * 1024))),
    load_semaphore=API_UPSTREAM_SEMAPHORE,
)
MEDIA_RESPONSE_CACHE = SharedResponseCache(
    None,
    max_entries=int(os.getenv('AMERICAMAP_MEDIA_CACHE_MAX_ENTRIES', '2048')),
    max_bytes=int(os.getenv('AMERICAMAP_MEDIA_CACHE_MAX_BYTES', str(96 * 1024 * 1024))),
    load_semaphore=MEDIA_UPSTREAM_SEMAPHORE,
)
COMPRESSED_RESPONSE_CACHE = SharedResponseCache(
    None,
    max_entries=int(os.getenv('AMERICAMAP_GZIP_CACHE_MAX_ENTRIES', '256')),
    max_bytes=int(os.getenv('AMERICAMAP_GZIP_CACHE_MAX_BYTES', str(64 * 1024 * 1024))),
)
STATIC_RESPONSE_CACHE = SharedResponseCache(
    None,
    max_entries=64,
    max_bytes=64 * 1024 * 1024,
)

TRAFFIC_LAYER_CACHE_TTLS = {
    'Incidents': 45,
    'DisabledVehicles': 45,
    'SevereImpact': 45,
    'MessageSigns': 60,
    'Cameras': 120,
    'Construction': 300,
}
TRAFFIC_LAYER_STALE_TTL = int(os.getenv('AMERICAMAP_TRAFFIC_STALE_TTL', '21600'))
TRAFFIC_TOOLTIP_CACHE_TTL = int(os.getenv('AMERICAMAP_TOOLTIP_CACHE_TTL', '300'))
TRAFFIC_TOOLTIP_STALE_TTL = int(os.getenv('AMERICAMAP_TOOLTIP_STALE_TTL', '21600'))
CAMERA_SNAPSHOT_CACHE_TTL = int(os.getenv('AMERICAMAP_SNAPSHOT_CACHE_TTL', '8'))
CAMERA_SNAPSHOT_STALE_TTL = int(os.getenv('AMERICAMAP_SNAPSHOT_STALE_TTL', '60'))
TRAFFIC_TILE_CACHE_TTL = int(os.getenv('AMERICAMAP_TILE_CACHE_TTL', '60'))
TRAFFIC_TILE_STALE_TTL = int(os.getenv('AMERICAMAP_TILE_STALE_TTL', '600'))
RADAR_TILE_CACHE_TTL = int(os.getenv('AMERICAMAP_RADAR_TILE_CACHE_TTL', '120'))
RADAR_TILE_STALE_TTL = int(os.getenv('AMERICAMAP_RADAR_TILE_STALE_TTL', '600'))

# Regional coverage grows by adding state adapters. Florida remains the original state;
# each additional state is additive rather than a replacement.
REGIONS = {
    'FL': {
        'code': 'FL',
        'name': 'Florida',
        'bounds': {'min_lat': 24.4, 'max_lat': 31.1, 'min_lon': -87.7, 'max_lon': -79.9},
        'traffic_origin': 'https://fl511.com',
        'traffic_adapter': 'iteris',
        'weather_network': 'FL_ASOS',
    },
    'GA': {
        'code': 'GA',
        'name': 'Georgia',
        'bounds': {'min_lat': 30.35, 'max_lat': 35.05, 'min_lon': -85.65, 'max_lon': -80.70},
        'traffic_origin': 'https://511ga.org',
        'traffic_adapter': 'iteris',
        'weather_network': 'GA_ASOS',
    },
    'AL': {
        'code': 'AL',
        'name': 'Alabama',
        'bounds': {'min_lat': 30.10, 'max_lat': 35.10, 'min_lon': -88.60, 'max_lon': -84.80},
        'traffic_origin': 'https://api.algotraffic.com',
        'traffic_adapter': 'algo',
        'weather_network': 'AL_ASOS',
    },
    'MS': {
        'code': 'MS',
        'name': 'Mississippi',
        'bounds': {'min_lat': 30.15, 'max_lat': 35.05, 'min_lon': -91.70, 'max_lon': -88.05},
        'traffic_origin': 'https://www.mdottraffic.com',
        'traffic_adapter': 'mdot',
        'weather_network': 'MS_ASOS',
    },
    'SC': {
        'code': 'SC',
        'name': 'South Carolina',
        'bounds': {'min_lat': 31.99, 'max_lat': 35.22, 'min_lon': -83.36, 'max_lon': -78.49},
        'traffic_origin': 'https://www.511sc.org',
        'traffic_adapter': 'sc511',
        'weather_network': 'SC_ASOS',
    },
    'NC': {
        'code': 'NC',
        'name': 'North Carolina',
        'bounds': {'min_lat': 33.72, 'max_lat': 36.61, 'min_lon': -84.53, 'max_lon': -75.39},
        'traffic_origin': 'https://drivenc.gov',
        'traffic_adapter': 'drivenc',
        'weather_network': 'NC_ASOS',
    },
    'TN': {
        'code': 'TN',
        'name': 'Tennessee',
        'bounds': {'min_lat': 34.85, 'max_lat': 36.74, 'min_lon': -90.32, 'max_lon': -81.63},
        'traffic_origin': 'https://smartway.tn.gov',
        'traffic_adapter': 'tdot',
        'weather_network': 'TN_ASOS',
    },
    'KY': {
        'code': 'KY',
        'name': 'Kentucky',
        'bounds': {'min_lat': 36.47, 'max_lat': 39.15, 'min_lon': -89.58, 'max_lon': -81.95},
        'traffic_origin': 'https://goky.ky.gov',
        'traffic_adapter': 'goky',
        'weather_network': 'KY_ASOS',
    },
    'VA': {
        'code': 'VA',
        'name': 'Virginia',
        'bounds': {'min_lat': 36.54, 'max_lat': 39.48, 'min_lon': -83.68, 'max_lon': -75.17},
        'traffic_origin': 'https://511.vdot.virginia.gov',
        'traffic_adapter': 'vdot',
        'weather_network': 'VA_ASOS',
    },
    'WV': {
        'code': 'WV',
        'name': 'West Virginia',
        'bounds': {'min_lat': 37.15, 'max_lat': 40.65, 'min_lon': -82.70, 'max_lon': -77.65},
        'traffic_origin': 'https://www.wv511.org',
        'traffic_adapter': 'wv511',
        'weather_network': 'WV_ASOS',
    },
    'MD': {
        'code': 'MD',
        'name': 'Maryland',
        'bounds': {'min_lat': 37.88, 'max_lat': 39.73, 'min_lon': -79.49, 'max_lon': -74.98},
        'traffic_origin': 'https://chart.maryland.gov',
        'traffic_adapter': 'mdchart',
        'weather_network': 'MD_ASOS',
    },
    'DC': {
        'code': 'DC',
        'name': 'Washington, D.C.',
        'bounds': {'min_lat': 38.79, 'max_lat': 39.00, 'min_lon': -77.13, 'max_lon': -76.89},
        'traffic_origin': 'https://www.trafficview.org',
        'traffic_adapter': 'dcgis',
        'weather_network': 'DC_ASOS',
    },
    'DE': {
        'code': 'DE',
        'name': 'Delaware',
        'bounds': {'min_lat': 38.44, 'max_lat': 39.84, 'min_lon': -75.79, 'max_lon': -74.98},
        'traffic_origin': 'https://tmc.deldot.gov',
        'traffic_adapter': 'deldot',
        'weather_network': 'DE_ASOS',
    },
    'PA': {
        'code': 'PA',
        'name': 'Pennsylvania',
        'bounds': {'min_lat': 39.71, 'max_lat': 42.52, 'min_lon': -80.52, 'max_lon': -74.68},
        'traffic_origin': 'https://www.511pa.com',
        'traffic_adapter': 'pa511',
        'weather_network': 'PA_ASOS',
    },
    'NJ': {
        'code': 'NJ',
        'name': 'New Jersey',
        'bounds': {'min_lat': 38.88, 'max_lat': 41.36, 'min_lon': -75.56, 'max_lon': -73.88},
        'traffic_origin': 'https://www.njta.gov',
        'traffic_adapter': 'njta',
        'weather_network': 'NJ_ASOS',
    },
    'CT': {
        'code': 'CT',
        'name': 'Connecticut',
        'bounds': {'min_lat': 40.95, 'max_lat': 42.08, 'min_lon': -73.75, 'max_lon': -71.78},
        'traffic_origin': 'https://www.ctroads.org',
        'traffic_adapter': 'ctroads',
        'weather_network': 'CT_ASOS',
    },
    'RI': {
        'code': 'RI',
        'name': 'Rhode Island',
        'bounds': {'min_lat': 41.10, 'max_lat': 42.03, 'min_lon': -71.91, 'max_lon': -71.08},
        'traffic_origin': 'https://www.dot.ri.gov',
        'traffic_adapter': 'ridot',
        'weather_network': 'RI_ASOS',
    },
    'MA': {
        'code': 'MA',
        'name': 'Massachusetts',
        'bounds': {'min_lat': 41.18, 'max_lat': 42.89, 'min_lon': -73.51, 'max_lon': -69.85},
        'traffic_origin': 'https://www.mass511.com',
        'traffic_adapter': 'mass511',
        'weather_network': 'MA_ASOS',
    },
    'NH': {
        'code': 'NH',
        'name': 'New Hampshire',
        'bounds': {'min_lat': 42.69, 'max_lat': 45.31, 'min_lon': -72.56, 'max_lon': -70.61},
        'traffic_origin': 'https://www.newengland511.org',
        'traffic_adapter': 'newengland511',
        'weather_network': 'NH_ASOS',
    },
    'VT': {
        'code': 'VT',
        'name': 'Vermont',
        'bounds': {'min_lat': 42.70, 'max_lat': 45.02, 'min_lon': -73.44, 'max_lon': -71.46},
        'traffic_origin': 'https://www.newengland511.org',
        'traffic_adapter': 'newengland511',
        'weather_network': 'VT_ASOS',
    },
    'ME': {
        'code': 'ME',
        'name': 'Maine',
        'bounds': {'min_lat': 42.90, 'max_lat': 47.48, 'min_lon': -71.10, 'max_lon': -66.85},
        'traffic_origin': 'https://www.newengland511.org',
        'traffic_adapter': 'newengland511',
        'weather_network': 'ME_ASOS',
    },
    'NY': {
        'code': 'NY',
        'name': 'New York',
        'bounds': {'min_lat': 40.45, 'max_lat': 45.02, 'min_lon': -79.77, 'max_lon': -71.85},
        'traffic_origin': 'https://www.511ny.org',
        'traffic_adapter': 'ny511',
        'weather_network': 'NY_ASOS',
    },
    'OH': {
        'code': 'OH',
        'name': 'Ohio',
        'bounds': {'min_lat': 38.40, 'max_lat': 42.00, 'min_lon': -84.82, 'max_lon': -80.51},
        'traffic_origin': 'https://www.ohgo.com',
        'traffic_adapter': 'ohgo',
        'weather_network': 'OH_ASOS',
    },
    'IN': {
        'code': 'IN',
        'name': 'Indiana',
        'bounds': {'min_lat': 37.77, 'max_lat': 41.77, 'min_lon': -88.10, 'max_lon': -84.78},
        'traffic_origin': 'https://511in.org',
        'traffic_adapter': 'trafficwise_in',
        'weather_network': 'IN_ASOS',
    },
    'IL': {
        'code': 'IL',
        'name': 'Illinois',
        'bounds': {'min_lat': 36.97, 'max_lat': 42.51, 'min_lon': -91.52, 'max_lon': -87.02},
        'traffic_origin': 'https://idot.illinois.gov/travel-and-maps.html',
        'traffic_adapter': 'idot_il',
        'weather_network': 'IL_ASOS',
    },
    'WI': {
        'code': 'WI',
        'name': 'Wisconsin',
        'bounds': {'min_lat': 42.48, 'max_lat': 47.31, 'min_lon': -92.89, 'max_lon': -86.25},
        'traffic_origin': 'https://511wi.gov',
        'traffic_adapter': 'iteris',
        'weather_network': 'WI_ASOS',
    },
    'MN': {
        'code': 'MN',
        'name': 'Minnesota',
        'bounds': {'min_lat': 43.49, 'max_lat': 49.39, 'min_lon': -97.24, 'max_lon': -89.49},
        'traffic_origin': 'https://511mn.org',
        'traffic_adapter': 'mndot_cars',
        'weather_network': 'MN_ASOS',
    },
    'IA': {
        'code': 'IA',
        'name': 'Iowa',
        'bounds': {'min_lat': 40.37, 'max_lat': 43.51, 'min_lon': -96.65, 'max_lon': -90.14},
        'traffic_origin': 'https://511ia.org',
        'traffic_adapter': 'iadot',
        'weather_network': 'IA_ASOS',
    },
    'MO': {
        'code': 'MO',
        'name': 'Missouri',
        'bounds': {'min_lat': 35.99, 'max_lat': 40.62, 'min_lon': -95.78, 'max_lon': -89.10},
        'traffic_origin': 'https://traveler.modot.org',
        'traffic_adapter': 'modot',
        'weather_network': 'MO_ASOS',
    },
    'AR': {
        'code': 'AR',
        'name': 'Arkansas',
        'bounds': {'min_lat': 33.00, 'max_lat': 36.50, 'min_lon': -94.62, 'max_lon': -89.64},
        'traffic_origin': 'https://www.idrivearkansas.com',
        # ARDOT's acceptable-use policy does not authorize republishing IDrive
        # data in third-party map applications. Keep these endpoints empty-safe
        # while the nationwide and independently licensed layers remain active.
        'traffic_adapter': 'restricted',
        'weather_network': 'AR_ASOS',
    },
    'LA': {
        'code': 'LA',
        'name': 'Louisiana',
        'bounds': {'min_lat': 28.80, 'max_lat': 33.05, 'min_lon': -94.05, 'max_lon': -88.75},
        'traffic_origin': 'https://www.511la.org',
        'traffic_adapter': 'iteris',
        'weather_network': 'LA_ASOS',
    },
    'OK': {
        'code': 'OK',
        'name': 'Oklahoma',
        'bounds': {'min_lat': 33.60, 'max_lat': 37.10, 'min_lon': -103.01, 'max_lon': -94.43},
        'traffic_origin': 'https://oktraffic.org',
        'traffic_adapter': 'oktraffic',
        'weather_network': 'OK_ASOS',
    },
    'TX': {
        'code': 'TX',
        'name': 'Texas',
        'bounds': {'min_lat': 25.84, 'max_lat': 36.51, 'min_lon': -106.65, 'max_lon': -93.50},
        'traffic_origin': 'https://drivetexas.org',
        'traffic_adapter': 'drivetexas',
        'weather_network': 'TX_ASOS',
    },
    'NM': {
        'code': 'NM',
        'name': 'New Mexico',
        'bounds': {'min_lat': 31.30, 'max_lat': 37.01, 'min_lon': -109.06, 'max_lon': -102.99},
        'traffic_origin': 'https://nmroads.com',
        'traffic_adapter': 'nmroads',
        'weather_network': 'NM_ASOS',
    },
    'AZ': {
        'code': 'AZ',
        'name': 'Arizona',
        'bounds': {'min_lat': 31.30, 'max_lat': 37.01, 'min_lon': -114.82, 'max_lon': -109.04},
        'traffic_origin': 'https://az511.gov',
        'traffic_adapter': 'iteris',
        'weather_network': 'AZ_ASOS',
    },
    'CA': {
        'code': 'CA',
        'name': 'California',
        'bounds': {'min_lat': 32.50, 'max_lat': 42.10, 'min_lon': -124.50, 'max_lon': -114.00},
        'traffic_origin': 'https://quickmap.dot.ca.gov',
        'traffic_adapter': 'caltrans_quickmap',
        'weather_network': 'CA_ASOS',
    },
    'NV': {
        'code': 'NV',
        'name': 'Nevada',
        'bounds': {'min_lat': 34.95, 'max_lat': 42.05, 'min_lon': -120.05, 'max_lon': -114.00},
        'traffic_origin': 'https://www.nvroads.com',
        'traffic_adapter': 'nvroads',
        'weather_network': 'NV_ASOS',
    },
    'OR': {
        'code': 'OR',
        'name': 'Oregon',
        'bounds': {'min_lat': 41.99, 'max_lat': 46.30, 'min_lon': -124.71, 'max_lon': -116.45},
        'traffic_origin': 'https://www.tripcheck.com',
        'traffic_adapter': 'tripcheck_or',
        'weather_network': 'OR_ASOS',
    },
    'WA': {
        'code': 'WA',
        'name': 'Washington',
        'bounds': {'min_lat': 45.54, 'max_lat': 49.01, 'min_lon': -124.85, 'max_lon': -116.91},
        'traffic_origin': 'https://wsdot.com/travel/real-time/traffic-map',
        'traffic_adapter': 'wsdot',
        'weather_network': 'WA_ASOS',
    },
    'ID': {
        'code': 'ID',
        'name': 'Idaho',
        'bounds': {'min_lat': 41.99, 'max_lat': 49.01, 'min_lon': -117.25, 'max_lon': -111.04},
        'traffic_origin': 'https://511.idaho.gov',
        'traffic_adapter': 'iteris',
        'weather_network': 'ID_ASOS',
    },
    'UT': {
        'code': 'UT',
        'name': 'Utah',
        'bounds': {'min_lat': 36.99, 'max_lat': 42.01, 'min_lon': -114.06, 'max_lon': -109.04},
        'traffic_origin': 'https://udottraffic.utah.gov',
        'traffic_adapter': 'iteris',
        'weather_network': 'UT_ASOS',
    },
    'CO': {
        'code': 'CO',
        'name': 'Colorado',
        'bounds': {'min_lat': 36.99, 'max_lat': 41.01, 'min_lon': -109.06, 'max_lon': -102.04},
        'traffic_origin': 'https://www.cotrip.org',
        'traffic_adapter': 'cotrip',
        'weather_network': 'CO_ASOS',
    },
    'MI': {
        'code': 'MI',
        'name': 'Michigan',
        'bounds': {'min_lat': 41.69, 'max_lat': 48.31, 'min_lon': -90.42, 'max_lon': -82.12},
        'traffic_origin': 'https://mdotjboss.state.mi.us/MiDrive/map',
        'traffic_adapter': 'midrive',
        'weather_network': 'MI_ASOS',
    },
    'WY': {
        'code': 'WY',
        'name': 'Wyoming',
        'bounds': {'min_lat': 40.99, 'max_lat': 45.01, 'min_lon': -111.06, 'max_lon': -104.04},
        'traffic_origin': 'https://map.wyoroad.info/511-map/',
        'traffic_adapter': 'wy511',
        'weather_network': 'WY_ASOS',
    },
    'MT': {
        'code': 'MT',
        'name': 'Montana',
        'bounds': {'min_lat': 44.35, 'max_lat': 49.01, 'min_lon': -116.06, 'max_lon': -104.03},
        'traffic_origin': 'https://www.511mt.net',
        'traffic_adapter': 'mt511',
        'weather_network': 'MT_ASOS',
    },
    'ND': {
        'code': 'ND',
        'name': 'North Dakota',
        'bounds': {'min_lat': 45.93, 'max_lat': 49.01, 'min_lon': -104.06, 'max_lon': -96.55},
        'traffic_origin': 'https://travel.dot.nd.gov',
        'traffic_adapter': 'ndroads',
        'weather_network': 'ND_ASOS',
    },
    'SD': {
        'code': 'SD',
        'name': 'South Dakota',
        'bounds': {'min_lat': 42.47, 'max_lat': 45.95, 'min_lon': -104.06, 'max_lon': -96.43},
        'traffic_origin': 'https://www.sd511.org',
        'traffic_adapter': 'sd511',
        'weather_network': 'SD_ASOS',
    },
    'NE': {
        'code': 'NE',
        'name': 'Nebraska',
        'bounds': {'min_lat': 39.99, 'max_lat': 43.01, 'min_lon': -104.06, 'max_lon': -95.30},
        'traffic_origin': 'https://www.511.nebraska.gov',
        'traffic_adapter': 'cars511',
        'weather_network': 'NE_ASOS',
    },
    'KS': {
        'code': 'KS',
        'name': 'Kansas',
        'bounds': {'min_lat': 36.99, 'max_lat': 40.01, 'min_lon': -102.06, 'max_lon': -94.58},
        'traffic_origin': 'https://www.kandrive.gov',
        'traffic_adapter': 'cars511',
        'weather_network': 'KS_ASOS',
    },
    'AK': {
        'code': 'AK', 'name': 'Alaska',
        'bounds': {'min_lat': 51.10, 'max_lat': 71.50, 'min_lon': -179.95, 'max_lon': -129.80},
        'traffic_origin': 'https://511.alaska.gov', 'traffic_adapter': 'iteris',
        'weather_network': 'AK_ASOS', 'country': 'US',
    },
    'HI': {
        'code': 'HI', 'name': 'Hawaii',
        'bounds': {'min_lat': 18.65, 'max_lat': 22.45, 'min_lon': -160.70, 'max_lon': -154.50},
        'traffic_origin': 'https://www.goakamai.org', 'traffic_adapter': 'goakamai',
        'weather_network': 'HI_ASOS', 'country': 'US',
    },
    'NL': {
        'code': 'NL', 'name': 'Newfoundland and Labrador',
        'bounds': {'min_lat': 46.60, 'max_lat': 60.32, 'min_lon': -67.83, 'max_lon': -52.61},
        'traffic_origin': 'https://511nl.ca', 'traffic_adapter': 'iteris',
        'weather_network': 'CA_NF_ASOS', 'country': 'CA',
    },
    'PE': {
        'code': 'PE', 'name': 'Prince Edward Island',
        'bounds': {'min_lat': 45.94, 'max_lat': 47.07, 'min_lon': -64.42, 'max_lon': -61.96},
        'traffic_origin': 'https://511.gov.pe.ca', 'traffic_adapter': 'iteris',
        'weather_network': 'CA_PE_ASOS', 'country': 'CA',
    },
    'NS': {
        'code': 'NS', 'name': 'Nova Scotia',
        'bounds': {'min_lat': 43.44, 'max_lat': 47.05, 'min_lon': -66.22, 'max_lon': -59.78},
        'traffic_origin': 'https://511.novascotia.ca', 'traffic_adapter': 'iteris',
        'weather_network': 'CA_NS_ASOS', 'country': 'CA',
    },
    'NB': {
        'code': 'NB', 'name': 'New Brunswick',
        'bounds': {'min_lat': 44.59, 'max_lat': 48.08, 'min_lon': -69.06, 'max_lon': -63.76},
        'traffic_origin': 'https://511.gnb.ca', 'traffic_adapter': 'iteris',
        'weather_network': 'CA_NB_ASOS', 'country': 'CA',
    },
    'QC': {
        'code': 'QC', 'name': 'Quebec',
        'bounds': {'min_lat': 44.99, 'max_lat': 62.59, 'min_lon': -79.77, 'max_lon': -57.10},
        'traffic_origin': 'https://www.quebec511.info', 'traffic_adapter': 'quebec511',
        'weather_network': 'CA_QC_ASOS', 'country': 'CA',
    },
    'ON': {
        'code': 'ON', 'name': 'Ontario',
        'bounds': {'min_lat': 41.90, 'max_lat': 56.87, 'min_lon': -95.16, 'max_lon': -74.34},
        'traffic_origin': 'https://511on.ca', 'traffic_adapter': 'ontario511',
        'weather_network': 'CA_ON_ASOS', 'country': 'CA',
    },
    'MB': {
        'code': 'MB', 'name': 'Manitoba',
        'bounds': {'min_lat': 48.99, 'max_lat': 60.01, 'min_lon': -102.01, 'max_lon': -88.97},
        'traffic_origin': 'https://www.manitoba511.ca', 'traffic_adapter': 'iteris',
        'weather_network': 'CA_MB_ASOS', 'country': 'CA',
    },
    'SK': {
        'code': 'SK', 'name': 'Saskatchewan',
        'bounds': {'min_lat': 48.99, 'max_lat': 60.01, 'min_lon': -110.01, 'max_lon': -101.35},
        'traffic_origin': 'https://hotline.gov.sk.ca', 'traffic_adapter': 'iteris',
        'weather_network': 'CA_SK_ASOS', 'country': 'CA',
    },
    'AB': {
        'code': 'AB', 'name': 'Alberta',
        'bounds': {'min_lat': 48.99, 'max_lat': 60.01, 'min_lon': -120.01, 'max_lon': -109.99},
        'traffic_origin': 'https://511.alberta.ca', 'traffic_adapter': 'iteris',
        'weather_network': 'CA_AB_ASOS', 'country': 'CA',
    },
    'BC': {
        'code': 'BC', 'name': 'British Columbia',
        'bounds': {'min_lat': 48.30, 'max_lat': 60.01, 'min_lon': -139.06, 'max_lon': -114.05},
        'traffic_origin': 'https://www.drivebc.ca', 'traffic_adapter': 'drivebc',
        'weather_network': 'CA_BC_ASOS', 'country': 'CA',
    },
    'YT': {
        'code': 'YT', 'name': 'Yukon',
        'bounds': {'min_lat': 59.99, 'max_lat': 69.66, 'min_lon': -141.02, 'max_lon': -123.78},
        'traffic_origin': 'https://511yukon.ca', 'traffic_adapter': 'iteris',
        'weather_network': 'CA_YT_ASOS', 'country': 'CA',
    },
    'NT': {
        'code': 'NT', 'name': 'Northwest Territories',
        'bounds': {'min_lat': 59.99, 'max_lat': 78.78, 'min_lon': -136.48, 'max_lon': -101.99},
        'traffic_origin': 'https://drivenwt.ca', 'traffic_adapter': 'drivenwt',
        'weather_network': 'CA_NT_ASOS', 'country': 'CA',
    },
    'NU': {
        'code': 'NU', 'name': 'Nunavut',
        'bounds': {'min_lat': 51.89, 'max_lat': 83.15, 'min_lon': -120.73, 'max_lon': -61.08},
        'traffic_origin': 'https://www.gov.nu.ca/transportation', 'traffic_adapter': 'unavailable',
        'weather_network': 'CA_NU_ASOS', 'country': 'CA',
    },
}
REGION_BOUNDS = {
    'min_lat': min(region['bounds']['min_lat'] for region in REGIONS.values()),
    'max_lat': max(region['bounds']['max_lat'] for region in REGIONS.values()),
    'min_lon': min(region['bounds']['min_lon'] for region in REGIONS.values()),
    'max_lon': max(region['bounds']['max_lon'] for region in REGIONS.values()),
}
DAVNIT_KEEP_SOURCES = { 'OPD', 'OCSO', 'FHP' }
ALLOWED_STREAM_HOST_SUFFIXES = (
    'divas.cloud',
    'navigator.dot.ga.gov',
    'wowza.com',
    'mdottraffic.com',
    'skyvdn.com',
    'tnsnapshots.com',
    'trimarc.org',
    'pws.trafficwise.org',
    'streamlock.net',
    'wzmedia.dot.ca.gov',
    'its.nv.gov',
    'vdotcameras.com',
    'roadsummary.com',
    'sha.maryland.gov',
    'services.ncdot.gov',
    'video.deldot.gov',
    'wink.njta.com',
    'trafficland.com',
    'trafficwise.org',
    'cctv1.dot.wi.gov',
    'video.dot.state.mn.us',
    'iowadot.gov',
    'modot.mo.gov',
    'dotd.la.gov',
    'stream.oktraffic.org',
    'cotrip.org',
)
PUBLIC_STATIC_FILES = frozenset({
    'index.html',
    'globe.js',
    'cyber-trails.js',
    'maplibre-gl.mjs',
    'maplibre-gl-shared.mjs',
    'maplibre-gl-worker.mjs',
    'maplibre-gl.css',
    'maplibre-LICENSE.txt',
    'trip.js',
    'drawings.js',
    'ui.css',
    'road-regions.json',
    'global-cameras.json',
    'ports.json',
    'places.json',
    'ireland-counties-2019.json',
    'globeview-logo.svg',
    'globeview-mark.svg',
    'state-boundary.json',
    'apple-touch-icon.png',
    'icon-192.png',
    'icon-512.png',
    'site.webmanifest',
    'robots.txt',
})
SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    'Content-Security-Policy': (
        "default-src 'self'; "
        "script-src 'self' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: blob: https://fl511.com https://511ga.org https://511wi.gov https://az511.gov https://511.idaho.gov https://udottraffic.utah.gov https://api.algotraffic.com https://www.mdottraffic.com https://*.mdottraffic.com https://drivenc.gov https://www.drivenc.gov https://snapshot.navigator.dot.ga.gov https://tiles.openfreemap.org "
        "https://mapservices.weather.noaa.gov https://*.rainviewer.com https://gibs.earthdata.nasa.gov https://tiles.versatiles.org "
        "https://*.arcgisonline.com https://tile.openweathermap.org https://embed.skylinewebcams.com https://www.ipcamlive.com https://*.ipcamlive.com https://kamera.atlas.vegvesen.no https://www.cita.lu https://weathercam.digitraffic.fi https://etraffic.dgt.es https://informo.madrid.es https://www.vegagerdin.is; "
        "connect-src 'self' https://api.rainviewer.com https://*.rainviewer.com https://gibs.earthdata.nasa.gov https://server.arcgisonline.com https://marine-api.open-meteo.com https://tiles.openfreemap.org https://tiles.versatiles.org https://*.wowza.com https://*.streamlock.net https://widevine-dash.ezdrm.com wss://cctv.trafficview.org:8420; "
        "media-src 'self' blob: https:; "
        "worker-src 'self' blob:; "
        "frame-src https://www.ipcamlive.com https://rtsp.me; "
        "frame-ancestors 'self'; "
        "object-src 'none';"
    ),
}
EMERGENCY_CACHE_TTL = 75
EMERGENCY_CACHE = { 'expires_at': 0, 'body': None, 'refreshing': False, 'last_error': None }
EMERGENCY_CACHE_LOCK = threading.Lock()
POWER_OUTAGE_CACHE_TTL = 600
POWER_OUTAGE_CACHE = { 'expires_at': 0, 'body': None, 'refreshing': False, 'last_error': None }
POWER_OUTAGE_CACHE_LOCK = threading.Lock()
GEOCODE_CACHE = {}
GEOCODE_CACHE_LOCK = threading.Lock()
GEOCODE_CACHE_MAX_SIZE = 2048
GEOCODE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8)
PULSEPOINT_AGENCY_CACHE_TTL = 24 * 60 * 60
PULSEPOINT_AGENCY_CACHE = {'expires_at': 0, 'agencies': None}
PULSEPOINT_AGENCY_CACHE_LOCK = threading.Lock()
TAMPA_FIRE_GRID_CACHE = {}
TAMPA_FIRE_GRID_CACHE_LOCK = threading.Lock()
TAMPA_FIRE_GRID_CACHE_MAX_SIZE = 256
REGISTRY_CACHE_TTL = 86400
REGISTRY_CACHE_NEGATIVE_TTL = 21600
REGISTRY_CACHE_RATE_LIMIT_TTL = 300
REGISTRY_CACHE = {}
REGISTRY_CACHE_LOCK = threading.Lock()
REGISTRY_CACHE_MAX_SIZE = 4096
RATE_LIMIT_WINDOW = 60
RATE_LIMITS = {
    '/stream/': 240,
    '/camera-snapshot/': 180,
    '/northern-ireland-camera/': 180,
    '/511/': 180,
    '/fl511/': 180,
    '/tile': 1200,
    '/radar-tile': 1200,
    '/naip-tile': 600,
    '/video-token': 20,
    '/emergency': 10,
    '/international-emergency': 10,
    '/power-outages': 10,
    '/aircraft': 90,
    '/aircraft/track': 12,
    '/radio/stations': 12,
    '/radio/click': 30,
    '/cyclone-guidance': 20,
    '/trip-route': 20,
    '/cyber': 30,
    '/sensors': 10,
    '/international-sensor-sample': 10,
    '/lpr': 30,
    '/temperature-stations': 10,
}
TEMPERATURE_CACHE_TTL = 300  # 5 minutes — matches IEM update cadence
TEMPERATURE_CACHE = { 'expires_at': 0, 'body': None, 'refreshing': False, 'last_error': None }
TEMPERATURE_CACHE_LOCK = threading.Lock()
SENSOR_CACHE_TTL = 300
SENSOR_CACHE = { 'expires_at': 0, 'body': None, 'last_error': None }
SENSOR_CACHE_LOCK = threading.Lock()
ALGO_API_VERSION = 'v4.0'
ALGO_CACHE_TTL = 60
ALGO_TRAFFIC_CACHE = {}
ALGO_TRAFFIC_CACHE_LOCK = threading.Lock()
MDOT_CACHE_TTL = 60
MDOT_TRAFFIC_CACHE = {}
MDOT_TRAFFIC_CACHE_LOCK = threading.Lock()
ITERIS_CACHE_TTL = 60
ITERIS_TRAFFIC_CACHE = {}
ITERIS_TRAFFIC_CACHE_LOCK = threading.Lock()
ITERIS_TRAFFIC_CACHE_MAX_SIZE = 64
EXTENDED_TRAFFIC_CACHE_TTL = 60
EXTENDED_TRAFFIC_CACHE = {}
EXTENDED_TRAFFIC_CACHE_LOCK = threading.Lock()
DRIVEBC_CAMERAS_URL = 'https://www.drivebc.ca/api/webcams/'
DRIVEBC_EVENTS_URL = 'https://api.open511.gov.bc.ca/events?format=json&status=ACTIVE&limit=500'
DRIVEBC_SOURCE_URL = 'https://www.drivebc.ca/'
DRIVENWT_SOURCE_URL = 'https://drivenwt.ca/'
DRIVENWT_DATA_PATH_URL = (
    'https://drivenwt.ca/Home/GetJsonDataPath?installationIndex=0'
)
QUEBEC_511_SOURCE_URL = 'https://www.quebec511.info/'
QUEBEC_CAMERAS_URL = (
    'https://ws.mapserver.transports.gouv.qc.ca/swtq?service=wfs&version=2.0.0&'
    'request=getfeature&typename=ms:infos_cameras&outfile=Camera&srsname=EPSG:4326&'
    'outputformat=geojson'
)
QUEBEC_CONSTRUCTION_URL = (
    'https://ws.mapserver.transports.gouv.qc.ca/swtq?service=wfs&version=2.0.0&'
    'request=getfeature&typename=ms:chantiers_mtmdet&outfile=TravauxRoutiers&'
    'srsname=EPSG:4326&outputformat=geojson'
)
GOAKAMAI_SOURCE_URL = 'https://goakamai.org/'
GOAKAMAI_CAMERAS_URL = 'https://a.cameraservice.goakamai.org/cameras?format=mapPage'
HAWAII_LANE_CLOSURES_URL = (
    'https://services.arcgis.com/HQ0xoN0EzDPBOEci/arcgis/rest/services/'
    'Lane_Closure_WFL1_View_NoEd/FeatureServer/0/query'
)
ECCC_WEATHER_ALERTS_URL = (
    'https://api.weather.gc.ca/collections/weather-alerts/items?f=json&limit=1000'
)
ONTARIO_511_EVENTS_URL = 'https://511on.ca/api/v2/get/event?format=json'
COTRIP_API_ROOT = 'https://api-511x-co.carsprogram.org'
COTRIP_SOURCE_URL = 'https://www.cotrip.org/'
COTRIP_CACHE_TTL = 60
COTRIP_TRAFFIC_CACHE = {}
COTRIP_TRAFFIC_CACHE_LOCK = threading.Lock()
MIDRIVE_ROOT = 'https://mdotjboss.state.mi.us/MiDrive/'
MIDRIVE_SOURCE_URL = 'https://www.michigan.gov/drive'
MIDRIVE_CACHE_TTL = 60
MIDRIVE_TRAFFIC_CACHE = {}
MIDRIVE_TRAFFIC_CACHE_LOCK = threading.Lock()
WY511_SOURCE_URL = 'https://map.wyoroad.info/511-map/'
WY511_CACHE_TTL = 60
WY511_TRAFFIC_CACHE = {}
WY511_TRAFFIC_CACHE_LOCK = threading.Lock()
WY511_XOR_KEY = b'EkhJp6wsgahsqkiw5nahFOSCwAND1zhZ'
WY511_FEED_URLS = {
    'Cameras': 'https://map.wyoroad.info/wti511map-data/Msg-FFBK373B.pbf',
    'MessageSigns': 'https://map.wyoroad.info/wti511map-data/Msg-esqtdRBe.pbf',
    'MessageSignReports': 'https://map.wyoroad.info/wti511map-data/Msg-S4ZFc9MH.pbf',
    'Incidents': 'https://map.wyoroad.info/wti511map-data/Msg-pPMjIHzPfU.pbf',
    'Construction': 'https://map.wyoroad.info/511-map-data/Msg-whSwRZYH.pbf',
}
MT511_DATA_ROOT = 'https://mt.cdn.iteris-atis.com/geojson/icons/metadata/'
MT511_SOURCE_URL = 'https://www.511mt.net/'
MT511_CACHE_TTL = 60
MT511_TRAFFIC_CACHE = {}
MT511_TRAFFIC_CACHE_LOCK = threading.Lock()
NDROADS_SOURCE_URL = 'https://travel.dot.nd.gov/'
NDROADS_CAMERAS_URL = 'https://travelfiles.dot.nd.gov/geojson_nc/cameras.json'
NDROADS_ALERTS_URL = 'https://travelfiles.dot.nd.gov/geojson_nc/alerts.json'
NDROADS_ESS_URL = 'https://travelfiles.dot.nd.gov/geojson_nc/ess.json'
NDROADS_WORK_ZONES_ROOT = (
    'https://gis.dot.nd.gov/ArcGIS/rest/services/external/rcrs_dynamic/MapServer'
)
NDROADS_CACHE_TTL = 60
NDROADS_TRAFFIC_CACHE = {}
NDROADS_TRAFFIC_CACHE_LOCK = threading.Lock()
SD511_DATA_ROOT = 'https://sd.cdn.iteris-atis.com/geojson/icons/metadata/'
SD511_SOURCE_URL = 'https://www.sd511.org/'
SD511_CACHE_TTL = 60
SD511_TRAFFIC_CACHE = {}
SD511_TRAFFIC_CACHE_LOCK = threading.Lock()
CARS511_CACHE_TTL = 60
CARS511_CACHE = {}
CARS511_CACHE_LOCK = threading.Lock()
CARS511_CONFIGS = {
    'NE': {
        'source_url': 'https://www.511.nebraska.gov/',
        'graphql_url': 'https://www.511.nebraska.gov/api/graphql',
        'api_root': 'https://netg.carsprogram.org',
        'snapshot_hosts': {'dot511.nebraska.gov'},
    },
    'KS': {
        'source_url': 'https://www.kandrive.gov/',
        'graphql_url': 'https://www.kandrive.gov/api/graphql',
        'api_root': 'https://kstg.carsprogram.org',
        'snapshot_hosts': {'kscam.carsprogram.org', 'www.kcscout.net'},
    },
}
CARS511_EVENT_QUERY = '''
query SearchBounds($north: Float!, $south: Float!, $east: Float!, $west: Float!, $slugs: [String!]!) {
  searchBoundsQuery(n: $north, s: $south, e: $east, w: $west, layerSlugs: $slugs) {
    results {
      __typename uri title cityReference bbox
      ... on Event { priority }
    }
    error { message type }
  }
}
'''
SC511_CACHE_TTL = 60
SC511_TRAFFIC_CACHE = {}
SC511_TRAFFIC_CACHE_LOCK = threading.Lock()
SC511_METADATA_ROOT = 'https://sc.cdn.iteris-atis.com/geojson/icons/metadata'
SC511_DATA_ROOT = 'https://sc.cdn.iteris-atis.com/geojson/icons/data'
SC511_LAYER_FILES = {
    'Cameras': 'icons.cameras.geojson',
    'MessageSigns': 'icons.dms.geojson',
    'Incidents': 'icons.incident.geojson',
    'Construction': 'icons.construction.geojson',
}
TDOT_API_BASE = 'https://www.tdot.tn.gov/opendata/api/public/'
TDOT_API_KEY = os.getenv('AMERICAMAP_TDOT_API_KEY', '').strip()
TDOT_CACHE_TTL = 60
TDOT_TRAFFIC_CACHE = {}
TDOT_TRAFFIC_CACHE_LOCK = threading.Lock()
TDOT_LAYER_RESOURCES = {
    'Cameras': 'RoadwayCameras',
    'MessageSigns': 'RoadwayMessageSigns',
    'Incidents': 'RoadwayIncidents',
    'SevereImpact': 'RoadwaySevereImpact',
    'Construction': 'RoadwayOperations',
}
GOKY_CACHE_TTL = 60
GOKY_TRAFFIC_CACHE = {}
GOKY_TRAFFIC_CACHE_LOCK = threading.Lock()
GOKY_FIRESTORE_URL = (
    'https://firestore.googleapis.com/v1/projects/kytc-goky/'
    'databases/(default)/documents/realtime'
)
GOKY_API_KEY = os.getenv('AMERICAMAP_GOKY_API_KEY', '').strip()
GOKY_KYTC_CAMERAS_URL = (
    'https://services2.arcgis.com/CcI36Pduqd0OR4W9/ArcGIS/rest/services/'
    'trafficCamerasCur_Prd/FeatureServer/0/query'
)
GOKY_FAYETTE_CAMERAS_URL = (
    'https://services1.arcgis.com/Mg7DLdfYcSWIaDnu/ArcGIS/rest/services/'
    'Traffic_Camera_Locations_Public_view/FeatureServer/0/query'
)
VDOT_CACHE_TTL = 60
VDOT_TRAFFIC_CACHE = {}
VDOT_TRAFFIC_CACHE_LOCK = threading.Lock()
VDOT_TRAFFIC_URLS = {
    'Cameras': 'https://511.vdot.virginia.gov/services/map/array/cameras',
    'MessageSigns': 'https://data.511-atis-ttrip-prod.iteriscloud.com/datasets/dms/dms_active.geojson',
    'IncidentsMinor': 'https://data.511-atis-ttrip-prod.iteriscloud.com/datasets/incidentUnfiltered/minor_incidents.geojson',
    'IncidentsMajor': 'https://data.511-atis-ttrip-prod.iteriscloud.com/datasets/incidentUnfiltered/major_incidents.geojson',
    'Construction': 'https://data.511-atis-ttrip-prod.iteriscloud.com/datasets/eventUnfiltered/active_construction.geojson',
}
WV511_CACHE_TTL = 60
WV511_TRAFFIC_CACHE = {}
WV511_TRAFFIC_CACHE_LOCK = threading.Lock()
WV511_CAMERA_URL = 'https://www.wv511.org/wsvc/gmap.asmx/buildCamerasJSONjs'
WV511_CAMERA_PLAYER_URL = 'https://www.wv511.org/flowplayeri.aspx?CAMID={camera_code}'
WV511_KML_URLS = {
    'MessageSignsActive': 'https://www.wv511.org/wsvc/gmap.asmx/buildDMSKML?isActive=1',
    'MessageSignsInactive': 'https://www.wv511.org/wsvc/gmap.asmx/buildDMSKML?isActive=0',
    'Incidents': 'https://www.wv511.org/wsvc/gmap.asmx/buildEventsKMLi_Filtered?CategoryIDs=&SeverityIDs=&is511Only=',
    'Construction': 'https://www.wv511.org/wsvc/gmap.asmx/buildPlannedEventsActiveKML',
}
MDCHART_CACHE_TTL = 60
MDCHART_TRAFFIC_CACHE = {}
MDCHART_TRAFFIC_CACHE_LOCK = threading.Lock()
MDCHART_TRAFFIC_URLS = {
    'Cameras': 'https://chartexp1.sha.maryland.gov/CHARTExportClientService/getCameraMapDataJSON.do',
    'MessageSigns': 'https://chartexp1.sha.maryland.gov/CHARTExportClientService/getDMSMapDataJSON.do',
    'Incidents': 'https://chartexp1.sha.maryland.gov/CHARTExportClientService/getEventMapDataJSON.do',
    'Construction': 'https://chartexp1.sha.maryland.gov/CHARTExportClientService/getActiveClosureMapDataJSON.do',
}
DCGIS_CACHE_TTL = 60
DCGIS_TRAFFIC_CACHE = {}
DCGIS_TRAFFIC_CACHE_LOCK = threading.Lock()
DCGIS_TRAFFIC_URLS = {
    'Construction': (
        'https://maps2.dcgis.dc.gov/dcgis/rest/services/'
        'FEEDS/DDOT/FeatureServer/12/query'
    ),
}
DC_TRAFFICVIEW_CCTV_URL = 'https://www.trafficview.org/map/accessGeoData.php'
DC_TRAFFICVIEW_THUMBNAIL_ROOT = 'https://cctv.trafficview.org/thumbnail'
DELDOT_CACHE_TTL = 60
DELDOT_TRAFFIC_CACHE = {}
DELDOT_TRAFFIC_CACHE_LOCK = threading.Lock()
DELDOT_TRAFFIC_URLS = {
    'Cameras': 'https://tmc.deldot.gov/json/videocamera-internal.json?id=VKAM',
    'Advisories': 'https://tmc.deldot.gov/json/advisory.json?id=VKAM',
    'Restrictions': 'https://tmc.deldot.gov/json/restriction.json?id=VKAM',
    'MessageSigns': 'https://tmc.deldot.gov/json/vmsg-vms.json',
    'WeatherStations': 'https://tmc.deldot.gov/json/weatherstation.json?id=VKAM',
}
DELDOT_SOURCE_URL = 'https://tmc.deldot.gov/datamap/'
PA511_CACHE_TTL = 60
PA511_TOOLTIP_CACHE_TTL = 90
PA511_TRAFFIC_CACHE = {}
PA511_TRAFFIC_CACHE_LOCK = threading.Lock()
PA511_TOOLTIP_CACHE = {}
PA511_TOOLTIP_CACHE_LOCK = threading.Lock()
PA511_SOURCE_URL = 'https://www.511pa.com/'
PA511_LAYER_SOURCES = {
    'Cameras': ('Cameras',),
    'MessageSigns': ('MessageSigns',),
    'Incidents': (
        'MajorRouteIncident',
        'OtherRouteIncident',
        'MajorRouteClosure',
        'OtherRouteClosure',
    ),
    'Construction': ('ActiveRoadwork', 'TurnpikePlannedRoadwork'),
}
PA511_LAYER_LABELS = {
    'MajorRouteIncident': 'Major-route incident',
    'OtherRouteIncident': 'Traffic incident',
    'MajorRouteClosure': 'Major-route closure',
    'OtherRouteClosure': 'Road closure',
    'ActiveRoadwork': 'Active roadwork',
    'TurnpikePlannedRoadwork': 'Turnpike roadwork',
}
NJTA_CACHE_TTL = 60
NJTA_TRAFFIC_CACHE = {}
NJTA_TRAFFIC_CACHE_LOCK = threading.Lock()
NJTA_CAMERA_URL = 'https://www.njta.gov/travel-resources/camera-list/'
NJTA_ALERTS_URL = 'https://www.njta.gov/wp-json/njta/v1/alerts'
NJTA_SOURCE_URL = 'https://www.njta.gov/travel-resources/travel-alert/'
CTROADS_CACHE_TTL = 60
CTROADS_TOOLTIP_CACHE_TTL = 90
CTROADS_TRAFFIC_CACHE = {}
CTROADS_TRAFFIC_CACHE_LOCK = threading.Lock()
CTROADS_TOOLTIP_CACHE = {}
CTROADS_TOOLTIP_CACHE_LOCK = threading.Lock()
CTROADS_SOURCE_URL = 'https://www.ctroads.org/map'
CTROADS_LAYER_SOURCES = {
    'Cameras': ('Cameras',),
    'MessageSigns': ('MessageSigns',),
    'Incidents': ('Incidents', 'Closures', 'TransitIncidents'),
    'Construction': ('Construction', 'TransitConstruction', 'ConstructionProjects'),
}
CTROADS_LAYER_LABELS = {
    'Incidents': 'Traffic incident',
    'Closures': 'Road closure',
    'TransitIncidents': 'Transit incident',
    'Construction': 'Roadwork',
    'TransitConstruction': 'Transit construction',
    'ConstructionProjects': 'Construction project',
}
RIDOT_CACHE_TTL = 60
RIDOT_TRAFFIC_CACHE = {}
RIDOT_TRAFFIC_CACHE_LOCK = threading.Lock()
RIDOT_SOURCE_URL = 'https://www.dot.ri.gov/travel/'
RIDOT_GIS_ROOT = 'https://gisprod.dot.ri.gov/scp/rest/services/TMC_ITS_Assets/MapServer'
RIDOT_CAMERA_PAGES = (
    'cameras_metro.php',
    'cameras_eastbay.php',
    'cameras_ncounty.php',
    'cameras_bstonenorth.php',
    'cameras_scounty.php',
    'cameras_westbay.php',
)
RIDOT_DMS_URL = 'https://www.dot.ri.gov/travel/data/test/test_TTSigns_Edit.php'
RIDOT_INCIDENT_URL = 'https://www.dot.ri.gov/travel/current_advisory.php'
RIDOT_ADVISORY_URL = 'https://www.dot.ri.gov/travel/traveladvisories.php'
# The public RIDOT travel-time table is ordered consistently but does not carry
# device IDs. These are its corresponding official GIS equipment IDs.
RIDOT_DMS_TRAVEL_TIME_EQUIPMENT_IDS = (
    852, 666, 96, 97, 98, 99, 101, 106, 103, 579,
    667, 671, 676, 677, 674, 850, 675, 108, 104,
)
MASS511_CACHE_TTL = 60
MASS511_TRAFFIC_CACHE = {}
MASS511_TRAFFIC_CACHE_LOCK = threading.Lock()
MASS511_SOURCE_URL = 'https://www.mass511.com/'
MASS511_CAMERA_URL = 'https://matg.carsprogram.org/cameras_v1/api/cameras'
MASS511_SIGN_URL = 'https://matg.carsprogram.org/signs_v1/api/signs'
MASS511_GRAPHQL_URL = 'https://www.mass511.com/api/graphql'
MASS511_SEARCH_QUERY = '''
query SearchBounds($slugs: [String!]!) {
  searchBoundsQuery(n: 43, s: 41, e: -69.8, w: -73.6, layerSlugs: $slugs) {
    results {
      __typename
      uri
      title
      cityReference
      bbox
      location { primaryLinearReference secondaryLinearReference }
      ... on Event { description priority features { id geometry properties } }
    }
    error { message type }
  }
}
'''
NEW_ENGLAND_511_CACHE_TTL = 60
NEW_ENGLAND_511_TOOLTIP_CACHE_TTL = 90
NEW_ENGLAND_511_TRAFFIC_CACHE = {}
NEW_ENGLAND_511_TRAFFIC_CACHE_LOCK = threading.Lock()
NEW_ENGLAND_511_TOOLTIP_CACHE = {}
NEW_ENGLAND_511_TOOLTIP_CACHE_LOCK = threading.Lock()
NEW_ENGLAND_511_SOURCE_URL = 'https://www.newengland511.org/'
NEW_ENGLAND_511_LAYER_SOURCES = {
    'MessageSigns': ('MessageSigns',),
    'Incidents': ('Incidents', 'IncidentClosures'),
    'Construction': (
        'Construction',
        'ConstructionClosures',
        'FutureRoadwork',
        'FutureConstructionClosure',
    ),
}
NEW_ENGLAND_511_LAYER_LABELS = {
    'MessageSigns': 'Message sign',
    'Incidents': 'Traffic incident',
    'IncidentClosures': 'Road closure',
    'Construction': 'Roadwork',
    'ConstructionClosures': 'Roadwork closure',
    'FutureRoadwork': 'Future roadwork',
    'FutureConstructionClosure': 'Future roadwork closure',
}
NEW_ENGLAND_511_CAMERA_AGENCIES = {
    'NH': 'NHDOT',
    'VT': 'VTrans',
    'ME': 'MaineDOT',
}
NY511_CACHE_TTL = 60
NY511_TOOLTIP_CACHE_TTL = 90
NY511_TRAFFIC_CACHE = {}
NY511_TRAFFIC_CACHE_LOCK = threading.Lock()
NY511_TOOLTIP_CACHE = {}
NY511_TOOLTIP_CACHE_LOCK = threading.Lock()
NY511_SOURCE_URL = 'https://www.511ny.org/'
NY511_LAYER_SOURCES = {
    'MessageSigns': ('MessageSigns',),
    'Incidents': ('Incidents', 'IncidentClosures'),
    'Construction': (
        'Construction',
        'ConstructionClosures',
        'FutureRoadwork',
        'FutureConstructionClosure',
    ),
}
NY511_LAYER_LABELS = {
    'MessageSigns': 'Message sign',
    'Incidents': 'Traffic incident',
    'IncidentClosures': 'Road closure',
    'Construction': 'Roadwork',
    'ConstructionClosures': 'Roadwork closure',
    'FutureRoadwork': 'Future roadwork',
    'FutureConstructionClosure': 'Future roadwork closure',
}
OHGO_CACHE_TTL = 60
OHGO_API_ROOT = 'https://api.ohgo.com'
OHGO_SOURCE_URL = 'https://www.ohgo.com/'
OHGO_TRAFFIC_CACHE = {}
OHGO_TRAFFIC_CACHE_LOCK = threading.Lock()
OHGO_LAYER_RESOURCES = {
    'Cameras': 'cameras',
    'MessageSigns': 'digital-signs',
    'Incidents': 'incidents',
    'Construction': 'construction',
}
OHGO_CAMERA_HOST_SUFFIXES = (
    'itscameras.dot.state.oh.us',
    'trimarc.org',
)
INDOT_TRAFFICWISE_CACHE_TTL = 60
INDOT_TRAFFICWISE_CACHE = {}
INDOT_TRAFFICWISE_CACHE_LOCK = threading.Lock()
INDOT_TRAFFICWISE_SOURCE_URL = 'https://511in.org/'
INDOT_TRAFFICWISE_GRAPHQL_URL = 'https://511in.org/api/graphql'
INDOT_TRAFFICWISE_CAMERA_QUERY = '''
query SearchBounds($slugs: [String!]!) {
  searchBoundsQuery(n: 42, s: 37.7, e: -84.7, w: -88.2, layerSlugs: $slugs) {
    cameraViews {
      title category uri url
      sources { type src }
      parentCollection { uri bbox }
    }
    error { message type }
  }
}
'''
INDOT_TRAFFICWISE_SIGN_QUERY = '''
query SearchBounds($slugs: [String!]!) {
  searchBoundsQuery(n: 42, s: 37.7, e: -84.7, w: -88.2, layerSlugs: $slugs) {
    results {
      __typename uri title bbox
      ... on Sign {
        signDisplayType signStatus
        views(limit: 5, orderBy: NEAREST_ASC) {
          __typename uri category title
          ... on SignComboView { textLines imageUrl }
          ... on SignTextView { textLines }
          ... on SignImageView { imageUrl }
          ... on SignOverlayView { imageUrl travelTimes }
          ... on SignOverlayTPIMView { textLines imageUrl }
        }
      }
    }
    error { message type }
  }
}
'''
INDOT_TRAFFICWISE_EVENT_QUERY = '''
query SearchBounds($slugs: [String!]!) {
  searchBoundsQuery(n: 42, s: 37.7, e: -84.7, w: -88.2, layerSlugs: $slugs) {
    results {
      __typename uri title cityReference bbox
      location { primaryLinearReference secondaryLinearReference }
      ... on Event { description priority features { id geometry properties } }
    }
    error { message type }
  }
}
'''
INDOT_TRAFFICWISE_WEATHER_QUERY = '''
query SearchBounds($slugs: [String!]!) {
  searchBoundsQuery(n: 42, s: 37.7, e: -84.7, w: -88.2, layerSlugs: $slugs) {
    results {
      __typename uri title bbox
      ... on Station {
        lastUpdated { timestamp timezone }
        status description weatherStationFields
        drivingConditions { title description icon color lastUpdated { timestamp timezone } }
        features { id geometry properties }
      }
    }
    error { message type }
  }
}
'''
MNDOT_CARS_CACHE_TTL = 60
MNDOT_CARS_CACHE = {}
MNDOT_CARS_CACHE_LOCK = threading.Lock()
MNDOT_CARS_SOURCE_URL = 'https://511mn.org/'
MNDOT_CARS_GRAPHQL_URL = 'https://511mn.org/api/graphql'
MNDOT_CARS_CAMERAS_URL = 'https://mntg.carsprogram.org/cameras_v1/api/cameras'
MNDOT_CARS_SIGNS_URL = 'https://mntg.carsprogram.org/signs_v1/api/signs'
MNDOT_CARS_EVENT_QUERY = '''
query MapFeatures($input: MapFeaturesArgs!) {
  mapFeaturesQuery(input: $input) {
    mapFeatures {
      __typename uri title tooltip bbox priority
      features { id geometry properties type }
    }
    error { message type }
  }
}
'''
MNDOT_CARS_WEATHER_QUERY = '''
query SearchBounds($slugs: [String!]!) {
  searchBoundsQuery(n: 49.39, s: 43.49, e: -89.49, w: -97.24, layerSlugs: $slugs) {
    results {
      __typename uri title bbox
      ... on Station {
        lastUpdated { timestamp timezone }
        status description weatherStationFields
      }
    }
    error { message type }
  }
}
'''
IADOT_CACHE_TTL = 60
IADOT_CACHE = {}
IADOT_CACHE_LOCK = threading.Lock()
IADOT_SOURCE_URL = 'https://iowadot.gov/travel-tools/iowa-511'
IADOT_CAMERA_SOURCE_URL = 'https://data.iowadot.gov/datasets/IowaDOT::traffic-cameras-3/about'
IADOT_TRAFFIC_URLS = {
    'Cameras': (
        'https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/'
        'Traffic_Cameras_View/FeatureServer/0/query'
    ),
    'Events': (
        'https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/'
        'CARS511_Iowa_View/FeatureServer/0/query'
    ),
    'SignsActive': (
        'https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/'
        'DMS_View/FeatureServer/0/query'
    ),
    'SignsInactive': (
        'https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/'
        'DMS_View/FeatureServer/1/query'
    ),
}
MODOT_CACHE_TTL = 60
MODOT_CACHE = {}
MODOT_CACHE_LOCK = threading.Lock()
MODOT_SOURCE_URL = 'https://traveler.modot.org/map/'
MODOT_STREAMING_CAMERAS_URL = (
    'https://traveler.modot.org/timconfig/feed/desktop/StreamingCams2.json'
)
MODOT_SNAPSHOT_CAMERAS_URL = 'https://traveler.modot.org/map/js/snapshot.json'
MODOT_SIGNS_URL = 'https://traveler.modot.org/timconfig/feed/desktop/MsgBrdV1.json'
MODOT_EVENTS_URL = 'https://traveler.modot.org/timconfig/feed/desktop/message.v2.json'
OKTRAFFIC_CACHE_TTL = 60
OKTRAFFIC_CACHE = {}
OKTRAFFIC_CACHE_LOCK = threading.Lock()
OKTRAFFIC_API_ROOT = 'https://oktraffic.org/api'
OKTRAFFIC_SOURCE_URL = 'https://oktraffic.org/'
DRIVETEXAS_CACHE_TTL = 60
DRIVETEXAS_CACHE = {}
DRIVETEXAS_CACHE_LOCK = threading.Lock()
DRIVETEXAS_API_URL = 'https://dtx-e-cdn.maplarge.com/Api/ProcessDirect'
DRIVETEXAS_SOURCE_URL = 'https://drivetexas.org/'
NMROADS_CACHE_TTL = 60
NMROADS_CACHE = {}
NMROADS_CACHE_LOCK = threading.Lock()
NMROADS_API_ROOT = 'https://servicev5.nmroads.com/RealMapWAR'
NMROADS_LIVE_API_ROOT = 'https://servicev4admin.nmroads.com/RealMapWAR'
NMROADS_SOURCE_URL = 'https://nmroads.com/'
CALTRANS_QUICKMAP_CACHE_TTL = 180
CALTRANS_QUICKMAP_CACHE = {}
CALTRANS_QUICKMAP_CACHE_LOCK = threading.Lock()
CALTRANS_QUICKMAP_SOURCE_URL = 'https://quickmap.dot.ca.gov/'
CALTRANS_QUICKMAP_DATA_ROOT = 'https://quickmap.dot.ca.gov/data'
CALTRANS_QUICKMAP_LAYER_FILES = {
    'Cameras': ('v2_cctv.kml',),
    'MessageSigns': ('v2_cms.kml',),
    'Incidents': ('v2_chp-only.kml', 'v2_chin.kml'),
    'Construction': ('v2_lcs2way.kml', 'v2_lcspending.kml'),
}
NVROADS_CACHE_TTL = 90
NVROADS_TOOLTIP_CACHE_TTL = 180
NVROADS_TOOLTIP_CACHE = {}
NVROADS_TOOLTIP_CACHE_LOCK = threading.Lock()
NVROADS_SOURCE_URL = 'https://www.nvroads.com/'
NVROADS_LAYER_SOURCES = {
    'Cameras': ('Cameras',),
    'MessageSigns': ('MessageSigns',),
    'Incidents': ('Incidents', 'Closures', 'WazeIncidents', 'WazeClosures'),
    'Construction': ('Construction',),
}
NVROADS_LAYER_LABELS = {
    'Incidents': 'Nevada 511 incident',
    'Closures': 'Nevada 511 road closure',
    'WazeIncidents': 'Nevada 511 Waze incident',
    'WazeClosures': 'Nevada 511 Waze closure',
    'Construction': 'Nevada 511 construction',
}
TRIPCHECK_OR_CACHE_TTL = 120
TRIPCHECK_OR_CACHE = {}
TRIPCHECK_OR_CACHE_LOCK = threading.Lock()
TRIPCHECK_OR_SOURCE_URL = 'https://www.tripcheck.com/'
TRIPCHECK_OR_DATA_ROOT = 'https://www.tripcheck.com/Scripts/map/data'
TRIPCHECK_OR_DATASETS = {
    'Cameras': 'cctvinventory.js',
    'Incidents': 'INCD.js',
    'Construction': 'EVENT.js',
    'RoadWeather': 'RWIS.js',
}
TRIPCHECK_OR_CAMERA_ROOT = 'https://www.tripcheck.com/RoadCams/cams/'
WSDOT_CACHE_TTL = 120
WSDOT_CACHE = {}
WSDOT_CACHE_LOCK = threading.Lock()
WSDOT_SOURCE_URL = 'https://wsdot.com/travel/real-time/traffic-map'
WSDOT_DATA_ROOT = 'https://data.wsdot.wa.gov/arcgis/rest/services/TravelInformation'
WSDOT_DATASETS = {
    'Cameras': 'TravelInfoCamerasWeather/FeatureServer/0/query',
    'RoadWeather': 'TravelInfoCamerasWeather/FeatureServer/1/query',
    'Alerts': 'TravelInfoRoadAlerts/FeatureServer/0/query',
}
IDOT_IL_CACHE_TTL = 60
IDOT_IL_TRAFFIC_CACHE = {}
IDOT_IL_TRAFFIC_CACHE_LOCK = threading.Lock()
IDOT_IL_SOURCE_URL = 'https://idot.illinois.gov/travel-and-maps.html'
IDOT_IL_CAMERA_REFERER = 'https://travelmidwest.com/'
IDOT_IL_TRAFFIC_URLS = {
    'Cameras': (
        'https://services2.arcgis.com/aIrBD8yn1TDTEXoz/arcgis/rest/services/'
        'TrafficCamerasTM_Public/FeatureServer/0/query'
    ),
    'MessageSigns': (
        'https://services2.arcgis.com/aIrBD8yn1TDTEXoz/arcgis/rest/services/'
        'Dynamic_Messaging_Signs/FeatureServer/0/query'
    ),
    'Incidents': (
        'https://services2.arcgis.com/aIrBD8yn1TDTEXoz/arcgis/rest/services/'
        'Illinois_Roadway_Incidents/FeatureServer/0/query'
    ),
    'Construction': (
        'https://services2.arcgis.com/aIrBD8yn1TDTEXoz/arcgis/rest/services/'
        'Road_Construction_Public/FeatureServer/2/query'
    ),
    'RoadWeather': (
        'https://services2.arcgis.com/aIrBD8yn1TDTEXoz/arcgis/rest/services/'
        'RWIS/FeatureServer/0/query'
    ),
}
IDOT_IL_OBJECT_ID_FIELDS = {
    'Cameras': 'OBJECTID',
    'MessageSigns': 'FID',
    'Incidents': 'OBJECTID',
    'Construction': 'OBJECTID',
    'RoadWeather': 'OBJECTID',
}
STATE_BOUNDARY_GEOMETRY_CACHE = None
STATE_BOUNDARY_GEOMETRY_CACHE_LOCK = threading.Lock()
STATE_REFERENCE_POINT_CACHE = {}
STATE_REFERENCE_POINT_CACHE_LOCK = threading.Lock()
RATE_LIMIT_DEFAULT = 120
_RATE_LIMIT_STATE = {}
_RATE_LIMIT_LOCK = threading.Lock()
LOCAL_TZ = ZoneInfo('America/New_York')
PINELLAS_ACTIVITY_URL = 'https://911.pinellas.gov/files/Activity.json'
PINELLAS_SHERIFF_CALLS_URL = 'https://www.pinellassheriff.gov/ExternalSitePages/activecalls'
MARION_FIRE_CALLS_URL = 'https://bcc.marionfl.org/firecalls/activecadcalls.aspx'
MARTIN_FIRE_CALLS_URL = 'https://frd-scanner.martin.fl.us/frdcad.html'
MIAMI_DADE_FIRE_CALLS_URL = 'https://www.miamidade.gov/firecalls/calls.html'
JAX_SHERIFF_CALLS_URL = 'https://callsforservice.jaxsheriff.org/'
JAX_SHERIFF_MAX_CALLS = 40
TAMPA_FIRE_CALLS_URL = 'https://ncapps.tampagov.net/callsforservice/TFR/Json'
TAMPA_FIRE_GRID_QUERY_URL = 'https://arcgis.tampagov.net/arcgis/rest/services/OpenData/Fire/MapServer/0/query'
TAMPA_FIRE_RECENT_HOURS = 8
CLEARWATER_POLICE_CALLS_URL = 'https://apps.myclearwater.com/activecalls/api/ActiveCalls'
TOPS_ACTIVE_INCIDENTS_URL = (
    'https://utility.arcgis.com/usrsvcs/servers'
    '/b1081fc7268643e5ab3253fc9bc3e1a5/rest/services/Active_Incidents_TOPS/FeatureServer/0/query'
)
TOPS_REFERER = 'https://www.talgov.com/gis/tops/'
DUKE_NC_PUBLIC_OUTAGES_URL = (
    'https://services3.arcgis.com/oX5r75R7mapdoI2F/ArcGIS/rest/services/'
    'Duke_Energy_Distribution_Outages_Public/FeatureServer/0/query'
)
DUKE_NC_PUBLIC_SOURCE_URL = 'https://www.ncdps.gov/power-outages'
DUKE_OH_PUBLIC_SOURCE_URL = 'https://outagemaps.duke-energy.com/#/current-outages/ohky'
AES_OHIO_POWER_XML_URL = 'https://myprofile.aes-ohio.com/DATA/DPLOMSDATA.xml'
AES_OHIO_POWER_SOURCE_URL = 'https://myprofile.aes-ohio.com/Outages/Outages.html'
DUKE_IN_PUBLIC_SOURCE_URL = 'https://outagemaps.duke-energy.com/#/current-outages/in'
AES_INDIANA_POWER_XML_URL = 'https://myaccount.aesindiana.com/OMSDATA/OMSDATA_OSI.xml'
AES_INDIANA_POWER_SOURCE_URL = 'https://myaccount.aesindiana.com/outages/outagemap.html'
NIPSCO_POWER_OUTAGES_URL = 'https://www.nipsco.com/nisource-api/ldc/GetPowerOutages'
NIPSCO_POWER_SOURCE_URL = 'https://www.nipsco.com/outages/power-outages'
WE_ENERGIES_WI_OUTAGES_URL = (
    'https://www.we-energies.com/outagesummary/view/OutageEventJSON'
)
WE_ENERGIES_WI_SOURCE_URL = (
    'https://www.we-energies.com/outagesummary/view/outagegrid'
)
WPS_WI_OUTAGES_URL = (
    'https://www.wisconsinpublicservice.com/outagesummary/view/OutageEventJSON'
)
WPS_WI_SOURCE_URL = (
    'https://www.wisconsinpublicservice.com/outagesummary/view/outagegrid'
)
MGE_WI_OUTAGES_URL = (
    'https://mge-svc.smartcmobile.com/WidgetAPI/Outage/'
    'PreloginGetOutageData?isPlannedOutage=0'
)
MGE_WI_SOURCE_URL = 'https://mge.smartcmobile.com/Outage/'
XCEL_MN_OUTAGES_URL = (
    'https://emcs-gis.esriemcs.com/arcgis/rest/services/'
    'Xcel/XcelOutage/MapServer/3/query'
)
XCEL_MN_SOURCE_URL = 'https://www.outagemap-xcelenergy.com/outagemap/'
XCEL_CO_SOURCE_URL = 'https://co.my.xcelenergy.com/s/outage-safety/outage-map'
MINNESOTA_POWER_OUTAGES_URL = (
    'https://services.arcgis.com/ehV0YC56b0w2eenG/arcgis/rest/services/'
    'OutageMap2021AGOL_viewLayer/FeatureServer/1/query'
)
MINNESOTA_POWER_SOURCE_URL = 'https://mnpower.com/OutageCenter/OutageMap'
MIDAMERICAN_IA_OUTAGES_URL = (
    'https://www.midamericanenergy.com/OutageWatch/api/Incident/GetIncidentOutageData/'
)
MIDAMERICAN_IA_SOURCE_URL = 'https://www.midamericanenergy.com/OutageWatch/dsk.html'
IOWAREC_OUTAGE_DETAILS_URL = 'https://www.iowarec.org/outages/details'
IOWAREC_OUTAGE_COUNTIES_URL = (
    'https://www.iowarec.org/local/modules/outages/assets/maps/counties.geojson'
)
IOWAREC_OUTAGE_SOURCE_URL = 'https://www.iowarec.org/outages'
LES_NE_OUTAGES_URL = (
    'https://services2.arcgis.com/knPkBS6PAixcYX73/arcgis/rest/services/'
    'ResponderOutages57_view/FeatureServer/1/query'
)
LES_NE_OUTAGES_SOURCE_URL = 'https://www.les.com/outage-center'
OTTER_TAIL_OUTAGES_ROOT = 'https://outages.otpco.com/data'
OTTER_TAIL_OUTAGES_SOURCE_URL = 'https://outages.otpco.com/'
MDU_OUTAGES_ROOT = 'https://customer.montana-dakota.com/outage-map'
MDU_OUTAGES_SOURCE_URL = 'https://customer.montana-dakota.com/outage-map'
MDEM_POWER_OUTAGES_URL = (
    'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services/'
    'PowerOutage_CountiesGeneralDEV_Join/FeatureServer/0/query'
)
MDEM_POWER_SOURCE_URL = 'https://mdgeodata.md.gov/PowerOutages/'
PEMA_PA_POWER_OUTAGES_URL = (
    'https://services2.arcgis.com/xtuWQvb2YQnp0z3F/ArcGIS/rest/services/'
    'Pennsylvania_County_Power_Outage_Status/FeatureServer/0/query'
)
PEMA_PA_POWER_SOURCE_URL = (
    'https://services2.arcgis.com/xtuWQvb2YQnp0z3F/ArcGIS/rest/services/'
    'Pennsylvania_County_Power_Outage_Status/FeatureServer'
)
PEPCO_DC_API_BASE = 'https://phi-pepco.ifactornotifi.com/bpu/sc5'
PEPCO_DC_INSTANCE_ID = 'bac68083-1c42-44ee-bb3c-6d1c1c026f52'
PEPCO_DC_VIEW_ID = 'ebc719f2-1185-46c2-8bf6-d0a2876c537f'
PEPCO_DC_SOURCE_URL = 'https://outagemap.pepco.com/'
DELMARVA_API_BASE = 'https://phi-delmar.ifactornotifi.com/bpu/sc5'
DELMARVA_INSTANCE_ID = 'b3e5379d-7d75-4ea0-a978-b42bab577210'
DELMARVA_VIEW_ID = 'd538c2a7-4991-4b49-8b87-02012114893a'
DELMARVA_SOURCE_URL = 'https://outagemap.delmarva.com/'
EVERSOURCE_CT_DATA_ROOT = (
    'https://outagemap.eversource.com/resources/data/external/interval_generation_data'
)
EVERSOURCE_CT_SOURCE_URL = 'https://outagemap.eversource.com/external/default.html'
NHEC_POWER_DATA_ROOT = 'https://outagemap-data.cloud.coop/nhec/Hosted_Outage_Map'
NHEC_POWER_SOURCE_URL = 'https://nhec.outagemap.coop/'
VTOUTAGES_API_ROOT = 'https://api.vtoutages.com/outage'
VTOUTAGES_SOURCE_URL = 'https://vtoutages.org/'
VTRANS_TOWNS_QUERY_URL = (
    'https://maps.vtrans.vermont.gov/arcgis/rest/services/'
    'VTrans511/511lookup/FeatureServer/16/query'
)
RIE_POWER_API_ROOT = 'https://outagemap.rienergy.com/OMAP/api/Omap'
RIE_POWER_SOURCE_URL = 'https://outagemap.rienergy.com/'
CONED_POWER_DATA_ROOT = 'https://outagemap.coned.com/resources/data/external/interval_generation_data'
CONED_POWER_SOURCE_URL = 'https://outagemap.coned.com/external/default.html'
ORU_POWER_DATA_ROOT = 'https://outagemap.oru.com/resources/data/external/interval_generation_data'
ORU_POWER_SOURCE_URL = 'https://outagemap.oru.com/external/default.html'
TECO_POWER_CONFIG_URL = 'https://outage-data-prod-hrcadje2h9aje9c9.a03.azurefd.net/api/v1/config'
TECO_POWER_TILES_URL = 'https://outage-data-prod-hrcadje2h9aje9c9.a03.azurefd.net/api/v1/outage-tiles'
TECO_POWER_SOURCE_URL = 'https://www.tampaelectric.com/poweroutages/'
KEYS_POWER_SOURCE_URL = 'https://powerstatus.keysenergy.com/'
KEYS_POWER_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': KEYS_POWER_SOURCE_URL,
    'Accept': 'application/json,text/plain,*/*',
}
KEYS_POWER_SUMMARY_URL = urllib.parse.urljoin(KEYS_POWER_SOURCE_URL, 'data/outageSummary.json')
KEYS_POWER_OUTAGES_URL = urllib.parse.urljoin(KEYS_POWER_SOURCE_URL, 'data/outages.json')
KEYS_POWER_POLYGONS_URL = urllib.parse.urljoin(KEYS_POWER_SOURCE_URL, 'data/outagePolygons.json')
KUBRA_POWER_PROVIDERS = {
    'jea': {
        'name': 'JEA',
        'instance_id': '2bb4315d-ff9d-4937-a231-57b8a9df189c',
        'view_id': '40a074f2-b303-42b7-b717-ed7d9d2ad9e2',
        'source_url': 'https://www.jea.com/Outage_Center/Outage_Map/',
    },
    'lakeland': {
        'name': 'Lakeland Electric',
        'instance_id': 'ea5d4449-04f0-4511-ba9e-c96762eda641',
        'view_id': 'f362d0d7-4493-4d66-bdd5-6b66609744b7',
        'source_url': 'https://outagemap.lakelandelectric.com/',
    },
    'ouc': {
        'name': 'OUC',
        'instance_id': 'b26c1223-ffac-4e60-b9de-402fbc33f9e4',
        'view_id': '535dc59f-6e00-445d-a855-73c700c6c451',
        'source_url': 'https://www.ouc.com/customer-service/outage-map/',
    },
    'seco': {
        'name': 'SECO Energy',
        'instance_id': 'c63333d7-dc3c-4fb1-b874-461216e528bf',
        'view_id': '0730fe50-c114-4eb0-97a9-3553ab8421cd',
        'source_url': 'https://stormcenter.secoenergy.com/',
    },
    'georgia_power': {
        'name': 'Georgia Power',
        'instance_id': '7b38c047-7950-444b-a25c-9b3e5ab986eb',
        'view_id': '67b44af5-3847-4ca3-9f4e-9190aac343d6',
        'source_url': 'https://outagemap.georgiapower.com/',
    },
    'alabama_power': {
        'name': 'Alabama Power',
        'instance_id': '7636a60f-7b81-4fb0-a30d-ed79a8e271e7',
        'view_id': 'c3471c92-6e1b-494b-a884-391139a2cc18',
        'source_url': 'https://outagemap.alabamapower.com/',
    },
    'mississippi_power': {
        'name': 'Mississippi Power',
        'instance_id': '32c5f878-d103-49e1-8a8a-ad0e2bdb4507',
        'view_id': '1521bb54-d36b-4d36-a64d-48a454ee39a0',
        'source_url': 'https://outagemap.mississippipower.com/?c=ExternalVisibility&o=ViewbyCounty',
    },
    'dominion_sc': {
        'name': 'Dominion Energy South Carolina',
        'instance_id': 'ba976967-dc3e-4d05-8206-50d0260b22c3',
        'view_id': '5b77d88d-a054-4008-a168-0e13468a4d6d',
        'source_url': 'https://outagemap.dominionenergysc.com/?c=VisibilityConfig&o=ViewByCounty',
    },
    'lge_ku': {
        'name': 'LG&E and Kentucky Utilities',
        'instance_id': '877fd1e9-4162-473f-b782-d8a53a85326b',
        'view_id': 'a6cee9e4-312b-4b77-9913-2ae371eb860d',
        'source_url': 'https://stormcenter.lge-ku.com/',
    },
    'dominion_va': {
        'name': 'Dominion Energy Virginia',
        'instance_id': '9c691bb6-767e-4532-b00e-286ac9adc223',
        'view_id': '38b5394c-8bca-4dfd-ac59-b321615446bd',
        'source_url': 'https://outagemap.dominionenergy.com/external/default.html?lv=true',
    },
    'appalachian_power': {
        'name': 'Appalachian Power',
        'instance_id': '6674f49e-0236-4ed8-a40a-b31747557ab7',
        'view_id': '8cfe790f-59f3-4ce3-a73f-a9642227411f',
        'source_url': 'https://outagemap.appalachianpower.com/?c=External&o=District',
    },
    'national_grid_ma': {
        'name': 'National Grid Massachusetts',
        'instance_id': '9cb2e5b7-d321-4575-a552-4ae7078cbc31',
        'view_id': 'ec79df5b-2c54-4fb9-a86a-b20775678236',
        'source_url': 'https://outagemap.ma.nationalgridus.com/',
    },
    'pseg_nj': {
        'name': 'PSE&G',
        'instance_id': '79d82478-c10a-4956-a045-89de3cda618c',
        'view_id': 'bdb7f5be-1c1a-46fa-862e-ebdf21851d2e',
        'source_url': 'https://outagecenter.pseg.com/external/default.html',
    },
    'jcpl_nj': {
        'name': 'JCP&L',
        'instance_id': '6c715f0e-bbec-465f-98cc-0b81623744be',
        'view_id': 'fcd7c23d-de37-4471-8ca3-37c3260f94fa',
        'source_url': 'https://outages-nj.firstenergycorp.com/',
    },
    'versant_me': {
        'name': 'Versant Power',
        'instance_id': '6abd095a-98f2-40a2-bb75-d51133d0c2c8',
        'view_id': '05bfafbb-0ad1-4ff1-8287-d32fd1ed7fce',
        'source_url': 'https://kubra.io/stormcenter/views/05bfafbb-0ad1-4ff1-8287-d32fd1ed7fce',
    },
    'national_grid_ny': {
        'name': 'National Grid New York',
        'instance_id': '9cb2e5b7-d321-4575-a552-4ae7078cbc31',
        'view_id': '1b69b604-7588-4753-8591-9f135a962a2f',
        'source_url': 'https://outagemap.ny.nationalgridus.com/',
    },
    'central_hudson': {
        'name': 'Central Hudson',
        'instance_id': 'd19170d4-3357-4f67-bc30-e88f85fc85c8',
        'view_id': '7e22d4b2-8c3d-4625-adc1-ffcdd2944de0',
        'source_url': 'https://outagemap.cenhud.com/',
    },
    'pseg_long_island': {
        'name': 'PSEG Long Island',
        'instance_id': '48006d55-45bb-40a4-99a6-1289326f0d0d',
        'view_id': 'fb50e444-70e5-4b69-b868-28463b5dbd19',
        'source_url': 'https://outagemap.psegliny.com/',
    },
    'aep_ohio': {
        'name': 'AEP Ohio',
        'instance_id': '9c0735d8-b721-4dce-b80b-558e98ce1083',
        'view_id': '9b2feb80-69f8-4035-925e-f2acbcf1728e',
        'source_url': 'https://outagemap.aepohio.com/',
    },
    'firstenergy_ohio': {
        'name': 'FirstEnergy Ohio',
        'instance_id': '6c715f0e-bbec-465f-98cc-0b81623744be',
        'view_id': 'db9c3f02-0a06-4672-a357-0f676eb75bfa',
        'source_url': 'https://outages-oh.firstenergycorp.com/',
        'thematic_source': 'public/thematic-12/thematic_areas.json',
    },
    'indiana_michigan_power': {
        'name': 'Indiana Michigan Power',
        'instance_id': 'e07a4ffb-83dd-4251-8dad-3fe5a26979b6',
        'view_id': '1b319a36-d1cf-443d-9ecf-6267acae0c3a',
        'source_url': 'https://outagemap.indianamichiganpower.com/',
        'state_code': 'IN',
    },
    'indiana_michigan_power_mi': {
        'name': 'Indiana Michigan Power',
        'instance_id': 'e07a4ffb-83dd-4251-8dad-3fe5a26979b6',
        'view_id': '1b319a36-d1cf-443d-9ecf-6267acae0c3a',
        'source_url': 'https://outagemap.indianamichiganpower.com/',
        'state_code': 'MI',
    },
    'comed_il': {
        'name': 'ComEd',
        'instance_id': '0f46457f-e3ee-473c-8040-d7da9e776ccb',
        'view_id': 'a7f6692c-38f6-4bef-8681-989b2bcba5da',
        'source_url': 'https://www.comed.com/outages/experiencing-an-outage/outage-map',
        'state_code': 'IL',
    },
    'ameren_il': {
        'name': 'Ameren Illinois',
        'instance_id': '92e209c0-35c3-4ec2-9ce9-30d87a55706f',
        'view_id': '9b7c2416-cb0c-4845-99c7-7b31041038a2',
        'source_url': 'https://outagemap.ameren.com/?c=External&o=StateZIP',
        'state_code': 'IL',
    },
    'ameren_mo': {
        'name': 'Ameren Missouri',
        'instance_id': '92e209c0-35c3-4ec2-9ce9-30d87a55706f',
        'view_id': '9b7c2416-cb0c-4845-99c7-7b31041038a2',
        'source_url': 'https://outagemap.ameren.com/?c=External&o=StateZIP',
        'state_code': 'MO',
    },
    'evergy_mo': {
        'name': 'Evergy Missouri',
        'instance_id': 'b1493825-4ee3-4706-a986-99a763a733db',
        'view_id': 'c1062d22-2919-487c-9000-e21b72b62278',
        'source_url': 'https://outagemap.evergy.com/',
        'state_code': 'MO',
    },
    'evergy_ks': {
        'name': 'Evergy Kansas',
        'instance_id': 'b1493825-4ee3-4706-a986-99a763a733db',
        'view_id': 'c1062d22-2919-487c-9000-e21b72b62278',
        'source_url': 'https://outagemap.evergy.com/',
        'state_code': 'KS',
    },
    'swepco_ar': {
        'name': 'SWEPCO Arkansas',
        'instance_id': '9632df6e-a385-400a-b822-b68d268e8e7c',
        'view_id': 'e51304d0-7052-458d-a5ac-0e1980cca976',
        'source_url': 'https://outagemap.swepco.com/',
        'state_code': 'AR',
    },
    'swepco_la': {
        'name': 'SWEPCO Louisiana',
        'instance_id': '9632df6e-a385-400a-b822-b68d268e8e7c',
        'view_id': 'e51304d0-7052-458d-a5ac-0e1980cca976',
        'source_url': 'https://outagemap.swepco.com/',
        'state_code': 'LA',
    },
    'pso_ok': {
        'name': 'Public Service Company of Oklahoma',
        'instance_id': '4bb3b3bc-e1c4-448b-b806-e4fc85c3b640',
        'view_id': 'e2356e43-c76f-4772-bf85-31240a2cc504',
        'source_url': 'https://outagemap.psoklahoma.com/',
        'state_code': 'OK',
    },
    'oge_ok': {
        'name': 'OG&E',
        'instance_id': 'dc85f79f-59f9-4e9e-9557-b3a9bee7e0ce',
        'view_id': '8fe9d356-96bc-41f1-b353-6720eb408936',
        'source_url': 'https://www.oge.com/wps/portal/ord/outages/systemwatch/',
        'state_code': 'OK',
    },
    'oncor_tx': {
        'name': 'Oncor',
        'instance_id': '560abba3-7881-4741-b538-ca416b58ba1e',
        'view_id': 'ca124b24-9a06-4b19-aeb3-1841a9c962e1',
        'source_url': 'https://stormcenter.oncor.com/external/default.html',
        'state_code': 'TX',
    },
    'aep_texas': {
        'name': 'AEP Texas',
        'instance_id': '3ff6812b-90d8-40cd-97a6-76633226f27b',
        'view_id': '6022fe09-5259-4763-892f-5f57463fa6a5',
        'source_url': 'https://outagemap.aeptexas.com/',
        'state_code': 'TX',
    },
    'tnmp_tx': {
        'name': 'Texas-New Mexico Power',
        'instance_id': '6f00c909-0ab9-46c1-94c5-c07435d5baf6',
        'view_id': '5bb75bf1-56fa-4d46-ac74-dd9b2106611a',
        'source_url': 'https://outagemap.tnmp.com/',
        'state_code': 'TX',
    },
    'pnm_nm': {
        'name': 'PNM',
        'instance_id': 'b6fe87bc-3c82-4afe-b7f0-7eee93a32dfa',
        'view_id': '92a8c818-17ed-401d-89be-73e5ae60ee5b',
        'source_url': 'https://outagemap.pnm.com/',
        'state_code': 'NM',
    },
}

PULSEPOINT_AGENCIES = {
    'GB803': 'El Paso Fire',
    'PID337': 'Fort Worth Fire Department',
    'CN407': 'Frisco Fire Department',
    'XV503': 'Georgetown Fire Department',
    'WB616': 'Grapevine Fire Department',
    'EMS1372': 'North Texas Emergency Communications Center',
    'CN713': 'Plano Fire-Rescue',
    'EMS1176': 'Williamson County EMS',
    '72002': 'Broken Arrow Fire',
    '63010': 'WFH-EMS Dept',
    '04600': 'Rogers FD',
    '00067': 'Springdale Fire',
    'EMS1296': 'Alachua/Gainesville',
    '06142': 'Boca Raton Fire',
    'EMS1236': 'Brevard County Fire',
    '10282': 'Broward County Fire',
    '48032': 'Clay County Fire',
    '10021': 'Coconut Creek FR',
    'CCSO1': 'Collier Co Sheriff',
    '10151': 'Coral Springs FD',
    '10242': 'Davie Fire',
    '06172': 'Delray Fire',
    '10192': 'Fort Lauderdale Fire',
    '06062': 'Greenacres Fire',
    '10052': 'Hollywood Fire',
    '10062': 'Lauderhill FD',
    '10252': 'Lighthouse Pt Fire',
    'EMS1203': 'Manatee County',
    '10092': 'Margate Fire Rescue',
    '14162': 'Marion County',
    'X1012': 'Miami Beach Fire',
    '10232': 'Miramar Fire-Rescue',
    '10132': 'N Lauderdale Fire',
    '06102': 'N Palm Beach Fire',
    '10182': 'Oakland Park Fire',
    '65060': 'Orange County Fire',
    'PID136': 'Orlando Airport Fire',
    '07212': 'Orlando FD',
    '06042': 'PB Gardens Fire',
    '06301': 'Palm Beach Co Fire',
    '28042': 'Pasco County Fire',
    '10082': 'Pembroke Pines Fire',
    '5102x': 'Polk County Fire',
    '10125': 'Pompano Beach Fire',
    '16072': 'Sarasota County',
    '17022': 'Seminole County Fire',
    '36011': 'South Walton Fire',
    'X4015': 'Sumter Fire & EMS',
    '10162': 'Sunrise Fire Rescue',
    '10202': 'Tamarac Fire',
    '06272': 'West Palm Beach Fire',
    '07042': 'Winter Park Fire',
    'PID89': 'Chatham County Fire',
    'EMS1093': 'Chatham EMS',
    'PID396': 'South Fulton Fire',
    'PID52': 'Columbus County Fire / EMS',
    'EMS1987': 'Durham 911',
    'PID158': 'Forsyth EMS',
    'PID157': 'Forsyth County Fire',
    'EMS1681': 'Iredell 911',
    'PID421': 'Kernersville Fire',
    'EMS1316': 'Mecklenburg EMS',
    'EMS1234': 'Pitt County Emergency Management',
    'EMS1209': 'Wake County Fire / EMS',
    'PID100': 'Wayne County Fire / EMS',
    'PID159': 'Winston-Salem Fire',
    'EMS1247': 'Hamilton County Emergency Services',
    'EMS1189': 'Putnam County 911',
    'EMS1087': 'Hardin County EMS',
    'EMS1808': 'Jessamine County',
    '00300': 'Albemarle County Fire Rescue',
    'EMS1855': 'Augusta County Emergency Communications',
    'EMS2003': 'Bath County 911',
    '54000': 'Charlottesville Fire Department',
    '55000': 'Chesapeake Fire Department',
    'EMS1402': 'Chesterfield County',
    'PID262': 'Danville Fire Department',
    '05900': 'Fairfax County Fire and Rescue',
    'PID275': 'Frederick County Fire and Rescue',
    '65000': 'Hampton Fire and Rescue',
    '66000': 'Harrisonburg Fire Department',
    '09500': 'James City County Fire Department',
    'PID98': 'Louisa County Fire and EMS',
    '68000': 'Lynchburg Fire Department',
    '70001': 'Newport News Fire Department',
    '71000': 'Norfolk Fire-Rescue',
    '73500': 'Poquoson Fire Department',
    '15301': 'Prince William County Fire and Rescue',
    '76000': 'Richmond Fire Department',
    'EMS1854': 'Rockbridge County',
    '16500': 'Rockingham County',
    '17700': 'Spotsylvania County Fire and Rescue',
    '79000': 'Staunton Fire Department',
    '80000': 'Suffolk Fire and Rescue',
    'EMS1264': 'Virginia Beach EMS',
    'EMS1968': 'Waynesboro',
    '83000': 'Williamsburg Fire Department',
    '19900': 'York County Fire and Life Safety',
    'EMS1583': 'Putnam County EMS',
    '24000': 'Anne Arundel County Fire',
    '13000': 'Howard County Fire and Rescue',
    '16000': "Prince George's County Fire/EMS",
    'EMS1312': 'Wicomico County Emergency Services',
    'EMS1205': 'DC Fire and EMS',
    'EMS1095': 'New Castle County EMS',
    'EMS1110': 'Allegheny County EMS',
    'PID233': 'Butler County',
    'EMS1969': 'Cambria 911',
    'EMS1252': 'Cameron County',
    'EMS1192': 'Chester County',
    'EMS1258': 'Clarion County',
    'EMS1394': 'Clearfield County',
    'PID92': 'Clinton County DES',
    'EMS1255': 'Crawford County',
    'PID6': 'Delaware County DES',
    'EMS1226': 'Elk County',
    'EMS1204': 'Erie County',
    'EMS1365': 'Forest County',
    'EMS1584': 'Lancaster County 911',
    'EMS2006': 'Lycoming County DPS',
    'EMS1265': 'McKean County',
    'EMS1179': 'Montgomery County',
    'EMS1685': 'Somerset County 911',
    'EMS1239': 'Warren County',
    'EMS1078': 'Burlington County',
    '25009': 'Columbus Fire',
    '31015': 'Cincinnati Fire',
    'EMS1035': 'Cleveland EMS',
    'EMS1034': 'Chagrin Valley Dispatch Fire / EMS',
    '77001': 'Akron Fire',
    'PID178': 'Toledo Firefighters',
    '25013': 'Washington Township Fire (Dublin)',
    '18801': 'Parma Fire',
    '18083': 'Parma Heights Fire',
    'PID94': 'Cuyahoga Falls Fire',
    'EMS1194': 'Geauga County Fire / EMS',
    'PID403': 'Hamilton County Fire / EMS',
    'EMS1321': 'Boone County Fire / EMS',
    '29003': 'Carmel Fire',
    '02017': 'Centre Township Fire',
    '29004': 'Cicero Fire',
    'EMS1315': 'Clark County Fire / EMS',
    '71002': 'Clay Township Fire',
    'EMS1103': 'Dearborn County 911',
    '29005': 'Fishers Fire',
    'EMS1210': 'Hancock County Fire / EMS',
    'PID214': 'Johnson County Fire / EMS',
    'PID50': 'La Porte County E911',
    'EMS1363': 'Madison County Fire / EMS',
    '71008': 'Mishawaka Fire',
    'PID62': 'New Carlisle Fire',
    '29007': 'Noblesville Fire',
    '71019': 'Penn Township Fire',
    'PID186': 'Perry County Fire / EMS',
    'PID160': 'Ripley County 911',
    '29008': 'Sheridan Fire',
    '71016': 'South Bend Fire',
    'PID343': 'St. Joseph County Fire',
    'PID399': 'Three Rivers Ambulance Authority',
    '29010': 'Westfield Fire',
    'EMS1124': 'AMT Central',
    'PID127': 'AMT East',
    'WJ134': 'Blackhawk Fire',
    'PID313': 'Blue Island Fire',
    'PID312': 'Calumet City Fire',
    'EMS1174': 'Champaign County',
    'WJ124': 'Cherry Valley Fire',
    'PID307': 'Chicago Heights Fire',
    'WJ143': 'Durand Fire',
    'PID311': 'Garden Homes Fire',
    'WJ153': 'Harlem-Roscoe Fire',
    'PID60': 'Lemont Fire',
    'WJ164': 'Loves Park Fire',
    'PID53': 'MABAS',
    'PID323': 'Merrionette Park Fire',
    'DD132': 'Naperville Fire',
    'WJ173': 'New Milford Fire',
    'WJ184': 'North Park Fire',
    'WJ194': 'Northwest Fire',
    'PID310': 'Oak Forest Fire',
    'CS132': 'Orland Fire',
    'PID61': 'Palos Fire',
    'WJ203': 'Pecatonica Fire',
    'WJ111': 'Rockford Fire',
    'WJ213': 'Rockton Fire',
    'WJ263': 'Shirland Fire',
    'WJ222': 'South Beloit Fire',
    'EMS1175': 'Vermilion County',
    'WJ234': 'West Suburban Fire',
    'WJ243': 'WIN-BUR-SEW Fire',
    '40030': 'Cudahy Fire',
    '13030': 'Dane County EMS / Fire',
    '40010': 'Franklin Fire',
    'EMS1180': 'Gold Cross Ambulance',
    'PID149': 'Green Bay Metro Fire',
    '40050': 'Greendale Fire',
    '40210': 'Greenfield Fire',
    'EMS1001': 'Gundersen Tri-State Ambulance',
    '40160': 'Hales Corners Fire',
    '13010': 'Madison Fire',
    '40200': 'Milwaukee Fire',
    'PID108': 'Monroe County 911',
    '40260': 'North Shore Fire',
    '40020': 'Oak Creek Fire',
    'EMS1318': 'Portage County',
    'PID324': 'Rock County',
    '40220': 'South Milwaukee Fire',
    '40070': 'St. Francis Fire',
    'EMS1396': 'Waukesha County 911',
    '67060': 'Waukesha Fire',
    '40110': 'Wauwatosa Fire',
    '40100': 'West Allis Fire',
    'EMS1355': 'Anoka County',
    'EMS1385': 'Crow Wing County',
    '14307': 'Moorhead Fire Department',
    'EMS1329': 'Ramsey County',
    'EMS1591': 'St. Louis County',
    '5200X': 'Coralville Police / Fire',
    '52002': 'Hills Fire',
    '52003': 'Iowa City Fire',
    '52004': 'Lone Tree Fire',
    '52005': 'North Liberty Fire',
    '52006': 'Oxford Fire',
    '52008': 'Solon Fire',
    'X5200': 'Swisher Fire',
    '52010': 'Tiffin Fire',
    '01602': 'Jackson Fire',
    '01604': 'Cape Rural Fire',
    '03601': 'Pacific Fire',
    '04813': 'Kansas City Fire',
    '09501': 'Robertson Fire',
    '09502': 'Valley Park Fire',
    '09504': 'Mehlville Fire',
    '09505': 'Spanish Lake Fire',
    '09506': 'Ladue Fire',
    '09507': 'Metro West Fire',
    '09508': 'Frontenac Fire',
    '0950x': 'Pattonville Fire',
    '09510': 'Creve Coeur Fire',
    '09511': 'Maplewood Fire',
    '09512': 'Lemay Fire',
    '09513': 'Crestwood Fire',
    '09514': 'West Co Fire/EMS',
    '09515': 'Webster Groves Fire',
    '09518': 'Eureka Fire',
    '09519': 'Kinloch Fire',
    '09520': 'Northeast Amb/Fire',
    '09521': 'Monarch Fire',
    '09522': 'Richmond Hts Fire',
    '09523': 'Shrewsbury Fire',
    '09524': 'Berkeley Fire',
    '09525': 'Clayton Fire',
    '09526': 'W Overland Fire EMS',
    '09527': 'Maryland Heights FPD',
    '09528': 'Fenton Fire',
    '09529': 'Brentwood Fire',
    '09530': 'Black Jack Fire',
    '09531': 'Florissant Vly Fire',
    '09532': 'Rock Hill Fire-EMS',
    '09533': 'University City Fire',
    '09534': 'Mid-County Fire',
    '09535': 'Olivette Fire',
    '09536': 'Community Fire',
    '09538': 'Ferguson Fire',
    '09539': 'Affton Fire',
    '09540': 'Hazelwood Fire',
    '09544': 'North County Fire',
    'EMS1241': 'COX MERCY',
    'EMS1328': 'Meramec Ambulance',
    'EMS1424': 'Boone County Joint',
    'EMS1757': 'JASCO',
    'PID239': 'Lone Jack Fire',
    'PID240': 'Sni-Valley Fire',
    'PID241': 'Fort Osage Fire',
    'PID242': 'Southern Jackson Co',
    'PID243': "Lee's Summit Fire",
    'PID304': 'Sugar Creek Fire',
    'PID344': 'Carterville FD',
    'PID349': 'Tri-Cities Fire',
    'PID350': 'Asbury Fire',
    'PID351': 'Avilla Fire',
    'PID352': 'Carl Junction FD',
    'PID353': 'Carthage Fire',
    'PID354': 'Duenweg Fire',
    'PID355': 'Golden City FD',
    'PID356': 'Jasper Fire',
    'PID357': 'Oronogo Fire',
    'PID358': 'Sarcoxie Rural Fire',
    'PID359': 'Webb City Fire',
    'PID360': 'Metro AMB',
    'PID366': 'Jasper Co SO',
}

DEFLOCK_INDEX_URL = 'https://cdn.deflock.me/regions/index.json'
LITHUANIA_TOLL_EQUIPMENT_URL = (
    'https://gis.ktvis.lt/arcgis/rest/services/LAKD/EISMOINFO_SLUOKSNIAI/MapServer/13/query'
    '?where=1%3D1&outFields=objectid%2Ckelionumeris%2Ckm%2Ctipas%2Cgaliojimopradzia%2Cgaliojimopabaiga'
    '&returnGeometry=true&outSR=4326&f=geojson'
)
MILAN_AREA_B_GATES_SOURCE = 'https://dati.comune.milano.it/dataset/ds959-varchi-areab'
MILAN_AREA_B_GATES_URL = (
    'https://dati.comune.milano.it/dataset/cebfe28e-a50e-4b81-9d14-7b0d37170f0d/'
    'resource/c3439df3-673d-45e7-b151-c6d8d70ba0e4/download/areab_varchi.geojson'
)
MILAN_AREA_C_GATES_SOURCE = 'https://dati.comune.milano.it/dataset/ds82_infogeo_varchi_elettronici_localizzazione_'
MILAN_AREA_C_GATES_URL = (
    'https://dati.comune.milano.it/dataset/4cad1605-8225-4ecd-9b82-868b3af453e5/'
    'resource/fa8fcc31-1722-4a50-a0ae-ce7b9c0d0361/download/ingressi_areac_varchi.geojson'
)
LITHUANIA_TOLL_EQUIPMENT_SOURCE = (
    'https://gis.ktvis.lt/arcgis/rest/services/LAKD/EISMOINFO_SLUOKSNIAI/MapServer/13'
)
GFC_WILDFIRE_URL = 'https://georgiafc.firesponse.com/public/api/Incident/geojson'
GFC_WILDFIRE_SOURCE_URL = 'https://georgiafc.firesponse.com/public/'
SCFC_WILDFIRE_URL = 'https://scfc.firesponse.com/public/api/Incident/geojson'
SCFC_WILDFIRE_SOURCE_URL = 'https://scfc.firesponse.com/public/'
NCFS_WILDFIRE_URL = 'https://ncfspublic.firesponse.com/api/incidents'
NCFS_WILDFIRE_SOURCE_URL = 'https://ncfspublic.firesponse.com/'
TDF_WILDFIRE_URL = 'https://tn.firesponse.com/public/api/Incident/geojson'
TDF_WILDFIRE_SOURCE_URL = 'https://www.tn.gov/tnwildlandfire/suppression/current-wildfires.html'
KDF_WILDFIRE_URL = 'https://kdf.firesponse.com/public/api/Incident/geojson'
KDF_WILDFIRE_SOURCE_URL = 'https://eec.ky.gov/Natural-Resources/Forestry/Pages/default.aspx'
VDOF_WILDFIRE_SOURCE_URL = 'https://www.dof.virginia.gov/wildland-prescribed-fire/wildfire-suppression/'
AFC_WILDFIRE_URL = (
    'https://gis.forestry.alabama.gov/arcgis/rest/services/'
    'AFCEnterprise/OnlyActiveFireForPublic/FeatureServer/0/query'
    '?where=1%3D1&outFields=*&returnGeometry=true&outSR=4326&f=geojson'
)
AFC_WILDFIRE_SOURCE_URL = 'https://forestry.alabama.gov/Pages/Maps/Wildfires.aspx'
WFIGS_WILDFIRE_URL = (
    'https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/'
    'WFIGS_Incident_Locations_Last24h/FeatureServer/0/query'
)
WFIGS_WILDFIRE_SOURCE_URL = 'https://www.nifc.gov/fire-information/maps'
ALERTDC_FEED_URL = 'https://trainingtrack.hsema.dc.gov/NRss/RssFeed'
ALERTDC_MAX_AGE_HOURS = 24
NOTIFY_NYC_RSS_URL = 'https://feeds.everbridge.net/feeds/453003085617722/rss/rss.xml'
NOTIFY_NYC_SOURCE_URL = 'https://a858-nycnotify.nyc.gov/notifynyc/'
NOTIFY_NYC_MAX_AGE_HOURS = 12
ENTERGY_MS_OUTAGES_URL = 'https://entergy.datacapable.com/datacapable/v2/p/entergy/r/ms/map/events'
ENTERGY_MS_SOURCE_URL = 'https://www.etrviewoutage.com/map?state=MS'
ENTERGY_AR_OUTAGES_URL = 'https://entergy.datacapable.com/datacapable/v2/p/entergy/r/ar/map/events'
ENTERGY_AR_SOURCE_URL = 'https://www.etrviewoutage.com/map?state=AR'
ENTERGY_LA_OUTAGES_URL = 'https://entergy.datacapable.com/datacapable/v2/p/entergy/r/la/map/events'
ENTERGY_LA_SOURCE_URL = 'https://www.etrviewoutage.com/map?state=LA'
ENTERGY_TX_OUTAGES_URL = 'https://entergy.datacapable.com/datacapable/v2/p/entergy/r/tx/map/events'
ENTERGY_TX_SOURCE_URL = 'https://www.etrviewoutage.com/map?state=TX'
CENTERPOINT_TX_OUTAGES_URL = (
    'https://centerpoint.datacapable.com/datacapable/v2/p/centerpoint/r/texas/map/events'
)
CENTERPOINT_TX_SOURCE_URL = 'https://tracker.centerpointenergy.com/map/'
APS_AZ_OUTAGES_URL = (
    'https://aps-ags.esriemcs.com/arcgis/rest/services/'
    'APSOutageMap/MapServer/0/query'
)
APS_AZ_OUTAGES_SOURCE_URL = 'https://outagemap.aps.com/outageviewer/'
PGE_CA_OUTAGES_URL = (
    'https://ags.pge.esriemcs.com/arcgis/rest/services/'
    '43/outages/MapServer/8/query'
)
PGE_CA_OUTAGES_SOURCE_URL = 'https://pgealerts.alerts.pge.com/outage-tools/outage-map/'
NVENERGY_OUTAGES_SOURCE_URL = 'https://www.nvenergy.com/outages-and-emergencies/view-current-outages'
NVENERGY_OUTAGE_LAYERS = (
    (
        'unplanned',
        'https://services.nvenergy.com/GISSvc/1.0/retrieveMapInfo/arcgis/rest/services/'
        'Outage/CURRENTOUTAGES_2024/MapServer/0/query',
    ),
    (
        'planned',
        'https://services.nvenergy.com/GISSvc/1.0/retrieveMapInfo/arcgis/rest/services/'
        'Outage/PLANNEDOUTAGES_2024/MapServer/0/query',
    ),
)
PACIFIC_POWER_OR_OUTAGES_URL = (
    'https://www.pacificpower.net/etc/pcorp/datafiles/outagemap/mapOR.json'
)
PACIFIC_POWER_WA_OUTAGES_URL = (
    'https://www.pacificpower.net/etc/pcorp/datafiles/outagemap/mapWA.json'
)
PACIFIC_POWER_OR_SOURCE_URL = 'https://www.pacificpower.net/outages-safety.html?source=APP'
ROCKY_MOUNTAIN_POWER_ID_OUTAGES_URL = (
    'https://www.rockymountainpower.net/etc/pcorp/datafiles/outagemap/mapID.json'
)
ROCKY_MOUNTAIN_POWER_UT_OUTAGES_URL = (
    'https://www.rockymountainpower.net/etc/pcorp/datafiles/outagemap/mapUT.json'
)
ROCKY_MOUNTAIN_POWER_WY_OUTAGES_URL = (
    'https://www.rockymountainpower.net/etc/pcorp/datafiles/outagemap/mapWY.json'
)
ROCKY_MOUNTAIN_POWER_SOURCE_URL = 'https://www.rockymountainpower.net/outages-safety.html?source=APP'
NORTHWESTERN_MT_OUTAGES_URL = 'https://www.northwesternenergy.com/get-outage-map-data'
NORTHWESTERN_MT_SOURCE_URL = 'https://www.northwesternenergy.com/outages/outage-map'
PHOENIX_FIRE_INCIDENTS_URL = (
    'https://maps.phoenix.gov/phxfire/rest/services/'
    'Active_Incidents__Public/MapServer/0/query'
)
PHOENIX_FIRE_SOURCE_URL = (
    'https://www.phoenix.gov/administration/departments/fire/data/incident-data.html'
)
CLECO_OUTAGES_URL = 'https://cleco-prod.azure-api.net/outage/api/1/outage/alloutages/2/1'
CLECO_OUTAGES_SOURCE_URL = 'https://myaccount.cleco.com/portal/#/previewoutage'
NES_OUTAGES_URL = 'https://utilisocial.io/datacapable/v2/p/NES/map/events'
NES_SOURCE_URL = 'https://www.nespower.com/outages/'
KUB_TN_OUTAGES_URL = 'https://www.kub.org/outage-data/data.json'
KUB_TN_SOURCE_URL = 'https://www.kub.org/outage/map'
ARCGIS_GEOMETRY_PROJECT_URL = (
    'https://utility.arcgisonline.com/ArcGIS/rest/services/Geometry/GeometryServer/project'
)
NWS_ALERTS_URL = 'https://api.weather.gov/alerts/active'
NWS_ZONE_CACHE_TTL = 24 * 60 * 60
NWS_ZONE_CACHE_MAX_SIZE = 4096
NWS_ZONE_CACHE = {}
NWS_ZONE_CACHE_LOCK = threading.Lock()
MDOT_TRAFFIC_SOURCE_URL = 'https://www.mdottraffic.com/default.aspx?fullsite=1'
GDOT_RWIS_OBSERVATIONS_URL = 'https://www.weather.gov/source/ffc/gdotrwis/rwisobs.js'
GDOT_RWIS_STATIONS_URL = 'https://www.weather.gov/source/ffc/js/roadcast/gdot_stations.js'
GDOT_RWIS_SOURCE_URL = 'https://www.weather.gov/ffc/gdot_rwis'

PULSEPOINT_CALL_TYPES = {
    "AA": ("Auto Aid", "Aid"),
    "MU": ("Mutual Aid", "Aid"),
    "ST": ("Strike Team/Task Force", "Aid"),
    "AC": ("Aircraft Crash", "Aircraft"),
    "AE": ("Aircraft Emergency", "Aircraft"),
    "AES": ("Aircraft Emergency Standby", "Aircraft"),
    "LZ": ("Landing Zone", "Aircraft"),
    "AED": ("AED Alarm", "Alarm"),
    "OA": ("Alarm", "Alarm"),
    "CMA": ("Carbon Monoxide", "Alarm"),
    "FA": ("Fire Alarm", "Alarm"),
    "MA": ("Manual Alarm", "Alarm"),
    "SD": ("Smoke Detector", "Alarm"),
    "TRBL": ("Trouble Alarm", "Alarm"),
    "WFA": ("Waterflow Alarm", "Alarm"),
    "FL": ("Flooding", "Assist"),
    "LR": ("Ladder Request", "Assist"),
    "LA": ("Lift Assist", "Assist"),
    "PA": ("Police Assist", "Assist"),
    "PS": ("Public Service", "Assist"),
    "SH": ("Sheared Hydrant", "Assist"),
    "EX": ("Explosion", "Explosion"),
    "PE": ("Pipeline Emergency", "Explosion"),
    "TE": ("Transformer Explosion", "Explosion"),
    "AF": ("Appliance Fire", "Fire"),
    "CHIM": ("Chimney Fire", "Fire"),
    "CF": ("Commercial Fire", "Fire"),
    "WSF": ("Confirmed Structure Fire", "Fire"),
    "WVEG": ("Confirmed Vegetation Fire", "Fire"),
    "CB": ("Controlled Burn/Prescribed Fire", "Fire"),
    "ELF": ("Electrical Fire", "Fire"),
    "EF": ("Extinguished Fire", "Fire"),
    "FIRE": ("Fire", "Fire"),
    "FULL": ("Full Assignment", "Fire"),
    "IF": ("Illegal Fire", "Fire"),
    "MF": ("Marine Fire", "Fire"),
    "OF": ("Outside Fire", "Fire"),
    "PF": ("Pole Fire", "Fire"),
    "GF": ("Refuse/Garbage Fire", "Fire"),
    "RF": ("Residential Fire", "Fire"),
    "SF": ("Structure Fire", "Fire"),
    "TF": ("Tank Fire", "Fire"),
    "VEG": ("Vegetation Fire", "Fire"),
    "VF": ("Vehicle Fire", "Fire"),
    "WF": ("Confirmed Fire", "Fire"),
    "WCF": ("Working Commercial Fire", "Fire"),
    "WRF": ("Working Residential Fire", "Fire"),
    "BT": ("Bomb Threat", "Hazard"),
    "EE": ("Electrical Emergency", "Hazard"),
    "EM": ("Emergency", "Hazard"),
    "ER": ("Emergency Response", "Hazard"),
    "GAS": ("Gas Leak", "Hazard"),
    "HC": ("Hazardous Condition", "Hazard"),
    "HMR": ("Hazardous Response", "Hazard"),
    "TD": ("Tree Down", "Hazard"),
    "WE": ("Water Emergency", "Hazard"),
    "AI": ("Arson Investigation", "Investigation"),
    "FWI": ("Fireworks Investigation", "Investigation"),
    "HMI": ("Hazmat Investigation", "Investigation"),
    "INV": ("Investigation", "Investigation"),
    "OI": ("Odor Investigation", "Investigation"),
    "SI": ("Smoke Investigation", "Investigation"),
    "CL": ("Commercial Lockout", "Lockout"),
    "LO": ("Lockout", "Lockout"),
    "RL": ("Residential Lockout", "Lockout"),
    "VL": ("Vehicle Lockout", "Lockout"),
    "CP": ("Community Paramedicine", "Medical"),
    "IFT": ("Interfacility Transfer", "Medical"),
    "ME": ("Medical Emergency", "Medical"),
    "MCI": ("Multi Casualty", "Medical"),
    "EQ": ("Earthquake", "Natural Disaster"),
    "FLW": ("Flood Warning", "Natural Disaster"),
    "TOW": ("Tornado Warning", "Natural Disaster"),
    "TSW": ("Tsunami Warning", "Natural Disaster"),
    "WX": ("Weather Incident", "Natural Disaster"),
    "AR": ("Animal Rescue", "Rescue"),
    "CR": ("Cliff Rescue", "Rescue"),
    "CSR": ("Confined Space Rescue", "Rescue"),
    "ELR": ("Elevator Rescue", "Rescue"),
    "EER": ("Elevator/Escalator Rescue", "Rescue"),
    "IR": ("Ice Rescue", "Rescue"),
    "IA": ("Industrial Accident", "Rescue"),
    "RES": ("Rescue", "Rescue"),
    "RR": ("Rope Rescue", "Rescue"),
    "SC": ("Structural Collapse", "Rescue"),
    "TR": ("Technical Rescue", "Rescue"),
    "TNR": ("Trench Rescue", "Rescue"),
    "USAR": ("Urban Search and Rescue", "Rescue"),
    "VS": ("Vessel Sinking", "Rescue"),
    "WR": ("Water Rescue", "Rescue"),
    "TCP": ("Collision Involving Pedestrian", "Vehicle"),
    "TCS": ("Collision Involving Structure", "Vehicle"),
    "TCT": ("Collision Involving Train", "Vehicle"),
    "TCE": ("Expanded Traffic Collision", "Vehicle"),
    "RTE": ("Railroad/Train Emergency", "Vehicle"),
    "TC": ("Traffic Collision", "Vehicle"),
    "PLE": ("Powerline Emergency", "Wires"),
    "WA": ("Wires Arching", "Wires"),
    "WD": ("Wires Down", "Wires"),
    "WDA": ("Wires Down/Arcing", "Wires"),
    "BP": ("Burn Permit", "Other"),
    "CA": ("Community Activity", "Other"),
    "FW": ("Fire Watch", "Other"),
    "MC": ("Move-up/Cover", "Other"),
    "NO": ("Notification", "Other"),
    "STBY": ("Standby", "Other"),
    "TEST": ("Test", "Other"),
    "TRNG": ("Training", "Other"),
    "NEWS": ("News", "Alert"),
    "CERT": ("CERT", "Alert"),
    "DISASTER": ("Disaster", "Alert"),
    "UNK": ("Unknown Call Type", "Unknown"),
}


def in_region(lat, lon):
    if lat == 0 or lon == 0:
        return False
    return any(
        region['bounds']['min_lat'] <= lat <= region['bounds']['max_lat'] and
        region['bounds']['min_lon'] <= lon <= region['bounds']['max_lon']
        for region in REGIONS.values()
    )


def traffic_region(state_code):
    return REGIONS.get(str(state_code or 'FL').strip().upper())


def state_boundary_geometries():
    global STATE_BOUNDARY_GEOMETRY_CACHE
    with STATE_BOUNDARY_GEOMETRY_CACHE_LOCK:
        if STATE_BOUNDARY_GEOMETRY_CACHE is not None:
            return STATE_BOUNDARY_GEOMETRY_CACHE
        with open(os.path.join(BASE_DIR, 'state-boundary.json'), encoding='utf-8') as handle:
            collection = json.load(handle)
        STATE_BOUNDARY_GEOMETRY_CACHE = {
            str((feature.get('properties') or {}).get('STUSAB') or '').upper(): feature.get('geometry')
            for feature in (collection.get('features') or [])
            if (feature.get('properties') or {}).get('STUSAB') and feature.get('geometry')
        }
        return STATE_BOUNDARY_GEOMETRY_CACHE


def point_in_ring(lon, lat, ring):
    inside = False
    previous = ring[-1] if ring else None
    for current in ring or []:
        if not previous or len(previous) < 2 or len(current) < 2:
            previous = current
            continue
        x1, y1 = safe_float(previous[0]), safe_float(previous[1])
        x2, y2 = safe_float(current[0]), safe_float(current[1])
        if ((y1 > lat) != (y2 > lat)):
            intersection = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < intersection:
                inside = not inside
        previous = current
    return inside


def point_in_polygon(lon, lat, polygon):
    if not polygon or not point_in_ring(lon, lat, polygon[0]):
        return False
    return not any(point_in_ring(lon, lat, hole) for hole in polygon[1:])


def point_in_state(state_code, lat, lon):
    region = traffic_region(state_code)
    if not region or not lat or not lon:
        return False
    bounds = region['bounds']
    if not (
        bounds['min_lat'] <= lat <= bounds['max_lat'] and
        bounds['min_lon'] <= lon <= bounds['max_lon']
    ):
        return False
    geometry = state_boundary_geometries().get(region['code']) or {}
    coordinates = geometry.get('coordinates') or []
    if geometry.get('type') == 'Polygon':
        return point_in_polygon(lon, lat, coordinates)
    if geometry.get('type') == 'MultiPolygon':
        return any(point_in_polygon(lon, lat, polygon) for polygon in coordinates)
    return False


def state_reference_point(state_code):
    """Return a stable point inside a state for area-wide, non-geometric alerts."""
    code = str(state_code or '').strip().upper()
    with STATE_REFERENCE_POINT_CACHE_LOCK:
        if code in STATE_REFERENCE_POINT_CACHE:
            return STATE_REFERENCE_POINT_CACHE[code]

    region = traffic_region(code)
    result = None
    if region:
        bounds = region['bounds']
        center_lat = (bounds['min_lat'] + bounds['max_lat']) / 2
        center_lon = (bounds['min_lon'] + bounds['max_lon']) / 2
        candidates = []
        # A small center-out grid is more reliable than a polygon bounding-box
        # midpoint for split or strongly concave states such as Michigan.
        for row in range(17):
            lat = bounds['min_lat'] + (row + 0.5) * (
                bounds['max_lat'] - bounds['min_lat']
            ) / 17
            for column in range(17):
                lon = bounds['min_lon'] + (column + 0.5) * (
                    bounds['max_lon'] - bounds['min_lon']
                ) / 17
                candidates.append(((lat - center_lat) ** 2 + (lon - center_lon) ** 2, lat, lon))
        for _, lat, lon in sorted(candidates):
            if point_in_state(code, lat, lon):
                result = (lat, lon)
                break

    with STATE_REFERENCE_POINT_CACHE_LOCK:
        STATE_REFERENCE_POINT_CACHE[code] = result
    return result


def traffic_region_or_default(state_code):
    return traffic_region(state_code) or REGIONS['FL']


def traffic_headers(region, *, accept=None):
    origin = region['traffic_origin']
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': f'{origin}/map',
    }
    if accept:
        headers['Accept'] = accept
    return headers


def fetch_iteris_layer(region, layer):
    key = (region['code'], layer)
    now = time.time()
    with ITERIS_TRAFFIC_CACHE_LOCK:
        cached = ITERIS_TRAFFIC_CACHE.get(key)
        if cached and now < cached['expires_at']:
            return cached['content']
    try:
        origin = region['traffic_origin']
        url = f'{origin}/map/mapIcons/{layer}'
        headers = traffic_headers(region, accept='application/json')
        headers['Accept-Encoding'] = 'identity'
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()
        if content[:2] == b'\x1f\x8b':
            import gzip
            content = gzip.decompress(content)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            content = b'{"item2":[]}'
        elif cached:
            return cached['content']
        else:
            raise
    except Exception:
        if cached:
            return cached['content']
        raise
    with ITERIS_TRAFFIC_CACHE_LOCK:
        _evict_if_full(ITERIS_TRAFFIC_CACHE, ITERIS_TRAFFIC_CACHE_MAX_SIZE)
        ITERIS_TRAFFIC_CACHE[key] = {
            'content': content,
            'expires_at': time.time() + ITERIS_CACHE_TTL,
        }
    return content


def iteris_tooltip(region, layer, item_id):
    origin = region['traffic_origin']
    url = f'{origin}/tooltip/{layer}/{item_id}?lang=en'
    req = urllib.request.Request(
        url,
        headers=traffic_headers(region, accept='text/html,application/xhtml+xml'),
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    info = _parse_511_tooltip_html(raw)
    snapshot_url = str(info.get('snapshot_url') or '').strip()
    if snapshot_url:
        snapshot_url = urllib.parse.urljoin(origin, snapshot_url)
        parsed_snapshot = urllib.parse.urlparse(snapshot_url)
        parsed_origin = urllib.parse.urlparse(origin)
        if not (
            parsed_snapshot.scheme == 'https' and
            parsed_snapshot.hostname == parsed_origin.hostname
        ):
            snapshot_url = ''
    info['upstream_snapshot_url'] = snapshot_url or None
    info['snapshot_url'] = (
        f'/camera-snapshot/{region["code"]}/{item_id}' if snapshot_url else None
    )
    return info


def fetch_extended_traffic_json(cache_key, url, *, headers=None, timeout=30, ttl=None):
    """Fetch a public transportation feed once per server refresh window.

    These adapters intentionally share their raw upstream response across the
    camera, incident, construction, tooltip, and snapshot endpoints. That keeps
    user traffic from multiplying requests to the source agency.
    """
    now = time.time()
    with EXTENDED_TRAFFIC_CACHE_LOCK:
        cached = EXTENDED_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['payload']
    try:
        payload = fetch_json_url(url, headers=headers or {
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/json,application/geo+json',
        }, timeout=timeout)
    except Exception:
        if cached:
            return cached['payload']
        raise
    with EXTENDED_TRAFFIC_CACHE_LOCK:
        _evict_if_full(EXTENDED_TRAFFIC_CACHE, 32)
        EXTENDED_TRAFFIC_CACHE[cache_key] = {
            'payload': payload,
            'expires_at': time.time() + (ttl or EXTENDED_TRAFFIC_CACHE_TTL),
        }
    return payload


def drivebc_cameras():
    payload = fetch_extended_traffic_json(
        'drivebc:cameras',
        DRIVEBC_CAMERAS_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/json',
            'Referer': DRIVEBC_SOURCE_URL,
        },
        ttl=120,
    )
    return payload if isinstance(payload, list) else []


def drivebc_events():
    payload = fetch_extended_traffic_json(
        'drivebc:events',
        DRIVEBC_EVENTS_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/json',
            'Referer': DRIVEBC_SOURCE_URL,
        },
    )
    return payload.get('events') or [] if isinstance(payload, dict) else []


def drivebc_event_id(event):
    return str(event.get('id') or '').rstrip('/').rsplit('/', 1)[-1]


def drivebc_event_location(event):
    road = next(iter(event.get('roads') or []), {})
    area = next(iter(event.get('areas') or []), {})
    parts = [
        display_text(road.get('name')),
        display_text(road.get('from')),
        display_text(area.get('name')),
    ]
    return ' · '.join(dict.fromkeys(part for part in parts if part)) or 'British Columbia'


def drivebc_event_records(layer):
    wants_construction = layer == 'Construction'
    if layer not in {'Incidents', 'Construction'}:
        return []
    records = []
    for event in drivebc_events():
        is_construction = str(event.get('event_type') or '').upper() in {
            'CONSTRUCTION', 'SPECIAL_EVENT'
        }
        if is_construction != wants_construction:
            continue
        item_id = drivebc_event_id(event)
        center = geojson_center(event.get('geography'))
        if not valid_traffic_item_id(item_id) or not center:
            continue
        lat, lon = center
        if not point_in_state('BC', lat, lon):
            continue
        records.append((item_id, lat, lon, event))
    return records


def drivebc_layer_payload(layer):
    if layer == 'Cameras':
        items = []
        for camera in drivebc_cameras():
            camera_id = str(camera.get('id') or '').strip()
            location = camera.get('location') or {}
            coords = location.get('coordinates') or []
            lat = optional_float(coords[1]) if len(coords) >= 2 else None
            lon = optional_float(coords[0]) if len(coords) >= 2 else None
            if (
                not valid_numeric_id(camera_id) or lat is None or lon is None or
                not camera.get('is_on') or not camera.get('should_appear') or
                not point_in_state('BC', lat, lon)
            ):
                continue
            items.append({
                'itemId': camera_id,
                'location': [lat, lon],
                'title': display_text(camera.get('name')) or f'DriveBC camera {camera_id}',
                'expando': {
                    'videoEnabled': False,
                    'videoId': camera_id,
                    'snapshotUrl': f'/camera-snapshot/BC/{camera_id}',
                    'snapshotFromVideo': False,
                },
            })
        return {'item2': items}
    if layer == 'MessageSigns':
        return {'item2': []}
    items = []
    for item_id, lat, lon, event in drivebc_event_records(layer):
        title = display_text(event.get('headline')) or (
            'Construction' if layer == 'Construction' else 'Traffic incident'
        )
        items.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': display_text(event.get('description')) or title,
                'severity': display_text(event.get('severity')) or None,
                'timestamp': event.get('updated') or event.get('created'),
                'location': drivebc_event_location(event),
            },
        })
    return {'item2': items}


def drivebc_camera_detail(site_id):
    target = str(site_id)
    for camera in drivebc_cameras():
        if str(camera.get('id') or '') != target:
            continue
        image_path = str((camera.get('links') or {}).get('imageDisplay') or '').strip()
        snapshot_url = urllib.parse.urljoin(DRIVEBC_SOURCE_URL, image_path) if image_path else None
        return {
            'name': display_text(camera.get('name')) or f'DriveBC camera {target}',
            'msg': display_text(camera.get('caption')) or 'DriveBC highway camera',
            'severity': None,
            'timestamp': camera.get('last_update_modified'),
            'location': display_text(camera.get('region_name')) or 'British Columbia',
            'video_id': target,
            'video_url': None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/BC/{target}' if snapshot_url else None,
            'upstream_snapshot_url': snapshot_url,
        }
    raise ValueError(f'DriveBC camera {site_id} not found')


def drivebc_tooltip(layer, item_id):
    if layer == 'Cameras':
        return drivebc_camera_detail(item_id)
    target = str(item_id)
    for event_id, _lat, _lon, event in drivebc_event_records(layer):
        if event_id != target:
            continue
        return {
            'name': display_text(event.get('headline')) or 'DriveBC traffic event',
            'msg': display_text(event.get('description')) or 'Traffic event',
            'severity': display_text(event.get('severity')) or None,
            'timestamp': event.get('updated') or event.get('created'),
            'location': drivebc_event_location(event),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'DriveBC {layer} item {item_id} not found')


def decode_google_polyline(encoded):
    points = []
    index = latitude = longitude = 0
    while index < len(encoded or ''):
        values = []
        for _ in range(2):
            result = shift = 0
            while index < len(encoded):
                value = ord(encoded[index]) - 63
                index += 1
                result |= (value & 31) << shift
                shift += 5
                if value < 32:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += values[0]
        longitude += values[1]
        points.append((latitude / 100000.0, longitude / 100000.0))
    return points


def drivenwt_data():
    path_payload = fetch_extended_traffic_json(
        'drivenwt:data-path',
        DRIVENWT_DATA_PATH_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/json',
            'Referer': DRIVENWT_SOURCE_URL,
        },
        ttl=30,
    )
    data_path = str(path_payload.get('filePath') or '').replace('/../', '/')
    if not data_path.startswith('/Dynamic/'):
        raise ValueError('DriveNWT returned an invalid data path')
    data_url = urllib.parse.urljoin(DRIVENWT_SOURCE_URL, data_path)
    payload = fetch_extended_traffic_json(
        f'drivenwt:data:{data_path}',
        data_url,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/json',
            'Referer': DRIVENWT_SOURCE_URL,
        },
        ttl=60,
    )
    points = decode_google_polyline(payload.get('CoordsEncoded') or '')
    cursor = 0
    for issue in payload.get('Issues') or []:
        issue_points = []
        for shape in issue.get('Geometry') or []:
            count = safe_int(shape.get('NumPoints')) or 0
            if count <= 0:
                continue
            path = points[cursor:cursor + count]
            cursor += count
            issue_points.extend(path)
        issue['_center'] = issue_points[len(issue_points) // 2] if issue_points else None
    for entity in payload.get('Entities') or []:
        entity['_position'] = points[cursor] if cursor < len(points) else None
        cursor += 1
    return payload


def drivenwt_issue_label(payload, issue):
    key = str((issue.get('TableViewInfo') or {}).get('IssueTypeId') or '')
    values = (payload.get('EntitySubTypeNames') or {}).get(key) or {}
    return display_text(values.get('en-us')) or 'Road advisory'


def drivenwt_issue_records(layer):
    if layer not in {'Incidents', 'Construction'}:
        return []
    payload = drivenwt_data()
    records = []
    for issue in payload.get('Issues') or []:
        item_id = str(issue.get('IssueId') or '').split('|')[1:2]
        item_id = item_id[0] if item_id else ''
        center = issue.get('_center')
        label = drivenwt_issue_label(payload, issue)
        is_construction = any(
            token in label.lower() for token in ('construction', 'equipment', 'width reduction')
        )
        if is_construction != (layer == 'Construction'):
            continue
        if not valid_numeric_id(item_id) or not center:
            continue
        lat, lon = center
        if point_in_state('NT', lat, lon):
            records.append((item_id, lat, lon, label, issue))
    return records


def drivenwt_layer_payload(layer):
    if layer == 'Cameras':
        items = []
        for entity in drivenwt_data().get('Entities') or []:
            if safe_int(entity.get('EntityType')) != 3:
                continue
            parts = str(entity.get('EntityId') or '').split('|')
            camera_id = parts[1] if len(parts) >= 2 else ''
            center = entity.get('_position')
            if not valid_numeric_id(camera_id) or not center:
                continue
            lat, lon = center
            if not point_in_state('NT', lat, lon):
                continue
            items.append({
                'itemId': camera_id,
                'location': [lat, lon],
                'title': display_text(entity.get('Location')) or f'DriveNWT camera {camera_id}',
                'expando': {
                    'videoEnabled': False,
                    'videoId': camera_id,
                    'snapshotUrl': f'/camera-snapshot/NT/{camera_id}',
                    'snapshotFromVideo': False,
                },
            })
        return {'item2': items}
    if layer == 'MessageSigns':
        return {'item2': []}
    items = []
    for item_id, lat, lon, label, issue in drivenwt_issue_records(layer):
        description = issue.get('Description') or {}
        table = issue.get('TableViewInfo') or {}
        items.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': label,
            'expando': {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Road Advisory',
                'description': display_text(description.get('BaseDescription')) or label,
                'severity': str(issue.get('Priority') or '') or None,
                'timestamp': epoch_milliseconds_iso(description.get('UpdateTimeUtcEpochMillis')),
                'location': display_text(table.get('Location')) or 'Northwest Territories',
            },
        })
    return {'item2': items}


def drivenwt_camera_detail(site_id):
    target = str(site_id)
    for entity in drivenwt_data().get('Entities') or []:
        parts = str(entity.get('EntityId') or '').split('|')
        if safe_int(entity.get('EntityType')) != 3 or len(parts) < 2 or parts[1] != target:
            continue
        image_path = str(entity.get('ImageUrl') or '').strip()
        upstream = urllib.parse.urljoin(DRIVENWT_SOURCE_URL, image_path) if image_path else None
        return {
            'name': display_text(entity.get('Location')) or f'DriveNWT camera {target}',
            'msg': 'Northwest Territories highway camera',
            'severity': None,
            'timestamp': None,
            'location': display_text(entity.get('Location')) or 'Northwest Territories',
            'video_id': target,
            'video_url': None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/NT/{target}' if upstream else None,
            'upstream_snapshot_url': upstream,
        }
    raise ValueError(f'DriveNWT camera {site_id} not found')


def drivenwt_tooltip(layer, item_id):
    if layer == 'Cameras':
        return drivenwt_camera_detail(item_id)
    target = str(item_id)
    for event_id, _lat, _lon, label, issue in drivenwt_issue_records(layer):
        if event_id != target:
            continue
        description = issue.get('Description') or {}
        table = issue.get('TableViewInfo') or {}
        return {
            'name': label,
            'msg': display_text(description.get('BaseDescription')) or label,
            'severity': str(issue.get('Priority') or '') or None,
            'timestamp': epoch_milliseconds_iso(description.get('UpdateTimeUtcEpochMillis')),
            'location': display_text(table.get('Location')) or 'Northwest Territories',
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'DriveNWT {layer} item {item_id} not found')


def quebec_511_dataset(kind):
    return fetch_extended_traffic_json(
        f'quebec511:{kind}',
        QUEBEC_CAMERAS_URL if kind == 'cameras' else QUEBEC_CONSTRUCTION_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/geo+json,application/json',
            'Referer': 'https://www.donneesquebec.ca/',
        },
        timeout=60,
        ttl=120 if kind == 'cameras' else 60,
    ).get('features') or []


def quebec_511_layer_payload(layer):
    if layer == 'Cameras':
        items = []
        for feature in quebec_511_dataset('cameras'):
            props = feature.get('properties') or {}
            camera_id = str(props.get('IDEcamera') or feature.get('id') or '').strip()
            center = geojson_center(feature.get('geometry'))
            if not valid_numeric_id(camera_id) or not center:
                continue
            lat, lon = center
            if not point_in_state('QC', lat, lon):
                continue
            items.append({
                'itemId': camera_id,
                'location': [lat, lon],
                'title': display_text(
                    props.get('DescriptionLocalisationEn') or props.get('DescriptionLocalisationFr')
                ) or f'Québec 511 camera {camera_id}',
                'expando': {
                    'videoEnabled': False,
                    'videoId': camera_id,
                    # The official catalogue exposes a viewer URL, but the
                    # viewer blocks server-side image retrieval. Keep the
                    # camera and its metadata without pretending a still works.
                    'snapshotUrl': None,
                    'snapshotFromVideo': False,
                },
            })
        return {'item2': items}
    if layer in {'MessageSigns', 'Incidents'}:
        return {'item2': []}
    items = []
    for feature in quebec_511_dataset('construction'):
        props = feature.get('properties') or {}
        item_id = str(props.get('identifiant') or feature.get('id') or '').strip()
        center = geojson_center(feature.get('geometry'))
        if not valid_numeric_id(item_id) or not center:
            continue
        lat, lon = center
        if not point_in_state('QC', lat, lon):
            continue
        title = display_text(
            props.get('descriptionAnglais') or props.get('identificationDesTravaux')
        ) or 'Road construction'
        items.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title.splitlines()[0],
            'expando': {
                'feedLabel': 'Construction',
                'description': title,
                'severity': display_text(props.get('entraveType')) or None,
                'timestamp': props.get('miseAJour'),
                'location': display_text(props.get('localisation')) or 'Quebec',
            },
        })
    return {'item2': items}


def quebec_511_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        for feature in quebec_511_dataset('cameras'):
            props = feature.get('properties') or {}
            if str(props.get('IDEcamera') or feature.get('id') or '') != target:
                continue
            name = display_text(
                props.get('DescriptionLocalisationEn') or props.get('DescriptionLocalisationFr')
            ) or f'Québec 511 camera {target}'
            return {
                'name': name,
                'msg': display_text(props.get('NomRegionDiffusion')) or 'Québec 511 traffic camera',
                'severity': None,
                'timestamp': props.get('DateDebutDiffusion'),
                'location': name,
                'video_id': target,
                'video_url': None,
                'video_enabled': False,
                'snapshot_url': None,
            }
    elif layer == 'Construction':
        for feature in quebec_511_dataset('construction'):
            props = feature.get('properties') or {}
            if str(props.get('identifiant') or feature.get('id') or '') != target:
                continue
            return {
                'name': display_text(props.get('identificationDesTravaux')) or 'Road construction',
                'msg': display_text(props.get('descriptionAnglais') or props.get('descriptionFrancais')),
                'severity': display_text(props.get('entraveType')) or None,
                'timestamp': props.get('miseAJour'),
                'location': display_text(props.get('localisation')) or 'Quebec',
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
    raise ValueError(f'Québec 511 {layer} item {item_id} not found')


def hawaii_lane_closures():
    query = urllib.parse.urlencode({
        'where': 'beginDate <= CURRENT_TIMESTAMP AND enDate >= CURRENT_TIMESTAMP',
        'outFields': '*',
        'returnGeometry': 'true',
        'outSR': 4326,
        'f': 'geojson',
    })
    payload = fetch_extended_traffic_json(
        'goakamai:lane-closures',
        f'{HAWAII_LANE_CLOSURES_URL}?{query}',
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/geo+json,application/json',
            'Referer': 'https://hidot.hawaii.gov/highways/roadwork/',
        },
        timeout=60,
        ttl=300,
    )
    return payload.get('features') or []


def goakamai_cameras():
    payload = fetch_extended_traffic_json(
        'goakamai:cameras',
        GOAKAMAI_CAMERAS_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/json,text/plain,*/*',
            'Origin': GOAKAMAI_SOURCE_URL.rstrip('/'),
            'Referer': f'{GOAKAMAI_SOURCE_URL}cameras/',
            'x-icx-copyright': 'ICxTransportationGroup',
            'x-icx-ts': str(int(time.time() * 1000)),
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        },
        ttl=60,
    )
    return payload if isinstance(payload, list) else []


def goakamai_camera_id(device_id):
    digest = hashlib.sha256(f'GOAKAMAI:{device_id}'.encode('utf-8')).digest()
    return str(int.from_bytes(digest[:7], 'big'))


def goakamai_camera_records():
    records = []
    for camera in goakamai_cameras():
        device_id = str(camera.get('deviceID') or camera.get('id') or '').strip()
        lat = optional_float(camera.get('lat'))
        lon = optional_float(camera.get('lon'))
        if (
            not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', device_id) or
            lat is None or lon is None or not point_in_state('HI', lat, lon)
        ):
            continue
        snapshot_url = str(camera.get('cameraImageURL') or '').strip()
        parsed_snapshot = urllib.parse.urlparse(snapshot_url)
        if parsed_snapshot.hostname != 'cctv.cdn.goakamai.org':
            snapshot_url = ''
        elif parsed_snapshot.scheme == 'http':
            snapshot_url = urllib.parse.urlunparse(parsed_snapshot._replace(scheme='https'))
        elif parsed_snapshot.scheme != 'https':
            snapshot_url = ''
        records.append({
            'id': goakamai_camera_id(device_id),
            'device_id': device_id,
            'name': display_text(camera.get('description')) or f'GoAkamai camera {device_id}',
            'lat': lat,
            'lon': lon,
            'snapshot_url': snapshot_url,
            'last_update': camera.get('lastUpdate') or None,
        })
    return records


def goakamai_camera_detail(site_id):
    target = str(site_id)
    for camera in goakamai_camera_records():
        if camera['id'] != target:
            continue
        return {
            'name': camera['name'],
            'msg': f'GoAkamai camera {camera["device_id"]}',
            'severity': None,
            'timestamp': camera['last_update'],
            'location': 'Hawaii',
            'video_id': target,
            'video_url': None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/HI/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
        }
    raise ValueError(f'GoAkamai camera {site_id} not found')


def goakamai_layer_payload(layer):
    # HDOT retired its public incident feed in 2021. Cameras and active lane
    # closures remain available from GoAkamai and HDOT respectively.
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': False,
                'videoId': camera['id'],
                'snapshotUrl': f'/camera-snapshot/HI/{camera["id"]}' if camera['snapshot_url'] else None,
                'snapshotFromVideo': False,
            },
        } for camera in goakamai_camera_records()]}
    if layer != 'Construction':
        return {'item2': []}
    items = []
    for feature in hawaii_lane_closures():
        props = feature.get('properties') or {}
        item_id = str(props.get('OBJECTID') or feature.get('id') or '').strip()
        center = geojson_center(feature.get('geometry'))
        if not valid_numeric_id(item_id) or not center:
            continue
        lat, lon = center
        if not point_in_state('HI', lat, lon):
            continue
        title = display_text(props.get('ClosReason')) or 'Lane closure'
        items.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': {
                'feedLabel': 'Construction',
                'description': display_text(props.get('Remarks')) or title,
                'severity': display_text(props.get('CloseFact')) or None,
                'timestamp': epoch_milliseconds_iso(props.get('EditDate')),
                'location': ' · '.join(part for part in (
                    display_text(props.get('Island')),
                    display_text(props.get('Route')),
                    display_text(props.get('IntersFrom')),
                    display_text(props.get('IntersTo')),
                ) if part) or 'Hawaii',
            },
        })
    return {'item2': items}


def goakamai_tooltip(layer, item_id):
    if layer == 'Cameras':
        return goakamai_camera_detail(item_id)
    if layer == 'Construction':
        target = str(item_id)
        for feature in hawaii_lane_closures():
            props = feature.get('properties') or {}
            if str(props.get('OBJECTID') or feature.get('id') or '') != target:
                continue
            return {
                'name': display_text(props.get('ClosReason')) or 'Lane closure',
                'msg': display_text(props.get('Remarks')) or 'Hawaii DOT lane closure',
                'severity': display_text(props.get('CloseFact')) or None,
                'timestamp': epoch_milliseconds_iso(props.get('EditDate')),
                'location': ' · '.join(part for part in (
                    display_text(props.get('Island')),
                    display_text(props.get('Route')),
                    display_text(props.get('IntersFrom')),
                    display_text(props.get('IntersTo')),
                ) if part) or 'Hawaii',
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
    raise ValueError(f'GoAkamai {layer} item {item_id} not found')


def ontario_511_events():
    payload = fetch_extended_traffic_json(
        'ontario511:events',
        ONTARIO_511_EVENTS_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/json',
            'Referer': 'https://511on.ca/',
        },
        timeout=45,
    )
    return payload if isinstance(payload, list) else []


def ontario_511_event_records(layer):
    if layer not in {'Incidents', 'Construction'}:
        return []
    wants_construction = layer == 'Construction'
    records = []
    for event in ontario_511_events():
        is_construction = str(event.get('EventType') or '').lower() == 'roadwork'
        if is_construction != wants_construction:
            continue
        item_id = str(event.get('ID') or '').strip()
        lat = optional_float(event.get('Latitude'))
        lon = optional_float(event.get('Longitude'))
        if (
            not valid_numeric_id(item_id) or lat is None or lon is None or
            not point_in_state('ON', lat, lon)
        ):
            continue
        records.append((item_id, lat, lon, event))
    return records


def ontario_511_layer_payload(layer):
    if layer == 'Cameras':
        payload = json.loads(fetch_iteris_layer(REGIONS['ON'], layer))
        for item in payload.get('item2') or []:
            item_id = str(item.get('itemId') or '')
            if not valid_numeric_id(item_id):
                continue
            expando = item.setdefault('expando', {})
            expando['snapshotUrl'] = f'/camera-snapshot/ON/{item_id}'
            expando.setdefault('snapshotFromVideo', False)
        return payload
    if layer == 'MessageSigns':
        return {'item2': []}
    items = []
    for item_id, lat, lon, event in ontario_511_event_records(layer):
        roadway = display_text(event.get('RoadwayName'))
        direction = display_text(event.get('DirectionOfTravel'))
        title = ' · '.join(part for part in (roadway, direction) if part) or (
            'Road construction' if layer == 'Construction' else 'Traffic incident'
        )
        items.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': display_text(event.get('Description')) or title,
                'severity': 'Full closure' if event.get('IsFullClosure') else (
                    display_text(event.get('Impact') or event.get('Severity')) or None
                ),
                'timestamp': epoch_milliseconds_iso(event.get('LastUpdated')),
                'location': title,
            },
        })
    return {'item2': items}


def ontario_511_tooltip(layer, item_id):
    if layer == 'Cameras':
        return iteris_tooltip(REGIONS['ON'], layer, item_id)
    target = str(item_id)
    for event_id, _lat, _lon, event in ontario_511_event_records(layer):
        if event_id != target:
            continue
        roadway = display_text(event.get('RoadwayName'))
        direction = display_text(event.get('DirectionOfTravel'))
        return {
            'name': ' · '.join(part for part in (roadway, direction) if part) or 'Ontario 511 event',
            'msg': display_text(event.get('Description')) or 'Traffic event',
            'severity': 'Full closure' if event.get('IsFullClosure') else (
                display_text(event.get('Impact') or event.get('Severity')) or None
            ),
            'timestamp': epoch_milliseconds_iso(event.get('LastUpdated')),
            'location': roadway or 'Ontario',
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'Ontario 511 {layer} item {item_id} not found')


def fetch_cotrip_dataset(resource, query=''):
    cache_key = f'{resource}?{query}'
    now = time.time()
    with COTRIP_TRAFFIC_CACHE_LOCK:
        cached = COTRIP_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['features']
    url = f'{COTRIP_API_ROOT}/{resource}/map-features'
    if query:
        url = f'{url}?{query}'
    payload = fetch_json_url(url, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': COTRIP_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
        'content-language': 'en',
    }, timeout=30)
    features = payload.get('features') if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise ValueError(f'COtrip {resource} returned no feature array')
    with COTRIP_TRAFFIC_CACHE_LOCK:
        if len(COTRIP_TRAFFIC_CACHE) >= 12:
            COTRIP_TRAFFIC_CACHE.pop(next(iter(COTRIP_TRAFFIC_CACHE)))
        COTRIP_TRAFFIC_CACHE[cache_key] = {
            'features': features,
            'expires_at': time.time() + COTRIP_CACHE_TTL,
        }
    return features


def cotrip_camera_view(props):
    views = [view for view in (props.get('views') or []) if not view.get('broken')]
    return next((view for view in views if view.get('type') == 'WMP'), None) or (
        views[0] if views else None
    )


def cotrip_feature_center(feature):
    props = feature.get('properties') or {}
    primary = (((props.get('eventReport') or {}).get('location') or {}).get('primaryPoint') or {})
    primary_lat = optional_float(primary.get('lat'))
    primary_lon = optional_float(primary.get('lon'))
    if primary_lat is not None and primary_lon is not None:
        return primary_lat, primary_lon
    geometry = feature.get('geometry') or {}
    coordinates = geometry.get('coordinates') or []
    if geometry.get('type') == 'Point' and len(coordinates) >= 2:
        return safe_float(coordinates[1]), safe_float(coordinates[0])
    points = []

    def collect(value):
        if (
            isinstance(value, (list, tuple)) and len(value) >= 2 and
            isinstance(value[0], (int, float)) and isinstance(value[1], (int, float))
        ):
            points.append((float(value[1]), float(value[0])))
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(coordinates)
    if points:
        return (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
    return None


def cotrip_item_id(layer, raw_id):
    if layer == 'Cameras' and str(raw_id).isdigit():
        return str(raw_id)
    return str(wv511_numeric_id(f'COTRIP:{layer}:{raw_id}'))


def cotrip_records(layer):
    if layer == 'Cameras':
        features = fetch_cotrip_dataset('cameras')
    elif layer == 'MessageSigns':
        features = fetch_cotrip_dataset('signs')
    elif layer == 'Incidents':
        features = fetch_cotrip_dataset('events', 'eventClassifications=roadReports')
    elif layer == 'Construction':
        features = fetch_cotrip_dataset(
            'events', 'eventClassifications=roadWork&maxBeginDateOffset=7200000'
        )
    else:
        return []
    records = []
    for feature in features:
        props = feature.get('properties') or {}
        raw_id = props.get('id')
        center = cotrip_feature_center(feature)
        if raw_id is None or not center:
            continue
        lat, lon = center
        if lat is None or lon is None or not point_in_state('CO', lat, lon):
            continue
        records.append({
            'id': cotrip_item_id(layer, raw_id),
            'raw_id': str(raw_id),
            'lat': lat,
            'lon': lon,
            'props': props,
        })
    return records


def cotrip_layer_payload(layer):
    normalized = []
    for record in cotrip_records(layer):
        props = record['props']
        if layer == 'Cameras':
            view = cotrip_camera_view(props)
            video_url = str((view or {}).get('url') or '').strip()
            is_video = (view or {}).get('type') == 'WMP' and video_url.endswith('.m3u8')
            snapshot_url = (
                str((view or {}).get('videoPreviewUrl') or '').strip() or
                (str((view or {}).get('url') or '').strip() if not is_video else '')
            )
            expando = {
                'videoEnabled': is_video,
                'videoId': record['id'],
                'videoUrl': video_url if is_video else None,
                'snapshotUrl': f'/camera-snapshot/CO/{record["id"]}' if snapshot_url else None,
                'snapshotFromVideo': False,
            }
        elif layer == 'MessageSigns':
            expando = {
                'message': display_text(props.get('status')).replace('_', ' ').title(),
                'timestamp': epoch_milliseconds_iso(props.get('updated')),
            }
        else:
            description = strip_tags(props.get('description'))
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': description,
                'severity': display_text(props.get('eventType')).title() or None,
                'timestamp': epoch_milliseconds_iso(props.get('updated')),
                'location': display_text(
                    (((props.get('eventReport') or {}).get('eventDescription') or {}).get('descriptionBrief'))
                ) or None,
            }
        normalized.append({
            'itemId': record['id'],
            'location': [record['lat'], record['lon']],
            'title': display_text(props.get('name') or props.get('title') or props.get('tooltip')),
            'expando': expando,
        })
    return {'item2': normalized}


def cotrip_record(layer, item_id):
    target = str(item_id)
    for record in cotrip_records(layer):
        if record['id'] == target:
            return record
    raise ValueError(f'COtrip {layer} item {item_id} not found')


def cotrip_camera_detail(site_id):
    record = cotrip_record('Cameras', site_id)
    props = record['props']
    view = cotrip_camera_view(props)
    video_url = str((view or {}).get('url') or '').strip()
    is_video = (view or {}).get('type') == 'WMP' and video_url.endswith('.m3u8')
    snapshot_url = (
        str((view or {}).get('videoPreviewUrl') or '').strip() or
        (str((view or {}).get('url') or '').strip() if not is_video else '')
    )
    return {
        'name': display_text((view or {}).get('name') or props.get('name')) or f'CDOT camera {site_id}',
        'msg': display_text(props.get('cameraOwner')) or 'Colorado DOT traffic camera',
        'severity': None,
        'timestamp': epoch_milliseconds_iso((view or {}).get('imageTimestamp') or props.get('lastUpdated')),
        'video_id': str(site_id),
        'video_url': video_url if is_video else None,
        'video_enabled': is_video,
        'snapshot_url': f'/camera-snapshot/CO/{site_id}' if snapshot_url else None,
        'upstream_snapshot_url': snapshot_url or None,
    }


def cotrip_tooltip(layer, item_id):
    if layer == 'Cameras':
        return cotrip_camera_detail(item_id)
    record = cotrip_record(layer, item_id)
    props = record['props']
    if layer == 'MessageSigns':
        message = display_text(props.get('status')).replace('_', ' ').title()
        severity = display_text(props.get('signFacingDirection')) or None
    else:
        message = strip_tags(props.get('description'))
        severity = display_text(props.get('eventType')).title() or None
    return {
        'name': display_text(props.get('name') or props.get('title') or props.get('tooltip')),
        'msg': message,
        'severity': severity,
        'timestamp': epoch_milliseconds_iso(props.get('updated')),
        'location': display_text(
            (((props.get('eventReport') or {}).get('eventDescription') or {}).get('descriptionBrief'))
        ) or None,
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def fetch_midrive_dataset(resource):
    now = time.time()
    with MIDRIVE_TRAFFIC_CACHE_LOCK:
        cached = MIDRIVE_TRAFFIC_CACHE.get(resource)
        if cached and now < cached['expires_at']:
            return cached['items']
    paths = {
        'Cameras': 'camera/AllForMap',
        'MessageSigns': 'dms/AllForMap',
        'Incidents': 'incidents/AllForMap/',
        'Construction': 'construction/AllForMap/',
    }
    path = paths.get(resource)
    if not path:
        return []
    source_url = urllib.parse.urljoin(MIDRIVE_ROOT, path)
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': urllib.parse.urljoin(MIDRIVE_ROOT, 'map'),
        'Accept': 'application/json',
    }
    # Mi Drive occasionally emits a transient 404 when its map datasets are
    # fetched together. One short retry keeps a single upstream race from
    # blanking an otherwise healthy layer.
    last_error = None
    for attempt in range(2):
        try:
            items = fetch_json_url(source_url, headers=headers, timeout=30)
            break
        except urllib.error.HTTPError as error:
            last_error = error
            if attempt or error.code not in {404, 429, 500, 502, 503, 504}:
                raise
            time.sleep(0.2)
    else:
        raise last_error
    if not isinstance(items, list):
        raise ValueError(f'Mi Drive {resource} returned no item array')
    with MIDRIVE_TRAFFIC_CACHE_LOCK:
        MIDRIVE_TRAFFIC_CACHE[resource] = {
            'items': items,
            'expires_at': time.time() + MIDRIVE_CACHE_TTL,
        }
    return items


def midrive_item_id(layer, raw_id):
    if layer in {'Cameras', 'MessageSigns', 'Incidents'} and str(raw_id).isdigit():
        return str(raw_id)
    return str(wv511_numeric_id(f'MIDRIVE:{layer}:{raw_id}'))


def midrive_records(layer):
    records = []
    for item in fetch_midrive_dataset(layer):
        raw_id = str(item.get('id') or '').strip()
        lat = optional_float(item.get('latitude'))
        lon = optional_float(item.get('longitude'))
        if not raw_id or lat is None or lon is None or not point_in_state('MI', lat, lon):
            continue
        records.append({
            'id': midrive_item_id(layer, raw_id),
            'raw_id': raw_id,
            'lat': lat,
            'lon': lon,
            'item': item,
        })
    return records


def midrive_layer_payload(layer):
    normalized = []
    for record in midrive_records(layer):
        item = record['item']
        if layer == 'Cameras':
            expando = {
                'videoEnabled': False,
                'videoId': record['id'],
                'snapshotUrl': f'/camera-snapshot/MI/{record["id"]}',
            }
        elif layer == 'MessageSigns':
            expando = {'message': '', 'timestamp': None}
        else:
            description = strip_tags(item.get('message')) if layer == 'Incidents' else display_text(item.get('title'))
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': description,
                'severity': 'Clearing' if str(item.get('title') or '').lower().startswith('cleared') else None,
                'timestamp': None,
                'location': None,
            }
        normalized.append({
            'itemId': record['id'],
            'location': [record['lat'], record['lon']],
            'title': display_text(item.get('title')) or f'Michigan {layer}',
            'expando': expando,
        })
    return {'item2': normalized}


def midrive_record(layer, item_id):
    target = str(item_id)
    for record in midrive_records(layer):
        if record['id'] == target:
            return record
    raise ValueError(f'Mi Drive {layer} item {item_id} not found')


def midrive_detail(path):
    return fetch_json_url(urllib.parse.urljoin(MIDRIVE_ROOT, path), headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': urllib.parse.urljoin(MIDRIVE_ROOT, 'map'),
        'Accept': 'application/json',
    }, timeout=20)


def midrive_camera_detail(site_id):
    record = midrive_record('Cameras', site_id)
    detail = midrive_detail(f'camera/getCameraInformation/{urllib.parse.quote(record["raw_id"])}')
    snapshot_url = str((detail or {}).get('link') or '').strip()
    return {
        'name': display_text((detail or {}).get('title')) or f'MDOT camera {site_id}',
        'msg': display_text((detail or {}).get('orientation')) or 'Michigan DOT traffic camera',
        'severity': None,
        'timestamp': None,
        'video_id': str(site_id),
        'video_url': None,
        'video_enabled': False,
        'snapshot_url': f'/camera-snapshot/MI/{site_id}' if snapshot_url else None,
        'upstream_snapshot_url': snapshot_url or None,
    }


def midrive_tooltip(layer, item_id):
    if layer == 'Cameras':
        return midrive_camera_detail(item_id)
    record = midrive_record(layer, item_id)
    item = record['item']
    if layer == 'MessageSigns':
        detail = midrive_detail(f'dms/getDMSInfo/{urllib.parse.quote(record["raw_id"])}')
        message = strip_tags(detail[0]) if isinstance(detail, list) and detail else ''
        name = display_text(detail[1]) if isinstance(detail, list) and len(detail) > 1 else display_text(item.get('title'))
        severity = None
    elif layer == 'Construction':
        detail = midrive_detail(
            f'construction/getConstructionInformation/{urllib.parse.quote(record["raw_id"])}'
        )
        message = strip_tags(detail[0]) if isinstance(detail, list) and detail else display_text(item.get('title'))
        name = display_text(detail[1]) if isinstance(detail, list) and len(detail) > 1 else display_text(item.get('title'))
        severity = 'Construction'
    else:
        message = strip_tags(item.get('message'))
        name = display_text(item.get('title'))
        severity = 'Clearing' if name.lower().startswith('cleared') else None
    return {
        'name': name or f'Michigan {layer}',
        'msg': message,
        'severity': severity,
        'timestamp': None,
        'location': None,
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def wy511_read_varint(data, offset):
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError('Invalid WYDOT protobuf varint')


def wy511_protobuf_fields(data):
    fields = []
    offset = 0
    while offset < len(data):
        tag, offset = wy511_read_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 7
        if field_number <= 0:
            raise ValueError('Invalid WYDOT protobuf field')
        if wire_type == 0:
            value, offset = wy511_read_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValueError('Truncated WYDOT protobuf double')
            value = struct.unpack('<d', data[offset:offset + 8])[0]
            offset += 8
        elif wire_type == 2:
            length, offset = wy511_read_varint(data, offset)
            if length < 0 or offset + length > len(data):
                raise ValueError('Truncated WYDOT protobuf message')
            value = data[offset:offset + length]
            offset += length
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError('Truncated WYDOT protobuf float')
            value = struct.unpack('<f', data[offset:offset + 4])[0]
            offset += 4
        else:
            raise ValueError(f'Unsupported WYDOT protobuf wire type {wire_type}')
        fields.append((field_number, wire_type, value))
    return fields


def wy511_field(fields, number, default=None):
    return next((value for field, _, value in fields if field == number), default)


def wy511_fields(fields, number):
    return [value for field, _, value in fields if field == number]


def wy511_text(value):
    if value is None:
        return ''
    if not isinstance(value, (bytes, bytearray)):
        return display_text(value)
    raw_text = bytes(value).decode('utf-8', 'replace')
    compact = ''.join(raw_text.split())
    if compact and len(compact) % 4 == 0 and re.fullmatch(r'[A-Za-z0-9+/]*={0,2}', compact):
        try:
            encrypted = base64.b64decode(compact, validate=True)
            decoded = bytes(
                byte ^ WY511_XOR_KEY[index % len(WY511_XOR_KEY)]
                for index, byte in enumerate(encrypted)
            ).decode('utf-8')
            if decoded and all(character.isprintable() or character in '\r\n\t' for character in decoded):
                return display_text(decoded)
        except (ValueError, UnicodeDecodeError):
            pass
    return display_text(raw_text)


def fetch_wy511_feed(resource):
    now = time.time()
    with WY511_TRAFFIC_CACHE_LOCK:
        cached = WY511_TRAFFIC_CACHE.get(resource)
        if cached and now < cached['expires_at']:
            return cached['body']
    url = WY511_FEED_URLS.get(resource)
    if not url:
        raise ValueError(f'Unknown Wyoming 511 resource {resource}')
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': WY511_SOURCE_URL,
        'Accept': 'application/octet-stream,*/*',
    })
    body = urllib.request.urlopen(request, timeout=30).read()
    if not body:
        raise ValueError(f'Wyoming 511 {resource} feed was empty')
    with WY511_TRAFFIC_CACHE_LOCK:
        WY511_TRAFFIC_CACHE[resource] = {
            'body': body,
            'expires_at': time.time() + WY511_CACHE_TTL,
        }
    return body


def wy511_report_time(value):
    try:
        return datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def wy511_dms_reports():
    reports = {}
    for _, wire_type, message in wy511_protobuf_fields(fetch_wy511_feed('MessageSignReports')):
        if wire_type != 2:
            continue
        fields = wy511_protobuf_fields(message)
        sign_id = str(wy511_field(fields, 1, ''))
        screens = []
        for screen_message in wy511_fields(fields, 4):
            screen_fields = wy511_protobuf_fields(screen_message)
            lines = []
            for line_message in wy511_fields(screen_fields, 3):
                line_fields = wy511_protobuf_fields(line_message)
                line = wy511_text(wy511_field(line_fields, 2))
                if line:
                    lines.append(line)
            if lines:
                screens.append(' '.join(lines))
        reports[sign_id] = {
            'message': ' / '.join(dict.fromkeys(screens)),
            'timestamp': wy511_report_time(wy511_field(fields, 2)),
        }
    return reports


def wy511_records(layer):
    records = []
    dms_reports = wy511_dms_reports() if layer == 'MessageSigns' else {}
    for _, wire_type, message in wy511_protobuf_fields(fetch_wy511_feed(layer)):
        if wire_type != 2:
            continue
        fields = wy511_protobuf_fields(message)
        if layer == 'Cameras':
            raw_id = str(wy511_field(fields, 1, ''))
            name = wy511_text(wy511_field(fields, 2)) or f'WYDOT camera {raw_id}'
            lon = optional_float(wy511_field(fields, 8))
            lat = optional_float(wy511_field(fields, 9))
            images = []
            for image_message in wy511_fields(fields, 3):
                image_fields = wy511_protobuf_fields(image_message)
                snapshot_url = wy511_text(wy511_field(image_fields, 3))
                if not snapshot_url.startswith('https://www.wyoroad.info/web-cam/cache?'):
                    snapshot_url = ''
                images.append({
                    'id': str(wy511_field(image_fields, 1, '')),
                    'name': wy511_text(wy511_field(image_fields, 2)),
                    'snapshot_url': snapshot_url,
                })
            extra = {'images': images}
        elif layer == 'MessageSigns':
            raw_id = str(wy511_field(fields, 1, ''))
            name = wy511_text(wy511_field(fields, 2)) or f'WYDOT message sign {raw_id}'
            lon = optional_float(wy511_field(fields, 4))
            lat = optional_float(wy511_field(fields, 5))
            extra = dms_reports.get(raw_id) or {'message': '', 'timestamp': None}
        elif layer == 'Incidents':
            raw_id = str(wy511_field(fields, 1, ''))
            incident_type = wy511_text(wy511_field(fields, 2))
            description = wy511_text(wy511_field(fields, 3))
            name = incident_type or description or f'Wyoming incident {raw_id}'
            lon = optional_float(wy511_field(fields, 8))
            lat = optional_float(wy511_field(fields, 9))
            impact_code = wy511_text(wy511_field(fields, 6)).upper()
            severity = {
                'C': 'Closed', 'CI': 'Closed', 'P': 'Partial Closure', 'PI': 'Partial Closure',
                'H': 'High Impact', 'HI': 'High Impact', 'M': 'Moderate Impact',
                'MI': 'Moderate Impact', 'L': 'Low Impact', 'LI': 'Low Impact',
            }.get(impact_code)
            extra = {
                'description': description,
                'severity': severity,
                'timestamp': wy511_report_time(wy511_field(fields, 4)),
                'location': wy511_text(wy511_field(fields, 5)) or None,
            }
        else:
            raw_id = str(wy511_field(fields, 3, ''))
            lon = optional_float(wy511_field(fields, 1))
            lat = optional_float(wy511_field(fields, 2))
            name = wy511_text(wy511_field(fields, 4)) or f'Wyoming construction {raw_id}'
            description_parts = [
                wy511_text(wy511_field(fields, field))
                for field in (8, 19, 15)
            ]
            routes = [wy511_text(value) for value in wy511_fields(fields, 22)]
            towns = [wy511_text(value) for value in wy511_fields(fields, 23)]
            extra = {
                'description': ' · '.join(dict.fromkeys(part for part in description_parts if part)) or name,
                'severity': wy511_text(wy511_field(fields, 15)) or 'Construction',
                'timestamp': wy511_text(wy511_field(fields, 20)) or None,
                'location': ' · '.join(dict.fromkeys(part for part in routes + towns if part)) or None,
            }
        if not raw_id or lat is None or lon is None or not point_in_state('WY', lat, lon):
            continue
        records.append({
            'id': raw_id,
            'lat': lat,
            'lon': lon,
            'name': name,
            **extra,
        })
    return records


def wy511_layer_payload(layer):
    normalized = []
    for record in wy511_records(layer):
        if layer == 'Cameras':
            expando = {
                'videoEnabled': False,
                'videoId': record['id'],
                'snapshotUrl': (
                    f'/camera-snapshot/WY/{record["id"]}'
                    if any(image.get('snapshot_url') for image in record['images']) else None
                ),
            }
        elif layer == 'MessageSigns':
            expando = {
                'message': record.get('message') or '',
                'timestamp': record.get('timestamp'),
            }
        else:
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': record.get('description'),
                'severity': record.get('severity'),
                'timestamp': record.get('timestamp'),
                'location': record.get('location'),
            }
        normalized.append({
            'itemId': record['id'],
            'location': [record['lat'], record['lon']],
            'title': record['name'],
            'expando': expando,
        })
    return {'item2': normalized}


def wy511_record(layer, item_id):
    target = str(item_id)
    for record in wy511_records(layer):
        if record['id'] == target:
            return record
    raise ValueError(f'Wyoming 511 {layer} item {item_id} not found')


def wy511_camera_detail(site_id):
    record = wy511_record('Cameras', site_id)
    image = next((item for item in record['images'] if item.get('snapshot_url')), None)
    return {
        'name': (image or {}).get('name') or record['name'],
        'msg': record['name'],
        'severity': None,
        'timestamp': None,
        'video_id': str(site_id),
        'video_url': None,
        'video_enabled': False,
        'snapshot_url': f'/camera-snapshot/WY/{site_id}' if image else None,
        'upstream_snapshot_url': (image or {}).get('snapshot_url'),
    }


def wy511_tooltip(layer, item_id):
    if layer == 'Cameras':
        return wy511_camera_detail(item_id)
    record = wy511_record(layer, item_id)
    return {
        'name': record['name'],
        'msg': record.get('message') or record.get('description') or '',
        'severity': record.get('severity'),
        'timestamp': record.get('timestamp'),
        'location': record.get('location'),
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def fetch_mt511_dataset(resource):
    now = time.time()
    with MT511_TRAFFIC_CACHE_LOCK:
        cached = MT511_TRAFFIC_CACHE.get(resource)
        if cached and now < cached['expires_at']:
            return cached['features']
    filenames = {
        'Cameras': 'icons.cameras.geojson',
        'RWISCameras': 'icons.rwis.geojson',
        'MessageSigns': 'icons.dms.geojson',
        'Incidents': 'icons.events.geojson',
        'Construction': 'icons.construction.geojson',
    }
    filename = filenames.get(resource)
    if not filename:
        raise ValueError(f'Unknown Montana 511 resource {resource}')
    data = fetch_json_url(
        urllib.parse.urljoin(MT511_DATA_ROOT, filename),
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': MT511_SOURCE_URL,
            'Accept': 'application/geo+json,application/json',
        },
        timeout=30,
    )
    features = data.get('features') if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise ValueError(f'Montana 511 {resource} returned no feature array')
    with MT511_TRAFFIC_CACHE_LOCK:
        MT511_TRAFFIC_CACHE[resource] = {
            'features': features,
            'expires_at': time.time() + MT511_CACHE_TTL,
        }
    return features


def mt511_item_id(layer, raw_id):
    if str(raw_id).isdigit():
        return str(raw_id)
    return str(wv511_numeric_id(f'MT511:{layer}:{raw_id}'))


def mt511_records(layer):
    resources = ('Cameras', 'RWISCameras') if layer == 'Cameras' else (layer,)
    records = []
    seen = set()
    for resource in resources:
        for feature in fetch_mt511_dataset(resource):
            props = feature.get('properties') or {}
            coords = (feature.get('geometry') or {}).get('coordinates') or []
            lon = optional_float(coords[0]) if len(coords) >= 2 else None
            lat = optional_float(coords[1]) if len(coords) >= 2 else None
            raw_id = display_text(props.get('id') or props.get('event_id') or feature.get('id'))
            if not raw_id or lat is None or lon is None or not point_in_state('MT', lat, lon):
                continue
            item_id = mt511_item_id(layer, raw_id)
            if item_id in seen:
                continue
            seen.add(item_id)
            if layer == 'Cameras':
                images = []
                for image in props.get('cameras') or []:
                    snapshot_url = str(image.get('image') or '').strip()
                    if not snapshot_url.startswith('https://mt.cdn.iteris-atis.com/'):
                        snapshot_url = ''
                    images.append({
                        'name': display_text(image.get('name') or image.get('description')),
                        'snapshot_url': snapshot_url,
                        'timestamp': epoch_milliseconds_iso(image.get('updateTime')),
                    })
                if not any(image.get('snapshot_url') for image in images):
                    continue
                name = display_text(props.get('name') or props.get('description')) or f'MDT camera {raw_id}'
                extra = {'images': images}
            elif layer == 'MessageSigns':
                name = display_text(props.get('name')) or f'MDT message sign {raw_id}'
                extra = {
                    'message': strip_tags(props.get('report')),
                    'timestamp': None,
                }
            else:
                name = display_text(props.get('headline')) or (
                    'Montana road construction' if layer == 'Construction' else 'Montana traffic incident'
                )
                extra = {
                    'description': strip_tags(props.get('report') or props.get('enhanced_report')),
                    'severity': display_text(props.get('headline') or props.get('category')).title() or None,
                    'timestamp': epoch_milliseconds_iso(props.get('last_update')),
                    'location': strip_tags(props.get('location_description') or props.get('label')) or None,
                }
            records.append({
                'id': item_id,
                'raw_id': raw_id,
                'lat': lat,
                'lon': lon,
                'name': name,
                **extra,
            })
    return records


def mt511_layer_payload(layer):
    normalized = []
    for record in mt511_records(layer):
        if layer == 'Cameras':
            expando = {
                'videoEnabled': False,
                'videoId': record['id'],
                'snapshotUrl': f'/camera-snapshot/MT/{record["id"]}',
            }
        elif layer == 'MessageSigns':
            expando = {
                'message': record.get('message') or '',
                'timestamp': record.get('timestamp'),
            }
        else:
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': record.get('description'),
                'severity': record.get('severity'),
                'timestamp': record.get('timestamp'),
                'location': record.get('location'),
            }
        normalized.append({
            'itemId': record['id'],
            'location': [record['lat'], record['lon']],
            'title': record['name'],
            'expando': expando,
        })
    return {'item2': normalized}


def mt511_record(layer, item_id):
    target = str(item_id)
    for record in mt511_records(layer):
        if record['id'] == target:
            return record
    raise ValueError(f'Montana 511 {layer} item {item_id} not found')


def mt511_camera_detail(site_id):
    record = mt511_record('Cameras', site_id)
    image = next(item for item in record['images'] if item.get('snapshot_url'))
    return {
        'name': image.get('name') or record['name'],
        'msg': record['name'],
        'severity': None,
        'timestamp': image.get('timestamp'),
        'video_id': str(site_id),
        'video_url': None,
        'video_enabled': False,
        'snapshot_url': f'/camera-snapshot/MT/{site_id}',
        'upstream_snapshot_url': image['snapshot_url'],
    }


def mt511_tooltip(layer, item_id):
    if layer == 'Cameras':
        return mt511_camera_detail(item_id)
    record = mt511_record(layer, item_id)
    return {
        'name': record['name'],
        'msg': record.get('message') or record.get('description') or '',
        'severity': record.get('severity'),
        'timestamp': record.get('timestamp'),
        'location': record.get('location'),
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def fetch_ndroads_dataset(resource):
    now = time.time()
    with NDROADS_TRAFFIC_CACHE_LOCK:
        cached = NDROADS_TRAFFIC_CACHE.get(resource)
        if cached and now < cached['expires_at']:
            return cached['features']
    if resource == 'Cameras':
        url = NDROADS_CAMERAS_URL
    elif resource == 'Incidents':
        url = NDROADS_ALERTS_URL
    elif resource in {'Construction21', 'Construction22'}:
        layer_id = resource.removeprefix('Construction')
        params = urllib.parse.urlencode({
            'where': '1=1',
            'outFields': '*',
            'returnGeometry': 'true',
            'outSR': 4326,
            'f': 'geojson',
        })
        url = f'{NDROADS_WORK_ZONES_ROOT}/{layer_id}/query?{params}'
    else:
        return []
    data = fetch_json_url(url, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': NDROADS_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
    }, timeout=30)
    features = data.get('features') if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise ValueError(f'North Dakota Roads {resource} returned no feature array')
    with NDROADS_TRAFFIC_CACHE_LOCK:
        NDROADS_TRAFFIC_CACHE[resource] = {
            'features': features,
            'expires_at': time.time() + NDROADS_CACHE_TTL,
        }
    return features


def ndroads_records(layer):
    if layer == 'MessageSigns':
        return []
    resources = ('Construction21', 'Construction22') if layer == 'Construction' else (layer,)
    records = []
    seen = set()
    for resource in resources:
        for feature in fetch_ndroads_dataset(resource):
            props = feature.get('properties') or {}
            geometry = feature.get('geometry') or {}
            center = geojson_center(geometry)
            if not center:
                continue
            lat, lon = center
            if not point_in_state('ND', lat, lon):
                continue
            if layer == 'Cameras':
                raw_id = display_text(props.get('ObjectID') or feature.get('id'))
                item_id = mt511_item_id('NDCameras', raw_id)
                images = []
                for image in props.get('Cameras') or []:
                    snapshot_url = str(image.get('FullPath') or image.get('LinkPath') or '').strip()
                    if not snapshot_url.startswith('https://www.dot.nd.gov/travel-info/cameras/'):
                        snapshot_url = ''
                    images.append({
                        'name': display_text(image.get('Description')),
                        'snapshot_url': snapshot_url,
                    })
                if not any(item.get('snapshot_url') for item in images):
                    continue
                highway = next(iter(props.get('Highways') or []), {})
                name = display_text((images[0] or {}).get('name')) or display_text(highway.get('HwyDesc')) or f'North Dakota camera {raw_id}'
                extra = {'images': images}
            elif layer == 'Incidents':
                raw_id = display_text(props.get('SegmentID') or props.get('ConditonExtentID') or feature.get('id'))
                item_id = mt511_item_id('NDIncidents', raw_id)
                name = display_text(props.get('MapIconDesc') or props.get('ConditionDesc')) or 'North Dakota traffic alert'
                extra = {
                    'description': strip_tags(props.get('Comment') or props.get('ConditionDesc')),
                    'severity': display_text(props.get('MapIconDesc')) or None,
                    'timestamp': epoch_milliseconds_iso(props.get('ModifyTime')),
                    'location': display_text(props.get('ExtentDesc') or props.get('HwyDesc')) or None,
                }
            else:
                raw_id = display_text(props.get('WorkZoneID') or feature.get('id'))
                item_id = mt511_item_id('NDConstruction', raw_id)
                name = display_text(props.get('WorkType')) or 'North Dakota road construction'
                description = ' · '.join(dict.fromkeys(
                    part for part in (
                        strip_tags(props.get('Comments')),
                        display_text(props.get('LaneReduction')),
                        display_text(props.get('DelayDesc')),
                    ) if part
                ))
                extra = {
                    'description': description or name,
                    'severity': display_text(props.get('LaneReduction') or props.get('DelayDesc')) or 'Construction',
                    'timestamp': epoch_milliseconds_iso(props.get('LastUpdated')),
                    'location': display_text(props.get('ProjectLocation') or props.get('HwyDesc')) or None,
                }
            if not raw_id or item_id in seen:
                continue
            seen.add(item_id)
            records.append({
                'id': item_id,
                'raw_id': raw_id,
                'lat': lat,
                'lon': lon,
                'name': name,
                **extra,
            })
    return records


def ndroads_layer_payload(layer):
    normalized = []
    for record in ndroads_records(layer):
        if layer == 'Cameras':
            expando = {
                'videoEnabled': False,
                'videoId': record['id'],
                'snapshotUrl': f'/camera-snapshot/ND/{record["id"]}',
            }
        elif layer == 'MessageSigns':
            expando = {'message': '', 'timestamp': None}
        else:
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': record.get('description'),
                'severity': record.get('severity'),
                'timestamp': record.get('timestamp'),
                'location': record.get('location'),
            }
        normalized.append({
            'itemId': record['id'],
            'location': [record['lat'], record['lon']],
            'title': record['name'],
            'expando': expando,
        })
    return {'item2': normalized}


def ndroads_record(layer, item_id):
    target = str(item_id)
    for record in ndroads_records(layer):
        if record['id'] == target:
            return record
    raise ValueError(f'North Dakota Roads {layer} item {item_id} not found')


def ndroads_camera_detail(site_id):
    record = ndroads_record('Cameras', site_id)
    image = next(item for item in record['images'] if item.get('snapshot_url'))
    return {
        'name': image.get('name') or record['name'],
        'msg': record['name'],
        'severity': None,
        'timestamp': None,
        'video_id': str(site_id),
        'video_url': None,
        'video_enabled': False,
        'snapshot_url': f'/camera-snapshot/ND/{site_id}',
        'upstream_snapshot_url': image['snapshot_url'],
    }


def ndroads_tooltip(layer, item_id):
    if layer == 'Cameras':
        return ndroads_camera_detail(item_id)
    record = ndroads_record(layer, item_id)
    return {
        'name': record['name'],
        'msg': record.get('description') or '',
        'severity': record.get('severity'),
        'timestamp': record.get('timestamp'),
        'location': record.get('location'),
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def fetch_sd511_dataset(resource):
    now = time.time()
    with SD511_TRAFFIC_CACHE_LOCK:
        cached = SD511_TRAFFIC_CACHE.get(resource)
        if cached and now < cached['expires_at']:
            return cached['features']
    filenames = {
        'Cameras': 'icons.cameras.geojson',
        'RWISCameras': 'icons.rwis.geojson',
        'Incidents': 'icons.incidents-accidents.geojson',
        'Restrictions': 'icons.restriction.geojson',
        'Disturbances': 'icons.disturbances.geojson',
        'Disasters': 'icons.disasters.geojson',
        'Obstructions': 'icons.obstructions.geojson',
        'ScheduledEvents': 'icons.scheduled-events.geojson',
        'Construction': 'icons.road-work.geojson',
    }
    filename = filenames.get(resource)
    if not filename:
        return []
    url = urllib.parse.urljoin(SD511_DATA_ROOT, filename)
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': SD511_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    text = raw.decode('utf-8', 'replace')
    try:
        data = json.loads(text)
        features = data.get('features') if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # The live SD511 camera/RWIS objects are occasionally published while
        # their final bytes are still being written. Retain every complete
        # GeoJSON feature rather than dropping the entire official feed.
        marker = re.search(r'"features"\s*:\s*\[', text)
        features = []
        cursor = marker.end() if marker else len(text)
        decoder = json.JSONDecoder()
        while cursor < len(text):
            while cursor < len(text) and text[cursor] in ' \t\r\n,':
                cursor += 1
            try:
                feature, cursor = decoder.raw_decode(text, cursor)
            except json.JSONDecodeError:
                break
            if isinstance(feature, dict):
                features.append(feature)
    if not isinstance(features, list) or (resource in {'Cameras', 'RWISCameras'} and not features):
        raise ValueError(f'South Dakota 511 {resource} returned no complete features')
    with SD511_TRAFFIC_CACHE_LOCK:
        SD511_TRAFFIC_CACHE[resource] = {
            'features': features,
            'expires_at': time.time() + SD511_CACHE_TTL,
        }
    return features


def sd511_records(layer):
    if layer == 'MessageSigns':
        return []
    if layer == 'Cameras':
        resources = ('Cameras', 'RWISCameras')
    elif layer == 'Incidents':
        resources = ('Incidents', 'Restrictions', 'Disturbances', 'Disasters', 'Obstructions', 'ScheduledEvents')
    else:
        resources = ('Construction',)
    records = []
    seen = set()
    for resource in resources:
        for feature in fetch_sd511_dataset(resource):
            props = feature.get('properties') or {}
            coords = (feature.get('geometry') or {}).get('coordinates') or []
            lon = optional_float(coords[0]) if len(coords) >= 2 else None
            lat = optional_float(coords[1]) if len(coords) >= 2 else None
            raw_id = display_text(props.get('event_id') or props.get('id') or feature.get('id'))
            if not raw_id or lat is None or lon is None or not point_in_state('SD', lat, lon):
                continue
            item_id = mt511_item_id(f'SD511:{layer}', raw_id)
            if item_id in seen:
                continue
            seen.add(item_id)
            if layer == 'Cameras':
                images = []
                for image in props.get('cameras') or []:
                    snapshot_url = str(image.get('image') or '').strip()
                    if not snapshot_url.startswith('https://sd.cdn.iteris-atis.com/camera_images/'):
                        snapshot_url = ''
                    images.append({
                        'name': display_text(image.get('name') or image.get('description')),
                        'snapshot_url': snapshot_url,
                        'timestamp': epoch_milliseconds_iso(image.get('updateTime')),
                    })
                if not any(image.get('snapshot_url') for image in images):
                    continue
                name = display_text(props.get('name') or props.get('description')) or f'South Dakota camera {raw_id}'
                extra = {'images': images}
            else:
                default_name = 'South Dakota road construction' if layer == 'Construction' else 'South Dakota traffic advisory'
                name = display_text(props.get('headline') or props.get('label')) or default_name
                extra = {
                    'description': strip_tags(props.get('report') or props.get('enhanced_report')),
                    'severity': display_text(props.get('headline') or props.get('category')).title() or None,
                    'timestamp': epoch_milliseconds_iso(props.get('start_time') or props.get('last_update')),
                    'location': strip_tags(props.get('location_description') or props.get('label')) or None,
                }
            records.append({
                'id': item_id,
                'raw_id': raw_id,
                'lat': lat,
                'lon': lon,
                'name': name,
                **extra,
            })
    return records


def sd511_layer_payload(layer):
    normalized = []
    for record in sd511_records(layer):
        if layer == 'Cameras':
            expando = {
                'videoEnabled': False,
                'videoId': record['id'],
                'snapshotUrl': f'/camera-snapshot/SD/{record["id"]}',
            }
        elif layer == 'MessageSigns':
            expando = {'message': '', 'timestamp': None}
        else:
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': record.get('description'),
                'severity': record.get('severity'),
                'timestamp': record.get('timestamp'),
                'location': record.get('location'),
            }
        normalized.append({
            'itemId': record['id'],
            'location': [record['lat'], record['lon']],
            'title': record['name'],
            'expando': expando,
        })
    return {'item2': normalized}


def sd511_record(layer, item_id):
    target = str(item_id)
    for record in sd511_records(layer):
        if record['id'] == target:
            return record
    raise ValueError(f'South Dakota 511 {layer} item {item_id} not found')


def sd511_camera_detail(site_id):
    record = sd511_record('Cameras', site_id)
    image = next(item for item in record['images'] if item.get('snapshot_url'))
    return {
        'name': image.get('name') or record['name'],
        'msg': record['name'],
        'severity': None,
        'timestamp': image.get('timestamp'),
        'video_id': str(site_id),
        'video_url': None,
        'video_enabled': False,
        'snapshot_url': f'/camera-snapshot/SD/{site_id}',
        'upstream_snapshot_url': image['snapshot_url'],
    }


def sd511_tooltip(layer, item_id):
    if layer == 'Cameras':
        return sd511_camera_detail(item_id)
    record = sd511_record(layer, item_id)
    return {
        'name': record['name'],
        'msg': record.get('description') or '',
        'severity': record.get('severity'),
        'timestamp': record.get('timestamp'),
        'location': record.get('location'),
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def cars511_cached_items(state_code, cache_key, loader):
    key = f'{state_code}:{cache_key}'
    now = time.time()
    with CARS511_CACHE_LOCK:
        cached = CARS511_CACHE.get(key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with CARS511_CACHE_LOCK:
        CARS511_CACHE[key] = {
            'items': items,
            'expires_at': time.time() + CARS511_CACHE_TTL,
        }
    return items


def cars511_headers(state_code, *, graphql=False):
    config = CARS511_CONFIGS[state_code]
    origin = config['source_url'].rstrip('/')
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Origin': origin,
        'Referer': config['source_url'],
        'Accept': 'application/json',
    }
    if graphql:
        headers['Content-Type'] = 'application/json'
    return headers


def fetch_cars511_dataset(state_code, resource):
    paths = {
        'Cameras': '/cameras_v1/api/cameras',
        'MessageSigns': '/signs_v1/api/signs',
    }
    path = paths.get(resource)
    if not path:
        return []

    def load():
        data = fetch_json_url(
            CARS511_CONFIGS[state_code]['api_root'] + path,
            headers=cars511_headers(state_code),
            timeout=35,
        )
        if not isinstance(data, list):
            raise ValueError(f'{state_code} 511 {resource} returned an invalid payload')
        return data

    return cars511_cached_items(state_code, resource, load)


def cars511_snapshot_url(state_code, camera):
    allowed_hosts = CARS511_CONFIGS[state_code]['snapshot_hosts']
    for view in camera.get('views') or []:
        candidate = str(view.get('videoPreviewUrl') or '').strip()
        if not candidate and view.get('type') == 'STILL_IMAGE':
            candidate = str(view.get('url') or '').strip()
        parsed = urllib.parse.urlparse(candidate)
        if (
            parsed.scheme == 'https' and parsed.hostname in allowed_hosts and
            parsed.path.lower().endswith(('.jpg', '.jpeg', '.png'))
        ):
            return candidate
    return None


def fetch_cars511_cameras(state_code):
    cameras = []
    for item in fetch_cars511_dataset(state_code, 'Cameras'):
        location = item.get('location') or {}
        raw_id = str(item.get('id') or '').strip()
        lat = optional_float(location.get('latitude'))
        lon = optional_float(location.get('longitude'))
        if (
            not raw_id.isdigit() or lat is None or lon is None or
            item.get('public') is False or not point_in_state(state_code, lat, lon)
        ):
            continue
        snapshot_url = cars511_snapshot_url(state_code, item)
        if not snapshot_url:
            continue
        route = display_text(location.get('routeId'))
        city = display_text(location.get('cityReference'))
        owner = display_text((item.get('cameraOwner') or {}).get('name'))
        cameras.append({
            'id': raw_id,
            'name': display_text(item.get('name')) or f'{state_code} DOT camera {raw_id}',
            'lat': lat,
            'lon': lon,
            'route': route,
            'city': city,
            'owner': owner,
            'snapshot_url': snapshot_url,
            'updated_at': epoch_milliseconds_iso(item.get('lastUpdated')),
            'updated_epoch': safe_int(item.get('lastUpdated')),
        })
    if not cameras:
        raise ValueError(f'{state_code} 511 returned no public cameras with snapshots')
    cameras.sort(key=lambda camera: camera['updated_epoch'], reverse=True)
    return cameras


def cars511_sign_message(item):
    messages = []
    for page in ((item.get('display') or {}).get('pages') or []):
        lines = [display_text(line) for line in page.get('lines') or [] if display_text(line)]
        if lines:
            messages.append(' / '.join(lines))
    return ' · '.join(dict.fromkeys(messages)) or 'No active message'


def fetch_cars511_signs(state_code):
    signs = []
    for item in fetch_cars511_dataset(state_code, 'MessageSigns'):
        location = item.get('location') or {}
        raw_id = display_text(item.get('id') or item.get('idForDisplay'))
        lat = optional_float(location.get('latitude'))
        lon = optional_float(location.get('longitude'))
        if not raw_id or lat is None or lon is None or not point_in_state(state_code, lat, lon):
            continue
        signs.append({
            'id': str(wv511_numeric_id(f'CARS511:{state_code}:SIGN:{raw_id}')),
            'name': display_text(item.get('name') or location.get('locationDescription')) or f'{state_code} DOT message sign',
            'lat': lat,
            'lon': lon,
            'message': cars511_sign_message(item),
            'status': display_text(item.get('status')),
            'agency': display_text(item.get('agencyName')),
            'updated_at': epoch_milliseconds_iso(item.get('lastUpdated')),
        })
    return signs


def cars511_event_coordinates(state_code, item):
    for feature in item.get('features') or []:
        geometry = feature.get('geometry') or {}
        coords = geometry.get('coordinates') or []
        if geometry.get('type') == 'Point' and len(coords) >= 2:
            lon = optional_float(coords[0])
            lat = optional_float(coords[1])
            if lat is not None and lon is not None and point_in_state(state_code, lat, lon):
                return lat, lon
    bbox = item.get('bbox') or []
    if len(bbox) >= 4:
        lon = (safe_float(bbox[0]) + safe_float(bbox[2])) / 2
        lat = (safe_float(bbox[1]) + safe_float(bbox[3])) / 2
        if point_in_state(state_code, lat, lon):
            return lat, lon
    return None


def fetch_cars511_all_events(state_code):
    def load():
        bounds = REGIONS[state_code]['bounds']
        payload = fetch_json_url(
            CARS511_CONFIGS[state_code]['graphql_url'],
            headers=cars511_headers(state_code, graphql=True),
            data={
                'query': CARS511_EVENT_QUERY,
                'variables': {
                    'north': bounds['max_lat'],
                    'south': bounds['min_lat'],
                    'east': bounds['max_lon'],
                    'west': bounds['min_lon'],
                    'slugs': ['constructionReports'],
                },
            },
            timeout=40,
        )
        search = ((payload.get('data') or {}).get('searchBoundsQuery') or {})
        error = search.get('error') or {}
        if error:
            raise ValueError(f'{state_code} 511 GraphQL error: {error.get("message") or error.get("type")}')
        records = []
        for item in search.get('results') or []:
            if item.get('__typename') != 'Event':
                continue
            uri = str(item.get('uri') or '').strip()
            coordinates = cars511_event_coordinates(state_code, item)
            if not uri or not coordinates:
                continue
            title = display_text(item.get('title')) or f'{state_code} traffic report'
            description = strip_tags(item.get('description')) or title
            searchable = f'{title} {description}'.casefold()
            construction = any(term in searchable for term in (
                'construction', 'road work', 'roadwork', 'maintenance', 'resurfac',
                'bridge work', 'pavement', 'milling', 'grading', 'chip seal',
                'work zone', 'shoulder work',
            ))
            records.append({
                'id': str(wv511_numeric_id(f'CARS511:{state_code}:EVENT:{uri}')),
                'name': title,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'description': description,
                'severity': f'Priority {safe_int(item.get("priority"))}' if item.get('priority') is not None else None,
                'location': display_text(item.get('cityReference')) or None,
                'timestamp': None,
                'construction': construction,
            })
        return records

    return cars511_cached_items(state_code, 'Events', load)


def fetch_cars511_events(state_code, layer):
    construction = layer == 'Construction'
    return [item for item in fetch_cars511_all_events(state_code) if item['construction'] == construction]


def cars511_layer_payload(state_code, layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': False,
                'videoId': camera['id'],
                'snapshotUrl': f'/camera-snapshot/{state_code}/{camera["id"]}',
            },
        } for camera in fetch_cars511_cameras(state_code)]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': sign['updated_at']},
        } for sign in fetch_cars511_signs(state_code)]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
        },
    } for item in fetch_cars511_events(state_code, layer)]}


def cars511_camera_detail(state_code, site_id):
    target = str(site_id)
    for camera in fetch_cars511_cameras(state_code):
        if camera['id'] != target:
            continue
        details = ' · '.join(part for part in (camera['route'], camera['city'], camera['owner']) if part)
        return {
            'name': camera['name'],
            'msg': details or f'{state_code} DOT traffic camera',
            'severity': None,
            'timestamp': camera['updated_at'],
            'video_id': target,
            'video_url': None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/{state_code}/{target}',
            'upstream_snapshot_url': camera['snapshot_url'],
        }
    raise ValueError(f'{state_code} 511 camera {site_id} not found')


def cars511_tooltip(state_code, layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return cars511_camera_detail(state_code, target)
    records = fetch_cars511_signs(state_code) if layer == 'MessageSigns' else fetch_cars511_events(state_code, layer)
    for item in records:
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': ' · '.join(part for part in (item['agency'], item['status']) if part),
                'timestamp': item['updated_at'],
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'{state_code} 511 {layer} item {item_id} not found')


def drivenc_layer_payload(layer):
    upstream_layer = 'Roadwork' if layer == 'Construction' else layer
    content = fetch_iteris_layer(REGIONS['NC'], upstream_layer)
    data = json.loads(content.decode('utf-8-sig'))
    if layer == 'Cameras':
        for item in data.get('item2') or []:
            item_id = str(item.get('itemId') or '').strip()
            if not valid_numeric_id(item_id):
                continue
            item['expando'] = {
                'videoEnabled': False,
                'videoId': item_id,
                'snapshotUrl': f'/camera-snapshot/NC/{item_id}',
            }
    return data


def drivenc_tooltip(layer, item_id):
    upstream_layer = 'Roadwork' if layer == 'Construction' else layer
    origin = REGIONS['NC']['traffic_origin']
    url = f'{origin}/tooltip/{upstream_layer}/{item_id}?lang=en'
    req = urllib.request.Request(
        url,
        headers=traffic_headers(REGIONS['NC'], accept='text/html,application/xhtml+xml'),
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    info = _parse_511_tooltip_html(raw)
    if layer == 'Cameras':
        info['snapshot_url'] = f'/camera-snapshot/NC/{item_id}'
    return info


def drivenc_camera_detail(site_id):
    info = drivenc_tooltip('Cameras', site_id)
    video_url = str(info.get('video_url') or '').strip()
    parsed_video = urllib.parse.urlparse(video_url)
    if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
        video_url = ''
    return {
        'name': info.get('name') or f'NCDOT camera {site_id}',
        'msg': info.get('msg'),
        'severity': None,
        'timestamp': info.get('timestamp'),
        'video_id': info.get('video_id') or str(site_id),
        'video_url': video_url or None,
        'video_enabled': bool(video_url),
        'snapshot_url': f'/camera-snapshot/NC/{site_id}',
        'upstream_snapshot_url': f'https://drivenc.gov/map/Cctv/{site_id}',
    }


def fetch_tdot_items(resource):
    now = time.time()
    with TDOT_TRAFFIC_CACHE_LOCK:
        cached = TDOT_TRAFFIC_CACHE.get(resource)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        if not TDOT_API_KEY:
            raise RuntimeError('AMERICAMAP_TDOT_API_KEY is required for TDOT feeds')
        data = fetch_json_url(
            urllib.parse.urljoin(TDOT_API_BASE, resource),
            headers={
                'ApiKey': TDOT_API_KEY,
                'Accept': 'application/json',
                'Referer': 'https://smartway.tn.gov/',
                'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            },
            timeout=60,
        )
        if not isinstance(data, list):
            raise ValueError(f'Unexpected TDOT {resource} response')
    except Exception:
        if cached:
            return cached['items']
        raise
    with TDOT_TRAFFIC_CACHE_LOCK:
        TDOT_TRAFFIC_CACHE[resource] = {
            'items': data,
            'expires_at': time.time() + TDOT_CACHE_TTL,
        }
    return data


def tdot_item(resource, item_id):
    target_id = str(item_id)
    for item in fetch_tdot_items(resource):
        if str(item.get('id')) == target_id:
            return item
    raise ValueError(f'TDOT {resource} item {item_id} not found')


def tdot_event_location(item):
    location = ((item.get('locations') or [{}])[0] or {})
    point = location.get('midPoint') or ((location.get('coordinates') or [{}])[0] or {})
    lat = safe_float(point.get('lat'))
    lon = safe_float(point.get('lng'))
    return lat, lon, location


def tdot_layer_payload(layer):
    resource = TDOT_LAYER_RESOURCES.get(layer)
    if not resource:
        return {'item2': []}
    tn_bounds = REGIONS['TN']['bounds']
    normalized = []
    for item in fetch_tdot_items(resource):
        item_id = str(item.get('id') or '').strip()
        if not valid_numeric_id(item_id):
            continue
        if layer == 'Cameras':
            lat = safe_float(item.get('lat'))
            lon = safe_float(item.get('lng'))
            title = item.get('title') or item.get('description') or 'TDOT camera'
            expando = {
                'videoEnabled': bool(item.get('httpsVideoUrl')),
                'videoId': item_id,
                'snapshotUrl': f'/camera-snapshot/TN/{item_id}',
            }
        elif layer == 'MessageSigns':
            point = (((item.get('location') or {}).get('coordinates') or [{}])[0] or {})
            lat = safe_float(point.get('lat'))
            lon = safe_float(point.get('lng'))
            title = item.get('title') or item.get('route') or 'TDOT message sign'
            expando = {
                'message': item.get('message') or '',
                'timestamp': None,
            }
        else:
            lat, lon, location = tdot_event_location(item)
            title = item.get('eventSubTypeDescription') or item.get('eventTypeName') or (
                'Severe Traffic Impact' if layer == 'SevereImpact' else 'TDOT event'
            )
            county = str(location.get('countyName') or '').strip()
            expando = {
                'feedLabel': 'Severe Traffic Impact' if layer == 'SevereImpact' else (
                    'Construction' if layer == 'Construction' else 'Traffic Incident'
                ),
                'description': item.get('description') or item.get('impactDescription'),
                'severity': 'Severe' if item.get('isSevere') or layer == 'SevereImpact' else None,
                'timestamp': item.get('revisedDate') or item.get('beginningDate'),
                'location': f'{county} County' if county else None,
            }
        if not (
            tn_bounds['min_lat'] <= lat <= tn_bounds['max_lat'] and
            tn_bounds['min_lon'] <= lon <= tn_bounds['max_lon']
        ):
            continue
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def tdot_camera_detail(site_id):
    item = tdot_item('RoadwayCameras', site_id)
    video_url = str(item.get('httpsVideoUrl') or '').strip()
    snapshot_url = str(item.get('thumbnailUrl') or '').strip()
    parsed_video = urllib.parse.urlparse(video_url)
    parsed_snapshot = urllib.parse.urlparse(snapshot_url)
    if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
        video_url = ''
    if parsed_snapshot.scheme != 'https' or not allowed_stream_host(parsed_snapshot.hostname):
        snapshot_url = ''
    return {
        'name': item.get('title') or item.get('description') or f'TDOT camera {site_id}',
        'msg': item.get('description'),
        'severity': None,
        'timestamp': None,
        'video_id': str(site_id),
        'video_url': video_url or None,
        'video_enabled': bool(video_url),
        'snapshot_url': f'/camera-snapshot/TN/{site_id}' if snapshot_url else None,
        'upstream_snapshot_url': snapshot_url or None,
    }


def tdot_tooltip(layer, item_id):
    if layer == 'Cameras':
        return tdot_camera_detail(item_id)
    resource = TDOT_LAYER_RESOURCES.get(layer)
    if not resource:
        raise ValueError('Unknown TDOT layer')
    item = tdot_item(resource, item_id)
    if layer == 'MessageSigns':
        return {
            'name': item.get('title') or item.get('route') or 'TDOT message sign',
            'msg': item.get('message') or 'No message displayed',
            'severity': None,
            'timestamp': None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    _, _, location = tdot_event_location(item)
    county = str(location.get('countyName') or '').strip()
    return {
        'name': item.get('eventSubTypeDescription') or item.get('eventTypeName') or 'TDOT event',
        'msg': item.get('description') or item.get('impactDescription'),
        'severity': 'Severe' if item.get('isSevere') or layer == 'SevereImpact' else None,
        'timestamp': item.get('revisedDate') or item.get('beginningDate'),
        'location': f'{county} County' if county else None,
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def firestore_value(value):
    value = value or {}
    if 'nullValue' in value:
        return None
    for key in ('stringValue', 'timestampValue', 'referenceValue'):
        if key in value:
            return value[key]
    if 'integerValue' in value:
        return safe_int(value['integerValue'])
    if 'doubleValue' in value:
        return safe_float(value['doubleValue'])
    if 'booleanValue' in value:
        return bool(value['booleanValue'])
    if 'geoPointValue' in value:
        return {
            'latitude': safe_float(value['geoPointValue'].get('latitude')),
            'longitude': safe_float(value['geoPointValue'].get('longitude')),
        }
    if 'mapValue' in value:
        return {
            key: firestore_value(item)
            for key, item in ((value['mapValue'].get('fields') or {}).items())
        }
    if 'arrayValue' in value:
        return [firestore_value(item) for item in (value['arrayValue'].get('values') or [])]
    return None


def goky_document(document):
    fields = {
        key: firestore_value(value)
        for key, value in (document.get('fields') or {}).items()
    }
    fields['_document_name'] = str(document.get('name') or '').rsplit('/', 1)[-1]
    fields['_updated_at'] = document.get('updateTime')
    return fields


def fetch_goky_realtime():
    cache_key = 'realtime'
    now = time.time()
    with GOKY_TRAFFIC_CACHE_LOCK:
        cached = GOKY_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        if not GOKY_API_KEY:
            raise RuntimeError('AMERICAMAP_GOKY_API_KEY is required for GoKY feeds')
        documents = []
        page_token = None
        for _ in range(10):
            params = {'pageSize': 300, 'key': GOKY_API_KEY}
            if page_token:
                params['pageToken'] = page_token
            data = fetch_json_url(
                f'{GOKY_FIRESTORE_URL}?{urllib.parse.urlencode(params)}',
                headers={
                    'Referer': 'https://goky.ky.gov/',
                    'Accept': 'application/json',
                    'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
                },
                timeout=30,
            )
            documents.extend(goky_document(document) for document in (data.get('documents') or []))
            page_token = data.get('nextPageToken')
            if not page_token:
                break
    except Exception:
        if cached:
            return cached['items']
        raise
    with GOKY_TRAFFIC_CACHE_LOCK:
        GOKY_TRAFFIC_CACHE[cache_key] = {
            'items': documents,
            'expires_at': time.time() + GOKY_CACHE_TTL,
        }
    return documents


def fetch_goky_cameras():
    cache_key = 'cameras'
    now = time.time()
    with GOKY_TRAFFIC_CACHE_LOCK:
        cached = GOKY_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        kytc_params = urllib.parse.urlencode({
            'where': '1=1',
            'outFields': 'OBJECTID,name,description,snapshot,status,latitude,longitude,county,updateTS',
            'returnGeometry': 'false',
            'f': 'json',
        })
        fayette_params = urllib.parse.urlencode({
            'where': '1=1',
            'outFields': 'objectid,location,still_url,last_edited_date',
            'returnGeometry': 'true',
            'outSR': 4326,
            'f': 'json',
        })
        kytc = fetch_json_url(f'{GOKY_KYTC_CAMERAS_URL}?{kytc_params}', timeout=30)
        fayette = fetch_json_url(f'{GOKY_FAYETTE_CAMERAS_URL}?{fayette_params}', timeout=30)
        cameras = []
        for feature in kytc.get('features') or []:
            attrs = feature.get('attributes') or {}
            cameras.append({
                'id': safe_int(attrs.get('OBJECTID')),
                'name': attrs.get('description') or attrs.get('name') or 'KYTC camera',
                'county': attrs.get('county'),
                'lat': safe_float(attrs.get('latitude')),
                'lon': safe_float(attrs.get('longitude')),
                'snapshot_url': attrs.get('snapshot'),
                'updated_at': epoch_milliseconds_iso(attrs.get('updateTS')),
                'source': 'KYTC GoKY',
            })
        for feature in fayette.get('features') or []:
            attrs = feature.get('attributes') or {}
            geometry = feature.get('geometry') or {}
            object_id = safe_int(attrs.get('objectid'))
            cameras.append({
                'id': 9_000_000 + object_id,
                'name': attrs.get('location') or 'Lexington traffic camera',
                'county': 'Fayette',
                'lat': safe_float(geometry.get('y')),
                'lon': safe_float(geometry.get('x')),
                'snapshot_url': attrs.get('still_url'),
                'updated_at': epoch_milliseconds_iso(attrs.get('last_edited_date')),
                'source': 'Lexington-Fayette Traffic Management Center',
            })
    except Exception:
        if cached:
            return cached['items']
        raise
    with GOKY_TRAFFIC_CACHE_LOCK:
        GOKY_TRAFFIC_CACHE[cache_key] = {
            'items': cameras,
            'expires_at': time.time() + GOKY_CACHE_TTL,
        }
    return cameras


def goky_numeric_id(document):
    raw = str(document.get('_document_name') or '')
    return str(int.from_bytes(hashlib.sha256(raw.encode('utf-8')).digest()[:7], 'big'))


def goky_location(document):
    point = document.get('location') or {}
    lat = safe_float(point.get('latitude'))
    lon = safe_float(point.get('longitude'))
    if not (lat and lon):
        line = document.get('line') or []
        if line:
            point = line[len(line) // 2] or {}
            lat = safe_float(point.get('latitude'))
            lon = safe_float(point.get('longitude'))
    return lat, lon


def goky_layer_payload(layer):
    ky_bounds = REGIONS['KY']['bounds']
    normalized = []
    if layer == 'Cameras':
        for camera in fetch_goky_cameras():
            lat = camera['lat']
            lon = camera['lon']
            if not (
                ky_bounds['min_lat'] <= lat <= ky_bounds['max_lat'] and
                ky_bounds['min_lon'] <= lon <= ky_bounds['max_lon']
            ):
                continue
            normalized.append({
                'itemId': str(camera['id']),
                'location': [lat, lon],
                'title': camera['name'],
                'expando': {
                    'videoEnabled': False,
                    'videoId': str(camera['id']),
                    'snapshotUrl': f'/camera-snapshot/KY/{camera["id"]}',
                },
            })
        return {'item2': normalized}

    allowed_types = {
        'MessageSigns': {'dms'},
        'Incidents': {'crsh', 'hzrd', 'wzcrsh', 'wzhzrd', 'wztrfc'},
        'Construction': {'wkzn', 'wzwk'},
    }.get(layer, set())
    for document in fetch_goky_realtime():
        if document.get('type') not in allowed_types:
            continue
        lat, lon = goky_location(document)
        if not (
            ky_bounds['min_lat'] <= lat <= ky_bounds['max_lat'] and
            ky_bounds['min_lon'] <= lon <= ky_bounds['max_lon']
        ):
            continue
        source = document.get('source') or {}
        display = document.get('display') or {}
        if layer == 'MessageSigns':
            title = source.get('location') or display.get('Road_Name') or 'GoKY digital sign'
            expando = {
                'message': source.get('message') or '',
                'timestamp': source.get('published') or document.get('_updated_at'),
            }
        else:
            title = source.get('type') or display.get('Source_Type') or (
                'Construction' if layer == 'Construction' else 'Traffic incident'
            )
            road = display.get('Road_Name') or display.get('Route')
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': source.get('description') or display.get('Description'),
                'severity': None,
                'timestamp': source.get('published') or document.get('_updated_at'),
                'location': road or (f'{document.get("county")} County' if document.get('county') else None),
            }
        normalized.append({
            'itemId': goky_numeric_id(document),
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def goky_camera_detail(site_id):
    target_id = str(site_id)
    for camera in fetch_goky_cameras():
        if str(camera.get('id')) != target_id:
            continue
        snapshot_url = str(camera.get('snapshot_url') or '').strip()
        parsed_snapshot = urllib.parse.urlparse(snapshot_url)
        if parsed_snapshot.scheme not in {'http', 'https'} or not allowed_stream_host(parsed_snapshot.hostname):
            snapshot_url = ''
        return {
            'name': camera.get('name') or f'Kentucky camera {site_id}',
            'msg': f'{camera.get("county")} County' if camera.get('county') else None,
            'severity': None,
            'timestamp': camera.get('updated_at'),
            'video_id': str(site_id),
            'video_url': None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/KY/{site_id}' if snapshot_url else None,
            'upstream_snapshot_url': snapshot_url or None,
        }
    raise ValueError(f'Kentucky camera {site_id} not found')


def goky_tooltip(layer, item_id):
    if layer == 'Cameras':
        return goky_camera_detail(item_id)
    for document in fetch_goky_realtime():
        if goky_numeric_id(document) != str(item_id):
            continue
        source = document.get('source') or {}
        display = document.get('display') or {}
        if layer == 'MessageSigns':
            return {
                'name': source.get('location') or display.get('Road_Name') or 'GoKY digital sign',
                'msg': source.get('message') or 'No message displayed',
                'severity': None,
                'timestamp': source.get('published') or document.get('_updated_at'),
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': source.get('type') or display.get('Source_Type') or 'GoKY traffic event',
            'msg': source.get('description') or display.get('Description'),
            'severity': None,
            'timestamp': source.get('published') or document.get('_updated_at'),
            'location': display.get('Road_Name') or display.get('Route'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'GoKY {layer} item {item_id} not found')


def fetch_vdot_dataset(dataset_key):
    now = time.time()
    with VDOT_TRAFFIC_CACHE_LOCK:
        cached = VDOT_TRAFFIC_CACHE.get(dataset_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        data = fetch_json_url(VDOT_TRAFFIC_URLS[dataset_key], headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': 'https://511.vdot.virginia.gov/',
            'Accept': 'application/geo+json,application/json',
        }, timeout=30)
        items = data.get('data') if dataset_key == 'Cameras' else data.get('features')
        if not isinstance(items, list):
            raise ValueError(f'VDOT {dataset_key} returned no feature collection')
    except Exception:
        if cached:
            return cached['items']
        raise
    with VDOT_TRAFFIC_CACHE_LOCK:
        VDOT_TRAFFIC_CACHE[dataset_key] = {
            'items': items,
            'expires_at': time.time() + VDOT_CACHE_TTL,
        }
    return items


def vdot_numeric_id(feature):
    raw = str(feature.get('id') or (feature.get('properties') or {}).get('id') or '')
    return str(int.from_bytes(hashlib.sha256(raw.encode('utf-8')).digest()[:7], 'big'))


def vdot_point(feature):
    coordinates = (feature.get('geometry') or {}).get('coordinates') or []
    if len(coordinates) < 2:
        return 0.0, 0.0
    return safe_float(coordinates[1]), safe_float(coordinates[0])


def vdot_features_for_layer(layer):
    if layer == 'Incidents':
        return fetch_vdot_dataset('IncidentsMinor') + fetch_vdot_dataset('IncidentsMajor')
    return fetch_vdot_dataset(layer)


def vdot_layer_payload(layer):
    if layer not in {'Cameras', 'MessageSigns', 'Incidents', 'Construction'}:
        return {'item2': []}
    va_bounds = REGIONS['VA']['bounds']
    normalized = []
    for feature in vdot_features_for_layer(layer):
        props = feature.get('properties') or {}
        lat, lon = vdot_point(feature)
        if not (
            va_bounds['min_lat'] <= lat <= va_bounds['max_lat'] and
            va_bounds['min_lon'] <= lon <= va_bounds['max_lon']
        ):
            continue
        if layer == 'Cameras':
            item_id = str(props.get('id') or feature.get('id') or '')
            video_url = str(props.get('https_url') or props.get('ios_url') or '').strip()
            snapshot_url = str(props.get('image_url') or '').strip()
            expando = {
                'videoEnabled': bool(video_url and not props.get('problem_stream')),
                'videoId': item_id,
                'videoUrl': video_url,
                'snapshotUrl': f'/camera-snapshot/VA/{item_id}' if snapshot_url else None,
            }
            title = props.get('description') or props.get('route') or 'VDOT traffic camera'
        elif layer == 'MessageSigns':
            item_id = vdot_numeric_id(feature)
            title = props.get('route') or props.get('DMS_name') or 'VDOT message sign'
            expando = {
                'message': props.get('text') or '',
                'timestamp': None,
            }
        else:
            item_id = vdot_numeric_id(feature)
            title = props.get('type') or ('Construction' if layer == 'Construction' else 'Traffic incident')
            location = props.get('location_description') or props.get('location') or props.get('route')
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': props.get('message_511') or location,
                'severity': props.get('priority'),
                'timestamp': epoch_milliseconds_iso(props.get('update') or props.get('start')),
                'location': location,
            }
        if not valid_numeric_id(item_id):
            continue
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def vdot_camera_detail(site_id):
    target_id = str(site_id)
    for feature in fetch_vdot_dataset('Cameras'):
        props = feature.get('properties') or {}
        if str(props.get('id') or feature.get('id') or '') != target_id:
            continue
        video_url = str(props.get('https_url') or props.get('ios_url') or '').strip()
        snapshot_url = str(props.get('image_url') or '').strip()
        parsed_video = urllib.parse.urlparse(video_url)
        parsed_snapshot = urllib.parse.urlparse(snapshot_url)
        if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
            video_url = ''
        if parsed_snapshot.scheme != 'https' or not allowed_stream_host(parsed_snapshot.hostname):
            snapshot_url = ''
        jurisdiction = display_text(props.get('jurisdiction'))
        route = display_text(props.get('route'))
        return {
            'name': props.get('description') or route or f'Virginia camera {site_id}',
            'msg': ' · '.join(part for part in (route, jurisdiction) if part) or None,
            'severity': None,
            'timestamp': None,
            'video_id': target_id,
            'video_url': video_url if not props.get('problem_stream') else None,
            'video_enabled': bool(video_url and not props.get('problem_stream')),
            'snapshot_url': f'/camera-snapshot/VA/{target_id}' if snapshot_url else None,
            'upstream_snapshot_url': snapshot_url or None,
        }
    raise ValueError(f'Virginia camera {site_id} not found')


def vdot_tooltip(layer, item_id):
    if layer == 'Cameras':
        return vdot_camera_detail(item_id)
    for feature in vdot_features_for_layer(layer):
        if vdot_numeric_id(feature) != str(item_id):
            continue
        props = feature.get('properties') or {}
        if layer == 'MessageSigns':
            return {
                'name': props.get('route') or props.get('DMS_name') or 'VDOT message sign',
                'msg': props.get('text') or 'No message displayed',
                'severity': None,
                'timestamp': None,
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': props.get('type') or ('Construction' if layer == 'Construction' else 'Traffic incident'),
            'msg': props.get('message_511') or props.get('location_description') or props.get('location'),
            'severity': props.get('priority'),
            'timestamp': epoch_milliseconds_iso(props.get('update') or props.get('start')),
            'location': props.get('location_description') or props.get('location') or props.get('route'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'VDOT {layer} item {item_id} not found')


def wv511_cached_text(cache_key, url):
    now = time.time()
    with WV511_TRAFFIC_CACHE_LOCK:
        cached = WV511_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['text']
    try:
        text = fetch_text_url(url, referer='https://www.wv511.org/', timeout=30)
    except Exception:
        if cached:
            return cached['text']
        raise
    with WV511_TRAFFIC_CACHE_LOCK:
        WV511_TRAFFIC_CACHE[cache_key] = {
            'text': text,
            'expires_at': time.time() + WV511_CACHE_TTL,
        }
    return text


def fetch_wv511_cameras():
    text = wv511_cached_text('Cameras', WV511_CAMERA_URL)
    match = re.search(r'var\s+camera_data\s*=\s*', text)
    if not match:
        raise ValueError('WV511 camera data was not found')
    data, _ = json.JSONDecoder().raw_decode(text[match.end():].lstrip())
    cameras = []
    for item in data.get('cams') or []:
        camera_code = str(item.get('md5') or '').strip().upper()
        digits = re.sub(r'\D', '', camera_code)
        if not digits:
            continue
        cameras.append({
            'id': str(int(digits)),
            'camera_code': camera_code,
            'name': strip_tags(item.get('description')) or item.get('title') or f'WV511 {camera_code}',
            'route': item.get('title'),
            'lat': safe_float(item.get('start_lat')),
            'lon': safe_float(item.get('start_lng')),
            # WV511 distributes cameras across several roadsummary hosts. The
            # authoritative player page resolves the correct host on demand.
            'video_url': None,
        })
    return cameras


def wv511_camera_stream_url(camera_code):
    code = str(camera_code or '').strip().upper()
    if not re.fullmatch(r'CAM\d+', code):
        return None
    player_url = WV511_CAMERA_PLAYER_URL.format(camera_code=urllib.parse.quote(code))
    text = wv511_cached_text(f'CameraPlayer:{code}', player_url)
    candidates = re.findall(
        r'https://[A-Za-z0-9.-]+\.roadsummary\.com/rtplive/[A-Za-z0-9_-]+/playlist\.m3u8',
        text,
        re.I,
    )
    for candidate in candidates:
        parsed = urllib.parse.urlparse(html.unescape(candidate))
        path_parts = parsed.path.strip('/').split('/')
        if (
            parsed.scheme == 'https' and
            allowed_stream_host(parsed.hostname) and
            len(path_parts) == 3 and
            path_parts[0] == 'rtplive' and
            path_parts[1].upper() == code and
            path_parts[2] == 'playlist.m3u8'
        ):
            return urllib.parse.urlunparse(parsed)
    return None


def fetch_wv511_kml(dataset_key):
    text = wv511_cached_text(dataset_key, WV511_KML_URLS[dataset_key])
    root = ET.fromstring(text)
    items = []
    for placemark in root.findall('.//{*}Placemark'):
        point = placemark.find('.//{*}Point/{*}coordinates')
        coords = str(point.text or '').strip().split(',') if point is not None else []
        if len(coords) < 2:
            continue
        raw_description = placemark.findtext('{*}description') or ''
        title_match = re.search(r'\btitle=["\']([^"\']*)', raw_description, re.I)
        items.append({
            'raw_id': str(placemark.get('id') or ''),
            'name': display_text(placemark.findtext('{*}name')),
            'description': strip_tags(raw_description),
            'message': html.unescape(title_match.group(1)).strip() if title_match else '',
            'lon': safe_float(coords[0]),
            'lat': safe_float(coords[1]),
        })
    return items


def wv511_numeric_id(raw_id):
    return str(int.from_bytes(hashlib.sha256(str(raw_id).encode('utf-8')).digest()[:7], 'big'))


def wv511_items_for_layer(layer):
    if layer == 'MessageSigns':
        return fetch_wv511_kml('MessageSignsActive') + fetch_wv511_kml('MessageSignsInactive')
    return fetch_wv511_kml(layer)


def wv511_layer_payload(layer):
    if layer not in {'Cameras', 'MessageSigns', 'Incidents', 'Construction'}:
        return {'item2': []}
    normalized = []
    if layer == 'Cameras':
        for camera in fetch_wv511_cameras():
            normalized.append({
                'itemId': camera['id'],
                'location': [camera['lat'], camera['lon']],
                'title': camera['name'],
                'expando': {
                    'videoEnabled': True,
                    'videoId': camera['id'],
                    'videoUrl': None,
                    'snapshotUrl': None,
                    'snapshotFromVideo': True,
                },
            })
        return {'item2': normalized}

    for item in wv511_items_for_layer(layer):
        item_id = wv511_numeric_id(item['raw_id'])
        if layer == 'MessageSigns':
            expando = {'message': item['message'], 'timestamp': None}
        else:
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': item['description'],
                'severity': None,
                'timestamp': None,
                'location': None,
            }
        normalized.append({
            'itemId': item_id,
            'location': [item['lat'], item['lon']],
            'title': item['name'],
            'expando': expando,
        })
    return {'item2': normalized}


def wv511_camera_detail(site_id):
    target_id = str(site_id)
    for camera in fetch_wv511_cameras():
        if camera['id'] != target_id:
            continue
        video_url = wv511_camera_stream_url(camera['camera_code'])
        return {
            'name': camera['name'],
            'msg': camera.get('route'),
            'severity': None,
            'timestamp': None,
            'video_id': target_id,
            'video_url': video_url,
            'video_enabled': bool(video_url),
            'snapshot_url': None,
            'upstream_snapshot_url': None,
            'snapshot_from_video': True,
        }
    raise ValueError(f'West Virginia camera {site_id} not found')


def wv511_tooltip(layer, item_id):
    if layer == 'Cameras':
        return wv511_camera_detail(item_id)
    for item in wv511_items_for_layer(layer):
        if wv511_numeric_id(item['raw_id']) != str(item_id):
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'] or 'WV511 message sign',
                'msg': item['message'] or 'No message displayed',
                'severity': None,
                'timestamp': None,
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'] or ('Construction' if layer == 'Construction' else 'Traffic incident'),
            'msg': item['description'],
            'severity': None,
            'timestamp': None,
            'location': None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'WV511 {layer} item {item_id} not found')


def fetch_mdchart_dataset(layer):
    now = time.time()
    with MDCHART_TRAFFIC_CACHE_LOCK:
        cached = MDCHART_TRAFFIC_CACHE.get(layer)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        data = fetch_json_url(MDCHART_TRAFFIC_URLS[layer], headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': 'https://chart.maryland.gov/',
            'Accept': 'application/json',
        }, timeout=30)
        items = data.get('data')
        if not isinstance(items, list):
            raise ValueError(f'Maryland CHART {layer} returned no data array')
    except Exception:
        if cached:
            return cached['items']
        raise
    with MDCHART_TRAFFIC_CACHE_LOCK:
        MDCHART_TRAFFIC_CACHE[layer] = {
            'items': items,
            'expires_at': time.time() + MDCHART_CACHE_TTL,
        }
    return items


def mdchart_numeric_id(raw_id):
    return str(int.from_bytes(hashlib.sha256(str(raw_id).encode('utf-8')).digest()[:7], 'big'))


def mdchart_camera_values(camera):
    raw_id = str(camera.get('id') or '').strip()
    stream_host = str(camera.get('cctvIp') or '').strip().lower()
    stream_url = f'https://{stream_host}/rtplive/{raw_id}/playlist.m3u8' if raw_id and stream_host else ''
    parsed_stream = urllib.parse.urlparse(stream_url)
    online = (
        str(camera.get('opStatus') or '').upper() == 'OK' and
        str(camera.get('commMode') or '').upper() == 'ONLINE'
    )
    if parsed_stream.scheme != 'https' or not allowed_stream_host(parsed_stream.hostname):
        stream_url = ''
    snapshot_url = f'https://chart.maryland.gov/wwwroot/thumbnails/{raw_id}.jpg' if raw_id else ''
    return raw_id, stream_url, snapshot_url, bool(online and stream_url)


def mdchart_layer_payload(layer):
    if layer not in {'Cameras', 'MessageSigns', 'Incidents', 'Construction'}:
        return {'item2': []}
    bounds = REGIONS['MD']['bounds']
    normalized = []
    for item in fetch_mdchart_dataset(layer):
        if item.get('closed') is True:
            continue
        lat = safe_float(item.get('lat'))
        lon = safe_float(item.get('lon'))
        if not (
            bounds['min_lat'] <= lat <= bounds['max_lat'] and
            bounds['min_lon'] <= lon <= bounds['max_lon']
        ):
            continue
        item_id = mdchart_numeric_id(item.get('id'))
        if layer == 'Cameras':
            raw_id, video_url, snapshot_url, video_enabled = mdchart_camera_values(item)
            title = item.get('description') or item.get('name') or 'Maryland CHART traffic camera'
            expando = {
                'videoEnabled': video_enabled,
                'videoId': item_id,
                'videoUrl': video_url or None,
                'snapshotUrl': f'/camera-snapshot/MD/{item_id}' if snapshot_url else None,
                'upstreamId': raw_id,
            }
        elif layer == 'MessageSigns':
            title = item.get('description') or item.get('name') or 'Maryland CHART message sign'
            expando = {
                'message': strip_tags(item.get('msgPlain') or item.get('msgMulti') or ''),
                'timestamp': epoch_milliseconds_iso(item.get('lastCachedDataUpdateTime')),
            }
        else:
            title = item.get('incidentType') or ('Active Closure' if layer == 'Construction' else 'Traffic incident')
            description = (
                item.get('publicComments') or item.get('trafficAlertTextMsg') or
                item.get('description') or item.get('name')
            )
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': description,
                'severity': 'Traffic Alert' if item.get('trafficAlert') else item.get('incidentType'),
                'timestamp': epoch_milliseconds_iso(
                    item.get('startDateTime') or item.get('createTime') or item.get('lastCachedDataUpdateTime')
                ),
                'location': item.get('description') or item.get('name'),
            }
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def mdchart_camera_detail(site_id):
    target_id = str(site_id)
    for camera in fetch_mdchart_dataset('Cameras'):
        if mdchart_numeric_id(camera.get('id')) != target_id:
            continue
        raw_id, video_url, snapshot_url, video_enabled = mdchart_camera_values(camera)
        category = ', '.join(camera.get('cameraCategories') or [])
        route = ' '.join(
            str(value).strip()
            for value in (camera.get('routePrefix'), camera.get('routeNumber'), camera.get('routeSuffix'))
            if str(value or '').strip()
        )
        return {
            'name': camera.get('description') or camera.get('name') or f'Maryland camera {raw_id}',
            'msg': ' · '.join(part for part in (route, category) if part) or None,
            'severity': None,
            'timestamp': epoch_milliseconds_iso(camera.get('lastCachedDataUpdateTime')),
            'video_id': target_id,
            'video_url': video_url or None,
            'video_enabled': video_enabled,
            'snapshot_url': f'/camera-snapshot/MD/{target_id}' if snapshot_url else None,
            'upstream_snapshot_url': snapshot_url or None,
        }
    raise ValueError(f'Maryland CHART camera {site_id} not found')


def mdchart_tooltip(layer, item_id):
    if layer == 'Cameras':
        return mdchart_camera_detail(item_id)
    for item in fetch_mdchart_dataset(layer):
        if mdchart_numeric_id(item.get('id')) != str(item_id):
            continue
        if layer == 'MessageSigns':
            return {
                'name': item.get('description') or item.get('name') or 'Maryland CHART message sign',
                'msg': strip_tags(item.get('msgPlain') or item.get('msgMulti') or '') or 'No message displayed',
                'severity': None,
                'timestamp': epoch_milliseconds_iso(item.get('lastCachedDataUpdateTime')),
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item.get('incidentType') or ('Active Closure' if layer == 'Construction' else 'Traffic incident'),
            'msg': (
                item.get('publicComments') or item.get('trafficAlertTextMsg') or
                item.get('description') or item.get('name')
            ),
            'severity': 'Traffic Alert' if item.get('trafficAlert') else item.get('incidentType'),
            'timestamp': epoch_milliseconds_iso(
                item.get('startDateTime') or item.get('createTime') or item.get('lastCachedDataUpdateTime')
            ),
            'location': item.get('description') or item.get('name'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'Maryland CHART {layer} item {item_id} not found')


def dcgis_query_where(layer):
    if layer == 'Cameras':
        return 'Operation_Status = 1'
    if layer == 'Construction':
        now_text = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        return (
            f"EXPIRATIONDATE >= TIMESTAMP '{now_text}' AND "
            f"EFFECTIVEDATE <= TIMESTAMP '{now_text}' AND "
            "STATUS = 'Issued' AND (ISEXCAVATION = 'T' OR ISPAVING = 'T')"
        )
    return '1=0'


def fetch_dcgis_dataset(layer):
    if layer not in {'Cameras', 'Construction'}:
        return []
    now = time.time()
    with DCGIS_TRAFFIC_CACHE_LOCK:
        cached = DCGIS_TRAFFIC_CACHE.get(layer)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        if layer == 'Cameras':
            bounds = REGIONS['DC']['bounds']
            form = urllib.parse.urlencode({
                'box-within': 'true',
                'box-loc': ','.join(str(value) for value in (
                    bounds['min_lon'], bounds['min_lat'],
                    bounds['max_lon'], bounds['max_lat'],
                )),
                'pf': 'formatted',
                'system': 'all',
                'type': 'device_cctv',
                'params': 'hideInactive',
            }).encode('utf-8')
            data = fetch_json_url(DC_TRAFFICVIEW_CCTV_URL, headers={
                'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
                'Referer': 'https://www.trafficview.org/live_traffic/',
                'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            }, data=form, timeout=30)
            page = data.get('features')
            if not isinstance(page, list):
                raise ValueError('TrafficView returned no D.C. camera feature array')
            items = [
                feature for feature in page
                if str((feature.get('attributes') or {}).get('id') or '').startswith('DDOT_')
            ]
        else:
            items = []
            offset = 0
            while offset < 5000:
                params = urllib.parse.urlencode({
                    'where': dcgis_query_where(layer),
                    'outFields': '*',
                    'returnGeometry': 'true',
                    'outSR': '4326',
                    'orderByFields': 'OBJECTID ASC',
                    'resultOffset': str(offset),
                    'resultRecordCount': '1000',
                    'f': 'json',
                })
                data = fetch_json_url(f'{DCGIS_TRAFFIC_URLS[layer]}?{params}', headers={
                    'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
                    'Referer': 'https://opendata.dc.gov/',
                    'Accept': 'application/json',
                }, timeout=30)
                page = data.get('features')
                if not isinstance(page, list):
                    raise ValueError(f'DDOT {layer} returned no feature array')
                items.extend(page)
                if len(page) < 1000 and not data.get('exceededTransferLimit'):
                    break
                offset += len(page)
                if not page:
                    break
    except Exception:
        if cached:
            return cached['items']
        raise
    with DCGIS_TRAFFIC_CACHE_LOCK:
        DCGIS_TRAFFIC_CACHE[layer] = {
            'items': items,
            'expires_at': time.time() + DCGIS_CACHE_TTL,
        }
    return items


def dcgis_item_id(layer, attrs):
    if layer == 'Cameras':
        return mdchart_numeric_id(f'DC:{attrs.get("id") or ""}')
    return str(safe_int(attrs.get('OBJECTID')))


def dcgis_camera_detail(site_id):
    target_id = str(site_id)
    for feature in fetch_dcgis_dataset('Cameras'):
        attrs = feature.get('attributes') or {}
        if dcgis_item_id('Cameras', attrs) != target_id:
            continue
        raw_id = str(attrs.get('id') or '').strip()
        online = str(attrs.get('status') or '').lower() == 'on'
        snapshot_url = f'{DC_TRAFFICVIEW_THUMBNAIL_ROOT}/{urllib.parse.quote(raw_id)}' if online else ''
        video_url = (
            f'wss://cctv.trafficview.org:8420/{urllib.parse.quote(raw_id)}.vod?progressive'
            if online else ''
        )
        return {
            'name': display_text(attrs.get('Location') or f'DDOT camera {raw_id}'),
            'msg': 'Public DDOT live camera and snapshot via MATOC TrafficView',
            'severity': None,
            'timestamp': None,
            'video_id': target_id,
            'video_url': video_url or None,
            'video_enabled': bool(video_url),
            'snapshot_url': f'/camera-snapshot/DC/{target_id}' if snapshot_url else None,
            'upstream_snapshot_url': snapshot_url or None,
        }
    raise ValueError(f'DDOT camera {site_id} not found')


def dcgis_permit_kind(attrs):
    kinds = []
    if str(attrs.get('ISEXCAVATION') or '').upper() == 'T':
        kinds.append('Excavation')
    if str(attrs.get('ISPAVING') or '').upper() == 'T':
        kinds.append('Paving')
    return ' / '.join(kinds) or 'Road work'


def dcgis_layer_payload(layer):
    if layer not in {'Cameras', 'MessageSigns', 'Incidents', 'Construction'}:
        return {'item2': []}
    if layer in {'MessageSigns', 'Incidents'}:
        return {'item2': []}
    bounds = REGIONS['DC']['bounds']
    normalized = []
    for feature in fetch_dcgis_dataset(layer):
        attrs = feature.get('attributes') or {}
        geometry = feature.get('geometry') or {}
        lat = safe_float(
            geometry.get('lat') if layer == 'Cameras'
            else geometry.get('y') or attrs.get('LATITUDE') or attrs.get('Latitude')
        )
        lon = safe_float(
            geometry.get('lon') if layer == 'Cameras'
            else geometry.get('x') or attrs.get('LONGITUDE') or attrs.get('Longitude')
        )
        if not (
            bounds['min_lat'] <= lat <= bounds['max_lat'] and
            bounds['min_lon'] <= lon <= bounds['max_lon']
        ):
            continue
        item_id = dcgis_item_id(layer, attrs)
        if layer == 'Cameras':
            if str(attrs.get('status') or '').lower() != 'on':
                continue
            title = display_text(attrs.get('Location') or f'DDOT camera {item_id}')
            expando = {
                'videoEnabled': True,
                'videoId': item_id,
                'videoUrl': (
                    f'wss://cctv.trafficview.org:8420/'
                    f'{urllib.parse.quote(str(attrs.get("id") or ""))}.vod?progressive'
                ),
                'snapshotUrl': f'/camera-snapshot/DC/{item_id}',
            }
        else:
            address = display_text(attrs.get('WLFULLADDRESS') or 'Washington, D.C.')
            kind = dcgis_permit_kind(attrs)
            permit = display_text(attrs.get('PERMITNUMBER') or attrs.get('TRACKINGNUMBER'))
            work = display_text(attrs.get('WORKDETAIL'))
            title = f'{kind} · {address}'
            expando = {
                'feedLabel': 'Construction',
                'description': ' · '.join(part for part in (kind, address, work) if part),
                'severity': f'Issued permit {permit}' if permit else 'Issued permit',
                'timestamp': epoch_milliseconds_iso(attrs.get('ISSUEDATE') or attrs.get('EDITED')),
                'location': address,
            }
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def dcgis_tooltip(layer, item_id):
    if layer in {'MessageSigns', 'Incidents'}:
        raise ValueError(f'DDOT does not publish a {layer} feed')
    if layer == 'Cameras':
        return dcgis_camera_detail(item_id)
    for feature in fetch_dcgis_dataset(layer):
        attrs = feature.get('attributes') or {}
        if dcgis_item_id(layer, attrs) != str(item_id):
            continue
        address = display_text(attrs.get('WLFULLADDRESS') or 'Washington, D.C.')
        kind = dcgis_permit_kind(attrs)
        work = display_text(attrs.get('WORKDETAIL'))
        permit = display_text(attrs.get('PERMITNUMBER') or attrs.get('TRACKINGNUMBER'))
        return {
            'name': f'{kind} · {address}',
            'msg': ' · '.join(part for part in (kind, address, work) if part),
            'severity': f'Issued permit {permit}' if permit else 'Issued permit',
            'timestamp': epoch_milliseconds_iso(attrs.get('ISSUEDATE') or attrs.get('EDITED')),
            'location': address,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'DDOT {layer} item {item_id} not found')


def deldot_numeric_id(kind, raw_id):
    return mdchart_numeric_id(f'DELDOT:{kind}:{raw_id}')


def fetch_deldot_dataset(kind):
    if kind not in DELDOT_TRAFFIC_URLS:
        return []
    now = time.time()
    with DELDOT_TRAFFIC_CACHE_LOCK:
        cached = DELDOT_TRAFFIC_CACHE.get(kind)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        data = fetch_json_url(DELDOT_TRAFFIC_URLS[kind], headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': DELDOT_SOURCE_URL,
            'Accept': 'application/json',
        }, timeout=30)
        if kind == 'Cameras':
            items = data.get('videoCameras')
        elif kind == 'Advisories':
            items = data.get('advisories')
        elif kind == 'Restrictions':
            items = data.get('restrictions')
        elif kind == 'MessageSigns':
            items = [
                sign
                for sign_type in (data.get('signTypes') or [])
                for sign in (sign_type.get('signs') or [])
            ]
        else:
            items = data.get('stations')
        if not isinstance(items, list):
            raise ValueError(f'DelDOT {kind} returned no item array')
    except Exception:
        if cached:
            return cached['items']
        raise
    with DELDOT_TRAFFIC_CACHE_LOCK:
        DELDOT_TRAFFIC_CACHE[kind] = {
            'items': items,
            'expires_at': time.time() + DELDOT_CACHE_TTL,
        }
    return items


def deldot_advisory_is_construction(item):
    advisory_type = item.get('type') or {}
    return (
        str(advisory_type.get('code') or '').upper() == 'C' or
        str(advisory_type.get('name') or '').lower() == 'construction'
    )


def deldot_items_for_layer(layer):
    if layer == 'Cameras':
        return [('camera', item) for item in fetch_deldot_dataset('Cameras')]
    if layer == 'MessageSigns':
        return [('sign', item) for item in fetch_deldot_dataset('MessageSigns')]
    advisories = fetch_deldot_dataset('Advisories')
    if layer == 'Incidents':
        return [('advisory', item) for item in advisories if not deldot_advisory_is_construction(item)]
    if layer == 'Construction':
        return (
            [('advisory', item) for item in advisories if deldot_advisory_is_construction(item)] +
            [('restriction', item) for item in fetch_deldot_dataset('Restrictions')]
        )
    return []


def deldot_raw_id(kind, item):
    if kind == 'camera':
        return item.get('id')
    if kind == 'sign':
        return item.get('permit') or item.get('systemId')
    if kind == 'restriction':
        return item.get('restrictionId')
    return item.get('id') or (item.get('published') or {}).get('id')


def deldot_location(kind, item):
    where = item.get('where') or {}
    lat = safe_float(item.get('lat') if kind in {'camera', 'sign'} else where.get('lat'))
    lon = safe_float(item.get('lon') if kind in {'camera', 'sign'} else where.get('lon'))
    return lat, lon


def deldot_camera_values(item):
    raw_id = str(item.get('id') or '').strip()
    video_url = str((item.get('urls') or {}).get('m3u8s') or '').strip()
    parsed_video = urllib.parse.urlparse(video_url)
    active = item.get('enabled') is True and str(item.get('status') or '').lower() == 'active'
    if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
        video_url = ''
    return raw_id, video_url, bool(active and video_url)


def deldot_event_values(kind, item):
    where = item.get('where') or {}
    location = display_text(where.get('location'))
    if kind == 'restriction':
        title = display_text(item.get('title') or location or 'DelDOT road restriction')
        description = ' · '.join(part for part in (
            display_text(item.get('impactType')),
            display_text(item.get('construction')),
            display_text(item.get('impactedRoadway')),
        ) if part)
        return {
            'title': title,
            'description': description or title,
            'severity': display_text(item.get('impactType')) or 'Road restriction',
            'timestamp': item.get('startDate'),
            'location': location or title,
        }
    advisory_type = item.get('type') or {}
    effect = display_text(item.get('effect'))
    impact = display_text(item.get('impact'))
    type_name = display_text(advisory_type.get('name'))
    title = location or type_name or 'DelDOT traffic advisory'
    return {
        'title': title,
        'description': ' · '.join(part for part in (type_name, location, effect, impact) if part),
        'severity': type_name or None,
        'timestamp': item.get('timestamp'),
        'location': location,
    }


def deldot_layer_payload(layer):
    if layer not in {'Cameras', 'MessageSigns', 'Incidents', 'Construction'}:
        return {'item2': []}
    bounds = REGIONS['DE']['bounds']
    normalized = []
    for kind, item in deldot_items_for_layer(layer):
        lat, lon = deldot_location(kind, item)
        if not (
            bounds['min_lat'] <= lat <= bounds['max_lat'] and
            bounds['min_lon'] <= lon <= bounds['max_lon']
        ):
            continue
        raw_id = deldot_raw_id(kind, item)
        item_id = deldot_numeric_id(kind, raw_id)
        if kind == 'camera':
            _, video_url, video_enabled = deldot_camera_values(item)
            title = display_text(item.get('title') or f'DelDOT camera {raw_id}')
            expando = {
                'videoEnabled': video_enabled,
                'videoId': item_id,
                'videoUrl': video_url or None,
                'snapshotUrl': None,
                'snapshotFromVideo': video_enabled,
            }
        elif kind == 'sign':
            title = display_text(item.get('title') or item.get('permit') or 'DelDOT message sign')
            expando = {
                'message': strip_tags(item.get('message')) or 'No message displayed',
                'timestamp': epoch_milliseconds_iso(item.get('messageLastUpdated') or item.get('timestamp')),
            }
        else:
            values = deldot_event_values(kind, item)
            title = values['title']
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': values['description'],
                'severity': values['severity'],
                'timestamp': values['timestamp'],
                'location': values['location'],
            }
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def deldot_camera_detail(site_id):
    target_id = str(site_id)
    for item in fetch_deldot_dataset('Cameras'):
        raw_id = deldot_raw_id('camera', item)
        if deldot_numeric_id('camera', raw_id) != target_id:
            continue
        _, video_url, video_enabled = deldot_camera_values(item)
        return {
            'name': display_text(item.get('title') or f'DelDOT camera {raw_id}'),
            'msg': ' · '.join(part for part in (
                display_text(item.get('county')),
                display_text(item.get('status')),
                f'DelDOT {raw_id}',
            ) if part),
            'severity': None,
            'timestamp': None,
            'video_id': target_id,
            'video_url': video_url or None,
            'video_enabled': video_enabled,
            'snapshot_url': None,
            'upstream_snapshot_url': None,
            'snapshot_from_video': video_enabled,
        }
    raise ValueError(f'DelDOT camera {site_id} not found')


def deldot_tooltip(layer, item_id):
    if layer == 'Cameras':
        return deldot_camera_detail(item_id)
    for kind, item in deldot_items_for_layer(layer):
        raw_id = deldot_raw_id(kind, item)
        if deldot_numeric_id(kind, raw_id) != str(item_id):
            continue
        if kind == 'sign':
            return {
                'name': display_text(item.get('title') or item.get('permit') or 'DelDOT message sign'),
                'msg': strip_tags(item.get('message')) or 'No message displayed',
                'severity': None,
                'timestamp': epoch_milliseconds_iso(item.get('messageLastUpdated') or item.get('timestamp')),
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        values = deldot_event_values(kind, item)
        return {
            'name': values['title'],
            'msg': values['description'],
            'severity': values['severity'],
            'timestamp': values['timestamp'],
            'location': values['location'],
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'DelDOT {layer} item {item_id} not found')


def fetch_pa511_dataset(source_layer):
    now = time.time()
    with PA511_TRAFFIC_CACHE_LOCK:
        cached = PA511_TRAFFIC_CACHE.get(source_layer)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        data = fetch_json_url(
            f'https://www.511pa.com/map/mapIcons/{source_layer}',
            headers={
                'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
                'Referer': 'https://www.511pa.com/map',
                'Accept': 'application/json',
            },
            timeout=30,
        )
        items = data.get('item2') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError(f'511PA {source_layer} returned no item array')
    except Exception:
        if cached:
            return cached['items']
        raise
    with PA511_TRAFFIC_CACHE_LOCK:
        PA511_TRAFFIC_CACHE[source_layer] = {
            'items': items,
            'expires_at': time.time() + PA511_CACHE_TTL,
        }
    return items


def pa511_item_id(layer, source_layer, raw_id):
    if layer in {'Cameras', 'MessageSigns'}:
        return str(raw_id)
    return mdchart_numeric_id(f'PA511:{source_layer}:{raw_id}')


def pa511_items_for_layer(layer):
    return [
        (source_layer, item)
        for source_layer in PA511_LAYER_SOURCES.get(layer, ())
        for item in fetch_pa511_dataset(source_layer)
    ]


def pa511_layer_payload(layer):
    bounds = REGIONS['PA']['bounds']
    normalized = []
    for source_layer, item in pa511_items_for_layer(layer):
        location = item.get('location') or []
        if len(location) < 2:
            continue
        lat = safe_float(location[0])
        lon = safe_float(location[1])
        if not (
            bounds['min_lat'] <= lat <= bounds['max_lat'] and
            bounds['min_lon'] <= lon <= bounds['max_lon']
        ):
            continue
        raw_id = str(item.get('itemId') or '').strip()
        if not raw_id.isdigit():
            continue
        item_id = pa511_item_id(layer, source_layer, raw_id)
        if layer == 'Cameras':
            # 511PA's icon feed does not include camera names. Leaving the
            # title empty lets the client hydrate visible/selected cameras
            # from the official tooltip instead of treating a placeholder as
            # a resolved name.
            title = ''
            expando = {
                'videoEnabled': False,
                'videoId': item_id,
                'videoUrl': None,
                'snapshotUrl': f'/camera-snapshot/PA/{raw_id}',
            }
        elif layer == 'MessageSigns':
            title = '511PA message sign'
            expando = {'message': 'Open for the current sign message'}
        else:
            title = PA511_LAYER_LABELS.get(source_layer, '511PA traffic event')
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': title,
                'severity': title,
                'timestamp': None,
                'location': None,
            }
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def fetch_pa511_tooltip_html(source_layer, raw_id):
    key = f'{source_layer}:{raw_id}'
    now = time.time()
    with PA511_TOOLTIP_CACHE_LOCK:
        cached = PA511_TOOLTIP_CACHE.get(key)
        if cached and now < cached['expires_at']:
            return cached['html']
    req = urllib.request.Request(
        f'https://www.511pa.com/tooltip/{source_layer}/{raw_id}?lang=en',
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': 'https://www.511pa.com/map',
            'Accept': 'text/html,application/xhtml+xml',
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    if raw[:2] == b'\x1f\x8b':
        import gzip
        raw = gzip.decompress(raw)
    text = raw.decode('utf-8', errors='replace')
    with PA511_TOOLTIP_CACHE_LOCK:
        if len(PA511_TOOLTIP_CACHE) >= 3000:
            PA511_TOOLTIP_CACHE.pop(next(iter(PA511_TOOLTIP_CACHE)))
        PA511_TOOLTIP_CACHE[key] = {
            'html': text,
            'expires_at': time.time() + PA511_TOOLTIP_CACHE_TTL,
        }
    return text


def pa511_html_text(value):
    text = re.sub(r'<br\s*/?>', ' · ', str(value or ''), flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', html.unescape(text)).strip(' ·')


def pa511_tooltip_detail(source_layer, raw_id):
    raw = fetch_pa511_tooltip_html(source_layer, raw_id)
    common = _parse_511_tooltip_html(raw)
    heading_match = re.search(r'<h4[^>]*>(.*?)</h4>', raw, re.S | re.I)
    description_match = re.search(
        r'<td[^>]+class=["\'][^"\']*descSection[^"\']*["\'][^>]*>(.*?)</td>',
        raw,
        re.S | re.I,
    )
    location_match = re.search(
        r'<strong[^>]*>\s*Location\s*</strong>\s*<br\s*/?>\s*(.*?)</td>',
        raw,
        re.S | re.I,
    )
    updated_match = re.search(
        r'<th[^>]*>\s*Last Updated\s*</th>\s*<td[^>]*>(.*?)</td>',
        raw,
        re.S | re.I,
    )
    message_match = re.search(
        r'<td[^>]+class=["\'][^"\']*msgContent[^"\']*["\'][^>]*>(.*?)</td>',
        raw,
        re.S | re.I,
    )
    media_match = re.search(r'data-lazy=["\']/map/Cctv/(\d+)', raw, re.I)
    heading = pa511_html_text(heading_match.group(1)) if heading_match else ''
    description = pa511_html_text(description_match.group(1)) if description_match else ''
    location = pa511_html_text(location_match.group(1)) if location_match else ''
    timestamp = pa511_html_text(updated_match.group(1)) if updated_match else common.get('timestamp')
    media_id = media_match.group(1) if media_match else None
    if source_layer in PA511_LAYER_LABELS:
        name = description or heading or PA511_LAYER_LABELS[source_layer]
    else:
        name = common.get('name') or description or heading or PA511_LAYER_LABELS.get(source_layer)
    message = (
        pa511_html_text(message_match.group(1)) if message_match else
        description or common.get('msg') or heading
    )
    detail = {
        'name': name,
        'msg': message,
        'severity': PA511_LAYER_LABELS.get(source_layer) or common.get('severity'),
        'timestamp': timestamp,
        'location': location or None,
        'video_id': str(raw_id) if source_layer == 'Cameras' else None,
        'video_url': None,
        'video_enabled': False,
    }
    if source_layer == 'Cameras':
        detail.update({
            'snapshot_url': f'/camera-snapshot/PA/{raw_id}' if media_id else None,
            'upstream_snapshot_url': f'https://www.511pa.com/map/Cctv/{media_id}' if media_id else None,
        })
    return detail


def pa511_find_item(layer, item_id):
    target = str(item_id)
    for source_layer, item in pa511_items_for_layer(layer):
        raw_id = str(item.get('itemId') or '').strip()
        if pa511_item_id(layer, source_layer, raw_id) == target:
            return source_layer, raw_id
    raise ValueError(f'511PA {layer} item {item_id} not found')


def pa511_camera_detail(site_id):
    if not str(site_id).isdigit():
        raise ValueError('Invalid 511PA camera id')
    return pa511_tooltip_detail('Cameras', str(site_id))


def pa511_tooltip(layer, item_id):
    source_layer, raw_id = pa511_find_item(layer, item_id)
    return pa511_tooltip_detail(source_layer, raw_id)


def njta_cached_items(cache_key, loader):
    now = time.time()
    with NJTA_TRAFFIC_CACHE_LOCK:
        cached = NJTA_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with NJTA_TRAFFIC_CACHE_LOCK:
        NJTA_TRAFFIC_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + NJTA_CACHE_TTL,
        }
    return items


def fetch_njta_cameras():
    def load():
        page = fetch_text_url(NJTA_CAMERA_URL, referer=NJTA_CAMERA_URL, timeout=30)
        match = re.search(r'\bdata-block-config=(?P<quote>["\'])(?P<data>.*?)(?P=quote)', page, re.S | re.I)
        if not match:
            raise ValueError('NJTA camera configuration was not found')
        config = json.loads(html.unescape(match.group('data')))
        groups = (((config.get('initialData') or {}).get('cameras')) or {})
        bounds = REGIONS['NJ']['bounds']
        cameras = []
        for roadway, group in groups.items():
            for item in group or []:
                item_id = str(item.get('id') or '').strip()
                lat = safe_float(item.get('lat'))
                lon = safe_float(item.get('lng'))
                video_url = str(item.get('video_url') or '').strip()
                parsed_video = urllib.parse.urlparse(video_url)
                if not item_id.isdigit() or not (
                    bounds['min_lat'] <= lat <= bounds['max_lat'] and
                    bounds['min_lon'] <= lon <= bounds['max_lon']
                ):
                    continue
                if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                    video_url = ''
                road_name = 'New Jersey Turnpike' if roadway == 'turnpike' else 'Garden State Parkway'
                location = display_text(item.get('relative_text'))
                direction = display_text(item.get('relative_direction'))
                mile_marker = item.get('mile_marker')
                details = [road_name]
                if location:
                    details.append(location)
                if direction:
                    details.append(f'{direction}bound')
                if mile_marker is not None:
                    details.append(f'Mile {mile_marker}')
                cameras.append({
                    'id': item_id,
                    'name': ' · '.join(details),
                    'roadway': road_name,
                    'lat': lat,
                    'lon': lon,
                    'video_url': video_url,
                })
        if not cameras:
            raise ValueError('NJTA returned no cameras')
        return cameras

    return njta_cached_items('Cameras', load)


def njta_alert_numeric_id(roadway, item):
    raw = '|'.join((
        str(roadway or ''),
        str(item.get('description') or ''),
        str(item.get('start') or ''),
    ))
    return str(int.from_bytes(hashlib.sha256(raw.encode('utf-8')).digest()[:7], 'big'))


def njta_alert_is_construction(item):
    values = item.get('types_raw') or [item.get('types')]
    text = ' '.join(str(value or '') for value in values).lower()
    return any(token in text for token in (
        'construction', 'roadwork', 'maintenance', 'milling', 'paving', 'bridge work',
    ))


def fetch_njta_alerts():
    def load():
        data = fetch_json_url(NJTA_ALERTS_URL, headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': NJTA_SOURCE_URL,
            'Accept': 'application/json',
        }, timeout=30)
        bounds = REGIONS['NJ']['bounds']
        alerts = []
        for roadway, group in (data.items() if isinstance(data, dict) else ()):
            road_name = 'New Jersey Turnpike' if roadway == 'turnpike' else 'Garden State Parkway'
            for item in group or []:
                location = item.get('location') or {}
                lat = safe_float(location.get('lat'))
                lon = safe_float(location.get('lng'))
                if not (
                    bounds['min_lat'] <= lat <= bounds['max_lat'] and
                    bounds['min_lon'] <= lon <= bounds['max_lon']
                ):
                    continue
                values = dict(item)
                values.update({
                    'id': njta_alert_numeric_id(roadway, item),
                    'roadway': road_name,
                    'lat': lat,
                    'lon': lon,
                    'construction': njta_alert_is_construction(item),
                })
                alerts.append(values)
        return alerts

    return njta_cached_items('Alerts', load)


def njta_layer_payload(layer):
    if layer == 'MessageSigns':
        return {'item2': []}
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['id'],
                'videoUrl': camera['video_url'] or None,
                'snapshotUrl': None,
                'snapshotFromVideo': bool(camera['video_url']),
            },
        } for camera in fetch_njta_cameras()]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    wants_construction = layer == 'Construction'
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': display_text(item.get('types')) or (
            'Construction' if wants_construction else 'Traffic incident'
        ),
        'expando': {
            'feedLabel': 'Construction' if wants_construction else 'Traffic Incident',
            'description': item.get('description'),
            'severity': 'Major' if item.get('is_major') else display_text(item.get('types')),
            'timestamp': epoch_milliseconds_iso(item.get('updated') or item.get('start')),
            'location': item.get('roadway'),
        },
    } for item in fetch_njta_alerts() if item['construction'] == wants_construction]}


def njta_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_njta_cameras():
        if camera['id'] != target:
            continue
        return {
            'name': camera['name'],
            'msg': camera['roadway'],
            'severity': None,
            'timestamp': None,
            'video_id': target,
            'video_url': camera['video_url'] or None,
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': None,
            'upstream_snapshot_url': None,
            'snapshot_from_video': bool(camera['video_url']),
        }
    raise ValueError(f'NJTA camera {site_id} not found')


def njta_tooltip(layer, item_id):
    if layer == 'Cameras':
        return njta_camera_detail(item_id)
    wants_construction = layer == 'Construction'
    for item in fetch_njta_alerts():
        if item['id'] != str(item_id) or item['construction'] != wants_construction:
            continue
        return {
            'name': display_text(item.get('types')) or (
                'Construction' if wants_construction else 'Traffic incident'
            ),
            'msg': item.get('description'),
            'severity': 'Major' if item.get('is_major') else display_text(item.get('types')),
            'timestamp': epoch_milliseconds_iso(item.get('updated') or item.get('start')),
            'location': item.get('roadway'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'NJTA {layer} item {item_id} not found')


def fetch_ctroads_dataset(source_layer):
    now = time.time()
    with CTROADS_TRAFFIC_CACHE_LOCK:
        cached = CTROADS_TRAFFIC_CACHE.get(source_layer)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        raw = fetch_iteris_layer(REGIONS['CT'], source_layer)
        data = json.loads(raw.decode('utf-8-sig'))
        items = data.get('item2') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError(f'CTroads {source_layer} returned no item array')
    except Exception:
        if cached:
            return cached['items']
        raise
    with CTROADS_TRAFFIC_CACHE_LOCK:
        CTROADS_TRAFFIC_CACHE[source_layer] = {
            'items': items,
            'expires_at': time.time() + CTROADS_CACHE_TTL,
        }
    return items


def ctroads_item_id(layer, source_layer, raw_id):
    if layer in {'Cameras', 'MessageSigns'}:
        return str(raw_id)
    return mdchart_numeric_id(f'CTROADS:{source_layer}:{raw_id}')


def ctroads_items_for_layer(layer):
    return [
        (source_layer, item)
        for source_layer in CTROADS_LAYER_SOURCES.get(layer, ())
        for item in fetch_ctroads_dataset(source_layer)
    ]


def ctroads_layer_payload(layer):
    bounds = REGIONS['CT']['bounds']
    normalized = []
    for source_layer, item in ctroads_items_for_layer(layer):
        location = item.get('location') or []
        if len(location) < 2:
            continue
        lat = safe_float(location[0])
        lon = safe_float(location[1])
        if not (
            bounds['min_lat'] <= lat <= bounds['max_lat'] and
            bounds['min_lon'] <= lon <= bounds['max_lon']
        ):
            continue
        raw_id = str(item.get('itemId') or '').strip()
        if not raw_id.isdigit():
            continue
        item_id = ctroads_item_id(layer, source_layer, raw_id)
        if layer == 'Cameras':
            title = ''
            expando = {
                'videoEnabled': False,
                'videoId': item_id,
                'videoUrl': None,
                'snapshotUrl': f'/camera-snapshot/CT/{raw_id}',
            }
        elif layer == 'MessageSigns':
            title = 'CTroads message sign'
            expando = {'message': 'Open for the current sign message'}
        else:
            title = CTROADS_LAYER_LABELS.get(source_layer, 'CTroads traffic event')
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': title,
                'severity': title,
                'timestamp': None,
                'location': None,
            }
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def fetch_ctroads_tooltip_html(source_layer, raw_id):
    key = f'{source_layer}:{raw_id}'
    now = time.time()
    with CTROADS_TOOLTIP_CACHE_LOCK:
        cached = CTROADS_TOOLTIP_CACHE.get(key)
        if cached and now < cached['expires_at']:
            return cached['html']
    req = urllib.request.Request(
        f'https://www.ctroads.org/tooltip/{source_layer}/{raw_id}?lang=en',
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': CTROADS_SOURCE_URL,
            'Accept': 'text/html,application/xhtml+xml',
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    if raw[:2] == b'\x1f\x8b':
        import gzip
        raw = gzip.decompress(raw)
    text = raw.decode('utf-8', errors='replace')
    with CTROADS_TOOLTIP_CACHE_LOCK:
        if len(CTROADS_TOOLTIP_CACHE) >= 3000:
            CTROADS_TOOLTIP_CACHE.pop(next(iter(CTROADS_TOOLTIP_CACHE)))
        CTROADS_TOOLTIP_CACHE[key] = {
            'html': text,
            'expires_at': time.time() + CTROADS_TOOLTIP_CACHE_TTL,
        }
    return text


def ctroads_tooltip_detail(source_layer, raw_id):
    raw = fetch_ctroads_tooltip_html(source_layer, raw_id)
    common = _parse_511_tooltip_html(raw)
    heading_match = re.search(r'<h4[^>]*>(.*?)</h4>', raw, re.S | re.I)
    strong_match = re.search(r'<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>', raw, re.S | re.I)
    message_match = re.search(
        r'<td[^>]+class=["\'][^"\']*msgContent[^"\']*["\'][^>]*>(.*?)</td>',
        raw,
        re.S | re.I,
    )
    updated_match = re.search(
        r'<th[^>]*>\s*Last Updated\s*</th>\s*<td[^>]*>(.*?)</td>',
        raw,
        re.S | re.I,
    )
    dated_cells = re.findall(
        r'<td[^>]*>\s*([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4},\s+\d{1,2}:\d{2}\s+[AP]M)\s*</td>',
        raw,
        re.I,
    )
    descriptive_cells = []
    for value in re.findall(r'<td[^>]+colspan=["\']2["\'][^>]*>(.*?)</td>', raw, re.S | re.I):
        clean = pa511_html_text(value)
        if clean and 'shareSocialIcons' not in value and len(clean) > 2:
            descriptive_cells.append(clean)
    heading = pa511_html_text(heading_match.group(1)) if heading_match else ''
    strong = pa511_html_text(strong_match.group(1)) if strong_match else ''
    message = pa511_html_text(message_match.group(1)) if message_match else ''
    updated = pa511_html_text(updated_match.group(1)) if updated_match else (dated_cells[-1] if dated_cells else None)
    media_match = re.search(r'data-lazy=["\']/map/Cctv/(\d+)', raw, re.I)

    if source_layer == 'Cameras':
        name = strong or common.get('name') or f'CTDOT camera {raw_id}'
        detail_message = 'Live CTroads camera snapshot'
    elif source_layer == 'MessageSigns':
        name = strong or common.get('name') or 'CTroads message sign'
        detail_message = message or common.get('msg') or 'No message displayed'
    else:
        name = CTROADS_LAYER_LABELS.get(source_layer) or heading or 'CTroads traffic event'
        detail_message = ' · '.join(dict.fromkeys(descriptive_cells)) or common.get('msg') or heading

    detail = {
        'name': name,
        'msg': detail_message,
        'severity': CTROADS_LAYER_LABELS.get(source_layer) or common.get('severity'),
        'timestamp': updated or common.get('timestamp'),
        'location': strong if source_layer == 'ConstructionProjects' else None,
        'video_id': str(raw_id) if source_layer == 'Cameras' else None,
        'video_url': None,
        'video_enabled': False,
    }
    if source_layer == 'Cameras':
        media_id = media_match.group(1) if media_match else None
        detail.update({
            'snapshot_url': f'/camera-snapshot/CT/{raw_id}' if media_id else None,
            'upstream_snapshot_url': f'https://www.ctroads.org/map/Cctv/{media_id}' if media_id else None,
        })
    return detail


def ctroads_find_item(layer, item_id):
    target = str(item_id)
    for source_layer, item in ctroads_items_for_layer(layer):
        raw_id = str(item.get('itemId') or '').strip()
        if ctroads_item_id(layer, source_layer, raw_id) == target:
            return source_layer, raw_id
    raise ValueError(f'CTroads {layer} item {item_id} not found')


def ctroads_camera_detail(site_id):
    if not str(site_id).isdigit():
        raise ValueError('Invalid CTroads camera id')
    return ctroads_tooltip_detail('Cameras', str(site_id))


def ctroads_tooltip(layer, item_id):
    source_layer, raw_id = ctroads_find_item(layer, item_id)
    return ctroads_tooltip_detail(source_layer, raw_id)


def ridot_cached_items(cache_key, loader):
    now = time.time()
    with RIDOT_TRAFFIC_CACHE_LOCK:
        cached = RIDOT_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with RIDOT_TRAFFIC_CACHE_LOCK:
        RIDOT_TRAFFIC_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + RIDOT_CACHE_TTL,
        }
    return items


def fetch_ridot_gis_assets(layer_id):
    def load():
        params = urllib.parse.urlencode({
            'where': '1=1',
            'outFields': '*',
            'returnGeometry': 'true',
            'outSR': '4326',
            'f': 'json',
        })
        data = fetch_json_url(
            f'{RIDOT_GIS_ROOT}/{int(layer_id)}/query?{params}',
            headers={
                'Referer': RIDOT_SOURCE_URL,
                'Accept': 'application/json',
            },
            timeout=30,
        )
        features = data.get('features') if isinstance(data, dict) else None
        if not isinstance(features, list):
            raise ValueError(f'RIDOT GIS layer {layer_id} returned no features')
        return features

    return ridot_cached_items(f'gis:{int(layer_id)}', load)


def ridot_image_key(value):
    raw = html.unescape(str(value or '')).strip().replace('\u2019', "'")
    path = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
    name = os.path.basename(path or raw).strip().lower()
    return re.sub(r'\s+', ' ', name)


def ridot_camera_label_key(value):
    label = strip_tags(value).lower()
    label = re.sub(r'^camera\s+at\s+', '', label)
    return re.sub(r'[^a-z0-9]+', ' ', label).strip()


def ridot_public_snapshot_url(value):
    raw = html.unescape(str(value or '')).strip()
    if raw.startswith('http://'):
        raw = 'https://' + raw[len('http://'):]
    parsed = urllib.parse.urlparse(raw)
    if (
        parsed.scheme != 'https' or parsed.hostname != 'www.dot.ri.gov' or
        not parsed.path.startswith('/img/travel/camimages/')
    ):
        return ''
    encoded_path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe='/')
    return urllib.parse.urlunsplit(('https', 'www.dot.ri.gov', encoded_path, parsed.query, ''))


def fetch_ridot_camera_streams():
    def load():
        streams = {}
        for filename in RIDOT_CAMERA_PAGES:
            page_url = urllib.parse.urljoin(RIDOT_SOURCE_URL, filename)
            page = fetch_text_url(page_url, referer=RIDOT_SOURCE_URL, timeout=30)
            anchors = {}
            for match in re.finditer(
                r'<a[^>]+id="(cam\d+[a-z]?)"[^>]*>\s*'
                r'<img[^>]+src="([^"]+)"[^>]*alt="([^"]*)"',
                page,
                re.I | re.S,
            ):
                anchors[match.group(1)] = (match.group(2), strip_tags(match.group(3)))
            for script in re.findall(r'<script\b[^>]*>(.*?)</script>', page, re.I | re.S):
                camera_match = re.search(r'\b(cam\d+[a-z]?)\b', script, re.I)
                stream_match = re.search(
                    r'openVideoPopup2\(\s*["\'](https://[^"\']+\.m3u8(?:\?[^"\']*)?)["\']',
                    script,
                    re.I,
                )
                if not camera_match or not stream_match:
                    continue
                anchor = anchors.get(camera_match.group(1))
                if not anchor:
                    continue
                video_url = html.unescape(stream_match.group(1)).strip()
                parsed_video = urllib.parse.urlparse(video_url)
                if (
                    parsed_video.scheme != 'https' or
                    parsed_video.hostname != 'cdn3.wowza.com' or
                    not parsed_video.path.endswith('.m3u8')
                ):
                    continue
                stream = {
                    'video_url': video_url,
                    'label': anchor[1],
                }
                streams[ridot_image_key(anchor[0])] = stream
                label_key = ridot_camera_label_key(anchor[1])
                if label_key:
                    streams[f'label:{label_key}'] = stream
        if not streams:
            raise ValueError('RIDOT returned no public camera streams')
        return streams

    return ridot_cached_items('camera-streams', load)


def fetch_ridot_cameras():
    def load():
        stream_index = fetch_ridot_camera_streams()
        bounds = REGIONS['RI']['bounds']
        cameras = []
        for feature in fetch_ridot_gis_assets(2):
            attrs = feature.get('attributes') or {}
            geom = feature.get('geometry') or {}
            equipment_id = str(attrs.get('EquipmentID') or '').strip()
            lat = safe_float(geom.get('y') or attrs.get('Latitude'))
            lon = safe_float(geom.get('x') or attrs.get('Longitude'))
            if not equipment_id.isdigit() or attrs.get('Enabled') == 0 or not (
                bounds['min_lat'] <= lat <= bounds['max_lat'] and
                bounds['min_lon'] <= lon <= bounds['max_lon']
            ):
                continue
            description = strip_tags(attrs.get('Description')) or f'RIDOT camera {equipment_id}'
            stream = (
                stream_index.get(ridot_image_key(attrs.get('CCVEWebURL'))) or
                stream_index.get(ridot_image_key(description + '.jpg')) or
                stream_index.get(f'label:{ridot_camera_label_key(description)}') or
                {}
            )
            cameras.append({
                'id': equipment_id,
                'name': description,
                'lat': lat,
                'lon': lon,
                'direction': display_text(attrs.get('Direction')),
                'snapshot_url': ridot_public_snapshot_url(attrs.get('CCVEWebURL')),
                'video_url': stream.get('video_url') or '',
            })
        if not cameras:
            raise ValueError('RIDOT GIS returned no cameras')
        return cameras

    return ridot_cached_items('Cameras', load)


def ridot_dms_messages():
    def load():
        page = fetch_text_url(RIDOT_DMS_URL, referer=RIDOT_SOURCE_URL, timeout=30)
        rows = []
        for row_html in re.findall(r'<tr\b[^>]*>(.*?)</tr>', page, re.I | re.S):
            cells = [strip_tags(cell) for cell in re.findall(r'<td\b[^>]*>(.*?)</td>', row_html, re.I | re.S)]
            if len(cells) < 2 or not cells[0]:
                continue
            message_parts = [part for part in cells[1:] if part]
            rows.append({
                'location': cells[0],
                'message': ' · '.join(message_parts),
            })
        return {
            str(equipment_id): row
            for equipment_id, row in zip(RIDOT_DMS_TRAVEL_TIME_EQUIPMENT_IDS, rows)
        }

    return ridot_cached_items('dms-messages', load)


def fetch_ridot_signs():
    def load():
        messages = ridot_dms_messages()
        bounds = REGIONS['RI']['bounds']
        signs = []
        for feature in fetch_ridot_gis_assets(1):
            attrs = feature.get('attributes') or {}
            geom = feature.get('geometry') or {}
            equipment_id = str(attrs.get('EquipmentID') or '').strip()
            lat = safe_float(geom.get('y') or attrs.get('Latitude'))
            lon = safe_float(geom.get('x') or attrs.get('Longitude'))
            if not equipment_id.isdigit() or attrs.get('Enabled') == 0 or not (
                bounds['min_lat'] <= lat <= bounds['max_lat'] and
                bounds['min_lon'] <= lon <= bounds['max_lon']
            ):
                continue
            current = messages.get(equipment_id) or {}
            signs.append({
                'id': equipment_id,
                'name': strip_tags(attrs.get('Description')) or f'RIDOT sign {equipment_id}',
                'lat': lat,
                'lon': lon,
                'message': current.get('message') or None,
                'published_location': current.get('location') or None,
            })
        return signs

    return ridot_cached_items('MessageSigns', load)


def ridot_traffic_anchor(roadway, nearest_exit):
    roadway_text = str(roadway or '').upper()
    exit_text = str(nearest_exit or '').upper()
    route_match = re.search(r'\b(?:I|US|RI|RTE|ROUTE)?\s*-?\s*(\d{1,3})\b', roadway_text)
    if not route_match:
        return None
    route_number = route_match.group(1).lstrip('0') or '0'
    exit_match = re.search(r'\b(\d{1,3})([A-Z]?)\b', exit_text)
    direction_match = re.search(r'\b([NSEW])B\b', roadway_text)
    direction = direction_match.group(1) if direction_match else ''
    best = None
    for feature in fetch_ridot_gis_assets(2) + fetch_ridot_gis_assets(1):
        attrs = feature.get('attributes') or {}
        geom = feature.get('geometry') or {}
        description = str(attrs.get('Description') or '').upper()
        if not re.search(rf'(?<!\d)0*{re.escape(route_number)}(?!\d)', description):
            continue
        score = 5
        if exit_match:
            exit_number = exit_match.group(1).lstrip('0') or '0'
            suffix = exit_match.group(2)
            candidate_exit = re.search(r'\bEXIT\s+0*(\d{1,3})([A-Z]?)\b', description)
            if candidate_exit and (candidate_exit.group(1).lstrip('0') or '0') == exit_number:
                score += 10
                if suffix and candidate_exit.group(2) == suffix:
                    score += 3
        candidate_direction = str(attrs.get('Direction') or '').upper()
        if direction and (candidate_direction.startswith(direction) or f'_{direction}_' in description):
            score += 2
        lat = safe_float(geom.get('y') or attrs.get('Latitude'))
        lon = safe_float(geom.get('x') or attrs.get('Longitude'))
        if score > (best[0] if best else -1) and lat and lon:
            best = (score, (lat, lon))
    return best[1] if best and best[0] >= 5 else None


def ridot_incident_records():
    def load():
        page = fetch_text_url(RIDOT_INCIDENT_URL, referer=RIDOT_SOURCE_URL, timeout=30)
        records = []
        chunks = re.split(r'>\s*INCIDENT\s*</font', page, flags=re.I)
        wanted = {
            'type', 'time reported', 'city', 'travel lanes cleared',
            'roadway', 'affected lanes', 'nearest exit', 'comments',
        }
        for chunk in chunks[1:]:
            cells = [strip_tags(cell) for cell in re.findall(r'<td\b[^>]*>(.*?)</td>', chunk, re.I | re.S)]
            fields = {}
            for index, cell in enumerate(cells[:-1]):
                key = cell.lower().strip()
                if key in wanted and key not in fields:
                    fields[key] = cells[index + 1]
            if not fields.get('type') or not fields.get('roadway'):
                continue
            city = fields.get('city') or 'Rhode Island'
            location = ' · '.join(part for part in (
                fields.get('roadway'), fields.get('nearest exit'), city,
            ) if part)
            coords = ridot_traffic_anchor(fields.get('roadway'), fields.get('nearest exit'))
            if not coords:
                coords = geocode_location(f'{city}, Rhode Island')
            if not coords:
                continue
            raw_id = '|'.join((
                fields.get('type') or '', fields.get('time reported') or '', location,
            ))
            records.append({
                'id': mdchart_numeric_id(f'RIDOT:INCIDENT:{raw_id}'),
                'name': fields.get('type'),
                'lat': coords[0],
                'lon': coords[1],
                'description': fields.get('comments') or fields.get('affected lanes') or fields.get('type'),
                'severity': fields.get('affected lanes') or None,
                'timestamp': parse_time_iso(fields.get('time reported'), ['%m/%d/%Y %I:%M %p']),
                'location': location,
            })
        return records

    return ridot_cached_items('Incidents', load)


def ridot_advisory_records():
    def load():
        page = fetch_text_url(RIDOT_ADVISORY_URL, referer=RIDOT_SOURCE_URL, timeout=30)
        start = page.find('id="Interstate"')
        end = page.find('class="medium-2 large-2 columns"', start + 1)
        if start == -1:
            raise ValueError('RIDOT travel-advisory section was not found')
        section = page[start:end if end != -1 else None]
        pending = []
        seen = set()
        for block in re.findall(r'<(?:p|li)\b[^>]*>(.*?)</(?:p|li)>', section, re.I | re.S):
            text = strip_tags(block)
            lower = text.lower()
            if not (40 <= len(text) <= 900) or ':' not in text:
                continue
            if any(skip in lower for skip in (
                'back to top', 'see full list', 'additional lane closures',
                'no lane closures scheduled', 'all schedules are weather-dependent',
            )):
                continue
            if not any(token in lower for token in (
                'closure', 'closed', 'construction', 'bridge', 'road work',
                'lane ', 'paving', 'milling', 'inspection', 'traffic pattern',
            )):
                continue
            city_label, detail = [part.strip() for part in text.split(':', 1)]
            city_label = re.sub(r'\s+', ' ', city_label).strip(' -')
            primary_city = re.split(r'[/,]', city_label, 1)[0].strip()
            if not primary_city or len(primary_city) > 50:
                continue
            raw_id = mdchart_numeric_id(f'RIDOT:ADVISORY:{text}')
            if raw_id in seen:
                continue
            seen.add(raw_id)
            location_clause = re.split(
                r'\b(?:alternating|left|right|shoulder|lane|all travel lanes|traffic has shifted)\b',
                detail,
                maxsplit=1,
                flags=re.I,
            )[0].strip(' ,.;')
            query = f'{location_clause}, {primary_city}, Rhode Island'
            pending.append({
                'id': raw_id,
                'name': 'RIDOT travel advisory',
                'description': text,
                'severity': 'Roadwork / restriction',
                'location': f'{city_label}: {location_clause}' if location_clause else city_label,
                'query': query,
                'fallback_query': f'{primary_city}, Rhode Island',
            })
        geocodes = parallel_geocode_queries(item['query'] for item in pending)
        missing = [
            item['fallback_query'] for item in pending
            if not geocodes.get(normalized_cache_key(item['query']))
        ]
        fallback_geocodes = parallel_geocode_queries(missing)
        records = []
        for item in pending:
            coords = (
                geocodes.get(normalized_cache_key(item['query'])) or
                fallback_geocodes.get(normalized_cache_key(item['fallback_query']))
            )
            if not coords:
                continue
            item = dict(item)
            item.update({'lat': coords[0], 'lon': coords[1], 'timestamp': None})
            records.append(item)
        return records

    return ridot_cached_items('Construction', load)


def ridot_layer_payload(layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['id'],
                'videoUrl': camera['video_url'] or None,
                'snapshotUrl': f'/camera-snapshot/RI/{camera["id"]}' if camera['snapshot_url'] else None,
            },
        } for camera in fetch_ridot_cameras()]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': None},
        } for sign in fetch_ridot_signs()]}
    records = ridot_advisory_records() if layer == 'Construction' else ridot_incident_records()
    if layer not in {'Incidents', 'Construction'}:
        records = []
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
        },
    } for item in records]}


def ridot_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_ridot_cameras():
        if camera['id'] != target:
            continue
        return {
            'name': camera['name'],
            'msg': camera['direction'] or 'Rhode Island DOT traffic camera',
            'severity': None,
            'timestamp': None,
            'video_id': target,
            'video_url': camera['video_url'] or None,
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': f'/camera-snapshot/RI/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
        }
    raise ValueError(f'RIDOT camera {site_id} not found')


def ridot_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return ridot_camera_detail(target)
    if layer == 'MessageSigns':
        for sign in fetch_ridot_signs():
            if sign['id'] == target:
                return {
                    'name': sign['published_location'] or sign['name'],
                    'msg': sign['message'] or 'No active travel-time message is currently published.',
                    'severity': 'RIDOT message sign',
                    'timestamp': None,
                    'video_id': None,
                    'video_url': None,
                    'video_enabled': False,
                }
    records = ridot_advisory_records() if layer == 'Construction' else ridot_incident_records()
    for item in records:
        if item['id'] == target:
            return {
                'name': item['name'],
                'msg': item['description'],
                'severity': item.get('severity'),
                'timestamp': item.get('timestamp'),
                'location': item.get('location'),
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
    raise ValueError(f'RIDOT {layer} item {item_id} not found')


def mass511_cached_items(cache_key, loader):
    now = time.time()
    with MASS511_TRAFFIC_CACHE_LOCK:
        cached = MASS511_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with MASS511_TRAFFIC_CACHE_LOCK:
        MASS511_TRAFFIC_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + MASS511_CACHE_TTL,
        }
    return items


def mass511_headers():
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Origin': 'https://www.mass511.com',
        'Referer': MASS511_SOURCE_URL,
        'Accept': 'application/json',
    }


def fetch_mass511_cameras():
    def load():
        payload = fetch_json_url(MASS511_CAMERA_URL, headers=mass511_headers(), timeout=30)
        if not isinstance(payload, list):
            raise ValueError('Mass511 returned an unexpected camera response')
        bounds = REGIONS['MA']['bounds']
        cameras = []
        for item in payload:
            location = item.get('location') or {}
            lat = safe_float(location.get('latitude'))
            lon = safe_float(location.get('longitude'))
            camera_id = str(item.get('id') or '').strip()
            views = [view for view in (item.get('views') or []) if isinstance(view, dict)]
            view = next(
                (candidate for candidate in views if candidate.get('url') and candidate.get('videoPreviewUrl')),
                next((candidate for candidate in views if candidate.get('url') or candidate.get('videoPreviewUrl')), {}),
            )
            if not camera_id.isdigit() or item.get('public') is False or not (
                bounds['min_lat'] <= lat <= bounds['max_lat'] and
                bounds['min_lon'] <= lon <= bounds['max_lon']
            ):
                continue
            preview_url = str(view.get('videoPreviewUrl') or '').strip()
            preview = urllib.parse.urlparse(preview_url)
            if not (
                preview.scheme == 'https' and preview.hostname == 'public.carsprogram.org' and
                preview.path.startswith('/cameras/MA/') and preview.path.endswith(('.jpg', '.jpeg'))
            ):
                preview_url = ''
            stream_api_url = str(view.get('url') or '').strip()
            stream_api = urllib.parse.urlparse(stream_api_url)
            if not (
                stream_api.scheme == 'https' and stream_api.hostname == 'api.trafficland.com' and
                stream_api.path.startswith('/v2.2/json/stream/')
            ):
                stream_api_url = ''
            cameras.append({
                'id': camera_id,
                'name': display_text(item.get('name')) or f'MassDOT camera {camera_id}',
                'lat': lat,
                'lon': lon,
                'direction': display_text(view.get('name')),
                'route': display_text(location.get('routeId')),
                'city': display_text(location.get('cityReference')),
                'owner': display_text((item.get('cameraOwner') or {}).get('name')) or 'MassDOT',
                'updated_at': epoch_milliseconds_iso(item.get('lastUpdated')),
                'snapshot_url': preview_url,
                'stream_api_url': stream_api_url,
            })
        if not cameras:
            raise ValueError('Mass511 returned no public cameras')
        return cameras

    return mass511_cached_items('Cameras', load)


def fetch_mass511_signs():
    def load():
        payload = fetch_json_url(MASS511_SIGN_URL, headers=mass511_headers(), timeout=30)
        if not isinstance(payload, list):
            raise ValueError('Mass511 returned an unexpected sign response')
        bounds = REGIONS['MA']['bounds']
        signs = []
        for item in payload:
            if item.get('agencyId') != 'massachusettsSigns':
                continue
            location = item.get('location') or {}
            lat = safe_float(location.get('latitude'))
            lon = safe_float(location.get('longitude'))
            raw_id = str(item.get('id') or item.get('idForDisplay') or '').strip()
            if not raw_id or not (
                bounds['min_lat'] <= lat <= bounds['max_lat'] and
                bounds['min_lon'] <= lon <= bounds['max_lon']
            ):
                continue
            pages = []
            for page in ((item.get('display') or {}).get('pages') or []):
                lines = [' '.join(str(line or '').split()) for line in (page.get('lines') or [])]
                text = ' / '.join(line for line in lines if line)
                if text:
                    pages.append(text)
            signs.append({
                'id': wv511_numeric_id(f'MASS511:SIGN:{raw_id}'),
                'raw_id': raw_id,
                'name': display_text(location.get('locationDescription') or item.get('name')) or 'MassDOT sign',
                'lat': lat,
                'lon': lon,
                'message': ' · '.join(pages) or 'No current message published.',
                'status': display_text(item.get('status')),
                'updated_at': epoch_milliseconds_iso(item.get('lastUpdated')),
            })
        return signs

    return mass511_cached_items('MessageSigns', load)


def mass511_event_coordinates(item):
    for feature in item.get('features') or []:
        geometry = feature.get('geometry') or {}
        coordinates = geometry.get('coordinates')
        if geometry.get('type') == 'Point' and isinstance(coordinates, list) and len(coordinates) >= 2:
            lon = safe_float(coordinates[0])
            lat = safe_float(coordinates[1])
            if in_region(lat, lon):
                return lat, lon
    bbox = item.get('bbox') or []
    if isinstance(bbox, list) and len(bbox) >= 4:
        lon = (safe_float(bbox[0]) + safe_float(bbox[2])) / 2
        lat = (safe_float(bbox[1]) + safe_float(bbox[3])) / 2
        if in_region(lat, lon):
            return lat, lon
    return None


def fetch_mass511_events(layer):
    slug = 'roadReports' if layer == 'Incidents' else 'constructionReports'

    def load():
        payload = fetch_json_url(
            MASS511_GRAPHQL_URL,
            headers={**mass511_headers(), 'Content-Type': 'application/json'},
            data={'query': MASS511_SEARCH_QUERY, 'variables': {'slugs': [slug]}},
            timeout=30,
        )
        search = ((payload.get('data') or {}).get('searchBoundsQuery') or {})
        error = search.get('error') or {}
        if error:
            raise ValueError(f'Mass511 GraphQL error: {error.get("message") or error.get("type")}')
        records = []
        for item in search.get('results') or []:
            coordinates = mass511_event_coordinates(item)
            uri = str(item.get('uri') or '').strip()
            if not coordinates or not uri:
                continue
            title = display_text(item.get('title')) or (
                'MassDOT construction' if layer == 'Construction' else 'MassDOT traffic incident'
            )
            city = display_text(item.get('cityReference'))
            records.append({
                'id': wv511_numeric_id(f'MASS511:{layer}:{uri}'),
                'name': title,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'description': strip_tags(item.get('description')) or title,
                'severity': f'Priority {safe_int(item.get("priority"))}' if item.get('priority') is not None else None,
                'timestamp': None,
                'location': city or None,
            })
        return records

    return mass511_cached_items(layer, load)


def mass511_layer_payload(layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['stream_api_url']),
                'videoId': camera['id'],
                'videoUrl': None,
                'snapshotUrl': f'/camera-snapshot/MA/{camera["id"]}' if camera['snapshot_url'] else None,
                'snapshotFromVideo': bool(camera['stream_api_url'] and not camera['snapshot_url']),
            },
        } for camera in fetch_mass511_cameras()]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': sign['updated_at']},
        } for sign in fetch_mass511_signs()]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
        },
    } for item in fetch_mass511_events(layer)]}


def mass511_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_mass511_cameras():
        if camera['id'] != target:
            continue
        details = ' · '.join(part for part in (
            camera['owner'], camera['direction'], camera['route'], camera['city'],
        ) if part)
        return {
            'name': camera['name'],
            'msg': details or 'MassDOT traffic camera',
            'severity': None,
            'timestamp': camera['updated_at'],
            'video_id': target,
            'video_url': None,
            'video_enabled': bool(camera['stream_api_url']),
            'snapshot_url': f'/camera-snapshot/MA/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
            'stream_api_url': camera['stream_api_url'] or None,
        }
    raise ValueError(f'Mass511 camera {site_id} not found')


def mass511_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return mass511_camera_detail(target)
    if layer == 'MessageSigns':
        records = fetch_mass511_signs()
    elif layer in {'Incidents', 'Construction'}:
        records = fetch_mass511_events(layer)
    else:
        records = []
    for item in records:
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': item.get('status') or 'MassDOT message sign',
                'timestamp': item.get('updated_at'),
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'Mass511 {layer} item {item_id} not found')


def new_england_511_cached_items(cache_key, loader):
    now = time.time()
    with NEW_ENGLAND_511_TRAFFIC_CACHE_LOCK:
        cached = NEW_ENGLAND_511_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with NEW_ENGLAND_511_TRAFFIC_CACHE_LOCK:
        NEW_ENGLAND_511_TRAFFIC_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + NEW_ENGLAND_511_CACHE_TTL,
        }
    return items


def new_england_511_headers(*, accept='application/json'):
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': 'https://www.newengland511.org/map',
        'Accept': accept,
    }


def fetch_new_england_511_dataset(source_layer):
    def load():
        payload = fetch_json_url(
            f'https://www.newengland511.org/map/mapIcons/{source_layer}',
            headers=new_england_511_headers(),
            timeout=30,
        )
        items = payload.get('item2') if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError(f'New England 511 {source_layer} returned no item array')
        return items

    return new_england_511_cached_items(f'layer:{source_layer}', load)


def new_england_511_camera_query(state_name, start, length=100):
    columns = [
        {'name': 'sortOrder'},
        {'name': 'state', 's': True, 'search': {'value': state_name}},
        {'name': 'roadway', 's': True},
        {'name': 'location'},
        {'name': ''},
    ]
    query = {
        'columns': columns,
        'order': [{'column': 1, 'dir': 'asc'}, {'column': 0, 'dir': 'asc'}],
        'start': start,
        'length': length,
        'search': {'value': ''},
    }
    params = urllib.parse.urlencode({
        'query': json.dumps(query, separators=(',', ':')),
        'lang': 'en',
    })
    return fetch_json_url(
        f'https://www.newengland511.org/List/GetData/Cameras?{params}',
        headers={
            **new_england_511_headers(),
            'Referer': 'https://www.newengland511.org/cctv',
        },
        timeout=30,
    )


def new_england_511_point_wkt(value):
    match = re.search(
        r'POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)',
        str(value or ''),
        re.I,
    )
    if not match:
        return None
    return safe_float(match.group(2)), safe_float(match.group(1))


def fetch_new_england_511_cameras(state_code):
    state = traffic_region(state_code)
    if not state:
        return []

    def load():
        records = []
        start = 0
        expected = None
        while expected is None or start < expected:
            payload = new_england_511_camera_query(state['name'], start)
            page = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(page, list):
                raise ValueError('New England 511 returned an unexpected camera-list response')
            expected = safe_int(payload.get('recordsFiltered'))
            records.extend(page)
            if not page or len(page) < 100:
                break
            start += len(page)

        cameras = []
        for item in records:
            if str(item.get('state') or '').strip().casefold() != state['name'].casefold():
                continue
            site_id = str(item.get('id') or '').strip()
            coordinates = new_england_511_point_wkt(
                (((item.get('latLng') or {}).get('geography') or {}).get('wellKnownText'))
            )
            images = [image for image in (item.get('images') or []) if isinstance(image, dict)]
            image = next(
                (
                    candidate for candidate in images
                    if str(candidate.get('id') or '').isdigit() and
                    not candidate.get('disabled') and not candidate.get('blocked')
                ),
                {},
            )
            image_id = str(image.get('id') or '').strip()
            if not site_id.isdigit() or not image_id.isdigit() or not coordinates:
                continue
            lat, lon = coordinates
            bounds = state['bounds']
            if not (
                bounds['min_lat'] <= lat <= bounds['max_lat'] and
                bounds['min_lon'] <= lon <= bounds['max_lon']
            ):
                continue
            video_url = str(image.get('videoUrl') or '').strip()
            source = re.sub(
                r'(?<=[a-z])(?=[A-Z])',
                ' ',
                strip_tags(item.get('source')),
            ).strip()
            if source.replace(' ', '').casefold() == state['name'].replace(' ', '').casefold():
                source = NEW_ENGLAND_511_CAMERA_AGENCIES.get(
                    state['code'], f'{state["code"]}DOT'
                )
            cameras.append({
                'id': site_id,
                'image_id': image_id,
                'name': display_text(item.get('location')) or f'{state["name"]} DOT camera {site_id}',
                'lat': lat,
                'lon': lon,
                'roadway': strip_tags(item.get('roadway')),
                'direction': display_text(item.get('direction')),
                'city': display_text(item.get('city')),
                'source': source or f'{state["name"]} DOT',
                'video_url': video_url or None,
                'video_type': str(image.get('videoType') or '').strip() or None,
                'video_auth_required': bool(image.get('isVideoAuthRequired')),
                'updated_at': item.get('lastUpdated'),
            })
        if not cameras:
            raise ValueError(f'New England 511 returned no {state["name"]} cameras')
        return cameras

    return new_england_511_cached_items(f'cameras:{state["code"]}', load)


def new_england_511_item_id(layer, source_layer, raw_id):
    if layer == 'MessageSigns':
        return str(raw_id)
    return wv511_numeric_id(f'NEWENGLAND511:{source_layer}:{raw_id}')


def new_england_511_items_for_layer(state_code, layer):
    return [
        (source_layer, item)
        for source_layer in NEW_ENGLAND_511_LAYER_SOURCES.get(layer, ())
        for item in fetch_new_england_511_dataset(source_layer)
        if len(item.get('location') or []) >= 2 and point_in_state(
            state_code,
            safe_float(item['location'][0]),
            safe_float(item['location'][1]),
        )
    ]


def new_england_511_layer_payload(state_code, layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['image_id'],
                'videoUrl': camera['video_url'],
                'snapshotUrl': f'/camera-snapshot/{state_code}/{camera["id"]}',
            },
        } for camera in fetch_new_england_511_cameras(state_code)]}
    if layer not in NEW_ENGLAND_511_LAYER_SOURCES:
        return {'item2': []}

    normalized = []
    for source_layer, item in new_england_511_items_for_layer(state_code, layer):
        location = item.get('location') or []
        raw_id = str(item.get('itemId') or '').strip()
        if not raw_id.isdigit():
            continue
        label = NEW_ENGLAND_511_LAYER_LABELS.get(source_layer, 'New England 511 traffic event')
        if layer == 'MessageSigns':
            expando = {'message': 'Open for the current sign message', 'timestamp': None}
        else:
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': label,
                'severity': label,
                'timestamp': None,
                'location': None,
            }
        normalized.append({
            'itemId': new_england_511_item_id(layer, source_layer, raw_id),
            'location': [safe_float(location[0]), safe_float(location[1])],
            'title': label,
            'expando': expando,
        })
    return {'item2': normalized}


def fetch_new_england_511_tooltip_html(source_layer, raw_id):
    key = f'{source_layer}:{raw_id}'
    now = time.time()
    with NEW_ENGLAND_511_TOOLTIP_CACHE_LOCK:
        cached = NEW_ENGLAND_511_TOOLTIP_CACHE.get(key)
        if cached and now < cached['expires_at']:
            return cached['html']
    req = urllib.request.Request(
        f'https://www.newengland511.org/tooltip/{source_layer}/{raw_id}?lang=en',
        headers=new_england_511_headers(accept='text/html,application/xhtml+xml'),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode('utf-8', errors='replace')
    with NEW_ENGLAND_511_TOOLTIP_CACHE_LOCK:
        if len(NEW_ENGLAND_511_TOOLTIP_CACHE) >= 3000:
            NEW_ENGLAND_511_TOOLTIP_CACHE.pop(next(iter(NEW_ENGLAND_511_TOOLTIP_CACHE)))
        NEW_ENGLAND_511_TOOLTIP_CACHE[key] = {
            'html': text,
            'expires_at': time.time() + NEW_ENGLAND_511_TOOLTIP_CACHE_TTL,
        }
    return text


def new_england_511_tooltip_detail(source_layer, raw_id):
    raw = fetch_new_england_511_tooltip_html(source_layer, raw_id)
    common = _parse_511_tooltip_html(raw)
    heading_match = re.search(r'<h4[^>]*>(.*?)</h4>', raw, re.S | re.I)
    description_match = re.search(
        r'<td[^>]+colspan=["\']2["\'][^>]*>(.*?)</td>', raw, re.S | re.I
    )
    pairs = {
        pa511_html_text(label): pa511_html_text(value)
        for label, value in re.findall(
            r'<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', raw, re.S | re.I
        )
    }
    heading = pa511_html_text(heading_match.group(1)) if heading_match else ''
    description = pa511_html_text(description_match.group(1)) if description_match else ''
    label = NEW_ENGLAND_511_LAYER_LABELS.get(source_layer, heading or 'New England 511 event')
    if source_layer == 'MessageSigns':
        return {
            'name': common.get('name') or label,
            'msg': common.get('msg') or 'No message displayed',
            'severity': label,
            'timestamp': common.get('timestamp'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    return {
        'name': label,
        'msg': description or common.get('msg') or heading,
        'severity': label,
        'timestamp': pairs.get('Last Updated') or pairs.get('Start Time') or common.get('timestamp'),
        'location': None,
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def new_england_511_find_item(state_code, layer, item_id):
    target = str(item_id)
    for source_layer, item in new_england_511_items_for_layer(state_code, layer):
        raw_id = str(item.get('itemId') or '').strip()
        if new_england_511_item_id(layer, source_layer, raw_id) == target:
            return source_layer, raw_id
    raise ValueError(f'New England 511 {state_code} {layer} item {item_id} not found')


def new_england_511_camera_detail(state_code, site_id):
    target = str(site_id)
    for camera in fetch_new_england_511_cameras(state_code):
        if camera['id'] != target:
            continue
        details = ' · '.join(part for part in (
            camera['roadway'], camera['direction'], camera['city'], camera['source'],
        ) if part)
        return {
            'name': camera['name'],
            'msg': details or f'{traffic_region(state_code)["name"]} traffic camera',
            'severity': None,
            'timestamp': camera.get('updated_at'),
            'video_id': camera['image_id'],
            'video_url': camera['video_url'],
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': f'/camera-snapshot/{state_code}/{target}',
            'upstream_snapshot_url': (
                f'https://www.newengland511.org/map/Cctv/{camera["image_id"]}'
            ),
        }
    raise ValueError(f'New England 511 {state_code} camera {site_id} not found')


def new_england_511_tooltip(state_code, layer, item_id):
    if layer == 'Cameras':
        return new_england_511_camera_detail(state_code, item_id)
    source_layer, raw_id = new_england_511_find_item(state_code, layer, item_id)
    return new_england_511_tooltip_detail(source_layer, raw_id)


def ny511_cached_items(cache_key, loader):
    now = time.time()
    with NY511_TRAFFIC_CACHE_LOCK:
        cached = NY511_TRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with NY511_TRAFFIC_CACHE_LOCK:
        NY511_TRAFFIC_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + NY511_CACHE_TTL,
        }
    return items


def ny511_headers(*, accept='application/json'):
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': 'https://www.511ny.org/map',
        'Accept': accept,
    }


def fetch_ny511_dataset(source_layer):
    def load():
        payload = fetch_json_url(
            f'https://www.511ny.org/map/mapIcons/{source_layer}',
            headers=ny511_headers(),
            timeout=30,
        )
        items = payload.get('item2') if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError(f'511NY {source_layer} returned no item array')
        return items

    return ny511_cached_items(f'layer:{source_layer}', load)


def ny511_camera_query(start, length=100):
    columns = [
        {'name': 'sortOrder'},
        {'name': 'state', 's': True, 'search': {'value': 'New York'}},
        {'name': 'roadway', 's': True},
        {'name': 'location'},
        {'name': ''},
    ]
    query = {
        'columns': columns,
        'order': [{'column': 1, 'dir': 'asc'}, {'column': 0, 'dir': 'asc'}],
        'start': start,
        'length': length,
        'search': {'value': ''},
    }
    params = urllib.parse.urlencode({
        'query': json.dumps(query, separators=(',', ':')),
        'lang': 'en',
    })
    return fetch_json_url(
        f'https://www.511ny.org/List/GetData/Cameras?{params}',
        headers={**ny511_headers(), 'Referer': 'https://www.511ny.org/cctv'},
        timeout=30,
    )


def fetch_ny511_cameras():
    def load():
        records = []
        start = 0
        expected = None
        while expected is None or start < expected:
            payload = ny511_camera_query(start)
            page = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(page, list):
                raise ValueError('511NY returned an unexpected camera-list response')
            expected = safe_int(payload.get('recordsFiltered'))
            records.extend(page)
            if not page or len(page) < 100:
                break
            start += len(page)

        cameras = []
        state = REGIONS['NY']
        for item in records:
            if str(item.get('state') or '').strip().casefold() != 'new york':
                continue
            site_id = str(item.get('id') or '').strip()
            coordinates = new_england_511_point_wkt(
                (((item.get('latLng') or {}).get('geography') or {}).get('wellKnownText'))
            )
            images = [image for image in (item.get('images') or []) if isinstance(image, dict)]
            image = next(
                (
                    candidate for candidate in images
                    if str(candidate.get('id') or '').isdigit() and
                    not candidate.get('disabled') and not candidate.get('blocked')
                ),
                {},
            )
            image_id = str(image.get('id') or '').strip()
            if not site_id.isdigit() or not image_id.isdigit() or not coordinates:
                continue
            lat, lon = coordinates
            bounds = state['bounds']
            if not (
                bounds['min_lat'] <= lat <= bounds['max_lat'] and
                bounds['min_lon'] <= lon <= bounds['max_lon']
            ):
                continue
            video_url = str(image.get('videoUrl') or '').strip()
            source = display_text(item.get('type')) or display_text(item.get('source'))
            cameras.append({
                'id': site_id,
                'image_id': image_id,
                'name': display_text(item.get('location')) or f'New York DOT camera {site_id}',
                'lat': lat,
                'lon': lon,
                'roadway': strip_tags(item.get('roadway')),
                'direction': display_text(item.get('direction')),
                'county': display_text(item.get('county')),
                'region': display_text(item.get('region')),
                'source': source or 'NYSDOT',
                'video_url': video_url or None,
                'video_type': str(image.get('videoType') or '').strip() or None,
                'video_auth_required': bool(image.get('isVideoAuthRequired')),
                'updated_at': item.get('lastUpdated'),
            })
        if not cameras:
            raise ValueError('511NY returned no New York cameras')
        return cameras

    return ny511_cached_items('cameras:NY', load)


def ny511_item_id(layer, source_layer, raw_id):
    if layer == 'MessageSigns':
        return str(raw_id)
    return wv511_numeric_id(f'NY511:{source_layer}:{raw_id}')


def ny511_items_for_layer(layer):
    return [
        (source_layer, item)
        for source_layer in NY511_LAYER_SOURCES.get(layer, ())
        for item in fetch_ny511_dataset(source_layer)
        if len(item.get('location') or []) >= 2 and point_in_state(
            'NY',
            safe_float(item['location'][0]),
            safe_float(item['location'][1]),
        )
    ]


def ny511_layer_payload(layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['image_id'],
                'videoUrl': camera['video_url'],
                'snapshotUrl': f'/camera-snapshot/NY/{camera["id"]}',
            },
        } for camera in fetch_ny511_cameras()]}
    if layer not in NY511_LAYER_SOURCES:
        return {'item2': []}

    normalized = []
    for source_layer, item in ny511_items_for_layer(layer):
        location = item.get('location') or []
        raw_id = str(item.get('itemId') or '').strip()
        if not raw_id.isdigit():
            continue
        label = NY511_LAYER_LABELS.get(source_layer, '511NY traffic event')
        if layer == 'MessageSigns':
            expando = {'message': 'Open for the current sign message', 'timestamp': None}
        else:
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': label,
                'severity': label,
                'timestamp': None,
                'location': None,
            }
        normalized.append({
            'itemId': ny511_item_id(layer, source_layer, raw_id),
            'location': [safe_float(location[0]), safe_float(location[1])],
            'title': label,
            'expando': expando,
        })
    return {'item2': normalized}


def fetch_ny511_tooltip_html(source_layer, raw_id):
    key = f'{source_layer}:{raw_id}'
    now = time.time()
    with NY511_TOOLTIP_CACHE_LOCK:
        cached = NY511_TOOLTIP_CACHE.get(key)
        if cached and now < cached['expires_at']:
            return cached['html']
    req = urllib.request.Request(
        f'https://www.511ny.org/tooltip/{source_layer}/{raw_id}?lang=en',
        headers=ny511_headers(accept='text/html,application/xhtml+xml'),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode('utf-8', errors='replace')
    with NY511_TOOLTIP_CACHE_LOCK:
        if len(NY511_TOOLTIP_CACHE) >= 3000:
            NY511_TOOLTIP_CACHE.pop(next(iter(NY511_TOOLTIP_CACHE)))
        NY511_TOOLTIP_CACHE[key] = {
            'html': text,
            'expires_at': time.time() + NY511_TOOLTIP_CACHE_TTL,
        }
    return text


def ny511_tooltip_detail(source_layer, raw_id):
    raw = fetch_ny511_tooltip_html(source_layer, raw_id)
    common = _parse_511_tooltip_html(raw)
    heading_match = re.search(r'<h4[^>]*>(.*?)</h4>', raw, re.S | re.I)
    description_match = re.search(
        r'<td[^>]+colspan=["\']2["\'][^>]*>(.*?)</td>', raw, re.S | re.I
    )
    pairs = {
        pa511_html_text(label): pa511_html_text(value)
        for label, value in re.findall(
            r'<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', raw, re.S | re.I
        )
    }
    heading = pa511_html_text(heading_match.group(1)) if heading_match else ''
    description = pa511_html_text(description_match.group(1)) if description_match else ''
    label = NY511_LAYER_LABELS.get(source_layer, heading or '511NY event')
    if source_layer == 'MessageSigns':
        return {
            'name': common.get('name') or label,
            'msg': common.get('msg') or 'No message displayed',
            'severity': label,
            'timestamp': common.get('timestamp'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    return {
        'name': label,
        'msg': description or common.get('msg') or heading,
        'severity': label,
        'timestamp': pairs.get('Last Updated') or pairs.get('Start Time') or common.get('timestamp'),
        'location': None,
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def ny511_find_item(layer, item_id):
    target = str(item_id)
    for source_layer, item in ny511_items_for_layer(layer):
        raw_id = str(item.get('itemId') or '').strip()
        if ny511_item_id(layer, source_layer, raw_id) == target:
            return source_layer, raw_id
    raise ValueError(f'511NY {layer} item {item_id} not found')


def ny511_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_ny511_cameras():
        if camera['id'] != target:
            continue
        details = ' · '.join(part for part in (
            camera['roadway'], camera['direction'], camera['county'],
            camera['region'], camera['source'],
        ) if part)
        return {
            'name': camera['name'],
            'msg': details or 'New York traffic camera',
            'severity': None,
            'timestamp': camera.get('updated_at'),
            'video_id': camera['image_id'],
            'video_url': camera['video_url'],
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': f'/camera-snapshot/NY/{target}',
            'upstream_snapshot_url': f'https://www.511ny.org/map/Cctv/{camera["image_id"]}',
        }
    raise ValueError(f'511NY camera {site_id} not found')


def ny511_tooltip(layer, item_id):
    if layer == 'Cameras':
        return ny511_camera_detail(item_id)
    source_layer, raw_id = ny511_find_item(layer, item_id)
    return ny511_tooltip_detail(source_layer, raw_id)


def ohgo_headers():
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/json',
        'Referer': OHGO_SOURCE_URL,
    }


def fetch_ohgo_dataset(resource):
    now = time.time()
    with OHGO_TRAFFIC_CACHE_LOCK:
        cached = OHGO_TRAFFIC_CACHE.get(resource)
        if cached and now < cached['expires_at']:
            return cached['data']
    data = fetch_json_url(
        f'{OHGO_API_ROOT}/{resource}',
        headers=ohgo_headers(),
        timeout=35,
    )
    if not isinstance(data, list):
        raise ValueError(f'OHGO {resource} returned an invalid payload')
    with OHGO_TRAFFIC_CACHE_LOCK:
        OHGO_TRAFFIC_CACHE[resource] = {
            'data': data,
            'expires_at': time.time() + OHGO_CACHE_TTL,
        }
    return data


def ohgo_numeric_id(layer, raw_id):
    return wv511_numeric_id(f'OHGO:{layer}:{raw_id}')


def ohgo_item_coordinates(item):
    lat = optional_float(item.get('Latitude'))
    lon = optional_float(item.get('Longitude'))
    if lat is None or lon is None or not point_in_state('OH', lat, lon):
        return None
    return lat, lon


def ohgo_items_for_layer(layer):
    resource = OHGO_LAYER_RESOURCES.get(layer)
    if not resource:
        return []
    return [
        item for item in fetch_ohgo_dataset(resource)
        if ohgo_item_coordinates(item)
    ]


def ohgo_camera_image_url(item):
    for camera_view in item.get('Cameras') or []:
        for key in ('LargeURL', 'SmallURL'):
            candidate = str(camera_view.get(key) or '').strip()
            parsed = urllib.parse.urlparse(candidate)
            hostname = str(parsed.hostname or '').lower()
            if (
                parsed.scheme == 'https' and
                any(hostname == suffix or hostname.endswith(f'.{suffix}')
                    for suffix in OHGO_CAMERA_HOST_SUFFIXES)
            ):
                return candidate
    return None


def ohgo_camera_detail(site_id):
    target = str(site_id)
    for item in ohgo_items_for_layer('Cameras'):
        raw_id = str(item.get('Id') or '').strip()
        if ohgo_numeric_id('Cameras', raw_id) != target:
            continue
        views = [
            display_text(view.get('Direction'))
            for view in item.get('Cameras') or []
            if display_text(view.get('Direction'))
        ]
        name = display_text(item.get('Location') or item.get('Description'))
        details = ' · '.join(part for part in (
            display_text(item.get('Description')) if display_text(item.get('Description')) != name else '',
            ', '.join(dict.fromkeys(views)),
            display_text(item.get('Provider')),
        ) if part)
        upstream_snapshot_url = ohgo_camera_image_url(item)
        return {
            'name': name or f'Ohio DOT camera {raw_id}',
            'msg': details or 'Ohio DOT OHGO traffic camera',
            'severity': None,
            'timestamp': None,
            'video_id': target,
            'video_url': None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/OH/{target}' if upstream_snapshot_url else None,
            'upstream_snapshot_url': upstream_snapshot_url,
        }
    raise ValueError(f'OHGO camera {site_id} not found')


def ohgo_layer_payload(layer):
    if layer not in OHGO_LAYER_RESOURCES:
        return {'item2': []}
    if layer == 'Cameras':
        normalized = []
        for item in ohgo_items_for_layer(layer):
            raw_id = str(item.get('Id') or '').strip()
            location = ohgo_item_coordinates(item)
            if not raw_id or not location:
                continue
            item_id = ohgo_numeric_id(layer, raw_id)
            snapshot_url = ohgo_camera_image_url(item)
            normalized.append({
                'itemId': item_id,
                'location': [location[0], location[1]],
                'title': display_text(item.get('Location') or item.get('Description')) or 'Ohio DOT camera',
                'expando': {
                    'videoEnabled': False,
                    'videoId': item_id,
                    'snapshotUrl': f'/camera-snapshot/OH/{item_id}' if snapshot_url else None,
                },
            })
        return {'item2': normalized}

    normalized = []
    for item in ohgo_items_for_layer(layer):
        raw_id = str(item.get('Id') or '').strip()
        location = ohgo_item_coordinates(item)
        if not raw_id or not location:
            continue
        item_id = ohgo_numeric_id(layer, raw_id)
        if layer == 'MessageSigns':
            messages = [display_text(message) for message in item.get('Messages') or []]
            message = '\n\n'.join(value for value in messages if value)
            expando = {'message': message or 'No active message', 'timestamp': None}
        else:
            description = display_text(item.get('Description'))
            category = display_text(item.get('Category'))
            status = display_text(item.get('RoadStatus') or item.get('Status'))
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': description,
                'severity': ' · '.join(part for part in (category, status) if part),
                'timestamp': item.get('StartDate') or item.get('EndDate'),
                'location': display_text(item.get('Location')),
            }
        normalized.append({
            'itemId': item_id,
            'location': [location[0], location[1]],
            'title': display_text(item.get('Location') or item.get('Description')) or 'OHGO traffic item',
            'expando': expando,
        })
    return {'item2': normalized}


def ohgo_find_item(layer, item_id):
    target = str(item_id)
    for item in ohgo_items_for_layer(layer):
        raw_id = str(item.get('Id') or '').strip()
        if ohgo_numeric_id(layer, raw_id) == target:
            return item
    raise ValueError(f'OHGO {layer} item {item_id} not found')


def ohgo_tooltip(layer, item_id):
    if layer == 'Cameras':
        return ohgo_camera_detail(item_id)
    item = ohgo_find_item(layer, item_id)
    if layer == 'MessageSigns':
        messages = [display_text(message) for message in item.get('Messages') or []]
        return {
            'name': display_text(item.get('Location') or item.get('Description')) or 'Ohio message sign',
            'msg': '\n\n'.join(value for value in messages if value) or 'No active message',
            'severity': display_text(item.get('SignTypeName')) or 'Message sign',
            'timestamp': None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    category = display_text(item.get('Category'))
    status = display_text(item.get('RoadStatus') or item.get('Status'))
    dates = ' · '.join(part for part in (
        f'Start {item.get("StartDate")}' if item.get('StartDate') else '',
        f'End {item.get("EndDate")}' if item.get('EndDate') else '',
    ) if part)
    return {
        'name': display_text(item.get('Location')) or category or 'OHGO traffic item',
        'msg': display_text(item.get('Description')),
        'severity': ' · '.join(part for part in (category, status) if part),
        'timestamp': dates or None,
        'location': display_text(item.get('Location')),
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def fetch_ohgo_road_weather():
    features = []
    for station in fetch_ohgo_dataset('weather-sensors'):
        location = ohgo_item_coordinates(station)
        if not location:
            continue
        lat, lon = location
        raw_id = str(station.get('Id') or '').strip()
        atmospheric = next((item for item in station.get('AtmosphericSensors') or [] if isinstance(item, dict)), {})
        surface = next((item for item in station.get('SurfaceSensors') or [] if isinstance(item, dict)), {})
        wind_mph = optional_float(atmospheric.get('AverageWindSpeed'))
        gust_mph = optional_float(atmospheric.get('MaximumWindSpeed'))
        visibility = optional_float(atmospheric.get('Visibility'))
        road_state = display_text(surface.get('Status'))
        if not road_state:
            precipitation = display_text(atmospheric.get('Precipitation'))
            road_state = (
                precipitation
                if precipitation.lower() not in {'', 'none', 'other', 'unknown'}
                else 'Unknown'
            )
        attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': f'OHGO_{raw_id}',
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': display_text(station.get('Location')) or f'OHGO weather station {raw_id}',
            'OBS_TIME_LOCAL': station.get('LastUpdate') or atmospheric.get('Updated') or surface.get('Updated'),
            'AIR_TEMP_F': optional_float(atmospheric.get('AirTemperature') or station.get('DegreesFahrenheit')),
            'DEW_POINT_F': optional_float(atmospheric.get('DewpointTemperature')),
            'RELATIVE_HUMIDITY': optional_float(atmospheric.get('Humidity')),
            'WIND_DIRECTION': display_text(atmospheric.get('WindDirection')),
            'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
            'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
            'VISIBILITY': visibility if visibility is not None and visibility >= 0 else None,
            'ROAD_TEMP_F': optional_float(surface.get('SurfaceTemperature')),
            'SUBSURFACE_TEMP_F': optional_float(surface.get('SubSurfaceTemperature')),
            'ROAD_STATE': road_state,
            'PRECIP_1H_IN': optional_float(atmospheric.get('PrecipitationAccumulation')),
            'PRECIP_12H_IN': None,
            'PRECIP_24H_IN': None,
            'SOURCE_URL': OHGO_SOURCE_URL,
        }
        features.append({'attributes': attrs, 'geometry': {'x': lon, 'y': lat}})
    return features


def indot_trafficwise_headers():
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Origin': 'https://511in.org',
        'Referer': INDOT_TRAFFICWISE_SOURCE_URL,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }


def indot_trafficwise_cached_items(cache_key, loader):
    now = time.time()
    with INDOT_TRAFFICWISE_CACHE_LOCK:
        cached = INDOT_TRAFFICWISE_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with INDOT_TRAFFICWISE_CACHE_LOCK:
        INDOT_TRAFFICWISE_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + INDOT_TRAFFICWISE_CACHE_TTL,
        }
    return items


def indot_trafficwise_search(query, slugs):
    payload = fetch_json_url(
        INDOT_TRAFFICWISE_GRAPHQL_URL,
        headers=indot_trafficwise_headers(),
        data={'query': query, 'variables': {'slugs': slugs}},
        timeout=35,
    )
    search = ((payload.get('data') or {}).get('searchBoundsQuery') or {})
    error = search.get('error') or {}
    if error:
        raise ValueError(
            f'INDOT TrafficWise GraphQL error: {error.get("message") or error.get("type")}'
        )
    return search


def indot_trafficwise_bbox_coordinates(item):
    bbox = item.get('bbox') or []
    if len(bbox) < 4:
        return None
    lon = (safe_float(bbox[0]) + safe_float(bbox[2])) / 2
    lat = (safe_float(bbox[1]) + safe_float(bbox[3])) / 2
    return (lat, lon) if point_in_state('IN', lat, lon) else None


def indot_trafficwise_event_coordinates(item):
    for feature in item.get('features') or []:
        geometry = feature.get('geometry') or {}
        coordinates = geometry.get('coordinates') or []
        if geometry.get('type') == 'Point' and len(coordinates) >= 2:
            lon = safe_float(coordinates[0])
            lat = safe_float(coordinates[1])
            if point_in_state('IN', lat, lon):
                return lat, lon
    return indot_trafficwise_bbox_coordinates(item)


def fetch_indot_trafficwise_cameras():
    def load():
        search = indot_trafficwise_search(
            INDOT_TRAFFICWISE_CAMERA_QUERY, ['normalCameras', 'hotCameras']
        )
        cameras = {}
        for item in search.get('cameraViews') or []:
            parent = item.get('parentCollection') or {}
            parent_uri = str(parent.get('uri') or '').strip()
            raw_id = parent_uri.rsplit('/', 1)[-1]
            coordinates = indot_trafficwise_bbox_coordinates(parent)
            if not raw_id.isdigit() or not coordinates:
                continue

            snapshot_url = str(item.get('url') or '').strip()
            snapshot = urllib.parse.urlparse(snapshot_url)
            if not (
                snapshot.scheme == 'https' and
                snapshot.hostname == 'public.carsprogram.org' and
                snapshot.path.startswith('/cameras/IN/') and
                snapshot.path.lower().endswith(('.png', '.jpg', '.jpeg'))
            ):
                snapshot_url = ''

            video_url = ''
            for source in item.get('sources') or []:
                candidate = str(source.get('src') or '').strip()
                parsed = urllib.parse.urlparse(candidate)
                hostname = str(parsed.hostname or '').lower()
                if (
                    source.get('type') == 'application/x-mpegURL' and
                    parsed.scheme == 'https' and
                    (hostname == 'trafficwise.org' or hostname.endswith('.trafficwise.org')) and
                    parsed.path.lower().endswith('.m3u8')
                ):
                    video_url = candidate
                    break

            lat, lon = coordinates
            camera = {
                'id': raw_id,
                'uri': parent_uri,
                'name': display_text(item.get('title')) or f'INDOT camera {raw_id}',
                'lat': lat,
                'lon': lon,
                'category': display_text(item.get('category')),
                'snapshot_url': snapshot_url,
                'video_url': video_url,
            }
            previous = cameras.get(raw_id)
            if not previous or (video_url and not previous.get('video_url')):
                cameras[raw_id] = camera
        if not cameras:
            raise ValueError('INDOT TrafficWise returned no public cameras')
        return list(cameras.values())

    return indot_trafficwise_cached_items('Cameras', load)


def indot_trafficwise_sign_message(item):
    messages = []
    for view in item.get('views') or []:
        lines = [display_text(line) for line in view.get('textLines') or [] if display_text(line)]
        if lines:
            messages.append(' / '.join(lines))
            continue
        travel_times = view.get('travelTimes')
        if isinstance(travel_times, dict):
            values = [f'{display_text(key)} {display_text(value)}'.strip() for key, value in travel_times.items()]
            if values:
                messages.append(' / '.join(values))
        elif isinstance(travel_times, list):
            values = [display_text(value) for value in travel_times if display_text(value)]
            if values:
                messages.append(' / '.join(values))
    return ' · '.join(dict.fromkeys(messages)) or 'No active message'


def fetch_indot_trafficwise_signs():
    def load():
        search = indot_trafficwise_search(
            INDOT_TRAFFICWISE_SIGN_QUERY, ['electronicSigns', 'electronicSignsInactive']
        )
        signs = []
        for item in search.get('results') or []:
            uri = str(item.get('uri') or '').strip()
            coordinates = indot_trafficwise_bbox_coordinates(item)
            if not uri or not coordinates:
                continue
            signs.append({
                'id': wv511_numeric_id(f'INDOT:SIGN:{uri}'),
                'uri': uri,
                'name': display_text(item.get('title')) or 'INDOT message sign',
                'lat': coordinates[0],
                'lon': coordinates[1],
                'message': indot_trafficwise_sign_message(item),
                'status': display_text(item.get('signStatus')),
                'display_type': display_text(item.get('signDisplayType')),
            })
        return signs

    return indot_trafficwise_cached_items('MessageSigns', load)


def fetch_indot_trafficwise_events(layer):
    slug = 'incidents' if layer == 'Incidents' else 'construction'

    def load():
        search = indot_trafficwise_search(INDOT_TRAFFICWISE_EVENT_QUERY, [slug])
        records = []
        for item in search.get('results') or []:
            uri = str(item.get('uri') or '').strip()
            coordinates = indot_trafficwise_event_coordinates(item)
            if not uri or not coordinates:
                continue
            title = display_text(item.get('title')) or (
                'INDOT construction' if layer == 'Construction' else 'INDOT traffic incident'
            )
            records.append({
                'id': wv511_numeric_id(f'INDOT:{layer}:{uri}'),
                'uri': uri,
                'name': title,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'description': strip_tags(item.get('description')) or title,
                'severity': (
                    f'Priority {safe_int(item.get("priority"))}'
                    if item.get('priority') is not None else None
                ),
                'location': display_text(item.get('cityReference')) or None,
                'timestamp': None,
            })
        return records

    return indot_trafficwise_cached_items(layer, load)


def indot_trafficwise_layer_payload(layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                # TrafficWise currently publishes HLS playlist URLs that return
                # 404 upstream. Keep the URLs available to the adapter, but do
                # not present a live button until INDOT restores the playlists.
                'videoEnabled': False,
                'videoId': camera['id'],
                'videoUrl': None,
                'snapshotUrl': f'/camera-snapshot/IN/{camera["id"]}' if camera['snapshot_url'] else None,
                'snapshotFromVideo': False,
            },
        } for camera in fetch_indot_trafficwise_cameras()]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': None},
        } for sign in fetch_indot_trafficwise_signs()]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
        },
    } for item in fetch_indot_trafficwise_events(layer)]}


def indot_trafficwise_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_indot_trafficwise_cameras():
        if camera['id'] != target:
            continue
        return {
            'name': camera['name'],
            'msg': camera['category'] or 'INDOT TrafficWise camera',
            'severity': None,
            'timestamp': None,
            'video_id': target,
            'video_url': camera['video_url'] or None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/IN/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
        }
    raise ValueError(f'INDOT TrafficWise camera {site_id} not found')


def indot_trafficwise_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return indot_trafficwise_camera_detail(target)
    records = (
        fetch_indot_trafficwise_signs()
        if layer == 'MessageSigns'
        else fetch_indot_trafficwise_events(layer) if layer in {'Incidents', 'Construction'} else []
    )
    for item in records:
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': ' · '.join(part for part in (item['display_type'], item['status']) if part),
                'timestamp': None,
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'INDOT TrafficWise {layer} item {item_id} not found')


def indot_trafficwise_measurement(fields, key):
    value = ((fields.get(key) or {}).get('displayValue'))
    match = re.search(r'-?\d+(?:\.\d+)?', str(value or ''))
    return optional_float(match.group(0)) if match else None


def fetch_indot_trafficwise_road_weather():
    def load():
        search = indot_trafficwise_search(
            INDOT_TRAFFICWISE_WEATHER_QUERY, ['stationsNormal', 'stationsAlert']
        )
        features = []
        seen = set()
        for station in search.get('results') or []:
            uri = str(station.get('uri') or '').strip()
            coordinates = indot_trafficwise_bbox_coordinates(station)
            if not uri or uri in seen or not coordinates:
                continue
            seen.add(uri)
            fields = station.get('weatherStationFields') or {}
            raw_id = uri.rsplit('/', 1)[-1]
            road_condition = display_text(
                ((fields.get('IN_ROAD_CONDITION_APPROACH') or {}).get('displayValue'))
            )
            if road_condition.lower() in {'', 'undefined', 'no report'}:
                conditions = [
                    display_text(item.get('title') or item.get('description'))
                    for item in station.get('drivingConditions') or []
                ]
                road_condition = next((value for value in conditions if value), 'Unknown')
            wind_mph = indot_trafficwise_measurement(fields, 'IN_WIND_AVG_SPEED_APPROACH')
            gust_mph = indot_trafficwise_measurement(fields, 'IN_WIND_MAX_SPEED_APPROACH')
            lat, lon = coordinates
            attrs = {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'INDOT_{raw_id}',
                'LATITUDE': lat,
                'LNGITUDE': lon,
                'LOCALNAM': display_text(station.get('title')) or f'INDOT weather station {raw_id}',
                'OBS_TIME_LOCAL': epoch_milliseconds_iso((station.get('lastUpdated') or {}).get('timestamp')),
                'AIR_TEMP_F': indot_trafficwise_measurement(fields, 'TEMP_AIR_TEMPERATURE'),
                'DEW_POINT_F': indot_trafficwise_measurement(fields, 'TEMP_DEW_POINT'),
                'RELATIVE_HUMIDITY': indot_trafficwise_measurement(fields, 'TEMP_RELATIVE_HUMIDITY'),
                'WIND_DIRECTION': display_text(
                    ((fields.get('IN_WIND_AVG_DIRECTION_APPROACH') or {}).get('displayValue'))
                ),
                'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
                'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
                'VISIBILITY': indot_trafficwise_measurement(fields, 'VISIBILITY'),
                'ROAD_TEMP_F': indot_trafficwise_measurement(fields, 'IN_SURFACE_TEMPERATURE_APPROACH'),
                'SUBSURFACE_TEMP_F': indot_trafficwise_measurement(fields, 'IN_PAVEMENT_TEMPERATURE_APPROACH'),
                'ROAD_STATE': road_condition,
                'PRECIP_1H_IN': None,
                'PRECIP_12H_IN': None,
                'PRECIP_24H_IN': None,
                'SOURCE_URL': INDOT_TRAFFICWISE_SOURCE_URL,
            }
            features.append({'attributes': attrs, 'geometry': {'x': lon, 'y': lat}})
        if not features:
            raise ValueError('INDOT TrafficWise returned no road-weather stations')
        return features

    return indot_trafficwise_cached_items('RoadWeather', load)


def mndot_cars_headers():
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Origin': 'https://511mn.org',
        'Referer': MNDOT_CARS_SOURCE_URL,
        'Accept': 'application/json',
    }


def mndot_cars_cached_items(cache_key, loader):
    now = time.time()
    with MNDOT_CARS_CACHE_LOCK:
        cached = MNDOT_CARS_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with MNDOT_CARS_CACHE_LOCK:
        MNDOT_CARS_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + MNDOT_CARS_CACHE_TTL,
        }
    return items


def mndot_cars_bbox_coordinates(item):
    bbox = item.get('bbox') or []
    if len(bbox) < 4:
        return None
    lon = (safe_float(bbox[0]) + safe_float(bbox[2])) / 2
    lat = (safe_float(bbox[1]) + safe_float(bbox[3])) / 2
    return (lat, lon) if point_in_state('MN', lat, lon) else None


def fetch_mndot_cameras():
    def load():
        payload = fetch_json_url(
            MNDOT_CARS_CAMERAS_URL,
            headers=mndot_cars_headers(),
            timeout=40,
        )
        if not isinstance(payload, list):
            raise ValueError('Minnesota 511 returned an invalid camera payload')
        cameras = []
        for item in payload:
            raw_id = str(item.get('id') or '').strip()
            location = item.get('location') or {}
            lat = optional_float(location.get('latitude'))
            lon = optional_float(location.get('longitude'))
            if (
                not valid_numeric_id(raw_id) or lat is None or lon is None or
                not item.get('public') or not point_in_state('MN', lat, lon)
            ):
                continue

            video_url = ''
            snapshot_url = ''
            view_name = ''
            for view in item.get('views') or []:
                candidate_video = str(view.get('url') or '').strip()
                parsed_video = urllib.parse.urlparse(candidate_video)
                if (
                    not video_url and view.get('type') == 'WMP' and
                    parsed_video.scheme == 'https' and
                    parsed_video.hostname == 'video.dot.state.mn.us' and
                    parsed_video.path.lower().endswith('.m3u8')
                ):
                    video_url = candidate_video
                    view_name = display_text(view.get('name'))

                candidate_snapshot = str(view.get('videoPreviewUrl') or '').strip()
                if not candidate_snapshot and view.get('type') != 'WMP':
                    candidate_snapshot = candidate_video
                parsed_snapshot = urllib.parse.urlparse(candidate_snapshot)
                if (
                    not snapshot_url and parsed_snapshot.scheme == 'https' and
                    parsed_snapshot.hostname == 'public.carsprogram.org' and
                    parsed_snapshot.path.startswith('/cameras/MN/')
                ):
                    snapshot_url = candidate_snapshot

            cameras.append({
                'id': raw_id,
                'name': display_text(item.get('name')) or f'MnDOT camera {raw_id}',
                'lat': lat,
                'lon': lon,
                'route': display_text(location.get('routeId')),
                'city': display_text(location.get('cityReference')),
                'owner': display_text((item.get('cameraOwner') or {}).get('name')),
                'view_name': view_name,
                'updated_at': epoch_milliseconds_iso(item.get('lastUpdated')),
                'snapshot_url': snapshot_url,
                'video_url': video_url,
            })
        if not cameras:
            raise ValueError('Minnesota 511 returned no public cameras')
        return cameras

    return mndot_cars_cached_items('Cameras', load)


def mndot_sign_message(item):
    messages = []
    image_active = False
    for page in (item.get('display') or {}).get('pages') or []:
        lines = []
        for value in page.get('lines') or []:
            text = display_text(value)
            if not text:
                continue
            parsed = urllib.parse.urlparse(text)
            if parsed.scheme in {'http', 'https'}:
                image_active = True
                continue
            lines.append(text)
        if lines:
            messages.append(' / '.join(lines))
    if messages:
        return ' · '.join(dict.fromkeys(messages))
    if image_active:
        return 'Active image message'
    return 'No active message'


def fetch_mndot_signs():
    def load():
        payload = fetch_json_url(
            MNDOT_CARS_SIGNS_URL,
            headers=mndot_cars_headers(),
            timeout=30,
        )
        if not isinstance(payload, list):
            raise ValueError('Minnesota 511 returned an invalid message-sign payload')
        signs = []
        for item in payload:
            raw_id = str(item.get('id') or '').strip()
            location = item.get('location') or {}
            lat = optional_float(location.get('latitude'))
            lon = optional_float(location.get('longitude'))
            if not raw_id or lat is None or lon is None or not point_in_state('MN', lat, lon):
                continue
            signs.append({
                'id': str(wv511_numeric_id(f'MNDOT:SIGN:{raw_id}')),
                'raw_id': raw_id,
                'name': display_text(item.get('name')) or 'MnDOT message sign',
                'lat': lat,
                'lon': lon,
                'message': mndot_sign_message(item),
                'status': display_text(item.get('status')),
                'location': display_text(location.get('locationDescription') or location.get('cityReference')),
                'updated_at': epoch_milliseconds_iso(item.get('lastUpdated')),
            })
        return signs

    return mndot_cars_cached_items('MessageSigns', load)


def mndot_event_coordinates(item):
    for feature in item.get('features') or []:
        geometry = feature.get('geometry') or {}
        coordinates = geometry.get('coordinates') or []
        if geometry.get('type') == 'Point' and isinstance(coordinates, list) and len(coordinates) >= 2:
            lon = safe_float(coordinates[0])
            lat = safe_float(coordinates[1])
            if point_in_state('MN', lat, lon):
                return lat, lon
    return mndot_cars_bbox_coordinates(item)


def mndot_event_is_construction(item):
    text = ' '.join(str(item.get(key) or '') for key in ('title', 'tooltip')).lower()
    if any(token in text for token in (
        'construction', 'roadwork', 'road work', 'maintenance', 'work zone',
        'bridge work', 'paving', 'resurfacing', 'utility work',
    )):
        return True
    for feature in item.get('features') or []:
        icon = ((feature.get('properties') or {}).get('icon') or {})
        icon_url = str(icon.get('url') or '').lower()
        if 'construction' in icon_url or 'roadwork' in icon_url:
            return True
    return False


def fetch_mndot_events(layer):
    def load_all():
        input_args = {
            'north': 49.39,
            'south': 43.49,
            'east': -89.49,
            'west': -97.24,
            'zoom': 14,
            'layerSlugs': ['roadReports'],
        }
        payload = fetch_json_url(
            MNDOT_CARS_GRAPHQL_URL,
            headers={**mndot_cars_headers(), 'Content-Type': 'application/json'},
            data={'query': MNDOT_CARS_EVENT_QUERY, 'variables': {'input': input_args}},
            timeout=40,
        )
        query = ((payload.get('data') or {}).get('mapFeaturesQuery') or {})
        error = query.get('error') or {}
        if error:
            raise ValueError(
                f'Minnesota 511 event error: {error.get("message") or error.get("type")}'
            )
        records = []
        for item in query.get('mapFeatures') or []:
            uri = str(item.get('uri') or '').strip()
            coordinates = mndot_event_coordinates(item)
            if not uri or not coordinates:
                continue
            event_layer = 'Construction' if mndot_event_is_construction(item) else 'Incidents'
            title = display_text(item.get('title')) or (
                'MnDOT roadwork' if event_layer == 'Construction' else 'MnDOT traffic incident'
            )
            records.append({
                'id': str(wv511_numeric_id(f'MNDOT:{uri}')),
                'uri': uri,
                'layer': event_layer,
                'name': title,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'description': strip_tags(item.get('tooltip')) or title,
                'severity': (
                    f'Priority {safe_int(item.get("priority"))}'
                    if item.get('priority') is not None else None
                ),
            })
        return records

    return [
        item for item in mndot_cars_cached_items('Events', load_all)
        if item['layer'] == layer
    ]


def mndot_layer_payload(layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['id'],
                'videoUrl': camera['video_url'] or None,
                'snapshotUrl': f'/camera-snapshot/MN/{camera["id"]}' if camera['snapshot_url'] else None,
                'snapshotFromVideo': False,
            },
        } for camera in fetch_mndot_cameras()]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': sign['updated_at']},
        } for sign in fetch_mndot_signs()]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': None,
        },
    } for item in fetch_mndot_events(layer)]}


def mndot_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_mndot_cameras():
        if camera['id'] != target:
            continue
        detail = ' · '.join(part for part in (
            camera['route'], camera['city'], camera['owner'] and f'{camera["owner"]} camera'
        ) if part)
        return {
            'name': camera['name'],
            'msg': detail or 'Minnesota 511 camera',
            'severity': None,
            'timestamp': camera['updated_at'],
            'video_id': target,
            'video_url': camera['video_url'] or None,
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': f'/camera-snapshot/MN/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
        }
    raise ValueError(f'Minnesota 511 camera {site_id} not found')


def mndot_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return mndot_camera_detail(target)
    records = (
        fetch_mndot_signs()
        if layer == 'MessageSigns'
        else fetch_mndot_events(layer) if layer in {'Incidents', 'Construction'} else []
    )
    for item in records:
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': item.get('status'),
                'timestamp': item.get('updated_at'),
                'location': item.get('location'),
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item.get('severity'),
            'timestamp': None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'Minnesota 511 {layer} item {item_id} not found')


def fetch_mndot_road_weather():
    def load():
        payload = fetch_json_url(
            MNDOT_CARS_GRAPHQL_URL,
            headers={**mndot_cars_headers(), 'Content-Type': 'application/json'},
            data={
                'query': MNDOT_CARS_WEATHER_QUERY,
                'variables': {'slugs': ['stationsNormal', 'stationsAlert']},
            },
            timeout=40,
        )
        query = ((payload.get('data') or {}).get('searchBoundsQuery') or {})
        error = query.get('error') or {}
        if error:
            raise ValueError(
                f'Minnesota 511 road-weather error: {error.get("message") or error.get("type")}'
            )
        features = []
        seen = set()
        for station in query.get('results') or []:
            uri = str(station.get('uri') or '').strip()
            coordinates = mndot_cars_bbox_coordinates(station)
            if not uri or uri in seen or not coordinates:
                continue
            seen.add(uri)
            fields = station.get('weatherStationFields') or {}
            raw_id = uri.rsplit('/', 1)[-1]
            road_state = display_text(
                ((fields.get('PAVEMENT_SURFACE_STATUS') or {}).get('displayValue')) or
                ((fields.get('IA_SURFACE_SITUATION') or {}).get('displayValue'))
            ) or 'Unknown'
            wind_mph = indot_trafficwise_measurement(fields, 'WIND_AVG_SPEED')
            gust_mph = indot_trafficwise_measurement(fields, 'WIND_MAX_SPEED')
            lat, lon = coordinates
            attrs = {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'MNDOT_{raw_id}',
                'LATITUDE': lat,
                'LNGITUDE': lon,
                'LOCALNAM': display_text(station.get('title')) or f'MnDOT weather station {raw_id}',
                'OBS_TIME_LOCAL': epoch_milliseconds_iso((station.get('lastUpdated') or {}).get('timestamp')),
                'AIR_TEMP_F': indot_trafficwise_measurement(fields, 'TEMP_AIR_TEMPERATURE'),
                'DEW_POINT_F': indot_trafficwise_measurement(fields, 'TEMP_DEW_POINT'),
                'RELATIVE_HUMIDITY': indot_trafficwise_measurement(fields, 'PRECIP_RELATIVE_HUMIDITY'),
                'WIND_DIRECTION': display_text(((fields.get('WIND_AVG_DIRECTION') or {}).get('displayValue'))),
                'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
                'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
                'VISIBILITY': indot_trafficwise_measurement(fields, 'VIS_VISIBILITY'),
                'ROAD_TEMP_F': indot_trafficwise_measurement(fields, 'PAVEMENT_SURFACE_TEMPERATURE'),
                'SUBSURFACE_TEMP_F': indot_trafficwise_measurement(fields, 'PAVEMENT_SUB_SURFACE_TEMPERATURE'),
                'ROAD_STATE': road_state,
                'PRECIP_1H_IN': indot_trafficwise_measurement(fields, 'PRECIP_PAST_HOUR'),
                'PRECIP_12H_IN': indot_trafficwise_measurement(fields, 'PRECIP_PAST_12_HOURS'),
                'PRECIP_24H_IN': indot_trafficwise_measurement(fields, 'PRECIP_PAST_24_HOURS'),
                'SOURCE_URL': MNDOT_CARS_SOURCE_URL,
            }
            features.append({'attributes': attrs, 'geometry': {'x': lon, 'y': lat}})
        if not features:
            raise ValueError('Minnesota 511 returned no road-weather stations')
        return features

    return mndot_cars_cached_items('RoadWeather', load)


def iadot_cached_items(cache_key, loader):
    now = time.time()
    with IADOT_CACHE_LOCK:
        cached = IADOT_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with IADOT_CACHE_LOCK:
        IADOT_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + IADOT_CACHE_TTL,
        }
    return items


def iadot_fetch_dataset(kind):
    url = IADOT_TRAFFIC_URLS.get(kind)
    if not url:
        return []

    def load():
        params = urllib.parse.urlencode({
            'where': '1=1',
            'outFields': '*',
            'returnGeometry': 'true',
            'outSR': '4326',
            'geometryPrecision': '6',
            'f': 'geojson',
        })
        payload = fetch_json_url(f'{url}?{params}', headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': IADOT_SOURCE_URL,
            'Accept': 'application/geo+json,application/json',
        }, timeout=35)
        return payload.get('features') or []

    return iadot_cached_items(f'Dataset:{kind}', load)


def iadot_point(feature):
    geometry = feature.get('geometry') or {}
    coordinates = geometry.get('coordinates') or []
    if geometry.get('type') != 'Point' or len(coordinates) < 2:
        return None
    lon = optional_float(coordinates[0])
    lat = optional_float(coordinates[1])
    if lat is None or lon is None or not point_in_state('IA', lat, lon):
        return None
    return lat, lon


def iadot_device_timestamp(date_value, time_value, utc_offset):
    try:
        date_text = str(int(date_value)).zfill(8)
        time_text = str(int(time_value)).zfill(6)
        stamp = datetime.datetime.strptime(date_text + time_text, '%Y%m%d%H%M%S')
        raw_offset = int(utc_offset or 0)
        sign = -1 if raw_offset < 0 else 1
        raw_offset = abs(raw_offset)
        offset = datetime.timedelta(hours=raw_offset // 100, minutes=raw_offset % 100)
        return stamp.replace(tzinfo=datetime.timezone(sign * offset)).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_iadot_cameras():
    cameras = []
    seen = set()
    for feature in iadot_fetch_dataset('Cameras'):
        props = feature.get('properties') or {}
        coordinates = iadot_point(feature)
        raw_id = str(props.get('device_id') or props.get('FID') or '').strip()
        if not valid_numeric_id(raw_id) or raw_id in seen or not coordinates:
            continue
        seen.add(raw_id)
        snapshot_url = str(props.get('ImageURL') or '').strip()
        parsed_snapshot = urllib.parse.urlparse(snapshot_url)
        if not (
            parsed_snapshot.scheme == 'https' and
            str(parsed_snapshot.hostname or '').lower().endswith('.iowadot.gov') and
            parsed_snapshot.path.lower().endswith(('.jpg', '.jpeg', '.png'))
        ):
            snapshot_url = ''
        video_url = str(props.get('VideoURL') or '').strip()
        parsed_video = urllib.parse.urlparse(video_url)
        if not (
            parsed_video.scheme == 'https' and
            str(parsed_video.hostname or '').lower().endswith('.iowadot.gov') and
            parsed_video.path.lower().endswith('.m3u8')
        ):
            video_url = ''
        lat, lon = coordinates
        cameras.append({
            'id': raw_id,
            'name': display_text(props.get('Desc_') or props.get('ImageName')) or f'Iowa DOT camera {raw_id}',
            'lat': lat,
            'lon': lon,
            'route': display_text(props.get('Route')),
            'region': display_text(props.get('REGION')),
            'camera_type': display_text(props.get('Type')),
            'updated_at': iadot_device_timestamp(
                props.get('UpdateDate'), props.get('UpdateTime'), props.get('UTCoffset')
            ),
            'snapshot_url': snapshot_url,
            'video_url': video_url,
        })
    if not cameras:
        raise ValueError('Iowa DOT returned no public cameras')
    return cameras


def fetch_iadot_signs():
    signs = []
    for kind, active in (('SignsActive', True), ('SignsInactive', False)):
        for feature in iadot_fetch_dataset(kind):
            props = feature.get('properties') or {}
            coordinates = iadot_point(feature)
            raw_id = str(props.get('FID') or '').strip()
            name = display_text(props.get('DeviceName'))
            if not raw_id or not coordinates:
                continue
            message = strip_tags(props.get('msghtml')) or display_text(
                props.get('msgtext') or props.get('msgtxt')
            )
            signs.append({
                'id': str(wv511_numeric_id(f'IADOT:SIGN:{kind}:{raw_id}:{name}')),
                'name': name or 'Iowa DOT message sign',
                'lat': coordinates[0],
                'lon': coordinates[1],
                'message': message or ('No current message' if not active else 'Active message'),
                'active': active,
                'status': 'Active' if active else 'Inactive',
                'route': display_text(props.get('Route')),
                'direction': display_text(props.get('Direction')),
                'updated_at': epoch_milliseconds_iso(props.get('EditDate')),
            })
    return signs


def iadot_event_is_construction(props):
    text = ' '.join(str(props.get(key) or '') for key in (
        'headline', 'phrase', 'cause', 'msg0', 'msg1', 'Desc0', 'Desc1'
    )).lower()
    return any(token in text for token in (
        'construction', 'roadwork', 'road work', 'maintenance', 'work zone',
        'bridge work', 'paving', 'resurfacing', 'utility work',
    ))


def fetch_iadot_events(layer):
    def load_all():
        events = []
        seen = set()
        for feature in iadot_fetch_dataset('Events'):
            props = feature.get('properties') or {}
            coordinates = iadot_point(feature)
            raw_id = str(props.get('ID') or props.get('OBJECTID') or '').strip()
            if not raw_id or raw_id in seen or not coordinates:
                continue
            seen.add(raw_id)
            event_layer = 'Construction' if iadot_event_is_construction(props) else 'Incidents'
            title = display_text(props.get('headline') or props.get('phrase')) or (
                'Iowa DOT roadwork' if event_layer == 'Construction' else 'Iowa DOT traffic incident'
            )
            description = ' '.join(part for part in (
                display_text(props.get('msg0')),
                display_text(props.get('msg1')),
                display_text(props.get('cause')),
                display_text(props.get('Instruct')),
            ) if part) or title
            events.append({
                'id': str(wv511_numeric_id(f'IADOT:EVENT:{raw_id}')),
                'raw_id': raw_id,
                'layer': event_layer,
                'name': title,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'description': description,
                'severity': (
                    f'Priority {safe_int(props.get("Priority"))}'
                    if props.get('Priority') is not None else display_text(props.get('STYLE')) or None
                ),
                'location': display_text(props.get('Desc0') or props.get('Desc1')) or None,
                'timestamp': epoch_milliseconds_iso(props.get('EditDate')),
            })
        return events

    return [item for item in iadot_cached_items('Events', load_all) if item['layer'] == layer]


def iadot_layer_payload(layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['id'],
                'videoUrl': camera['video_url'] or None,
                'snapshotUrl': f'/camera-snapshot/IA/{camera["id"]}' if camera['snapshot_url'] else None,
                'snapshotFromVideo': False,
            },
        } for camera in fetch_iadot_cameras()]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': sign['updated_at']},
        } for sign in fetch_iadot_signs()]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
        },
    } for item in fetch_iadot_events(layer)]}


def iadot_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_iadot_cameras():
        if camera['id'] != target:
            continue
        return {
            'name': camera['name'],
            'msg': ' · '.join(part for part in (
                camera['route'], camera['region'], camera['camera_type']
            ) if part) or 'Iowa DOT camera',
            'severity': None,
            'timestamp': camera['updated_at'],
            'video_id': target,
            'video_url': camera['video_url'] or None,
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': f'/camera-snapshot/IA/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
        }
    raise ValueError(f'Iowa DOT camera {site_id} not found')


def iadot_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return iadot_camera_detail(target)
    records = (
        fetch_iadot_signs()
        if layer == 'MessageSigns'
        else fetch_iadot_events(layer) if layer in {'Incidents', 'Construction'} else []
    )
    for item in records:
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': item['status'],
                'timestamp': item['updated_at'],
                'location': ' · '.join(part for part in (item['route'], item['direction']) if part),
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'Iowa DOT {layer} item {item_id} not found')


def modot_cached_items(cache_key, loader):
    now = time.time()
    with MODOT_CACHE_LOCK:
        cached = MODOT_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with MODOT_CACHE_LOCK:
        MODOT_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + MODOT_CACHE_TTL,
        }
    return items


def modot_json(url):
    return fetch_json_url(url, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': MODOT_SOURCE_URL,
        'Accept': 'application/json',
    }, timeout=35)


def modot_point(lon_value, lat_value):
    lon = optional_float(lon_value)
    lat = optional_float(lat_value)
    if lat is None or lon is None or not point_in_state('MO', lat, lon):
        return None
    return lat, lon


def modot_clean_html(value):
    text = re.sub(r'<br\s*/?>', ' · ', str(value or ''), flags=re.I)
    text = strip_tags(text)
    return re.sub(r'\s*·\s*', ' · ', text).strip(' ·')


def fetch_modot_cameras():
    def load():
        cameras = []
        seen = set()
        streaming = modot_json(MODOT_STREAMING_CAMERAS_URL)
        for item in streaming if isinstance(streaming, list) else []:
            coordinates = modot_point(item.get('x'), item.get('y'))
            video_url = str(item.get('html') or '').strip()
            parsed = urllib.parse.urlparse(video_url)
            if not (
                coordinates and parsed.scheme == 'https' and
                str(parsed.hostname or '').lower().endswith('.modot.mo.gov') and
                parsed.path.lower().endswith('.m3u8')
            ):
                continue
            name = display_text(item.get('location')) or 'MoDOT live camera'
            camera_id = str(wv511_numeric_id(f'MODOT:STREAM:{name}:{video_url}'))
            if camera_id in seen:
                continue
            seen.add(camera_id)
            cameras.append({
                'id': camera_id,
                'name': name,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'video_url': video_url,
                'snapshot_url': '',
                'camera_type': 'MoDOT live camera',
                'updated_at': None,
            })

        snapshot_payload = modot_json(MODOT_SNAPSHOT_CAMERAS_URL)
        for item in snapshot_payload.get('cameras') or []:
            location = item.get('location') or {}
            coordinates = modot_point(location.get('x'), location.get('y'))
            snapshot_url = urllib.parse.urljoin(MODOT_SOURCE_URL, str(item.get('url') or '').strip())
            parsed = urllib.parse.urlparse(snapshot_url)
            if not (
                coordinates and parsed.scheme == 'https' and
                parsed.hostname == 'traveler.modot.org' and
                parsed.path.lower().endswith(('.jpg', '.jpeg', '.png'))
            ):
                continue
            name = display_text(item.get('caption')) or 'MoDOT snapshot camera'
            camera_id = str(wv511_numeric_id(
                f'MODOT:SNAPSHOT:{item.get("id")}:{name}:{snapshot_url}'
            ))
            if camera_id in seen:
                continue
            seen.add(camera_id)
            cameras.append({
                'id': camera_id,
                'name': name,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'video_url': '',
                'snapshot_url': snapshot_url,
                'camera_type': 'MoDOT snapshot camera',
                'updated_at': None,
            })
        if not cameras:
            raise ValueError('MoDOT returned no public cameras')
        return cameras

    return modot_cached_items('Cameras', load)


def fetch_modot_signs():
    def load():
        signs = []
        payload = modot_json(MODOT_SIGNS_URL)
        for item in payload if isinstance(payload, list) else []:
            coordinates = modot_point(item.get('x'), item.get('y'))
            if not coordinates:
                continue
            name = display_text(item.get('dev')) or 'MoDOT message sign'
            message = modot_clean_html(item.get('msg')) or 'No current message'
            sign_id = str(wv511_numeric_id(
                f'MODOT:SIGN:{name}:{coordinates[0]}:{coordinates[1]}'
            ))
            signs.append({
                'id': sign_id,
                'name': name,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'message': message,
                'active': bool(item.get('msg')),
                'status': 'Active' if item.get('msg') else 'Inactive',
                'updated_at': None,
            })
        return signs

    return modot_cached_items('MessageSigns', load)


def fetch_modot_events(layer):
    def load_all():
        events = []
        payload = modot_json(MODOT_EVENTS_URL)
        for item in payload if isinstance(payload, list) else []:
            major_type = str(item.get('MT') or '').upper()
            if major_type == 'WZ':
                event_layer = 'Construction'
            elif major_type in {'TI', 'FL'}:
                event_layer = 'Incidents'
            else:
                continue
            geometry = item.get('GEOM') or {}
            coordinates = modot_point(geometry.get('x'), geometry.get('y'))
            if not coordinates:
                continue
            raw_id = str(item.get('OID') or '').strip()
            title = modot_clean_html(item.get('MSGS')) or display_text(item.get('MSGL')) or (
                'MoDOT roadwork' if event_layer == 'Construction' else 'MoDOT traffic incident'
            )
            description = modot_clean_html(item.get('MSG')) or title
            events.append({
                'id': str(wv511_numeric_id(
                    f'MODOT:EVENT:{raw_id}:{major_type}:{coordinates[0]}:{coordinates[1]}'
                )),
                'layer': event_layer,
                'name': title,
                'lat': coordinates[0],
                'lon': coordinates[1],
                'description': description,
                'severity': display_text(item.get('LOI')) or None,
                'location': display_text(item.get('MSGL')) or None,
                'timestamp': None,
            })
        return events

    return [item for item in modot_cached_items('Events', load_all) if item['layer'] == layer]


def modot_layer_payload(layer):
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['id'],
                'videoUrl': camera['video_url'] or None,
                'snapshotUrl': f'/camera-snapshot/MO/{camera["id"]}' if camera['snapshot_url'] else None,
                'snapshotFromVideo': False,
            },
        } for camera in fetch_modot_cameras()]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': sign['updated_at']},
        } for sign in fetch_modot_signs()]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
        },
    } for item in fetch_modot_events(layer)]}


def modot_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_modot_cameras():
        if camera['id'] != target:
            continue
        return {
            'name': camera['name'],
            'msg': camera['camera_type'],
            'severity': None,
            'timestamp': camera['updated_at'],
            'video_id': target,
            'video_url': camera['video_url'] or None,
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': f'/camera-snapshot/MO/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
        }
    raise ValueError(f'MoDOT camera {site_id} not found')


def modot_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return modot_camera_detail(target)
    records = (
        fetch_modot_signs()
        if layer == 'MessageSigns'
        else fetch_modot_events(layer) if layer in {'Incidents', 'Construction'} else []
    )
    for item in records:
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': item['status'],
                'timestamp': item['updated_at'],
                'location': None,
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'MoDOT {layer} item {item_id} not found')


def oktraffic_headers():
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': OKTRAFFIC_SOURCE_URL,
        'Accept': 'application/json',
    }


def oktraffic_json(resource, filter_value=None, timeout=35):
    params = urllib.parse.urlencode({'filter': json.dumps(filter_value, separators=(',', ':'))}) \
        if filter_value else ''
    url = f'{OKTRAFFIC_API_ROOT}/{resource}'
    if params:
        url += '?' + params
    return fetch_json_url(url, headers=oktraffic_headers(), timeout=timeout)


def oktraffic_cached_items(cache_key, loader):
    now = time.time()
    with OKTRAFFIC_CACHE_LOCK:
        cached = OKTRAFFIC_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with OKTRAFFIC_CACHE_LOCK:
        OKTRAFFIC_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + OKTRAFFIC_CACHE_TTL,
        }
    return items


def oktraffic_stream_url(value):
    candidate = str(value or '').strip()
    parsed = urllib.parse.urlparse(candidate)
    if not (
        parsed.scheme == 'https' and
        parsed.hostname == 'stream.oktraffic.org' and
        parsed.path.startswith('/delay-stream/') and
        parsed.path.lower().endswith('.m3u8')
    ):
        return ''
    return candidate


def fetch_oktraffic_cameras():
    def load():
        cameras = []
        payload = oktraffic_json('MapCameras', {
            'include': ['streamDictionary', 'offlineCamera'],
        })
        for item in payload if isinstance(payload, list) else []:
            lat = optional_float(item.get('latitude'))
            lon = optional_float(item.get('longitude'))
            raw_id = str(item.get('id') or '').strip()
            if lat is None or lon is None or not raw_id.isdigit() or not point_in_state('OK', lat, lon):
                continue
            stream = item.get('streamDictionary') or {}
            video_url = oktraffic_stream_url(stream.get('streamSrc'))
            cameras.append({
                'id': raw_id,
                'name': str(item.get('location') or '').strip() or f'OKTraffic camera {raw_id}',
                'lat': lat,
                'lon': lon,
                'direction': str(item.get('direction') or '').strip() or None,
                'city': display_text(item.get('city')) or None,
                'status': display_text(item.get('status')) or None,
                'updated_at': item.get('recordTime'),
                'video_url': video_url,
            })
        return cameras
    return oktraffic_cached_items('Cameras', load)


def oktraffic_sign_message(value):
    text = html.unescape(str(value or ''))
    text = re.sub(r'\[(?:nl|np)\]', ' / ', text, flags=re.I)
    text = re.sub(r'\[[^\]]+\]', ' ', text)
    text = strip_tags(text)
    text = re.sub(r'[\x00-\x1f]+', ' / ', text)
    text = re.sub(r'\s*/\s*(?:/\s*)+', ' / ', text)
    return re.sub(r'\s+', ' ', text).strip(' /') or 'No active message'


def fetch_oktraffic_signs():
    def load():
        signs = []
        payload = oktraffic_json('Signs', {
            'include': [
                {'relation': 'device', 'scope': {'include': 'address'}},
                'dmsStatus',
                'signType',
            ],
        })
        for item in payload if isinstance(payload, list) else []:
            device = item.get('device') or {}
            address = device.get('address') or {}
            status = item.get('dmsStatus') or {}
            raw_id = str(item.get('id') or '').strip()
            lat = optional_float(address.get('latitude'))
            lon = optional_float(address.get('longitude'))
            if lat is None or lon is None or not raw_id.isdigit() or not point_in_state('OK', lat, lon):
                continue
            signs.append({
                'id': raw_id,
                'name': str(address.get('name') or '').strip() or f'OKTraffic message sign {raw_id}',
                'lat': lat,
                'lon': lon,
                'message': oktraffic_sign_message(status.get('message')),
                'active': bool(str(status.get('message') or '').strip()),
                'direction': str(address.get('direction') or '').strip() or None,
                'city': display_text(address.get('city')) or None,
                'updated_at': status.get('recordTime'),
                'sign_type': str((item.get('signType') or {}).get('name') or '').strip() or None,
            })
        return signs
    return oktraffic_cached_items('MessageSigns', load)


def oktraffic_incident_label(item):
    kind = str(item.get('subtype') or item.get('type') or 'Traffic incident').strip()
    kind = re.sub(r'^(?:HAZARD_ON_(?:ROAD|SHOULDER)_|ACCIDENT_)', '', kind)
    label = kind.replace('_', ' ').strip().title()
    return {
        'Accident': 'Crash',
        'Road Closed': 'Road closure',
        'Jam': 'Traffic congestion',
    }.get(label, label or 'Traffic incident')


def fetch_oktraffic_incidents():
    def load():
        incidents = []
        payload = oktraffic_json('WazeAlerts')
        for item in payload if isinstance(payload, list) else []:
            lat = optional_float(item.get('latitude'))
            lon = optional_float(item.get('longitude'))
            raw_id = str(item.get('uuid') or '').strip()
            if lat is None or lon is None or not raw_id or not point_in_state('OK', lat, lon):
                continue
            label = oktraffic_incident_label(item)
            street = display_text(item.get('street'))
            city = display_text(item.get('city'))
            location = ' · '.join(part for part in (street, city) if part) or None
            description = strip_tags(item.get('reportDescription')) or label
            incidents.append({
                'id': str(wv511_numeric_id(f'OKTRAFFIC:WAZE:{raw_id}')),
                'name': f'{label} · {street}' if street else label,
                'lat': lat,
                'lon': lon,
                'description': description,
                'severity': (
                    f'Waze reliability {safe_int(item.get("reliability"))}/10'
                    if item.get('reliability') is not None else None
                ),
                'timestamp': item.get('incidentDate') or epoch_milliseconds_iso(item.get('pubMillis')),
                'location': location,
            })
        return incidents
    return oktraffic_cached_items('Incidents', load)


def oktraffic_contract_center(item):
    detail = item.get('contractDetail') or {}
    lat = optional_float(detail.get('latitude'))
    lon = optional_float(detail.get('longitude'))
    if lat is not None and lon is not None and point_in_state('OK', lat, lon):
        return lat, lon
    for group in item.get('locationGroups') or []:
        locations = sorted(group.get('locations') or [], key=lambda entry: safe_int(entry.get('order')))
        if not locations:
            continue
        point = locations[len(locations) // 2]
        lat = optional_float(point.get('latitude'))
        lon = optional_float(point.get('longitude'))
        if lat is not None and lon is not None and point_in_state('OK', lat, lon):
            return lat, lon
    return None


def fetch_oktraffic_construction():
    def load():
        construction = []
        payload = oktraffic_json('Contracts/getMapContracts', timeout=50)
        for item in payload if isinstance(payload, list) else []:
            center = oktraffic_contract_center(item)
            raw_id = str(item.get('id') or '').strip()
            if not center or not raw_id.isdigit():
                continue
            lat, lon = center
            location = display_text(item.get('location'))
            project = display_text(item.get('description'))
            road_names = []
            impacts = []
            for group in item.get('locationGroups') or []:
                road_names.extend(display_text(road) for road in group.get('roadNames') or [] if road)
                impacts.extend(display_text(group.get(key)) for key in (
                    'vehicleImpact', 'restrictions', 'lanesInvolved'
                ) if group.get(key))
            roads = ', '.join(dict.fromkeys(road_names))
            title = ' · '.join(part for part in (roads, location or project) if part) or 'Oklahoma road construction'
            detail_parts = [part for part in (project, display_text(item.get('status'))) if part]
            percent = optional_float(item.get('percentComplete'))
            if percent is not None:
                detail_parts.append(f'{percent:g}% complete')
            detail_parts.extend(dict.fromkeys(impacts))
            construction.append({
                'id': raw_id,
                'name': title,
                'lat': lat,
                'lon': lon,
                'description': ' · '.join(dict.fromkeys(detail_parts)) or title,
                'severity': display_text(item.get('contractStatus')) or None,
                'timestamp': item.get('lastUpdatedDate'),
                'location': location or roads or None,
            })
        return construction
    return oktraffic_cached_items('Construction', load)


def oktraffic_records(layer):
    if layer == 'Cameras':
        return fetch_oktraffic_cameras()
    if layer == 'MessageSigns':
        return fetch_oktraffic_signs()
    if layer == 'Incidents':
        return fetch_oktraffic_incidents()
    if layer == 'Construction':
        return fetch_oktraffic_construction()
    return []


def oktraffic_layer_payload(layer):
    records = oktraffic_records(layer)
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['id'],
                'videoUrl': camera['video_url'] or None,
                'snapshotUrl': None,
                'snapshotFromVideo': False,
            },
        } for camera in records]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': sign['updated_at']},
        } for sign in records]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
        },
    } for item in records]}


def oktraffic_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_oktraffic_cameras():
        if camera['id'] != target:
            continue
        detail = ' · '.join(part for part in (
            camera.get('city'),
            f'{camera["direction"]} view' if camera.get('direction') else None,
            camera.get('status'),
        ) if part)
        return {
            'name': camera['name'],
            'msg': detail or 'Oklahoma DOT traffic camera',
            'severity': None,
            'timestamp': camera.get('updated_at'),
            'video_id': target,
            'video_url': camera['video_url'] or None,
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': None,
            'upstream_snapshot_url': None,
        }
    raise ValueError(f'OKTraffic camera {site_id} not found')


def oktraffic_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return oktraffic_camera_detail(target)
    for item in oktraffic_records(layer):
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': ' · '.join(part for part in (
                    item.get('city'), item.get('direction'), item.get('sign_type')
                ) if part),
                'timestamp': item.get('updated_at'),
                'location': None,
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'OKTraffic {layer} item {item_id} not found')


def drivetexas_headers():
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': DRIVETEXAS_SOURCE_URL,
        'Accept': 'application/json',
    }


def drivetexas_query(table, fields, take=5000):
    request_data = {
        'action': 'table/query',
        'query': {
            'sqlselect': fields,
            'start': 0,
            'table': table,
            'take': take,
        },
    }
    url = f'{DRIVETEXAS_API_URL}?{urllib.parse.urlencode({"request": json.dumps(request_data, separators=(",", ":"))})}'
    payload = fetch_json_url(url, headers=drivetexas_headers(), timeout=45)
    columns = ((payload.get('data') or {}).get('data') or {}) if isinstance(payload, dict) else {}
    if not isinstance(columns, dict) or not columns:
        raise ValueError('DriveTexas returned an unexpected MapLarge response')
    lengths = [len(values) for values in columns.values() if isinstance(values, list)]
    if not lengths:
        return []
    row_count = min(lengths)
    return [
        {
            field: values[index]
            for field, values in columns.items()
            if isinstance(values, list) and index < len(values)
        }
        for index in range(row_count)
    ]


def drivetexas_cached_items(cache_key, loader):
    now = time.time()
    with DRIVETEXAS_CACHE_LOCK:
        cached = DRIVETEXAS_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with DRIVETEXAS_CACHE_LOCK:
        DRIVETEXAS_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + DRIVETEXAS_CACHE_TTL,
        }
    return items


def drivetexas_point(value):
    match = re.fullmatch(
        r'\s*POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)\s*',
        str(value or ''),
        re.I,
    )
    if not match:
        return None
    lon = optional_float(match.group(1))
    lat = optional_float(match.group(2))
    if lat is None or lon is None or not point_in_state('TX', lat, lon):
        return None
    return lat, lon


def drivetexas_stream_url(value):
    candidate = str(value or '').strip()
    parsed = urllib.parse.urlparse(candidate)
    hostname = str(parsed.hostname or '').lower()
    if not (
        parsed.scheme == 'https' and
        (hostname == 'skyvdn.com' or hostname.endswith('.skyvdn.com')) and
        parsed.path.startswith('/rtplive/') and
        parsed.path.lower().endswith('.m3u8')
    ):
        return ''
    return candidate


def fetch_drivetexas_cameras():
    def load():
        rows = drivetexas_query('appgeo/cameraPoint', [
            'id', 'guid', 'route', 'jurisdiction', 'description', 'name',
            'direction', 'active', 'problemstream', 'lastUpdated', 'httpsurl',
            'imageurl', 'prerollurl', 'XY',
        ])
        cameras = []
        seen = set()
        for item in rows:
            center = drivetexas_point(item.get('XY'))
            raw_id = str(item.get('id') or item.get('guid') or '').strip()
            if not center or not raw_id or raw_id in seen or not safe_int(item.get('active')):
                continue
            seen.add(raw_id)
            lat, lon = center
            video_url = '' if safe_int(item.get('problemstream')) else drivetexas_stream_url(item.get('httpsurl'))
            route = display_text(item.get('route'))
            description = strip_tags(item.get('description'))
            raw_name = str(item.get('name') or '').strip()
            name = description or route or raw_name or 'DriveTexas traffic camera'
            cameras.append({
                'id': str(wv511_numeric_id(f'DRIVETEXAS:CAM:{raw_id}')),
                'raw_id': raw_id,
                'name': name,
                'lat': lat,
                'lon': lon,
                'route': route or None,
                'direction': display_text(item.get('direction')) or None,
                'jurisdiction': display_text(item.get('jurisdiction')) or None,
                'updated_at': epoch_milliseconds_iso(item.get('lastUpdated')),
                'video_url': video_url,
            })
        if not cameras:
            raise ValueError('DriveTexas returned no active cameras')
        return cameras
    return drivetexas_cached_items('Cameras', load)


def drivetexas_route_name(route):
    raw = re.sub(r'\s+', '', str(route or '').upper())
    match = re.fullmatch(r'([A-Z]{1,3})(\d+)([A-Z]?)', raw)
    if not match:
        return display_text(route)
    prefix, number, suffix = match.groups()
    return f'{prefix} {number.lstrip("0") or "0"}{suffix}'


DRIVETEXAS_CONDITION_LABELS = {
    'A': 'Crash',
    'C': 'Construction',
    'D': 'Road damage',
    'F': 'Flooding',
    'I': 'Ice / snow',
    'O': 'Road hazard',
    'X': 'Evacuation route',
    'Y': 'Travel restriction',
    'Z': 'Road closure',
}


def fetch_drivetexas_conditions():
    def load():
        rows = drivetexas_query('appgeo/conditionsPoint', [
            'OBJECTID', 'GLOBALID', 'RTENM', 'RDWAYNM', 'TRVLDRCTCD',
            'CNSTRNTTYPECD', 'CONDDSCR', 'CONDLMTFROMDSCR', 'CONDLMTTODSCR',
            'CONDSTARTTS', 'CONDENDTS', 'CNSTRNTDELAYFLAG',
            'CNSTRNTDETOURFLAG', 'lastUpdated', 'XY',
        ])
        conditions = []
        for item in rows:
            center = drivetexas_point(item.get('XY'))
            raw_id = str(item.get('OBJECTID') or item.get('GLOBALID') or '').strip()
            code = str(item.get('CNSTRNTTYPECD') or '').strip().upper()
            if not center or not raw_id or code not in DRIVETEXAS_CONDITION_LABELS:
                continue
            lat, lon = center
            item_id = str(wv511_numeric_id(f'DRIVETEXAS:COND:{raw_id}'))
            label = DRIVETEXAS_CONDITION_LABELS[code]
            route = display_text(item.get('RDWAYNM')) or drivetexas_route_name(item.get('RTENM'))
            direction = display_text(item.get('TRVLDRCTCD'))
            location_parts = [
                display_text(item.get('CONDLMTFROMDSCR')),
                display_text(item.get('CONDLMTTODSCR')),
            ]
            location = ' to '.join(part for part in location_parts if part) or route or None
            description = re.sub(r'\s+', ' ', strip_tags(item.get('CONDDSCR'))).strip(' -')
            detail_parts = [description or label]
            if str(item.get('CNSTRNTDELAYFLAG') or '').upper() == 'Y':
                detail_parts.append('Delays expected')
            if str(item.get('CNSTRNTDETOURFLAG') or '').upper() == 'Y':
                detail_parts.append('Detour reported')
            conditions.append({
                'id': item_id,
                'name': f'{label} · {route}' if route else label,
                'lat': lat,
                'lon': lon,
                'layer': 'Construction' if code == 'C' else 'Incidents',
                'description': ' · '.join(dict.fromkeys(part for part in detail_parts if part)),
                'severity': label,
                'timestamp': epoch_milliseconds_iso(item.get('lastUpdated') or item.get('CONDSTARTTS')),
                'start_time': epoch_milliseconds_iso(item.get('CONDSTARTTS')),
                'end_time': epoch_milliseconds_iso(item.get('CONDENDTS')),
                'location': location,
            })
        return conditions
    return drivetexas_cached_items('Conditions', load)


def drivetexas_records(layer):
    if layer == 'Cameras':
        return fetch_drivetexas_cameras()
    if layer in {'Incidents', 'Construction'}:
        return [item for item in fetch_drivetexas_conditions() if item['layer'] == layer]
    return []


def drivetexas_layer_payload(layer):
    records = drivetexas_records(layer)
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['id'],
                'videoUrl': camera['video_url'] or None,
                'snapshotUrl': None,
                'snapshotFromVideo': False,
            },
        } for camera in records]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else item['severity'],
            'description': item['description'],
            'severity': item['severity'],
            'timestamp': item['timestamp'],
            'location': item['location'],
        },
    } for item in records]}


def drivetexas_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_drivetexas_cameras():
        if camera['id'] != target:
            continue
        detail = ' · '.join(part for part in (
            camera.get('route'), camera.get('direction'), camera.get('jurisdiction')
        ) if part)
        return {
            'name': camera['name'],
            'msg': detail or 'TxDOT DriveTexas traffic camera',
            'severity': None,
            'timestamp': camera.get('updated_at'),
            'video_id': target,
            'video_url': camera['video_url'] or None,
            'video_enabled': bool(camera['video_url']),
            'snapshot_url': None,
            'upstream_snapshot_url': None,
        }
    raise ValueError(f'DriveTexas camera {site_id} not found')


def drivetexas_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return drivetexas_camera_detail(target)
    for item in drivetexas_records(layer):
        if item['id'] != target:
            continue
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item['severity'],
            'timestamp': item['timestamp'],
            'location': item['location'],
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'DriveTexas {layer} item {item_id} not found')


def nmroads_headers():
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': NMROADS_SOURCE_URL,
        'Accept': 'application/json,text/javascript,*/*',
    }


def nmroads_json(api_root, resource, params=None, timeout=35):
    url = f'{api_root}/{resource}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    return fetch_json_url(url, headers=nmroads_headers(), timeout=timeout)


def nmroads_cached_items(cache_key, loader):
    now = time.time()
    with NMROADS_CACHE_LOCK:
        cached = NMROADS_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = loader()
    except Exception:
        if cached:
            return cached['items']
        raise
    with NMROADS_CACHE_LOCK:
        NMROADS_CACHE[cache_key] = {
            'items': items,
            'expires_at': time.time() + NMROADS_CACHE_TTL,
        }
    return items


def fetch_nmroads_cameras():
    def load():
        payload = nmroads_json(NMROADS_API_ROOT, 'GetCameraInfo')
        cameras = []
        for item in payload.get('cameraInfo') or []:
            lat = optional_float(item.get('lat'))
            lon = optional_float(item.get('lon'))
            raw_name = str(item.get('name') or '').strip()
            if (
                lat is None or lon is None or not raw_name or
                not item.get('enabled') or not point_in_state('NM', lat, lon)
            ):
                continue
            camera_id = str(wv511_numeric_id(f'NMROADS:CAM:{raw_name}'))
            snapshot_url = f'{NMROADS_API_ROOT}/GetCameraImage?{urllib.parse.urlencode({"ts": "0", "cameraName": raw_name})}'
            cameras.append({
                'id': camera_id,
                'raw_name': raw_name,
                'name': strip_tags(item.get('title')) or raw_name.replace('_', ' '),
                'lat': lat,
                'lon': lon,
                'camera_type': display_text(item.get('cameraType')) or 'Roadside camera',
                'grouping': display_text(item.get('grouping')) or None,
                'district': safe_int(item.get('district')),
                'snapshot_url': snapshot_url,
            })
        if not cameras:
            raise ValueError('NMRoads returned no active cameras')
        return cameras
    return nmroads_cached_items('Cameras', load)


def fetch_nmroads_signs():
    def load():
        payload = nmroads_json(NMROADS_LIVE_API_ROOT, 'GetMessageSigns')
        signs = []
        for item in payload.get('messageSigns') or []:
            lat = optional_float(item.get('latitude'))
            lon = optional_float(item.get('longitude'))
            raw_name = str(item.get('name') or item.get('description') or '').strip()
            if lat is None or lon is None or not raw_name or not point_in_state('NM', lat, lon):
                continue
            signs.append({
                'id': str(wv511_numeric_id(f'NMROADS:SIGN:{raw_name}')),
                'name': strip_tags(item.get('description')) or raw_name,
                'lat': lat,
                'lon': lon,
                'message': re.sub(r'\s+', ' ', strip_tags(item.get('signText'))).strip() or 'No active message',
                'updated_at': epoch_milliseconds_iso(item.get('updateTime')) if safe_float(item.get('updateTime')) > 0 else None,
                'grouping': display_text(item.get('grouping')) or None,
            })
        return signs
    return nmroads_cached_items('MessageSigns', load)


NMROADS_EVENT_LABELS = {
    5: 'Road closure',
    6: 'Crash',
    7: 'Traffic alert',
    8: 'Lane closure',
    9: 'Roadwork',
    13: 'Fair driving conditions',
    14: 'Weather advisory',
    16: 'Difficult driving conditions',
    17: 'Severe driving conditions',
    18: 'Special event',
    19: 'Construction closure',
    20: 'Seasonal closure',
    21: 'Signal power outage',
}
NMROADS_CONSTRUCTION_EVENT_TYPES = {8, 9, 19}


def fetch_nmroads_events():
    def load():
        payload = nmroads_json(NMROADS_API_ROOT, 'GetEventsJSON', {
            'eventType': ','.join(str(code) for code in NMROADS_EVENT_LABELS),
            'returnData': 'all',
        }, timeout=45)
        events = []
        for item in payload.get('events') or []:
            event_type = safe_int(item.get('eventType'))
            if event_type not in NMROADS_EVENT_LABELS:
                continue
            x = optional_float(item.get('longitude'))
            y = optional_float(item.get('latitude'))
            if x is None or y is None:
                continue
            lat, lon = web_mercator_to_latlon(x, y)
            if not point_in_state('NM', lat, lon):
                continue
            raw_id = str(item.get('GUID') or item.get('incidentId') or '').strip()
            if not raw_id:
                continue
            label = NMROADS_EVENT_LABELS[event_type]
            title = re.sub(r'\s+', ' ', strip_tags(item.get('title'))).strip() or label
            description = re.sub(r'\s+', ' ', strip_tags(item.get('description'))).strip() or title
            events.append({
                'id': str(wv511_numeric_id(f'NMROADS:EVENT:{raw_id}')),
                'name': title,
                'lat': lat,
                'lon': lon,
                'layer': 'Construction' if event_type in NMROADS_CONSTRUCTION_EVENT_TYPES else 'Incidents',
                'description': description,
                'severity': label,
                'timestamp': epoch_milliseconds_iso(item.get('updatedateEpoc')),
                'location': ' '.join(part for part in (
                    display_text(item.get('routeName')),
                    str(item.get('routeNumber') or '').strip(),
                ) if part) or None,
            })
        return events
    return nmroads_cached_items('Events', load)


def nmroads_records(layer):
    if layer == 'Cameras':
        return fetch_nmroads_cameras()
    if layer == 'MessageSigns':
        return fetch_nmroads_signs()
    if layer in {'Incidents', 'Construction'}:
        return [item for item in fetch_nmroads_events() if item['layer'] == layer]
    return []


def nmroads_layer_payload(layer):
    records = nmroads_records(layer)
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': False,
                'videoId': None,
                'videoUrl': None,
                'snapshotUrl': f'/camera-snapshot/NM/{camera["id"]}',
                'snapshotFromVideo': False,
            },
        } for camera in records]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': sign['updated_at']},
        } for sign in records]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': item['severity'],
            'description': item['description'],
            'severity': item['severity'],
            'timestamp': item['timestamp'],
            'location': item['location'],
        },
    } for item in records]}


def nmroads_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_nmroads_cameras():
        if camera['id'] != target:
            continue
        detail = ' · '.join(part for part in (
            camera.get('camera_type'), camera.get('grouping'),
            f'District {camera["district"]}' if camera.get('district') else None,
        ) if part)
        return {
            'name': camera['name'],
            'msg': detail or 'NMDOT roadside camera',
            'severity': None,
            'timestamp': None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/NM/{target}',
            'upstream_snapshot_url': camera['snapshot_url'],
        }
    raise ValueError(f'NMRoads camera {site_id} not found')


def nmroads_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return nmroads_camera_detail(target)
    for item in nmroads_records(layer):
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': item.get('grouping'),
                'timestamp': item['updated_at'],
                'location': None,
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item['severity'],
            'timestamp': item['timestamp'],
            'location': item['location'],
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'NMRoads {layer} item {item_id} not found')


def caltrans_quickmap_headers(accept='application/vnd.google-earth.kml+xml,application/xml'):
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': CALTRANS_QUICKMAP_SOURCE_URL,
        'Accept': accept,
    }


def caltrans_quickmap_html_text(value):
    with_breaks = re.sub(r'<br\s*/?>', ' / ', str(value or ''), flags=re.I)
    return re.sub(r'\s*/\s*(?:/\s*)+', ' / ', strip_tags(with_breaks)).strip(' /')


def caltrans_quickmap_fragment(description, class_name, tag='(?:h2|p|div|span)'):
    match = re.search(
        rf'<{tag}[^>]*class=["\'][^"\']*\b{re.escape(class_name)}\b[^"\']*["\'][^>]*>'
        rf'(.*?)</(?:h2|p|div|span)>',
        str(description or ''),
        re.I | re.S,
    )
    return caltrans_quickmap_html_text(match.group(1)) if match else ''


def caltrans_quickmap_url(value, kind):
    candidate = html.unescape(str(value or '').strip())
    parsed = urllib.parse.urlparse(candidate)
    hostname = str(parsed.hostname or '').lower()
    if parsed.scheme != 'https':
        return ''
    if kind == 'snapshot':
        if not (
            re.fullmatch(r'cwwp\d*\.dot\.ca\.gov', hostname) and
            '/cctv/image/' in parsed.path.lower() and
            parsed.path.lower().endswith(('.jpg', '.jpeg', '.png'))
        ):
            return ''
    elif kind == 'video':
        if not (
            hostname == 'wzmedia.dot.ca.gov' and
            parsed.path.lower().endswith('.m3u8')
        ):
            return ''
    else:
        return ''
    return candidate


def caltrans_quickmap_coordinates(placemark, namespace):
    point_nodes = placemark.findall('.//k:Point/k:coordinates', namespace)
    line_nodes = placemark.findall('.//k:LineString/k:coordinates', namespace)
    for node in point_nodes + line_nodes:
        points = []
        for raw_point in str(node.text or '').split():
            pieces = raw_point.split(',')
            if len(pieces) < 2:
                continue
            lon = optional_float(pieces[0])
            lat = optional_float(pieces[1])
            if lon is not None and lat is not None:
                points.append((lat, lon))
        if not points:
            continue
        candidates = points if node in point_nodes else (
            points[len(points) // 2], points[0], points[-1]
        )
        for lat, lon in candidates:
            if point_in_state('CA', lat, lon):
                return lat, lon
    return None


def parse_caltrans_quickmap_kml(filename):
    now = time.time()
    with CALTRANS_QUICKMAP_CACHE_LOCK:
        cached = CALTRANS_QUICKMAP_CACHE.get(filename)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        url = f'{CALTRANS_QUICKMAP_DATA_ROOT}/{urllib.parse.quote(filename)}'
        req = urllib.request.Request(url, headers=caltrans_quickmap_headers())
        raw = urllib.request.urlopen(req, timeout=45).read()
        root = ET.fromstring(raw)
        namespace = {'k': 'http://www.opengis.net/kml/2.2'}
        items = []
        for placemark in root.findall('.//k:Placemark', namespace):
            coordinates = caltrans_quickmap_coordinates(placemark, namespace)
            if not coordinates:
                continue
            lat, lon = coordinates
            description = placemark.findtext('k:description', default='', namespaces=namespace)
            style = str(
                placemark.findtext('k:styleUrl', default='', namespaces=namespace) or ''
            ).strip().lower()
            title = caltrans_quickmap_fragment(description, 'iw-title', tag='h2')
            paragraphs = [
                caltrans_quickmap_html_text(fragment)
                for fragment in re.findall(
                    r'<p[^>]*class=["\'][^"\']*\biw-text\b[^"\']*["\'][^>]*>(.*?)</p>',
                    str(description or ''),
                    re.I | re.S,
                )
            ]
            paragraphs = list(dict.fromkeys(part for part in paragraphs if part))
            timestamp = caltrans_quickmap_fragment(description, 'iw-timestamp', tag='span')
            timestamp = re.sub(r'^Last updated:\s*', '', timestamp, flags=re.I).strip() or None
            snapshot_match = re.search(
                r'(?:poster|src)=["\'](https://[^"\']+/cctv/image/[^"\']+)["\']',
                str(description or ''),
                re.I,
            )
            video_match = re.search(
                r'<source[^>]+src=["\'](https://[^"\']+\.m3u8(?:\?[^"\']*)?)["\']',
                str(description or ''),
                re.I,
            )
            message_frames = [
                caltrans_quickmap_html_text(fragment)
                for fragment in re.findall(
                    r'<div[^>]*class=["\'][^"\']*\bcms[12]\b[^"\']*["\'][^>]*>(.*?)</div>',
                    str(description or ''),
                    re.I | re.S,
                )
            ]
            message = ' · '.join(dict.fromkeys(part for part in message_frames if part))
            stable_key = f'{filename}|{style}|{title}|{lat:.5f}|{lon:.5f}'
            items.append({
                'id': str(wv511_numeric_id(f'CALTRANS:{stable_key}')),
                'name': title or 'Caltrans traveler information',
                'lat': lat,
                'lon': lon,
                'style': style,
                'description': ' · '.join(paragraphs) or title or 'Caltrans traveler information',
                'message': message or ('No active message' if style == '#cms_empty' else None),
                'timestamp': timestamp,
                'snapshot_url': caltrans_quickmap_url(
                    snapshot_match.group(1) if snapshot_match else '', 'snapshot'
                ),
                'video_url': caltrans_quickmap_url(
                    video_match.group(1) if video_match else '', 'video'
                ),
            })
    except Exception:
        if cached:
            return cached['items']
        raise
    with CALTRANS_QUICKMAP_CACHE_LOCK:
        CALTRANS_QUICKMAP_CACHE[filename] = {
            'items': items,
            'expires_at': time.time() + CALTRANS_QUICKMAP_CACHE_TTL,
        }
    return items


def caltrans_quickmap_records(layer):
    files = CALTRANS_QUICKMAP_LAYER_FILES.get(layer) or ()
    records = [item for filename in files for item in parse_caltrans_quickmap_kml(filename)]
    if layer == 'Cameras':
        return [item for item in records if item['style'] != '#cctv-oos']
    if layer == 'Construction':
        return [item for item in records if not item['style'].startswith('#srra')]
    return records


def caltrans_quickmap_proxy_stream_url(value):
    video_url = caltrans_quickmap_url(value, 'video')
    if not video_url:
        return ''
    parsed = urllib.parse.urlparse(video_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(('_am_state', 'CA'))
    return f'/stream/{parsed.netloc}{parsed.path}?{urllib.parse.urlencode(query)}'


def caltrans_quickmap_layer_payload(layer):
    records = caltrans_quickmap_records(layer)
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': bool(camera['video_url']),
                'videoId': camera['id'],
                'videoUrl': caltrans_quickmap_proxy_stream_url(camera['video_url']) or None,
                'snapshotUrl': f'/camera-snapshot/CA/{camera["id"]}' if camera['snapshot_url'] else None,
                'snapshotFromVideo': False,
            },
        } for camera in records]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': sign['timestamp']},
        } for sign in records]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': (
                'Construction' if layer == 'Construction' else
                'Emergency' if item['style'] == '#emergency' else
                'Road closure' if item['style'] == '#full-closure' else
                'Highway information' if item['style'] == '#chin' else
                'CHP incident'
            ),
            'description': item['description'],
            'severity': item['style'].lstrip('#').replace('-', ' ').title() or None,
            'timestamp': item['timestamp'],
            'location': None,
        },
    } for item in records]}


def caltrans_quickmap_camera_detail(site_id):
    target = str(site_id)
    for camera in caltrans_quickmap_records('Cameras'):
        if camera['id'] != target:
            continue
        stream_url = caltrans_quickmap_proxy_stream_url(camera['video_url'])
        return {
            'name': camera['name'],
            'msg': camera['description'] or 'Caltrans traffic camera',
            'severity': None,
            'timestamp': camera['timestamp'],
            'video_id': target,
            'video_url': stream_url or None,
            'video_enabled': bool(stream_url),
            'snapshot_url': f'/camera-snapshot/CA/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
        }
    raise ValueError(f'Caltrans QuickMap camera {site_id} not found')


def caltrans_quickmap_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return caltrans_quickmap_camera_detail(target)
    for item in caltrans_quickmap_records(layer):
        if item['id'] != target:
            continue
        return {
            'name': item['name'],
            'msg': item['message'] if layer == 'MessageSigns' else item['description'],
            'severity': item['style'].lstrip('#').replace('-', ' ').title() or None,
            'timestamp': item['timestamp'],
            'location': None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'Caltrans QuickMap {layer} item {item_id} not found')


def fetch_nvroads_dataset(source_layer):
    content = fetch_iteris_layer(REGIONS['NV'], source_layer)
    payload = json.loads(content.decode('utf-8-sig'))
    items = payload.get('item2') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError(f'Nevada 511 {source_layer} returned no item array')
    return items


def nvroads_item_id(layer, source_layer, raw_id):
    if layer in {'Cameras', 'MessageSigns'}:
        return str(raw_id)
    return str(wv511_numeric_id(f'NVROADS:{source_layer}:{raw_id}'))


def nvroads_items_for_layer(layer):
    return [
        (source_layer, item)
        for source_layer in NVROADS_LAYER_SOURCES.get(layer, ())
        for item in fetch_nvroads_dataset(source_layer)
        if len(item.get('location') or []) >= 2 and point_in_state(
            'NV',
            safe_float(item['location'][0]),
            safe_float(item['location'][1]),
        )
    ]


def nvroads_layer_payload(layer):
    if layer not in NVROADS_LAYER_SOURCES:
        return {'item2': []}
    normalized = []
    for source_layer, item in nvroads_items_for_layer(layer):
        raw_id = str(item.get('itemId') or '').strip()
        location = item.get('location') or []
        if not raw_id.isdigit() or len(location) < 2:
            continue
        if layer == 'Cameras':
            expando = {
                'videoEnabled': False,
                'videoId': raw_id,
                'snapshotUrl': f'/camera-snapshot/NV/{raw_id}',
            }
            title = display_text(item.get('title')) or ''
        elif layer == 'MessageSigns':
            expando = {'message': '', 'timestamp': None}
            title = display_text(item.get('title')) or 'Nevada 511 message sign'
        else:
            label = NVROADS_LAYER_LABELS.get(source_layer, 'Nevada 511 traffic event')
            expando = {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': label,
                'severity': label,
                'timestamp': None,
                'location': None,
            }
            title = label
        normalized.append({
            'itemId': nvroads_item_id(layer, source_layer, raw_id),
            'location': [safe_float(location[0]), safe_float(location[1])],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def fetch_nvroads_tooltip_html(source_layer, raw_id):
    cache_key = f'{source_layer}:{raw_id}'
    now = time.time()
    with NVROADS_TOOLTIP_CACHE_LOCK:
        cached = NVROADS_TOOLTIP_CACHE.get(cache_key)
        if cached and now < cached['expires_at']:
            return cached['html']
    req = urllib.request.Request(
        f'https://www.nvroads.com/tooltip/{source_layer}/{raw_id}?lang=en',
        headers=traffic_headers(REGIONS['NV'], accept='text/html,application/xhtml+xml'),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    with NVROADS_TOOLTIP_CACHE_LOCK:
        if len(NVROADS_TOOLTIP_CACHE) >= 3000:
            NVROADS_TOOLTIP_CACHE.pop(next(iter(NVROADS_TOOLTIP_CACHE)))
        NVROADS_TOOLTIP_CACHE[cache_key] = {
            'html': raw,
            'expires_at': time.time() + NVROADS_TOOLTIP_CACHE_TTL,
        }
    return raw


def nvroads_camera_detail(site_id):
    target = str(site_id)
    camera_ids = {
        str(item.get('itemId') or '').strip()
        for item in fetch_nvroads_dataset('Cameras')
    }
    if target not in camera_ids:
        raise ValueError(f'Nevada 511 camera {site_id} not found')
    raw = fetch_nvroads_tooltip_html('Cameras', target)
    common = _parse_511_tooltip_html(raw)
    direction_match = re.search(
        r'<div[^>]+class=["\'][^"\']*\bdirDescHeader\b[^"\']*["\'][^>]*>(.*?)</div>',
        raw,
        re.I | re.S,
    )
    direction = pa511_html_text(direction_match.group(1)) if direction_match else ''
    name = display_text(common.get('name'))
    if not name or name.casefold() == 'n/a':
        name = direction or f'Nevada DOT camera {target}'
    video_url = str(common.get('video_url') or '').strip()
    parsed_video = urllib.parse.urlparse(video_url)
    if not (
        parsed_video.scheme == 'https' and
        allowed_stream_host(parsed_video.hostname) and
        parsed_video.path.lower().endswith('.m3u8')
    ):
        video_url = ''
    upstream_snapshot_url = urllib.parse.urljoin(
        NVROADS_SOURCE_URL,
        str(common.get('snapshot_url') or f'/map/Cctv/{target}'),
    )
    parsed_snapshot = urllib.parse.urlparse(upstream_snapshot_url)
    if not (
        parsed_snapshot.scheme == 'https' and
        parsed_snapshot.hostname == 'www.nvroads.com' and
        parsed_snapshot.path == f'/map/Cctv/{target}'
    ):
        upstream_snapshot_url = ''
    return {
        'name': name,
        'msg': direction or 'Nevada DOT traffic camera',
        'severity': None,
        'timestamp': common.get('timestamp'),
        'video_id': common.get('video_id') or target,
        'video_url': video_url or None,
        'video_enabled': bool(video_url),
        'snapshot_url': f'/camera-snapshot/NV/{target}' if upstream_snapshot_url else None,
        'upstream_snapshot_url': upstream_snapshot_url or None,
    }


def nvroads_find_item(layer, item_id):
    target = str(item_id)
    for source_layer, item in nvroads_items_for_layer(layer):
        raw_id = str(item.get('itemId') or '').strip()
        if nvroads_item_id(layer, source_layer, raw_id) == target:
            return source_layer, raw_id
    raise ValueError(f'Nevada 511 {layer} item {item_id} not found')


def nvroads_tooltip(layer, item_id):
    if layer == 'Cameras':
        return nvroads_camera_detail(item_id)
    source_layer, raw_id = nvroads_find_item(layer, item_id)
    raw = fetch_nvroads_tooltip_html(source_layer, raw_id)
    common = _parse_511_tooltip_html(raw)
    heading_match = re.search(r'<h4[^>]*>(.*?)</h4>', raw, re.I | re.S)
    heading = pa511_html_text(heading_match.group(1)) if heading_match else ''
    if layer == 'MessageSigns':
        message_match = re.search(
            r'<td[^>]+class=["\']msgContent["\'][^>]*>(.*?)</td>', raw, re.I | re.S
        )
        return {
            'name': common.get('name') or 'Nevada 511 message sign',
            'msg': pa511_html_text(message_match.group(1)) if message_match else common.get('msg'),
            'severity': None,
            'timestamp': common.get('timestamp'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    label = NVROADS_LAYER_LABELS.get(source_layer, heading or 'Nevada 511 event')
    return {
        'name': heading or label,
        'msg': common.get('msg') or label,
        'severity': label,
        'timestamp': common.get('timestamp'),
        'location': None,
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def fetch_tripcheck_or_dataset(dataset):
    filename = TRIPCHECK_OR_DATASETS.get(dataset)
    if not filename:
        return []
    now = time.time()
    with TRIPCHECK_OR_CACHE_LOCK:
        cached = TRIPCHECK_OR_CACHE.get(dataset)
        if cached and now < cached['expires_at']:
            return cached['features']
    try:
        data = fetch_json_url(
            f'{TRIPCHECK_OR_DATA_ROOT}/{filename}',
            headers={
                'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
                'Referer': TRIPCHECK_OR_SOURCE_URL,
                'Accept': 'application/json,text/javascript,*/*',
            },
            timeout=30,
        )
        features = data.get('features') if isinstance(data, dict) else None
        if not isinstance(features, list):
            raise ValueError(f'Oregon TripCheck {dataset} returned no feature array')
    except Exception:
        if cached:
            return cached['features']
        raise
    with TRIPCHECK_OR_CACHE_LOCK:
        TRIPCHECK_OR_CACHE[dataset] = {
            'features': features,
            'expires_at': time.time() + TRIPCHECK_OR_CACHE_TTL,
        }
    return features


def tripcheck_or_camera_record(site_id):
    target = str(site_id)
    for feature in fetch_tripcheck_or_dataset('Cameras'):
        attrs = feature.get('attributes') or {}
        if str(attrs.get('cameraId') or '') != target:
            continue
        lat = optional_float(attrs.get('latitude'))
        lon = optional_float(attrs.get('longitude'))
        filename = str(attrs.get('filename') or '').strip()
        if (
            lat is None or lon is None or not point_in_state('OR', lat, lon) or
            '/' in filename or '\\' in filename or
            not re.search(r'\.(?:jpe?g|png|webp)$', filename, re.I)
        ):
            break
        return attrs, urllib.parse.urljoin(
            TRIPCHECK_OR_CAMERA_ROOT,
            urllib.parse.quote(filename),
        )
    raise ValueError(f'Oregon TripCheck camera {site_id} not found')


def tripcheck_or_event_records(layer):
    if layer not in {'Incidents', 'Construction'}:
        return []
    records = []
    for feature in fetch_tripcheck_or_dataset(layer):
        attrs = feature.get('attributes') or {}
        event_id = str(attrs.get('incidentId') or '').strip()
        lat = optional_float(attrs.get('startLatitude'))
        lon = optional_float(attrs.get('startLongitude'))
        if (
            not event_id.isdigit() or lat is None or lon is None or
            not point_in_state('OR', lat, lon)
        ):
            continue
        records.append((event_id, lat, lon, attrs))
    return records


def tripcheck_or_event_text(attrs):
    parts = []
    for value in (
        attrs.get('eventSubTypeName'),
        attrs.get('comments'),
        attrs.get('tmddOther'),
    ):
        text = display_text(value)
        if text and text not in parts:
            parts.append(text)
    lanes = [display_text(value) for value in (attrs.get('lanesAffected') or [])]
    lanes = [value for value in lanes if value]
    if lanes:
        parts.append('Lanes affected: ' + ', '.join(lanes))
    return ' · '.join(parts) or display_text(attrs.get('eventTypeName')) or 'Traffic event'


def tripcheck_or_layer_payload(layer):
    if layer == 'Cameras':
        items = []
        for feature in fetch_tripcheck_or_dataset('Cameras'):
            attrs = feature.get('attributes') or {}
            camera_id = str(attrs.get('cameraId') or '').strip()
            lat = optional_float(attrs.get('latitude'))
            lon = optional_float(attrs.get('longitude'))
            filename = str(attrs.get('filename') or '').strip()
            if (
                not camera_id.isdigit() or lat is None or lon is None or
                not point_in_state('OR', lat, lon) or
                '/' in filename or '\\' in filename or
                not re.search(r'\.(?:jpe?g|png|webp)$', filename, re.I)
            ):
                continue
            items.append({
                'itemId': camera_id,
                'location': [lat, lon],
                'title': display_text(attrs.get('title')) or f'Oregon DOT camera {camera_id}',
                'expando': {
                    'videoEnabled': False,
                    'videoId': camera_id,
                    'snapshotUrl': f'/camera-snapshot/OR/{camera_id}',
                    'snapshotFromVideo': False,
                },
            })
        return {'item2': items}
    if layer == 'MessageSigns':
        return {'item2': []}
    items = []
    for event_id, lat, lon, attrs in tripcheck_or_event_records(layer):
        event_name = display_text(attrs.get('eventTypeName')) or (
            'Construction' if layer == 'Construction' else 'Traffic incident'
        )
        route = display_text(attrs.get('route'))
        title = ' · '.join(part for part in (route, event_name) if part)
        items.append({
            'itemId': event_id,
            'location': [lat, lon],
            'title': title,
            'expando': {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': tripcheck_or_event_text(attrs),
                'severity': display_text(attrs.get('odotSeverityDescript')) or None,
                'timestamp': attrs.get('lastUpdated'),
                'location': display_text(attrs.get('beginMarker') or attrs.get('locationName')) or None,
            },
        })
    return {'item2': items}


def tripcheck_or_camera_detail(site_id):
    attrs, upstream_snapshot_url = tripcheck_or_camera_record(site_id)
    target = str(site_id)
    return {
        'name': display_text(attrs.get('title')) or f'Oregon DOT camera {target}',
        'msg': display_text(attrs.get('route')) or 'Oregon DOT TripCheck traffic camera',
        'severity': None,
        'timestamp': None,
        'video_id': target,
        'video_url': None,
        'video_enabled': False,
        'snapshot_url': f'/camera-snapshot/OR/{target}',
        'upstream_snapshot_url': upstream_snapshot_url,
    }


def tripcheck_or_tooltip(layer, item_id):
    if layer == 'Cameras':
        return tripcheck_or_camera_detail(item_id)
    target = str(item_id)
    for event_id, _lat, _lon, attrs in tripcheck_or_event_records(layer):
        if event_id != target:
            continue
        event_name = display_text(attrs.get('eventTypeName')) or 'Traffic event'
        route = display_text(attrs.get('route'))
        return {
            'name': ' · '.join(part for part in (route, event_name) if part),
            'msg': tripcheck_or_event_text(attrs),
            'severity': display_text(attrs.get('odotSeverityDescript')) or None,
            'timestamp': attrs.get('lastUpdated'),
            'location': display_text(attrs.get('beginMarker') or attrs.get('locationName')) or None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'Oregon TripCheck {layer} item {item_id} not found')


def tripcheck_or_measurement(value, suffix_pattern, minimum=None, maximum=None):
    match = re.search(r'-?\d+(?:\.\d+)?', str(value or ''))
    if not match or (suffix_pattern and not re.search(suffix_pattern, str(value or ''), re.I)):
        return None
    number = optional_float(match.group(0))
    if number is None:
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def fetch_tripcheck_or_road_weather():
    features = []
    for feature in fetch_tripcheck_or_dataset('RoadWeather'):
        attrs = feature.get('attributes') or {}
        station_id = str(attrs.get('roadWeatherReportID') or '').strip()
        lat = optional_float(attrs.get('latitude'))
        lon = optional_float(attrs.get('longitude'))
        if (
            not station_id.isdigit() or lat is None or lon is None or
            not point_in_state('OR', lat, lon) or
            str(attrs.get('opStatus') or 'Active').casefold() != 'active'
        ):
            continue
        air_temp = tripcheck_or_measurement(attrs.get('currTemp'), r'f\b', -100, 180)
        dew_point = tripcheck_or_measurement(attrs.get('dewPoint'), r'f\b', -120, 180)
        road_temp = tripcheck_or_measurement(attrs.get('roadTemp'), r'f\b', -100, 220)
        humidity = tripcheck_or_measurement(attrs.get('humidity'), r'%', 0, 100)
        wind_mph = tripcheck_or_measurement(attrs.get('windSpeed'), r'mph\b', 0, 250)
        gust_mph = tripcheck_or_measurement(attrs.get('windSpeedGust'), r'mph\b', 0, 300)
        precipitation = display_text(attrs.get('precip'))
        features.append({
            'attributes': {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'ORTRIPCHECK_{station_id}',
                'LATITUDE': lat,
                'LNGITUDE': lon,
                'LOCALNAM': display_text(attrs.get('tripcheckName') or attrs.get('locationName')) or f'TripCheck RWIS {station_id}',
                'OBS_TIME_LOCAL': attrs.get('updateTime'),
                'AIR_TEMP_F': air_temp,
                'DEW_POINT_F': dew_point,
                'RELATIVE_HUMIDITY': humidity,
                'WIND_DIRECTION': display_text(attrs.get('windDirection')) or None,
                'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
                'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
                'VISIBILITY': display_text(attrs.get('visibility')) or None,
                'ROAD_TEMP_F': road_temp,
                'SUBSURFACE_TEMP_F': None,
                'ROAD_STATE': precipitation or 'Road weather station',
                'PRECIP_1H_IN': tripcheck_or_measurement(attrs.get('rain1hr'), r'', 0, 20),
                'PRECIP_12H_IN': None,
                'PRECIP_24H_IN': None,
                'SOURCE_URL': TRIPCHECK_OR_SOURCE_URL,
            },
            'geometry': {'x': lon, 'y': lat},
        })
    if not features:
        raise ValueError('Oregon TripCheck returned no active road-weather stations')
    return features


def fetch_wsdot_dataset(dataset):
    endpoint = WSDOT_DATASETS.get(dataset)
    if not endpoint:
        return []
    now = time.time()
    with WSDOT_CACHE_LOCK:
        cached = WSDOT_CACHE.get(dataset)
        if cached and now < cached['expires_at']:
            return cached['features']
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': '*',
        'returnGeometry': 'true',
        'outSR': '4326',
        'f': 'geojson',
    })
    try:
        data = fetch_json_url(
            f'{WSDOT_DATA_ROOT}/{endpoint}?{params}',
            headers={
                'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
                'Referer': WSDOT_SOURCE_URL,
                'Accept': 'application/geo+json,application/json',
            },
            timeout=30,
        )
        features = data.get('features') if isinstance(data, dict) else None
        if not isinstance(features, list):
            raise ValueError(f'WSDOT {dataset} returned no feature array')
    except Exception:
        if cached:
            return cached['features']
        raise
    with WSDOT_CACHE_LOCK:
        WSDOT_CACHE[dataset] = {
            'features': features,
            'expires_at': time.time() + WSDOT_CACHE_TTL,
        }
    return features


def wsdot_point_record(feature):
    coordinates = (feature.get('geometry') or {}).get('coordinates') or []
    if len(coordinates) < 2:
        return None
    lon = optional_float(coordinates[0])
    lat = optional_float(coordinates[1])
    if lat is None or lon is None or not point_in_state('WA', lat, lon):
        return None
    return lat, lon, feature.get('properties') or {}


def wsdot_camera_record(site_id):
    target = str(site_id)
    for feature in fetch_wsdot_dataset('Cameras'):
        record = wsdot_point_record(feature)
        if not record:
            continue
        _lat, _lon, props = record
        if str(props.get('OBJECTID') or '') != target:
            continue
        image_url = str(props.get('ImageURL') or '').strip()
        parsed = urllib.parse.urlparse(image_url)
        if (
            parsed.scheme != 'https' or not parsed.hostname or
            parsed.username or parsed.password or
            not re.search(r'\.(?:jpe?g|png|webp)(?:$|\?)', parsed.path + (('?' + parsed.query) if parsed.query else ''), re.I)
        ):
            break
        return props, image_url
    raise ValueError(f'WSDOT camera {site_id} not found')


def wsdot_alert_records(layer):
    construction_categories = {'construction', 'maintenance', 'road work', 'lane closure'}
    records = []
    for feature in fetch_wsdot_dataset('Alerts'):
        record = wsdot_point_record(feature)
        if not record:
            continue
        lat, lon, props = record
        category = display_text(props.get('EventCategoryDescription')).casefold()
        is_construction = category in construction_categories
        if (layer == 'Construction') != is_construction:
            continue
        event_id = str(props.get('OBJECTID') or '').strip()
        if not event_id.isdigit():
            continue
        records.append((event_id, lat, lon, props))
    return records


def wsdot_layer_payload(layer):
    if layer == 'Cameras':
        items = []
        for feature in fetch_wsdot_dataset('Cameras'):
            record = wsdot_point_record(feature)
            if not record:
                continue
            lat, lon, props = record
            camera_id = str(props.get('OBJECTID') or '').strip()
            image_url = str(props.get('ImageURL') or '').strip()
            parsed = urllib.parse.urlparse(image_url)
            if not camera_id.isdigit() or parsed.scheme != 'https' or not parsed.hostname:
                continue
            items.append({
                'itemId': camera_id,
                'location': [lat, lon],
                'title': display_text(props.get('CameraTitle')) or f'WSDOT camera {camera_id}',
                'expando': {
                    'videoEnabled': False,
                    'videoId': camera_id,
                    'snapshotUrl': f'/camera-snapshot/WA/{camera_id}',
                    'snapshotFromVideo': False,
                },
            })
        return {'item2': items}
    if layer == 'MessageSigns':
        return {'item2': []}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    items = []
    for event_id, lat, lon, props in wsdot_alert_records(layer):
        category = display_text(props.get('EventCategoryDescription')) or 'Road alert'
        event_type = display_text(props.get('EventCategoryTypeDescription'))
        road = display_text(props.get('Road'))
        direction = display_text(props.get('RoadDirection'))
        items.append({
            'itemId': event_id,
            'location': [lat, lon],
            'title': ' · '.join(part for part in (road, direction, category) if part),
            'expando': {
                'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
                'description': display_text(props.get('HeadlineMessage')) or event_type or category,
                'severity': 'Road closed' if safe_int(props.get('RoadClosedFlag')) else event_type or category,
                'timestamp': epoch_milliseconds_iso(props.get('LastModifiedDate')),
                'location': ' '.join(part for part in (road, direction) if part) or None,
            },
        })
    return {'item2': items}


def wsdot_camera_detail(site_id):
    props, upstream_snapshot_url = wsdot_camera_record(site_id)
    target = str(site_id)
    return {
        'name': display_text(props.get('CameraTitle')) or f'WSDOT camera {target}',
        'msg': display_text(props.get('CompassDirection')) or 'WSDOT traffic camera',
        'severity': None,
        'timestamp': None,
        'video_id': target,
        'video_url': None,
        'video_enabled': False,
        'snapshot_url': f'/camera-snapshot/WA/{target}',
        'upstream_snapshot_url': upstream_snapshot_url,
    }


def wsdot_tooltip(layer, item_id):
    if layer == 'Cameras':
        return wsdot_camera_detail(item_id)
    target = str(item_id)
    for event_id, _lat, _lon, props in wsdot_alert_records(layer):
        if event_id != target:
            continue
        category = display_text(props.get('EventCategoryDescription')) or 'Road alert'
        event_type = display_text(props.get('EventCategoryTypeDescription'))
        road = display_text(props.get('Road'))
        direction = display_text(props.get('RoadDirection'))
        return {
            'name': ' · '.join(part for part in (road, direction, category) if part),
            'msg': display_text(props.get('HeadlineMessage')) or event_type or category,
            'severity': 'Road closed' if safe_int(props.get('RoadClosedFlag')) else event_type or category,
            'timestamp': epoch_milliseconds_iso(props.get('LastModifiedDate')),
            'location': ' '.join(part for part in (road, direction) if part) or None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'WSDOT {layer} item {item_id} not found')


def fetch_wsdot_road_weather():
    features = []
    for feature in fetch_wsdot_dataset('RoadWeather'):
        record = wsdot_point_record(feature)
        if not record:
            continue
        lat, lon, props = record
        station_id = str(props.get('OBJECTID') or '').strip()
        surface_c = optional_float(props.get('SurfaceTemperature'))
        if not station_id.isdigit() or surface_c is None or not -80 <= surface_c <= 100:
            continue
        air_temp = optional_float(props.get('TemperatureFarhenheit'))
        wind_mph = optional_float(props.get('WindSpeed'))
        features.append({
            'attributes': {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'WSDOT_{station_id}',
                'LATITUDE': lat,
                'LNGITUDE': lon,
                'LOCALNAM': display_text(props.get('WeatherStationDescription')) or f'WSDOT weather station {station_id}',
                'OBS_TIME_LOCAL': epoch_milliseconds_iso(props.get('WeatherReportDateTime')),
                'AIR_TEMP_F': air_temp if air_temp is not None and -100 <= air_temp <= 180 else None,
                'DEW_POINT_F': None,
                'RELATIVE_HUMIDITY': None,
                'WIND_DIRECTION': display_text(props.get('CardinalCompassDirection')) or None,
                'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None and 0 <= wind_mph <= 250 else None,
                'WIND_GUST_KTS': None,
                'VISIBILITY': display_text(props.get('Visibility')) or None,
                'ROAD_TEMP_F': round(surface_c * 9 / 5 + 32, 1),
                'SUBSURFACE_TEMP_F': None,
                'ROAD_STATE': 'Road weather station',
                'PRECIP_1H_IN': None,
                'PRECIP_12H_IN': None,
                'PRECIP_24H_IN': None,
                'SOURCE_URL': WSDOT_SOURCE_URL,
            },
            'geometry': {'x': lon, 'y': lat},
        })
    if not features:
        raise ValueError('WSDOT returned no road-surface weather stations')
    return features


def fetch_nmroads_road_weather():
    rwis_items = nmroads_json(NMROADS_LIVE_API_ROOT, 'GetCachedObject', {'key': 'RWISData'})
    rwis_by_name = {
        str(item.get('name') or ''): str(item.get('text') or '')
        for item in rwis_items if isinstance(item, dict)
    }
    features = []
    for camera in fetch_nmroads_cameras():
        raw = rwis_by_name.get(camera['raw_name'])
        if not raw:
            continue
        temp_match = re.search(r'Temperature:\s*(-?\d+(?:\.\d+)?)\s*F', raw, re.I)
        humidity_match = re.search(r'Humidity:\s*(\d+(?:\.\d+)?)%', raw, re.I)
        wind_match = re.search(r'Wind:\s*([A-Z]+)\s+at\s+(\d+(?:\.\d+)?)\s*MPH', raw, re.I)
        observed = raw.splitlines()[0].strip() if raw.splitlines() else None
        features.append({
            'attributes': {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'NMROADS_{camera["id"]}',
                'LATITUDE': camera['lat'],
                'LNGITUDE': camera['lon'],
                'LOCALNAM': camera['name'],
                'OBS_TIME_LOCAL': observed,
                'AIR_TEMP_F': optional_float(temp_match.group(1)) if temp_match else None,
                'DEW_POINT_F': None,
                'RELATIVE_HUMIDITY': optional_float(humidity_match.group(1)) if humidity_match else None,
                'WIND_DIRECTION': wind_match.group(1) if wind_match else None,
                'WIND_SPEED_KTS': round(optional_float(wind_match.group(2)) / 1.15078, 1) if wind_match else None,
                'WIND_GUST_KTS': None,
                'VISIBILITY': None,
                'ROAD_TEMP_F': None,
                'SUBSURFACE_TEMP_F': None,
                'ROAD_STATE': 'Current roadside weather',
                'PRECIP_1H_IN': None,
                'PRECIP_12H_IN': None,
                'PRECIP_24H_IN': None,
                'SOURCE_URL': NMROADS_SOURCE_URL,
            },
            'geometry': {'x': camera['lon'], 'y': camera['lat']},
        })
    if not features:
        raise ValueError('NMRoads returned no current RWIS observations')
    return features


def oktraffic_metric(value, minimum, maximum):
    number = optional_float(value)
    return number if number is not None and minimum <= number <= maximum else None


def oktraffic_precipitation_label(value):
    code = safe_int(value, default=-1)
    if code == 0:
        return 'No precipitation reported'
    return f'Precipitation code {code}' if code >= 0 else 'Unknown'


def fetch_oktraffic_road_weather():
    features = []
    odot = oktraffic_json('OdotRwisStations', {
        'include': {
            'relation': 'odotRwisStationData',
            'scope': {'order': 'updatedAt DESC', 'limit': 1},
        },
    })
    for station in odot if isinstance(odot, list) else []:
        lat = optional_float(station.get('latitude'))
        lon = optional_float(station.get('longitude'))
        raw_id = str(station.get('id') or '').strip()
        if lat is None or lon is None or not raw_id or not point_in_state('OK', lat, lon):
            continue
        data = ((station.get('odotRwisStationData') or [{}])[0] or {})
        air_temp = oktraffic_metric(data.get('temperature'), -80, 180)
        surface_values = [
            oktraffic_metric(data.get('surfaceTemperature1'), -80, 200),
            oktraffic_metric(data.get('surfaceTemperature2'), -80, 200),
        ]
        surface_values = [value for value in surface_values if value is not None]
        road_temp = round(sum(surface_values) / len(surface_values), 1) if surface_values else None
        wind_mph = oktraffic_metric(data.get('windSpeed'), 0, 250)
        gust_mph = oktraffic_metric(data.get('gustWindSpeed'), 0, 300)
        attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': f'OKTRAFFIC_{raw_id}',
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': display_text(station.get('shortName') or station.get('name')) or f'OKTraffic RWIS {raw_id}',
            'OBS_TIME_LOCAL': data.get('updatedAt') or station.get('updatedAt'),
            'AIR_TEMP_F': air_temp,
            'DEW_POINT_F': None,
            'RELATIVE_HUMIDITY': None,
            'WIND_DIRECTION': display_text(data.get('windDirection')) or None,
            'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
            'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
            'VISIBILITY': None,
            'ROAD_TEMP_F': road_temp,
            'SUBSURFACE_TEMP_F': oktraffic_metric(data.get('temperatureProbe1'), -80, 200),
            'ROAD_STATE': oktraffic_precipitation_label(data.get('precipitationType')),
            'PRECIP_1H_IN': None,
            'PRECIP_12H_IN': None,
            'PRECIP_24H_IN': None,
            'SOURCE_URL': OKTRAFFIC_SOURCE_URL,
        }
        features.append({'attributes': attrs, 'geometry': {'x': lon, 'y': lat}})

    tulsa = oktraffic_json('RwisStations', {
        'include': ['lastRwisStationData', 'rwisStationLocation'],
    })
    for station in tulsa if isinstance(tulsa, list) else []:
        location = station.get('rwisStationLocation') or {}
        data = station.get('lastRwisStationData') or {}
        lat = optional_float(location.get('latitude'))
        lon = optional_float(location.get('longitude'))
        raw_id = str(station.get('id') or '').strip()
        if lat is None or lon is None or not raw_id or not point_in_state('OK', lat, lon):
            continue
        air_c = oktraffic_metric(data.get('airTemperature'), -80, 80)
        road_c = oktraffic_metric(data.get('surfaceTemperature'), -80, 100)
        dew_c = oktraffic_metric(data.get('dewPointTemperature'), -100, 80)
        wind_ms = oktraffic_metric(data.get('windSpeed'), 0, 100)
        gust_ms = oktraffic_metric(data.get('maxWindSpeed'), 0, 120)
        surface_code = str(data.get('surfaceState') or '').strip()
        attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': f'OKTRAFFIC_TULSA_{raw_id}',
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': display_text(station.get('name') or location.get('shortName')) or f'Tulsa RWIS {raw_id}',
            'OBS_TIME_LOCAL': data.get('updatedAt') or station.get('updatedAt'),
            'AIR_TEMP_F': round(air_c * 9 / 5 + 32, 1) if air_c is not None else None,
            'DEW_POINT_F': round(dew_c * 9 / 5 + 32, 1) if dew_c is not None else None,
            'RELATIVE_HUMIDITY': oktraffic_metric(data.get('relativeHumidity'), 0, 100),
            'WIND_DIRECTION': display_text(data.get('windDirection')) or None,
            'WIND_SPEED_KTS': round(wind_ms * 1.94384, 1) if wind_ms is not None else None,
            'WIND_GUST_KTS': round(gust_ms * 1.94384, 1) if gust_ms is not None else None,
            'VISIBILITY': oktraffic_metric(data.get('visibility'), 0, 100000),
            'ROAD_TEMP_F': round(road_c * 9 / 5 + 32, 1) if road_c is not None else None,
            'SUBSURFACE_TEMP_F': None,
            'ROAD_STATE': f'Surface state code {surface_code}' if surface_code else 'Unknown',
            'PRECIP_1H_IN': None,
            'PRECIP_12H_IN': None,
            'PRECIP_24H_IN': None,
            'SOURCE_URL': OKTRAFFIC_SOURCE_URL,
        }
        features.append({'attributes': attrs, 'geometry': {'x': lon, 'y': lat}})
    if not features:
        raise ValueError('OKTraffic returned no road-weather stations')
    return features


def idot_il_headers():
    return {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': IDOT_IL_SOURCE_URL,
        'Accept': 'application/json',
    }


def fetch_idot_il_dataset(kind):
    url = IDOT_IL_TRAFFIC_URLS.get(kind)
    if not url:
        return []
    now = time.time()
    with IDOT_IL_TRAFFIC_CACHE_LOCK:
        cached = IDOT_IL_TRAFFIC_CACHE.get(kind)
        if cached and now < cached['expires_at']:
            return cached['items']

    items = []
    offset = 0
    oid_field = IDOT_IL_OBJECT_ID_FIELDS[kind]
    try:
        while True:
            params = urllib.parse.urlencode({
                'where': '1=1',
                'outFields': '*',
                'returnGeometry': 'true',
                'outSR': '4326',
                'orderByFields': f'{oid_field} ASC',
                'resultOffset': offset,
                'resultRecordCount': 1000,
                'f': 'json',
            })
            payload = fetch_json_url(
                f'{url}?{params}', headers=idot_il_headers(), timeout=35
            )
            if payload.get('error'):
                raise ValueError(
                    f'Illinois DOT ArcGIS error: {payload["error"].get("message") or payload["error"]}'
                )
            page = payload.get('features') or []
            items.extend(page)
            if not page or not payload.get('exceededTransferLimit'):
                break
            offset += len(page)
    except Exception:
        if cached:
            return cached['items']
        raise

    with IDOT_IL_TRAFFIC_CACHE_LOCK:
        IDOT_IL_TRAFFIC_CACHE[kind] = {
            'items': items,
            'expires_at': time.time() + IDOT_IL_CACHE_TTL,
        }
    return items


def idot_il_point(feature):
    geometry = feature.get('geometry') or {}
    lon = optional_float(geometry.get('x'))
    lat = optional_float(geometry.get('y'))
    if lat is None or lon is None:
        attrs = feature.get('attributes') or {}
        lon = optional_float(attrs.get('x') or attrs.get('Longitude'))
        lat = optional_float(attrs.get('y') or attrs.get('Latitude'))
    return (lat, lon) if lat is not None and lon is not None and point_in_state('IL', lat, lon) else None


def idot_il_path_point(feature):
    paths = (feature.get('geometry') or {}).get('paths') or []
    points = [point for path in paths for point in path if isinstance(point, list) and len(point) >= 2]
    if not points:
        return idot_il_point(feature)
    for point in (points[len(points) // 2], points[0], points[-1]):
        lon = optional_float(point[0])
        lat = optional_float(point[1])
        if lat is not None and lon is not None and point_in_state('IL', lat, lon):
            return lat, lon
    return None


def idot_il_snapshot_url(value):
    candidate = str(value or '').strip()
    parsed = urllib.parse.urlparse(candidate)
    if not (
        parsed.scheme == 'https' and
        parsed.hostname == 'cctv.travelmidwest.com' and
        parsed.path.startswith('/snapshots/') and
        parsed.path.lower().endswith(('.jpg', '.jpeg', '.png'))
    ):
        return ''
    return candidate


def fetch_idot_il_cameras():
    cameras = {}
    for feature in fetch_idot_il_dataset('Cameras'):
        attrs = feature.get('attributes') or {}
        coordinates = idot_il_point(feature)
        match = re.search(r'[?&]id=([^&]+)', str(attrs.get('ImgPath') or ''), re.I)
        if not coordinates or not match:
            continue
        raw_id = urllib.parse.unquote(match.group(1)).strip()
        if not raw_id:
            continue
        direction = display_text(attrs.get('CameraDirection'))
        snapshot_url = idot_il_snapshot_url(attrs.get('SnapShot'))
        usable = str(attrs.get('TooOld') or '').lower() != 'true'
        camera = cameras.setdefault(raw_id, {
            'id': wv511_numeric_id(f'IDOTIL:CAM:{raw_id}'),
            'raw_id': raw_id,
            'name': display_text(attrs.get('CameraLocation')) or f'Illinois DOT camera {raw_id}',
            'lat': coordinates[0],
            'lon': coordinates[1],
            'snapshot_url': '',
            'directions': [],
        })
        if direction and direction not in camera['directions']:
            camera['directions'].append(direction)
        if snapshot_url and (not camera['snapshot_url'] or usable):
            camera['snapshot_url'] = snapshot_url
    return list(cameras.values())


def idot_il_sign_message(attrs):
    messages = []
    for frame in range(3):
        lines = []
        for suffix in ('A', 'B', 'C'):
            value = display_text(attrs.get(f'Message{frame}{suffix}')).strip('[] ')
            value = re.sub(r'\s*,\s*', ' / ', value)
            if value:
                lines.append(value)
        message = ' / '.join(lines)
        if message and message not in messages:
            messages.append(message)
    return ' · '.join(messages) or 'No active message'


def fetch_idot_il_signs():
    signs = []
    for feature in fetch_idot_il_dataset('MessageSigns'):
        attrs = feature.get('attributes') or {}
        coordinates = idot_il_point(feature)
        raw_id = display_text(attrs.get('DeviceID') or attrs.get('FID'))
        if not coordinates or not raw_id:
            continue
        road = ' '.join(part for part in (
            display_text(attrs.get('RoadDirection')),
            display_text(attrs.get('RoadName')),
        ) if part)
        location = display_text(attrs.get('Location'))
        signs.append({
            'id': wv511_numeric_id(f'IDOTIL:SIGN:{raw_id}'),
            'name': location or road or 'Illinois DOT message sign',
            'lat': coordinates[0],
            'lon': coordinates[1],
            'message': idot_il_sign_message(attrs),
            'status': display_text(attrs.get('Status')) or None,
            'road': road or None,
        })
    return signs


def fetch_idot_il_incidents():
    incidents = []
    for feature in fetch_idot_il_dataset('Incidents'):
        attrs = feature.get('attributes') or {}
        item_type = display_text(attrs.get('TRAFFIC_ITEM_TYPE_DESC'))
        if item_type.upper() == 'CONSTRUCTION':
            continue
        coordinates = idot_il_point(feature)
        raw_id = display_text(attrs.get('OBJECTID'))
        if not coordinates or not raw_id:
            continue
        roadway = display_text(attrs.get('LOCATION_DEFINED_ORIGIN_RDWY'))
        title = item_type.replace('_', ' ').title() or 'Illinois traffic incident'
        if roadway:
            title = f'{title} · {roadway}'
        description = strip_tags(
            attrs.get('TRAFFIC_ITEM_DESCRIPTION') or
            attrs.get('TRAFFIC_ITEM_DESCRIPTION_NO_EX') or
            attrs.get('DESCRIPTION') or
            attrs.get('COMMENTS') or title
        )
        incidents.append({
            'id': wv511_numeric_id(f'IDOTIL:INC:{raw_id}'),
            'name': title,
            'lat': coordinates[0],
            'lon': coordinates[1],
            'description': description,
            'severity': display_text(attrs.get('CRITICALITY_DESC')).title() or None,
            'timestamp': epoch_milliseconds_iso(attrs.get('START_TIME')),
            'location': roadway or None,
        })
    return incidents


def fetch_idot_il_construction():
    construction = []
    for feature in fetch_idot_il_dataset('Construction'):
        attrs = feature.get('attributes') or {}
        coordinates = idot_il_path_point(feature)
        raw_id = display_text(attrs.get('OBJECTID'))
        if not coordinates or not raw_id:
            continue
        route = display_text(attrs.get('Route'))
        location = display_text(attrs.get('Location'))
        kind = display_text(attrs.get('ConstructionType'))
        title = ' · '.join(part for part in (route, location or kind) if part) or 'Illinois road construction'
        details = [
            strip_tags(value) for value in (
                attrs.get('ConstructionType'),
                attrs.get('Comments'),
                attrs.get('SuggestionToMotorist'),
            ) if strip_tags(value)
        ]
        severity = ' · '.join(part for part in (
            display_text(attrs.get('ImpactOnTravel')),
            display_text(attrs.get('TrafficAlert')),
            display_text(attrs.get('RestrictionType')),
        ) if part) or None
        construction.append({
            'id': wv511_numeric_id(f'IDOTIL:CON:{raw_id}'),
            'name': title,
            'lat': coordinates[0],
            'lon': coordinates[1],
            'description': ' · '.join(dict.fromkeys(details)) or title,
            'severity': severity,
            'timestamp': epoch_milliseconds_iso(attrs.get('StartDate')),
            'location': location or None,
        })
    return construction


def idot_il_records(layer):
    if layer == 'Cameras':
        return fetch_idot_il_cameras()
    if layer == 'MessageSigns':
        return fetch_idot_il_signs()
    if layer == 'Incidents':
        return fetch_idot_il_incidents()
    if layer == 'Construction':
        return fetch_idot_il_construction()
    return []


def idot_il_layer_payload(layer):
    records = idot_il_records(layer)
    if layer == 'Cameras':
        return {'item2': [{
            'itemId': camera['id'],
            'location': [camera['lat'], camera['lon']],
            'title': camera['name'],
            'expando': {
                'videoEnabled': False,
                'videoId': camera['id'],
                'videoUrl': None,
                'snapshotUrl': f'/camera-snapshot/IL/{camera["id"]}' if camera['snapshot_url'] else None,
                'snapshotFromVideo': False,
            },
        } for camera in records]}
    if layer == 'MessageSigns':
        return {'item2': [{
            'itemId': sign['id'],
            'location': [sign['lat'], sign['lon']],
            'title': sign['name'],
            'expando': {'message': sign['message'], 'timestamp': None},
        } for sign in records]}
    if layer not in {'Incidents', 'Construction'}:
        return {'item2': []}
    return {'item2': [{
        'itemId': item['id'],
        'location': [item['lat'], item['lon']],
        'title': item['name'],
        'expando': {
            'feedLabel': 'Construction' if layer == 'Construction' else 'Traffic Incident',
            'description': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
        },
    } for item in records]}


def idot_il_camera_detail(site_id):
    target = str(site_id)
    for camera in fetch_idot_il_cameras():
        if camera['id'] != target:
            continue
        directions = ', '.join(camera['directions'])
        return {
            'name': camera['name'],
            'msg': f'Available views: {directions}' if directions else 'Illinois DOT traffic camera',
            'severity': None,
            'timestamp': None,
            'video_id': target,
            'video_url': None,
            'video_enabled': False,
            'snapshot_url': f'/camera-snapshot/IL/{target}' if camera['snapshot_url'] else None,
            'upstream_snapshot_url': camera['snapshot_url'] or None,
        }
    raise ValueError(f'Illinois DOT camera {site_id} not found')


def idot_il_tooltip(layer, item_id):
    target = str(item_id)
    if layer == 'Cameras':
        return idot_il_camera_detail(target)
    for item in idot_il_records(layer):
        if item['id'] != target:
            continue
        if layer == 'MessageSigns':
            return {
                'name': item['name'],
                'msg': item['message'],
                'severity': ' · '.join(part for part in (item.get('road'), item.get('status')) if part),
                'timestamp': None,
                'video_id': None,
                'video_url': None,
                'video_enabled': False,
            }
        return {
            'name': item['name'],
            'msg': item['description'],
            'severity': item.get('severity'),
            'timestamp': item.get('timestamp'),
            'location': item.get('location'),
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    raise ValueError(f'Illinois DOT {layer} item {item_id} not found')


def fetch_idot_il_road_weather():
    features = []
    for feature in fetch_idot_il_dataset('RoadWeather'):
        attrs = feature.get('attributes') or {}
        coordinates = idot_il_point(feature)
        raw_id = display_text(attrs.get('StationID') or attrs.get('OBJECTID'))
        if not coordinates or not raw_id:
            continue
        air_temp = optional_float(attrs.get('Temperature'))
        if air_temp is None:
            air_temp = optional_float(attrs.get('Temp'))
        surface_temp = optional_float(attrs.get('SurfaceTemp'))
        if surface_temp == 0 and air_temp is not None and air_temp > 20:
            surface_temp = None
        wind_mph = optional_float(attrs.get('WindSpeed'))
        gust_mph = optional_float(attrs.get('WindGusts'))
        road_state = display_text(
            attrs.get('SurfaceCondition') or
            attrs.get('PrecipitationDescription') or
            attrs.get('PrecipitationLevel')
        ) or 'Unknown'
        lat, lon = coordinates
        sensor_attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': f'IDOTIL_{raw_id}',
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': display_text(attrs.get('Displayname')) or f'Illinois DOT weather station {raw_id}',
            'OBS_TIME_LOCAL': epoch_milliseconds_iso(attrs.get('ObsDateTime_Local')),
            'AIR_TEMP_F': air_temp,
            'DEW_POINT_F': optional_float(attrs.get('DewPoint')),
            'RELATIVE_HUMIDITY': optional_float(attrs.get('RelativeHumidity')),
            'WIND_DIRECTION': display_text(attrs.get('WindDirection')),
            'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
            'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
            'VISIBILITY': None,
            'ROAD_TEMP_F': surface_temp,
            'SUBSURFACE_TEMP_F': None,
            'ROAD_STATE': road_state,
            'PRECIP_1H_IN': None,
            'PRECIP_12H_IN': None,
            'PRECIP_24H_IN': None,
            'SOURCE_URL': IDOT_IL_SOURCE_URL,
        }
        features.append({'attributes': sensor_attrs, 'geometry': {'x': lon, 'y': lat}})
    if not features:
        raise ValueError('Illinois DOT returned no road-weather stations')
    return features


def fetch_iem_state_rwis(state_code, network):
    state_name = REGIONS[state_code]['name']
    data = fetch_json_url(
        f'https://mesonet.agron.iastate.edu/api/1/currents.geojson?network={network}',
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/geo+json,application/json',
        },
        timeout=20,
    )
    features = []
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        coords = (feature.get('geometry') or {}).get('coordinates') or []
        if len(coords) < 2:
            continue
        lon = optional_float(coords[0])
        lat = optional_float(coords[1])
        raw_id = str(props.get('station') or '').strip()
        if lat is None or lon is None or not raw_id or not point_in_state(state_code, lat, lon):
            continue
        features.append({
            'attributes': {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'{state_code}RWIS_{raw_id}',
                'LATITUDE': lat,
                'LNGITUDE': lon,
                'LOCALNAM': display_text(props.get('name')) or f'{state_name} RWIS {raw_id}',
                'OBS_TIME_LOCAL': props.get('local_valid') or props.get('utc_valid'),
                'AIR_TEMP_F': optional_float(props.get('tmpf')),
                'DEW_POINT_F': optional_float(props.get('dwpf')),
                'RELATIVE_HUMIDITY': optional_float(props.get('relh')),
                'WIND_DIRECTION': display_text(props.get('drct')),
                'WIND_SPEED_KTS': optional_float(props.get('sknt')),
                'WIND_GUST_KTS': optional_float(props.get('gust')),
                'VISIBILITY': optional_float(props.get('vsby')),
                'ROAD_TEMP_F': optional_float(props.get('tsf0')),
                'SUBSURFACE_TEMP_F': optional_float(props.get('rwis_subf')),
                'ROAD_STATE': display_text(props.get('scond0')) or 'Unknown',
                'PRECIP_1H_IN': optional_float(props.get('phour')),
                'PRECIP_12H_IN': None,
                'PRECIP_24H_IN': optional_float(props.get('pday')),
                'SOURCE_URL': f'https://mesonet.agron.iastate.edu/sites/networks.php?network={network}',
            },
            'geometry': {'x': lon, 'y': lat},
        })
    if not features:
        raise ValueError(f'{state_name} RWIS returned no current reporting stations')
    return features


def fetch_iem_wisconsin_rwis():
    return fetch_iem_state_rwis('WI', 'WI_RWIS')


def fetch_iem_iowa_rwis():
    return fetch_iem_state_rwis('IA', 'IA_RWIS')


def fetch_iem_arizona_rwis():
    return fetch_iem_state_rwis('AZ', 'AZ_RWIS')


def fetch_iem_california_rwis():
    return fetch_iem_state_rwis('CA', 'CA_RWIS')


def fetch_iem_nevada_rwis():
    return fetch_iem_state_rwis('NV', 'NV_RWIS')


def fetch_iem_idaho_rwis():
    return fetch_iem_state_rwis('ID', 'ID_RWIS')


def fetch_iem_utah_rwis():
    return fetch_iem_state_rwis('UT', 'UT_RWIS')


def fetch_iem_wyoming_rwis():
    return fetch_iem_state_rwis('WY', 'WY_RWIS')


def fetch_iem_montana_rwis():
    return fetch_iem_state_rwis('MT', 'MT_RWIS')


def fetch_iem_nebraska_rwis():
    return fetch_iem_state_rwis('NE', 'NE_RWIS')


def fetch_iem_kansas_rwis():
    return fetch_iem_state_rwis('KS', 'KS_RWIS')


def fetch_iem_arkansas_rwis():
    return fetch_iem_state_rwis('AR', 'AR_RWIS')


def fetch_iem_connecticut_rwis():
    return fetch_iem_state_rwis('CT', 'CT_RWIS')


def fetch_iem_massachusetts_rwis():
    return fetch_iem_state_rwis('MA', 'MA_RWIS')


def fetch_iem_maryland_rwis():
    return fetch_iem_state_rwis('MD', 'MD_RWIS')


def fetch_iem_new_york_rwis():
    return fetch_iem_state_rwis('NY', 'NY_RWIS')


def fetch_iem_south_carolina_rwis():
    return fetch_iem_state_rwis('SC', 'SC_RWIS')


def fetch_ndroads_road_weather():
    data = fetch_json_url(
        NDROADS_ESS_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': NDROADS_SOURCE_URL,
            'Accept': 'application/geo+json,application/json',
        },
        timeout=20,
    )
    features = []
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        coords = (feature.get('geometry') or {}).get('coordinates') or []
        if len(coords) < 2:
            continue
        lon = optional_float(coords[0])
        lat = optional_float(coords[1])
        raw_id = display_text(props.get('device_id') or feature.get('id'))
        if lat is None or lon is None or not raw_id or not point_in_state('ND', lat, lon):
            continue
        wind_mph = optional_float(props.get('ave_speed'))
        gust_mph = optional_float(props.get('gust_speed'))
        features.append({
            'attributes': {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'NDRWIS_{raw_id}',
                'LATITUDE': lat,
                'LNGITUDE': lon,
                'LOCALNAM': display_text(props.get('name')) or f'North Dakota RWIS {raw_id}',
                'OBS_TIME_LOCAL': (
                    props.get('surface_timestamp') or
                    props.get('temp_timestamp') or
                    props.get('wind_timestamp')
                ),
                'AIR_TEMP_F': optional_float(props.get('air_temp')),
                'DEW_POINT_F': None,
                'RELATIVE_HUMIDITY': None,
                'WIND_DIRECTION': display_text(props.get('ave_dir')),
                'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
                'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
                'VISIBILITY': None,
                'ROAD_TEMP_F': optional_float(props.get('surface_temp')),
                'SUBSURFACE_TEMP_F': optional_float(props.get('subsurface_temp')),
                'ROAD_STATE': display_text(props.get('status_string')) or 'Unknown',
                'PRECIP_1H_IN': None,
                'PRECIP_12H_IN': None,
                'PRECIP_24H_IN': None,
                'SOURCE_URL': NDROADS_SOURCE_URL,
            },
            'geometry': {'x': lon, 'y': lat},
        })
    if not features:
        raise ValueError('North Dakota Roads returned no current road-weather stations')
    return features


def fetch_sd511_road_weather():
    features = []
    for feature in fetch_sd511_dataset('RWISCameras'):
        props = feature.get('properties') or {}
        coords = (feature.get('geometry') or {}).get('coordinates') or []
        if len(coords) < 2:
            continue
        lon = optional_float(coords[0])
        lat = optional_float(coords[1])
        raw_id = display_text(feature.get('id') or props.get('id'))
        if lat is None or lon is None or not raw_id or not point_in_state('SD', lat, lon):
            continue
        atmos = next(iter(props.get('atmos') or []), {})
        surface = next(iter(props.get('surface') or []), {})
        wind_mph = optional_float(nested_value(atmos, 'wind_speed', 'value'))
        gust_mph = optional_float(nested_value(atmos, 'wind_gust', 'value'))
        observation = (
            nested_value(atmos, 'observation_time', 'value') or
            nested_value(surface, 'observation_time', 'value')
        )
        features.append({
            'attributes': {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'SDRWIS_{raw_id}',
                'LATITUDE': lat,
                'LNGITUDE': lon,
                'LOCALNAM': display_text(props.get('name')) or f'South Dakota RWIS {raw_id}',
                'OBS_TIME_LOCAL': epoch_milliseconds_iso(observation),
                'AIR_TEMP_F': optional_float(nested_value(atmos, 'air_temperature', 'value')),
                'DEW_POINT_F': optional_float(nested_value(atmos, 'dewpoint_temperature', 'value')),
                'RELATIVE_HUMIDITY': optional_float(nested_value(atmos, 'relative_humidity', 'value')),
                'WIND_DIRECTION': display_text(nested_value(atmos, 'wind_direction', 'value')),
                'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
                'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
                'VISIBILITY': None,
                'ROAD_TEMP_F': optional_float(nested_value(surface, 'surface_temperature', 'value')),
                'SUBSURFACE_TEMP_F': None,
                'ROAD_STATE': display_text(nested_value(surface, 'surface_condition', 'value')) or 'Unknown',
                'PRECIP_1H_IN': optional_float(nested_value(atmos, 'precip_accumulated', 'value')),
                'PRECIP_12H_IN': None,
                'PRECIP_24H_IN': None,
                'SOURCE_URL': SD511_SOURCE_URL,
            },
            'geometry': {'x': lon, 'y': lat},
        })
    if not features:
        raise ValueError('South Dakota 511 returned no current road-weather stations')
    return features


def cotrip_rwis_number(value):
    match = re.search(r'-?\d+(?:\.\d+)?', str(value or ''))
    return optional_float(match.group(0)) if match else None


def fetch_cotrip_road_weather():
    features = []
    for feature in fetch_cotrip_dataset('rwis'):
        props = feature.get('properties') or {}
        center = cotrip_feature_center(feature)
        if not center or not point_in_state('CO', *center):
            continue
        lat, lon = center
        values = {
            item.get('name'): item.get('value')
            for item in (props.get('rwisData') or [])
            if item.get('name')
        }
        wind_mph = cotrip_rwis_number(values.get('WIND_AVG_SPEED'))
        gust_mph = cotrip_rwis_number(values.get('WIND_MAX_SPEED'))
        raw_id = str(props.get('id') or props.get('stationIdentifier') or '').strip()
        if not raw_id:
            continue
        features.append({
            'attributes': {
                'SENSOR_TYPE': 'road_weather',
                'IDSTR': f'COTRIP_{raw_id}',
                'LATITUDE': lat,
                'LNGITUDE': lon,
                'LOCALNAM': display_text(props.get('name')) or f'Colorado DOT RWIS {raw_id}',
                'OBS_TIME_LOCAL': epoch_milliseconds_iso(props.get('dataUpdated') or props.get('updated')),
                'AIR_TEMP_F': cotrip_rwis_number(values.get('TEMP_AIR_TEMPERATURE')),
                'DEW_POINT_F': cotrip_rwis_number(values.get('TEMP_DEW_POINT')),
                'RELATIVE_HUMIDITY': None,
                'WIND_DIRECTION': display_text(values.get('WIND_AVG_DIRECTION')) or None,
                'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
                'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
                'VISIBILITY': cotrip_rwis_number(values.get('VIS_VISIBILITY')),
                'ROAD_TEMP_F': cotrip_rwis_number(values.get('PAVEMENT_SURFACE_TEMPERATURE')),
                'SUBSURFACE_TEMP_F': cotrip_rwis_number(values.get('PAVEMENT_SUB_SURFACE_TEMPERATURE')),
                'ROAD_STATE': 'Unknown',
                'PRECIP_1H_IN': None,
                'PRECIP_12H_IN': cotrip_rwis_number(values.get('PRECIP_PAST_12_HOURS')),
                'PRECIP_24H_IN': cotrip_rwis_number(values.get('PRECIP_PAST_24_HOURS')),
                'SOURCE_URL': COTRIP_SOURCE_URL,
            },
            'geometry': {'x': lon, 'y': lat},
        })
    if not features:
        raise ValueError('COtrip returned no current road-weather stations')
    return features


def stream_traffic_region(hostname):
    host = str(hostname or '').lower()
    if host.endswith('navigator.dot.ga.gov'):
        return REGIONS['GA']
    if host == 'wowza.com' or host.endswith('.wowza.com'):
        return REGIONS['AL']
    if host == 'mdottraffic.com' or host.endswith('.mdottraffic.com'):
        return REGIONS['MS']
    if host.startswith('mcleansfs') and host.endswith('.skyvdn.com'):
        return REGIONS['TN']
    if host == 'skyvdn.com' or host.endswith('.skyvdn.com'):
        return REGIONS['SC']
    if host == 'services.ncdot.gov' or host.endswith('.services.ncdot.gov'):
        return REGIONS['NC']
    if host == 'vdotcameras.com' or host.endswith('.vdotcameras.com'):
        return REGIONS['VA']
    if host == 'roadsummary.com' or host.endswith('.roadsummary.com'):
        return REGIONS['WV']
    if host == 'sha.maryland.gov' or host.endswith('.sha.maryland.gov'):
        return REGIONS['MD']
    if host == 'video.deldot.gov' or host.endswith('.video.deldot.gov'):
        return REGIONS['DE']
    if host == 'wink.njta.com' or host.endswith('.wink.njta.com'):
        return REGIONS['NJ']
    if host == 'trafficland.com' or host.endswith('.trafficland.com'):
        return REGIONS['MA']
    if host == 'trafficwise.org' or host.endswith('.trafficwise.org'):
        return REGIONS['IN']
    if host == 'cctv1.dot.wi.gov':
        return REGIONS['WI']
    if host == 'video.dot.state.mn.us':
        return REGIONS['MN']
    if host == 'iowadot.gov' or host.endswith('.iowadot.gov'):
        return REGIONS['IA']
    if host == 'modot.mo.gov' or host.endswith('.modot.mo.gov'):
        return REGIONS['MO']
    if host == 'dotd.la.gov' or host.endswith('.dotd.la.gov'):
        return REGIONS['LA']
    if host == 'stream.oktraffic.org':
        return REGIONS['OK']
    if host == 'wzmedia.dot.ca.gov':
        return REGIONS['CA']
    if (
        host == 'trimarc.org' or host.endswith('.trimarc.org') or
        host == 'pws.trafficwise.org' or host.endswith('.pws.trafficwise.org') or
        host == 'streamlock.net' or host.endswith('.streamlock.net')
    ):
        return REGIONS['KY']
    return REGIONS['FL']


def display_text(value):
    text = str(value or '').strip()
    if not text:
        return ''
    return text.title() if text.upper() == text else text


def file_path(name):
    return os.path.join(BASE_DIR, name)


def normalized_host(value):
    host = str(value or '').strip().lower()
    if not host:
        return ''
    if host.startswith('['):
        end = host.find(']')
        return host[1:end] if end != -1 else host.strip('[]')
    if host.count(':') == 1:
        return host.split(':', 1)[0]
    return host


DEFAULT_ALLOWED_HOSTS = tuple(sorted({
    host for host in (
        normalized_host(HOST),
        'localhost',
        '127.0.0.1',
        '::1',
    ) if host and host not in {'0.0.0.0', '::'}
}))
ALLOWED_HOSTS = tuple(
    part for part in (
        normalized_host(raw)
        for raw in os.getenv(
            'AMERICAMAP_ALLOWED_HOSTS',
            os.getenv('FLORIDAMAP_ALLOWED_HOSTS', ','.join(DEFAULT_ALLOWED_HOSTS)),
        ).split(',')
    )
    if part
)


def allowed_request_host(hostname):
    host = normalized_host(hostname)
    if not host:
        return False
    for allowed in ALLOWED_HOSTS:
        if allowed == '*':
            return True
        if allowed.startswith('*.'):
            if host.endswith(allowed[1:]) and host != allowed[2:]:
                return True
            continue
        if host == allowed:
            return True
    return False


def allowed_stream_host(hostname):
    host = str(hostname or '').strip().lower()
    if not host:
        return False
    return any(host == suffix or host.endswith('.' + suffix) for suffix in ALLOWED_STREAM_HOST_SUFFIXES)


def valid_numeric_id(value):
    return bool(re.fullmatch(r'\d{1,18}', str(value or '').strip()))


def valid_traffic_item_id(value):
    return bool(re.fullmatch(r'[A-Za-z0-9_-]{1,96}', str(value or '').strip()))


def valid_icao(value):
    return bool(re.fullmatch(r'[0-9a-fA-F]{6}', str(value or '').strip()))


def safe_int_param(value, minimum=None, maximum=None):
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def safe_bbox_param(value):
    parts = str(value or '').split(',')
    if len(parts) != 4:
        return None
    try:
        min_lon, min_lat, max_lon, max_lat = [float(part) for part in parts]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in (min_lon, min_lat, max_lon, max_lat)):
        return None
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        return None
    return min_lon, min_lat, max_lon, max_lat


def public_static_target(path):
    raw_path = urllib.parse.unquote(path or '/')
    if raw_path in {'', '/'}:
        return 'index.html'
    target = raw_path.lstrip('/')
    if not target or '/' in target or '\\' in target or target.startswith('.'):
        return None
    return target if target in PUBLIC_STATIC_FILES else None


def normalized_cache_key(value):
    return ' '.join(str(value or '').split())


def strip_tags(value):
    text = re.sub(r'<!--.*?-->', ' ', str(value or ''), flags=re.S)
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', text)).split())


def local_time_iso(value):
    try:
        now = datetime.datetime.now(LOCAL_TZ)
        hour, minute, second = [int(part) for part in str(value).split(':')]
        stamp = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if stamp > now + datetime.timedelta(minutes=5):
            stamp -= datetime.timedelta(days=1)
        return stamp.isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def geocode_query(location, locality=''):
    text = ' '.join(str(location or '').split())
    if not text:
        return ''
    text = html.unescape(text)
    text = re.sub(r'(?i)\b(\d{1,5})\s+BLOCK\s*&\s+', r'\1 ', text)
    text = re.sub(r'\s*/\s*', ' & ', text)
    text = re.sub(r'\s+', ' ', text).strip(' ,')
    return f'{text}, {locality}' if locality else text


def parse_time_iso(value, formats):
    text = ' '.join(str(value or '').split())
    if not text:
        return None
    now = datetime.datetime.now(LOCAL_TZ)
    for fmt in formats:
        try:
            stamp = datetime.datetime.strptime(text, fmt)
        except (TypeError, ValueError):
            continue
        if '%Y' not in fmt:
            stamp = stamp.replace(year=now.year)
            if stamp.replace(tzinfo=LOCAL_TZ) > now + datetime.timedelta(days=1):
                stamp = stamp.replace(year=now.year - 1)
        return stamp.replace(tzinfo=LOCAL_TZ).isoformat()
    return None


def tops_time_iso(value):
    try:
        text = ' '.join(str(value or '').split())
        if not text:
            return None
        stamp = datetime.datetime.strptime(text, '%b %d %Y %I:%M%p')
        return stamp.replace(tzinfo=LOCAL_TZ).isoformat()
    except (TypeError, ValueError):
        return None


def web_mercator_to_latlon(x, y):
    lon = x * 180.0 / 20037508.34
    lat = y * 180.0 / 20037508.34
    lat = 180.0 / math.pi * (2.0 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2.0)
    return lat, lon


def rings_center(rings):
    points = [
        (float(point[0]), float(point[1]))
        for ring in (rings or [])
        for point in ring
        if len(point) >= 2
    ]
    if not points:
        return None
    min_lon = min(point[0] for point in points)
    max_lon = max(point[0] for point in points)
    min_lat = min(point[1] for point in points)
    max_lat = max(point[1] for point in points)
    lat = (min_lat + max_lat) / 2.0
    lon = (min_lon + max_lon) / 2.0
    return (lat, lon) if in_region(lat, lon) else None


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_json_url(url, headers=None, data=None, timeout=20):
    req_headers = {'User-Agent': 'Mozilla/5.0'}
    if headers:
        req_headers.update(headers)
    payload = data
    if isinstance(data, (dict, list)):
        payload = json.dumps(data).encode('utf-8')
        req_headers.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(url, data=payload, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if raw[:2] == b'\x1f\x8b':
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw)


def fetch_sc511_layer(layer):
    filename = SC511_LAYER_FILES.get(layer)
    if not filename:
        return {'type': 'FeatureCollection', 'features': []}
    now = time.time()
    with SC511_TRAFFIC_CACHE_LOCK:
        cached = SC511_TRAFFIC_CACHE.get(layer)
        if cached and now < cached['expires_at']:
            return cached['data']
    try:
        data = fetch_json_url(
            f'{SC511_METADATA_ROOT}/{filename}',
            headers={
                'Accept': 'application/geo+json,application/json',
                'Referer': 'https://www.511sc.org/',
            },
            timeout=20,
        )
        if not isinstance(data, dict) or not isinstance(data.get('features'), list):
            raise ValueError(f'Unexpected South Carolina 511 {layer} response')
    except Exception:
        if cached:
            return cached['data']
        raise
    with SC511_TRAFFIC_CACHE_LOCK:
        SC511_TRAFFIC_CACHE[layer] = {
            'data': data,
            'expires_at': time.time() + SC511_CACHE_TTL,
        }
    return data


def sc511_feature_id(feature, layer):
    props = feature.get('properties') or {}
    if layer == 'Cameras':
        candidates = (props.get('name'), props.get('id'))
    elif layer == 'MessageSigns':
        candidates = (props.get('DMS_name'), props.get('event_id'), feature.get('id'))
    else:
        candidates = (props.get('event_id'), feature.get('id'), props.get('name'))
    for value in candidates:
        match = re.search(r'(\d{1,18})$', str(value or '').strip())
        if match:
            return match.group(1)
    return None


def sc511_layer_payload(layer):
    sc_bounds = REGIONS['SC']['bounds']
    normalized = []
    for feature in fetch_sc511_layer(layer).get('features') or []:
        props = feature.get('properties') or {}
        coords = (feature.get('geometry') or {}).get('coordinates') or []
        if len(coords) < 2:
            continue
        lon = safe_float(coords[0])
        lat = safe_float(coords[1])
        if not (
            sc_bounds['min_lat'] <= lat <= sc_bounds['max_lat'] and
            sc_bounds['min_lon'] <= lon <= sc_bounds['max_lon']
        ):
            continue
        item_id = sc511_feature_id(feature, layer)
        if not item_id:
            continue

        if layer == 'Cameras':
            if props.get('active') is False:
                continue
            title = display_text(props.get('description') or props.get('name') or f'SCDOT camera {item_id}')
            video_url = str(props.get('https_url') or props.get('ios_url') or '').strip()
            expando = {
                'videoEnabled': bool(video_url and not props.get('problem_stream')),
                'videoId': item_id,
                'videoUrl': video_url,
                'snapshotUrl': f'/camera-snapshot/SC/{item_id}',
            }
        elif layer == 'MessageSigns':
            title = display_text(props.get('location_description') or props.get('DMS_name') or f'SCDOT sign {item_id}')
            expando = {'message': None, 'timestamp': None}
        else:
            title = display_text(
                props.get('location_description') or props.get('headline') or f'South Carolina {layer}'
            )
            expando = {
                'feedLabel': display_text(props.get('headline') or ('Construction' if layer == 'Construction' else 'Incident')),
                'description': None,
                'severity': None,
                'timestamp': None,
                'location': props.get('location_description'),
            }
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def sc511_camera_detail(site_id):
    target_id = str(site_id)
    for feature in fetch_sc511_layer('Cameras').get('features') or []:
        if sc511_feature_id(feature, 'Cameras') != target_id:
            continue
        props = feature.get('properties') or {}
        video_url = str(props.get('https_url') or props.get('ios_url') or '').strip()
        snapshot_url = str(props.get('image_url') or '').strip()
        parsed_video = urllib.parse.urlparse(video_url)
        parsed_snapshot = urllib.parse.urlparse(snapshot_url)
        if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
            video_url = ''
        if parsed_snapshot.scheme != 'https' or not allowed_stream_host(parsed_snapshot.hostname):
            snapshot_url = ''
        return {
            'name': display_text(props.get('description') or props.get('name') or f'SCDOT camera {site_id}'),
            'msg': display_text(props.get('jurisdiction') or '') or None,
            'severity': None,
            'timestamp': None,
            'video_id': target_id,
            'video_url': video_url if not props.get('problem_stream') else None,
            'video_enabled': bool(video_url and not props.get('problem_stream')),
            'snapshot_url': f'/camera-snapshot/SC/{target_id}' if snapshot_url else None,
            'upstream_snapshot_url': snapshot_url or None,
        }
    raise ValueError(f'Unknown South Carolina camera {site_id}')


def sc511_tooltip(layer, item_id):
    if layer == 'Cameras':
        return sc511_camera_detail(item_id)
    prefix = 'dms_DMS_' if layer == 'MessageSigns' else 'event_'
    data = fetch_json_url(
        f'{SC511_DATA_ROOT}/{prefix}{urllib.parse.quote(str(item_id))}',
        headers={
            'Accept': 'application/json',
            'Referer': 'https://www.511sc.org/',
        },
        timeout=15,
    )
    timestamp = None
    unix_time = safe_float(data.get('unixtime'))
    if unix_time > 0:
        timestamp = datetime.datetime.fromtimestamp(unix_time, datetime.timezone.utc).isoformat()
    message = strip_tags(data.get('report') or data.get('text1') or '') or None
    return {
        'name': display_text(data.get('label') or data.get('location_description') or f'South Carolina {layer}'),
        'msg': message,
        'severity': display_text(data.get('severity') or '') or None,
        'timestamp': timestamp,
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def algo_api_url(resource, item_id=None):
    base = f'https://api.algotraffic.com/{ALGO_API_VERSION}/{resource}'
    return f'{base}/{item_id}' if item_id is not None else f'{base}?take=1000'


def algo_headers():
    return {
        'User-Agent': 'GlobeView/1.0',
        'Referer': 'https://algotraffic.com/',
        'Accept': 'application/json',
    }


def fetch_algo_items(resource):
    now = time.time()
    with ALGO_TRAFFIC_CACHE_LOCK:
        cached = ALGO_TRAFFIC_CACHE.get(resource)
        if cached and now < cached['expires_at']:
            return cached['items']
    try:
        items = fetch_json_url(algo_api_url(resource), headers=algo_headers(), timeout=20)
        if not isinstance(items, list):
            raise ValueError(f'Unexpected ALGO {resource} response')
    except Exception:
        if cached:
            return cached['items']
        raise
    with ALGO_TRAFFIC_CACHE_LOCK:
        ALGO_TRAFFIC_CACHE[resource] = {
            'items': items,
            'expires_at': time.time() + ALGO_CACHE_TTL,
        }
    return items


def fetch_algo_item(resource, item_id):
    target_id = str(item_id)
    with ALGO_TRAFFIC_CACHE_LOCK:
        cached = ALGO_TRAFFIC_CACHE.get(resource)
    if cached:
        for item in cached['items']:
            if str(item.get('id')) == target_id:
                return item
    item = fetch_json_url(algo_api_url(resource, target_id), headers=algo_headers(), timeout=15)
    if not isinstance(item, dict):
        raise ValueError(f'Unexpected ALGO {resource} item response')
    return item


def algo_location_label(location):
    location = location or {}
    route = str(location.get('displayRouteDesignator') or location.get('routeDesignator') or '').strip()
    cross_street = str(location.get('displayCrossStreet') or location.get('crossStreet') or '').strip()
    direction = display_text(location.get('direction'))
    city = display_text(location.get('city'))
    primary = route
    if cross_street:
        primary = f'{primary} at {cross_street}' if primary else cross_street
    parts = [part for part in (primary, direction, city) if part]
    return ' · '.join(parts) or 'Alabama roadway'


def algo_message_text(item):
    pages = []
    for page in item.get('pages') or []:
        lines = [
            str(line.get('text') or '').strip()
            for line in page.get('lines') or []
            if str(line.get('text') or '').strip()
        ]
        if lines:
            pages.append(' / '.join(lines))
    return ' • '.join(pages)


def algo_event_label(event_type):
    labels = {
        'Roadwork': 'Construction',
        'RoadCondition': 'Road Condition',
        'RegionalEvent': 'Regional Event',
    }
    return labels.get(str(event_type or ''), display_text(event_type) or 'Incident')


def algo_event_severity(severity):
    return {
        'MinorDelay': 'Minor',
        'ModerateDelay': 'Moderate',
        'MajorDelay': 'Major',
    }.get(str(severity or ''), display_text(severity))


def algo_event_description(item):
    parts = []
    for value in (item.get('subTitle'), item.get('description')):
        text = str(value or '').strip()
        if text and text not in parts:
            parts.append(text)
    return ' · '.join(parts) or str(item.get('title') or algo_event_label(item.get('type')))


def algo_layer_payload(layer):
    normalized = []
    if layer == 'Cameras':
        for item in fetch_algo_items('Cameras'):
            location = item.get('location') or {}
            lat = safe_float(location.get('latitude'))
            lon = safe_float(location.get('longitude'))
            if not in_region(lat, lon):
                continue
            playback = item.get('playbackUrls') or {}
            hls_url = str(playback.get('hls') or '').strip()
            dash_url = str(playback.get('dash') or '').strip()
            normalized.append({
                'itemId': item.get('id'),
                'location': [lat, lon],
                'title': algo_location_label(location),
                'expando': {
                    'videoEnabled': bool(hls_url or dash_url),
                    'videoUrl': hls_url,
                    'dashUrl': dash_url,
                    'videoId': item.get('id'),
                    'snapshotUrl': item.get('snapshotImageUrl'),
                },
            })
    elif layer == 'MessageSigns':
        for item in fetch_algo_items('MessageSigns'):
            location = item.get('location') or {}
            lat = safe_float(location.get('latitude'))
            lon = safe_float(location.get('longitude'))
            if not in_region(lat, lon):
                continue
            normalized.append({
                'itemId': item.get('id'),
                'location': [lat, lon],
                'title': algo_location_label(location),
                'expando': {'message': algo_message_text(item), 'timestamp': None},
            })
    elif layer in {'Incidents', 'Construction'}:
        for item in fetch_algo_items('TrafficEvents'):
            is_roadwork = item.get('type') == 'Roadwork'
            if (layer == 'Construction') != is_roadwork:
                continue
            location = item.get('startLocation') or item.get('endLocation') or {}
            lat = safe_float(location.get('latitude'))
            lon = safe_float(location.get('longitude'))
            if not in_region(lat, lon):
                continue
            label = algo_event_label(item.get('type'))
            normalized.append({
                'itemId': item.get('id'),
                'location': [lat, lon],
                'title': item.get('title') or label,
                'expando': {
                    'feedLabel': label,
                    'description': algo_event_description(item),
                    'severity': algo_event_severity(item.get('severity')),
                    'timestamp': item.get('lastUpdatedAt') or item.get('start'),
                    'location': algo_location_label(location),
                },
            })
    return {'item2': normalized}


def algo_tooltip(layer, item_id):
    if layer == 'Cameras':
        item = fetch_algo_item('Cameras', item_id)
        playback = item.get('playbackUrls') or {}
        hls_url = str(playback.get('hls') or '').strip()
        return {
            'name': algo_location_label(item.get('location')),
            'msg': None,
            'severity': None,
            'timestamp': None,
            'video_id': str(item.get('id') or item_id),
            'video_url': hls_url or None,
            'video_enabled': bool(hls_url),
        }
    if layer == 'MessageSigns':
        item = fetch_algo_item('MessageSigns', item_id)
        return {
            'name': algo_location_label(item.get('location')),
            'msg': algo_message_text(item),
            'severity': None,
            'timestamp': None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }
    item = fetch_algo_item('TrafficEvents', item_id)
    return {
        'name': item.get('title') or algo_event_label(item.get('type')),
        'msg': algo_event_description(item),
        'severity': algo_event_severity(item.get('severity')),
        'timestamp': item.get('lastUpdatedAt') or item.get('start'),
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


MDOT_LAYER_METHODS = {
    'Cameras': 'LoadCameraData',
    'MessageSigns': 'LoadDMSData',
    'Incidents': 'LoadAlertData',
    'Construction': 'LoadAlertData',
}


def fetch_mdot_markers(method):
    now = time.time()
    with MDOT_TRAFFIC_CACHE_LOCK:
        cached = MDOT_TRAFFIC_CACHE.get(method)
        if cached and now < cached['expires_at']:
            return cached['items']
    url = f'https://www.mdottraffic.com/default.aspx/{method}'
    try:
        data = fetch_json_url(url, headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'application/json',
            'Referer': MDOT_TRAFFIC_SOURCE_URL,
        }, data={}, timeout=20)
        items = data.get('d') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError(f'Unexpected MDOT {method} response')
    except Exception:
        if cached:
            return cached['items']
        raise
    with MDOT_TRAFFIC_CACHE_LOCK:
        MDOT_TRAFFIC_CACHE[method] = {
            'items': items,
            'expires_at': time.time() + MDOT_CACHE_TTL,
        }
    return items


def mdot_marker_id(marker, prefix):
    match = re.fullmatch(rf'{re.escape(prefix)}_(\d+)', str(marker.get('markerid') or ''))
    return match.group(1) if match else None


def mdot_layer_payload(layer):
    method = MDOT_LAYER_METHODS.get(layer)
    if not method:
        return {'item2': []}
    normalized = []
    for marker in fetch_mdot_markers(method):
        prefix = 'camsite' if layer == 'Cameras' else 'dms' if layer == 'MessageSigns' else 'alert'
        item_id = mdot_marker_id(marker, prefix)
        lat = safe_float(marker.get('lat'))
        lon = safe_float(marker.get('lon'))
        if not item_id or not in_region(lat, lon):
            continue
        marker_group = str(marker.get('markergroup') or '')
        if layer == 'Construction' and marker_group != 'map-construction':
            continue
        if layer == 'Incidents' and marker_group not in {'map-closed-roads', 'map-incident-alerts'}:
            continue

        title = strip_tags(marker.get('tooltip')) or f'Mississippi {layer}'
        expando = {}
        if layer == 'Cameras':
            expando = {
                'videoEnabled': True,
                'videoId': item_id,
                'snapshotUrl': f'/camera-snapshot/MS/{item_id}',
            }
        elif layer == 'MessageSigns':
            expando = {'message': '', 'timestamp': None}
        else:
            expando = {
                'feedLabel': 'Road Closure' if marker_group == 'map-closed-roads' else (
                    'Traffic Incident' if layer == 'Incidents' else 'Construction'
                ),
                'description': None,
                'severity': None,
                'timestamp': None,
                'location': title,
            }
        normalized.append({
            'itemId': item_id,
            'location': [lat, lon],
            'title': title,
            'expando': expando,
        })
    return {'item2': normalized}


def fetch_mdot_popup(path):
    url = urllib.parse.urljoin('https://www.mdottraffic.com/', path)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'text/html,application/xhtml+xml',
        'Referer': MDOT_TRAFFIC_SOURCE_URL,
    })
    return urllib.request.urlopen(req, timeout=15).read().decode('utf-8', 'replace')


def mdot_element_text(text, element_id):
    match = re.search(
        rf'<[^>]+id=["\']{re.escape(element_id)}["\'][^>]*>(.*?)</[^>]+>',
        text,
        re.I | re.S,
    )
    return strip_tags(match.group(1)) if match else ''


def mdot_camera_detail(site_id):
    text = fetch_mdot_popup(f'mapbubbles/camerasite.aspx?site={site_id}')
    image_match = re.search(
        r'<img[^>]+id=["\']camimg["\'][^>]+src=["\']([^"\']+)["\']',
        text,
        re.I | re.S,
    )
    snapshot_url = html.unescape(image_match.group(1)).strip() if image_match else ''
    parsed_snapshot = urllib.parse.urlparse(snapshot_url)
    query = urllib.parse.parse_qs(parsed_snapshot.query)
    stream_name = str((query.get('streamname') or [''])[0]).strip()
    if not stream_name:
        stream_match = re.search(r'streamcam(?:_hq)?\.aspx\?cam=([\w.-]+)', text, re.I)
        stream_name = f'{stream_match.group(1)}.stream' if stream_match else ''
    elif not stream_name.endswith('.stream'):
        stream_name += '.stream'

    valid_snapshot = (
        parsed_snapshot.scheme == 'https' and
        allowed_stream_host(parsed_snapshot.hostname) and
        bool(stream_name) and
        'novideo' not in snapshot_url.lower()
    )
    video_url = None
    if valid_snapshot:
        stream_path = urllib.parse.quote(stream_name, safe='.-_')
        video_url = f'https://{parsed_snapshot.netloc}/rtplive/{stream_path}/playlist.m3u8'
    return {
        'name': mdot_element_text(text, 'siteTitle') or mdot_element_text(text, 'camTitle') or f'MDOT camera {site_id}',
        'msg': mdot_element_text(text, 'camSummary') or None,
        'severity': None,
        'timestamp': None,
        'video_id': str(site_id),
        'video_url': video_url,
        'video_enabled': bool(video_url),
        'snapshot_url': f'/camera-snapshot/MS/{site_id}' if valid_snapshot else None,
        'upstream_snapshot_url': snapshot_url if valid_snapshot else None,
    }


def mdot_table_pairs(text):
    pairs = {}
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.I | re.S):
        cells = [strip_tags(cell) for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.I | re.S)]
        if len(cells) >= 2 and cells[0]:
            pairs[cells[0].rstrip(':').strip()] = cells[1]
    return pairs


def mdot_marker_title(method, prefix, item_id, fallback):
    target = f'{prefix}_{item_id}'
    for marker in fetch_mdot_markers(method):
        if str(marker.get('markerid')) == target:
            return strip_tags(marker.get('tooltip')) or fallback
    return fallback


def mdot_tooltip(layer, item_id):
    if layer == 'Cameras':
        return mdot_camera_detail(item_id)
    if layer == 'MessageSigns':
        text = fetch_mdot_popup(f'mapbubbles/msgboard.aspx?mbid={item_id}')
        messages = [
            strip_tags(value)
            for value in re.findall(
                r'<td[^>]+class\s*=\s*["\']?messagetext["\']?[^>]*>(.*?)</td>',
                text,
                re.I | re.S,
            )
        ]
        posted = re.search(r'Last\s+Post:\s*([^<]+)', text, re.I)
        return {
            'name': mdot_marker_title('LoadDMSData', 'dms', item_id, 'Mississippi message sign'),
            'msg': ' / '.join(message for message in messages if message) or None,
            'severity': None,
            'timestamp': strip_tags(posted.group(1)) if posted else None,
            'video_id': None,
            'video_url': None,
            'video_enabled': False,
        }

    text = fetch_mdot_popup(f'mapbubbles/trafficalert.aspx?aid={item_id}')
    pairs = mdot_table_pairs(text)
    detail_parts = []
    for label in ('Justification', 'Additional', 'Lanes Affected', 'County'):
        value = pairs.get(label)
        if value:
            detail_parts.append(value if label in {'Justification', 'Additional'} else f'{label}: {value}')
    return {
        'name': mdot_marker_title('LoadAlertData', 'alert', item_id, 'Mississippi traffic event'),
        'msg': ' · '.join(detail_parts) or None,
        'severity': pairs.get('Traffic Impact'),
        'timestamp': pairs.get('Last Updated') or pairs.get('Begin'),
        'video_id': None,
        'video_url': None,
        'video_enabled': False,
    }


def decode_polyline_points(encoded, precision=5):
    if not encoded:
        return []
    index = 0
    lat = 0
    lon = 0
    factor = 10 ** precision
    coords = []
    while index < len(encoded):
        result = 1
        shift = 0
        while True:
            b = ord(encoded[index]) - 63 - 1
            index += 1
            result += b << shift
            shift += 5
            if b < 0x1F:
                break
        lat += ~(result >> 1) if result & 1 else (result >> 1)

        result = 1
        shift = 0
        while True:
            b = ord(encoded[index]) - 63 - 1
            index += 1
            result += b << shift
            shift += 5
            if b < 0x1F:
                break
        lon += ~(result >> 1) if result & 1 else (result >> 1)
        coords.append((lat / factor, lon / factor))
    return coords


def point_geometry(lat, lon):
    return {
        'type': 'Point',
        'coordinates': [lon, lat],
    }


def polygon_geometry_from_points(points):
    if len(points) < 3:
        return None
    coords = [[lon, lat] for lat, lon in points]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return {
        'type': 'Polygon',
        'coordinates': [coords],
    }


def polygon_geometry_from_google_points(points):
    coords = [
        [safe_float(point.get('lng')), safe_float(point.get('lat'))]
        for point in (points or [])
        if point.get('lat') is not None and point.get('lng') is not None
    ]
    if len(coords) < 3:
        return None
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return {
        'type': 'Polygon',
        'coordinates': [coords],
    }


def kubra_geometry(geom):
    rings = []
    for encoded in (geom or {}).get('a') or []:
        points = decode_polyline_points(encoded)
        if len(points) >= 3:
            rings.append(points)
    if not rings:
        return None
    if len(rings) == 1:
        return polygon_geometry_from_points(rings[0])
    polygons = []
    for ring in rings:
        polygon = polygon_geometry_from_points(ring)
        if polygon:
            polygons.append([polygon['coordinates'][0]])
    if not polygons:
        return None
    return {
        'type': 'MultiPolygon',
        'coordinates': polygons,
    }


def kubra_center(geom):
    for encoded in (geom or {}).get('p') or []:
        points = decode_polyline_points(encoded)
        if points:
            lat, lon = points[0]
            if in_region(lat, lon):
                return lat, lon
    for encoded in (geom or {}).get('a') or []:
        points = decode_polyline_points(encoded)
        if not points:
            continue
        lat = sum(point[0] for point in points) / len(points)
        lon = sum(point[1] for point in points) / len(points)
        if in_region(lat, lon):
            return lat, lon
    return None


def geojson_center(geometry):
    geometry_type = (geometry or {}).get('type')
    coords = (geometry or {}).get('coordinates') or []
    if geometry_type == 'Point' and len(coords) >= 2:
        lat = safe_float(coords[1])
        lon = safe_float(coords[0])
        return (lat, lon) if in_region(lat, lon) else None
    if geometry_type == 'LineString' and coords:
        point = coords[len(coords) // 2]
        if len(point) >= 2:
            lat = safe_float(point[1])
            lon = safe_float(point[0])
            return (lat, lon) if in_region(lat, lon) else None
    if geometry_type == 'MultiLineString' and coords:
        longest = max(coords, key=len, default=[])
        if longest:
            point = longest[len(longest) // 2]
            if len(point) >= 2:
                lat = safe_float(point[1])
                lon = safe_float(point[0])
                return (lat, lon) if in_region(lat, lon) else None
    if geometry_type == 'Polygon':
        return rings_center(coords)
    if geometry_type == 'MultiPolygon':
        return rings_center([
            ring
            for polygon in coords
            for ring in (polygon or [])
        ])
    return None


def keys_power_geometry(item):
    points = (item or {}).get('points') or {}
    coords = points.get('coordinates')
    geometry_type = points.get('type')
    if not coords:
        return None
    if geometry_type == 'Polygon':
        return {
            'type': 'Polygon',
            'coordinates': coords,
        }
    if geometry_type == 'MultiPolygon':
        return {
            'type': 'MultiPolygon',
            'coordinates': coords,
        }
    return None


def kubra_provider_state(provider):
    base = (
        'https://kubra.io/stormcenter/api/v1/stormcenters/'
        f'{provider["instance_id"]}/views/{provider["view_id"]}'
    )
    state = fetch_json_url(f'{base}/currentState')
    config = fetch_json_url(f'{base}/configuration/{state["stormcenterDeploymentId"]}')
    inner = config.get('config') or config
    return state, inner


def kubra_provider_summary(name, source_url, summary_doc):
    summary_data = (summary_doc.get('summaryFileData') or {})
    totals = ((summary_data.get('totals') or [{}])[0] or {})
    return {
        'provider': name,
        'source_url': source_url,
        'total_outages': safe_int(totals.get('total_outages')),
        'total_customers_affected': safe_int((totals.get('total_cust_a') or {}).get('val')),
        'total_customers_served': safe_int(totals.get('total_cust_s')),
        'last_updated': summary_data.get('date_generated'),
        'mappable': False,
    }


def geocode_location(query):
    key = normalized_cache_key(query)
    if not key:
        return None
    with GEOCODE_CACHE_LOCK:
        if key in GEOCODE_CACHE:
            return GEOCODE_CACHE[key]

    params = urllib.parse.urlencode({
        'SingleLine': key,
        'f': 'json',
        'outFields': 'Match_addr,Addr_type,Type',
        'maxLocations': 1,
        'outSR': 4326,
    })
    url = 'https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?' + params
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.arcgis.com/',
    })

    result = None
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        for candidate in data.get('candidates') or []:
            if float(candidate.get('score') or 0) < 80:
                continue
            location = candidate.get('location') or {}
            lat = float(location.get('y') or 0)
            lon = float(location.get('x') or 0)
            if in_region(lat, lon):
                result = (lat, lon)
                break
    except Exception:
        result = None

    with GEOCODE_CACHE_LOCK:
        _evict_if_full(GEOCODE_CACHE, GEOCODE_CACHE_MAX_SIZE)
        GEOCODE_CACHE[key] = result
    return result


def parallel_geocode_queries(queries, max_workers=6):
    ordered_keys = []
    seen = set()
    for query in queries:
        key = normalized_cache_key(query)
        if key and key not in seen:
            seen.add(key)
            ordered_keys.append(key)

    if not ordered_keys:
        return {}

    if len(ordered_keys) == 1:
        key = ordered_keys[0]
        return {key: geocode_location(key)}

    futures = {
        GEOCODE_EXECUTOR.submit(geocode_location, key): key
        for key in ordered_keys[:max_workers]
    }
    pending_keys = ordered_keys[max_workers:]
    results = {}

    while futures:
        done, _ = concurrent.futures.wait(
            futures,
            return_when=concurrent.futures.FIRST_COMPLETED
        )
        for future in done:
            key = futures.pop(future)
            try:
                results[key] = future.result()
            except Exception:
                results[key] = None
            if pending_keys:
                next_key = pending_keys.pop(0)
                futures[GEOCODE_EXECUTOR.submit(geocode_location, next_key)] = next_key

    return results


def pulsepoint_secret():
    configured = os.getenv('PULSEPOINT_SECRET', '').strip()
    if configured:
        return configured

    # PulsePoint's public web app derives this client-side value. Keep the same
    # derivation here so the public feed works without a private credential.
    label = 'CommonIncidents'
    return label[13] + label[1] + label[2] + 'brady' + str(5) + 'r' + label[6].lower() + label[5] + 'gs'


def pulsepoint_evp_bytes_to_key(password, salt, key_len=32, iv_len=16):
    data = b''
    prev = b''
    while len(data) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        data += prev
    return data[:key_len], data[key_len:key_len + iv_len]


def decrypt_pulsepoint_payload(content):
    payload = json.loads(content.decode('utf-8') if isinstance(content, bytes) else content)
    ciphertext = base64.b64decode(payload['ct'])
    salt = bytes.fromhex(payload['s']) if payload.get('s') else b''
    iv = bytes.fromhex(payload['iv']) if payload.get('iv') else None
    key, derived_iv = pulsepoint_evp_bytes_to_key(pulsepoint_secret().encode(), salt)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv or derived_iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad_len = padded[-1]
    plaintext = padded[:-pad_len].decode('utf-8')
    return json.loads(json.loads(plaintext))


def pulsepoint_call_info(call_type):
    return PULSEPOINT_CALL_TYPES.get(call_type or 'UNK', (call_type or 'Incident', 'Unknown'))


def pulsepoint_icon(category, description):
    if category == 'Medical':
        return 'medical.png'
    if category in { 'Fire', 'Alarm', 'Explosion' }:
        return 'fire.png'
    if category == 'Vehicle':
        return 'traffic.png'
    if description == 'Police Assist':
        return 'police.png'
    if category in { 'Aid', 'Assist', 'Investigation', 'Lockout', 'Other' }:
        return 'patrol.png'
    return 'warning.png'


def pinellas_icon(call_type, call_code, units):
    type_upper = str(call_type or '').upper()
    code_upper = str(call_code or '').upper()
    unit_text = ' '.join(
        f'{unit.get("ID", "")} {unit.get("Type", "")} {unit.get("Station", "")}'
        for unit in (units or [])
    ).upper()
    if 'MED' in type_upper or code_upper in { 'ME', 'AED' } or any(token in unit_text for token in { 'AMBULANCE', 'RESCUE', 'SQUAD' }):
        return 'medical.png'
    if any(token in type_upper for token in { 'FIRE', 'ALARM', 'SMOKE' }) or code_upper.startswith('F') or any(token in unit_text for token in { 'ENGINE', 'LADDER', 'TRUCK' }):
        return 'fire.png'
    if any(token in type_upper for token in { 'TRAFFIC', 'CRASH', 'ACCIDENT' }) or code_upper in { 'TC', 'TA' }:
        return 'traffic.png'
    if any(token in unit_text for token in { 'PATROL', 'POLICE', 'SHERIFF' }):
        return 'police.png'
    return 'warning.png'


def incident_icon(description, default='warning.png'):
    text = str(description or '').upper()
    if any(token in text for token in { 'MEDICAL', 'OVERDOSE', 'CHEST PAIN', 'STROKE', 'TRAUMA', 'HEMORRHAGE', 'BREATHING', 'FALLS', 'SICK PERSON' }):
        return 'medical.png'
    if any(token in text for token in { 'FIRE', 'SMOKE', 'WILDLAND', 'ALARM', 'EXPLOSION' }):
        return 'fire.png'
    if any(token in text for token in { 'TRAFFIC', 'CRASH', 'VEHICLE', 'TRANSPORT' }):
        return 'traffic.png'
    if any(token in text for token in { 'PATROL', 'SUSPICIOUS', 'ASSIST', 'DETAIL', 'BAKER', 'JUVENILE', 'CIVIL', 'INVESTIGATION', 'FRAUD', 'ALARM-', 'STOP', 'OFFENDER', 'WELL BEING', 'CITIZEN' }):
        return 'police.png'
    return default


def tops_icon(description):
    text = str(description or '').upper()
    if any(token in text for token in { 'TRAFFIC', 'CRASH', 'ACCIDENT', 'HIT AND RUN' }):
        return 'traffic.png'
    if any(token in text for token in { 'FIRE', 'SMOKE', 'EXPLOSION' }):
        return 'fire.png'
    return 'police.png'


def fetch_davnit_emergency():
    req = urllib.request.Request(
        'https://www.davnit.net/esmap/api/incidents/active',
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    features = []
    for feature in data.get('features', []):
        props = feature.get('properties') or {}
        coords = (feature.get('geometry') or {}).get('coordinates') or []
        if len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            continue
        if props.get('source') not in DAVNIT_KEEP_SOURCES or not in_region(lat, lon):
            continue
        features.append(feature)
    return features


def fetch_pulsepoint_agencies():
    now = time.time()
    with PULSEPOINT_AGENCY_CACHE_LOCK:
        cached = PULSEPOINT_AGENCY_CACHE['agencies']
        if cached and now < PULSEPOINT_AGENCY_CACHE['expires_at']:
            return dict(cached)

    def search_state(state_code):
        url = (
            'https://api.pulsepoint.org/v1/webapp?resource=searchagencies&token=' +
            urllib.parse.quote(state_code)
        )
        req = urllib.request.Request(url, headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=12) as response:
            payload = decrypt_pulsepoint_payload(response.read())
        state_suffix = re.compile(rf'\[{re.escape(state_code)}\]\s*$', re.I)
        agencies = {}
        for item in payload.get('searchagencies') or []:
            display_name = display_text(item.get('Display1'))
            if not state_suffix.search(display_name):
                continue
            agency_id = display_text(item.get('agencyid') or item.get('AgencyID'))
            if not agency_id:
                continue
            name = state_suffix.sub('', display_name).strip() or agency_id
            agencies[agency_id] = name
        return agencies

    # The public Respond-for-Web search accepts a two-letter state query. It
    # currently resolves the complete participating-agency set in roughly two
    # seconds, and the 24-hour cache keeps this discovery off the hot path.
    discovered = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(SOURCE_FETCH_WORKERS, 16)
    ) as executor:
        futures = {
            executor.submit(search_state, state_code): state_code
            for state_code in REGIONS
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                discovered.update(future.result())
            except Exception:
                continue

    # Preserve the curated list as a fallback for any state-search failure or
    # transient omission from the upstream directory.
    agencies = dict(PULSEPOINT_AGENCIES)
    agencies.update(discovered)
    with PULSEPOINT_AGENCY_CACHE_LOCK:
        PULSEPOINT_AGENCY_CACHE['agencies'] = dict(agencies)
        PULSEPOINT_AGENCY_CACHE['expires_at'] = time.time() + PULSEPOINT_AGENCY_CACHE_TTL
    return agencies


def fetch_pulsepoint_incidents():
    # PulsePoint silently truncates very large multi-agency requests. Fetching
    # bounded groups keeps every verified agency represented as coverage grows.
    agencies = fetch_pulsepoint_agencies()
    agency_ids = list(agencies)
    chunks = [agency_ids[index:index + 25] for index in range(0, len(agency_ids), 25)]

    def fetch_chunk(chunk):
        value = ','.join(chunk)
        url = f'https://api.pulsepoint.org/v1/webapp?resource=incidents&agencyid={urllib.parse.quote(value)}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = decrypt_pulsepoint_payload(resp.read())
        return (payload.get('incidents') or {}).get('active') or []

    active = []
    successful_chunks = 0
    chunk_errors = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(SOURCE_FETCH_WORKERS, 6, len(chunks))
    ) as executor:
        futures = [executor.submit(fetch_chunk, chunk) for chunk in chunks]
        for future in concurrent.futures.as_completed(futures):
            try:
                active.extend(future.result())
                successful_chunks += 1
            except Exception as exc:
                chunk_errors.append(str(exc))
    if chunks and not successful_chunks:
        raise ValueError('; '.join(chunk_errors) or 'PulsePoint incident requests failed')

    features = []
    seen = set()
    for item in active:
        try:
            lat = float(item.get('Latitude') or 0)
            lon = float(item.get('Longitude') or 0)
        except (TypeError, ValueError):
            continue
        if not in_region(lat, lon):
            continue

        agency_id = str(item.get('AgencyID') or '')
        incident_id = str(item.get('ID') or '')
        unique_key = (agency_id, incident_id)
        if unique_key in seen:
            continue
        seen.add(unique_key)
        call_type = str(item.get('PulsePointIncidentCallType') or 'UNK')
        description, category = pulsepoint_call_info(call_type)
        location = (
            item.get('FullDisplayAddress') or
            item.get('MedicalEmergencyDisplayAddress') or
            'Unknown location'
        )
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': description,
                'icon': pulsepoint_icon(category, description),
                'key': f'pp:{agency_id}:{incident_id}',
                'location': location,
                'source': agencies.get(agency_id, agency_id or 'PulsePoint'),
                'source_id': agency_id,
                'time': item.get('CallReceivedDateTime'),
                'category': category,
                'call_type': call_type,
            }
        })
    return features


def fetch_gfc_wildfires():
    data = fetch_json_url(GFC_WILDFIRE_URL, headers={
        'User-Agent': 'GlobeView/1.0',
        'Accept': 'application/json',
        'Referer': GFC_WILDFIRE_SOURCE_URL,
    })
    features = []
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        if props.get('IS_STATUS_CLOSED') or props.get('IS_STATUS_OUT'):
            continue
        status_label = str(props.get('Status') or '').strip().lower()
        age_days = safe_int(props.get('differenceOfDays'))
        if status_label not in {'active', 'reported'} and age_days > 7:
            continue
        center = geojson_center(feature.get('geometry'))
        if not center:
            continue
        lat, lon = center
        size = safe_float(props.get('Size'))
        contained = safe_float(props.get('Contained'))
        status = display_text(props.get('Status') or 'Reported')
        detail_parts = [status]
        if size > 0:
            detail_parts.append(f'{size:g} acres')
        if contained > 0:
            detail_parts.append(f'{contained:g}% contained')
        name = display_text(props.get('Name') or 'Georgia wildfire')
        county = display_text(props.get('AdminDivision') or '')
        location = f'{name} · {county} County' if county and county.lower() not in name.lower() else name
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': 'Wildfire · ' + ' · '.join(detail_parts),
                'icon': 'fire.png',
                'key': f'gfc:{props.get("Id") or props.get("Code")}',
                'location': location,
                'source': 'Georgia Forestry Commission',
                'source_id': props.get('Code') or props.get('Number'),
                'source_url': GFC_WILDFIRE_SOURCE_URL,
                'time': props.get('StatusUpdatedTimestamp') or props.get('Discovery'),
                'category': 'Wildfire',
                'call_type': props.get('Status'),
            }
        })
    return features


def fetch_scfc_wildfires():
    data = fetch_json_url(SCFC_WILDFIRE_URL, headers={
        'User-Agent': 'GlobeView/1.0',
        'Accept': 'application/json',
        'Referer': SCFC_WILDFIRE_SOURCE_URL,
    })
    features = []
    sc_bounds = REGIONS['SC']['bounds']
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        if props.get('IS_STATUS_CLOSED') or props.get('IS_STATUS_OUT'):
            continue
        status_label = str(props.get('Status') or '').strip().lower()
        age_days = safe_int(props.get('differenceOfDays'))
        if status_label not in {'active', 'reported', 'contained', 'controlled'} and age_days > 7:
            continue
        center = geojson_center(feature.get('geometry'))
        if not center:
            continue
        lat, lon = center
        if not (
            sc_bounds['min_lat'] <= lat <= sc_bounds['max_lat'] and
            sc_bounds['min_lon'] <= lon <= sc_bounds['max_lon']
        ):
            continue
        size = safe_float(props.get('Size'))
        contained = safe_float(props.get('Contained'))
        status = display_text(props.get('Status') or 'Reported')
        detail_parts = [status]
        if size > 0:
            detail_parts.append(f'{size:g} acres')
        if contained > 0:
            detail_parts.append(f'{contained:g}% contained')
        name = display_text(props.get('Name') or 'South Carolina wildfire')
        county = display_text(props.get('AdminDivision') or '')
        location = f'{name} · {county} County' if county and county.lower() not in name.lower() else name
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': 'Wildfire · ' + ' · '.join(detail_parts),
                'icon': 'fire.png',
                'key': f'scfc:{props.get("Id") or props.get("Code")}',
                'location': location,
                'source': 'South Carolina Forestry Commission',
                'source_id': props.get('Code') or props.get('Number'),
                'source_url': SCFC_WILDFIRE_SOURCE_URL,
                'time': props.get('StatusUpdatedTimestamp') or props.get('Discovery'),
                'category': 'Wildfire',
                'call_type': props.get('Status'),
            }
        })
    return features


def fetch_tdf_wildfires():
    data = fetch_json_url(TDF_WILDFIRE_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/geo+json,application/json',
        'Referer': 'https://tn.firesponse.com/public/',
    })
    features = []
    tn_bounds = REGIONS['TN']['bounds']
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        if str(props.get('publicvisibility') or 'Visible').lower() not in {'visible', 'true', '1'}:
            continue
        if props.get('IS_STATUS_CLOSED') or props.get('IS_STATUS_OUT'):
            continue
        status_label = str(props.get('Status') or '').strip().lower()
        age_days = safe_int(props.get('differenceOfDays'))
        if status_label not in {'active', 'reported', 'contained', 'controlled'} and age_days > 7:
            continue
        center = geojson_center(feature.get('geometry'))
        if not center:
            continue
        lat, lon = center
        if not (
            tn_bounds['min_lat'] <= lat <= tn_bounds['max_lat'] and
            tn_bounds['min_lon'] <= lon <= tn_bounds['max_lon']
        ):
            continue
        size = safe_float(props.get('Size'))
        contained = safe_float(props.get('Contained'))
        status = display_text(props.get('Status') or 'Reported')
        details = [status]
        if size > 0:
            details.append(f'{size:g} acres')
        if contained > 0:
            details.append(f'{contained:g}% contained')
        name = display_text(props.get('Name') or 'Tennessee wildfire')
        county = display_text(props.get('AdminDivision') or '')
        location = f'{name} · {county} County' if county and county.lower() not in name.lower() else name
        incident_id = props.get('Id') or props.get('Code') or props.get('Number')
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': 'Wildfire · ' + ' · '.join(details),
                'icon': 'fire.png',
                'key': f'tdf:{incident_id}',
                'location': location,
                'source': 'Tennessee Division of Forestry',
                'source_id': props.get('Code') or props.get('Number') or incident_id,
                'source_url': TDF_WILDFIRE_SOURCE_URL,
                'time': props.get('StatusUpdatedTimestamp') or props.get('Discovery'),
                'category': 'Wildfire',
                'call_type': props.get('Status'),
            },
        })
    return features


def fetch_kdf_wildfires():
    data = fetch_json_url(KDF_WILDFIRE_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/geo+json,application/json',
        'Referer': 'https://kdf.firesponse.com/public/',
    })
    features = []
    ky_bounds = REGIONS['KY']['bounds']
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        if props.get('IS_STATUS_CLOSED') or props.get('IS_STATUS_OUT'):
            continue
        status_label = str(props.get('Status') or '').strip().lower()
        age_days = safe_int(props.get('differenceOfDays'))
        if status_label not in {'active', 'reported', 'contained', 'controlled'} and age_days > 7:
            continue
        center = geojson_center(feature.get('geometry'))
        if not center:
            continue
        lat, lon = center
        if not (
            ky_bounds['min_lat'] <= lat <= ky_bounds['max_lat'] and
            ky_bounds['min_lon'] <= lon <= ky_bounds['max_lon']
        ):
            continue
        size = safe_float(props.get('Size'))
        contained = safe_float(props.get('Contained'))
        status = display_text(props.get('Status') or 'Reported')
        details = [status]
        if size > 0:
            details.append(f'{size:g} acres')
        if contained > 0:
            details.append(f'{contained:g}% contained')
        name = display_text(props.get('Name') or 'Kentucky wildfire')
        county = display_text(props.get('AdminDivision') or '')
        location = f'{name} · {county} County' if county and county.lower() not in name.lower() else name
        incident_id = props.get('Id') or props.get('Code') or props.get('Number')
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': 'Wildfire · ' + ' · '.join(details),
                'icon': 'fire.png',
                'key': f'kdf:{incident_id}',
                'location': location,
                'source': 'Kentucky Division of Forestry',
                'source_id': props.get('Code') or props.get('Number') or incident_id,
                'source_url': KDF_WILDFIRE_SOURCE_URL,
                'time': props.get('StatusUpdatedTimestamp') or props.get('Discovery'),
                'category': 'Wildfire',
                'call_type': props.get('Status'),
            },
        })
    return features


def fetch_ncfs_wildfires():
    data = fetch_json_url(NCFS_WILDFIRE_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/geo+json,application/json',
        'Referer': NCFS_WILDFIRE_SOURCE_URL,
    })
    features = []
    nc_bounds = REGIONS['NC']['bounds']
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        if str(props.get('publicvisibility') or 'Visible').lower() != 'visible':
            continue
        center = geojson_center(feature.get('geometry'))
        if not center:
            continue
        lat, lon = center
        if not (
            nc_bounds['min_lat'] <= lat <= nc_bounds['max_lat'] and
            nc_bounds['min_lon'] <= lon <= nc_bounds['max_lon']
        ):
            continue
        status = display_text(props.get('statusname') or 'Reported')
        size = safe_float(props.get('size'))
        contained = safe_float(props.get('containment'))
        detail_parts = [status]
        if size > 0:
            detail_parts.append(f'{size:g} {str(props.get("sizeunit") or "acres").lower()}')
        if contained > 0:
            detail_parts.append(f'{contained:g}% contained')
        name = display_text(props.get('name') or f'Wildfire {props.get("number") or ""}')
        county = display_text(props.get('admindivision') or '')
        location = f'{name} · {county}' if county and county.lower() not in name.lower() else name
        incident_id = props.get('id') or props.get('number') or feature.get('id')
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': 'Wildfire · ' + ' · '.join(detail_parts),
                'icon': 'fire.png',
                'key': f'ncfs:{incident_id}',
                'location': location,
                'source': 'North Carolina Forest Service',
                'source_id': props.get('number') or incident_id,
                'source_url': NCFS_WILDFIRE_SOURCE_URL,
                'time': props.get('lastupdated') or props.get('statustimestamp'),
                'category': 'Wildfire',
                'call_type': props.get('statusname'),
            },
        })
    return features


def fetch_afc_wildfires():
    data = fetch_json_url(AFC_WILDFIRE_URL, headers={
        'User-Agent': 'GlobeView/1.0',
        'Accept': 'application/geo+json,application/json',
        'Referer': AFC_WILDFIRE_SOURCE_URL,
    })
    features = []
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        center = geojson_center(feature.get('geometry'))
        if not center:
            continue
        lat, lon = center
        wildfire_id = props.get('WildfireID') or feature.get('id')
        name = display_text(props.get('Name'))
        county = display_text(props.get('County'))
        location = name or (f'{county} County' if county else 'Alabama')
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': 'Active Wildfire',
                'icon': 'fire.png',
                'key': f'afc:{wildfire_id}',
                'location': location,
                'source': 'Alabama Forestry Commission',
                'source_id': wildfire_id,
                'source_url': AFC_WILDFIRE_SOURCE_URL,
                'time': None,
                'category': 'Wildfire',
                'call_type': 'Active Wildfire',
            }
        })
    return features


def epoch_milliseconds_iso(value):
    try:
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return value


def fetch_wfigs_state_wildfires(state_code, source_url=WFIGS_WILDFIRE_SOURCE_URL):
    state = REGIONS[state_code]
    state_bounds = state['bounds']
    params = urllib.parse.urlencode({
        'where': "IncidentTypeCategory='WF'",
        'geometry': ','.join(str(value) for value in (
            state_bounds['min_lon'], state_bounds['min_lat'], state_bounds['max_lon'], state_bounds['max_lat'],
        )),
        'geometryType': 'esriGeometryEnvelope',
        'spatialRel': 'esriSpatialRelIntersects',
        'inSR': 4326,
        'outSR': 4326,
        'outFields': (
            'IncidentName,IncidentSize,PercentContained,DiscoveryAcres,FireDiscoveryDateTime,'
            'POOState,POOCounty,UniqueFireIdentifier,ModifiedOnDateTime_dt'
        ),
        'returnGeometry': 'true',
        'f': 'geojson',
    })
    data = fetch_json_url(f'{WFIGS_WILDFIRE_URL}?{params}', headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/geo+json,application/json',
    })
    features = []
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        center = geojson_center(feature.get('geometry'))
        if not center:
            continue
        lat, lon = center
        if not point_in_state(state_code, lat, lon):
            continue
        incident_id = props.get('UniqueFireIdentifier') or feature.get('id')
        size = safe_float(props.get('IncidentSize') or props.get('DiscoveryAcres'))
        contained = safe_float(props.get('PercentContained'))
        detail_parts = ['Active Wildfire']
        if size > 0:
            detail_parts.append(f'{size:g} acres')
        if contained > 0:
            detail_parts.append(f'{contained:g}% contained')
        name = display_text(props.get('IncidentName') or f'{state["name"]} wildfire')
        county = display_text(props.get('POOCounty'))
        location = f'{name} · {county} County' if county and county.lower() not in name.lower() else name
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': ' · '.join(detail_parts),
                'icon': 'fire.png',
                'key': f'wfigs-{state_code.lower()}:{incident_id}',
                'location': location,
                'source': 'NIFC / WFIGS',
                'source_id': incident_id,
                'source_url': source_url,
                'time': epoch_milliseconds_iso(
                    props.get('ModifiedOnDateTime_dt') or props.get('FireDiscoveryDateTime')
                ),
                'category': 'Wildfire',
                'call_type': 'Active Wildfire',
            },
        })
    return features


def fetch_wfigs_mississippi_wildfires():
    return fetch_wfigs_state_wildfires('MS')


def fetch_wfigs_virginia_wildfires():
    return fetch_wfigs_state_wildfires('VA', VDOF_WILDFIRE_SOURCE_URL)


def fetch_wfigs_west_virginia_wildfires():
    return fetch_wfigs_state_wildfires('WV')


def fetch_wfigs_maryland_wildfires():
    return fetch_wfigs_state_wildfires('MD')


def fetch_wfigs_dc_wildfires():
    return fetch_wfigs_state_wildfires('DC')


def fetch_wfigs_delaware_wildfires():
    return fetch_wfigs_state_wildfires('DE')


def fetch_wfigs_pennsylvania_wildfires():
    return fetch_wfigs_state_wildfires('PA')


def fetch_wfigs_new_jersey_wildfires():
    return fetch_wfigs_state_wildfires('NJ')


def fetch_wfigs_connecticut_wildfires():
    return fetch_wfigs_state_wildfires('CT')


def fetch_wfigs_rhode_island_wildfires():
    return fetch_wfigs_state_wildfires('RI')


def fetch_wfigs_massachusetts_wildfires():
    return fetch_wfigs_state_wildfires('MA')


def fetch_wfigs_new_hampshire_wildfires():
    return fetch_wfigs_state_wildfires('NH')


def fetch_wfigs_vermont_wildfires():
    return fetch_wfigs_state_wildfires('VT')


def fetch_wfigs_maine_wildfires():
    return fetch_wfigs_state_wildfires('ME')


def fetch_wfigs_new_york_wildfires():
    return fetch_wfigs_state_wildfires('NY')


def fetch_wfigs_ohio_wildfires():
    return fetch_wfigs_state_wildfires('OH')


def fetch_wfigs_indiana_wildfires():
    return fetch_wfigs_state_wildfires('IN')


def fetch_wfigs_illinois_wildfires():
    return fetch_wfigs_state_wildfires('IL')


def fetch_wfigs_wisconsin_wildfires():
    return fetch_wfigs_state_wildfires('WI')


def fetch_wfigs_minnesota_wildfires():
    return fetch_wfigs_state_wildfires('MN')


def fetch_wfigs_iowa_wildfires():
    return fetch_wfigs_state_wildfires('IA')


def fetch_wfigs_missouri_wildfires():
    return fetch_wfigs_state_wildfires('MO')


def fetch_wfigs_arkansas_wildfires():
    return fetch_wfigs_state_wildfires('AR')


def fetch_wfigs_louisiana_wildfires():
    return fetch_wfigs_state_wildfires('LA')


def fetch_wfigs_oklahoma_wildfires():
    return fetch_wfigs_state_wildfires('OK')


def fetch_wfigs_texas_wildfires():
    return fetch_wfigs_state_wildfires('TX')


def fetch_wfigs_new_mexico_wildfires():
    return fetch_wfigs_state_wildfires('NM')


def fetch_wfigs_arizona_wildfires():
    return fetch_wfigs_state_wildfires('AZ')


def fetch_wfigs_california_wildfires():
    return fetch_wfigs_state_wildfires('CA', 'https://www.fire.ca.gov/incidents')


def fetch_wfigs_nevada_wildfires():
    return fetch_wfigs_state_wildfires('NV')


def fetch_wfigs_oregon_wildfires():
    return fetch_wfigs_state_wildfires('OR')


def fetch_wfigs_washington_wildfires():
    return fetch_wfigs_state_wildfires('WA')


def fetch_wfigs_idaho_wildfires():
    return fetch_wfigs_state_wildfires('ID')


def fetch_wfigs_utah_wildfires():
    return fetch_wfigs_state_wildfires('UT')


def fetch_wfigs_colorado_wildfires():
    return fetch_wfigs_state_wildfires('CO')


def fetch_wfigs_michigan_wildfires():
    return fetch_wfigs_state_wildfires('MI')


def fetch_wfigs_wyoming_wildfires():
    return fetch_wfigs_state_wildfires('WY')


def fetch_wfigs_montana_wildfires():
    return fetch_wfigs_state_wildfires('MT')


def fetch_wfigs_north_dakota_wildfires():
    return fetch_wfigs_state_wildfires('ND')


def fetch_wfigs_south_dakota_wildfires():
    return fetch_wfigs_state_wildfires('SD')


def fetch_wfigs_nebraska_wildfires():
    return fetch_wfigs_state_wildfires('NE')


def fetch_wfigs_kansas_wildfires():
    return fetch_wfigs_state_wildfires('KS')


def fetch_wfigs_alaska_wildfires():
    return fetch_wfigs_state_wildfires('AK')


def fetch_wfigs_hawaii_wildfires():
    return fetch_wfigs_state_wildfires('HI')


def notify_nyc_location(headline, description, area_description):
    parts = [part.strip() for part in str(headline or '').split(' - ') if part.strip()]
    if len(parts) >= 3:
        candidate = re.sub(r'\s*\([A-Z/]+\)\s*$', '', parts[-1]).strip()
        if candidate and candidate.casefold() not in {
            'nyc', 'new york city', 'notification', 'update',
        }:
            return candidate
    text = display_text(description)
    match = re.search(
        r'\b(?:area of|at|near)\s+([^.;]{4,100}?)(?=\s+in\s+(?:the\s+)?'
        r'(?:Bronx|Brooklyn|Manhattan|Queens|Staten Island)|[.;]|$)',
        text,
        re.I,
    )
    if match:
        return match.group(1).strip(' ,')
    return display_text(area_description) or 'New York City'


def notify_nyc_cap_center(area):
    coordinates = []
    for polygon in area.findall('{urn:oasis:names:tc:emergency:cap:1.2}polygon'):
        for pair in str(polygon.text or '').split():
            values = pair.split(',', 1)
            if len(values) != 2:
                continue
            lat = optional_float(values[0])
            lon = optional_float(values[1])
            if lat is not None and lon is not None:
                coordinates.append((lat, lon))
    for circle in area.findall('{urn:oasis:names:tc:emergency:cap:1.2}circle'):
        pair = str(circle.text or '').split()[0].split(',', 1)
        if len(pair) == 2:
            lat = optional_float(pair[0])
            lon = optional_float(pair[1])
            if lat is not None and lon is not None:
                coordinates.append((lat, lon))
    if not coordinates:
        return None
    lats = [coord[0] for coord in coordinates]
    lons = [coord[1] for coord in coordinates]
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2


def fetch_notify_nyc_incidents():
    req = urllib.request.Request(NOTIFY_NYC_RSS_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/rss+xml,application/xml,text/xml',
    })
    raw = urllib.request.urlopen(req, timeout=25).read()
    rss = ET.fromstring(raw)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=NOTIFY_NYC_MAX_AGE_HOURS
    )
    active_items = []
    for item in rss.findall('./channel/item'):
        author = str(item.findtext('author') or '')
        if '[English]' not in author:
            continue
        try:
            published = datetime.datetime.strptime(
                str(item.findtext('pubDate') or ''), '%a, %d %b %Y %H:%M:%S GMT'
            ).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if published < cutoff:
            continue
        cap_url = str(item.findtext('link') or '').strip()
        if not cap_url.startswith('https://feeds.everbridge.net/'):
            continue
        active_items.append((published, cap_url))

    features = []
    for published, cap_url in active_items[:24]:
        cap_req = urllib.request.Request(cap_url, headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/xml,text/xml',
        })
        cap = ET.fromstring(urllib.request.urlopen(cap_req, timeout=20).read())
        ns = '{urn:oasis:names:tc:emergency:cap:1.2}'
        if str(cap.findtext(f'{ns}status') or '').casefold() != 'actual':
            continue
        if str(cap.findtext(f'{ns}msgType') or '').casefold() in {'cancel', 'ack', 'error'}:
            continue
        info = cap.find(f'{ns}info')
        if info is None or not str(info.findtext(f'{ns}language') or '').lower().startswith('en'):
            continue
        headline = display_text(info.findtext(f'{ns}headline') or 'Notify NYC alert')
        detail = display_text(info.findtext(f'{ns}description') or headline)
        if re.search(r'\b(reopened|restoration|restored|resolved|cancell?ed|all clear)\b', headline, re.I):
            continue
        expires_text = str(info.findtext(f'{ns}expires') or '').strip()
        if expires_text:
            try:
                expires = datetime.datetime.fromisoformat(expires_text.replace('Z', '+00:00'))
                if expires.astimezone(datetime.timezone.utc) <= datetime.datetime.now(datetime.timezone.utc):
                    continue
            except ValueError:
                pass
        area = info.find(f'{ns}area')
        if area is None:
            continue
        center = notify_nyc_cap_center(area)
        if not center or not point_in_state('NY', center[0], center[1]):
            continue
        area_description = display_text(area.findtext(f'{ns}areaDesc'))
        location = notify_nyc_location(headline, detail, area_description)
        icon = incident_icon(f'{headline} {detail}', default='warning.png')
        category = 'Public Safety'
        if icon == 'police.png':
            category = 'Police'
        elif icon == 'fire.png':
            category = 'Fire/EMS'
        elif icon == 'traffic.png':
            category = 'Traffic'
        identifier = str(cap.findtext(f'{ns}identifier') or hashlib.sha1(cap_url.encode()).hexdigest()[:16])
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [center[1], center[0]]},
            'properties': {
                'description': headline,
                'details': detail,
                'icon': icon,
                'key': f'notify-nyc:{identifier}',
                'location': location,
                'source': 'NYC Emergency Management / Notify NYC',
                'source_id': identifier,
                'source_url': cap_url or NOTIFY_NYC_SOURCE_URL,
                'time': cap.findtext(f'{ns}sent') or published.isoformat(),
                'category': category,
                'call_type': info.findtext(f'{ns}event') or headline,
            },
        })
    return features


def alertdc_location_candidate(text):
    value = html.unescape(str(text or '')).replace('&#xA;', ' ')
    block_match = re.search(
        r'\b(\d{1,5}(?:\s*-\s*\d{1,5})?\s+blocks?\s+of\s+'
        r"[A-Za-z0-9 .'-]+?(?:NW|NE|SE|SW))\b",
        value,
        re.I,
    )
    if block_match:
        location = block_match.group(1)
        location = re.sub(r'\s*-\s*\d{1,5}', '', location, count=1)
        return re.sub(r'\s+blocks?\s+of\s+', ' ', location, flags=re.I).strip(' ,')

    interstate_match = re.search(
        r'\b((?:northbound|southbound|eastbound|westbound|N/B|S/B|E/B|W/B)?\s*'
        r'I-\d+(?:\s+at\s+(?:the\s+)?[^.;]{3,70})?)',
        value,
        re.I,
    )
    if interstate_match:
        return ' '.join(interstate_match.group(1).split()).strip(' ,')

    route_match = re.search(
        r'\b((?:northbound|southbound|eastbound|westbound|N/B|S/B|E/B|W/B)?\s*'
        r'(?:US-?\d+|[A-Z][A-Za-z0-9 .\'-]+(?:Bridge|Road|Street|Avenue|Boulevard|Parkway))'
        r'(?:\s+(?:NW|NE|SE|SW))?(?:\s+(?:at|near|between)\s+[^.;]{3,70})?)',
        value,
        re.I,
    )
    if route_match:
        return ' '.join(route_match.group(1).split()).strip(' ,')
    return ''


def fetch_alertdc_incidents():
    req = urllib.request.Request(ALERTDC_FEED_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'text/html,application/xhtml+xml',
    })
    html_text = urllib.request.urlopen(req, timeout=25).read().decode('utf-8', 'replace')
    cutoff = datetime.datetime.now(LOCAL_TZ) - datetime.timedelta(hours=ALERTDC_MAX_AGE_HOURS)
    records = []
    for row in re.findall(r'<tr>(.*?)</tr>', html_text, re.I | re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.I | re.S)
        if len(cells) < 2:
            continue
        when_text = strip_tags(cells[0])
        when_iso = parse_time_iso(when_text, ['%m/%d/%Y %I:%M %p'])
        try:
            when_dt = datetime.datetime.fromisoformat(when_iso) if when_iso else None
        except ValueError:
            when_dt = None
        if not when_dt or when_dt < cutoff:
            continue

        link_match = re.search(r'<a\s+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', cells[1], re.I | re.S)
        if not link_match:
            continue
        relative_url = html.unescape(link_match.group(1))
        title = strip_tags(link_match.group(2))
        combined = strip_tags(cells[1])
        details = combined[len(title):].strip() if combined.startswith(title) else combined
        status_text = f'{title} {details}'.lower()
        if (
            'final update' in status_text or
            'has cleared' in status_text or
            'have reopened' in status_text or
            'is resolved' in status_text
        ):
            continue
        incident_id_match = re.search(r'/AlertDetails/(\d+)', relative_url, re.I)
        incident_id = incident_id_match.group(1) if incident_id_match else hashlib.md5(relative_url.encode()).hexdigest()[:16]
        location = alertdc_location_candidate(details) or alertdc_location_candidate(title)
        records.append({
            'incident_id': incident_id,
            'title': title,
            'details': details,
            'location': location,
            'query': geocode_query(location, 'Washington, D.C.') if location else '',
            'time': when_iso,
            'source_url': urllib.parse.urljoin(ALERTDC_FEED_URL, relative_url),
        })

    geocodes = parallel_geocode_queries(record['query'] for record in records if record['query'])
    features = []
    dc_center = (38.9072, -77.0369)
    for record in records:
        coords = geocodes.get(normalized_cache_key(record['query'])) if record['query'] else None
        lat, lon = coords or dc_center
        description = display_text(record['title'] or 'D.C. public safety alert')
        detail_text = record['details'] or description
        icon = incident_icon(detail_text, default='warning.png')
        category = 'Public Safety'
        if icon == 'police.png' or 'crime alert' in description.lower():
            category = 'Police'
            icon = 'police.png'
        elif icon == 'fire.png':
            category = 'Fire/EMS'
        elif icon == 'traffic.png':
            category = 'Traffic'
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': description,
                'details': detail_text,
                'icon': icon,
                'key': f'alertdc:{record["incident_id"]}',
                'location': record['location'] or 'Washington, D.C.',
                'source': 'D.C. HSEMA AlertDC',
                'source_id': record['incident_id'],
                'source_url': record['source_url'],
                'time': record['time'],
                'category': category,
                'call_type': record['title'],
            },
        })
    return features


def nws_alert_icon(event):
    label = str(event or '').lower()
    if any(token in label for token in ('fire', 'red flag', 'smoke')):
        return 'fire.png'
    if any(token in label for token in ('flood', 'tornado', 'thunderstorm', 'hurricane', 'storm', 'wind', 'heat', 'freeze', 'winter')):
        return 'warning.png'
    return 'warning.png'


def nws_zone_state_code(zone_url):
    parsed = urllib.parse.urlparse(str(zone_url or ''))
    zone_id = parsed.path.rstrip('/').rsplit('/', 1)[-1].upper()
    state_code = zone_id[:2]
    return state_code if state_code in REGIONS else None


def nws_zone_center(zone_url):
    url = str(zone_url or '').strip()
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != 'https' or parsed.hostname != 'api.weather.gov' or
        not parsed.path.startswith('/zones/')
    ):
        return None

    now = time.time()
    with NWS_ZONE_CACHE_LOCK:
        cached = NWS_ZONE_CACHE.get(url)
        if cached and now < cached['expires_at']:
            return cached['center']

    center = None
    try:
        data = fetch_json_url(url, headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Accept': 'application/geo+json,application/json',
        }, timeout=12)
        center = geojson_center(data.get('geometry'))
    except Exception:
        center = None

    with NWS_ZONE_CACHE_LOCK:
        _evict_if_full(NWS_ZONE_CACHE, NWS_ZONE_CACHE_MAX_SIZE)
        NWS_ZONE_CACHE[url] = {
            'center': center,
            'expires_at': time.time() + NWS_ZONE_CACHE_TTL,
        }
    return center


def fetch_nws_emergency_alerts():
    data = fetch_json_url(NWS_ALERTS_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/geo+json,application/json',
    }, timeout=20)
    alerts = data.get('features') or []

    # NWS omits geometry for many county/forecast-zone advisories. Resolve one
    # representative official NWS zone per affected state in parallel, instead
    # of making 49 state API calls followed by serial third-party geocoding.
    alert_records = []
    requested_zone_urls = []
    for feature in alerts:
        props = feature.get('properties') or {}
        center = geojson_center(feature.get('geometry'))
        state_zone_urls = {}
        if not center:
            for zone_url in props.get('affectedZones') or []:
                state_code = nws_zone_state_code(zone_url)
                if state_code and state_code not in state_zone_urls:
                    state_zone_urls[state_code] = zone_url
                    requested_zone_urls.append(zone_url)
        alert_records.append((feature, props, center, state_zone_urls))

    zone_centers = {}
    unique_zone_urls = list(dict.fromkeys(requested_zone_urls))
    if unique_zone_urls:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(SOURCE_FETCH_WORKERS, 16, len(unique_zone_urls))
        ) as executor:
            future_map = {
                executor.submit(nws_zone_center, zone_url): zone_url
                for zone_url in unique_zone_urls
            }
            for future in concurrent.futures.as_completed(future_map):
                zone_url = future_map[future]
                try:
                    zone_centers[zone_url] = future.result()
                except Exception:
                    zone_centers[zone_url] = None

    features = []
    seen = set()
    for feature, props, center, state_zone_urls in alert_records:
        alert_id = str(
            props.get('id') or feature.get('id') or
            hashlib.md5(json.dumps(props, sort_keys=True, default=str).encode()).hexdigest()[:16]
        )
        centers = [(None, center)] if center else []
        if not centers:
            for state_code, zone_url in state_zone_urls.items():
                zone_center = zone_centers.get(zone_url) or state_reference_point(state_code)
                if zone_center:
                    centers.append((state_code, zone_center))

        for state_code, marker_center in centers:
            if not marker_center or not in_region(*marker_center):
                continue
            marker_key = f'nws:{alert_id}:{state_code or "geometry"}'
            if marker_key in seen:
                continue
            seen.add(marker_key)
            lat, lon = marker_center
            event = display_text(props.get('event') or 'Weather Alert')
            severity = display_text(props.get('severity') or '')
            description = f'{event} · {severity}' if severity else event
            features.append({
                'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                'properties': {
                    'description': description,
                    'icon': nws_alert_icon(event),
                    'key': marker_key,
                    'location': props.get('areaDesc') or (
                        REGIONS[state_code]['name'] if state_code else 'Affected area'
                    ),
                    'source': 'NOAA / National Weather Service',
                    'source_id': alert_id,
                    'source_url': props.get('@id') or props.get('id') or 'https://www.weather.gov/',
                    'time': props.get('onset') or props.get('effective') or props.get('sent'),
                    'category': 'Weather Alert',
                    'call_type': props.get('event'),
                    'state': state_code,
                }
            })
    return features


def fetch_eccc_emergency_alerts():
    """Return active official Environment Canada alerts as map markers."""
    data = fetch_json_url(ECCC_WEATHER_ALERTS_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/geo+json,application/json',
    }, timeout=30)
    features = []
    seen = set()
    now = datetime.datetime.now(datetime.timezone.utc)
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        if str(props.get('status_en') or '').lower() == 'ended':
            continue
        try:
            expires = datetime.datetime.fromisoformat(
                str(props.get('expiration_datetime') or '').replace('Z', '+00:00')
            )
        except ValueError:
            expires = None
        if expires and expires < now:
            continue
        center = geojson_center(feature.get('geometry'))
        province = str(props.get('province') or '').strip().upper()
        if not center or province not in REGIONS:
            continue
        lat, lon = center
        if not point_in_state(province, lat, lon):
            continue
        alert_id = str(feature.get('id') or props.get('id') or '')
        key = f'eccc:{alert_id}'
        if not alert_id or key in seen:
            continue
        seen.add(key)
        event = display_text(
            props.get('alert_short_name_en') or props.get('alert_name_en') or 'Weather Alert'
        )
        alert_type = display_text(props.get('alert_type')).title()
        risk = display_text(props.get('risk_colour_en')).title()
        description = ' · '.join(part for part in (event, alert_type, risk) if part)
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'description': description or event,
                'details': display_text(props.get('alert_text_en')),
                'icon': nws_alert_icon(event),
                'key': key,
                'location': display_text(props.get('feature_name_en')) or REGIONS[province]['name'],
                'source': 'Environment and Climate Change Canada',
                'source_id': alert_id,
                'source_url': 'https://weather.gc.ca/',
                'time': props.get('publication_datetime') or props.get('validity_datetime'),
                'category': 'Weather Alert',
                'call_type': props.get('alert_name_en'),
                'state': province,
            },
        })
    return features


def fetch_pinellas_emergency():
    req = urllib.request.Request(
        PINELLAS_ACTIVITY_URL,
        headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://911.pinellas.gov/',
        }
    )
    resp = urllib.request.urlopen(req, timeout=20)
    data = json.loads(resp.read())
    features = []
    for item in data.get('CallInfo', []):
        try:
            lat = float(item.get('Lat') or 0)
            lon = float(item.get('Lon') or 0)
        except (TypeError, ValueError):
            continue
        if not in_region(lat, lon):
            continue
        units = item.get('Units') or []
        description = display_text(item.get('Type') or item.get('Code') or 'Incident')
        location = item.get('Location') or item.get('Grid') or 'Unknown location'
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': description,
                'icon': pinellas_icon(item.get('Type'), item.get('Code'), units),
                'key': f'pinellas:{item.get("IncidentNo")}',
                'location': location,
                'source': 'Pinellas 911',
                'source_id': item.get('IncidentNo'),
                'time': local_time_iso(item.get('Received')) or item.get('Received'),
                'category': 'Pinellas',
                'call_type': item.get('Code') or item.get('Type'),
            }
        })
    return features


def fetch_pinellas_sheriff_calls():
    req = urllib.request.Request(
        PINELLAS_SHERIFF_CALLS_URL,
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    html_text = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    rows = re.findall(
        r'<tr><td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td></tr>',
        html_text,
        re.S
    )
    records = []
    for report, when, problem, address, units in rows:
        report = strip_tags(report)
        address = strip_tags(address)
        problem = display_text(strip_tags(problem))
        query = f'{address}, Pinellas County, Florida'
        records.append({
            'report': report,
            'when': when,
            'problem': problem,
            'address': address,
            'query': query,
        })

    geocodes = parallel_geocode_queries((record['query'] for record in records))
    features = []
    for record in records:
        coords = geocodes.get(normalized_cache_key(record['query']))
        if not coords:
            continue
        lat, lon = coords
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': record['problem'] or 'Police Call',
                'icon': incident_icon(record['problem'], default='police.png'),
                'key': f'pcso:{record["report"]}',
                'location': record['address'] or 'Unknown location',
                'source': 'Pinellas Sheriff',
                'source_id': record['report'],
                'time': parse_time_iso(strip_tags(record['when']), ['%m/%d/%Y %I:%M:%S %p']) or strip_tags(record['when']),
                'category': 'Police',
                'call_type': record['problem'],
            }
        })
    return features


def fetch_marion_fire_calls():
    req = urllib.request.Request(
        MARION_FIRE_CALLS_URL,
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    html_text = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    rows = re.findall(r'<tr>(.*?)</tr>', html_text, re.S)
    records = []
    for row in rows:
        cells = [strip_tags(cell) for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(cells) != 6:
            continue
        when, incident_id, call_type, units, status, location = cells
        if not incident_id:
            continue
        query = f'{location}, Marion County, Florida'
        records.append({
            'when': when,
            'incident_id': incident_id,
            'call_type': call_type,
            'location': location,
            'query': query,
        })

    geocodes = parallel_geocode_queries((record['query'] for record in records))
    features = []
    for record in records:
        coords = geocodes.get(normalized_cache_key(record['query']))
        if not coords:
            continue
        lat, lon = coords
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': display_text(record['call_type']) or 'Fire/Rescue Call',
                'icon': incident_icon(record['call_type'], default='fire.png'),
                'key': f'marion:{record["incident_id"]}',
                'location': display_text(record['location']) or 'Unknown location',
                'source': 'Marion Fire/Rescue',
                'source_id': record['incident_id'],
                'time': parse_time_iso(record['when'], ['%b %d, %H:%M']) or record['when'],
                'category': 'Fire/EMS',
                'call_type': record['call_type'],
            }
        })
    return features


def fetch_martin_fire_calls():
    req = urllib.request.Request(
        MARTIN_FIRE_CALLS_URL,
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    html_text = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    match = re.search(r'Last update\s+(\d+/\d+/\d+\s+\d+:\d+:\d+)', html_text, re.I)
    last_update = match.group(1) if match else ''
    rows = re.findall(r'<tr>(.*?)</tr>', html_text, re.S)
    records = []
    for row in rows:
        cells = [strip_tags(cell) for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(cells) < 8 or not re.fullmatch(r'\d+', cells[0]):
            continue
        incident_id, unit, status, prefix, street, suffix, code, call_type = cells[:8]
        location = ' '.join(part for part in [prefix, street, suffix] if part)
        query = f'{location}, Martin County, Florida'
        records.append({
            'incident_id': incident_id,
            'code': code,
            'call_type': call_type,
            'location': location,
            'query': query,
        })

    geocodes = parallel_geocode_queries((record['query'] for record in records))
    features = []
    for record in records:
        coords = geocodes.get(normalized_cache_key(record['query']))
        if not coords:
            continue
        lat, lon = coords
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': display_text(record['call_type']) or 'Fire/Rescue Call',
                'icon': incident_icon(record['call_type'], default='medical.png'),
                'key': f'martin:{record["incident_id"]}',
                'location': display_text(record['location']) or 'Unknown location',
                'source': 'Martin Fire Rescue',
                'source_id': record['incident_id'],
                'time': parse_time_iso(last_update, ['%m/%d/%Y %H:%M:%S']) or last_update,
                'category': 'Fire/EMS',
                'call_type': record['code'] or record['call_type'],
            }
        })
    return features


def tampa_fire_grid_info(grid):
    key = str(grid or '').strip()
    if not key:
        return None
    with TAMPA_FIRE_GRID_CACHE_LOCK:
        if key in TAMPA_FIRE_GRID_CACHE:
            return TAMPA_FIRE_GRID_CACHE[key]

    if not re.fullmatch(r'[\w\-]{1,20}', key):
        return None
    where = f'LABEL = {int(key)}' if key.isdigit() and len(key) <= 3 else f"FIRE_GRID = '{key}'"
    params = urllib.parse.urlencode({
        'where': where,
        'outFields': 'LABEL,COMMUNITY,FIRESTATION,FIRE_GRID',
        'returnGeometry': 'true',
        'f': 'json',
        'outSR': 4326,
    })
    req = urllib.request.Request(
        f'{TAMPA_FIRE_GRID_QUERY_URL}?{params}',
        headers={'User-Agent': 'Mozilla/5.0'}
    )

    result = None
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        feature = (data.get('features') or [None])[0] or {}
        attrs = feature.get('attributes') or {}
        coords = rings_center((feature.get('geometry') or {}).get('rings'))
        if coords:
            result = {
                'coords': coords,
                'community': display_text(attrs.get('COMMUNITY')),
                'label': attrs.get('LABEL') or attrs.get('FIRE_GRID') or key,
                'station': attrs.get('FIRESTATION'),
            }
    except Exception:
        result = None

    with TAMPA_FIRE_GRID_CACHE_LOCK:
        _evict_if_full(TAMPA_FIRE_GRID_CACHE, TAMPA_FIRE_GRID_CACHE_MAX_SIZE)
        TAMPA_FIRE_GRID_CACHE[key] = result
    return result


def fetch_miami_dade_fire_calls():
    req = urllib.request.Request(
        MIAMI_DADE_FIRE_CALLS_URL,
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    html_text = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    rows = re.findall(r'<tr class="(?:odd|even)"\s*>(.*?)</tr>', html_text, re.S)
    records = []
    for row in rows:
        cells = [strip_tags(cell) for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(cells) != 5:
            continue
        when, fire_code, incident_type, address, units = cells
        query = geocode_query(address, 'Miami-Dade County, Florida')
        source_id = hashlib.md5(f'{when}|{incident_type}|{address}'.encode('utf-8')).hexdigest()[:16]
        records.append({
            'when': when,
            'fire_code': fire_code,
            'incident_type': incident_type,
            'address': address,
            'query': query,
            'source_id': source_id,
        })

    geocodes = parallel_geocode_queries((record['query'] for record in records))
    features = []
    for record in records:
        coords = geocodes.get(normalized_cache_key(record['query']))
        if not coords:
            continue
        lat, lon = coords
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': display_text(record['incident_type']) or 'Fire/Rescue Call',
                'icon': incident_icon(record['incident_type'], default='fire.png'),
                'key': f'mdfr:{record["source_id"]}',
                'location': record['address'] or 'Unknown location',
                'source': 'Miami-Dade Fire Rescue',
                'source_id': record['source_id'],
                'time': local_time_iso(record['when']) or record['when'],
                'category': 'Fire/EMS',
                'call_type': record['fire_code'] or record['incident_type'],
            }
        })
    return features


def fetch_tampa_fire_calls():
    req = urllib.request.Request(
        TAMPA_FIRE_CALLS_URL,
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    rows = json.loads(urllib.request.urlopen(req, timeout=20).read())
    cutoff = datetime.datetime.now(LOCAL_TZ) - datetime.timedelta(hours=TAMPA_FIRE_RECENT_HOURS)
    features = []
    for row in rows:
        dispatched = parse_time_iso(row.get('Dispatched'), ['%m/%d/%Y %I:%M:%S %p'])
        if not dispatched:
            continue
        try:
            dispatched_dt = datetime.datetime.fromisoformat(dispatched)
        except ValueError:
            continue
        if dispatched_dt < cutoff:
            continue

        grid = row.get('Grid')
        grid_info = tampa_fire_grid_info(grid)
        if not grid_info:
            continue
        lat, lon = grid_info['coords']
        location_parts = [f'Grid {grid_info.get("label") or grid}']
        if grid_info.get('community'):
            location_parts.append(grid_info['community'])
        if grid_info.get('station'):
            location_parts.append(f'Station {grid_info["station"]}')
        description = display_text(row.get('Description') or 'Fire/Rescue Call')
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': description,
                'icon': incident_icon(description, default='fire.png'),
                'key': f'tfr:{row.get("Incident")}',
                'location': ' · '.join(part for part in location_parts if part),
                'source': 'Tampa Fire Rescue',
                'source_id': row.get('Incident'),
                'time': dispatched,
                'category': 'Fire/EMS',
                'call_type': row.get('Description'),
            }
        })
    return features


def fetch_jax_sheriff_calls():
    req = urllib.request.Request(
        JAX_SHERIFF_CALLS_URL,
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    html_text = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    rows = re.findall(r"<tr class='closedCall'>(.*?)</tr>", html_text, re.S)
    records = []
    for row in rows[:JAX_SHERIFF_MAX_CALLS]:
        cells = [strip_tags(cell) for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(cells) < 5:
            continue
        incident_id, dispatched, address, signal, description = cells[:5]
        if not incident_id or not address or str(description).upper() == 'CANCEL':
            continue
        query = geocode_query(address, 'Jacksonville, Florida')
        records.append({
            'incident_id': incident_id,
            'dispatched': dispatched,
            'address': address,
            'signal': signal,
            'description': description,
            'query': query,
        })

    geocodes = parallel_geocode_queries((record['query'] for record in records))
    features = []
    for record in records:
        coords = geocodes.get(normalized_cache_key(record['query']))
        if not coords:
            continue
        lat, lon = coords
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': display_text(record['description']) or 'Police Call',
                'icon': incident_icon(record['description'], default='police.png'),
                'key': f'jax:{record["incident_id"]}',
                'location': record['address'],
                'source': 'Jacksonville Sheriff (Completed)',
                'source_id': record['incident_id'],
                'time': parse_time_iso(record['dispatched'], ['%m/%d/%Y %H:%M']) or record['dispatched'],
                'category': 'Police',
                'call_type': record['signal'] or record['description'],
            }
        })
    return features


def fetch_clearwater_police_calls():
    req = urllib.request.Request(
        CLEARWATER_POLICE_CALLS_URL,
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    records = []
    for row in data.get('data') or []:
        address = display_text(row.get('Address'))
        description = display_text(row.get('Online_Description') or 'Police Call')
        query = geocode_query(address, 'Clearwater, Florida')
        records.append({
            'row': row,
            'address': address,
            'description': description,
            'query': query,
        })

    geocodes = parallel_geocode_queries((record['query'] for record in records))
    features = []
    for record in records:
        row = record['row']
        coords = geocodes.get(normalized_cache_key(record['query']))
        if not coords:
            continue
        lat, lon = coords
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': record['description'],
                'icon': incident_icon(record['description'], default='police.png'),
                'key': f'clearwater:{row.get("Master_Incident_Number")}',
                'location': record['address'] or 'Unknown location',
                'source': 'Clearwater Police',
                'source_id': row.get('Master_Incident_Number'),
                'time': parse_time_iso(row.get('Response_Date'), ['%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S']) or row.get('Response_Date'),
                'category': 'Police',
                'call_type': row.get('Online_Description'),
            }
        })
    return features


def fetch_tops_incidents():
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': 'EventDate,IncidentTypeCode,IncidentTypeDescription,EventHeadLine,EventAddress,InitialPriorityKey,CommonPlace,OBJECTID',
        'returnGeometry': 'true',
        'f': 'json',
    })
    req = urllib.request.Request(
        f'{TOPS_ACTIVE_INCIDENTS_URL}?{params}',
        headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': TOPS_REFERER,
        }
    )
    resp = urllib.request.urlopen(req, timeout=20)
    data = json.loads(resp.read())
    features = []
    for feature in data.get('features', []):
        attrs = feature.get('attributes') or {}
        geom = feature.get('geometry') or {}
        try:
            x = float(geom.get('x') or 0)
            y = float(geom.get('y') or 0)
        except (TypeError, ValueError):
            continue
        lat, lon = web_mercator_to_latlon(x, y)
        if not in_region(lat, lon):
            continue
        address = attrs.get('EventAddress') or 'Unknown location'
        commonplace = attrs.get('CommonPlace')
        location = f'{address} · {commonplace}' if commonplace and commonplace not in address else address
        description = display_text(attrs.get('IncidentTypeDescription') or attrs.get('IncidentTypeCode') or 'Incident')
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'description': description,
                'icon': tops_icon(attrs.get('IncidentTypeDescription')),
                'key': f'tops:{attrs.get("OBJECTID")}',
                'location': location,
                'source': 'Tallahassee / Leon TOPS',
                'source_id': attrs.get('OBJECTID'),
                'time': tops_time_iso(attrs.get('EventDate')) or attrs.get('EventDate'),
                'category': 'Police',
                'call_type': attrs.get('IncidentTypeCode'),
            }
        })
    return features


def phoenix_fire_icon(symbol_code, description):
    token = str(symbol_code or '').lower()
    if any(label in token for label in ('fire', 'boatfire', 'trainfire')):
        return 'fire.png', 'Fire'
    if any(label in token for label in ('crash', 'airplane')):
        return 'traffic.png', 'Traffic'
    if any(label in token for label in (
        'bluestar', 'heart', 'medical', 'snake', 'heat', 'bite', 'mryuk'
    )):
        return 'medical.png', 'Medical'
    return incident_icon(description, default='warning.png'), 'Other'


def fetch_phoenix_fire_incidents():
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': '*',
        'returnGeometry': 'true',
        'outSR': '4326',
        'f': 'geojson',
    })
    data = fetch_json_url(
        f'{PHOENIX_FIRE_INCIDENTS_URL}?{params}',
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': PHOENIX_FIRE_SOURCE_URL,
            'Accept': 'application/geo+json,application/json',
        },
        timeout=20,
    )
    features = []
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        coordinates = (feature.get('geometry') or {}).get('coordinates') or []
        if len(coordinates) < 2:
            continue
        lon = optional_float(coordinates[0])
        lat = optional_float(coordinates[1])
        incident_id = display_text(props.get('Incident') or props.get('OBJECTID'))
        if (
            lat is None or lon is None or not incident_id or
            not point_in_state('AZ', lat, lon)
        ):
            continue
        description = display_text(
            props.get('NatureDesc') or props.get('Nature') or 'Fire / EMS Incident'
        )
        icon, category = phoenix_fire_icon(props.get('SymbolCode'), description)
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'description': description,
                'icon': icon,
                'key': f'phoenix-fire:{incident_id}',
                'location': display_text(props.get('GenLocInfo')) or 'Phoenix metro area',
                'source': 'Phoenix Fire Active Incidents',
                'source_id': incident_id,
                'time': epoch_milliseconds_iso(props.get('Date')),
                'category': category,
                'call_type': display_text(props.get('Nature')),
            },
        })
    return features


EMERGENCY_SOURCE_FETCHERS = (
    ('davnit', fetch_davnit_emergency),
    ('pulsepoint', fetch_pulsepoint_incidents),
    ('pinellas', fetch_pinellas_emergency),
    ('pinellas_sheriff', fetch_pinellas_sheriff_calls),
    ('marion', fetch_marion_fire_calls),
    ('martin', fetch_martin_fire_calls),
    ('miami_dade', fetch_miami_dade_fire_calls),
    ('tampa_fire', fetch_tampa_fire_calls),
    ('jax', fetch_jax_sheriff_calls),
    ('clearwater', fetch_clearwater_police_calls),
    ('tops', fetch_tops_incidents),
    ('phoenix_fire', fetch_phoenix_fire_incidents),
    ('gfc_wildfires', fetch_gfc_wildfires),
    ('scfc_wildfires', fetch_scfc_wildfires),
    ('tdf_wildfires', fetch_tdf_wildfires),
    ('kdf_wildfires', fetch_kdf_wildfires),
    ('ncfs_wildfires', fetch_ncfs_wildfires),
    ('afc_wildfires', fetch_afc_wildfires),
    ('wfigs_ms_wildfires', fetch_wfigs_mississippi_wildfires),
    ('wfigs_va_wildfires', fetch_wfigs_virginia_wildfires),
    ('wfigs_wv_wildfires', fetch_wfigs_west_virginia_wildfires),
    ('wfigs_md_wildfires', fetch_wfigs_maryland_wildfires),
    ('wfigs_dc_wildfires', fetch_wfigs_dc_wildfires),
    ('wfigs_de_wildfires', fetch_wfigs_delaware_wildfires),
    ('wfigs_pa_wildfires', fetch_wfigs_pennsylvania_wildfires),
    ('wfigs_nj_wildfires', fetch_wfigs_new_jersey_wildfires),
    ('wfigs_ct_wildfires', fetch_wfigs_connecticut_wildfires),
    ('wfigs_ri_wildfires', fetch_wfigs_rhode_island_wildfires),
    ('wfigs_ma_wildfires', fetch_wfigs_massachusetts_wildfires),
    ('wfigs_nh_wildfires', fetch_wfigs_new_hampshire_wildfires),
    ('wfigs_vt_wildfires', fetch_wfigs_vermont_wildfires),
    ('wfigs_me_wildfires', fetch_wfigs_maine_wildfires),
    ('wfigs_ny_wildfires', fetch_wfigs_new_york_wildfires),
    ('wfigs_oh_wildfires', fetch_wfigs_ohio_wildfires),
    ('wfigs_in_wildfires', fetch_wfigs_indiana_wildfires),
    ('wfigs_il_wildfires', fetch_wfigs_illinois_wildfires),
    ('wfigs_wi_wildfires', fetch_wfigs_wisconsin_wildfires),
    ('wfigs_mn_wildfires', fetch_wfigs_minnesota_wildfires),
    ('wfigs_ia_wildfires', fetch_wfigs_iowa_wildfires),
    ('wfigs_mo_wildfires', fetch_wfigs_missouri_wildfires),
    ('wfigs_ar_wildfires', fetch_wfigs_arkansas_wildfires),
    ('wfigs_la_wildfires', fetch_wfigs_louisiana_wildfires),
    ('wfigs_ok_wildfires', fetch_wfigs_oklahoma_wildfires),
    ('wfigs_tx_wildfires', fetch_wfigs_texas_wildfires),
    ('wfigs_nm_wildfires', fetch_wfigs_new_mexico_wildfires),
    ('wfigs_az_wildfires', fetch_wfigs_arizona_wildfires),
    ('wfigs_ca_wildfires', fetch_wfigs_california_wildfires),
    ('wfigs_nv_wildfires', fetch_wfigs_nevada_wildfires),
    ('wfigs_or_wildfires', fetch_wfigs_oregon_wildfires),
    ('wfigs_wa_wildfires', fetch_wfigs_washington_wildfires),
    ('wfigs_id_wildfires', fetch_wfigs_idaho_wildfires),
    ('wfigs_ut_wildfires', fetch_wfigs_utah_wildfires),
    ('wfigs_co_wildfires', fetch_wfigs_colorado_wildfires),
    ('wfigs_mi_wildfires', fetch_wfigs_michigan_wildfires),
    ('wfigs_wy_wildfires', fetch_wfigs_wyoming_wildfires),
    ('wfigs_mt_wildfires', fetch_wfigs_montana_wildfires),
    ('wfigs_nd_wildfires', fetch_wfigs_north_dakota_wildfires),
    ('wfigs_sd_wildfires', fetch_wfigs_south_dakota_wildfires),
    ('wfigs_ne_wildfires', fetch_wfigs_nebraska_wildfires),
    ('wfigs_ks_wildfires', fetch_wfigs_kansas_wildfires),
    ('wfigs_ak_wildfires', fetch_wfigs_alaska_wildfires),
    ('wfigs_hi_wildfires', fetch_wfigs_hawaii_wildfires),
    ('notify_nyc', fetch_notify_nyc_incidents),
    ('alertdc', fetch_alertdc_incidents),
    ('nws_alerts', fetch_nws_emergency_alerts),
    ('eccc_alerts', fetch_eccc_emergency_alerts),
)


def build_emergency_content():
    def load_source(name, fetcher):
        return name, list(fetcher())

    source_results = {}
    errors = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(SOURCE_FETCH_WORKERS, len(EMERGENCY_SOURCE_FETCHERS))
    ) as executor:
        future_map = {
            executor.submit(load_source, name, fetcher): name
            for name, fetcher in EMERGENCY_SOURCE_FETCHERS
        }
        for future in concurrent.futures.as_completed(future_map):
            name = future_map[future]
            try:
                _, source_results[name] = future.result()
            except Exception as exc:
                errors.append(f'{name}: {exc}')

    features_by_key = {}
    for name, _ in EMERGENCY_SOURCE_FETCHERS:
        for feature in source_results.get(name, []):
            key = (feature.get('properties') or {}).get('key')
            if key:
                features_by_key[key] = feature

    if not features_by_key and errors:
        raise ValueError('; '.join(errors))

    body = {
        'type': 'FeatureCollection',
        'features': list(features_by_key.values()),
        'source_counts': {
            name: len(source_results.get(name, []))
            for name, _ in EMERGENCY_SOURCE_FETCHERS
        },
        'source_errors': errors,
        'last_updated': time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime()),
    }
    return json.dumps(body).encode(), errors


def refresh_emergency_cache_sync():
    content, errors = build_emergency_content()
    with EMERGENCY_CACHE_LOCK:
        EMERGENCY_CACHE['body'] = content
        EMERGENCY_CACHE['expires_at'] = time.time() + EMERGENCY_CACHE_TTL
        EMERGENCY_CACHE['last_error'] = '; '.join(errors) if errors else None
    return content


def refresh_emergency_cache_async():
    with EMERGENCY_CACHE_LOCK:
        if EMERGENCY_CACHE['refreshing']:
            return False
        EMERGENCY_CACHE['refreshing'] = True

    def worker():
        try:
            refresh_emergency_cache_sync()
        except Exception as exc:
            with EMERGENCY_CACHE_LOCK:
                EMERGENCY_CACHE['last_error'] = str(exc)
        finally:
            with EMERGENCY_CACHE_LOCK:
                EMERGENCY_CACHE['refreshing'] = False

    threading.Thread(target=worker, name='emergency-cache-refresh', daemon=True).start()
    return True


def fetch_wec_wisconsin_outages(provider_key, provider_name, outages_url, source_url, customers_served):
    data = fetch_json_url(outages_url, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': source_url,
        'Accept': 'application/json',
    }, timeout=25)
    if not isinstance(data, list):
        raise ValueError(f'{provider_name} returned an invalid outage payload')
    features = []
    total_affected = 0
    latest_update = None
    for index, item in enumerate(data):
        lat = optional_float(item.get('Latitude'))
        lon = optional_float(item.get('Longitude'))
        slices = item.get('Slices') or []
        affected = sum(safe_int(value.get('AffectedCusts')) for value in slices)
        total_affected += affected
        updated_at = display_text(item.get('LastUpdated')) or None
        if updated_at and (not latest_update or updated_at > latest_update):
            latest_update = updated_at
        if lat is None or lon is None or affected <= 0 or not point_in_state('WI', lat, lon):
            continue
        area_names = []
        for value in slices:
            area = display_text(value.get('City') or value.get('County'))
            if area and area not in area_names:
                area_names.append(area)
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'key': f'power:{provider_key}:{wv511_numeric_id(f"{lat}:{lon}:{index}")}',
                'provider': provider_name,
                'provider_key': provider_key,
                'source_url': source_url,
                'kind': 'point',
                'area_name': ', '.join(area_names[:3]) or f'{provider_name} outage',
                'customers_affected': affected,
                'customers_served': customers_served,
                'outages': 1,
                'status': display_text(item.get('CrewStatus')) or None,
                'reason': display_text(item.get('Cause')) or None,
                'etr': display_text(item.get('ETR')) or None,
                'start_time': display_text(item.get('OffTime')) or None,
                'updated_at': updated_at,
            },
        })
    return features, {
        'provider': provider_name,
        'source_url': source_url,
        'total_outages': len(data),
        'total_customers_affected': total_affected,
        'total_customers_served': customers_served,
        'last_updated': latest_update,
        'mappable': bool(features),
    }


def fetch_we_energies_wisconsin_outages():
    return fetch_wec_wisconsin_outages(
        'we_energies_wi', 'We Energies', WE_ENERGIES_WI_OUTAGES_URL,
        WE_ENERGIES_WI_SOURCE_URL, 1_100_000,
    )


def fetch_wps_wisconsin_outages():
    return fetch_wec_wisconsin_outages(
        'wps_wi', 'Wisconsin Public Service', WPS_WI_OUTAGES_URL,
        WPS_WI_SOURCE_URL, 450_000,
    )


def fetch_mge_wisconsin_outages():
    payload = fetch_json_url(MGE_WI_OUTAGES_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': MGE_WI_SOURCE_URL,
        'Accept': 'application/json',
        'SourceType': '0',
    }, timeout=25)
    data = ((payload.get('result') or {}).get('Data') or {})
    records = data.get('listOutageResultSetTwo') or []
    totals = (data.get('listTotalOutage') or [{}])[0] or {}
    customers_served = safe_int(totals.get('TotalCusomerServed'))
    features = []
    total_affected = 0
    for item in records:
        lat = optional_float(item.get('OutageLatitude'))
        lon = optional_float(item.get('OutageLongitude'))
        affected = safe_int(item.get('CustomerAffected'))
        total_affected += affected
        if lat is None or lon is None or affected <= 0 or not point_in_state('WI', lat, lon):
            continue
        raw_id = str(item.get('Outageid') or wv511_numeric_id(f'{lat}:{lon}'))
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'key': f'power:mge_wi:{raw_id}',
                'provider': 'Madison Gas and Electric',
                'provider_key': 'mge_wi',
                'source_url': MGE_WI_SOURCE_URL,
                'kind': 'point',
                'area_name': f'ZIP {item.get("ZipCode")}' if item.get('ZipCode') else 'MGE outage',
                'customers_affected': affected,
                'customers_served': customers_served,
                'outages': 1,
                'status': display_text(item.get('CrewStatus') or item.get('OutageStatus')) or None,
                'reason': None,
                'etr': item.get('Restorationdate') or None,
                'start_time': item.get('Outagedate') or None,
                'updated_at': totals.get('LastUpdated') or None,
            },
        })
    return features, {
        'provider': 'Madison Gas and Electric',
        'source_url': MGE_WI_SOURCE_URL,
        'total_outages': len(records),
        'total_customers_affected': total_affected,
        'total_customers_served': customers_served,
        'last_updated': totals.get('LastUpdated'),
        'mappable': bool(features),
    }


def fetch_xcel_state_outages(state_code, provider_key, provider_name, source_url):
    params = urllib.parse.urlencode({
        'where': f"states='{state_code}'",
        'outFields': (
            'objectid,boundary,comments,customers,off_,etr,ticket,outageimpact,'
            'outagetype,outagestatus,states,crewstatus,county,cause,city,xcelarea'
        ),
        'returnGeometry': 'true',
        'outSR': '4326',
        'geometryPrecision': '5',
        'f': 'geojson',
    })
    payload = fetch_json_url(f'{XCEL_MN_OUTAGES_URL}?{params}', headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': source_url,
        'Accept': 'application/geo+json,application/json',
    }, timeout=30)
    records = payload.get('features') or []
    features = []
    total_affected = 0
    for item in records:
        props = item.get('properties') or {}
        affected = safe_int(props.get('customers'))
        total_affected += affected
        center = geojson_center(item.get('geometry'))
        if affected <= 0 or not center or not point_in_state(state_code, *center):
            continue
        raw_id = props.get('objectid') or props.get('ticket') or item.get('id')
        area = display_text(props.get('xcelarea') or props.get('city') or props.get('county'))
        features.append({
            'type': 'Feature',
            'geometry': item.get('geometry'),
            'properties': {
                'key': f'power:{provider_key}:{raw_id}',
                'provider': provider_name,
                'provider_key': provider_key,
                'source_url': source_url,
                'kind': 'point',
                'area_name': area or 'Xcel Energy outage',
                'customers_affected': affected,
                'customers_served': None,
                'outages': 1,
                'status': display_text(props.get('crewstatus') or props.get('outagetype')) or None,
                'reason': display_text(props.get('cause')) or None,
                'etr': epoch_milliseconds_iso(props.get('etr')),
                'start_time': epoch_milliseconds_iso(props.get('off_')),
                'updated_at': None,
                'details': display_text(props.get('comments')) or None,
            },
        })
    return features, {
        'provider': provider_name,
        'source_url': source_url,
        'total_outages': len(records),
        'total_customers_affected': total_affected,
        'total_customers_served': None,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_xcel_minnesota_outages():
    return fetch_xcel_state_outages(
        'MN', 'xcel_mn', 'Xcel Energy (Minnesota)', XCEL_MN_SOURCE_URL
    )


def fetch_xcel_colorado_outages():
    return fetch_xcel_state_outages(
        'CO', 'xcel_co', 'Xcel Energy (Colorado)', XCEL_CO_SOURCE_URL
    )


def fetch_minnesota_power_outages():
    params = urllib.parse.urlencode({
        'where': 'CUSTCOUNT > 0',
        'outFields': 'OBJECTID,ORDERID,STATUS,LASTUPDATE,ETR,CUSTCOUNT,LOCATION,DATEOFF,CAUSE',
        'returnGeometry': 'true',
        'outSR': '4326',
        'geometryPrecision': '5',
        'maxAllowableOffset': '0.0005',
        'f': 'geojson',
    })
    payload = fetch_json_url(f'{MINNESOTA_POWER_OUTAGES_URL}?{params}', headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': MINNESOTA_POWER_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
    }, timeout=30)
    records = payload.get('features') or []
    features = []
    total_affected = 0
    latest_update = None
    for item in records:
        props = item.get('properties') or {}
        affected = safe_int(props.get('CUSTCOUNT'))
        total_affected += affected
        updated_at = epoch_milliseconds_iso(props.get('LASTUPDATE'))
        if updated_at and (not latest_update or updated_at > latest_update):
            latest_update = updated_at
        center = geojson_center(item.get('geometry'))
        if affected <= 0 or not center or not point_in_state('MN', *center):
            continue
        raw_id = props.get('ORDERID') or props.get('OBJECTID') or item.get('id')
        features.append({
            'type': 'Feature',
            'geometry': item.get('geometry'),
            'properties': {
                'key': f'power:minnesota_power:{raw_id}',
                'provider': 'Minnesota Power',
                'provider_key': 'minnesota_power',
                'source_url': MINNESOTA_POWER_SOURCE_URL,
                'kind': 'polygon',
                'area_name': display_text(props.get('LOCATION')) or 'Minnesota Power outage',
                'customers_affected': affected,
                'customers_served': None,
                'outages': 1,
                'status': display_text(props.get('STATUS')) or None,
                'reason': display_text(props.get('CAUSE')) or None,
                'etr': epoch_milliseconds_iso(props.get('ETR')),
                'start_time': epoch_milliseconds_iso(props.get('DATEOFF')),
                'updated_at': updated_at,
            },
        })
    return features, {
        'provider': 'Minnesota Power',
        'source_url': MINNESOTA_POWER_SOURCE_URL,
        'total_outages': len(records),
        'total_customers_affected': total_affected,
        'total_customers_served': None,
        'last_updated': latest_update,
        'mappable': bool(features),
    }


def fetch_midamerican_iowa_outages():
    payload = fetch_json_url(MIDAMERICAN_IA_OUTAGES_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': MIDAMERICAN_IA_SOURCE_URL,
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
    }, data=b'', timeout=30)
    if not isinstance(payload, list):
        raise ValueError('MidAmerican Energy returned an invalid outage payload')
    features = []
    total_affected = 0
    for item in payload:
        lat = optional_float(item.get('Latitude'))
        lon = optional_float(item.get('Longitude'))
        affected = safe_int(item.get('Downstream'))
        if lat is None or lon is None or affected <= 0 or not point_in_state('IA', lat, lon):
            continue
        total_affected += affected
        raw_id = str(item.get('IncidentID') or wv511_numeric_id(f'{lat}:{lon}'))
        area = display_text(item.get('METRO_AREA') or item.get('District'))
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {
                'key': f'power:midamerican_ia:{raw_id}',
                'provider': 'MidAmerican Energy',
                'provider_key': 'midamerican_ia',
                'source_url': MIDAMERICAN_IA_SOURCE_URL,
                'kind': 'point',
                'area_name': area or 'MidAmerican Energy outage',
                'customers_affected': affected,
                'customers_served': None,
                'outages': 1,
                'status': display_text(item.get('FacJobStatusCd') or item.get('StatusCd')) or None,
                'reason': display_text(item.get('LocationCause')) or None,
                'etr': display_text(item.get('ETR')) or None,
                'start_time': display_text(item.get('CreateDatetime')) or None,
                'updated_at': None,
            },
        })
    return features, {
        'provider': 'MidAmerican Energy',
        'source_url': MIDAMERICAN_IA_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': total_affected,
        'total_customers_served': None,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_iowarec_outages():
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': IOWAREC_OUTAGE_SOURCE_URL,
        'Accept': 'application/json,text/html,*/*',
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
    }
    details = fetch_json_url(
        IOWAREC_OUTAGE_DETAILS_URL,
        headers=headers,
        data=b'',
        timeout=30,
    )
    county_html = str(details.get('DetailsByCountyAlpha') or '')
    county_records = []
    for match in re.finditer(
        r'<dl[^>]+id=["\']county(\d+)["\'][^>]*>.*?<dt>(.*?)</dt>.*?'
        r'<dd>\s*([\d,]+)\s+member-consumers?\s+without power\.(.*?)</dd>',
        county_html,
        re.I | re.S,
    ):
        affected = safe_int(match.group(3).replace(',', ''))
        if affected <= 0:
            continue
        providers = [
            strip_tags(value)
            for value in re.findall(r'<li>(.*?)</li>', match.group(4), re.I | re.S)
            if strip_tags(value)
        ]
        county_records.append({
            'id': match.group(1),
            'name': strip_tags(match.group(2)),
            'affected': affected,
            'details': ' · '.join(providers),
        })

    county_geojson = fetch_json_url(IOWAREC_OUTAGE_COUNTIES_URL, headers={
        'User-Agent': headers['User-Agent'],
        'Referer': IOWAREC_OUTAGE_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
    }, timeout=30)
    geometries = {
        str(feature.get('id')): feature.get('geometry')
        for feature in county_geojson.get('features') or []
    }
    features = []
    for item in county_records:
        geometry = geometries.get(item['id'])
        center = geojson_center(geometry)
        if not geometry or not center or not point_in_state('IA', *center):
            continue
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:iowarec:{item["id"]}',
                'provider': 'Iowa Association of Electric Cooperatives',
                'provider_key': 'iowarec',
                'source_url': IOWAREC_OUTAGE_SOURCE_URL,
                'kind': 'polygon',
                'area_name': item['name'] or 'Iowa cooperative outage',
                'customers_affected': item['affected'],
                'customers_served': None,
                'outages': 1,
                'status': item['details'] or None,
                'reason': None,
                'etr': None,
                'start_time': None,
                'updated_at': None,
            },
        })
    return features, {
        'provider': 'Iowa Association of Electric Cooperatives',
        'source_url': IOWAREC_OUTAGE_SOURCE_URL,
        'total_outages': len(county_records),
        'total_customers_affected': sum(item['affected'] for item in county_records),
        'total_customers_served': None,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_les_nebraska_outages():
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': '*',
        'returnGeometry': 'true',
        'outSR': 4326,
        'f': 'geojson',
    })
    data = fetch_json_url(f'{LES_NE_OUTAGES_URL}?{params}', headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': LES_NE_OUTAGES_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
    }, timeout=20)
    features = []
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        center = geojson_center(feature.get('geometry'))
        if not center or not point_in_state('NE', *center):
            continue
        affected = safe_int(props.get('CUSTOMER_COUNT'))
        if affected <= 0:
            continue
        outage_id = props.get('INCIDENT_ID') or props.get('OBJECTID') or len(features)
        features.append({
            'type': 'Feature',
            'geometry': feature.get('geometry'),
            'properties': {
                'key': f'power:les_ne:{outage_id}',
                'provider': 'Lincoln Electric System',
                'provider_key': 'les_ne',
                'source_url': LES_NE_OUTAGES_SOURCE_URL,
                'kind': 'point',
                'area_name': display_text(props.get('LES_AREA')) or 'Lincoln-area outage',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': 1,
                'status': display_text(props.get('STATUS')) or None,
                'reason': display_text(props.get('CAUSE')) or None,
                'etr': None,
                'start_time': props.get('TIME_OUTAGE'),
                'updated_at': None,
            },
        })
    return features, {
        'provider': 'Lincoln Electric System',
        'source_url': LES_NE_OUTAGES_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': sum(
            safe_int((feature.get('properties') or {}).get('customers_affected'))
            for feature in features
        ),
        'total_customers_served': None,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_otter_tail_power_outages():
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': OTTER_TAIL_OUTAGES_SOURCE_URL,
        'Accept': 'application/json',
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        summary_future = executor.submit(
            fetch_json_url,
            f'{OTTER_TAIL_OUTAGES_ROOT}/outageSummary.json',
            headers,
            None,
            20,
        )
        outages_future = executor.submit(
            fetch_json_url,
            f'{OTTER_TAIL_OUTAGES_ROOT}/outages.json',
            headers,
            None,
            20,
        )
        summary = summary_future.result()
        outages = outages_future.result()

    if not isinstance(summary, dict) or not isinstance(outages, list):
        raise ValueError('Unexpected Otter Tail Power outage response')
    features = []
    for item in outages:
        point = item.get('outagePoint') or {}
        lat = optional_float(point.get('lat'))
        lon = optional_float(point.get('lng'))
        if lat is None or lon is None:
            continue
        state_code = next(
            (code for code in ('MN', 'ND', 'SD') if point_in_state(code, lat, lon)),
            None,
        )
        if not state_code:
            continue
        affected = safe_int(item.get('customersOutNow'))
        if affected <= 0:
            continue
        outage_id = item.get('outageRecID') or item.get('outageRecId') or len(features)
        streets = item.get('streetsAffected') or []
        if isinstance(streets, str):
            streets = [streets]
        area_name = (
            display_text(item.get('outageName')) or
            display_text('; '.join(str(value) for value in streets[:3])) or
            f'{REGIONS[state_code]["name"]} service-area outage'
        )
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:otter_tail:{outage_id}',
                'provider': 'Otter Tail Power Company',
                'provider_key': 'otter_tail',
                'source_url': OTTER_TAIL_OUTAGES_SOURCE_URL,
                'kind': 'point',
                'area_name': area_name,
                'customers_affected': affected,
                'customers_served': safe_int(summary.get('customersServed')),
                'outages': 1,
                'status': display_text(item.get('outageWorkStatus')) or None,
                'reason': display_text(item.get('outageCause')) or None,
                'etr': item.get('estimatedRestorationTime') or item.get('outageEndTime'),
                'start_time': item.get('outageStartTime'),
                'updated_at': item.get('outageModifiedTime') or summary.get('updateTime'),
                'state': state_code,
            },
        })
    return features, {
        'provider': 'Otter Tail Power Company',
        'source_url': OTTER_TAIL_OUTAGES_SOURCE_URL,
        'total_outages': len(outages),
        'total_customers_affected': safe_int(summary.get('customersOutNow')),
        'total_customers_served': safe_int(summary.get('customersServed')),
        'last_updated': summary.get('updateTime'),
        'mappable': bool(features),
    }


def fetch_mdu_power_outages():
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': MDU_OUTAGES_SOURCE_URL,
        'Accept': 'application/json',
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fixed_future = executor.submit(
            fetch_json_url, f'{MDU_OUTAGES_ROOT}/fn-outages', headers, None, 20
        )
        reported_future = executor.submit(
            fetch_json_url, f'{MDU_OUTAGES_ROOT}/ivr-outages', headers, None, 20
        )
        fixed_document = fixed_future.result()
        reported_document = reported_future.result()

    fixed_rows = fixed_document.get('object') or []
    reported_rows = reported_document.get('object') or []
    if not isinstance(fixed_rows, list) or not isinstance(reported_rows, list):
        raise ValueError('Unexpected Montana-Dakota Utilities outage response')

    state_codes = ('MT', 'ND', 'SD', 'WY')
    features = []
    for item in fixed_rows:
        lat = optional_float(item.get('latitude'))
        lon = optional_float(item.get('longitude'))
        if lat is None or lon is None:
            continue
        state_code = next(
            (code for code in state_codes if point_in_state(code, lat, lon)),
            None,
        )
        affected = safe_int(item.get('outages'))
        if not state_code or affected <= 0:
            continue
        outage_id = item.get('id') or hashlib.md5(
            f'{lat:.5f}|{lon:.5f}|{affected}'.encode()
        ).hexdigest()[:16]
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:mdu:{outage_id}',
                'provider': 'Montana-Dakota Utilities',
                'provider_key': 'mdu',
                'source_url': MDU_OUTAGES_SOURCE_URL,
                'kind': 'point',
                'area_name': f'{REGIONS[state_code]["name"]} MDU outage',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': 1,
                'status': None,
                'reason': None,
                'etr': None,
                'start_time': item.get('outageDateTime'),
                'updated_at': None,
                'state': state_code,
            },
        })

    geocode_records = []
    for item in reported_rows:
        state_code = display_text(item.get('state')).upper()
        city = display_text(item.get('city'))
        if state_code not in state_codes or not city:
            continue
        geocode_records.append({
            'item': item,
            'state': state_code,
            'query': f'{city}, {REGIONS[state_code]["name"]}',
        })
    geocodes = parallel_geocode_queries(record['query'] for record in geocode_records)
    for record in geocode_records:
        item = record['item']
        center = geocodes.get(normalized_cache_key(record['query']))
        if not center or not point_in_state(record['state'], *center):
            continue
        lat, lon = center
        outage_id = item.get('outageEventId') or hashlib.md5(
            json.dumps(item, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:mdu:{outage_id}',
                'provider': 'Montana-Dakota Utilities',
                'provider_key': 'mdu',
                'source_url': MDU_OUTAGES_SOURCE_URL,
                'kind': 'point',
                'area_name': ' · '.join(filter(None, (
                    display_text(item.get('city')),
                    display_text(item.get('cityLocDetail')),
                ))),
                'customers_affected': safe_int(item.get('customersAffected')),
                'customers_served': 0,
                'outages': 1,
                'status': display_text(item.get('status')) or None,
                'reason': display_text(item.get('reason')) or None,
                'etr': item.get('estRepairTime'),
                'start_time': item.get('outageDateTime'),
                'updated_at': None,
                'state': record['state'],
            },
        })

    return features, {
        'provider': 'Montana-Dakota Utilities',
        'source_url': MDU_OUTAGES_SOURCE_URL,
        'total_outages': len(fixed_rows) + len(reported_rows),
        'total_customers_affected': sum(
            safe_int((feature.get('properties') or {}).get('customers_affected'))
            for feature in features
        ),
        'total_customers_served': None,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_mdem_power_outages():
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': 'COUNTY,area,outages,customers,percent_out,dt_stamp,ObjectId',
        'returnGeometry': 'true',
        'outSR': '4326',
        'geometryPrecision': '5',
        'maxAllowableOffset': '0.002',
        'f': 'geojson',
    })
    data = fetch_json_url(f'{MDEM_POWER_OUTAGES_URL}?{params}', headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': MDEM_POWER_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
    }, timeout=30)
    features = []
    total_affected = 0
    total_served = 0
    latest_update = None
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        affected = safe_int(props.get('outages'))
        served = safe_int(props.get('customers'))
        total_affected += affected
        total_served += served
        timestamp = epoch_milliseconds_iso(props.get('dt_stamp'))
        if timestamp and (not latest_update or timestamp > latest_update):
            latest_update = timestamp
        if affected <= 0 or not feature.get('geometry'):
            continue
        county = display_text(props.get('COUNTY') or props.get('area') or 'Maryland county')
        features.append({
            'type': 'Feature',
            'geometry': feature['geometry'],
            'properties': {
                'key': f'power:mdem:{props.get("ObjectId") or county}',
                'provider': 'Maryland Utilities',
                'provider_key': 'mdem',
                'source_url': MDEM_POWER_SOURCE_URL,
                'kind': 'area',
                'area_name': f'{county} County' if county.lower() != 'baltimore city' else county,
                'customers_affected': affected,
                'customers_served': served,
                'outages': 0,
                'percent_customers_affected': safe_float(props.get('percent_out')),
                'etr': None,
                'start_time': None,
                'updated_at': timestamp,
            },
        })
    return features, {
        'provider': 'Maryland Utilities',
        'source_url': MDEM_POWER_SOURCE_URL,
        'total_outages': 0,
        'total_customers_affected': total_affected,
        'total_customers_served': total_served,
        'last_updated': latest_update,
        'mappable': True,
    }


def fetch_pema_pennsylvania_power_outages():
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': (
            'OBJECTID,COUNTY_NAME,COUNTY_OUT,COUNTY_SERVED,OUTAGE_PERCENT,'
            'OUTAGE_LEVEL,OUTAGE_DATE,LAST_UPDATED'
        ),
        'returnGeometry': 'true',
        'outSR': '4326',
        'geometryPrecision': '5',
        'maxAllowableOffset': '0.002',
        'f': 'geojson',
    })
    data = fetch_json_url(f'{PEMA_PA_POWER_OUTAGES_URL}?{params}', headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': PEMA_PA_POWER_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
    }, timeout=30)
    features = []
    total_affected = 0
    total_served = 0
    latest_update = None
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        affected = safe_int(props.get('COUNTY_OUT'))
        served = safe_int(props.get('COUNTY_SERVED'))
        total_affected += affected
        total_served += served
        timestamp = epoch_milliseconds_iso(props.get('LAST_UPDATED') or props.get('OUTAGE_DATE'))
        if timestamp and (not latest_update or timestamp > latest_update):
            latest_update = timestamp
        if affected <= 0 or not feature.get('geometry'):
            continue
        county = display_text(props.get('COUNTY_NAME') or 'Pennsylvania county')
        features.append({
            'type': 'Feature',
            'geometry': feature['geometry'],
            'properties': {
                'key': f'power:pema_pa:{props.get("OBJECTID") or county}',
                'provider': 'Pennsylvania Utilities',
                'provider_key': 'pema_pa',
                'source_url': PEMA_PA_POWER_SOURCE_URL,
                'kind': 'area',
                'area_name': f'{county} County',
                'customers_affected': affected,
                'customers_served': served,
                'outages': 0,
                'percent_customers_affected': safe_float(props.get('OUTAGE_PERCENT')),
                'status': display_text(props.get('OUTAGE_LEVEL')) or None,
                'etr': None,
                'start_time': None,
                'updated_at': timestamp,
            },
        })
    return features, {
        'provider': 'Pennsylvania Utilities',
        'source_url': PEMA_PA_POWER_SOURCE_URL,
        'total_outages': 0,
        'total_customers_affected': total_affected,
        'total_customers_served': total_served,
        'last_updated': latest_update,
        'mappable': True,
    }


def fetch_pepco_dc_power_outages():
    view_root = (
        f'{PEPCO_DC_API_BASE}/stormcenter/api/v1/stormcenters/'
        f'{PEPCO_DC_INSTANCE_ID}/views/{PEPCO_DC_VIEW_ID}'
    )
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': PEPCO_DC_SOURCE_URL,
        'Accept': 'application/json',
    }
    state = fetch_json_url(f'{view_root}/currentState', headers=headers, timeout=30)
    data_root = (state.get('data') or {}).get('interval_generation_data')
    if not data_root:
        raise ValueError('Pepco outage feed returned no current data root')
    thematic = fetch_json_url(
        f'{PEPCO_DC_API_BASE}/{data_root}/public/thematic-4/thematic_areas.json',
        headers=headers,
        timeout=30,
    )
    summary_doc = fetch_json_url(
        f'{PEPCO_DC_API_BASE}/{data_root}/public/summary-1/data.json',
        headers=headers,
        timeout=30,
    )
    updated_at = (
        (summary_doc.get('summaryFileData') or {}).get('date_generated') or
        epoch_milliseconds_iso(state.get('updatedAt'))
    )
    dc_item = next(
        (
            item for item in thematic.get('file_data') or []
            if str(item.get('title') or '').upper() == 'DC' or
            str(item.get('id') or '').upper().endswith('|DC|DISTRICT')
        ),
        None,
    )
    if not dc_item:
        raise ValueError('Pepco outage feed returned no D.C. district')
    desc = dc_item.get('desc') or {}
    affected = safe_int((desc.get('cust_a') or {}).get('val'))
    served = safe_int(desc.get('cust_s'))
    outages = safe_int(desc.get('n_out'))
    geometry = kubra_geometry(dc_item.get('geom') or {})
    center = kubra_center(dc_item.get('geom') or {})
    if not geometry and center:
        geometry = point_geometry(center[0], center[1])
    features = []
    if affected > 0 and geometry:
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': 'power:pepco_dc:district',
                'provider': 'Pepco',
                'provider_key': 'pepco_dc',
                'source_url': PEPCO_DC_SOURCE_URL,
                'kind': 'area',
                'area_name': 'District of Columbia',
                'customers_affected': affected,
                'customers_served': served,
                'outages': outages,
                'percent_customers_affected': safe_float((desc.get('percent_cust_a') or {}).get('val')),
                'etr': desc.get('etr') if not str(desc.get('etr') or '').startswith('ETR-') else None,
                'start_time': desc.get('start_time'),
                'updated_at': updated_at,
            },
        })
    return features, {
        'provider': 'Pepco',
        'source_url': PEPCO_DC_SOURCE_URL,
        'total_outages': outages,
        'total_customers_affected': affected,
        'total_customers_served': served,
        'last_updated': updated_at,
        'mappable': bool(geometry),
    }


def fetch_delmarva_delaware_power_outages():
    view_root = (
        f'{DELMARVA_API_BASE}/stormcenter/api/v1/stormcenters/'
        f'{DELMARVA_INSTANCE_ID}/views/{DELMARVA_VIEW_ID}'
    )
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': DELMARVA_SOURCE_URL,
        'Accept': 'application/json',
    }
    state = fetch_json_url(f'{view_root}/currentState', headers=headers, timeout=30)
    data_root = (state.get('data') or {}).get('interval_generation_data')
    if not data_root:
        raise ValueError('Delmarva outage feed returned no current data root')
    thematic = fetch_json_url(
        f'{DELMARVA_API_BASE}/{data_root}/public/thematic-6/thematic_areas.json',
        headers=headers,
        timeout=30,
    )
    summary_doc = fetch_json_url(
        f'{DELMARVA_API_BASE}/{data_root}/public/summary-1/data.json',
        headers=headers,
        timeout=30,
    )
    updated_at = (
        (summary_doc.get('summaryFileData') or {}).get('date_generated') or
        epoch_milliseconds_iso(state.get('updatedAt'))
    )
    features = []
    total_affected = 0
    total_outages = 0
    total_served_in_active_areas = 0
    for item in thematic.get('file_data') or []:
        desc = item.get('desc') or {}
        hierarchy = desc.get('hierarchy') or {}
        if str(hierarchy.get('state') or '').upper() != 'DE':
            continue
        affected = safe_int((desc.get('cust_a') or {}).get('val'))
        served = safe_int(desc.get('cust_s'))
        outages = safe_int(desc.get('n_out'))
        total_affected += affected
        total_outages += outages
        total_served_in_active_areas += served
        geometry = kubra_geometry(item.get('geom') or {})
        center = kubra_center(item.get('geom') or {})
        if not geometry and center:
            geometry = point_geometry(center[0], center[1])
        if affected <= 0 or not geometry:
            continue
        area_name = display_text(desc.get('name') or item.get('title') or 'Delaware')
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:delmarva_de:{item.get("id") or area_name}',
                'provider': 'Delmarva Power',
                'provider_key': 'delmarva_de',
                'source_url': DELMARVA_SOURCE_URL,
                'kind': 'area',
                'area_name': f'{area_name} County',
                'customers_affected': affected,
                'customers_served': served,
                'outages': outages,
                'percent_customers_affected': safe_float((desc.get('percent_cust_a') or {}).get('val')),
                'etr': desc.get('etr') if not str(desc.get('etr') or '').startswith('ETR-') else None,
                'start_time': desc.get('start_time'),
                'updated_at': updated_at,
            },
        })
    return features, {
        'provider': 'Delmarva Power (Delaware)',
        'source_url': DELMARVA_SOURCE_URL,
        'total_outages': total_outages,
        'total_customers_affected': total_affected,
        'total_customers_served': total_served_in_active_areas,
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def fetch_kubra_power_outages(provider_key, provider):
    state, config = kubra_provider_state(provider)
    data_root = state.get('data', {}).get('interval_generation_data')
    interval_layers = ((config.get('layers') or {}).get('data') or {}).get('interval_generation_data') or []
    summary_config = (((config.get('summary') or {}).get('data') or {}).get('interval_generation_data') or {})
    summary_source = summary_config.get('source')
    thematic_sources = []
    for layer in interval_layers:
        if layer.get('type') == 'THEMATIC_LAYER_V2':
            source = layer.get('source') or []
            if source:
                thematic_sources.append(source[0])
    preferred_thematic_source = str(provider.get('thematic_source') or '').strip()
    if preferred_thematic_source:
        thematic_sources = [preferred_thematic_source] + [
            source for source in thematic_sources if source != preferred_thematic_source
        ]

    provider_summary = {
        'provider': provider['name'],
        'source_url': provider['source_url'],
        'total_outages': 0,
        'total_customers_affected': 0,
        'total_customers_served': 0,
        'last_updated': None,
        'mappable': False,
    }

    if summary_source and data_root:
        summary_doc = fetch_json_url(f'https://kubra.io/{data_root}/{summary_source}')
        provider_summary = kubra_provider_summary(provider['name'], provider['source_url'], summary_doc)

    if not (data_root and thematic_sources):
        return [], provider_summary

    thematic_doc = None
    for thematic_source in thematic_sources:
        try:
            thematic_doc = fetch_json_url(f'https://kubra.io/{data_root}/{thematic_source}')
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise

    if not thematic_doc:
        return [], provider_summary

    features = []
    filter_state_code = str(provider.get('state_code') or '').upper()
    for item in thematic_doc.get('file_data') or []:
        desc = item.get('desc') or {}
        customers_affected = safe_int((desc.get('cust_a') or {}).get('val'))
        if customers_affected <= 0:
            continue
        geometry = kubra_geometry(item.get('geom') or {})
        center = kubra_center(item.get('geom') or {})
        if not geometry and center:
            geometry = point_geometry(center[0], center[1])
        if not geometry:
            continue
        if filter_state_code:
            feature_center = center or geojson_center(geometry)
            if not feature_center or not point_in_state(
                filter_state_code, feature_center[0], feature_center[1]
            ):
                continue

        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:{provider_key}:{item.get("id") or item.get("title")}',
                'provider': provider['name'],
                'provider_key': provider_key,
                'source_url': provider['source_url'],
                'kind': 'area',
                'area_name': desc.get('name') or item.get('title') or provider['name'],
                'customers_affected': customers_affected,
                'customers_served': safe_int(desc.get('cust_s')),
                'outages': safe_int(desc.get('n_out')),
                'percent_customers_affected': safe_float((desc.get('percent_cust_a') or {}).get('val')),
                'etr': desc.get('etr') if not str(desc.get('etr') or '').startswith('ETR-') else None,
                'start_time': desc.get('start_time'),
                'updated_at': provider_summary.get('last_updated'),
            }
        })

    if filter_state_code:
        provider_summary.update({
            'total_outages': sum(
                safe_int((feature.get('properties') or {}).get('outages')) for feature in features
            ),
            'total_customers_affected': sum(
                safe_int((feature.get('properties') or {}).get('customers_affected'))
                for feature in features
            ),
            'total_customers_served': sum(
                safe_int((feature.get('properties') or {}).get('customers_served'))
                for feature in features
            ),
        })
    provider_summary['mappable'] = bool(features)
    return features, provider_summary


def fetch_aps_arizona_outages():
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': '*',
        'returnGeometry': 'true',
        'outSR': '4326',
        'f': 'geojson',
    })
    source_urls = (
        APS_AZ_OUTAGES_URL,
        APS_AZ_OUTAGES_URL.replace('/0/query', '/5/query'),
        APS_AZ_OUTAGES_URL.replace('/0/query', '/8/query'),
    )
    features = []
    for source_url in source_urls:
        data = fetch_json_url(
            f'{source_url}?{params}',
            headers={
                'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
                'Referer': APS_AZ_OUTAGES_SOURCE_URL,
                'Accept': 'application/geo+json,application/json',
            },
            timeout=20,
        )
        for feature in data.get('features') or []:
            props = feature.get('properties') or {}
            coordinates = (feature.get('geometry') or {}).get('coordinates') or []
            if len(coordinates) < 2:
                continue
            lon = optional_float(coordinates[0])
            lat = optional_float(coordinates[1])
            customers_affected = safe_int(props.get('customers'))
            source_id = display_text(props.get('Ticket') or props.get('OBJECTID'))
            if (
                lat is None or lon is None or not source_id or customers_affected <= 0 or
                not point_in_state('AZ', lat, lon)
            ):
                continue
            city = display_text(props.get('City'))
            boundary = display_text(props.get('Boundary'))
            area_name = ' · '.join(part for part in (city, boundary) if part) or 'APS service area'
            features.append({
                'type': 'Feature',
                'geometry': point_geometry(lat, lon),
                'properties': {
                    'key': f'power:aps_az:{source_id}',
                    'provider': 'Arizona Public Service',
                    'provider_key': 'aps_az',
                    'source_url': APS_AZ_OUTAGES_SOURCE_URL,
                    'kind': 'point',
                    'area_name': area_name,
                    'customers_affected': customers_affected,
                    'customers_served': 0,
                    'outages': 1,
                    'percent_customers_affected': None,
                    'status': display_text(props.get('outagetype')),
                    'reason': display_text(props.get('Cause') or props.get('Comments')),
                    'etr': epoch_milliseconds_iso(props.get('etr')),
                    'start_time': epoch_milliseconds_iso(props.get('off')),
                    'updated_at': None,
                },
            })
    total_affected = sum(
        safe_int((feature.get('properties') or {}).get('customers_affected'))
        for feature in features
    )
    return features, {
        'provider': 'Arizona Public Service',
        'source_url': APS_AZ_OUTAGES_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': total_affected,
        'total_customers_served': 0,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_pge_california_outages():
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': (
            'OBJECTID,OUTAGE_ID,OUTAGE_START,CURRENT_ETOR,EST_CUSTOMERS,'
            'OUTAGE_CAUSE,CREW_CURRENT_STATUS,LAST_UPDATE'
        ),
        'returnGeometry': 'true',
        'outSR': '4326',
        'geometryPrecision': '5',
        'maxAllowableOffset': '0.0005',
        'f': 'geojson',
    })
    data = fetch_json_url(
        f'{PGE_CA_OUTAGES_URL}?{params}',
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': PGE_CA_OUTAGES_SOURCE_URL,
            'Accept': 'application/geo+json,application/json',
        },
        timeout=30,
    )
    features = []
    latest_update = None
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        geometry = feature.get('geometry')
        center = geojson_center(geometry)
        source_id = display_text(props.get('OUTAGE_ID') or props.get('OBJECTID'))
        affected = safe_int(props.get('EST_CUSTOMERS'))
        if (
            not geometry or not center or not source_id or affected <= 0 or
            not point_in_state('CA', center[0], center[1])
        ):
            continue
        updated_at = epoch_milliseconds_iso(props.get('LAST_UPDATE'))
        if updated_at and (not latest_update or updated_at > latest_update):
            latest_update = updated_at
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:pge_ca:{source_id}',
                'provider': 'Pacific Gas and Electric',
                'provider_key': 'pge_ca',
                'source_url': PGE_CA_OUTAGES_SOURCE_URL,
                'kind': 'area',
                'area_name': f'PG&E outage {source_id}',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': 1,
                'percent_customers_affected': None,
                'status': display_text(props.get('CREW_CURRENT_STATUS')) or None,
                'reason': display_text(props.get('OUTAGE_CAUSE')) or None,
                'etr': epoch_milliseconds_iso(props.get('CURRENT_ETOR')),
                'start_time': epoch_milliseconds_iso(props.get('OUTAGE_START')),
                'updated_at': updated_at,
            },
        })
    total_affected = sum(
        safe_int((feature.get('properties') or {}).get('customers_affected'))
        for feature in features
    )
    return features, {
        'provider': 'Pacific Gas and Electric',
        'source_url': PGE_CA_OUTAGES_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': total_affected,
        'total_customers_served': 0,
        'last_updated': latest_update,
        'mappable': bool(features),
    }


def fetch_nvenergy_nevada_outages():
    query = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': '*',
        'returnGeometry': 'true',
        'outSR': '4326',
        'f': 'geojson',
    })
    features = []
    for outage_kind, endpoint in NVENERGY_OUTAGE_LAYERS:
        data = fetch_json_url(
            f'{endpoint}?{query}',
            headers={
                'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
                'Referer': 'https://maps.nvenergy.com/outagemap/index.html',
                'Accept': 'application/geo+json,application/json',
            },
            timeout=30,
        )
        for feature in data.get('features') or []:
            props = feature.get('properties') or {}
            coordinates = (feature.get('geometry') or {}).get('coordinates') or []
            if len(coordinates) < 2:
                continue
            lon = optional_float(coordinates[0])
            lat = optional_float(coordinates[1])
            source_id = display_text(
                props.get('EVENTID') or props.get('SOID') or props.get('ESRI_OID')
            )
            affected = safe_int(
                props.get('NUMCUSTDEENERGIZED') or props.get('CUST_COUNT')
            )
            if (
                lat is None or lon is None or not source_id or affected <= 0 or
                not point_in_state('NV', lat, lon)
            ):
                continue
            planned = outage_kind == 'planned'
            features.append({
                'type': 'Feature',
                'geometry': point_geometry(lat, lon),
                'properties': {
                    'key': f'power:nvenergy:{outage_kind}:{source_id}',
                    'provider': 'NV Energy',
                    'provider_key': 'nvenergy',
                    'source_url': NVENERGY_OUTAGES_SOURCE_URL,
                    'kind': 'point',
                    'area_name': f'NV Energy {outage_kind} outage {source_id}',
                    'customers_affected': affected,
                    'customers_served': 0,
                    'outages': 1,
                    'percent_customers_affected': None,
                    'status': 'Planned outage' if planned else 'Unplanned outage',
                    'reason': display_text(
                        props.get('OUTAGE_TYPE') if planned else props.get('CAUSEDESC')
                    ),
                    'etr': display_text(
                        props.get('PLANNED_END') if planned else props.get('ERTDATETIME')
                    ),
                    'start_time': display_text(
                        props.get('PLANNED_START') if planned else props.get('STARTDATETIME')
                    ),
                    'updated_at': None,
                },
            })
    total_affected = sum(
        safe_int((feature.get('properties') or {}).get('customers_affected'))
        for feature in features
    )
    return features, {
        'provider': 'NV Energy',
        'source_url': NVENERGY_OUTAGES_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': total_affected,
        'total_customers_served': 0,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_pacific_power_oregon_outages():
    data = fetch_json_url(
        PACIFIC_POWER_OR_OUTAGES_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': PACIFIC_POWER_OR_SOURCE_URL,
            'Accept': 'application/json,text/plain,*/*',
        },
        timeout=20,
    )
    features = []
    updated_at = display_text(data.get('last_upd')) or None
    for index, outage in enumerate(data.get('outages') or []):
        lat = optional_float(outage.get('latitude'))
        lon = optional_float(outage.get('longitude'))
        affected = safe_int(outage.get('custOut'))
        if (
            lat is None or lon is None or affected <= 0 or
            not point_in_state('OR', lat, lon)
        ):
            continue
        zip_codes = display_text(outage.get('zip'))
        reported = display_text(outage.get('reported'))
        source_id = f'{lat:.5f}:{lon:.5f}:{reported or index}'
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:pacific_power_or:{source_id}',
                'provider': 'Pacific Power',
                'provider_key': 'pacific_power_or',
                'source_url': PACIFIC_POWER_OR_SOURCE_URL,
                'kind': 'point',
                'area_name': f'ZIP {zip_codes}' if zip_codes else 'Pacific Power service area',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': max(1, safe_int(outage.get('outCount'))),
                'percent_customers_affected': None,
                'status': display_text(outage.get('crewStatus')) or None,
                'reason': display_text(outage.get('cause')) or None,
                'etr': display_text(outage.get('etr')) or None,
                'start_time': reported or None,
                'updated_at': updated_at,
            },
        })
    total_affected = sum(
        safe_int((feature.get('properties') or {}).get('customers_affected'))
        for feature in features
    )
    return features, {
        'provider': 'Pacific Power (Oregon)',
        'source_url': PACIFIC_POWER_OR_SOURCE_URL,
        'total_outages': safe_int(data.get('count'), default=len(features)),
        'total_customers_affected': safe_int(data.get('totalState'), default=total_affected),
        'total_customers_served': 0,
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def fetch_pacific_power_washington_outages():
    data = fetch_json_url(
        PACIFIC_POWER_WA_OUTAGES_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': PACIFIC_POWER_OR_SOURCE_URL,
            'Accept': 'application/json,text/plain,*/*',
        },
        timeout=20,
    )
    features = []
    updated_at = display_text(data.get('last_upd')) or None
    for index, outage in enumerate(data.get('outages') or []):
        lat = optional_float(outage.get('latitude'))
        lon = optional_float(outage.get('longitude'))
        affected = safe_int(outage.get('custOut'))
        if (
            lat is None or lon is None or affected <= 0 or
            not point_in_state('WA', lat, lon)
        ):
            continue
        zip_codes = display_text(outage.get('zip'))
        reported = display_text(outage.get('reported'))
        source_id = f'{lat:.5f}:{lon:.5f}:{reported or index}'
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:pacific_power_wa:{source_id}',
                'provider': 'Pacific Power',
                'provider_key': 'pacific_power_wa',
                'source_url': PACIFIC_POWER_OR_SOURCE_URL,
                'kind': 'point',
                'area_name': f'ZIP {zip_codes}' if zip_codes else 'Pacific Power service area',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': max(1, safe_int(outage.get('outCount'))),
                'percent_customers_affected': None,
                'status': display_text(outage.get('crewStatus')) or None,
                'reason': display_text(outage.get('cause')) or None,
                'etr': display_text(outage.get('etr')) or None,
                'start_time': reported or None,
                'updated_at': updated_at,
            },
        })
    total_affected = sum(
        safe_int((feature.get('properties') or {}).get('customers_affected'))
        for feature in features
    )
    return features, {
        'provider': 'Pacific Power (Washington)',
        'source_url': PACIFIC_POWER_OR_SOURCE_URL,
        'total_outages': safe_int(data.get('count'), default=len(features)),
        'total_customers_affected': safe_int(data.get('totalState'), default=total_affected),
        'total_customers_served': 0,
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def fetch_pacificorp_state_outages(
    state_code, provider_key, provider_name, outages_url, source_url
):
    data = fetch_json_url(
        outages_url,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': source_url,
            'Accept': 'application/json,text/plain,*/*',
        },
        timeout=20,
    )
    features = []
    updated_at = display_text(data.get('last_upd')) or None
    for index, outage in enumerate(data.get('outages') or []):
        lat = optional_float(outage.get('latitude'))
        lon = optional_float(outage.get('longitude'))
        affected = safe_int(outage.get('custOut'))
        if (
            lat is None or lon is None or affected <= 0 or
            not point_in_state(state_code, lat, lon)
        ):
            continue
        zip_codes = display_text(outage.get('zip'))
        reported = display_text(outage.get('reported'))
        source_id = f'{lat:.5f}:{lon:.5f}:{reported or index}'
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:{provider_key}:{source_id}',
                'provider': provider_name,
                'provider_key': provider_key,
                'source_url': source_url,
                'kind': 'point',
                'area_name': f'ZIP {zip_codes}' if zip_codes else f'{provider_name} service area',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': max(1, safe_int(outage.get('outCount'))),
                'percent_customers_affected': None,
                'status': display_text(outage.get('crewStatus')) or None,
                'reason': display_text(outage.get('cause')) or None,
                'etr': display_text(outage.get('etr')) or None,
                'start_time': reported or None,
                'updated_at': updated_at,
            },
        })
    total_affected = sum(
        safe_int((feature.get('properties') or {}).get('customers_affected'))
        for feature in features
    )
    return features, {
        'provider': provider_name,
        'source_url': source_url,
        'total_outages': safe_int(data.get('count'), default=len(features)),
        'total_customers_affected': safe_int(data.get('totalState'), default=total_affected),
        'total_customers_served': 0,
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def fetch_northwestern_state_outages(state_code, provider_key, provider_name, default_area):
    response = fetch_json_url(
        NORTHWESTERN_MT_OUTAGES_URL,
        headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': NORTHWESTERN_MT_SOURCE_URL,
            'Accept': 'application/json,text/javascript,*/*',
        },
        timeout=20,
    )
    payload = response.get('d') if isinstance(response, dict) else response
    outages = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(outages, list):
        raise ValueError('NorthWestern Energy returned no outage array')
    features = []
    for index, outage in enumerate(outages):
        lat = optional_float(outage.get('YCOORD'))
        lon = optional_float(outage.get('XCOORD'))
        affected = safe_int(outage.get('NUM_CUST'))
        if lat is None or lon is None or affected <= 0 or not point_in_state(state_code, lat, lon):
            continue
        event_id = display_text(outage.get('EVENTID') or outage.get('EVENTNUM') or index)
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:{provider_key}:{event_id}',
                'provider': provider_name,
                'provider_key': provider_key,
                'source_url': NORTHWESTERN_MT_SOURCE_URL,
                'kind': 'point',
                'area_name': display_text(outage.get('DISPATCHGROUP') or outage.get('SUBSTATION')) or default_area,
                'customers_affected': affected,
                'customers_served': 0,
                'outages': 1,
                'percent_customers_affected': None,
                'status': display_text(outage.get('EVENT_STATUS')) or None,
                'reason': display_text(outage.get('CAUSE_CODE') or outage.get('EVENT_TYPE')) or None,
                'etr': display_text(outage.get('LOCAL_ERT') or outage.get('EST_REP_TIME')) or None,
                'start_time': display_text(outage.get('LOCAL_OFF_DTS') or outage.get('OFF_DTS')) or None,
                'updated_at': None,
            },
        })
    return features, {
        'provider': provider_name,
        'source_url': NORTHWESTERN_MT_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': sum(
            safe_int((feature.get('properties') or {}).get('customers_affected'))
            for feature in features
        ),
        'total_customers_served': 0,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_northwestern_montana_outages():
    return fetch_northwestern_state_outages(
        'MT', 'northwestern_mt', 'NorthWestern Energy (Montana)', 'Montana service area'
    )


def fetch_northwestern_south_dakota_outages():
    return fetch_northwestern_state_outages(
        'SD', 'northwestern_sd', 'NorthWestern Energy (South Dakota)', 'South Dakota service area'
    )


def fetch_rocky_mountain_power_idaho_outages():
    return fetch_pacificorp_state_outages(
        'ID',
        'rocky_mountain_power_id',
        'Rocky Mountain Power (Idaho)',
        ROCKY_MOUNTAIN_POWER_ID_OUTAGES_URL,
        ROCKY_MOUNTAIN_POWER_SOURCE_URL,
    )


def fetch_rocky_mountain_power_utah_outages():
    return fetch_pacificorp_state_outages(
        'UT',
        'rocky_mountain_power_ut',
        'Rocky Mountain Power (Utah)',
        ROCKY_MOUNTAIN_POWER_UT_OUTAGES_URL,
        ROCKY_MOUNTAIN_POWER_SOURCE_URL,
    )


def fetch_rocky_mountain_power_wyoming_outages():
    return fetch_pacificorp_state_outages(
        'WY',
        'rocky_mountain_power_wy',
        'Rocky Mountain Power (Wyoming)',
        ROCKY_MOUNTAIN_POWER_WY_OUTAGES_URL,
        ROCKY_MOUNTAIN_POWER_SOURCE_URL,
    )


def fetch_ifactor_county_power_outages(
    provider_key, provider_name, data_root, source_url, thematic_path, allowed_titles=None
):
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': source_url,
        'Accept': 'application/json',
    }
    metadata = fetch_json_url(f'{data_root}/metadata.json', headers=headers, timeout=20)
    directory = str(metadata.get('directory') or '').strip()
    if not re.fullmatch(r'[0-9_]{10,40}', directory):
        raise ValueError(f'{provider_name} returned an invalid outage-data directory')
    current_root = f'{data_root}/{directory}'
    overview = fetch_json_url(f'{current_root}/data.json', headers=headers, timeout=20)
    thematic = fetch_json_url(
        f'{current_root}/{thematic_path}/thematic_areas.json', headers=headers, timeout=20
    )
    overview_data = overview.get('summaryFileData') or {}
    features = []
    included_items = []
    allowed = {str(title).upper() for title in (allowed_titles or [])}
    for item in thematic.get('file_data') or []:
        title = display_text(item.get('title') or item.get('id') or provider_name)
        if allowed and title.upper() not in allowed:
            continue
        desc = item.get('desc') or {}
        included_items.append(desc)
        affected = safe_int((desc.get('cust_a') or {}).get('val'))
        if affected <= 0:
            continue
        geometry = kubra_geometry(item.get('geom') or {})
        center = kubra_center(item.get('geom') or {})
        if not geometry and center:
            geometry = point_geometry(center[0], center[1])
        if not geometry:
            continue
        served = safe_int(desc.get('cust_s'))
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:{provider_key}:{item.get("id") or title}',
                'provider': provider_name,
                'provider_key': provider_key,
                'source_url': source_url,
                'kind': 'area',
                'area_name': title.title(),
                'customers_affected': affected,
                'customers_served': served,
                'outages': safe_int(desc.get('n_out')),
                'percent_customers_affected': (
                    round(affected * 100 / served, 3) if served > 0 else 0
                ),
                'etr': desc.get('etr') if not str(desc.get('etr') or '').startswith('ETR-') else None,
                'start_time': desc.get('start'),
                'updated_at': overview_data.get('date_generated'),
            },
        })
    if allowed:
        total_affected = sum(safe_int((item.get('cust_a') or {}).get('val')) for item in included_items)
        total_served = sum(safe_int(item.get('cust_s')) for item in included_items)
        total_outages = sum(safe_int(item.get('n_out')) for item in included_items)
    else:
        total_affected = safe_int((overview_data.get('total_cust_a') or {}).get('val'))
        total_served = safe_int(overview_data.get('total_cust_s'))
        total_outages = safe_int(overview_data.get('total_outages'))
    return features, {
        'provider': provider_name,
        'source_url': source_url,
        'total_outages': total_outages,
        'total_customers_affected': total_affected,
        'total_customers_served': total_served,
        'last_updated': overview_data.get('date_generated'),
        'mappable': bool(features),
    }


def fetch_coned_new_york_power_outages():
    return fetch_ifactor_county_power_outages(
        'coned_ny', 'Con Edison', CONED_POWER_DATA_ROOT, CONED_POWER_SOURCE_URL,
        'thematic_countyborough',
    )


def fetch_orange_rockland_new_york_power_outages():
    return fetch_ifactor_county_power_outages(
        'orange_rockland_ny', 'Orange & Rockland Utilities', ORU_POWER_DATA_ROOT,
        ORU_POWER_SOURCE_URL, 'thematic_county', {'ORANGE', 'ROCKLAND', 'SULLIVAN'},
    )


def fetch_eversource_power_outages(state_prefix, region_id, provider_key, provider_name):
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': EVERSOURCE_CT_SOURCE_URL,
        'Accept': 'application/json',
    }
    metadata = fetch_json_url(f'{EVERSOURCE_CT_DATA_ROOT}/metadata.json', headers=headers, timeout=20)
    directory = str(metadata.get('directory') or '').strip()
    if not re.fullmatch(r'[0-9_]{10,40}', directory):
        raise ValueError('Eversource returned an invalid outage-data directory')
    data_root = f'{EVERSOURCE_CT_DATA_ROOT}/{directory}'
    overview = fetch_json_url(f'{data_root}/data.json', headers=headers, timeout=20)
    thematic_towns = fetch_json_url(
        f'{data_root}/thematic_town/thematic_areas.json', headers=headers, timeout=20
    )
    thematic_regions = fetch_json_url(
        f'{data_root}/thematic_region/thematic_areas.json', headers=headers, timeout=20
    )
    overview_data = overview.get('summaryFileData') or {}
    region_item = next(
        (
            item for item in (thematic_regions.get('file_data') or [])
            if str(item.get('id') or '').upper() == region_id
        ),
        {},
    )
    region_desc = region_item.get('desc') or {}
    updated_at = overview_data.get('date_generated')
    features = []
    for item in thematic_towns.get('file_data') or []:
        item_id = str(item.get('id') or '')
        if not item_id.startswith(f'{state_prefix}_'):
            continue
        desc = item.get('desc') or {}
        affected = safe_int((desc.get('cust_a') or {}).get('val'))
        if affected <= 0:
            continue
        geometry = kubra_geometry(item.get('geom') or {})
        center = kubra_center(item.get('geom') or {})
        if not geometry and center:
            geometry = point_geometry(center[0], center[1])
        if not geometry:
            continue
        percent_raw = str(desc.get('percent_out') or '').rstrip('%')
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:{provider_key}:{item_id}',
                'provider': provider_name,
                'provider_key': provider_key,
                'source_url': EVERSOURCE_CT_SOURCE_URL,
                'kind': 'area',
                'area_name': display_text(item.get('title')) or item_id.removeprefix(f'{state_prefix}_').title(),
                'customers_affected': affected,
                'customers_served': safe_int(desc.get('cust_s')),
                'outages': safe_int(desc.get('n_out')),
                'percent_customers_affected': safe_float(percent_raw),
                'etr': desc.get('etr') or None,
                'updated_at': updated_at,
            },
        })
    summary_affected = safe_int((region_desc.get('cust_a') or {}).get('val'))
    return features, {
        'provider': provider_name,
        'source_url': EVERSOURCE_CT_SOURCE_URL,
        'total_outages': safe_int(region_desc.get('n_out')),
        'total_customers_affected': summary_affected,
        'total_customers_served': safe_int(region_desc.get('cust_s')),
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def fetch_eversource_connecticut_power_outages():
    return fetch_eversource_power_outages(
        'CT', 'CONNECTICUT', 'eversource_ct', 'Eversource Connecticut'
    )


def fetch_eversource_massachusetts_power_outages():
    return fetch_eversource_power_outages(
        'MA', 'E.MASSACHUSETTS', 'eversource_ma', 'Eversource Massachusetts'
    )


def fetch_eversource_new_hampshire_power_outages():
    return fetch_eversource_power_outages(
        'NH', 'NEW HAMPSHIRE', 'eversource_nh', 'Eversource New Hampshire'
    )


def fetch_nhec_power_outages():
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': NHEC_POWER_SOURCE_URL,
        'Accept': 'application/json',
    }
    config = fetch_json_url(
        f'{NHEC_POWER_DATA_ROOT}/config.json', headers=headers, timeout=20
    )
    summary = fetch_json_url(
        f'{NHEC_POWER_DATA_ROOT}/summary.json', headers=headers, timeout=20
    )
    boundary_extent = ((config.get('mapSettings') or {}).get('boundaryExtent') or [])
    if len(boundary_extent) < 2 or not isinstance(summary, dict):
        raise ValueError('NHEC returned an unexpected outage response')

    updated_raw = safe_int(summary.get('lastUpdate'))
    updated_at = (
        datetime.datetime.fromtimestamp(updated_raw / 1000, tz=datetime.timezone.utc).isoformat()
        if updated_raw > 0 else None
    )
    outages = [item for item in (summary.get('outages') or []) if isinstance(item, dict)]
    features = []
    for index, item in enumerate(outages):
        affected = safe_int(item.get('nbrOut'))
        if affected <= 0 or 'x' not in item or 'y' not in item:
            continue
        x = safe_float(item.get('x')) + safe_float(boundary_extent[0])
        y = safe_float(item.get('y')) + safe_float(boundary_extent[1])
        lat, lon = web_mercator_to_latlon(x, y)
        if not point_in_state('NH', lat, lon):
            continue
        outage_id = str(
            item.get('id') or item.get('outageId') or item.get('outageID') or
            hashlib.sha1(
                f'{x:.2f}:{y:.2f}:{affected}:{item.get("timeOff") or ""}'.encode()
            ).hexdigest()[:16]
        )
        comment = strip_tags(item.get('comment'))
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:nhec:{outage_id}',
                'provider': 'New Hampshire Electric Cooperative',
                'provider_key': 'nhec',
                'source_url': NHEC_POWER_SOURCE_URL,
                'kind': 'point',
                'area_name': comment or f'NHEC outage {index + 1}',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': 1,
                'percent_customers_affected': 0,
                'etr': item.get('estimateTime') or item.get('estimatedRestorationTime'),
                'start_time': item.get('timeOff'),
                'updated_at': updated_at,
            },
        })

    return features, {
        'provider': 'New Hampshire Electric Cooperative',
        'source_url': NHEC_POWER_SOURCE_URL,
        'total_outages': len(outages),
        'total_customers_affected': sum(safe_int(item.get('nbrOut')) for item in outages),
        'total_customers_served': safe_int(summary.get('totalServed')),
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def vermont_town_key(value):
    normalized = re.sub(r'\bsaint\b', 'st', str(value or '').casefold())
    return re.sub(r'[^a-z0-9]+', '', normalized)


def fetch_vtrans_town_geometries(town_names):
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': VTOUTAGES_SOURCE_URL,
        'Accept': 'application/geo+json,application/json',
    }
    geometry_index = {}
    names = sorted({display_text(name) for name in town_names if display_text(name)})
    for start in range(0, len(names), 40):
        chunk = names[start:start + 40]
        clauses = []
        for name in chunk:
            escaped = name.upper().replace("'", "''")
            clauses.append(f"UPPER(TOWN)='{escaped}'")
        params = urllib.parse.urlencode({
            'where': ' OR '.join(clauses) if clauses else 'OBJECTID < 0',
            'outFields': 'OBJECTID,TOWN,TOWNNAME,TITLENAME,COMMTYPE',
            'returnGeometry': 'true',
            'outSR': '4326',
            'geometryPrecision': '5',
            'maxAllowableOffset': '0.001',
            'f': 'geojson',
        })
        data = fetch_json_url(
            f'{VTRANS_TOWNS_QUERY_URL}?{params}', headers=headers, timeout=30
        )
        for feature in data.get('features') or []:
            geometry = feature.get('geometry')
            props = feature.get('properties') or {}
            if not geometry:
                continue
            for candidate in (props.get('TOWN'), props.get('TOWNNAME')):
                key = vermont_town_key(candidate)
                if key and key not in geometry_index:
                    geometry_index[key] = geometry
    return geometry_index


def fetch_vtoutages_power_outages():
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Origin': VTOUTAGES_SOURCE_URL.rstrip('/'),
        'Referer': VTOUTAGES_SOURCE_URL,
        'Accept': 'application/json',
    }
    utilities = fetch_json_url(
        f'{VTOUTAGES_API_ROOT}/utility?utilityType=electric',
        headers=headers,
        timeout=25,
    )
    towns = fetch_json_url(
        f'{VTOUTAGES_API_ROOT}/town?utilityType=electric&expand=utility',
        headers=headers,
        timeout=25,
    )
    if not isinstance(utilities, list) or not isinstance(towns, list):
        raise ValueError('VTOutages returned an unexpected statewide outage response')

    active_towns = [town for town in towns if safe_int(town.get('totalCustomers')) > 0]
    geometry_index = fetch_vtrans_town_geometries(
        town.get('name') for town in active_towns
    )
    utility_index = {
        str(item.get('utilityId')): item
        for item in utilities
        if item.get('utilityId') is not None
    }
    features = []
    for town in active_towns:
        town_name = display_text(town.get('name') or 'Vermont municipality')
        geometry = geometry_index.get(vermont_town_key(town_name))
        if not geometry:
            continue
        town_utilities = [item for item in (town.get('utilities') or []) if isinstance(item, dict)]
        provider_names = []
        provider_codes = []
        updated_values = []
        for item in town_utilities:
            provider_name = display_text(item.get('description') or item.get('name'))
            if provider_name and provider_name not in provider_names:
                provider_names.append(provider_name)
            provider_code = str(item.get('code') or '').strip().lower()
            if provider_code and provider_code not in provider_codes:
                provider_codes.append(provider_code)
            utility = utility_index.get(str(item.get('utility_id') or item.get('utilityId') or ''))
            if utility and utility.get('asOfDate'):
                updated_values.append(str(utility['asOfDate']))
        provider = ' / '.join(provider_names) or 'Vermont Utilities'
        provider_key = (
            f'vtoutages_{provider_codes[0]}' if len(provider_codes) == 1 else 'vtoutages'
        )
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:vtoutages:{town.get("townId") or vermont_town_key(town_name)}',
                'provider': provider,
                'provider_key': provider_key,
                'source_url': VTOUTAGES_SOURCE_URL,
                'kind': 'area',
                'area_name': town_name,
                'customers_affected': safe_int(town.get('totalCustomers')),
                'customers_served': 0,
                'outages': 0,
                'percent_customers_affected': 0,
                'status': 'Reported through Vermont statewide utility outage data',
                'etr': None,
                'start_time': None,
                'updated_at': max(updated_values) if updated_values else None,
            },
        })

    updated_values = [str(item['asOfDate']) for item in utilities if item.get('asOfDate')]
    return features, {
        'provider': 'Vermont Statewide Utilities',
        'source_url': VTOUTAGES_SOURCE_URL,
        'total_outages': sum(safe_int(item.get('totalEvents')) for item in utilities),
        'total_customers_affected': sum(
            safe_int(item.get('totalCustomers')) for item in utilities
        ),
        'total_customers_served': 0,
        'last_updated': max(updated_values) if updated_values else None,
        'mappable': len(features) == len(active_towns),
    }


def fetch_rhode_island_energy_power_outages():
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': RIE_POWER_SOURCE_URL,
        'Accept': 'application/json',
    }
    pins = fetch_json_url(
        f'{RIE_POWER_API_ROOT}/Outage/Pins?opco=RI', headers=headers, timeout=25
    )
    tabular = fetch_json_url(
        f'{RIE_POWER_API_ROOT}/Outage/Tabular?opco=RI', headers=headers, timeout=25
    )
    if not isinstance(pins, list) or not isinstance(tabular, dict):
        raise ValueError('Rhode Island Energy returned an unexpected outage response')

    municipality_served = {}
    total_served = 0
    for county in tabular.get('data') or []:
        total_served += safe_int(county.get('tc'))
        for municipality in county.get('mun') or []:
            name = str(municipality.get('nm') or '').strip().upper()
            if name:
                municipality_served[name] = safe_int(municipality.get('tc'))

    polygon_ids = [safe_int(item.get('id')) for item in pins if item.get('hp') and item.get('id')]
    polygon_index = {}
    for start in range(0, len(polygon_ids), 20):
        chunk = polygon_ids[start:start + 20]
        encoded_ids = urllib.parse.quote(json.dumps(chunk, separators=(',', ':')), safe='[],:')
        polygons = fetch_json_url(
            f'{RIE_POWER_API_ROOT}/Outage/Polygon/Datas/{encoded_ids}?opco=RI',
            headers=headers,
            timeout=25,
        )
        for polygon in polygons if isinstance(polygons, list) else []:
            polygon_index[str(polygon.get('id'))] = polygon.get('pts') or []

    updated_at = parse_time_iso(str(tabular.get('dt') or ''), ['%m-%d-%Y %H:%M']) or tabular.get('dt')
    bounds = REGIONS['RI']['bounds']
    features = []
    for item in pins:
        outage_id = str(item.get('id') or '').strip()
        lat = safe_float(item.get('a'))
        lon = safe_float(item.get('o'))
        affected = safe_int(item.get('nc'))
        if not outage_id or affected <= 0 or not (
            bounds['min_lat'] <= lat <= bounds['max_lat'] and
            bounds['min_lon'] <= lon <= bounds['max_lon']
        ):
            continue
        municipality = str(item.get('mun') or '').strip().upper()
        county = display_text(item.get('cty'))
        served = municipality_served.get(municipality, 0)
        points = []
        for point in polygon_index.get(outage_id, []):
            point_lat = safe_float(point.get('a'))
            point_lon = safe_float(point.get('o'))
            if point_lat and point_lon:
                points.append([point_lon, point_lat])
        if len(points) >= 3:
            if points[0] != points[-1]:
                points.append(points[0])
            geometry = {'type': 'Polygon', 'coordinates': [points]}
            kind = 'area'
        else:
            geometry = point_geometry(lat, lon)
            kind = 'point'
        area_name = display_text(municipality) or (
            f'{county} County' if county else 'Rhode Island Energy outage'
        )
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:rie:{outage_id}',
                'provider': 'Rhode Island Energy',
                'provider_key': 'rie',
                'source_url': RIE_POWER_SOURCE_URL,
                'kind': kind,
                'area_name': area_name,
                'customers_affected': affected,
                'customers_served': served,
                'outages': 1,
                'percent_customers_affected': (affected / served * 100) if served else 0,
                'etr': None,
                'start_time': None,
                'updated_at': updated_at,
            },
        })
    return features, {
        'provider': 'Rhode Island Energy',
        'source_url': RIE_POWER_SOURCE_URL,
        'total_outages': len(pins),
        'total_customers_affected': safe_int(tabular.get('oc')),
        'total_customers_served': total_served,
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def project_esri_points(points, in_sr):
    projected = []
    for start in range(0, len(points), 100):
        chunk = points[start:start + 100]
        params = urllib.parse.urlencode({
            'inSR': str(in_sr),
            'outSR': '4326',
            'geometries': json.dumps({
                'geometryType': 'esriGeometryPoint',
                'geometries': [{'x': x, 'y': y} for x, y in chunk],
            }, separators=(',', ':')),
            'f': 'json',
        })
        data = fetch_json_url(f'{ARCGIS_GEOMETRY_PROJECT_URL}?{params}', timeout=25)
        geometries = data.get('geometries') or []
        if len(geometries) != len(chunk):
            raise ValueError('ArcGIS geometry projection returned an unexpected point count')
        projected.extend((safe_float(item.get('y')), safe_float(item.get('x'))) for item in geometries)
    return projected


def outage_area_value(item, property_name):
    for detail in item.get('additionalProperties') or []:
        if detail.get('property') == property_name:
            values = detail.get('value') or []
            return str(values[0]).strip() if values else ''
    return ''


def fetch_nes_outages():
    rows = fetch_json_url(NES_OUTAGES_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/json',
        'Referer': NES_SOURCE_URL,
    })
    if not isinstance(rows, list):
        raise ValueError('Unexpected Nashville Electric Service outage response')
    tn_bounds = REGIONS['TN']['bounds']
    features = []
    for item in rows:
        lat = safe_float(item.get('latitude'))
        lon = safe_float(item.get('longitude'))
        if not (
            tn_bounds['min_lat'] <= lat <= tn_bounds['max_lat'] and
            tn_bounds['min_lon'] <= lon <= tn_bounds['max_lon']
        ):
            continue
        outage_id = item.get('id') or item.get('identifier') or len(features)
        county = outage_area_value(item, 'AREA_COUNTY')
        area = outage_area_value(item, 'AREA_SERVICE') or outage_area_value(item, 'AREA_MUNICIPALITY')
        area_name = area or (f'{county} County' if county else 'Nashville-area outage')
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:nes:{outage_id}',
                'provider': 'Nashville Electric Service',
                'provider_key': 'nes',
                'source_url': NES_SOURCE_URL,
                'kind': 'point',
                'area_name': area_name,
                'customers_affected': safe_int(item.get('numPeople')),
                'customers_served': 0,
                'outages': 1,
                'status': item.get('status'),
                'reason': item.get('cause') or None,
                'etr': epoch_milliseconds_iso(item.get('etrTime')),
                'start_time': epoch_milliseconds_iso(item.get('startTime')),
                'updated_at': epoch_milliseconds_iso(item.get('lastUpdatedTime')),
            },
        })
    provider_summary = {
        'provider': 'Nashville Electric Service',
        'source_url': NES_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': sum(
            safe_int((feature.get('properties') or {}).get('customers_affected')) for feature in features
        ),
        'total_customers_served': 0,
        'last_updated': max(
            ((feature.get('properties') or {}).get('updated_at') or '' for feature in features),
            default=None,
        ),
        'mappable': bool(features),
    }
    return features, provider_summary


def fetch_kub_tennessee_outages():
    data = fetch_json_url(KUB_TN_OUTAGES_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/json',
        'Referer': KUB_TN_SOURCE_URL,
    })
    rows = data.get('electricOutages') or []
    coords = [(safe_float(item.get('x')), safe_float(item.get('y'))) for item in rows]
    projected = project_esri_points(coords, 2274) if coords else []
    tn_bounds = REGIONS['TN']['bounds']
    features = []
    for item, (lat, lon) in zip(rows, projected):
        if not (
            tn_bounds['min_lat'] <= lat <= tn_bounds['max_lat'] and
            tn_bounds['min_lon'] <= lon <= tn_bounds['max_lon']
        ):
            continue
        customers_affected = safe_int(item.get('customerCount'))
        if customers_affected <= 0:
            continue
        outage_id = item.get('id') or len(features)
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:kub_tn:{outage_id}',
                'provider': 'Knoxville Utilities Board',
                'provider_key': 'kub_tn',
                'source_url': KUB_TN_SOURCE_URL,
                'kind': 'point',
                'area_name': 'KUB service-area outage',
                'customers_affected': customers_affected,
                'customers_served': 0,
                'outages': 1,
                'status': item.get('statusMessage'),
                'reason': None,
                'etr': item.get('estimatedRestoreTime'),
                'start_time': item.get('reportedDateTime'),
                'updated_at': (data.get('electricOutageInfo') or {}).get('lastUpdated'),
            },
        })
    info = data.get('electricOutageInfo') or {}
    provider_summary = {
        'provider': 'Knoxville Utilities Board',
        'source_url': KUB_TN_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': safe_int(info.get('electricCustomersWithoutPower')),
        'total_customers_served': safe_int(info.get('totalElectricCustomers')),
        'last_updated': info.get('lastUpdated'),
        'mappable': bool(features),
    }
    return features, provider_summary


def fetch_entergy_state_outages(state_code, provider_key, provider_name, outages_url, source_url):
    rows = fetch_json_url(outages_url, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/json',
        'Referer': source_url,
    })
    if not isinstance(rows, list):
        raise ValueError(f'Unexpected {provider_name} outage response')
    features = []
    for item in rows:
        if item.get('type') == 'PLANNED_OUTAGE' and str(item.get('status') or '').upper() != 'IN PROGRESS':
            continue
        lat = safe_float(item.get('latitude'))
        lon = safe_float(item.get('longitude'))
        if not point_in_state(state_code, lat, lon):
            continue
        outage_id = item.get('id') or item.get('identifier') or len(features)
        customers_affected = safe_int(item.get('numPeople'))
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:{provider_key}:{outage_id}',
                'provider': provider_name,
                'provider_key': provider_key,
                'source_url': source_url,
                'kind': 'point',
                'area_name': f'{REGIONS[state_code]["name"]} outage',
                'customers_affected': customers_affected,
                'customers_served': 0,
                'outages': 1,
                'status': item.get('status'),
                'reason': item.get('cause') or item.get('description'),
                'etr': epoch_milliseconds_iso(item.get('etrTime')),
                'start_time': epoch_milliseconds_iso(item.get('startTime')),
                'updated_at': epoch_milliseconds_iso(item.get('lastUpdatedTime')),
            },
        })
    provider_summary = {
        'provider': provider_name,
        'source_url': source_url,
        'total_outages': len(features),
        'total_customers_affected': sum(
            safe_int((feature.get('properties') or {}).get('customers_affected')) for feature in features
        ),
        'total_customers_served': 0,
        'last_updated': max(
            ((feature.get('properties') or {}).get('updated_at') or '' for feature in features),
            default=None,
        ),
        'mappable': bool(features),
    }
    return features, provider_summary


def fetch_entergy_mississippi_outages():
    return fetch_entergy_state_outages(
        'MS', 'entergy_ms', 'Entergy Mississippi',
        ENTERGY_MS_OUTAGES_URL, ENTERGY_MS_SOURCE_URL,
    )


def fetch_entergy_arkansas_outages():
    return fetch_entergy_state_outages(
        'AR', 'entergy_ar', 'Entergy Arkansas',
        ENTERGY_AR_OUTAGES_URL, ENTERGY_AR_SOURCE_URL,
    )


def fetch_entergy_louisiana_outages():
    return fetch_entergy_state_outages(
        'LA', 'entergy_la', 'Entergy Louisiana',
        ENTERGY_LA_OUTAGES_URL, ENTERGY_LA_SOURCE_URL,
    )


def fetch_entergy_texas_outages():
    return fetch_entergy_state_outages(
        'TX', 'entergy_tx', 'Entergy Texas',
        ENTERGY_TX_OUTAGES_URL, ENTERGY_TX_SOURCE_URL,
    )


def fetch_centerpoint_texas_outages():
    return fetch_entergy_state_outages(
        'TX', 'centerpoint_tx', 'CenterPoint Energy Texas',
        CENTERPOINT_TX_OUTAGES_URL, CENTERPOINT_TX_SOURCE_URL,
    )


def cleco_time_iso(value):
    text = ' '.join(str(value or '').split())
    if not text:
        return None
    central_tz = ZoneInfo('America/Chicago')
    for fmt in ('%m/%d/%Y %H:%M:%S', '%m/%d/%Y %I:%M:%S %p'):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=central_tz).isoformat()
        except ValueError:
            continue
    return text


def fetch_cleco_louisiana_outages():
    document = fetch_json_url(CLECO_OUTAGES_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/json',
        'Referer': CLECO_OUTAGES_SOURCE_URL,
    })
    status = document.get('status') or {}
    rows = document.get('data') or []
    if status.get('error') or not isinstance(rows, list):
        raise ValueError('Unexpected Cleco outage response')
    features = []
    for item in rows:
        lat = safe_float(item.get('lat'))
        lon = safe_float(item.get('lon'))
        affected = safe_int(item.get('affectedCount'))
        if affected <= 0 or not point_in_state('LA', lat, lon):
            continue
        incident_id = display_text(item.get('incidentId')) or str(len(features))
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:cleco:{incident_id}',
                'provider': 'Cleco',
                'provider_key': 'cleco_la',
                'source_url': CLECO_OUTAGES_SOURCE_URL,
                'kind': 'point',
                'area_name': display_text(item.get('location')) or 'Louisiana outage',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': 1,
                'status': item.get('status'),
                'reason': display_text(item.get('description')) or None,
                'etr': cleco_time_iso(item.get('endTime')),
                'start_time': cleco_time_iso(item.get('startTime')),
                'updated_at': cleco_time_iso(item.get('lastUpdateTime')),
            },
        })
    latest_update = max(
        ((feature.get('properties') or {}).get('updated_at') or '' for feature in features),
        default=None,
    )
    return features, {
        'provider': 'Cleco',
        'source_url': CLECO_OUTAGES_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': sum(
            safe_int((feature.get('properties') or {}).get('customers_affected'))
            for feature in features
        ),
        'total_customers_served': 0,
        'last_updated': latest_update,
        'mappable': bool(features),
    }


def fetch_duke_power_outages():
    return fetch_duke_public_outages(
        'FL', 'duke', 'Duke Energy Florida',
        'https://outagemaps.duke-energy.com/#/current-outages/fl', 'DEF'
    )


def fetch_duke_public_outages(
    state_code, provider_key, provider_name, source_url, jurisdiction=None
):
    state_bounds = REGIONS[state_code]['bounds']
    where = f"JURISDICTION='{jurisdiction}'" if jurisdiction else '1=1'
    params = urllib.parse.urlencode({
        'where': where,
        'geometry': ','.join(str(value) for value in (
            state_bounds['min_lon'], state_bounds['min_lat'],
            state_bounds['max_lon'], state_bounds['max_lat'],
        )),
        'geometryType': 'esriGeometryEnvelope',
        'spatialRel': 'esriSpatialRelIntersects',
        'inSR': 4326,
        'outSR': 4326,
        'outFields': 'OBJECTID,START_TIME,ETR,CAUSE,CREW_STATUS,AFFECTED_CUSTOMERS,JURISDICTION,last_edited_date',
        'returnGeometry': 'true',
        'f': 'geojson',
    })
    data = fetch_json_url(f'{DUKE_NC_PUBLIC_OUTAGES_URL}?{params}', headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'application/geo+json,application/json',
    })
    features = []
    for feature in data.get('features') or []:
        props = feature.get('properties') or {}
        center = geojson_center(feature.get('geometry'))
        if not center:
            continue
        lat, lon = center
        if not (
            state_bounds['min_lat'] <= lat <= state_bounds['max_lat'] and
            state_bounds['min_lon'] <= lon <= state_bounds['max_lon']
        ):
            continue
        customers_affected = safe_int(props.get('AFFECTED_CUSTOMERS'))
        if customers_affected <= 0:
            continue
        outage_id = props.get('OBJECTID') or len(features)
        updated_at = epoch_milliseconds_iso(props.get('last_edited_date'))
        features.append({
            'type': 'Feature',
            'geometry': feature.get('geometry'),
            'properties': {
                'key': f'power:{provider_key}:{outage_id}',
                'provider': provider_name,
                'provider_key': provider_key,
                'source_url': source_url,
                'kind': 'point',
                'area_name': f'{REGIONS[state_code]["name"]} outage',
                'customers_affected': customers_affected,
                'customers_served': 0,
                'outages': 1,
                'status': props.get('CREW_STATUS'),
                'reason': props.get('CAUSE'),
                'etr': props.get('ETR'),
                'start_time': props.get('START_TIME'),
                'updated_at': updated_at,
            },
        })
    provider_summary = {
        'provider': provider_name,
        'source_url': source_url,
        'total_outages': len(features),
        'total_customers_affected': sum(
            safe_int((feature.get('properties') or {}).get('customers_affected')) for feature in features
        ),
        'total_customers_served': 0,
        'last_updated': max(
            ((feature.get('properties') or {}).get('updated_at') or '' for feature in features),
            default=None,
        ),
        'mappable': bool(features),
    }
    return features, provider_summary


def fetch_duke_north_carolina_outages():
    return fetch_duke_public_outages(
        'NC', 'duke_nc', 'Duke Energy North Carolina', DUKE_NC_PUBLIC_SOURCE_URL
    )


def fetch_duke_ohio_outages():
    return fetch_duke_public_outages(
        'OH', 'duke_oh', 'Duke Energy Ohio', DUKE_OH_PUBLIC_SOURCE_URL, 'DEM'
    )


def fetch_duke_indiana_outages():
    return fetch_duke_public_outages(
        'IN', 'duke_in', 'Duke Energy Indiana', DUKE_IN_PUBLIC_SOURCE_URL, 'DEM'
    )


def fetch_aes_ohio_power_outages():
    req = urllib.request.Request(AES_OHIO_POWER_XML_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': AES_OHIO_POWER_SOURCE_URL,
        'Accept': 'application/xml,text/xml,*/*',
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())
    message = root.find('Message')
    updated_at = display_text(message.findtext('Outageat')) if message is not None else None
    if str(updated_at or '').lower() == 'null':
        updated_at = None
    features = []
    for marker in root.findall('Markers'):
        lat = optional_float(marker.findtext('LAT'))
        lon = optional_float(marker.findtext('LNG'))
        affected = safe_int(marker.findtext('TOTALCUSTS'))
        if lat is None or lon is None or affected <= 0 or not point_in_state('OH', lat, lon):
            continue
        incident_id = display_text(marker.findtext('INCIDENTID')) or str(len(features))
        county = display_text(marker.findtext('COUNTY'))
        etr = display_text(marker.findtext('EstimateTime'))
        if etr.lower() == 'null':
            etr = None
        start_time = display_text(marker.findtext('OutageTime'))
        if start_time.lower() == 'null':
            start_time = None
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:aes_ohio:{incident_id}',
                'provider': 'AES Ohio',
                'provider_key': 'aes_ohio',
                'source_url': AES_OHIO_POWER_SOURCE_URL,
                'kind': 'point',
                'area_name': f'{county.title()} County' if county else 'AES Ohio outage',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': 1,
                'etr': etr,
                'start_time': start_time,
                'updated_at': updated_at,
            },
        })
    reported_total = safe_int(message.findtext('TotalOut')) if message is not None else 0
    return features, {
        'provider': 'AES Ohio',
        'source_url': AES_OHIO_POWER_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': reported_total or sum(
            safe_int((feature.get('properties') or {}).get('customers_affected'))
            for feature in features
        ),
        'total_customers_served': 539000,
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def fetch_aes_indiana_power_outages():
    req = urllib.request.Request(AES_INDIANA_POWER_XML_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': AES_INDIANA_POWER_SOURCE_URL,
        'Accept': 'application/xml,text/xml,*/*',
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        root = ET.fromstring(resp.read())
    updated_at = display_text(root.findtext('.//Outageat'))
    if str(updated_at or '').lower() == 'null':
        updated_at = None
    features = []
    for marker in root.findall('.//Marker'):
        lat = optional_float(marker.findtext('Lat'))
        lon = optional_float(marker.findtext('Long'))
        affected = safe_int(marker.findtext('CustAffected'))
        if lat is None or lon is None or affected <= 0 or not point_in_state('IN', lat, lon):
            continue
        incident_id = display_text(marker.findtext('IncidentId')) or str(len(features))
        etr = display_text(marker.findtext('Etr'))
        if etr.lower() in {'', 'null', 'unknown'}:
            etr = None
        start_time = display_text(marker.findtext('OutageStart'))
        if start_time.lower() in {'', 'null'}:
            start_time = None
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:aes_indiana:{incident_id}',
                'provider': 'AES Indiana',
                'provider_key': 'aes_indiana',
                'source_url': AES_INDIANA_POWER_SOURCE_URL,
                'kind': 'point',
                'area_name': 'AES Indiana outage',
                'customers_affected': affected,
                'customers_served': 0,
                'outages': 1,
                'etr': etr,
                'start_time': start_time,
                'updated_at': updated_at,
            },
        })
    reported_affected = safe_int(root.findtext('.//TotalCustAffected'))
    return features, {
        'provider': 'AES Indiana',
        'source_url': AES_INDIANA_POWER_SOURCE_URL,
        'total_outages': len(features),
        'total_customers_affected': reported_affected or sum(
            safe_int((feature.get('properties') or {}).get('customers_affected'))
            for feature in features
        ),
        'total_customers_served': 500000,
        'last_updated': updated_at,
        'mappable': bool(features),
    }


def fetch_nipsco_power_outages():
    payload = fetch_json_url(NIPSCO_POWER_OUTAGES_URL, headers={
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Referer': NIPSCO_POWER_SOURCE_URL,
        'Accept': 'application/json',
    }, timeout=35)
    rows = payload.get('outageList') or []
    if not isinstance(rows, list):
        raise ValueError('NIPSCO returned an unexpected outage response')

    groups = {}
    total_outages = 0
    total_affected = 0
    for item in rows:
        lat = optional_float(item.get('lat'))
        lon = optional_float(item.get('lng'))
        affected = safe_int(item.get('affected'))
        if lat is None or lon is None or affected <= 0 or not point_in_state('IN', lat, lon):
            continue
        total_outages += 1
        total_affected += affected
        city = display_text(item.get('city')) or 'Northern Indiana'
        postal_code = display_text(item.get('zip'))
        key = (city.casefold(), postal_code)
        group = groups.setdefault(key, {
            'city': city,
            'zip': postal_code,
            'affected': 0,
            'outages': 0,
            'weighted_lat': 0.0,
            'weighted_lon': 0.0,
            'status': display_text(item.get('status')),
            'cause': display_text(item.get('cause') or item.get('comment')),
            'reported': display_text(item.get('reported')),
            'restore': display_text(item.get('restore')),
        })
        group['affected'] += affected
        group['outages'] += 1
        group['weighted_lat'] += lat * affected
        group['weighted_lon'] += lon * affected
        if not group['status']:
            group['status'] = display_text(item.get('status'))
        if not group['cause']:
            group['cause'] = display_text(item.get('cause') or item.get('comment'))
        reported = display_text(item.get('reported'))
        if reported and (not group['reported'] or reported < group['reported']):
            group['reported'] = reported
        restore = display_text(item.get('restore'))
        if restore.startswith('0001-'):
            restore = ''
        if restore and (not group['restore'] or restore > group['restore']):
            group['restore'] = restore

    features = []
    for (city_key, postal_code), group in groups.items():
        affected = group['affected']
        lat = group['weighted_lat'] / affected
        lon = group['weighted_lon'] / affected
        area_name = group['city'] + (f' {postal_code}' if postal_code else '')
        features.append({
            'type': 'Feature',
            'geometry': point_geometry(lat, lon),
            'properties': {
                'key': f'power:nipsco_in:{city_key}:{postal_code}',
                'provider': 'NIPSCO',
                'provider_key': 'nipsco_in',
                'source_url': NIPSCO_POWER_SOURCE_URL,
                'kind': 'point',
                'area_name': area_name,
                'customers_affected': affected,
                'customers_served': 0,
                'outages': group['outages'],
                'status': group['status'] or None,
                'reason': group['cause'] or None,
                'etr': group['restore'] if not group['restore'].startswith('0001-') else None,
                'start_time': group['reported'] or None,
                'updated_at': None,
            },
        })
    return features, {
        'provider': 'NIPSCO',
        'source_url': NIPSCO_POWER_SOURCE_URL,
        'total_outages': total_outages,
        'total_customers_affected': total_affected,
        'total_customers_served': 0,
        'last_updated': None,
        'mappable': bool(features),
    }


def fetch_teco_power_outages():
    florida_bounds = REGIONS['FL']['bounds']
    config = fetch_json_url(TECO_POWER_CONFIG_URL)
    tiles_doc = fetch_json_url(TECO_POWER_TILES_URL, data={
        'size': 10000,
        'query': {
            'bool': {
                'must': {'match_all': {}},
                'filter': {
                    'geo_bounding_box': {
                        'polygonCenter': {
                            'top_left': {
                                'lat': florida_bounds['max_lat'],
                                'lon': florida_bounds['min_lon'],
                            },
                            'bottom_right': {
                                'lat': florida_bounds['min_lat'],
                                'lon': florida_bounds['max_lon'],
                            },
                        }
                    }
                }
            }
        },
        'sort': [{'updateTime': 'asc'}, {'incidentId': 'asc'}],
        '_source': [
            'updateTime',
            'status',
            'reason',
            'customerCount',
            'polygonCenter',
            'incidentId',
            'polygonPointsGoogle',
            'estimatedTimeOfRestoration',
        ],
    })

    hits = (((tiles_doc.get('hits') or {}).get('hits') or []))
    total = safe_int((((tiles_doc.get('hits') or {}).get('total') or {}).get('value')))
    provider_summary = {
        'provider': 'Tampa Electric',
        'source_url': TECO_POWER_SOURCE_URL,
        'total_outages': total,
        'total_customers_affected': safe_int((((tiles_doc.get('aggregations') or {}).get('customerCountSum') or {}).get('value'))),
        'total_customers_served': 0,
        'last_updated': ((tiles_doc.get('_tiles') or {}).get('generated')) or config.get('lastDateTime'),
        'mappable': True,
    }

    features = []
    for hit in hits:
        source = hit.get('_source') or {}
        center = source.get('polygonCenter') or []
        if len(center) < 2:
            continue
        lon = safe_float(center[0])
        lat = safe_float(center[1])
        if not in_region(lat, lon):
            continue

        geometry = polygon_geometry_from_google_points(source.get('polygonPointsGoogle'))
        if not geometry:
            geometry = point_geometry(lat, lon)

        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:teco:{source.get("incidentId")}',
                'provider': 'Tampa Electric',
                'provider_key': 'teco',
                'source_url': TECO_POWER_SOURCE_URL,
                'kind': 'point',
                'area_name': source.get('incidentId') or 'Tampa Electric outage',
                'customers_affected': safe_int(source.get('customerCount')),
                'customers_served': 0,
                'outages': 1,
                'status': source.get('status'),
                'reason': source.get('reason'),
                'etr': source.get('estimatedTimeOfRestoration'),
                'start_time': None,
                'updated_at': source.get('updateTime') or provider_summary.get('last_updated'),
            }
        })
    return features, provider_summary


def fetch_keys_power_outages():
    summary_doc = fetch_json_url(KEYS_POWER_SUMMARY_URL, headers=KEYS_POWER_HEADERS)
    outages_doc = fetch_json_url(KEYS_POWER_OUTAGES_URL, headers=KEYS_POWER_HEADERS)
    polygons_doc = fetch_json_url(KEYS_POWER_POLYGONS_URL, headers=KEYS_POWER_HEADERS)

    polygons_by_id = {}
    for item in polygons_doc or []:
        outage_id = item.get('outageRecId')
        if not outage_id:
            continue
        geometry = keys_power_geometry(item)
        if geometry:
            polygons_by_id[str(outage_id)] = geometry

    active_outages = [
        item for item in (outages_doc or [])
        if not item.get('isPlanned')
    ]
    customers_served = safe_int(summary_doc.get('customersServed'))
    provider_summary = {
        'provider': 'Keys Energy Services',
        'source_url': KEYS_POWER_SOURCE_URL,
        'total_outages': len(active_outages),
        'total_customers_affected': safe_int(summary_doc.get('customersOutNow')),
        'total_customers_served': customers_served,
        'last_updated': summary_doc.get('updateTime'),
        'mappable': False,
    }

    features = []
    for item in active_outages:
        outage_id = str(item.get('outageRecId') or item.get('outageId') or '').strip()
        point = item.get('outagePoint') or {}
        lat = safe_float(point.get('lat'))
        lon = safe_float(point.get('lng'))
        geometry = polygons_by_id.get(outage_id)
        if geometry and not in_region(lat, lon):
            center = geojson_center(geometry)
            if center:
                lat, lon = center
        if geometry is None and in_region(lat, lon):
            geometry = point_geometry(lat, lon)
        if not geometry or not in_region(lat, lon):
            continue

        customers_affected = safe_int(item.get('customersOutNow'))
        features.append({
            'type': 'Feature',
            'geometry': geometry,
            'properties': {
                'key': f'power:keys:{outage_id or len(features)}',
                'provider': 'Keys Energy Services',
                'provider_key': 'keys',
                'source_url': KEYS_POWER_SOURCE_URL,
                'kind': 'area' if geometry.get('type') != 'Point' else 'point',
                'area_name': item.get('outageName') or item.get('address') or 'Keys outage',
                'customers_affected': customers_affected,
                'customers_served': customers_served,
                'outages': 1,
                'percent_customers_affected': (
                    (customers_affected / customers_served) * 100.0
                    if customers_served > 0 else 0.0
                ),
                'status': item.get('outageWorkStatus'),
                'reason': item.get('cause'),
                'etr': item.get('estimatedTimeOfRestoral'),
                'start_time': item.get('outageStartTime'),
                'updated_at': item.get('outageModifiedTime') or provider_summary.get('last_updated'),
            }
        })

    provider_summary['mappable'] = bool(features)
    return features, provider_summary


def _parse_511_tooltip_html(text):
    """Extract common fields from an Iteris 511 tooltip into a plain dict."""
    def inner_text(s):
        return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', s)).split()) if s else None

    name_m = re.search(r'<td[^>]*>\s*<(?:b|strong)[^>]*>(.*?)</(?:b|strong)>', text, re.S | re.I)
    msg_m = re.search(r'<td[^>]+class=["\']msgContent["\'][^>]*>(.*?)</td>', text, re.S)
    if not msg_m:
        msg_m = re.search(r'<td[^>]+colspan=["\']2["\'][^>]*>(.*?)</td>', text, re.S)
    sev_m = re.search(r'<th[^>]*>\s*Severity\s*</th>\s*<td[^>]*>(.*?)</td>', text, re.S | re.I)
    ts_m = re.search(
        r'<td[^>]*>\s*([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4},\s+\d{1,2}:\d{2}\s+[AP]M)\s*</td>', text
    )
    video_id_m = re.search(r'data-camera-id=["\'](\d+)["\']', text, re.I)
    video_url_m = re.search(r'data-videourl=["\']([^"\']+)["\']', text, re.I)
    snapshot_url_m = re.search(
        r'<img(?=[^>]*class=["\'][^"\']*\bcctvImage\b[^"\']*["\'])'
        r'[^>]+(?:src|data-lazy)=["\']([^"\']+)["\']',
        text,
        re.I,
    )
    return {
        'name':      inner_text(name_m.group(1)) if name_m else None,
        'msg':       inner_text(msg_m.group(1))  if msg_m  else None,
        'severity':  inner_text(sev_m.group(1))  if sev_m  else None,
        'timestamp': ts_m.group(1).strip()        if ts_m   else None,
        'video_id':  video_id_m.group(1)          if video_id_m else None,
        'video_url': html.unescape(video_url_m.group(1)) if video_url_m else None,
        'video_enabled': bool(video_id_m and video_url_m),
        'snapshot_url': html.unescape(snapshot_url_m.group(1)) if snapshot_url_m else None,
        'snapshot_from_video': False,
    }


def _evict_if_full(cache, max_size):
    """Remove the oldest entry when the cache is at capacity. Must be called under the cache's lock."""
    if len(cache) >= max_size:
        cache.pop(next(iter(cache)))


def request_client_ip(peer_ip, forwarded_for=None):
    try:
        peer = ipaddress.ip_address(str(peer_ip or '').strip())
    except ValueError:
        return str(peer_ip or '')
    if not any(peer in network for network in TRUSTED_PROXY_NETWORKS):
        return str(peer)

    chain = []
    for token in str(forwarded_for or '').split(',')[-16:]:
        try:
            chain.append(ipaddress.ip_address(token.strip()))
        except ValueError:
            continue
    chain.append(peer)
    for address in reversed(chain):
        if not any(address in network for network in TRUSTED_PROXY_NETWORKS):
            return str(address)
    return str(peer)


def check_rate_limit(ip, path):
    limit = RATE_LIMITS.get(path, RATE_LIMIT_DEFAULT)
    now = time.time()
    key = (ip, path)
    with _RATE_LIMIT_LOCK:
        state = _RATE_LIMIT_STATE.get(key)
        if state is None or (now - state['window_start']) >= RATE_LIMIT_WINDOW:
            _RATE_LIMIT_STATE[key] = {'count': 1, 'window_start': now}
            if len(_RATE_LIMIT_STATE) > 5000:
                cutoff = now - RATE_LIMIT_WINDOW
                for k in [k for k, v in _RATE_LIMIT_STATE.items() if v['window_start'] < cutoff]:
                    del _RATE_LIMIT_STATE[k]
            return True
        if state['count'] >= limit:
            return False
        state['count'] += 1
        return True


def cached_deflock_json(url, key, ttl=900):
    def load():
        data = fetch_json_url(url, headers={'User-Agent': 'GlobalMap/1.0'}, timeout=35)
        return json.dumps(data, separators=(',', ':')).encode(), 'application/json'

    content, _, _ = API_RESPONSE_CACHE.get_or_load(
        key, load, ttl=ttl, stale_ttl=3600, persist=False, wait_timeout=40,
    )
    return json.loads(content)


def deflock_tiles_for_bbox(index, bbox):
    tile_size = int(index.get('tile_size_degrees') or 20)
    tile_template = str(index.get('tile_url') or '')
    available = set(index.get('regions') or [])
    if not tile_template or tile_size <= 0:
        raise ValueError('DeFlock index did not include a usable tile template')
    min_lon, min_lat, max_lon, max_lat = bbox
    min_lat_tile = math.floor(min_lat / tile_size) * tile_size
    max_lat_tile = math.floor(max_lat / tile_size) * tile_size
    min_lon_tile = math.floor(min_lon / tile_size) * tile_size
    max_lon_tile = math.floor(max_lon / tile_size) * tile_size
    tile_requests = []
    for lat_tile in range(min_lat_tile, max_lat_tile + tile_size, tile_size):
        for lon_tile in range(min_lon_tile, max_lon_tile + tile_size, tile_size):
            tile_key = f'{lat_tile}/{lon_tile}'
            if tile_key not in available:
                continue
            tile_url = tile_template.replace('{lat}', str(lat_tile)).replace('{lon}', str(lon_tile))
            tile_requests.append((tile_key, tile_url))
    return tile_requests


def lithuania_toll_plate_readers(payload, bbox, now=None):
    if not isinstance(payload, dict) or not isinstance(payload.get('features'), list):
        raise ValueError('Lithuanian toll equipment catalog is invalid')
    now = time.time() if now is None else now
    min_lon, min_lat, max_lon, max_lat = bbox
    elements = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        props = row.get('properties') or {}
        geom = row.get('geometry') or {}
        if not isinstance(props, dict) or not isinstance(geom, dict):
            continue
        equipment_id = props.get('objectid')
        kind = props.get('tipas')
        coordinates = geom.get('coordinates') or []
        if (not isinstance(equipment_id, int) or equipment_id < 1 or equipment_id in seen
                or kind not in {'ANAK', 'AKNAĮ'} or geom.get('type') != 'Point'
                or not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2):
            continue
        try:
            lon, lat = float(coordinates[0]), float(coordinates[1])
            start = float(props['galiojimopradzia']) / 1000
            end = props.get('galiojimopabaiga')
            end = float(end) / 1000 if end is not None else None
        except (TypeError, ValueError, KeyError):
            continue
        if (not all(math.isfinite(value) for value in (lon, lat, start))
                or (end is not None and not math.isfinite(end))
                or not (20.8 <= lon <= 26.9 and 53.8 <= lat <= 56.5)
                or not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat)
                or start > now or (end is not None and end <= now)):
            continue
        seen.add(equipment_id)
        road = str(props.get('kelionumeris') or '').strip()
        km = props.get('km')
        detail = []
        if re.fullmatch(r'A\d{1,2}', road):
            detail.append(road)
        if isinstance(km, (int, float)) and math.isfinite(km) and 0 <= km <= 500:
            detail.append(f'km {km:g}')
        detail.append('Toll-control location · operating status unverified')
        elements.append({
            'type': 'node', 'id': f'lt:via:toll:{equipment_id}', 'lat': lat, 'lon': lon,
            'title': ('Vehicle classifier and plate reader' if kind == 'AKNAĮ'
                      else 'Plate reader · toll enforcement'),
            'detail': ' · '.join(detail),
            'source': 'Via Lietuva / Eismoinfo · mapped equipment',
            'source_url': LITHUANIA_TOLL_EQUIPMENT_SOURCE,
        })
    return elements


def milan_area_b_plate_readers(payload, bbox):
    if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection' or not isinstance(payload.get('features'), list):
        raise ValueError('Milan Area B gate catalog is invalid')
    min_lon, min_lat, max_lon, max_lat = bbox
    elements = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        props = row.get('properties') or {}
        geom = row.get('geometry') or {}
        if not isinstance(props, dict) or not isinstance(geom, dict):
            continue
        gate_id = props.get('id_amat')
        coords = geom.get('coordinates')
        if (not isinstance(gate_id, int) or gate_id < 1 or gate_id in seen
                or props.get('stato') != 'ATTIVI E SANZIONANTI'
                or geom.get('type') != 'Point' or not isinstance(coords, (list, tuple))
                or len(coords) < 2):
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if (not math.isfinite(lon) or not math.isfinite(lat)
                or not (9.0 <= lon <= 9.35 and 45.35 <= lat <= 45.6)
                or not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat)):
            continue
        seen.add(gate_id)
        name = re.sub(r'^\d+\s*-\s*', '', str(props.get('nome') or '')).strip()[:80]
        elements.append({
            'type': 'node', 'id': f'it:milano:areab:{gate_id}', 'lat': lat, 'lon': lon,
            'title': f'Area B plate reader · {name}' if name else 'Area B plate reader',
            'detail': 'Entry gate listed active in the city inventory file dated 2023 · current operation unverified',
            'source': 'Comune di Milano · CC BY', 'source_url': MILAN_AREA_B_GATES_SOURCE,
        })
    return elements


def milan_area_c_plate_readers(payload, bbox):
    if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection' or not isinstance(payload.get('features'), list):
        raise ValueError('Milan Area C gate catalog is invalid')
    min_lon, min_lat, max_lon, max_lat = bbox
    elements = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        props = row.get('properties') or {}
        geom = row.get('geometry') or {}
        if not isinstance(props, dict) or not isinstance(geom, dict):
            continue
        gate_id = props.get('id_amat')
        coords = geom.get('coordinates')
        if (not isinstance(gate_id, int) or gate_id < 1 or gate_id in seen
                or geom.get('type') != 'Point' or not isinstance(coords, (list, tuple))
                or len(coords) < 2):
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if (not math.isfinite(lon) or not math.isfinite(lat)
                or not (9.0 <= lon <= 9.35 and 45.35 <= lat <= 45.6)
                or not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat)):
            continue
        seen.add(gate_id)
        name = str(props.get('label') or '').strip()[:80]
        elements.append({
            'type': 'node', 'id': f'it:milano:areac:{gate_id}', 'lat': lat, 'lon': lon,
            'title': f'Area C plate reader · {name}' if name else 'Area C plate reader',
            'detail': 'City entry-gate inventory · current operation unverified',
            'source': 'Comune di Milano · CC BY', 'source_url': MILAN_AREA_C_GATES_SOURCE,
        })
    return elements


def cached_dutch_anpr_catalog():
    def load():
        search = fetch_text_url(DUTCH_ANPR_SEARCH_URL, timeout=20)
        source_url, quarter = dutch_anpr_latest_plan(search)
        page = fetch_text_url(source_url, timeout=35)
        elements = parse_dutch_anpr_plan(page, source_url, quarter)
        return json.dumps(elements, separators=(',', ':')).encode(), 'application/json'

    content, _, _ = API_RESPONSE_CACHE.get_or_load(
        'nl-police-anpr-current-plan:v1', load, ttl=21600, stale_ttl=86400,
        persist=False, wait_timeout=60)
    return json.loads(content)


def fetch_deflock_lpr_content(bbox, limit=10000):
    min_lon, min_lat, max_lon, max_lat = bbox
    elements_by_id = {}
    lithuania_visible = (min_lon <= 26.9 and max_lon >= 20.8
                         and min_lat <= 56.5 and max_lat >= 53.8)
    dutch_visible = (min_lon <= 7.3 and max_lon >= 3.1
                     and min_lat <= 53.7 and max_lat >= 50.7)
    milan_visible = (min_lon <= 9.35 and max_lon >= 9.0
                     and min_lat <= 45.6 and max_lat >= 45.35)
    source_errors = []
    try:
        index = cached_deflock_json(DEFLOCK_INDEX_URL, 'deflock-index:v1')
        tile_requests = deflock_tiles_for_bbox(index, bbox)
    except (OSError, ValueError, KeyError, TypeError) as error:
        if not (lithuania_visible or dutch_visible or milan_visible):
            raise
        index, tile_requests = {}, []
        source_errors.append(f'DeFlock: {error}')

    def fetch_lpr_tile(request):
        tile_key, tile_url = request
        version = hashlib.sha256(tile_url.encode()).hexdigest()[:12]
        items = cached_deflock_json(tile_url, f'deflock-tile:v1:{tile_key}:{version}')
        return tile_key, items

    tile_keys = [tile_key for tile_key, _ in tile_requests]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(tile_requests) or 1)) as executor:
            tile_results = executor.map(fetch_lpr_tile, tile_requests)
            for _, items in tile_results:
                for item in items if isinstance(items, list) else []:
                    try:
                        lat = float(item.get('lat'))
                        lon = float(item.get('lon'))
                    except (TypeError, ValueError):
                        continue
                    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                        continue
                    normalized = {
                        'type': 'node',
                        'id': item.get('id'),
                        'lat': lat,
                        'lon': lon,
                        'tags': item.get('tags') or {},
                    }
                    elements_by_id[str(item.get('id') or f'{lat}:{lon}')] = normalized
    except (OSError, ValueError, KeyError, TypeError) as error:
        if not (lithuania_visible or dutch_visible or milan_visible):
            raise
        source_errors.append(f'DeFlock tiles: {error}')

    # Via Lietuva publishes official toll-control locations separately from the
    # community-mapped DeFlock catalog. Fetch this small inventory only for views
    # that intersect Lithuania, and never treat it as a live plate-read feed.
    if lithuania_visible:
        try:
            catalog = cached_deflock_json(
                LITHUANIA_TOLL_EQUIPMENT_URL, 'lt-via-toll-equipment:v1', ttl=21600)
            for item in lithuania_toll_plate_readers(catalog, bbox):
                elements_by_id[item['id']] = item
        except (OSError, ValueError, KeyError, TypeError) as error:
            source_errors.append(f'Via Lietuva: {error}')
    if dutch_visible:
        try:
            for item in dutch_anpr_for_bbox(cached_dutch_anpr_catalog(), bbox):
                elements_by_id[item['id']] = item
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
            source_errors.append(f'Dutch ANPR plan: {error}')
    if milan_visible:
        try:
            catalog = cached_deflock_json(MILAN_AREA_B_GATES_URL, 'it-milan-area-b-gates:v1', ttl=86400)
            for item in milan_area_b_plate_readers(catalog, bbox):
                elements_by_id[item['id']] = item
        except (OSError, ValueError, KeyError, TypeError) as error:
            source_errors.append(f'Milan Area B gates: {error}')
        try:
            catalog = cached_deflock_json(MILAN_AREA_C_GATES_URL, 'it-milan-area-c-gates:v1', ttl=86400)
            for item in milan_area_c_plate_readers(catalog, bbox):
                elements_by_id[item['id']] = item
        except (OSError, ValueError, KeyError, TypeError) as error:
            source_errors.append(f'Milan Area C gates: {error}')
    if source_errors:
        print(f'[lpr] {"; ".join(source_errors)}', flush=True)

    expiration = index.get('expiration_utc')
    expires_at = None
    try:
        expires_at = datetime.datetime.fromtimestamp(float(expiration), datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        pass
    elements = list(elements_by_id.values())
    total_elements = len(elements)
    if len(elements) > limit:
        step = len(elements) / limit
        elements = [elements[int(index * step)] for index in range(limit)]
    body = {
        'elements': elements,
        'source': 'DeFlock / OpenStreetMap',
        'source_url': 'https://deflock.me/',
        'tiles': tile_keys,
        'expires_at': expires_at,
        'total_elements': total_elements,
        'elements_limited': len(elements) < total_elements,
        'viewport_filtered': True,
        'sourceErrors': source_errors,
    }
    return json.dumps(body, separators=(',', ':')).encode()


def fetch_text_url(url, *, referer=None, timeout=25):
    headers = {
        'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
        'Accept': 'text/javascript,text/plain,*/*',
    }
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'replace')


def optional_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def nested_value(document, *path):
    value = document
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def fetch_deldot_road_weather():
    features = []
    for station in fetch_deldot_dataset('WeatherStations'):
        lat = optional_float(station.get('lat'))
        lon = optional_float(station.get('lon'))
        if lat is None or lon is None or not in_region(lat, lon):
            continue
        atmospheric = station.get('atmospheric') or {}
        wind = atmospheric.get('windAvg') or {}
        gust = atmospheric.get('windGust') or {}
        visibility = nested_value(atmospheric, 'visibility', 'distance') or {}
        precip = atmospheric.get('precip') or {}
        surfaces = station.get('surfaceSensors') or []
        surface = next((item for item in surfaces if isinstance(item, dict)), {})
        wind_mph = optional_float(wind.get('value'))
        gust_mph = optional_float(gust.get('value'))
        visibility_value = optional_float(visibility.get('value'))
        visibility_text = None
        if visibility_value is not None:
            visibility_text = f'{visibility_value:g} {visibility.get("uom") or ""}'.strip()
        attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': f'DELDOT_{station.get("id") or station.get("legacyId")}',
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': display_text(station.get('title') or station.get('id') or 'DelDOT road weather'),
            'OBS_TIME_LOCAL': nested_value(station, 'lastDataReceived', 'eTfmt'),
            'AIR_TEMP_F': optional_float(nested_value(atmospheric, 'airTemp', 'f', 'value')),
            'DEW_POINT_F': optional_float(nested_value(atmospheric, 'dewPoint', 'f', 'value')),
            'RELATIVE_HUMIDITY': optional_float(nested_value(atmospheric, 'relHumidity', 'value')),
            'WIND_DIRECTION': wind.get('heading') or optional_float(nested_value(wind, 'direction', 'value')),
            'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
            'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
            'VISIBILITY': visibility_text,
            'ROAD_TEMP_F': optional_float(nested_value(surface, 'temperature', 'f', 'value')),
            'SUBSURFACE_TEMP_F': optional_float(nested_value((station.get('subSurfaceSensors') or [{}])[0], 'temperature', 'f', 'value')),
            'ROAD_STATE': nested_value(surface, 'status', 'name') or 'Unknown',
            'PRECIP_1H_IN': optional_float(nested_value(precip, 'past1Hour', 'accum', 'value')),
            'PRECIP_12H_IN': optional_float(nested_value(precip, 'past12Hours', 'accum', 'value')),
            'PRECIP_24H_IN': optional_float(nested_value(precip, 'past24Hours', 'accum', 'value')),
            'SOURCE_URL': DELDOT_SOURCE_URL,
        }
        features.append({
            'attributes': attrs,
            'geometry': {'x': lon, 'y': lat},
        })
    return features


def pa511_weather_values(raw):
    values = {}
    for label, value in re.findall(
        r'<td[^>]+class=["\'][^"\']*tooltipHeaders[^"\']*["\'][^>]*>'
        r'(.*?)</td>\s*<td[^>]*>(.*?)</td>',
        raw,
        re.S | re.I,
    ):
        values[pa511_html_text(label)] = pa511_html_text(value)
    return values


def pa511_measurement_number(value):
    match = re.search(r'-?\d+(?:\.\d+)?', str(value or ''))
    return float(match.group(0)) if match else None


def fetch_pa511_road_weather():
    stations = fetch_pa511_dataset('WeatherStations')

    def normalize(station):
        location = station.get('location') or []
        if len(location) < 2:
            return None
        lat = optional_float(location[0])
        lon = optional_float(location[1])
        raw_id = str(station.get('itemId') or '').strip()
        if lat is None or lon is None or not raw_id or not in_region(lat, lon):
            return None
        raw = fetch_pa511_tooltip_html('WeatherStations', raw_id)
        values = pa511_weather_values(raw)
        name_match = re.search(r'<td[^>]*>\s*<b[^>]*>(.*?)</b>', raw, re.S | re.I)
        wind_mph = pa511_measurement_number(values.get('Wind Speed (avg)'))
        gust_mph = pa511_measurement_number(values.get('Wind Speed (gust)'))
        attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': f'PA511_{raw_id}',
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': pa511_html_text(name_match.group(1)) if name_match else f'511PA weather station {raw_id}',
            'OBS_TIME_LOCAL': values.get('Last Updated'),
            'AIR_TEMP_F': pa511_measurement_number(values.get('Air Temp')),
            'DEW_POINT_F': pa511_measurement_number(values.get('Dewpoint Temp')),
            'RELATIVE_HUMIDITY': pa511_measurement_number(values.get('Relative Humidity')),
            'WIND_DIRECTION': values.get('Wind Direction (avg)'),
            'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
            'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
            'VISIBILITY': values.get('Visibility'),
            'ROAD_TEMP_F': pa511_measurement_number(values.get('Surface Temp')),
            'SUBSURFACE_TEMP_F': None,
            'ROAD_STATE': values.get('Surface Status') or 'Unknown',
            'PRECIP_1H_IN': pa511_measurement_number(values.get('Precip One Hour')),
            'PRECIP_12H_IN': pa511_measurement_number(values.get('Precip Twelve Hours')),
            'PRECIP_24H_IN': pa511_measurement_number(values.get('Precip 24 Hours')),
            'SOURCE_URL': PA511_SOURCE_URL,
        }
        return {'attributes': attrs, 'geometry': {'x': lon, 'y': lat}}

    features = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(normalize, station) for station in stations]
        for future in concurrent.futures.as_completed(futures):
            try:
                feature = future.result()
            except Exception:
                continue
            if feature:
                features.append(feature)
    return features


def fetch_new_england_511_road_weather(state_code):
    stations = [
        station for station in fetch_new_england_511_dataset('WeatherStations')
        if len(station.get('location') or []) >= 2 and point_in_state(
            state_code,
            safe_float(station['location'][0]),
            safe_float(station['location'][1]),
        )
    ]

    def normalize(station):
        location = station.get('location') or []
        lat = optional_float(location[0])
        lon = optional_float(location[1])
        raw_id = str(station.get('itemId') or '').strip()
        if lat is None or lon is None or not raw_id.isdigit():
            return None
        raw = fetch_new_england_511_tooltip_html('WeatherStations', raw_id)
        values = pa511_weather_values(raw)
        name_match = re.search(r'<td[^>]*>\s*<b[^>]*>(.*?)</b>', raw, re.S | re.I)
        wind_mph = pa511_measurement_number(
            values.get('Wind Speed') or values.get('Wind Speed (avg)')
        )
        gust_mph = pa511_measurement_number(
            values.get('Wind Gust') or values.get('Wind Speed (gust)')
        )
        attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': f'NEWENGLAND511_{state_code}_{raw_id}',
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': (
                pa511_html_text(name_match.group(1))
                if name_match else f'{traffic_region(state_code)["name"]} road weather station {raw_id}'
            ),
            'OBS_TIME_LOCAL': values.get('Last Updated'),
            'AIR_TEMP_F': pa511_measurement_number(
                values.get('Air Temperature') or values.get('Air Temp')
            ),
            'DEW_POINT_F': pa511_measurement_number(
                values.get('Dewpoint Temperature') or values.get('Dewpoint Temp')
            ),
            'RELATIVE_HUMIDITY': pa511_measurement_number(values.get('Relative Humidity')),
            'WIND_DIRECTION': values.get('Wind Direction') or values.get('Wind Direction (avg)'),
            'WIND_SPEED_KTS': round(wind_mph / 1.15078, 1) if wind_mph is not None else None,
            'WIND_GUST_KTS': round(gust_mph / 1.15078, 1) if gust_mph is not None else None,
            'VISIBILITY': values.get('Visibility'),
            'ROAD_TEMP_F': pa511_measurement_number(
                values.get('Surface Temperature') or values.get('Surface Temp')
            ),
            'SUBSURFACE_TEMP_F': pa511_measurement_number(values.get('Subsurface Temperature')),
            'ROAD_STATE': values.get('Surface Status') or values.get('Precipitation Type') or 'Unknown',
            'PRECIP_1H_IN': pa511_measurement_number(values.get('Precip One Hour')),
            'PRECIP_12H_IN': pa511_measurement_number(values.get('Precip Twelve Hours')),
            'PRECIP_24H_IN': pa511_measurement_number(values.get('Precip 24 Hours')),
            'SOURCE_URL': NEW_ENGLAND_511_SOURCE_URL,
        }
        return {'attributes': attrs, 'geometry': {'x': lon, 'y': lat}}

    features = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(normalize, station) for station in stations]
        for future in concurrent.futures.as_completed(futures):
            try:
                feature = future.result()
            except Exception:
                continue
            if feature:
                features.append(feature)
    return features


def fetch_fdot_sensors():
    url = (
        'https://services1.arcgis.com/O1JpcwDW8sjYuddV/arcgis/rest/services'
        '/Real_Time_Traffic_Volume_and_Speed_Current_All_Directions_TDA'
        '/FeatureServer/0/query'
        '?where=LATITUDE+BETWEEN+24.4+AND+31.1+AND+LNGITUDE+BETWEEN+-87.7+AND+-79.9'
        '&outFields=LATITUDE,LNGITUDE,CURAVSPD,MAXSPEEDR,LOCALNAM,IDSTR'
        '&f=json&resultRecordCount=5000'
    )
    return fetch_json_url(url, headers={'User-Agent': 'Mozilla/5.0'}).get('features') or []


def rwis_cell_text(value):
    return re.sub(r'<[^>]+>', '', html.unescape(str(value or ''))).strip()


def fetch_gdot_rwis_content():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        stations_future = executor.submit(
            fetch_text_url,
            GDOT_RWIS_STATIONS_URL,
            referer=GDOT_RWIS_SOURCE_URL,
        )
        observations_future = executor.submit(
            fetch_text_url,
            GDOT_RWIS_OBSERVATIONS_URL,
            referer=GDOT_RWIS_SOURCE_URL,
        )
        stations_text = stations_future.result()
        observations_text = observations_future.result()

    stations_payload = re.sub(r'^\s*var\s+locations\s*=\s*', '', stations_text).strip().rstrip(';')
    observations_payload = re.sub(r'^\s*var\s+rwisobs\s*=\s*', '', observations_text).strip().rstrip(';')
    stations = ast.literal_eval(stations_payload)
    observations = json.loads(observations_payload)

    features = []
    for station_id, station in stations.items():
        try:
            lat = float(str(station.get('lat') or '').strip())
            lon = float(str(station.get('lon') or '').strip())
        except (TypeError, ValueError):
            continue
        if not in_region(lat, lon):
            continue

        raw_html = (observations.get(station_id) or {}).get('rawhtml') or ''
        latest_cells = None
        for row in re.findall(r"<tr\s+align=['\"]center['\"]>(.*?)</tr>", raw_html, re.I | re.S):
            cells = [rwis_cell_text(cell) for cell in re.findall(r'<td>(.*?)</td>', row, re.I | re.S)]
            if cells and re.match(r'^\d{2}/\d{2}\s+\d{2}:\d{2}$', cells[0]):
                latest_cells = cells
                break
        if not latest_cells or len(latest_cells) < 13:
            continue

        wind_speed = latest_cells[5]
        wind_speed_match = re.match(r'^(\d+(?:\.\d+)?)(?:G(\d+(?:\.\d+)?))?$', wind_speed)
        wind_speed_value = optional_float(wind_speed_match.group(1)) if wind_speed_match else 0 if wind_speed.upper() == 'CALM' else None
        wind_gust_value = optional_float(wind_speed_match.group(2)) if wind_speed_match and wind_speed_match.group(2) else None
        attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': station_id,
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': station.get('text') or station_id.replace('_', ' '),
            'OBS_TIME_LOCAL': latest_cells[0],
            'AIR_TEMP_F': optional_float(latest_cells[1]),
            'DEW_POINT_F': optional_float(latest_cells[2]),
            'RELATIVE_HUMIDITY': optional_float(latest_cells[3]),
            'WIND_DIRECTION': latest_cells[4] or None,
            'WIND_SPEED_KTS': wind_speed_value,
            'WIND_GUST_KTS': wind_gust_value,
            'VISIBILITY': latest_cells[6] or None,
            'ROAD_TEMP_F': optional_float(latest_cells[7]),
            'SUBSURFACE_TEMP_F': optional_float(latest_cells[8]),
            'ROAD_STATE': latest_cells[9] or 'Unknown',
            'PRECIP_1H_IN': optional_float(latest_cells[10]),
            'PRECIP_12H_IN': optional_float(latest_cells[11]),
            'PRECIP_24H_IN': optional_float(latest_cells[12]),
            'SOURCE_URL': GDOT_RWIS_SOURCE_URL,
        }
        features.append({
            'attributes': attrs,
            'geometry': {'x': lon, 'y': lat},
        })

    body = {
        'features': features,
        'source': 'GDOT Road Weather Information System / NOAA NWS',
        'source_url': GDOT_RWIS_SOURCE_URL,
        'last_updated': time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime()),
    }
    return json.dumps(body, separators=(',', ':')).encode()


def mdot_measurement(value):
    match = re.search(r'-?\d+(?:\.\d+)?', str(value or ''))
    return optional_float(match.group(0)) if match else None


def fetch_mdot_rwis_content():
    features = []
    for marker in fetch_mdot_markers('LoadRWISData'):
        station_id = mdot_marker_id(marker, 'rwis')
        lat = safe_float(marker.get('lat'))
        lon = safe_float(marker.get('lon'))
        if not station_id or not in_region(lat, lon):
            continue
        text = fetch_mdot_popup(f'mapbubbles/rwissite.aspx?rwisid={station_id}')
        values = {}
        for label, value in re.findall(
            r'<span[^>]+class=["\']rwis2["\'][^>]*>(.*?)</span>\s*<p[^>]+class=["\']rwis1["\'][^>]*>(.*?)</p>',
            text,
            re.I | re.S,
        ):
            values[strip_tags(label)] = strip_tags(value)
        update_match = re.search(r'Last\s+Update:\s*(.*?)</p>', text, re.I | re.S)
        attrs = {
            'SENSOR_TYPE': 'road_weather',
            'IDSTR': f'MDOT_{station_id}',
            'LATITUDE': lat,
            'LNGITUDE': lon,
            'LOCALNAM': strip_tags(marker.get('tooltip')) or f'MDOT RWIS {station_id}',
            'OBS_TIME_LOCAL': strip_tags(update_match.group(1)) if update_match else None,
            'AIR_TEMP_F': mdot_measurement(values.get('Air Temp')),
            'DEW_POINT_F': mdot_measurement(values.get('Dew Point')),
            'RELATIVE_HUMIDITY': mdot_measurement(values.get('Relative Humidity')),
            'WIND_DIRECTION': values.get('Avg Wind Direction'),
            'WIND_SPEED_KTS': mdot_measurement(values.get('Avg Wind Speed')),
            'ROAD_TEMP_F': mdot_measurement(values.get('Surface Temp')),
            'ROAD_STATE': values.get('Surface') or 'Unknown',
            'PRECIP_1H_IN': mdot_measurement(values.get('Precip Rate')),
            'SOURCE_URL': MDOT_TRAFFIC_SOURCE_URL,
        }
        features.append({
            'attributes': attrs,
            'geometry': {'x': lon, 'y': lat},
        })
    return {
        'features': features,
        'source': 'Mississippi DOT Road Weather Information System',
        'source_url': MDOT_TRAFFIC_SOURCE_URL,
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    # Each keep-alive connection occupies a ThreadingHTTPServer worker, even while
    # idle. Close after each response so one browser cannot consume many slots.
    protocol_version = 'HTTP/1.0'
    server_version = 'GlobeView'
    sys_version = ''
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        '.webmanifest': 'application/manifest+json',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(SERVER_IDLE_TIMEOUT)

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        if code >= 400 and not DEBUG:
            message = http.HTTPStatus(code).phrase
            explain = None
        super().send_error(code, message, explain)

    def _write_bytes(
        self,
        status,
        content,
        content_type,
        cache_control='no-store',
        allow_origin=None,
        extra_headers=None,
    ):
        if isinstance(content, str):
            content = content.encode('utf-8')
        elif isinstance(content, bytearray):
            content = bytes(content)
        compressible = (
            content_type.startswith('text/') or
            any(token in content_type for token in ('json', 'javascript', 'xml', 'svg'))
        )
        content_digest = None
        etag = None
        if status == 200 and cache_control != 'no-store':
            content_digest = hashlib.blake2b(content, digest_size=16).hexdigest()
            etag = f'W/"{content_digest}"'
            request_etags = str(self.headers.get('If-None-Match') or '')
            if request_etags.strip() == '*' or etag in request_etags:
                self.send_response(304)
                self.send_header('ETag', etag)
                if compressible:
                    self.send_header('Vary', 'Accept-Encoding')
                if allow_origin:
                    self.send_header('Access-Control-Allow-Origin', allow_origin)
                for key, value in (extra_headers or {}).items():
                    self.send_header(str(key), str(value))
                self.send_header('Cache-Control', cache_control)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        use_gzip = (
            len(content) >= 1024 and
            compressible and
            'gzip' in str(self.headers.get('Accept-Encoding') or '').lower()
        )
        if use_gzip:
            if content_digest is None:
                content_digest = hashlib.blake2b(content, digest_size=16).hexdigest()
            try:
                content, _, _ = COMPRESSED_RESPONSE_CACHE.get_or_load(
                    f'gzip:v1:{content_digest}',
                    lambda: (gzip.compress(content, compresslevel=4), 'application/gzip'),
                    ttl=86400,
                    stale_ttl=7 * 86400,
                    persist=False,
                )
            except Exception:
                content = gzip.compress(content, compresslevel=4)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        if etag:
            self.send_header('ETag', etag)
        if use_gzip:
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Vary', 'Accept-Encoding')
        if allow_origin:
            self.send_header('Access-Control-Allow-Origin', allow_origin)
        for key, value in (extra_headers or {}).items():
            self.send_header(str(key), str(value))
        self.send_header('Cache-Control', cache_control)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _log_exception(self, context, exc):
        stamp = datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'
        print(f'[{stamp}] {context}: {exc}', flush=True)

    def _write_streamed_upstream(self, response, content_type, prefix=b''):
        content_length = safe_int_param(
            response.headers.get('Content-Length'),
            minimum=0,
        )
        if content_length is not None and content_length > MAX_STREAM_MEDIA_BYTES:
            raise ValueError('Stream media is too large')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        if content_length is not None:
            self.send_header('Content-Length', str(content_length))
        else:
            self.send_header('Connection', 'close')
            self.close_connection = True
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            sent = len(prefix)
            if prefix:
                self.wfile.write(prefix)
            while True:
                chunk = response.read(min(64 * 1024, MAX_STREAM_MEDIA_BYTES - sent + 1))
                if not chunk:
                    break
                if sent + len(chunk) > MAX_STREAM_MEDIA_BYTES:
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Allow', 'GET, HEAD, OPTIONS')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_HEAD(self):
        self._dispatch_request(head_only=True)

    def do_GET(self):
        self._dispatch_request(head_only=False)

    def _dispatch_request(self, head_only=False):
        if not allowed_request_host(self.headers.get('Host')):
            self.send_error(400, 'Invalid host'); return

        if len(self.path) > 8192:
            self.send_error(414, 'Request target too long'); return
        parsed = urllib.parse.urlparse(self.path)

        client_ip = request_client_ip(
            self.client_address[0],
            self.headers.get('X-Forwarded-For'),
        )
        if not check_rate_limit(client_ip, rate_limit_bucket(parsed.path)):
            if parsed.path == '/aircraft':
                self._write_bytes(429, b'{"error":"aircraft_request_limit"}', 'application/json',
                                  cache_control='no-store', extra_headers={'Retry-After': '60'})
                return
            self.send_error(429, 'Too Many Requests'); return

        if parsed.path == '/healthz':
            self._handle_healthz(parsed)
        elif parsed.path == '/video-token':
            self._handle_video_token(parsed)
        elif parsed.path.startswith('/camera-snapshot/'):
            self._handle_camera_snapshot(parsed)
        elif parsed.path.startswith('/catalonia-camera/'):
            self._handle_catalonia_camera(parsed)
        elif parsed.path.startswith('/northern-ireland-camera/'):
            self._handle_northern_ireland_camera(parsed)
        elif parsed.path.startswith('/madrid-camera/'):
            self._handle_madrid_camera(parsed)
        elif parsed.path.startswith('/tfl-camera/'):
            self._handle_tfl_camera(parsed)
        elif parsed.path.startswith('/estonia-camera/'):
            self._handle_estonia_camera(parsed)
        elif parsed.path.startswith('/lyon-camera/'):
            self._handle_lyon_camera(parsed)
        elif parsed.path.startswith('/vitoria-camera/'):
            self._handle_vitoria_camera(parsed)
        elif parsed.path.startswith('/vigo-camera/'):
            self._handle_vigo_camera(parsed)
        elif parsed.path.startswith('/luxembourg-camera/'):
            self._handle_luxembourg_camera(parsed)
        elif parsed.path.startswith('/dgt-camera/'):
            self._handle_dgt_camera(parsed)
        elif parsed.path.startswith('/lithuania-camera/'):
            self._handle_lithuania_camera(parsed)
        elif parsed.path.startswith('/lithuania-road-event/'):
            self._handle_lithuania_road_event(parsed)
        elif parsed.path.startswith('/ireland-camera/'):
            self._handle_ireland_camera(parsed)
        elif parsed.path.startswith('/stream/'):
            self._handle_stream_proxy(parsed)
        elif parsed.path == '/tile':
            self._handle_tile(parsed)
        elif parsed.path == '/radar-tile':
            self._handle_radar_tile(parsed)
        elif parsed.path == '/naip-tile':
            self._handle_naip_tile(parsed)
        elif parsed.path.startswith('/terrain/'):
            self._handle_terrain_tile(parsed)
        elif parsed.path == '/emergency':
            self._handle_emergency(parsed)
        elif parsed.path == '/international-emergency':
            self._handle_international_emergency(parsed)
        elif parsed.path == '/international-roads':
            self._handle_international_roads(parsed)
        elif parsed.path == '/international-traffic':
            self._handle_international_traffic(parsed)
        elif parsed.path == '/international-sensor-sample':
            self._handle_international_sensor_sample(parsed)
        elif parsed.path == '/international-power':
            self._handle_international_power(parsed)
        elif parsed.path == '/power-outages':
            self._handle_power_outages(parsed)
        elif parsed.path == '/sensors':
            self._handle_sensors(parsed)
        elif parsed.path == '/temperature-stations':
            self._handle_temperature_stations(parsed)
        elif parsed.path == '/lpr':
            self._handle_lpr(parsed)
        elif parsed.path == '/aircraft':
            self._handle_aircraft(parsed)
        elif parsed.path == '/aircraft/route':
            self._handle_aircraft_route(parsed)
        elif parsed.path == '/aircraft/track':
            self._handle_aircraft_track(parsed)
        elif parsed.path == '/radio/stations':
            self._handle_radio_stations(parsed)
        elif parsed.path == '/radio/click':
            self._handle_radio_click(parsed)
        elif parsed.path == '/vessels':
            self._handle_vessels(parsed)
        elif parsed.path == '/vessels/search':
            self._handle_vessel_search(parsed)
        elif parsed.path == '/hazards':
            self._handle_hazards(parsed)
        elif parsed.path == '/cyclone-guidance':
            self._handle_cyclone_guidance(parsed)
        elif parsed.path == '/trip-route':
            self._handle_trip_route(parsed)
        elif parsed.path == '/arcgis-layer':
            self._handle_arcgis_layer(parsed)
        elif parsed.path == '/cyber':
            self._handle_cyber(parsed)
        elif parsed.path == '/registry':
            self._handle_registry(parsed)
        elif parsed.path.startswith('/511/') or parsed.path.startswith('/fl511/'):
            self._handle_511_layer(parsed)
        elif parsed.path in {'/511tooltip', '/fl511tooltip'}:
            self._handle_511_tooltip(parsed)
        else:
            self._handle_static(parsed)

    def _write_page(self, status, filename, cache_control='no-cache'):
        path = file_path(filename)
        file_stat = os.stat(path)
        app_script = 'globe.js' if filename == 'index.html' else None
        app_mtime = int(os.path.getmtime(file_path(app_script))) if app_script else 0
        css_mtime = int(os.path.getmtime(file_path('ui.css'))) if filename == 'index.html' else 0
        logo_mtime = int(os.path.getmtime(file_path('globeview-logo.svg'))) if filename == 'index.html' else 0
        loading_mark_mtime = int(os.path.getmtime(file_path('globeview-mark.svg'))) if filename == 'index.html' else 0
        cache_key = f'static:v1:{filename}:{file_stat.st_mtime_ns}:{file_stat.st_size}:{app_mtime}:{css_mtime}:{logo_mtime}:{loading_mark_mtime}'

        def load_static_file():
            with open(path, 'rb') as handle:
                content = handle.read()
            if filename == 'index.html':
                content = content.replace(
                    b'src="/globe.js"',
                    f'src="/globe.js?v={app_mtime}"'.encode(),
                )
                content = content.replace(
                    b'href="/ui.css"',
                    f'href="/ui.css?v={css_mtime}"'.encode(),
                )
                content = content.replace(
                    b'src="/globeview-logo.svg"',
                    f'src="/globeview-logo.svg?v={logo_mtime}"'.encode(),
                )
                content = content.replace(
                    b'src="/globeview-mark.svg"',
                    f'src="/globeview-mark.svg?v={loading_mark_mtime}"'.encode(),
                )
            return content, self.guess_type(filename)

        content, content_type, _ = STATIC_RESPONSE_CACHE.get_or_load(
            cache_key,
            load_static_file,
            ttl=86400,
            stale_ttl=7 * 86400,
            persist=False,
        )
        self._write_bytes(status, content, content_type, cache_control=cache_control)

    def _respond_not_found(self):
        try:
            self._write_page(404, '404.html')
        except FileNotFoundError:
            self.send_error(404, 'Not found')
        except Exception as e:
            self._log_exception('404-page', e)
            self.send_error(404, 'Not found')

    def _handle_static(self, parsed):
        target = public_static_target(parsed.path)
        if not target:
            self._respond_not_found(); return
        try:
            if target.endswith('.html'):
                cache_control = 'no-cache'
            elif target.endswith('.js'):
                version = urllib.parse.parse_qs(parsed.query).get('v', [''])[0]
                current_version = str(int(os.path.getmtime(file_path(target))))
                cache_control = ('public, max-age=31536000, immutable' if version == current_version
                                 else 'public, max-age=60')
            else:
                cache_control = 'public, max-age=86400'
            self._write_page(200, target, cache_control=cache_control)
        except FileNotFoundError:
            self._respond_not_found()
        except Exception as e:
            self._log_exception('static', e)
            self.send_error(500, str(e))

    def _handle_healthz(self, parsed):
        payload = {
            'status': 'ok',
            'service': 'globeview-proxy',
            'regions': list(REGIONS),
            'cache': {
                'api': API_RESPONSE_CACHE.snapshot(),
                'media': MEDIA_RESPONSE_CACHE.snapshot(),
                'compression': COMPRESSED_RESPONSE_CACHE.snapshot(),
                'static': STATIC_RESPONSE_CACHE.snapshot(),
            },
            'server': self.server.snapshot() if hasattr(self.server, 'snapshot') else {},
            'vessels': ais_health(),
            'time': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        }
        body = json.dumps(payload).encode()
        self._write_bytes(200, body, 'application/json', cache_control='no-store')

    def _handle_video_token(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        cam_id = params.get('id', [None])[0]
        region = traffic_region(params.get('state', ['FL'])[0])
        if not valid_numeric_id(cam_id):
            self.send_error(400, 'Missing id'); return
        if not region:
            self.send_error(400, 'Invalid state'); return
        if region.get('traffic_adapter') == 'restricted':
            self.send_error(404, 'Camera media is not available for this state'); return
        try:
            proxied_url = self._get_proxied_stream_url(cam_id, region)
            body = json.dumps({'url': proxied_url}).encode()
            self._write_bytes(200, body, 'application/json')
        except Exception as e:
            self._log_exception('video-token', e)
            self.send_error(500, str(e))

    def _load_camera_snapshot(self, state_code, site_id):
        if state_code == 'MS':
            detail = mdot_camera_detail(site_id)
            referer = MDOT_TRAFFIC_SOURCE_URL
        elif state_code == 'SC':
            detail = sc511_camera_detail(site_id)
            referer = 'https://www.511sc.org/'
        elif state_code == 'NC':
            detail = drivenc_camera_detail(site_id)
            referer = 'https://drivenc.gov/'
        elif state_code == 'TN':
            detail = tdot_camera_detail(site_id)
            referer = 'https://smartway.tn.gov/'
        elif state_code == 'KY':
            detail = goky_camera_detail(site_id)
            referer = 'https://goky.ky.gov/'
        elif state_code == 'VA':
            detail = vdot_camera_detail(site_id)
            referer = 'https://511.vdot.virginia.gov/'
        elif state_code == 'MD':
            detail = mdchart_camera_detail(site_id)
            referer = 'https://chart.maryland.gov/'
        elif state_code == 'DC':
            detail = dcgis_camera_detail(site_id)
            referer = 'https://www.trafficview.org/live_traffic/'
        elif state_code == 'PA':
            detail = pa511_camera_detail(site_id)
            referer = 'https://www.511pa.com/map'
        elif state_code == 'CT':
            detail = ctroads_camera_detail(site_id)
            referer = CTROADS_SOURCE_URL
        elif state_code == 'RI':
            detail = ridot_camera_detail(site_id)
            referer = RIDOT_SOURCE_URL
        elif state_code == 'MA':
            detail = mass511_camera_detail(site_id)
            referer = MASS511_SOURCE_URL
        elif state_code in {'NH', 'VT', 'ME'}:
            detail = new_england_511_camera_detail(state_code, site_id)
            referer = NEW_ENGLAND_511_SOURCE_URL
        elif state_code == 'NY':
            detail = ny511_camera_detail(site_id)
            referer = NY511_SOURCE_URL
        elif state_code == 'OH':
            detail = ohgo_camera_detail(site_id)
            referer = OHGO_SOURCE_URL
        elif state_code == 'IN':
            detail = indot_trafficwise_camera_detail(site_id)
            referer = INDOT_TRAFFICWISE_SOURCE_URL
        elif state_code == 'IL':
            detail = idot_il_camera_detail(site_id)
            referer = IDOT_IL_CAMERA_REFERER
        elif state_code == 'MN':
            detail = mndot_camera_detail(site_id)
            referer = MNDOT_CARS_SOURCE_URL
        elif state_code == 'IA':
            detail = iadot_camera_detail(site_id)
            referer = IADOT_CAMERA_SOURCE_URL
        elif state_code == 'MO':
            detail = modot_camera_detail(site_id)
            referer = MODOT_SOURCE_URL
        elif state_code == 'NM':
            detail = nmroads_camera_detail(site_id)
            referer = NMROADS_SOURCE_URL
        elif state_code == 'CA':
            detail = caltrans_quickmap_camera_detail(site_id)
            referer = CALTRANS_QUICKMAP_SOURCE_URL
        elif state_code == 'NV':
            detail = nvroads_camera_detail(site_id)
            referer = NVROADS_SOURCE_URL
        elif state_code == 'OR':
            detail = tripcheck_or_camera_detail(site_id)
            referer = TRIPCHECK_OR_SOURCE_URL
        elif state_code == 'WA':
            detail = wsdot_camera_detail(site_id)
            referer = WSDOT_SOURCE_URL
        elif state_code == 'CO':
            detail = cotrip_camera_detail(site_id)
            referer = COTRIP_SOURCE_URL
        elif state_code == 'MI':
            detail = midrive_camera_detail(site_id)
            referer = MIDRIVE_SOURCE_URL
        elif state_code == 'WY':
            detail = wy511_camera_detail(site_id)
            referer = WY511_SOURCE_URL
        elif state_code == 'MT':
            detail = mt511_camera_detail(site_id)
            referer = MT511_SOURCE_URL
        elif state_code == 'ND':
            detail = ndroads_camera_detail(site_id)
            referer = NDROADS_SOURCE_URL
        elif state_code == 'SD':
            detail = sd511_camera_detail(site_id)
            referer = SD511_SOURCE_URL
        elif state_code == 'HI':
            detail = goakamai_camera_detail(site_id)
            referer = f'{GOAKAMAI_SOURCE_URL}cameras/'
        elif state_code in {'NE', 'KS'}:
            detail = cars511_camera_detail(state_code, site_id)
            referer = CARS511_CONFIGS[state_code]['source_url']
        elif state_code == 'BC':
            detail = drivebc_camera_detail(site_id)
            referer = DRIVEBC_SOURCE_URL
        elif state_code == 'NT':
            detail = drivenwt_camera_detail(site_id)
            referer = DRIVENWT_SOURCE_URL
        elif (
            traffic_region(state_code) and
            traffic_region(state_code).get('traffic_adapter') in {'iteris', 'ontario511'}
        ):
            region = traffic_region(state_code)
            detail = iteris_tooltip(region, 'Cameras', site_id)
            referer = region['traffic_origin']
        else:
            raise ValueError('Invalid camera state')
        snapshot_url = detail.get('upstream_snapshot_url')
        if not snapshot_url:
            raise FileNotFoundError('Snapshot unavailable')
        if state_code == 'DC':
            separator = '&' if '?' in snapshot_url else '?'
            snapshot_url = f'{snapshot_url}{separator}force=true&time={int(time.time() * 1000)}'
        req = urllib.request.Request(snapshot_url, headers={
            'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)',
            'Referer': referer,
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()
            content_type = resp.headers.get_content_type() or 'image/jpeg'
        if content.startswith(b'\x89PNG\r\n\x1a\n'):
            content_type = 'image/png'
        elif content.startswith(b'\xff\xd8\xff'):
            content_type = 'image/jpeg'
        elif content.startswith(b'RIFF') and content[8:12] == b'WEBP':
            content_type = 'image/webp'
        if content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
            content_type = 'image/jpeg'
        return content, content_type

    def _handle_camera_snapshot(self, parsed):
        parts = parsed.path.strip('/').split('/')
        state_code = parts[1] if len(parts) == 3 else ''
        site_id = parts[2] if len(parts) == 3 else ''
        if not valid_numeric_id(site_id):
            self.send_error(400, 'Invalid camera id'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'camera-snapshot:v1:{state_code}:{site_id}',
                lambda: self._load_camera_snapshot(state_code, site_id),
                ttl=CAMERA_SNAPSHOT_CACHE_TTL,
                stale_ttl=CAMERA_SNAPSHOT_STALE_TTL,
                persist=False,
                wait_timeout=20,
            )
            self._write_bytes(
                200,
                content,
                content_type,
                cache_control='public, max-age=4, stale-while-revalidate=30',
                extra_headers={'X-GlobeView-Cache': cache_status},
            )
        except ValueError:
            self.send_error(400, 'Invalid camera state')
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except urllib.error.HTTPError as exc:
            self._log_exception('camera-snapshot', exc)
            self.send_error(502, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('camera-snapshot', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_catalonia_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/catalonia-camera/')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,24}', camera_id):
            self.send_error(400, 'Invalid camera id'); return

        def load_snapshot():
            # SCT's camera host uses obsolete TLS parameters on the production
            # host. Fetch its public image over HTTP server-side, then validate
            # and serve it to browsers from our HTTPS origin.
            url = ('http://mct.gencat.cat/mct2bo/TransitCamera?'
                   f'nom={camera_id}.gif&visualitzacio=imatge')
            request = urllib.request.Request(url, headers={
                'User-Agent': 'GlobeView/1.0 (+https://github.com/codebooker/GlobeView)'})
            with urllib.request.urlopen(request, timeout=15) as response:
                if urllib.parse.urlsplit(response.url).hostname != 'mct.gencat.cat':
                    raise ValueError('Unexpected camera redirect')
                content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError('Camera image exceeded size limit')
            if content.startswith(b'GIF87a') or content.startswith(b'GIF89a'):
                content_type = 'image/gif'
            elif content.startswith(b'\xff\xd8\xff'):
                content_type = 'image/jpeg'
            elif content.startswith(b'\x89PNG\r\n\x1a\n'):
                content_type = 'image/png'
            else:
                raise ValueError('Camera returned no supported image')
            return content, content_type

        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'catalonia-camera:v1:{camera_id}', load_snapshot,
                ttl=180, stale_ttl=300, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except Exception as exc:
            self._log_exception('catalonia-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_northern_ireland_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/northern-ireland-camera/')
        if not re.fullmatch(r'\d{1,5}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'northern-ireland-camera:v1:{camera_id}',
                lambda: northern_ireland_camera_snapshot(camera_id),
                ttl=60, stale_ttl=180, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=30, stale-while-revalidate=60',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('northern-ireland-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_madrid_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/madrid-camera/')
        if not re.fullmatch(r'\d{4,6}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'madrid-camera:v2:{camera_id}',
                lambda: madrid_camera_snapshot(camera_id),
                ttl=300, stale_ttl=600, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self.send_error(404, 'Snapshot unavailable')
            else:
                self._log_exception('madrid-camera', exc)
                self.send_error(502, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('madrid-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_tfl_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/tfl-camera/')
        if not re.fullmatch(r'\d{5}\.\d{5}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'tfl-camera:v1:{camera_id}', lambda: tfl_camera_snapshot(camera_id),
                ttl=30, stale_ttl=0, persist=False, wait_timeout=15)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=30',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('tfl-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_estonia_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/estonia-camera/')
        if not re.fullmatch(r'\d{1,6}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'estonia-camera:v1:{camera_id}',
                lambda: estonia_camera_snapshot(camera_id),
                ttl=600, stale_ttl=900, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('estonia-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_lyon_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/lyon-camera/')
        if not re.fullmatch(r'CW[A-Z0-9]{3,10}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'lyon-camera:v1:{camera_id}',
                lambda: lyon_camera_snapshot(camera_id),
                ttl=60, stale_ttl=180, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=30, stale-while-revalidate=60',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('lyon-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_vitoria_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/vitoria-camera/')
        if not re.fullmatch(r'CM\d{2}(?:_ROI_[1-4])?', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'vitoria-camera:v1:{camera_id}',
                lambda: vitoria_camera_snapshot(camera_id),
                ttl=60, stale_ttl=180, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self.send_error(404, 'Snapshot unavailable')
            else:
                self._log_exception('vitoria-camera', exc)
                self.send_error(502, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('vitoria-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_vigo_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/vigo-camera/')
        if not re.fullmatch(r'\d{1,3}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'vigo-camera:v1:{camera_id}',
                lambda: vigo_camera_snapshot(camera_id),
                ttl=60, stale_ttl=180, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('vigo-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_luxembourg_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/luxembourg-camera/')
        if not re.fullmatch(r'\d{1,8}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'luxembourg-camera:v1:{camera_id}',
                lambda: luxembourg_camera_snapshot(camera_id),
                ttl=60, stale_ttl=0, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self.send_error(404, 'Snapshot unavailable')
            else:
                self._log_exception('luxembourg-camera', exc)
                self.send_error(502, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('luxembourg-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_dgt_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/dgt-camera/')
        if not re.fullmatch(r'\d{1,7}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'dgt-camera:v1:{camera_id}',
                lambda: dgt_camera_snapshot(camera_id),
                ttl=120, stale_ttl=300, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self.send_error(404, 'Snapshot unavailable')
            else:
                self._log_exception('dgt-camera', exc)
                self.send_error(502, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('dgt-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_lithuania_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/lithuania-camera/')
        if not re.fullmatch(r'\d{1,6}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'lithuania-camera:v1:{camera_id}',
                lambda: lithuania_camera_snapshot(camera_id),
                ttl=120, stale_ttl=180, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self.send_error(404, 'Snapshot unavailable')
            else:
                self._log_exception('lithuania-camera', exc)
                self.send_error(502, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('lithuania-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_lithuania_road_event(self, parsed):
        event_id = urllib.parse.unquote(parsed.path.removeprefix('/lithuania-road-event/'))
        if not re.fullmatch(r'(MJ|OB):\d{1,7}', event_id):
            self.send_error(400, 'Invalid road event ID'); return
        try:
            content, content_type, cache_status = API_RESPONSE_CACHE.get_or_load(
                f'lithuania-road-event:v1:{event_id}',
                lambda: (json.dumps(lithuania_event_detail(event_id)).encode(), 'application/json'),
                ttl=300, stale_ttl=600, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Road event unavailable')
        except Exception as exc:
            self._log_exception('lithuania-road-event', exc)
            self.send_error(502, 'Road event unavailable')

    def _handle_ireland_camera(self, parsed):
        camera_id = parsed.path.removeprefix('/ireland-camera/')
        if not re.fullmatch(r'\d{1,5}', camera_id):
            self.send_error(400, 'Invalid camera ID'); return
        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'ireland-camera:v1:{camera_id}',
                lambda: tii_camera_snapshot(camera_id),
                ttl=180, stale_ttl=300, persist=False, wait_timeout=20)
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=60, stale-while-revalidate=120',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except FileNotFoundError:
            self.send_error(404, 'Snapshot unavailable')
        except Exception as exc:
            self._log_exception('ireland-camera', exc)
            self.send_error(502, 'Snapshot unavailable')

    def _handle_registry(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        icao = params.get('icao', [None])[0]
        if not valid_icao(icao):
            self.send_error(400, 'Missing icao'); return
        key = str(icao).strip().lower()
        now = time.time()
        with REGISTRY_CACHE_LOCK:
            cached = REGISTRY_CACHE.get(key)
        if cached and now < cached['expires_at']:
            self._write_bytes(200, cached['content'], 'application/json')
            return
        url = f'https://api.adsbdb.com/v0/aircraft/{icao}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=10)
            content = resp.read()
            with REGISTRY_CACHE_LOCK:
                _evict_if_full(REGISTRY_CACHE, REGISTRY_CACHE_MAX_SIZE)
                REGISTRY_CACHE[key] = {'content': content, 'expires_at': now + REGISTRY_CACHE_TTL}
            self._write_bytes(200, content, 'application/json')
        except urllib.error.HTTPError as e:
            if e.code in {404, 429}:
                content = json.dumps({'response': {'aircraft': None}}).encode()
                ttl = REGISTRY_CACHE_NEGATIVE_TTL if e.code == 404 else REGISTRY_CACHE_RATE_LIMIT_TTL
                with REGISTRY_CACHE_LOCK:
                    _evict_if_full(REGISTRY_CACHE, REGISTRY_CACHE_MAX_SIZE)
                    REGISTRY_CACHE[key] = {'content': content, 'expires_at': now + ttl}
                self._write_bytes(200, content, 'application/json')
                return
            self._log_exception('registry', e)
            self.send_error(502)
        except Exception as e:
            self._log_exception('registry', e)
            self.send_error(502)

    def _load_511_layer_content(self, region, layer):
        adapter = region.get('traffic_adapter')
        if adapter == 'algo':
            payload = algo_layer_payload(layer)
        elif adapter == 'mdot':
            payload = mdot_layer_payload(layer)
        elif adapter == 'sc511':
            payload = sc511_layer_payload(layer)
        elif adapter == 'drivenc':
            payload = drivenc_layer_payload(layer)
        elif adapter == 'tdot':
            payload = tdot_layer_payload(layer)
        elif adapter == 'goky':
            payload = goky_layer_payload(layer)
        elif adapter == 'vdot':
            payload = vdot_layer_payload(layer)
        elif adapter == 'wv511':
            payload = wv511_layer_payload(layer)
        elif adapter == 'mdchart':
            payload = mdchart_layer_payload(layer)
        elif adapter == 'dcgis':
            payload = dcgis_layer_payload(layer)
        elif adapter == 'deldot':
            payload = deldot_layer_payload(layer)
        elif adapter == 'pa511':
            payload = pa511_layer_payload(layer)
        elif adapter == 'njta':
            payload = njta_layer_payload(layer)
        elif adapter == 'ctroads':
            payload = ctroads_layer_payload(layer)
        elif adapter == 'ridot':
            payload = ridot_layer_payload(layer)
        elif adapter == 'mass511':
            payload = mass511_layer_payload(layer)
        elif adapter == 'newengland511':
            payload = new_england_511_layer_payload(region['code'], layer)
        elif adapter == 'ny511':
            payload = ny511_layer_payload(layer)
        elif adapter == 'ohgo':
            payload = ohgo_layer_payload(layer)
        elif adapter == 'trafficwise_in':
            payload = indot_trafficwise_layer_payload(layer)
        elif adapter == 'idot_il':
            payload = idot_il_layer_payload(layer)
        elif adapter == 'mndot_cars':
            payload = mndot_layer_payload(layer)
        elif adapter == 'iadot':
            payload = iadot_layer_payload(layer)
        elif adapter == 'modot':
            payload = modot_layer_payload(layer)
        elif adapter == 'oktraffic':
            payload = oktraffic_layer_payload(layer)
        elif adapter == 'drivetexas':
            payload = drivetexas_layer_payload(layer)
        elif adapter == 'nmroads':
            payload = nmroads_layer_payload(layer)
        elif adapter == 'caltrans_quickmap':
            payload = caltrans_quickmap_layer_payload(layer)
        elif adapter == 'nvroads':
            payload = nvroads_layer_payload(layer)
        elif adapter == 'tripcheck_or':
            payload = tripcheck_or_layer_payload(layer)
        elif adapter == 'wsdot':
            payload = wsdot_layer_payload(layer)
        elif adapter == 'cotrip':
            payload = cotrip_layer_payload(layer)
        elif adapter == 'midrive':
            payload = midrive_layer_payload(layer)
        elif adapter == 'wy511':
            payload = wy511_layer_payload(layer)
        elif adapter == 'mt511':
            payload = mt511_layer_payload(layer)
        elif adapter == 'ndroads':
            payload = ndroads_layer_payload(layer)
        elif adapter == 'sd511':
            payload = sd511_layer_payload(layer)
        elif adapter == 'cars511':
            payload = cars511_layer_payload(region['code'], layer)
        elif adapter == 'drivebc':
            payload = drivebc_layer_payload(layer)
        elif adapter == 'drivenwt':
            payload = drivenwt_layer_payload(layer)
        elif adapter == 'quebec511':
            payload = quebec_511_layer_payload(layer)
        elif adapter == 'goakamai':
            payload = goakamai_layer_payload(layer)
        elif adapter == 'ontario511':
            payload = ontario_511_layer_payload(layer)
        elif adapter in {'restricted', 'unavailable'}:
            payload = {'item2': []}
        else:
            content = fetch_iteris_layer(region, layer)
            if layer != 'Cameras':
                return content
            payload = json.loads(content)
            for item in payload.get('item2') or []:
                item_id = str(item.get('itemId') or '')
                if not valid_numeric_id(item_id):
                    continue
                expando = item.setdefault('expando', {})
                expando['snapshotUrl'] = f'/camera-snapshot/{region["code"]}/{item_id}'
                expando.setdefault('snapshotFromVideo', False)
        return json.dumps(payload, separators=(',', ':')).encode()

    def _handle_511_layer(self, parsed):
        # State adapters normalize each traffic service to the original 511 shape.
        if parsed.path.startswith('/fl511/'):
            region = REGIONS['FL']
            layer = parsed.path[len('/fl511/'):]
        else:
            parts = parsed.path.strip('/').split('/')
            if len(parts) == 3:
                region = traffic_region(parts[1])
                layer = parts[2]
            elif len(parts) == 2:
                region = REGIONS['FL']
                layer = parts[1]
            else:
                region = None
                layer = ''
        if not region:
            self.send_error(400, 'Invalid state'); return
        if not layer.isalpha():
            self.send_error(400, 'Invalid layer'); return
        adapter = region.get('traffic_adapter') or 'iteris'
        ttl = TRAFFIC_LAYER_CACHE_TTLS.get(layer, 60)
        cache_key = f'511:v4:{region["code"]}:{adapter}:{layer}'
        try:
            content, content_type, cache_status = API_RESPONSE_CACHE.get_or_load(
                cache_key,
                lambda: (self._load_511_layer_content(region, layer), 'application/json'),
                ttl=ttl,
                stale_ttl=TRAFFIC_LAYER_STALE_TTL,
                persist=True,
            )
            self._write_bytes(
                200,
                content,
                content_type,
                cache_control='public, max-age=15, stale-while-revalidate=60',
                extra_headers={'X-GlobeView-Cache': cache_status},
            )
        except Exception as e:
            self._log_exception('511-layer', e)
            self.send_error(502, str(e))

    def _load_511_tooltip_content(self, region, layer, item_id):
        adapter = region.get('traffic_adapter')
        if adapter == 'algo':
            info = algo_tooltip(layer, item_id)
        elif adapter == 'mdot':
            info = mdot_tooltip(layer, item_id)
        elif adapter == 'sc511':
            info = sc511_tooltip(layer, item_id)
        elif adapter == 'drivenc':
            info = drivenc_tooltip(layer, item_id)
        elif adapter == 'tdot':
            info = tdot_tooltip(layer, item_id)
        elif adapter == 'goky':
            info = goky_tooltip(layer, item_id)
        elif adapter == 'vdot':
            info = vdot_tooltip(layer, item_id)
        elif adapter == 'wv511':
            info = wv511_tooltip(layer, item_id)
        elif adapter == 'mdchart':
            info = mdchart_tooltip(layer, item_id)
        elif adapter == 'dcgis':
            info = dcgis_tooltip(layer, item_id)
        elif adapter == 'deldot':
            info = deldot_tooltip(layer, item_id)
        elif adapter == 'pa511':
            info = pa511_tooltip(layer, item_id)
        elif adapter == 'njta':
            info = njta_tooltip(layer, item_id)
        elif adapter == 'ctroads':
            info = ctroads_tooltip(layer, item_id)
        elif adapter == 'ridot':
            info = ridot_tooltip(layer, item_id)
        elif adapter == 'mass511':
            info = mass511_tooltip(layer, item_id)
        elif adapter == 'newengland511':
            info = new_england_511_tooltip(region['code'], layer, item_id)
        elif adapter == 'ny511':
            info = ny511_tooltip(layer, item_id)
        elif adapter == 'ohgo':
            info = ohgo_tooltip(layer, item_id)
        elif adapter == 'trafficwise_in':
            info = indot_trafficwise_tooltip(layer, item_id)
        elif adapter == 'idot_il':
            info = idot_il_tooltip(layer, item_id)
        elif adapter == 'mndot_cars':
            info = mndot_tooltip(layer, item_id)
        elif adapter == 'iadot':
            info = iadot_tooltip(layer, item_id)
        elif adapter == 'modot':
            info = modot_tooltip(layer, item_id)
        elif adapter == 'oktraffic':
            info = oktraffic_tooltip(layer, item_id)
        elif adapter == 'drivetexas':
            info = drivetexas_tooltip(layer, item_id)
        elif adapter == 'nmroads':
            info = nmroads_tooltip(layer, item_id)
        elif adapter == 'caltrans_quickmap':
            info = caltrans_quickmap_tooltip(layer, item_id)
        elif adapter == 'nvroads':
            info = nvroads_tooltip(layer, item_id)
        elif adapter == 'tripcheck_or':
            info = tripcheck_or_tooltip(layer, item_id)
        elif adapter == 'wsdot':
            info = wsdot_tooltip(layer, item_id)
        elif adapter == 'cotrip':
            info = cotrip_tooltip(layer, item_id)
        elif adapter == 'midrive':
            info = midrive_tooltip(layer, item_id)
        elif adapter == 'wy511':
            info = wy511_tooltip(layer, item_id)
        elif adapter == 'mt511':
            info = mt511_tooltip(layer, item_id)
        elif adapter == 'ndroads':
            info = ndroads_tooltip(layer, item_id)
        elif adapter == 'sd511':
            info = sd511_tooltip(layer, item_id)
        elif adapter == 'cars511':
            info = cars511_tooltip(region['code'], layer, item_id)
        elif adapter == 'drivebc':
            info = drivebc_tooltip(layer, item_id)
        elif adapter == 'drivenwt':
            info = drivenwt_tooltip(layer, item_id)
        elif adapter == 'quebec511':
            info = quebec_511_tooltip(layer, item_id)
        elif adapter == 'goakamai':
            info = goakamai_tooltip(layer, item_id)
        elif adapter == 'ontario511':
            info = ontario_511_tooltip(layer, item_id)
        else:
            info = iteris_tooltip(region, layer, item_id)
        return json.dumps(info, separators=(',', ':')).encode()

    def _handle_511_tooltip(self, parsed):
        # Parse state 511 tooltip HTML server-side so raw upstream HTML is never served.
        params = urllib.parse.parse_qs(parsed.query)
        layer = params.get('layer', [None])[0]
        item_id = params.get('id', [None])[0]
        region = traffic_region(params.get('state', ['FL'])[0])
        if not layer or not valid_traffic_item_id(item_id):
            self.send_error(400, 'Missing layer or id'); return
        if not region:
            self.send_error(400, 'Invalid state'); return
        if not layer.isalpha():
            self.send_error(400, 'Invalid layer'); return
        if region.get('traffic_adapter') in {'restricted', 'unavailable'}:
            self.send_error(404, 'Traffic item details are not available for this jurisdiction'); return
        ttl = 90 if layer == 'MessageSigns' else TRAFFIC_TOOLTIP_CACHE_TTL
        adapter = region.get('traffic_adapter') or 'iteris'
        cache_key = f'511-tooltip:v4:{region["code"]}:{adapter}:{layer}:{item_id}'
        try:
            content, content_type, cache_status = API_RESPONSE_CACHE.get_or_load(
                cache_key,
                lambda: (
                    self._load_511_tooltip_content(region, layer, item_id),
                    'application/json',
                ),
                ttl=ttl,
                stale_ttl=TRAFFIC_TOOLTIP_STALE_TTL,
                persist=True,
            )
            self._write_bytes(
                200,
                content,
                content_type,
                cache_control='public, max-age=30, stale-while-revalidate=120',
                extra_headers={'X-GlobeView-Cache': cache_status},
            )
        except Exception as e:
            self._log_exception('511-tooltip', e)
            self.send_error(502)

    def _handle_aircraft(self, parsed):
        try:
            params = urllib.parse.parse_qs(parsed.query)
            scope = params.get('scope', ['world'])[0]
            if scope == 'local':
                content = aircraft_snapshot('local', params.get('lat', [''])[0],
                                            params.get('lon', [''])[0], params.get('dist', [''])[0])
            elif scope in ('hex', 'callsign', 'registration'):
                content = aircraft_snapshot(scope, identifier=params.get('id', [''])[0])
            elif scope == 'world':
                content = aircraft_snapshot('world')
            else:
                self.send_error(400, 'Invalid aircraft scope'); return
            cache_control = ('public, max-age=15' if scope == 'local' else
                             'public, max-age=60' if scope == 'world' else 'no-store')
            self._write_bytes(200, content, 'application/json', cache_control=cache_control)
        except ValueError:
            self.send_error(400, 'Invalid aircraft viewport')
        except AircraftRateLimited as error:
            self._write_bytes(429, b'{"error":"aircraft_provider_busy"}', 'application/json',
                              cache_control='no-store', extra_headers={'Retry-After': str(error.retry_after)})
        except Exception as e:
            self._log_exception('aircraft', e)
            self.send_error(502, 'Aircraft feed unavailable')

    def _handle_aircraft_track(self, parsed):
        try:
            params = urllib.parse.parse_qs(parsed.query)
            content = aircraft_track(params.get('id', [''])[0])
            self._write_bytes(200, content, 'application/json', cache_control='no-store')
        except ValueError:
            self.send_error(400, 'Invalid aircraft identifier')
        except AircraftRateLimited as error:
            self._write_bytes(429, b'{"error":"aircraft_track_provider_busy"}', 'application/json',
                              cache_control='no-store', extra_headers={'Retry-After': str(error.retry_after)})
        except Exception as error:
            self._log_exception('aircraft-track', error)
            self.send_error(502, 'Aircraft track unavailable')

    def _handle_aircraft_route(self, parsed):
        try:
            params = urllib.parse.parse_qs(parsed.query)
            result = aircraft_route(params.get('callsign', [''])[0],
                                    params.get('lat', [''])[0], params.get('lon', [''])[0])
            self._write_bytes(200, json.dumps(result, separators=(',', ':')).encode(),
                              'application/json', cache_control='no-store')
        except ValueError:
            self.send_error(400, 'Invalid aircraft route lookup')
        except Exception as error:
            self._log_exception('aircraft route', error)
            self.send_error(502, 'Aircraft route unavailable')

    def _handle_radio_stations(self, parsed):
        try:
            content = json.dumps(radio_catalog_snapshot(), separators=(',', ':'), ensure_ascii=False).encode()
            self._write_bytes(200, content, 'application/json', cache_control='public, max-age=600')
        except Exception as error:
            self._log_exception('radio catalog', error)
            self.send_error(502, 'Radio directory unavailable')

    def _handle_radio_click(self, parsed):
        station_id = urllib.parse.parse_qs(parsed.query).get('id', [''])[0]
        try:
            ok = record_station_click(station_id)
            self._write_bytes(200 if ok else 502, json.dumps({'ok': ok}).encode(), 'application/json', cache_control='no-store')
        except ValueError:
            self.send_error(400, 'Invalid station ID')

    def _handle_vessels(self, parsed):
        try:
            params = urllib.parse.parse_qs(parsed.query)
            parts = params.get('bbox', [''])[0].split(',')
            if len(parts) != 4:
                raise ValueError('Missing vessel viewport')
            content = json.dumps(vessel_snapshot(*parts, client_id=params.get('client', [''])[0]), separators=(',', ':')).encode()
            self._write_bytes(200, content, 'application/json', cache_control='no-store')
        except ValueError:
            self.send_error(400, 'Zoom closer to view vessel positions')
        except Exception as e:
            self._log_exception('vessels', e)
            self.send_error(502, 'Vessel feed unavailable')

    def _handle_vessel_search(self, parsed):
        try:
            query = urllib.parse.parse_qs(parsed.query).get('q', [''])[0]
            content = json.dumps(vessel_search(query), separators=(',', ':')).encode()
            self._write_bytes(200, content, 'application/json', cache_control='no-store')
        except ValueError as error:
            self.send_error(400, str(error))
        except Exception as error:
            self._log_exception('vessel-search', error)
            self.send_error(502, 'Vessel search unavailable')

    def _handle_hazards(self, parsed):
        try:
            layer = urllib.parse.parse_qs(parsed.query).get('layer', [''])[0]
            content = hazard_snapshot(layer)
            self._write_bytes(200, content, 'application/json', cache_control='public, max-age=30')
        except ValueError:
            self.send_error(400, 'Invalid hazard layer')
        except Exception as e:
            self._log_exception('hazards', e)
            self.send_error(502, 'Hazard feed unavailable')

    def _handle_cyclone_guidance(self, parsed):
        event_id = urllib.parse.parse_qs(parsed.query).get('id', [''])[0]
        try:
            content = json.dumps(cyclone_guidance_snapshot(event_id), separators=(',', ':')).encode()
            self._write_bytes(200, content, 'application/json', cache_control='public, max-age=300')
        except ValueError:
            self.send_error(400, 'Unknown cyclone')
        except Exception as error:
            self._log_exception('cyclone guidance', error)
            self.send_error(502, 'Cyclone guidance unavailable')

    def _handle_arcgis_layer(self, parsed):
        try:
            params = urllib.parse.parse_qs(parsed.query)
            layer = params.get('layer', [''])[0]
            bbox = params.get('bbox', [''])[0]
            # arcgis_viewport validates the whitelist and bounding box before any upstream request.
            from arcgis_catalog import LAYERS, parse_bbox
            if layer not in LAYERS:
                raise ValueError('Unknown ArcGIS layer')
            normalized = 'world' if LAYERS[layer].get('global') else parse_bbox(bbox)
            key = f'arcgis:v1:{layer}:{normalized}'
            content, _, _ = API_RESPONSE_CACHE.get_or_load(
                key,
                lambda: (json.dumps(arcgis_viewport(layer, bbox), separators=(',', ':')).encode(), 'application/json'),
                ttl=3600, stale_ttl=86400, wait_timeout=30,
            )
            self._write_bytes(200, content, 'application/json', cache_control='public, max-age=300')
        except ValueError:
            self._write_bytes(400, b'{"error":"invalid_arcgis_request"}', 'application/json', cache_control='no-store')
        except Exception as error:
            self._log_exception('arcgis layer', error)
            self._write_bytes(502, b'{"error":"arcgis_unavailable"}', 'application/json', cache_control='no-store')

    def _handle_trip_route(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        try:
            origin = parse_route_point(params.get('from', [''])[0])
            destination = parse_route_point(params.get('to', [''])[0])
            mode = params.get('mode', ['driving'])[0]
            if mode not in {'driving', 'walking', 'cycling'} or origin == destination:
                raise ValueError('Invalid trip request')
            start = f'{origin[0]},{origin[1]}'
            end = f'{destination[0]},{destination[1]}'
            key = f'trip-route:v2:{mode}:{start}:{end}'
            content, _, _ = API_RESPONSE_CACHE.get_or_load(
                key,
                lambda: (json.dumps(route_snapshot(start, end, mode), separators=(',', ':')).encode(), 'application/json'),
                ttl=900, stale_ttl=3600, persist=False, wait_timeout=30,
            )
            self._write_bytes(200, content, 'application/json', cache_control='no-store')
        except ValueError:
            self._write_bytes(400, b'{"error":"invalid_trip"}', 'application/json', cache_control='no-store')
        except RouteNotFound:
            self._write_bytes(404, b'{"error":"no_route"}', 'application/json', cache_control='no-store')
        except RouteTooLong:
            self._write_bytes(422, b'{"error":"route_too_long"}', 'application/json', cache_control='no-store')
        except RouteBusy:
            self._write_bytes(429, b'{"error":"routing_busy"}', 'application/json', cache_control='no-store',
                              extra_headers={'Retry-After': '2'})
        except RouteUnavailable as error:
            self._log_exception('trip-route', error)
            self._write_bytes(502, b'{"error":"routing_unavailable"}', 'application/json', cache_control='no-store')

    def _handle_cyber(self, parsed):
        try:
            feed = urllib.parse.parse_qs(parsed.query).get('feed', [''])[0]
            if feed not in ('scans', 'kev', 'outbreaks', 'attacks'):
                raise ValueError('Invalid cyber feed')
            live = feed == 'attacks'
            content, _, _ = API_RESPONSE_CACHE.get_or_load(
                f'cyber:v2:{feed}', lambda: (cyber_snapshot(feed), 'application/json'),
                ttl=60 if live else 3600, stale_ttl=120 if live else 86400,
                wait_timeout=20 if live else 120,
            )
            self._write_bytes(200, content, 'application/json',
                              cache_control='public, max-age=30' if live else 'public, max-age=300')
        except ValueError:
            self.send_error(400, 'Invalid cyber feed')
        except Exception as e:
            self._log_exception('cyber', e)
            self.send_error(502, 'Cyber feed unavailable')

    def _handle_lpr(self, parsed):
        try:
            params = urllib.parse.parse_qs(parsed.query)
            bbox_value = params.get('bbox', [None])[0]
            bbox = safe_bbox_param(bbox_value) if bbox_value else None
            if not bbox:
                self.send_error(400, 'A valid bbox is required'); return
            limit_value = params.get('limit', [None])[0]
            limit = 10000
            if limit_value is not None:
                try:
                    limit = max(100, min(10000, int(limit_value)))
                except (TypeError, ValueError):
                    self.send_error(400, 'Invalid limit'); return
            content = fetch_deflock_lpr_content(bbox, limit)
            self._write_bytes(200, content, 'application/json')
        except Exception as e:
            self._log_exception('lpr', e)
            self.send_error(502, str(e))

    def _handle_sensors(self, parsed):
        try:
            now = time.time()
            with SENSOR_CACHE_LOCK:
                cached_body = SENSOR_CACHE['body']
                expires_at = SENSOR_CACHE['expires_at']
                if cached_body and now < expires_at:
                    content = cached_body
                else:
                    try:
                        sensor_sources = [
                            (
                                'FDOT Real-Time Traffic Volume and Speed',
                                fetch_fdot_sensors,
                            ),
                            (
                                'GDOT Road Weather Information System / NOAA NWS',
                                lambda: json.loads(fetch_gdot_rwis_content()).get('features') or [],
                            ),
                            (
                                'Mississippi DOT Road Weather Information System',
                                lambda: (fetch_mdot_rwis_content().get('features') or []),
                            ),
                            (
                                'Delaware DOT Road Weather Information System',
                                fetch_deldot_road_weather,
                            ),
                            (
                                'Pennsylvania DOT 511PA Road Weather Information System',
                                fetch_pa511_road_weather,
                            ),
                            (
                                'New Hampshire DOT New England 511 Road Weather Information System',
                                lambda: fetch_new_england_511_road_weather('NH'),
                            ),
                            (
                                'Vermont Agency of Transportation New England 511 Road Weather Information System',
                                lambda: fetch_new_england_511_road_weather('VT'),
                            ),
                            (
                                'Maine DOT New England 511 Road Weather Information System',
                                lambda: fetch_new_england_511_road_weather('ME'),
                            ),
                            (
                                'Ohio DOT OHGO Road Weather Information System',
                                fetch_ohgo_road_weather,
                            ),
                            (
                                'Indiana DOT TrafficWise Road Weather Information System',
                                fetch_indot_trafficwise_road_weather,
                            ),
                            (
                                'Illinois DOT Road Weather Information System',
                                fetch_idot_il_road_weather,
                            ),
                            (
                                'Wisconsin Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_wisconsin_rwis,
                            ),
                            (
                                'Minnesota DOT 511 Road Weather Information System',
                                fetch_mndot_road_weather,
                            ),
                            (
                                'Iowa DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_iowa_rwis,
                            ),
                            (
                                'Oklahoma DOT OKTraffic Road Weather Information System',
                                fetch_oktraffic_road_weather,
                            ),
                            (
                                'New Mexico DOT NMRoads Road Weather Information System',
                                fetch_nmroads_road_weather,
                            ),
                            (
                                'Arizona DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_arizona_rwis,
                            ),
                            (
                                'California DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_california_rwis,
                            ),
                            (
                                'Nevada DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_nevada_rwis,
                            ),
                            (
                                'Oregon DOT TripCheck Road Weather Information System',
                                fetch_tripcheck_or_road_weather,
                            ),
                            (
                                'Washington State DOT Road Weather Information System',
                                fetch_wsdot_road_weather,
                            ),
                            (
                                'Idaho Transportation Department Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_idaho_rwis,
                            ),
                            (
                                'Utah DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_utah_rwis,
                            ),
                            (
                                'Colorado DOT COtrip Road Weather Information System',
                                fetch_cotrip_road_weather,
                            ),
                            (
                                'Wyoming DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_wyoming_rwis,
                            ),
                            (
                                'Montana DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_montana_rwis,
                            ),
                            (
                                'North Dakota Roads Environmental Sensor Sites',
                                fetch_ndroads_road_weather,
                            ),
                            (
                                'South Dakota 511 Road Weather Information System',
                                fetch_sd511_road_weather,
                            ),
                            (
                                'Nebraska DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_nebraska_rwis,
                            ),
                            (
                                'Kansas DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_kansas_rwis,
                            ),
                            (
                                'Arkansas DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_arkansas_rwis,
                            ),
                            (
                                'Connecticut DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_connecticut_rwis,
                            ),
                            (
                                'Massachusetts DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_massachusetts_rwis,
                            ),
                            (
                                'Maryland DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_maryland_rwis,
                            ),
                            (
                                'New York State DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_new_york_rwis,
                            ),
                            (
                                'South Carolina DOT Road Weather Information System via Iowa Environmental Mesonet',
                                fetch_iem_south_carolina_rwis,
                            ),
                        ]
                        source_features = []
                        successful_sources = []
                        source_errors = []
                        with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(SOURCE_FETCH_WORKERS, len(sensor_sources))
                        ) as executor:
                            futures = {
                                executor.submit(loader): label
                                for label, loader in sensor_sources
                            }
                            for future in concurrent.futures.as_completed(futures):
                                label = futures[future]
                                try:
                                    source_features.extend(future.result())
                                    successful_sources.append(label)
                                except Exception as source_error:
                                    source_errors.append(f'{label}: {source_error}')
                        if not source_features and source_errors:
                            raise ValueError('; '.join(source_errors))
                        body = {
                            'features': source_features,
                            'sources': successful_sources,
                            'source_errors': source_errors,
                            'last_updated': time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime()),
                        }
                        content = json.dumps(body, separators=(',', ':')).encode()
                        SENSOR_CACHE['body'] = content
                        SENSOR_CACHE['expires_at'] = time.time() + SENSOR_CACHE_TTL
                        SENSOR_CACHE['last_error'] = None
                    except Exception as exc:
                        SENSOR_CACHE['last_error'] = str(exc)
                        if cached_body:
                            content = cached_body
                        else:
                            raise
            self._write_bytes(200, content, 'application/json')
        except Exception as e:
            self._log_exception('sensors', e)
            self.send_error(502, str(e))

    def _handle_emergency(self, parsed):
        try:
            now = time.time()
            with EMERGENCY_CACHE_LOCK:
                cached_body = EMERGENCY_CACHE['body']
                expires_at = EMERGENCY_CACHE['expires_at']

            if cached_body and now < expires_at:
                content = cached_body
            elif cached_body:
                refresh_emergency_cache_async()
                content = cached_body
            else:
                content = refresh_emergency_cache_sync()

            self._write_bytes(200, content, 'application/json')
        except Exception as e:
            self._log_exception('emergency', e)
            self.send_error(502, str(e))

    def _handle_international_emergency(self, parsed):
        try:
            content = json.dumps(international_emergency_snapshot()).encode()
            self._write_bytes(200, content, 'application/json', cache_control='public, max-age=30')
        except Exception as e:
            self._log_exception('international-emergency', e)
            self.send_error(502, str(e))

    def _handle_international_roads(self, parsed):
        try:
            params = urllib.parse.parse_qs(parsed.query)
            layer = params.get('layer', [''])[0]
            bbox = params.get('bbox', [None])[0]
            if bbox is not None:
                bbox = tuple(float(value) for value in bbox.split(','))
                if len(bbox) != 4:
                    raise ValueError('Invalid road bounds')
            self._write_bytes(200, json.dumps(international_road_snapshot(layer, bbox)).encode(), 'application/json')
        except ValueError:
            self.send_error(400, 'Invalid road layer')
        except Exception as error:
            self._log_exception('international-roads', error)
            self.send_error(502, 'International road feed unavailable')

    def _handle_international_traffic(self, parsed):
        try:
            self._write_bytes(200, json.dumps(international_traffic_snapshot()).encode(),
                              'application/json', cache_control='public, max-age=60')
        except Exception as error:
            self._log_exception('international-traffic', error)
            self.send_error(502, 'International traffic feed unavailable')

    def _handle_international_sensor_sample(self, parsed):
        collector_id = urllib.parse.parse_qs(parsed.query).get('id', [''])[0]
        if not re.fullmatch(r'M\d{4}', collector_id):
            self.send_error(400, 'Invalid sensor ID')
            return
        try:
            body, _, _ = API_RESPONSE_CACHE.get_or_load(
                f'ch-zurich-sensor:v1:{collector_id}',
                lambda: (json.dumps(zurich_sensor_sample(collector_id)).encode(), 'application/json'),
                ttl=30, stale_ttl=60, persist=False, wait_timeout=10)
            self._write_bytes(200, body, 'application/json', cache_control='no-store')
        except Exception as error:
            self._log_exception('international-sensor-sample', error)
            self.send_error(503, 'Recent sensor reading unavailable')

    def _handle_international_power(self, parsed):
        try:
            self._write_bytes(200, json.dumps(international_power_snapshot()).encode(), 'application/json')
        except Exception as error:
            self._log_exception('international-power', error)
            self.send_error(502, 'International power feed unavailable')

    def _handle_temperature_stations(self, parsed):
        try:
            now = time.time()
            with TEMPERATURE_CACHE_LOCK:
                cached_body = TEMPERATURE_CACHE['body']
                expires_at = TEMPERATURE_CACHE['expires_at']
                refreshing = TEMPERATURE_CACHE['refreshing']

            if cached_body and now < expires_at:
                self._write_bytes(200, cached_body, 'application/json')
                return

            if cached_body and refreshing:
                self._write_bytes(200, cached_body, 'application/json')
                return

            with TEMPERATURE_CACHE_LOCK:
                if TEMPERATURE_CACHE['refreshing']:
                    body = TEMPERATURE_CACHE['body']
                    if body:
                        self._write_bytes(200, body, 'application/json')
                        return
                TEMPERATURE_CACHE['refreshing'] = True

            try:
                # Re-shape into a leaner GeoJSON FeatureCollection
                features = []
                source_errors = []

                def fetch_region_temperature_stations(region):
                    url = (
                        'https://mesonet.agron.iastate.edu/api/1/currents.geojson'
                        f'?network={urllib.parse.quote(region["weather_network"])}'
                    )
                    req = urllib.request.Request(url, headers={
                        'User-Agent': 'GlobeView/1.0',
                        'Accept': 'application/json',
                    })
                    resp = urllib.request.urlopen(req, timeout=15)
                    raw = resp.read()
                    if raw[:2] == b'\x1f\x8b':
                        import gzip
                        raw = gzip.decompress(raw)
                    iem_data = json.loads(raw)
                    region_features = []
                    for feat in iem_data.get('features', []):
                        props = feat.get('properties', {})
                        geom = feat.get('geometry', {})
                        coords = geom.get('coordinates')
                        if not coords or len(coords) < 2:
                            continue
                        tmpf = props.get('tmpf')
                        if tmpf is None:
                            continue
                        try:
                            tmpf = float(tmpf)
                        except (TypeError, ValueError):
                            continue
                        dwpf = props.get('dwpf')
                        relh = props.get('relh')
                        sknt = props.get('sknt')
                        drct = props.get('drct')
                        region_features.append({
                            'type': 'Feature',
                            'geometry': {'type': 'Point', 'coordinates': [coords[0], coords[1]]},
                            'properties': {
                                'station': props.get('station', ''),
                                'name': props.get('name', ''),
                                'tmpf': round(tmpf, 1),
                                'dwpf': round(float(dwpf), 1) if dwpf is not None else None,
                                'relh': round(float(relh), 1) if relh is not None else None,
                                'sknt': round(float(sknt), 1) if sknt is not None else None,
                                'drct': int(drct) if drct is not None else None,
                                'utc_valid': props.get('utc_valid'),
                                'state': region['code'],
                            }
                        })
                    return region_features

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(SOURCE_FETCH_WORKERS, 8)
                ) as executor:
                    futures = {
                        executor.submit(fetch_region_temperature_stations, region): region['code']
                        for region in REGIONS.values()
                    }
                    for future in concurrent.futures.as_completed(futures):
                        state_code = futures[future]
                        try:
                            features.extend(future.result())
                        except Exception as source_error:
                            source_errors.append(f'{state_code}: {source_error}')
                if not features and source_errors:
                    raise ValueError('; '.join(source_errors))

                out = json.dumps({
                    'type': 'FeatureCollection',
                    'features': features,
                    'source_errors': source_errors,
                }).encode()
                new_expires = now + TEMPERATURE_CACHE_TTL
                with TEMPERATURE_CACHE_LOCK:
                    TEMPERATURE_CACHE['body'] = out
                    TEMPERATURE_CACHE['expires_at'] = new_expires
                    TEMPERATURE_CACHE['refreshing'] = False
                    TEMPERATURE_CACHE['last_error'] = None
                self._write_bytes(200, out, 'application/json')
            except Exception as inner_e:
                with TEMPERATURE_CACHE_LOCK:
                    TEMPERATURE_CACHE['refreshing'] = False
                    TEMPERATURE_CACHE['last_error'] = str(inner_e)
                raise
        except Exception as e:
            self._log_exception('temperature-stations', e)
            self.send_error(502, str(e))

    def _handle_power_outages(self, parsed):
        try:
            now = time.time()
            with POWER_OUTAGE_CACHE_LOCK:
                cached_body = POWER_OUTAGE_CACHE['body']
                expires_at = POWER_OUTAGE_CACHE['expires_at']
                refreshing = POWER_OUTAGE_CACHE['refreshing']

            if cached_body and now < expires_at:
                self._write_bytes(200, cached_body, 'application/json')
                return

            if cached_body and refreshing:
                self._write_bytes(200, cached_body, 'application/json')
                return

            with POWER_OUTAGE_CACHE_LOCK:
                if POWER_OUTAGE_CACHE['refreshing']:
                    stale = POWER_OUTAGE_CACHE['body']
                    if stale:
                        self._write_bytes(200, stale, 'application/json')
                        return
                POWER_OUTAGE_CACHE['refreshing'] = True

            try:
                features = []
                providers = []
                errors = []

                # Utility endpoints are independent and many take several seconds
                # when their service area has a large event. Fetching them in
                # sequence made a nationwide cold load wait for the sum of every
                # provider latency. Keep a bounded pool so one slow utility does
                # not hold all other outage data off the map.
                source_fetchers = [
                    ('duke', fetch_duke_power_outages, ()),
                    ('duke_nc', fetch_duke_north_carolina_outages, ()),
                    ('duke_oh', fetch_duke_ohio_outages, ()),
                    ('duke_in', fetch_duke_indiana_outages, ()),
                    ('aes_ohio', fetch_aes_ohio_power_outages, ()),
                    ('aes_indiana', fetch_aes_indiana_power_outages, ()),
                    ('nipsco_in', fetch_nipsco_power_outages, ()),
                    ('we_energies_wi', fetch_we_energies_wisconsin_outages, ()),
                    ('wps_wi', fetch_wps_wisconsin_outages, ()),
                    ('mge_wi', fetch_mge_wisconsin_outages, ()),
                    ('xcel_mn', fetch_xcel_minnesota_outages, ()),
                    ('xcel_co', fetch_xcel_colorado_outages, ()),
                    ('minnesota_power', fetch_minnesota_power_outages, ()),
                    ('midamerican_ia', fetch_midamerican_iowa_outages, ()),
                    ('iowarec', fetch_iowarec_outages, ()),
                    ('les_ne', fetch_les_nebraska_outages, ()),
                    ('otter_tail', fetch_otter_tail_power_outages, ()),
                    ('mdu', fetch_mdu_power_outages, ()),
                    ('teco', fetch_teco_power_outages, ()),
                    ('keys', fetch_keys_power_outages, ()),
                    ('entergy_ms', fetch_entergy_mississippi_outages, ()),
                    ('entergy_ar', fetch_entergy_arkansas_outages, ()),
                    ('entergy_la', fetch_entergy_louisiana_outages, ()),
                    ('entergy_tx', fetch_entergy_texas_outages, ()),
                    ('centerpoint_tx', fetch_centerpoint_texas_outages, ()),
                    ('cleco_la', fetch_cleco_louisiana_outages, ()),
                    ('nes', fetch_nes_outages, ()),
                    ('kub_tn', fetch_kub_tennessee_outages, ()),
                    ('mdem', fetch_mdem_power_outages, ()),
                    ('pema_pa', fetch_pema_pennsylvania_power_outages, ()),
                    ('pepco_dc', fetch_pepco_dc_power_outages, ()),
                    ('delmarva_de', fetch_delmarva_delaware_power_outages, ()),
                    ('eversource_ct', fetch_eversource_connecticut_power_outages, ()),
                    ('eversource_ma', fetch_eversource_massachusetts_power_outages, ()),
                    ('eversource_nh', fetch_eversource_new_hampshire_power_outages, ()),
                    ('nhec', fetch_nhec_power_outages, ()),
                    ('vtoutages', fetch_vtoutages_power_outages, ()),
                    ('rie', fetch_rhode_island_energy_power_outages, ()),
                    ('coned_ny', fetch_coned_new_york_power_outages, ()),
                    ('orange_rockland_ny', fetch_orange_rockland_new_york_power_outages, ()),
                    ('aps_az', fetch_aps_arizona_outages, ()),
                    ('pge_ca', fetch_pge_california_outages, ()),
                    ('nvenergy', fetch_nvenergy_nevada_outages, ()),
                    ('pacific_power_or', fetch_pacific_power_oregon_outages, ()),
                    ('pacific_power_wa', fetch_pacific_power_washington_outages, ()),
                    ('rocky_mountain_power_id', fetch_rocky_mountain_power_idaho_outages, ()),
                    ('rocky_mountain_power_ut', fetch_rocky_mountain_power_utah_outages, ()),
                    ('rocky_mountain_power_wy', fetch_rocky_mountain_power_wyoming_outages, ()),
                    ('northwestern_mt', fetch_northwestern_montana_outages, ()),
                    ('northwestern_sd', fetch_northwestern_south_dakota_outages, ()),
                ]
                source_fetchers.extend(
                    (provider_key, fetch_kubra_power_outages, (provider_key, provider))
                    for provider_key, provider in KUBRA_POWER_PROVIDERS.items()
                )

                source_results = {}
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(SOURCE_FETCH_WORKERS, len(source_fetchers))
                ) as executor:
                    future_map = {
                        executor.submit(loader, *args): source_name
                        for source_name, loader, args in source_fetchers
                    }
                    for future in concurrent.futures.as_completed(future_map):
                        source_name = future_map[future]
                        try:
                            source_results[source_name] = future.result()
                        except Exception as e:
                            errors.append(f'{source_name}: {e}')

                # Preserve the configured order so the provider status panel is
                # stable even though the network work finishes out of order.
                for source_name, _, _ in source_fetchers:
                    result = source_results.get(source_name)
                    if not result:
                        continue
                    provider_features, provider_summary = result
                    features.extend(provider_features)
                    providers.append(provider_summary)

                if not features and not providers and errors:
                    raise ValueError('; '.join(errors))

                body = {
                    'type': 'FeatureCollection',
                    'features': features,
                    'providers': providers,
                    'source_errors': errors,
                    'last_updated': time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime()),
                }
                content = json.dumps(body).encode()
                with POWER_OUTAGE_CACHE_LOCK:
                    POWER_OUTAGE_CACHE['body'] = content
                    POWER_OUTAGE_CACHE['expires_at'] = time.time() + POWER_OUTAGE_CACHE_TTL
                    POWER_OUTAGE_CACHE['last_error'] = None
                    POWER_OUTAGE_CACHE['refreshing'] = False
                self._write_bytes(200, content, 'application/json')
            except Exception as e:
                with POWER_OUTAGE_CACHE_LOCK:
                    POWER_OUTAGE_CACHE['refreshing'] = False
                    POWER_OUTAGE_CACHE['last_error'] = str(e)
                raise
        except Exception as e:
            self._log_exception('power-outages', e)
            self.send_error(502)

    def _handle_terrain_tile(self, parsed):
        match = re.fullmatch(r'/terrain/(\d{1,2})/(\d{1,8})/(\d{1,8})\.webp', parsed.path)
        if not match:
            self.send_error(404); return
        z, x, y = map(int, match.groups())
        if z > 12 or x >= (1 << z) or y >= (1 << z):
            self.send_error(404); return

        def load_terrain_tile():
            url = f'https://tiles.mapterhorn.com/{z}/{x}/{y}.webp'
            request = urllib.request.Request(url, headers={'User-Agent': 'GlobalMap/1.0', 'Accept': 'image/webp'})
            with urllib.request.urlopen(request, timeout=12) as response:
                if response.headers.get_content_type() != 'image/webp':
                    raise ValueError('Unexpected terrain tile content type')
                content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError('Terrain tile is too large')
            return content, 'image/webp'

        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'terrain-tile:v1:{z}:{x}:{y}', load_terrain_tile,
                ttl=86400, stale_ttl=604800, persist=False, wait_timeout=15,
            )
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=86400',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except urllib.error.HTTPError as error:
            self.send_error(404 if error.code == 404 else 502)
        except Exception as error:
            self._log_exception('terrain-tile', error)
            self.send_error(502)

    def _handle_radar_tile(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        x = safe_int_param(params.get('x', [None])[0], minimum=0)
        y = safe_int_param(params.get('y', [None])[0], minimum=0)
        z = safe_int_param(params.get('z', [None])[0], minimum=0, maximum=22)
        size = safe_int_param(params.get('size', ['256'])[0], minimum=256, maximum=1024)
        if x is None or y is None or z is None or size not in (256, 512, 1024):
            self.send_error(400, 'Missing or invalid x/y/z/size'); return
        max_coordinate = (1 << z) - 1
        if x > max_coordinate or y > max_coordinate:
            self.send_error(400, 'Invalid tile coordinate'); return

        world_half = 20037508.342789244
        tile_span = (world_half * 2.0) / (1 << z)
        min_x = -world_half + x * tile_span
        max_x = min_x + tile_span
        max_y = world_half - y * tile_span
        min_y = max_y - tile_span
        upstream_params = urllib.parse.urlencode({
            'bbox': f'{min_x},{min_y},{max_x},{max_y}',
            'bboxSR': '3857',
            'imageSR': '3857',
            'size': f'{size},{size}',
            'format': 'png32',
            # NOAA's ImageServer defaults to bilinear resampling, which makes
            # native radar pixels look smeared at street-level zooms.
            'interpolation': 'RSP_NearestNeighbor',
            'f': 'image',
            '_': str(int(time.time() // RADAR_TILE_CACHE_TTL)),
        })
        url = (
            'https://mapservices.weather.noaa.gov/eventdriven/rest/services/radar/'
            f'radar_base_reflectivity_time/ImageServer/exportImage?{upstream_params}'
        )

        def load_radar_tile():
            req = urllib.request.Request(url, headers={
                'User-Agent': 'GlobeView/1.0',
                'Accept': 'image/png,image/*;q=0.8',
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                content = resp.read()
                content_type = resp.headers.get('Content-Type', 'image/png')
            if not content_type.startswith('image/'):
                raise ValueError(f'Unexpected radar tile content type: {content_type}')
            return content, content_type

        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'radar-tile:v2:{z}:{x}:{y}:{size}',
                load_radar_tile,
                ttl=RADAR_TILE_CACHE_TTL,
                stale_ttl=RADAR_TILE_STALE_TTL,
                persist=False,
                wait_timeout=15,
            )
            self._write_bytes(
                200,
                content,
                content_type,
                cache_control='public, max-age=60, stale-while-revalidate=300',
                extra_headers={'X-GlobeView-Cache': cache_status},
            )
        except Exception as e:
            self._log_exception('radar-tile', e)
            self.send_error(502, str(e))

    def _handle_naip_tile(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        z = safe_int_param(params.get('z', [None])[0], minimum=14, maximum=19)
        x = safe_int_param(params.get('x', [None])[0], minimum=0)
        y = safe_int_param(params.get('y', [None])[0], minimum=0)
        if z is None or x is None or y is None or x >= (1 << z) or y >= (1 << z):
            self.send_error(400, 'Invalid NAIP tile coordinate'); return

        world_half = 20037508.342789244
        span = world_half * 2 / (1 << z)
        min_x = -world_half + x * span
        max_y = world_half - y * span
        bbox = f'{min_x},{max_y - span},{min_x + span},{max_y}'
        url = ('https://imagery.nationalmap.gov/arcgis/rest/services/'
               'USGSNAIPPlus/ImageServer/exportImage?' + urllib.parse.urlencode({
                   'bbox': bbox, 'bboxSR': 3857, 'imageSR': 3857,
                   'size': '256,256', 'format': 'png32',
                   'transparent': 'true', 'f': 'image',
               }))

        def load_naip_tile():
            request = urllib.request.Request(url, headers={
                'User-Agent': 'GlobalMap/1.0', 'Accept': 'image/png',
            })
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.headers.get_content_type() != 'image/png':
                    raise ValueError('Unexpected NAIP tile content type')
                content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError('NAIP tile is too large')
            return content, 'image/png'

        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'naip-tile:v1:{z}:{x}:{y}', load_naip_tile,
                ttl=86400, stale_ttl=604800, persist=False, wait_timeout=20,
            )
            self._write_bytes(200, content, content_type,
                              cache_control='public, max-age=86400',
                              extra_headers={'X-GlobeView-Cache': cache_status})
        except Exception as error:
            self._log_exception('naip-tile', error)
            self.send_error(502)

    def _handle_tile(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        x = safe_int_param(params.get('x', [None])[0], minimum=0)
        y = safe_int_param(params.get('y', [None])[0], minimum=0)
        z = safe_int_param(params.get('z', [None])[0], minimum=0, maximum=22)
        if x is None or y is None or z is None:
            self.send_error(400, 'Missing x/y/z'); return
        max_coordinate = (1 << z) - 1
        if x > max_coordinate or y > max_coordinate:
            self.send_error(400, 'Invalid tile coordinate'); return
        url = f'https://tiles.ibi511.com/Geoservice/GetTrafficTile?x={x}&y={y}&z={z}'

        def load_tile():
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read()
                content_type = resp.headers.get('Content-Type', 'image/png')
            if content_type not in ('image/png', 'image/jpeg', 'image/webp'):
                content_type = 'image/png'
            return content, content_type

        try:
            content, content_type, cache_status = MEDIA_RESPONSE_CACHE.get_or_load(
                f'traffic-tile:v1:{z}:{x}:{y}',
                load_tile,
                ttl=TRAFFIC_TILE_CACHE_TTL,
                stale_ttl=TRAFFIC_TILE_STALE_TTL,
                persist=False,
                wait_timeout=15,
            )
            self._write_bytes(
                200,
                content,
                content_type,
                cache_control='public, max-age=60, stale-while-revalidate=300',
                extra_headers={'X-GlobeView-Cache': cache_status},
            )
        except Exception as e:
            self._log_exception('tile', e)
            self.send_error(502, str(e))

    def _handle_stream_proxy(self, parsed):
        # /stream/dis-se11.divas.cloud:8200/chan-376_h/index.m3u8?token=...
        # Strip the leading /stream/
        rest = parsed.path[len('/stream/'):]
        parsed_upstream = urllib.parse.urlsplit(f'https://{rest}')
        if parsed_upstream.username or parsed_upstream.password or not allowed_stream_host(parsed_upstream.hostname):
            self.send_error(403, 'Invalid stream host'); return
        if not parsed_upstream.path.startswith('/'):
            self.send_error(400, 'Invalid stream path'); return
        upstream_url = urllib.parse.urlunsplit((
            'https',
            parsed_upstream.netloc,
            parsed_upstream.path,
            parsed_upstream.query,
            parsed_upstream.fragment,
        ))
        proxy_query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        requested_state = next(
            (value.upper() for key, value in proxy_query_pairs if key == '_am_state'),
            '',
        )
        upstream_query = urllib.parse.urlencode([
            (key, value) for key, value in proxy_query_pairs if key != '_am_state'
        ])
        if upstream_query:
            upstream_url += '?' + upstream_query

        if not STREAM_REQUEST_SEMAPHORE.acquire(blocking=False):
            self.close_connection = True
            self._write_bytes(
                503,
                b'Video relay is busy; retry shortly.\n',
                'text/plain; charset=utf-8',
                cache_control='no-store',
                extra_headers={'Retry-After': '2', 'Connection': 'close'},
            )
            return
        try:
            region = traffic_region(requested_state) or stream_traffic_region(parsed_upstream.hostname)
            origin = region['traffic_origin']
            req = urllib.request.Request(upstream_url, headers={
                'Referer': f'{origin}/',
                'Origin': origin,
                'User-Agent': 'Mozilla/5.0',
                'Accept-Encoding': 'identity',
                # MDOT's nginx closes some large MPEG-TS responses early for a
                # plain HTTP/1.1 GET; its open-ended byte response is reliable.
                **({'Range': 'bytes=0-'} if (
                    region.get('traffic_adapter') == 'mdot' and parsed_upstream.path.endswith('.ts')
                ) else {}),
            })
            with STREAM_HTTP_OPENER.open(req, timeout=15) as resp:
                content_type = resp.headers.get('Content-Type', 'application/octet-stream')
                is_playlist = (
                    'mpegurl' in content_type.lower() or
                    parsed_upstream.path.lower().endswith(('.m3u8', '.m3u'))
                )
                prefix = resp.read(10)
                if is_playlist or prefix.startswith(b'#EXTM3U'):
                    content = prefix + resp.read(MAX_STREAM_PLAYLIST_BYTES - len(prefix) + 1)
                    if len(content) > MAX_STREAM_PLAYLIST_BYTES:
                        raise ValueError('Stream playlist is too large')
                    content = self._rewrite_m3u8(content, upstream_url, parsed.query)
                    self._write_bytes(
                        200,
                        content,
                        'application/vnd.apple.mpegurl',
                        cache_control='no-store',
                    )
                else:
                    media_type = content_type.split(';', 1)[0].strip().lower()
                    if not (
                        media_type.startswith(('video/', 'audio/', 'image/')) or
                        media_type in {'application/octet-stream', 'application/mp2t', 'binary/octet-stream'}
                    ):
                        raise ValueError('Unexpected stream media type')
                    self._write_streamed_upstream(resp, content_type, prefix=prefix)
        except Exception as e:
            self._log_exception('stream-proxy', e)
            if not self.wfile.closed:
                try:
                    self.send_error(502, str(e))
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        finally:
            STREAM_REQUEST_SEMAPHORE.release()

    def _rewrite_m3u8(self, content, upstream_url, qs):
        """Rewrite relative URLs in m3u8 to go through /stream/ proxy."""
        base = upstream_url.rsplit('/', 1)[0]
        # Extract base host+path for constructing proxy URLs
        up_parsed = urllib.parse.urlparse(base)
        host_path = up_parsed.netloc + up_parsed.path

        lines = content.decode('utf-8').splitlines()
        inherited_state = next(
            (value for key, value in urllib.parse.parse_qsl(qs, keep_blank_values=True) if key == '_am_state'),
            '',
        )

        def proxy_query(candidate):
            pairs = urllib.parse.parse_qsl(candidate or '', keep_blank_values=True)
            if inherited_state and not any(key == '_am_state' for key, _ in pairs):
                pairs.append(('_am_state', inherited_state))
            return urllib.parse.urlencode(pairs)

        out = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                # It's a URI line — make it absolute via our proxy
                if stripped.startswith('http'):
                    p = urllib.parse.urlparse(stripped)
                    if not allowed_stream_host(p.hostname):
                        continue
                    proxy_path = f'/stream/{p.netloc}{p.path}'
                    q = proxy_query(p.query or qs)
                    out.append(f'{proxy_path}?{q}' if q else proxy_path)
                else:
                    # Relative URL — combine with base
                    q = proxy_query(urllib.parse.urlparse(stripped).query or qs)
                    seg_name = stripped.split('?')[0]
                    out.append(f'/stream/{host_path}/{seg_name}?{q}' if q else f'/stream/{host_path}/{seg_name}')
            else:
                out.append(line)
        return '\n'.join(out).encode('utf-8')

    def _get_proxied_stream_url(self, cam_id, region):
        # Ask the camera's own state 511 service for an authenticated URL.
        if region.get('traffic_adapter') == 'mdot':
            camera = mdot_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No public MDOT HLS stream is available for this camera')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'algo':
            camera = fetch_algo_item('Cameras', cam_id)
            video_url = str((camera.get('playbackUrls') or {}).get('hls') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No allowlisted HLS video URL returned by ALGO Traffic')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'sc511':
            camera = sc511_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No public South Carolina HLS stream is available for this camera')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'tdot':
            camera = tdot_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No public TDOT HLS stream is available for this camera')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'vdot':
            camera = vdot_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No public VDOT HLS stream is available for this camera')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'wv511':
            camera = wv511_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No public WV511 HLS stream is available for this camera')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'mdchart':
            camera = mdchart_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No public Maryland CHART HLS stream is available for this camera')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'dcgis':
            camera = dcgis_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if (
                parsed_video.scheme != 'wss' or
                parsed_video.hostname != 'cctv.trafficview.org' or
                parsed_video.port != 8420
            ):
                raise ValueError('No public DDOT camera stream is available for this camera')
            return video_url

        if region.get('traffic_adapter') == 'deldot':
            camera = deldot_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No public DelDOT HLS stream is available for this camera')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'njta':
            camera = njta_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if parsed_video.scheme != 'https' or not allowed_stream_host(parsed_video.hostname):
                raise ValueError('No public NJTA HLS stream is available for this camera')
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{parsed_video.query}' if parsed_video.query else proxied

        if region.get('traffic_adapter') == 'ridot':
            camera = ridot_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if (
                parsed_video.scheme != 'https' or
                parsed_video.hostname != 'cdn3.wowza.com' or
                not parsed_video.path.endswith('.m3u8')
            ):
                raise ValueError('No public RIDOT HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'RI'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'mass511':
            camera = mass511_camera_detail(cam_id)
            stream_api_url = str(camera.get('stream_api_url') or '').strip()
            stream_api = urllib.parse.urlparse(stream_api_url)
            if not (
                stream_api.scheme == 'https' and
                stream_api.hostname == 'api.trafficland.com' and
                stream_api.path.startswith('/v2.2/json/stream/')
            ):
                raise ValueError('No public Mass511 live stream is available for this camera')
            stream_data = fetch_json_url(
                stream_api_url,
                headers=mass511_headers(),
                timeout=20,
            )
            urls = stream_data.get('urls') or {}
            video_url = str(
                ((urls.get('lq') or {}).get('hls')) or
                ((urls.get('hq') or {}).get('hls')) or
                ''
            ).strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if (
                parsed_video.scheme != 'https' or
                not parsed_video.hostname or
                not parsed_video.hostname.endswith('.trafficland.com') or
                not parsed_video.path.endswith('.m3u8')
            ):
                raise ValueError('TrafficLand returned an invalid Mass511 stream')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'MA'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'trafficwise_in':
            camera = indot_trafficwise_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            hostname = str(parsed_video.hostname or '').lower()
            if (
                parsed_video.scheme != 'https' or
                not (hostname == 'trafficwise.org' or hostname.endswith('.trafficwise.org')) or
                not parsed_video.path.lower().endswith('.m3u8')
            ):
                raise ValueError('No public INDOT TrafficWise HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'IN'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'mndot_cars':
            camera = mndot_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if (
                parsed_video.scheme != 'https' or
                parsed_video.hostname != 'video.dot.state.mn.us' or
                not parsed_video.path.lower().endswith('.m3u8')
            ):
                raise ValueError('No public Minnesota 511 HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'MN'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'iadot':
            camera = iadot_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            hostname = str(parsed_video.hostname or '').lower()
            if (
                parsed_video.scheme != 'https' or
                not (hostname == 'iowadot.gov' or hostname.endswith('.iowadot.gov')) or
                not parsed_video.path.lower().endswith('.m3u8')
            ):
                raise ValueError('No public Iowa DOT HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'IA'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'modot':
            camera = modot_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            hostname = str(parsed_video.hostname or '').lower()
            if (
                parsed_video.scheme != 'https' or
                not hostname.endswith('.modot.mo.gov') or
                not parsed_video.path.lower().endswith('.m3u8')
            ):
                raise ValueError('No public MoDOT HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'MO'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'oktraffic':
            camera = oktraffic_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if (
                parsed_video.scheme != 'https' or
                parsed_video.hostname != 'stream.oktraffic.org' or
                not parsed_video.path.startswith('/delay-stream/') or
                not parsed_video.path.lower().endswith('.m3u8')
            ):
                raise ValueError('No public OKTraffic HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'OK'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'drivetexas':
            camera = drivetexas_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            hostname = str(parsed_video.hostname or '').lower()
            if (
                parsed_video.scheme != 'https' or
                not (hostname == 'skyvdn.com' or hostname.endswith('.skyvdn.com')) or
                not parsed_video.path.startswith('/rtplive/') or
                not parsed_video.path.lower().endswith('.m3u8')
            ):
                raise ValueError('No public DriveTexas HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'TX'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'caltrans_quickmap':
            camera = caltrans_quickmap_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            if not video_url.startswith('/stream/wzmedia.dot.ca.gov/'):
                raise ValueError('No public Caltrans QuickMap HLS stream is available for this camera')
            return video_url

        if region.get('traffic_adapter') == 'nvroads':
            camera = nvroads_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            if not (
                parsed_video.scheme == 'https' and
                allowed_stream_host(parsed_video.hostname) and
                parsed_video.path.lower().endswith('.m3u8')
            ):
                raise ValueError('No public Nevada 511 HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'NV'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        if region.get('traffic_adapter') == 'cotrip':
            camera = cotrip_camera_detail(cam_id)
            video_url = str(camera.get('video_url') or '').strip()
            parsed_video = urllib.parse.urlparse(video_url)
            hostname = str(parsed_video.hostname or '').lower()
            if (
                parsed_video.scheme != 'https' or
                not (hostname == 'cotrip.org' or hostname.endswith('.cotrip.org')) or
                not parsed_video.path.lower().endswith('.m3u8')
            ):
                raise ValueError('No public COtrip HLS stream is available for this camera')
            query_pairs = urllib.parse.parse_qsl(parsed_video.query, keep_blank_values=True)
            query_pairs.append(('_am_state', 'CO'))
            proxied = f'/stream/{parsed_video.netloc}{parsed_video.path}'
            return f'{proxied}?{urllib.parse.urlencode(query_pairs)}'

        origin = region['traffic_origin']
        referer = f'{origin}/map'
        req = urllib.request.Request(
            f'{origin}/Camera/GetVideoUrl?imageId={cam_id}',
            headers={'Referer': referer, 'User-Agent': 'Mozilla/5.0'}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        token_data = json.load(resp)

        if not isinstance(token_data, dict):
            video_url = str(token_data or '').strip()
            if not video_url.startswith('https://'):
                raise ValueError('No video URL returned by state 511 service')
            suffix = ''
        else:
            # Step 2: get base video URL from camera tooltip (contains data-videourl attribute)
            import re as _re
            tip_req = urllib.request.Request(
                f'{origin}/tooltip/Cameras/{cam_id}?lang=en',
                headers={'Referer': referer, 'User-Agent': 'Mozilla/5.0'}
            )
            tip_resp = urllib.request.urlopen(tip_req, timeout=10)
            tip_html = tip_resp.read().decode(errors='ignore')
            m = _re.search(r'data-videourl="([^"]+)"', tip_html)
            if not m:
                raise ValueError(f'No video URL found in tooltip for camera {cam_id}')
            video_url = m.group(1)

            # Step 3: exchange token for secure suffix
            body = json.dumps(token_data).encode()
            token_service_url = (
                'https://vds.nc.insight-atms.com/api/SecureTokenUri/GetSecureTokenUriBySourceId'
                if region.get('traffic_adapter') == 'drivenc'
                else 'https://divas.cloud/VDS-API/SecureTokenUri/GetSecureTokenUriBySourceId'
            )
            req2 = urllib.request.Request(
                token_service_url,
                data=body,
                headers={
                    'Content-Type': 'application/json',
                    'Referer': f'{origin}/',
                    'Origin': origin,
                    'User-Agent': 'Mozilla/5.0'
                },
                method='POST'
            )
            resp2 = urllib.request.urlopen(req2, timeout=10)
            suffix = resp2.read().decode().strip().strip('"')

        # Build proxied URL: /stream/{host}{path}?{token}
        p = urllib.parse.urlparse(video_url)
        if p.scheme != 'https' or not allowed_stream_host(p.hostname):
            raise ValueError('State 511 returned a non-allowlisted video URL')
        qs = urllib.parse.urlparse(video_url + suffix).query or suffix.lstrip('?')
        proxied = f'/stream/{p.netloc}{p.path}?{qs}' if qs else f'/stream/{p.netloc}{p.path}'
        return proxied


class ProductionHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = SERVER_REQUEST_QUEUE

    def __init__(self, *args, max_concurrent=None, **kwargs):
        self.max_concurrent = max(1, int(max_concurrent or SERVER_MAX_CONCURRENT_REQUESTS))
        self._request_slots = threading.BoundedSemaphore(self.max_concurrent)
        self._metrics_lock = threading.Lock()
        self._active_requests = 0
        self._peak_requests = 0
        self._accepted_requests = 0
        self._rejected_requests = 0
        self._started_at = time.time()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            with self._metrics_lock:
                self._rejected_requests += 1
            try:
                body = b'GlobeView is busy; retry shortly.\n'
                request.sendall(
                    b'HTTP/1.1 503 Service Unavailable\r\n'
                    b'Content-Type: text/plain; charset=utf-8\r\n'
                    b'Cache-Control: no-store\r\n'
                    b'Retry-After: 2\r\n'
                    b'Connection: close\r\n'
                    + f'Content-Length: {len(body)}\r\n\r\n'.encode('ascii')
                    + body
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return

        with self._metrics_lock:
            self._active_requests += 1
            self._accepted_requests += 1
            self._peak_requests = max(self._peak_requests, self._active_requests)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._metrics_lock:
                self._active_requests -= 1
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._metrics_lock:
                self._active_requests -= 1
            self._request_slots.release()

    def snapshot(self):
        with self._metrics_lock:
            return {
                'active_requests': self._active_requests,
                'peak_requests': self._peak_requests,
                'accepted_requests': self._accepted_requests,
                'rejected_requests': self._rejected_requests,
                'max_concurrent_requests': self.max_concurrent,
                'request_queue': self.request_queue_size,
                'idle_timeout_seconds': SERVER_IDLE_TIMEOUT,
                'max_stream_requests': MAX_STREAM_REQUESTS,
                'api_upstream_concurrency': API_UPSTREAM_CONCURRENCY,
                'media_upstream_concurrency': MEDIA_UPSTREAM_CONCURRENCY,
                'source_fetch_workers': SOURCE_FETCH_WORKERS,
                'uptime_seconds': round(time.time() - self._started_at, 1),
            }


if __name__ == '__main__':
    refresh_emergency_cache_async()
    server = ProductionHTTPServer((HOST, PORT), Handler)
    print(f'Serving on http://{HOST}:{PORT}')
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('Keyboard interrupt received, exiting.', flush=True)
    finally:
        server.server_close()
        CACHE_REFRESH_EXECUTOR.shutdown(wait=True, cancel_futures=True)
        API_RESPONSE_CACHE.close()
        MEDIA_RESPONSE_CACHE.close()
        COMPRESSED_RESPONSE_CACHE.close()
        STATIC_RESPONSE_CACHE.close()
