"""Located references for Armenia's public road-camera inventory, without video."""

import functools
import json
import math
from pathlib import Path

SOURCE_URL = 'https://mia.gov.am/certificate/'
DOCUMENT_URL = ('https://mia.gov.am/wp-content/uploads/2026/05/'
                '%D4%BD%D5%A1%D5%B9%D5%B4%D5%A5%D6%80%D5%B8%D6%82%D5%AF%D5%B6%D5%A5%D6%80.docx')
DOCUMENT_MODIFIED = '2026-05-25T09:24:00Z'
CATALOG_PATH = Path(__file__).with_name('armenia-road-cameras.json')


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
