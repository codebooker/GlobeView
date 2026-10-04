"""Hiroshima Prefecture road-camera stills with DoboX location attribution."""

import csv
import datetime as dt
import email.utils
import io
import re
import time
import urllib.request
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError


LOCATIONS_URL = 'https://hiroshima-dobox.jp/resource_download/32516'
LOCATION_SOURCE = 'https://hiroshima-dobox.jp/resources/32516'
CAMERA_LIST_URL = 'https://www.roadnavi.pref.hiroshima.lg.jp/camera_list.php'
CAMERA_ORIGIN = 'https://www.roadnavi.pref.hiroshima.lg.jp'
_JST = ZoneInfo('Asia/Tokyo')
_HEADERS = {'User-Agent': 'GlobeView/1.0 (public road-camera reader)'}
_LISTED = {}


def _read(url, limit):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=15) as response:
        if response.url != url:
            raise ValueError('Hiroshima camera source redirected')
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('Hiroshima camera source exceeded size limit')
    return body


def _locations(body):
    rows = {}
    for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig'))):
        try:
            ident = str(int(row['観測地点ID']))
            lat, lon = float(row['緯度']), float(row['経度'])
        except (KeyError, TypeError, ValueError):
            continue
        if 33.5 <= lat <= 35.3 and 131.5 <= lon <= 133.6:
            rows[ident] = {'lat': lat, 'lon': lon, 'name': row.get('観測所名', '').strip(),
                           'road': row.get('路線名', '').strip()}
    if len(rows) < 100:
        raise ValueError('Hiroshima camera coordinate catalog is incomplete')
    return rows


def _listed_cameras(body, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    text = body.decode('utf-8', 'replace')
    cameras = set()
    # The listing supplies one image and an observation timestamp per camera.
    for card in text.split('<div class="content">')[1:]:
        match = re.search(r'href="camera_detail\.php\?id=(\d+)"[^>]*>\s*'
                          r'<img[^>]+src="snow_pic/(\d+)\.jpg\?[^"<]*"', card)
        stamp = re.search(r'<td class="time">(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})', card)
        if not match or not stamp or int(match[1]) != int(match[2]):
            continue
        observed = dt.datetime.strptime(stamp[1], '%Y/%m/%d %H:%M:%S').replace(tzinfo=_JST)
        age = (now - observed).total_seconds()
        if -300 <= age <= 30 * 60:
            cameras.add(str(int(match[1])))
    if len(cameras) < 75:
        raise ValueError('Hiroshima road-camera listing is incomplete or stale')
    return cameras


def camera_features():
    locations = _locations(_read(LOCATIONS_URL, 100000))
    listed = _listed_cameras(_read(CAMERA_LIST_URL, 150000))
    cameras = listed & locations.keys()
    if len(cameras) < 75:
        raise ValueError('Hiroshima road-camera locations do not match the live listing')
    _LISTED.clear()
    _LISTED.update({ident: time.time() for ident in cameras})
    return [{
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [locations[ident]['lon'], locations[ident]['lat']]},
        'properties': {
            'key': f'jp:hiroshima:camera:{ident}', 'layer': 'cameras',
            'title': f"{locations[ident]['name']} · {locations[ident]['road']}".strip(' ·'),
            'detail': 'Recent still · locations © Hiroshima Prefecture DoboX (CC BY)',
            'snapshot_url': f'/hiroshima-camera/{ident}', 'snapshot_refresh_ms': 60000,
            'source': 'Hiroshima Prefecture road navigation / DoboX',
            'source_url': f'{CAMERA_ORIGIN}/camera_detail.php?id={ident}',
        },
    } for ident in sorted(cameras, key=int)]


def camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{1,3}', camera_id):
        raise ValueError('Invalid Hiroshima camera ID')
    ident = str(int(camera_id))
    if time.time() - _LISTED.get(ident, 0) > 15 * 60:
        raise FileNotFoundError('Hiroshima camera is not in the current listing')
    url = f'{CAMERA_ORIGIN}/snow_pic/{ident}.jpg'
    with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=15) as response:
        if response.url != url or response.headers.get_content_type() != 'image/jpeg':
            raise ValueError('Unexpected Hiroshima camera response')
        modified = response.headers.get('Last-Modified')
        body = response.read(2 * 1024 * 1024 + 1)
    if not modified or not 0 <= time.time() - email.utils.parsedate_to_datetime(modified).timestamp() <= 30 * 60:
        raise FileNotFoundError('Hiroshima road-camera image is stale')
    if not 4000 <= len(body) <= 2 * 1024 * 1024 or not body.startswith(b'\xff\xd8\xff'):
        raise FileNotFoundError('Hiroshima road-camera image is unavailable')
    try:
        with Image.open(io.BytesIO(body)) as picture:
            if picture.format != 'JPEG' or picture.width < 240 or picture.height < 160:
                raise ValueError('Hiroshima camera image has unexpected dimensions')
            picture.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('Hiroshima camera image is invalid') from exc
    return body, 'image/jpeg'
