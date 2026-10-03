"""Armenia MIA junction references and OSM-mapped speed cameras, without video."""

import datetime as dt
import functools
import json
import math
from pathlib import Path

SOURCE_URL = 'https://mia.gov.am/certificate/'
DOCUMENT_URL = ('https://mia.gov.am/wp-content/uploads/2026/05/'
                '%D4%BD%D5%A1%D5%B9%D5%B4%D5%A5%D6%80%D5%B8%D6%82%D5%AF%D5%B6%D5%A5%D6%80.docx')
DOCUMENT_MODIFIED = '2026-05-25T09:24:00Z'
CATALOG_PATH = Path(__file__).with_name('armenia-road-cameras.json')
OSM_SOURCE_URL = 'https://download.geofabrik.de/asia/armenia.html'
SPEED_CATALOG_PATH = Path(__file__).with_name('armenia-speed-cameras.json')
ARMENIA_BOUNDS = (43.4, 38.8, 46.7, 41.4)


@functools.lru_cache(maxsize=1)
def enforcement_catalog():
    with CATALOG_PATH.open(encoding='utf-8') as source:
        catalog = json.load(source)
    if (catalog.get('sourceUrl') != SOURCE_URL or catalog.get('documentUrl') != DOCUMENT_URL
            or catalog.get('documentModified') != DOCUMENT_MODIFIED):
        raise ValueError('Unexpected Armenian road-camera inventory provenance')
    rows = catalog.get('locations')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 158:
        raise ValueError('Invalid Armenian camera catalog')
    ids = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or row['id'] in ids or not row['id'].startswith('am:mia:surveillance:')
                or row.get('locationKind') != 'junction_reference'
                or not isinstance(row.get('osmNode'), int)
                or not isinstance(row.get('osmWays'), list) or len(row['osmWays']) < 2
                or not isinstance(row.get('roadNames'), list) or len(row['roadNames']) < 2
                or not row.get('sourceAddress')):
            raise ValueError('Invalid Armenian camera junction reference')
        for name, low, high in (('lat', 40.08, 40.28), ('lon', 44.40, 44.63)):
            value = row.get(name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not low <= value <= high):
                raise ValueError('Armenian camera reference is outside Yerevan')
        ids.add(row['id'])
    return rows


def enforcement_for_bbox(bbox):
    west, south, east, north = bbox
    return [{
        'type': 'node', 'id': row['id'], 'lat': row['lat'], 'lon': row['lon'],
        'title': 'Road surveillance camera · inventory',
        'detail': (' × '.join(row['roadNames'])
                   + ' · Approximate junction reference · Public video unavailable'),
        'source': 'Armenia MIA · May 2026 document · status unverified · OSM (ODbL)',
        'source_url': SOURCE_URL,
        'record_kind': 'road_surveillance_inventory',
        'location_kind': row['locationKind'],
    } for row in enforcement_catalog()
        if west <= row['lon'] <= east and south <= row['lat'] <= north]


@functools.lru_cache(maxsize=1)
def speed_camera_catalog():
    with SPEED_CATALOG_PATH.open(encoding='utf-8') as source:
        catalog = json.load(source)
    if (catalog.get('sourceUrl') != OSM_SOURCE_URL or catalog.get('osmLicence') != 'ODbL 1.0'
            or catalog.get('tag') != 'highway=speed_camera'):
        raise ValueError('Unexpected Armenian speed-camera provenance')
    try:
        dt.datetime.fromisoformat(catalog['osmDataThrough'].replace('Z', '+00:00'))
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ValueError('Invalid Armenian speed-camera snapshot date')
    rows = catalog.get('locations')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError('Invalid Armenian speed-camera catalog')
    ids = set()
    for row in rows:
        if (not isinstance(row, dict) or isinstance(row.get('osmNode'), bool)
                or not isinstance(row.get('osmNode'), int) or row['osmNode'] <= 0
                or row['osmNode'] in ids or row.get('locationKind') != 'osm_mapped_node'):
            raise ValueError('Invalid Armenian speed-camera node')
        for key, low, high in (('lon', ARMENIA_BOUNDS[0], ARMENIA_BOUNDS[2]),
                               ('lat', ARMENIA_BOUNDS[1], ARMENIA_BOUNDS[3])):
            value = row.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not low <= value <= high):
                raise ValueError('Armenian speed camera is outside the extract bounds')
        speed = row.get('mappedLimitKmh')
        directions = row.get('directions', [])
        if (speed is not None and (isinstance(speed, bool) or not isinstance(speed, int)
                                  or not 5 <= speed <= 150)):
            raise ValueError('Invalid mapped speed limit')
        if (not isinstance(directions, list) or len(directions) > 4
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or not 0 <= value < 360 for value in directions)):
            raise ValueError('Invalid mapped camera direction')
        ids.add(row['osmNode'])
    return catalog


def speed_cameras_for_bbox(bbox):
    west, south, east, north = bbox
    catalog = speed_camera_catalog()
    items = []
    for row in catalog['locations']:
        if not (west <= row['lon'] <= east and south <= row['lat'] <= north):
            continue
        detail = []
        if row.get('mappedLimitKmh') is not None:
            detail.append(f"Mapped limit {row['mappedLimitKmh']} km/h")
        if row.get('directions'):
            detail.append('Facing ' + ' / '.join(f'{value:g}°' for value in row['directions']))
        detail.extend(['Mapped location', 'Public video unavailable'])
        items.append({
            'type': 'node', 'id': row['osmNode'], 'lat': row['lat'], 'lon': row['lon'],
            'title': 'Mapped speed camera', 'detail': ' · '.join(detail),
            'source': f"OSM (ODbL) · {catalog['osmDataThrough'][:10]} snapshot · status unverified",
            'source_url': f"https://www.openstreetmap.org/node/{row['osmNode']}",
            'record_kind': 'mapped_speed_camera', 'location_kind': row['locationKind'],
        })
    return items
