#!/usr/bin/env python3
"""Build Georgian OSM enforcement references from a downloaded PBF and .poly.

    uv run --with osmium --with shapely python -m scripts.update_georgia_enforcement \
        --osm-pbf /tmp/georgia.osm.pbf --boundary /tmp/georgia.poly

Pyosmium and Shapely are developer-only dependencies. The published extract
boundary filters original points; camera positions are never interpolated.
"""

import argparse
import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path

from georgia_enforcement import BOUNDARY_URL, CATALOG_PATH, GEORGIA_BOUNDS, SOURCE_URL


def enforcement_record(node):
    tags = node.get('tags', {})
    speed_camera = tags.get('highway') == 'speed_camera'
    plate_reader = bool({'ALPR', 'ANPR'} & {
        value.strip().upper() for value in tags.get('surveillance:type', '').split(';')})
    if not speed_camera and not (tags.get('man_made') == 'surveillance' and plate_reader):
        return None, None
    if (tags.get('fixme') or any('fixme' in str(value).casefold()
                                for key, value in tags.items() if key.startswith('name'))
            or any(tags.get(key) == 'yes' for key in ('disused', 'abandoned', 'demolished', 'removed'))
            or any(tags.get(f'{prefix}:highway') == 'speed_camera'
                   for prefix in ('disused', 'abandoned', 'demolished', 'removed'))):
        return None, 'Position or equipment status flagged for review'
    osm_id, lat, lon = node.get('id'), node.get('lat'), node.get('lon')
    if (isinstance(osm_id, bool) or not isinstance(osm_id, int) or osm_id <= 0
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in (lat, lon))
            or not GEORGIA_BOUNDS[0] <= lon <= GEORGIA_BOUNDS[2]
            or not GEORGIA_BOUNDS[1] <= lat <= GEORGIA_BOUNDS[3]):
        return None, 'Invalid or out-of-extract position'
    kinds = (['speed_camera'] if speed_camera else []) + (['plate_reader'] if plate_reader else [])
    speed = tags.get('maxspeed', '')
    speed = (int(speed) if speed_camera and re.fullmatch(r'\d{1,3}', speed)
             and 5 <= int(speed) <= 150 else None)
    direction = tags.get('direction', '')
    directions = []
    if re.fullmatch(r'\d+(?:\.\d+)?(?:;\d+(?:\.\d+)?){0,3}', direction):
        values = [float(value) for value in direction.split(';')]
        if all(0 <= value <= 360 for value in values):
            directions = sorted(set(value % 360 for value in values))
    return {'osmNode': osm_id, 'lat': lat, 'lon': lon, 'locationKind': 'osm_mapped_node',
            'mappedKinds': kinds, 'mappedLimitKmh': speed, 'directions': directions}, None


def extract_boundary(text):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    lines = text.splitlines()
    index, outer, holes = 1, [], []
    while index < len(lines) and lines[index].strip() != 'END':
        label = lines[index].strip()
        if not label:
            raise ValueError('Invalid extract boundary ring')
        index += 1
        ring = []
        while index < len(lines) and lines[index].strip() != 'END':
            values = [float(value) for value in lines[index].split()]
            if len(values) != 2 or not all(math.isfinite(value) for value in values):
                raise ValueError('Invalid extract boundary coordinate')
            ring.append(values)
            index += 1
        if index >= len(lines) or len(ring) < 4 or ring[0] != ring[-1]:
            raise ValueError('Incomplete extract boundary ring')
        polygon = Polygon(ring)
        if not polygon.is_valid or polygon.is_empty:
            raise ValueError('Invalid extract boundary polygon')
        (holes if label.startswith('!') else outer).append(polygon)
        index += 1
    if index >= len(lines) or not outer:
        raise ValueError('Incomplete extract boundary')
    area = unary_union(outer).difference(unary_union(holes))
    west, south, east, north = area.bounds
    if (area.is_empty or not area.is_valid or area.area < 1
            or west < GEORGIA_BOUNDS[0] or south < GEORGIA_BOUNDS[1]
            or east > GEORGIA_BOUNDS[2] or north > GEORGIA_BOUNDS[3]):
        raise ValueError('Unexpected Georgian extract boundary')
    return area


def read_enforcement(path, area):
    import osmium
    from shapely.geometry import Point

    reader = osmium.io.Reader(str(path))
    timestamp = reader.header().get('osmosis_replication_timestamp')
    reader.close()
    if not timestamp:
        raise ValueError('OSM extract has no data timestamp')

    class Cameras(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.locations, self.omitted, self.count = [], [], 0

        def node(self, node):
            if (node.tags.get('highway') != 'speed_camera'
                    and node.tags.get('man_made') != 'surveillance'):
                return
            record, reason = enforcement_record({
                'id': node.id, 'lat': node.location.lat if node.location.valid() else None,
                'lon': node.location.lon if node.location.valid() else None, 'tags': dict(node.tags)})
            if record is None and reason is None:
                return
            self.count += 1
            if record and not area.covers(Point(record['lon'], record['lat'])):
                record, reason = None, 'Outside the published extract boundary'
            if record:
                self.locations.append(record)
            else:
                self.omitted.append({'osmNode': node.id, 'reason': reason})

    handler = Cameras()
    handler.apply_file(str(path))
    return handler, timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--osm-pbf', type=Path, required=True)
    parser.add_argument('--boundary', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=CATALOG_PATH)
    args = parser.parse_args()
    area = extract_boundary(args.boundary.read_text())
    cameras, timestamp = read_enforcement(args.osm_pbf, area)
    if not cameras.locations:
        raise ValueError('No mapped enforcement nodes inside Georgia')
    catalog = {
        'sourceUrl': SOURCE_URL, 'boundarySource': BOUNDARY_URL, 'osmLicence': 'ODbL 1.0',
        'osmDataThrough': timestamp, 'checkedDate': dt.datetime.now(dt.timezone.utc).date().isoformat(),
        'osmInputSha256': hashlib.sha256(args.osm_pbf.read_bytes()).hexdigest(),
        'boundaryInputSha256': hashlib.sha256(args.boundary.read_bytes()).hexdigest(),
        'boundaryBounds': list(area.bounds), 'sourceNodes': cameras.count,
        'locations': sorted(cameras.locations, key=lambda row: row['osmNode']), 'omitted': cameras.omitted,
    }
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(cameras.locations)} mapped enforcement nodes; {len(cameras.omitted)} need review')


if __name__ == '__main__':
    main()
