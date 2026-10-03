"""Offline OSM-mapped Georgian enforcement references, without live status/video."""

import datetime as dt
import functools
import json
import math
import re
from pathlib import Path

SOURCE_URL = 'https://download.geofabrik.de/europe/georgia.html'
BOUNDARY_URL = 'https://download.geofabrik.de/europe/georgia.poly'
CATALOG_PATH = Path(__file__).with_name('georgia-enforcement.json')
GEORGIA_BOUNDS = (39.8, 41.0, 46.8, 43.7)
KINDS = {'speed_camera', 'plate_reader'}


@functools.lru_cache(maxsize=1)
def enforcement_catalog():
    with CATALOG_PATH.open(encoding='utf-8') as source:
        catalog = json.load(source)
    if (catalog.get('sourceUrl') != SOURCE_URL or catalog.get('boundarySource') != BOUNDARY_URL
            or catalog.get('osmLicence') != 'ODbL 1.0'):
        raise ValueError('Unexpected Georgian enforcement provenance')
    try:
        stamp = dt.datetime.fromisoformat(catalog['osmDataThrough'].replace('Z', '+00:00'))
        if stamp.utcoffset() != dt.timedelta(0):
            raise ValueError('Snapshot must be in UTC')
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ValueError('Invalid Georgian enforcement snapshot date')
    for key in ('osmInputSha256', 'boundaryInputSha256'):
        if not isinstance(catalog.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', catalog[key]):
            raise ValueError('Invalid Georgian enforcement input hash')
    rows = catalog.get('locations')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError('Invalid Georgian enforcement catalog')
    ids = set()
    for row in rows:
        if (not isinstance(row, dict) or isinstance(row.get('osmNode'), bool)
                or not isinstance(row.get('osmNode'), int) or row['osmNode'] <= 0
                or row['osmNode'] in ids or row.get('locationKind') != 'osm_mapped_node'):
            raise ValueError('Invalid Georgian enforcement node')
        kinds = row.get('mappedKinds')
        if (not isinstance(kinds, list) or not kinds or len(kinds) > 2
                or any(not isinstance(kind, str) or kind not in KINDS for kind in kinds)
                or len(kinds) != len(set(kinds))):
            raise ValueError('Invalid Georgian mapped capabilities')
        for key, low, high in (('lon', GEORGIA_BOUNDS[0], GEORGIA_BOUNDS[2]),
                               ('lat', GEORGIA_BOUNDS[1], GEORGIA_BOUNDS[3])):
            value = row.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not low <= value <= high):
                raise ValueError('Georgian enforcement node is outside the extract bounds')
        speed = row.get('mappedLimitKmh')
        if speed is not None and (isinstance(speed, bool) or not isinstance(speed, int)
                                   or not 5 <= speed <= 150 or 'speed_camera' not in kinds):
            raise ValueError('Invalid Georgian mapped speed limit')
        directions = row.get('directions', [])
        if (not isinstance(directions, list) or len(directions) > 4
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or not 0 <= value < 360 for value in directions)):
            raise ValueError('Invalid Georgian mapped direction')
        ids.add(row['osmNode'])
    return catalog


def enforcement_for_bbox(bbox):
    west, south, east, north = bbox
    catalog = enforcement_catalog()
    items = []
    for row in catalog['locations']:
        if not (west <= row['lon'] <= east and south <= row['lat'] <= north):
            continue
        kinds = row['mappedKinds']
        title = 'Mapped speed camera' if 'speed_camera' in kinds else 'Mapped plate-reader camera'
        if len(kinds) == 2:
            title += ' · plate reader'
        detail = []
        if row.get('mappedLimitKmh') is not None:
            detail.append(f"Mapped limit {row['mappedLimitKmh']} km/h")
        if row.get('directions'):
            detail.append('Facing ' + ' / '.join(f'{value:g}°' for value in row['directions']))
        detail.extend(['Mapped location', 'Public video unavailable'])
        items.append({
            'type': 'node', 'id': row['osmNode'], 'lat': row['lat'], 'lon': row['lon'],
            'title': title, 'detail': ' · '.join(detail),
            'source': f"OSM (ODbL) · {catalog['osmDataThrough'][:10]} snapshot · status unverified",
            'source_url': f"https://www.openstreetmap.org/node/{row['osmNode']}",
            'record_kind': 'mapped_speed_camera' if 'speed_camera' in kinds else 'mapped_plate_reader',
            'location_kind': row['locationKind'],
        })
    return items
