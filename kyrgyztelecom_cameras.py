"""Audited public Kyrgyztelecom street views; no arbitrary media proxying."""

import concurrent.futures
import datetime as dt
import email.utils
import json
import re
import time
import urllib.request


SOURCE = 'https://online.kt.kg/'
CATALOG = SOURCE + 'gen/cameras'
STREAM_BASE = 'https://stream.kt.kg:5443/live/'
# The operator publishes no coordinates. This is the named square visible in all
# three audited views, not a claim about camera installation positions.
SQUARE_REFERENCE = [74.6036915, 42.8761325]
LOCATION_SOURCE = 'https://www.openstreetmap.org/way/181568920'
CAMERAS = {
    'camera25': ('Бишкек площадь Ала-Тоо', 'Square overview'),
    'camera27': ('Площадь Ала-Тоо', 'Chuy Avenue'),
    'camera24': ('Бишкек', 'Manas monument / Chuy Avenue'),
}
MAX_SEGMENT = 6 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _read(url, limit, method='GET'):
    request = urllib.request.Request(url, method=method,
                                     headers={'User-Agent': 'GlobeView/1.0 (public camera reader)'})
    with _OPENER.open(request, timeout=12) as response:
        if response.status != 200 or response.url != url:
            raise ValueError('Camera response redirected or was incomplete')
        body = response.read(limit + 1) if method == 'GET' else b''
        headers = dict(response.headers.items())
    if len(body) > limit:
        raise ValueError('Camera response exceeded size limit')
    return body, {key.lower(): value for key, value in headers.items()}


def _stamp(headers):
    try:
        stamp = email.utils.parsedate_to_datetime(headers['last-modified']).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Camera freshness metadata is missing') from exc
    if not -120 <= time.time() - stamp <= 180:
        raise FileNotFoundError('Camera media is stale')
    return stamp


def _playlist(camera_id):
    if camera_id not in CAMERAS:
        raise ValueError('Unknown Kyrgyztelecom camera')
    url = STREAM_BASE + camera_id + '.m3u8'
    body, headers = _read(url, 32 * 1024)
    stamp = _stamp(headers)
    text = body.decode('utf-8')
    if not text.startswith('#EXTM3U') or '#EXT-X-ENDLIST' in text or text.count('#EXTINF:') < 2:
        raise FileNotFoundError('Camera playlist is not live')
    # Only ordinary MPEG-TS segments on this exact camera path are supported.
    segments = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')]
    if not segments or any(not re.fullmatch(re.escape(camera_id) + r'-\d{1,12}\.ts', line)
                           for line in segments) or '#EXT-X-KEY:' in text:
        raise ValueError('Camera playlist contains unsupported assets')
    return url, STREAM_BASE + segments[-1], stamp


def _segment_headers(headers):
    stamp = _stamp(headers)
    try:
        size = int(headers['content-length'])
    except (KeyError, ValueError) as exc:
        raise ValueError('Camera segment size is unknown') from exc
    if not 188 <= size <= MAX_SEGMENT or headers.get('content-type', '').split(';')[0] != 'video/mp2t':
        raise ValueError('Camera segment is oversized or unsupported')
    return stamp, size


def camera_segment(camera_id):
    """Download one bounded, fresh segment for server-side preview decoding."""
    _, segment, _ = _playlist(camera_id)
    body, headers = _read(segment, MAX_SEGMENT)
    _, size = _segment_headers(headers)
    if len(body) != size or body[0] != 0x47:
        raise ValueError('Camera segment is incomplete or invalid')
    return body


def _view(camera_id):
    try:
        url, segment, stamp = _playlist(camera_id)
        _, headers = _read(segment, 0, method='HEAD')
        segment_stamp, _ = _segment_headers(headers)
        return {'label': CAMERAS[camera_id][1], 'url': '/kyrgyztelecom-camera/' + camera_id,
                'video_url': url, 'video_format': 'hls', 'stamp': min(stamp, segment_stamp)}
    except (OSError, ValueError):
        return None


def camera_features():
    body, _ = _read(CATALOG, 32 * 1024)
    rows = json.loads(body)['cameras']
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('Camera catalog is unsupported')
    # Require the audited public name/stream pairing; do not import city
    # centroids for the catalog's otherwise unlocated panorama cameras.
    published = {row.get('stream'): row.get('name') for row in rows if isinstance(row, dict)}
    ids = [camera_id for camera_id, (name, _) in CAMERAS.items()
           if published.get(STREAM_BASE + camera_id + '.m3u8') == name]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        views = [view for view in pool.map(_view, ids) if view is not None]
    if not views:
        return []
    stamp = min(view.pop('stamp') for view in views)
    primary = views[0]
    return [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': SQUARE_REFERENCE},
             'properties': {
                 'key': 'kg:kyrgyztelecom:ala-too', 'layer': 'cameras',
                 'title': 'Bishkek · Ala-Too Square',
                 'detail': f'{len(views)} live street views · approximate square reference',
                 'source': '© Kyrgyztelecom · OpenStreetMap reference location',
                 'source_url': SOURCE, 'location_source_url': LOCATION_SOURCE,
                 'snapshot_url': primary['url'], 'snapshot_refresh_ms': 60000,
                 'video_url': primary['video_url'], 'video_format': 'hls',
                 'camera_views': views, 'valid_until': stamp + 600,
                 'updated_at': dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(),
             }}]
