"""On-demand ATCF track guidance for selected EONET tropical cyclones."""

import datetime as dt
import gzip
import io
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from hazard_feeds import hazard_snapshot


NHC_BASE = 'https://ftp.nhc.noaa.gov/atcf'
UCAR_BASE = 'https://hurricanes.ral.ucar.edu/realtime/plots'
UCAR_BASINS = {
    'WP': 'northwestpacific',
    'IO': 'northindian',
    'SH': 'southernhemisphere',
}
USER_AGENT = 'GlobeView/1.0 (on-demand tropical cyclone guidance)'
TTL_SECONDS = 900
_UTC = dt.timezone.utc
_cache = {}
_index_cache = {}
_lock = threading.RLock()

# Official and consensus tracks are visually separate from the model guidance.
MODEL_AIDS = (
    ('AVNI', 'GFS', '#71c5e8'),
    ('EMXI', 'ECMWF', '#e0bc76'),
    ('CMCI', 'Canadian', '#b99be3'),
    ('CTCI', 'COAMPS-TC', '#e3a477'),
    ('HFAI', 'HAFS-A', '#e57978'),
    ('HFBI', 'HAFS-B', '#edae73'),
    ('HFB2', 'HAFS-B', '#edae73'),
    ('HMNI', 'HMON', '#9bca83'),
    ('HMN2', 'HMON', '#9bca83'),
    ('HWFI', 'HWRF', '#75c8b0'),
    ('HWF2', 'HWRF', '#75c8b0'),
    ('NVGI', 'NAVGEM', '#91aee0'),
    ('UKXI', 'UKMET', '#d3a2c8'),
    ('AEMI', 'GEFS mean', '#6296c7'),
    ('CEMI', 'Canadian ensemble mean', '#9275bd'),
    ('EMNI', 'ECMWF ensemble mean', '#c9a85d'),
)
MODEL_META = {code: (name, color) for code, name, color in MODEL_AIDS}
TRACK_CODES = set(MODEL_META) | {'OFCL', 'OFCI', 'TVCN'}
TYPHOON_MODEL_AIDS = (
    ('AVNO', 'GFS', '#71c5e8'),
    ('EMX', 'ECMWF', '#e0bc76'),
    ('CMC', 'Canadian', '#b99be3'),
    ('UKM', 'UKMET', '#d3a2c8'),
    ('NGX', 'NAVGEM', '#91aee0'),
)
TYPHOON_ENSEMBLE_CODES = {f'AP{number:02d}' for number in range(1, 31)}
TYPHOON_TRACK_CODES = {code for code, _, _ in TYPHOON_MODEL_AIDS} | TYPHOON_ENSEMBLE_CODES | {'AEMN'}
JTWC_BASINS = {'wp', 'io', 'sh'}


def _read(url, max_bytes, gzip_content=False):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=18) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('Cyclone guidance exceeded size limit')
    if gzip_content:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as archive:
            body = archive.read(32 * 1024 * 1024 + 1)
        if len(body) > 32 * 1024 * 1024:
            raise ValueError('Cyclone guidance expanded beyond size limit')
    return body.decode('utf-8', errors='replace')


def _coordinate(value, latitude):
    match = re.fullmatch(r'(\d{1,5})([NSEW])', value.strip().upper())
    if not match:
        return None
    magnitude = int(match.group(1)) / 10
    hemisphere = match.group(2)
    if latitude and (hemisphere not in 'NS' or magnitude > 90):
        return None
    if not latitude and (hemisphere not in 'EW' or magnitude > 180):
        return None
    return -magnitude if hemisphere in 'SW' else magnitude


def parse_adeck(text, now=None, basin='nhc'):
    """Return one recent ATCF cycle, keeping model and ensemble runs distinct."""
    now = now or dt.datetime.now(_UTC)
    allowed_codes = TYPHOON_TRACK_CODES if basin in JTWC_BASINS else TRACK_CODES
    cycles = {}
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) < 8 or parts[4] not in allowed_codes:
            continue
        stamp = parts[2]
        if not re.fullmatch(r'\d{10}', stamp):
            continue
        try:
            issued = dt.datetime.strptime(stamp, '%Y%m%d%H').replace(tzinfo=_UTC)
            tau = int(parts[5])
        except ValueError:
            continue
        age = (now - issued).total_seconds()
        if age < -6 * 3600 or age > 24 * 3600 or not 0 <= tau <= 168:
            continue
        lat, lon = _coordinate(parts[6], True), _coordinate(parts[7], False)
        if lat is None or lon is None:
            continue
        cycles.setdefault(stamp, {}).setdefault(parts[4], {}).setdefault(tau, [lon, lat])

    ensemble_fallback = None
    for stamp in sorted(cycles, reverse=True):
        aids = cycles[stamp]
        tracks = []
        used_names = set()
        models = TYPHOON_MODEL_AIDS if basin in JTWC_BASINS else MODEL_AIDS
        for code, name, color in models:
            points = aids.get(code, {})
            if name in used_names or len(points) < 3 or max(points) < 24:
                continue
            used_names.add(name)
            tracks.append({'code': code, 'model': name, 'kind': 'model', 'color': color,
                           'points': [points[tau] for tau in sorted(points)]})
        if basin in JTWC_BASINS:
            for code in sorted(TYPHOON_ENSEMBLE_CODES):
                points = aids.get(code, {})
                if len(points) < 3 or max(points) < 24:
                    continue
                tracks.append({'code': code, 'model': f'GEFS member {code[2:]}', 'kind': 'ensemble',
                               'color': '#8faec1', 'points': [points[tau] for tau in sorted(points)]})
            mean_points = aids.get('AEMN', {})
            if len(mean_points) >= 3 and max(mean_points) >= 24:
                tracks.append({'code': 'AEMN', 'model': 'GEFS mean', 'kind': 'consensus',
                               'color': '#f0ce78', 'points': [mean_points[tau] for tau in sorted(mean_points)]})
            cycle = dt.datetime.strptime(stamp, '%Y%m%d%H').replace(tzinfo=_UTC).isoformat().replace('+00:00', 'Z')
            result = {'cycle': cycle, 'tracks': tracks}
            if len([track for track in tracks if track['kind'] == 'model']) >= 2:
                return result
            if ensemble_fallback is None and len([track for track in tracks if track['kind'] == 'ensemble']) >= 5:
                ensemble_fallback = result
            continue
        if len(tracks) < 2:
            continue
        for code, name, kind, color in (
            ('TVCN', 'Model consensus', 'consensus', '#f0ce78'),
            ('OFCL', 'NHC official forecast', 'official', '#f3f8ee'),
            ('OFCI', 'NHC official forecast', 'official', '#f3f8ee'),
        ):
            points = aids.get(code, {})
            if kind == 'official' and any(track['kind'] == 'official' for track in tracks):
                continue
            if len(points) < 2:
                continue
            tracks.append({'code': code, 'model': name, 'kind': kind, 'color': color,
                           'points': [points[tau] for tau in sorted(points)]})
        return {'cycle': dt.datetime.strptime(stamp, '%Y%m%d%H').replace(tzinfo=_UTC).isoformat().replace('+00:00', 'Z'),
                'tracks': tracks}
    return ensemble_fallback if basin in JTWC_BASINS else None


def _storm_name(title):
    return re.sub(r'^(?:HURRICANE|SUPER TYPHOON|TYPHOON|TROPICAL STORM|TROPICAL DEPRESSION|TROPICAL CYCLONE)\s+',
                  '', title.strip().upper())


def _nhc_storm_index():
    """The per-basin .nhc indexes can be historical; use NOAA's master list."""
    with _lock:
        cached = _index_cache.get('nhc')
        if cached and cached[0] > time.time():
            return cached[1]
        text = _read(f'{NHC_BASE}/index/storm_list.txt', 1024 * 1024)
        storms = {}
        for line in text.splitlines():
            parts = [part.strip() for part in line.split(',')]
            if not parts or not parts[0]:
                continue
            match = re.fullmatch(r'(?:AL|EP|CP)\d{2}(\d{4})', parts[-1].upper())
            if match:
                storms.setdefault((parts[0].upper(), int(match[1])), set()).add(parts[-1].upper())
        # Ambiguous names must not select a different storm's forecast.
        index = {key: next(iter(ids)) for key, ids in storms.items() if len(ids) == 1}
        _index_cache['nhc'] = (time.time() + TTL_SECONDS, index)
        return index


def _resolve_atcf_id(storm):
    source = urllib.parse.urlsplit(storm.get('sourceUrl') or '')
    if source.hostname in {'www.metoc.navy.mil', 'metoc.navy.mil'}:
        match = re.search(r'/([a-z]{2})(\d{2})(\d{2})\.tcw(?:$|/)', source.path, re.I)
        if match:
            basin, number, year = match.groups()
            if basin.upper() in {'AL', 'EP', 'CP', 'WP', 'IO', 'SH'}:
                return f'{basin.upper()}{number}20{year}'
            return None
    name = _storm_name(storm.get('title') or '')
    if not name or name.isdigit():
        return None
    year = dt.datetime.now(_UTC).year
    if source.hostname in {'www.nhc.noaa.gov', 'nhc.noaa.gov'}:
        archive = re.match(r'/archive/(\d{4})/', source.path)
        if archive:
            year = int(archive[1])
    try:
        return _nhc_storm_index().get((name, year))
    except Exception:
        return None


def guidance_snapshot(event_id):
    if not isinstance(event_id, str) or len(event_id) > 80:
        raise ValueError('Invalid cyclone ID')
    with _lock:
        cached = _cache.get(event_id)
        if cached and cached[0] > time.time():
            return cached[1]
        cyclones = json.loads(hazard_snapshot('cyclones'))['items']
        storm = next((item for item in cyclones if item['id'] == event_id), None)
        if not storm:
            raise ValueError('Cyclone not found')
        result = {'stormId': event_id, 'title': storm['title'], 'status': 'unavailable',
                  'tracks': [], 'cycle': None, 'sourceUrl': storm.get('sourceUrl') or '',
                  'message': 'Public multi-model guidance is unavailable for this storm or basin.'}
        atcf_id = _resolve_atcf_id(storm)
        if atcf_id and re.fullmatch(r'(?:AL|EP|CP|WP|IO|SH)\d{2}\d{4}', atcf_id):
            if atcf_id[:2] in UCAR_BASINS:
                year = atcf_id[-4:]
                directory = UCAR_BASINS[atcf_id[:2]]
                source_url = f'{UCAR_BASE}/{directory}/{year}/{atcf_id.lower()}/a{atcf_id.lower()}.dat'
                max_bytes, zipped, basin = 12 * 1024 * 1024, False, atcf_id[:2].lower()
            else:
                source_url = f'{NHC_BASE}/aid_public/a{atcf_id.lower()}.dat.gz'
                max_bytes, zipped, basin = 4 * 1024 * 1024, True, 'nhc'
            try:
                forecast = parse_adeck(_read(source_url, max_bytes, gzip_content=zipped), basin=basin)
                if forecast:
                    result.update({'status': 'available', 'tracks': forecast['tracks'],
                                   'cycle': forecast['cycle'], 'sourceUrl': source_url, 'message': ''})
                else:
                    result['message'] = 'No current model runs. This storm may have weakened or ended.'
            except Exception:
                result['message'] = 'Current model guidance could not be loaded from UCAR.' if basin in JTWC_BASINS else 'Current model guidance could not be loaded from NHC.'
        _cache[event_id] = (time.time() + TTL_SECONDS, result)
        return result
