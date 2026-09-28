"""Cached cybersecurity feeds, including FortiGuard's observed threat-map activity."""
import concurrent.futures
import datetime as dt
from email.utils import parsedate_to_datetime
import ipaddress
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


_UTC = dt.timezone.utc
_LOCK = threading.Lock()
_CACHE = {}
_INFLIGHT = {}
_RETRY_AFTER = {}
_GEO_CACHE = {}
_GEO_LOCK = threading.Lock()
_TTL = {'scans': 3600, 'kev': 3600, 'outbreaks': 3600, 'attacks': 60}


def _get(url, limit=5_000_000, timeout=20):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobalMap/1.0 (public cybersecurity visualization)',
        'Accept': 'application/json,text/plain',
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('Cyber feed exceeded size limit')
    return body


def _geolocate(ip):
    with _GEO_LOCK:
        cached = _GEO_CACHE.get(ip)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
    url = ('https://stat.ripe.net/data/geoloc/data.json?resource='
           f'{urllib.parse.quote(ip)}&sourceapp=globalmap')
    try:
        data = json.loads(_get(url, 100_000, timeout=6))['data']['located_resources']
        locations = data[0].get('locations', []) if data else []
        location = max(locations, key=lambda row: float(row.get('covered_percentage') or 0)) if locations else None
        if location:
            lat, lon = float(location['latitude']), float(location['longitude'])
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                location = None
        result = {'lat': lat, 'lon': lon, 'country': location.get('country'), 'city': location.get('city')} if location else None
    except (KeyError, IndexError, TypeError, ValueError, OSError):
        result = None
    with _GEO_LOCK:
        _GEO_CACHE[ip] = (time.monotonic() + (86400 if result else 3600), result)
    return result


def _scans():
    # SANS asks consumers to use the static feed and fetch it no more than hourly.
    lines = _get('https://feeds.dshield.org/feeds/topips.txt', 100_000).decode('utf-8', 'replace').splitlines()
    sources = []
    seen = set()
    for line in lines:
        parts = line.split('\t')
        ip = parts[0].strip() if parts else ''
        try:
            if not ipaddress.ip_address(ip).is_global or ip in seen:
                continue
        except ValueError:
            continue
        seen.add(ip)
        sources.append((ip, parts[1].strip() if len(parts) > 1 else '', 0, 0))
        if len(sources) >= 100:
            break
    feed_type = 'static'
    if len(sources) < 50:
        # The advertised top-100 text feed sometimes contains far fewer rows.
        # Use SANS's own ranked API only when it supplies a fuller snapshot.
        try:
            rows = json.loads(_get('https://isc.sans.edu/api/topips/records/100?json', 1_000_000))
            api_sources = []
            api_seen = set()
            hostnames = {ip: hostname for ip, hostname, _, _ in sources}
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                ip = str(row.get('source') or '').strip()
                try:
                    if not ipaddress.ip_address(ip).is_global or ip in api_seen:
                        continue
                except ValueError:
                    continue
                api_seen.add(ip)
                try:
                    reports = max(0, int(row.get('reports') or 0))
                    targets = max(0, int(row.get('targets') or 0))
                except (TypeError, ValueError):
                    reports = targets = 0
                api_sources.append((ip, hostnames.get(ip, ''), reports, targets))
                if len(api_sources) >= 100:
                    break
            if len(api_sources) > len(sources):
                sources = api_sources
                feed_type = 'api'
        except (OSError, TypeError, ValueError):
            pass
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        locations = list(pool.map(lambda entry: _geolocate(entry[0]), sources))
    items = []
    for rank, ((ip, hostname, reports, targets), location) in enumerate(zip(sources, locations), 1):
        if not location:
            continue
        items.append({
            'id': ip, 'ip': ip, 'rank': rank, 'hostname': hostname if hostname != 'TIMEOUT' else '',
            'reports': reports, 'targets': targets,
            'lat': location['lat'], 'lon': location['lon'],
            'country': location['country'], 'city': location['city'],
            'sourceUrl': 'https://isc.sans.edu/ipinfo.html?ip=' + urllib.parse.quote(ip),
        })
    return {
        'source': 'SANS Technology Institute, Internet Storm Center; locations: RIPEstat',
        'observed': dt.datetime.now(_UTC).isoformat().replace('+00:00', 'Z'),
        'listed': len(sources),
        'feedType': feed_type,
        'items': items,
    }


def _kev():
    data = json.loads(_get('https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json'))
    rows = sorted(data.get('vulnerabilities', []), key=lambda row: (row.get('dateAdded') or '', row.get('cveID') or ''), reverse=True)
    items = []
    for row in rows[:15]:
        cve = str(row.get('cveID') or '')
        if not cve.startswith('CVE-'):
            continue
        items.append({
            'cve': cve, 'dateAdded': row.get('dateAdded'),
            'vendor': row.get('vendorProject'), 'product': row.get('product'),
            'name': row.get('vulnerabilityName'), 'description': row.get('shortDescription'),
            'ransomwareUse': row.get('knownRansomwareCampaignUse'),
            'sourceUrl': 'https://nvd.nist.gov/vuln/detail/' + urllib.parse.quote(cve),
        })
    return {'source': 'CISA Known Exploited Vulnerabilities Catalog',
            'released': data.get('dateReleased'), 'catalogUrl': 'https://www.cisa.gov/known-exploited-vulnerabilities-catalog',
            'items': items}


def _outbreaks():
    """Fortinet's published RSS alerts are reports, not geolocated attack telemetry."""
    feed_url = 'https://filestore.fortinet.com/fortiguard/rss/outbreakalert.xml'
    root = ET.fromstring(_get(feed_url, 1_000_000))
    items = []
    for row in root.findall('.//item'):
        title = ' '.join((row.findtext('title') or '').split())[:180]
        link = (row.findtext('link') or '').strip()
        parsed = urllib.parse.urlsplit(link)
        if not title or parsed.scheme != 'https' or parsed.hostname != 'fortiguard.fortinet.com':
            continue
        published = None
        try:
            published = parsedate_to_datetime(row.findtext('pubDate') or '').astimezone(_UTC).isoformat().replace('+00:00', 'Z')
        except (TypeError, ValueError, OverflowError):
            pass
        description = ' '.join((row.findtext('description') or '').split())
        if len(description) > 220:
            description = description[:217].rsplit(' ', 1)[0].rstrip(' ,;:') + '…'
        items.append({'title': title, 'published': published, 'summary': description, 'sourceUrl': link})
    items.sort(key=lambda item: item['published'] or '', reverse=True)
    return {'source': 'FortiGuard Labs Outbreak Alerts', 'feedUrl': feed_url, 'items': items[:8]}


def _fortiguard_attacks(payload, now=None):
    """Keep only recent, geolocated detections; never expose source IPs."""
    if not isinstance(payload, dict) or not isinstance(payload.get('ips'), dict):
        raise ValueError('FortiGuard activity response is invalid')
    now = time.time() if now is None else now
    items = []
    seen = set()
    for bucket, rows in payload['ips'].items():
        if not str(bucket).isdigit() or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            event_id = str(row.get('redis_ms') or '')
            if not re.fullmatch(r'\d{13}-\d+', event_id) or event_id in seen:
                continue
            try:
                observed = int(event_id.split('-', 1)[0]) / 1000
                src = [float(row['src_long']), float(row['src_lat'])]
                dest = [float(row['dest_long']), float(row['dest_lat'])]
            except (TypeError, ValueError, KeyError):
                continue
            if not (-10 <= now - observed <= 150):
                continue
            if not all(math.isfinite(v) for v in src + dest):
                continue
            if not (-180 <= src[0] <= 180 and -90 <= src[1] <= 90 and
                    -180 <= dest[0] <= 180 and -90 <= dest[1] <= 90):
                continue
            if abs(src[0] - dest[0]) < .01 and abs(src[1] - dest[1]) < .01:
                continue
            seen.add(event_id)
            items.append({
                'id': event_id, 'observed': int(observed * 1000),
                'src': src, 'dest': dest,
                'srcLabel': ' · '.join(str(row.get(k) or '').strip()[:50] for k in ('src_city', 'src_country')).strip(' ·'),
                'destLabel': ' · '.join(str(row.get(k) or '').strip()[:50] for k in ('dest_city', 'dest_country')).strip(' ·'),
                'threat': str(row.get('vuln_name') or row.get('profile_type') or 'Threat detection')[:120],
                'severity': str(row.get('severity') or '')[:20],
            })
    items.sort(key=lambda item: item['observed'], reverse=True)
    return {'source': 'FortiGuard Labs threat map · observed detections',
            'observed': items[0]['observed'] if items else None, 'items': items[:120]}


def _attacks():
    params = {'outbreak_id': 0, 'segment_sec': 5, 'replay': 'false',
              'limit': 10, 'last_sec': 60, '_gv': int(time.time() // 60)}
    url = 'https://fortiguard.fortinet.com/api/threatmap/live/outbreak?' + urllib.parse.urlencode(params)
    return _fortiguard_attacks(json.loads(_get(url, limit=1_000_000, timeout=12)))


_LOADERS = {'scans': _scans, 'kev': _kev, 'outbreaks': _outbreaks, 'attacks': _attacks}


def cyber_snapshot(layer):
    if layer not in _LOADERS:
        raise ValueError('Unknown cyber feed')
    while True:
        with _LOCK:
            now = time.monotonic()
            cached = _CACHE.get(layer)
            if cached and now < cached['expires']:
                return cached['body']
            if now < _RETRY_AFTER.get(layer, 0):
                if cached and now < cached['stale']:
                    return cached['body']
                raise RuntimeError('Cyber feed is temporarily unavailable')
            event = _INFLIGHT.get(layer)
            if event is None:
                event = threading.Event()
                _INFLIGHT[layer] = event
                break
        event.wait(timeout=30)
    try:
        body = json.dumps(_LOADERS[layer](), separators=(',', ':')).encode()
    except Exception:
        with _LOCK:
            _RETRY_AFTER[layer] = time.monotonic() + (30 if layer == 'attacks' else 300)
            cached = _CACHE.get(layer)
            if cached and time.monotonic() < cached['stale']:
                return cached['body']
        raise
    else:
        now = time.monotonic()
        with _LOCK:
            _CACHE[layer] = {'body': body, 'expires': now + _TTL[layer],
                             'stale': now + (120 if layer == 'attacks' else 86400)}
            _RETRY_AFTER.pop(layer, None)
        return body
    finally:
        with _LOCK:
            _INFLIGHT.pop(layer, None)
            event.set()
