#!/usr/bin/env python3
"""Rebuild the fixed January 2025 IIV inventory from downloaded primary sources.

    python3 scripts/update_uzbek_enforcement.py --html /tmp/iiv.html \
        --boundary /tmp/uzbekistan-nominatim.json

HTML: https://gov.uz/oz/iiv/news/view/34435
Boundary: Nominatim search for Uzbekistan with polygon_geojson=1, limit=1.
The boundary is only a validation filter; all device coordinates come from IIV.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uzbek_enforcement import CATALOG_PATH, SOURCE_DATE, SOURCE_URL, parse_inventory


def point_in_ring(point, ring):
    x, y = point
    inside = False
    for first, second in zip(ring, ring[1:] + ring[:1]):
        if ((first[1] > y) != (second[1] > y)
                and x < (second[0] - first[0]) * (y - first[1])
                / (second[1] - first[1]) + first[0]):
            inside = not inside
    return inside


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', required=True, type=Path)
    parser.add_argument('--boundary', required=True, type=Path)
    args = parser.parse_args()
    html = args.html.read_text(encoding='utf-8')
    if '2025-yilning yanvar' not in html or SOURCE_DATE not in html:
        raise ValueError('Input is not the January 2025 IIV inventory')
    country = json.loads(args.boundary.read_text(encoding='utf-8'))
    if (len(country) != 1 or country[0].get('osm_type') != 'relation'
            or country[0].get('osm_id') != 196240
            or country[0].get('geojson', {}).get('type') != 'MultiPolygon'):
        raise ValueError('Expected the Uzbekistan national boundary, OSM relation 196240')
    polygons = country[0]['geojson']['coordinates']
    locations, rejected = parse_inventory(html)
    verified = [item for item in locations if any(
        point_in_ring((item['lon'], item['lat']), polygon[0])
        and not any(point_in_ring((item['lon'], item['lat']), hole)
                    for hole in polygon[1:]) for polygon in polygons)]
    if not verified:
        raise ValueError('No inventory locations are inside Uzbekistan')
    catalog = {
        'sourceUrl': SOURCE_URL, 'sourceDate': SOURCE_DATE,
        'checkedDate': dt.datetime.now(dt.timezone.utc).date().isoformat(),
        'boundarySource': 'https://www.openstreetmap.org/relation/196240',
        'rejectedRows': rejected, 'outsideBoundary': len(locations) - len(verified),
        'locations': verified,
    }
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8')
    print(f'{len(verified)} unique locations; {rejected} malformed rows; '
          f'{len(locations) - len(verified)} outside the national boundary')


if __name__ == '__main__':
    main()
