#!/usr/bin/env python3
"""Build the OSM-mapped Armenia speed-camera catalog from an offline extract.

    python scripts/update_armenia_speed_cameras.py --osm-pbf /tmp/armenia.osm.pbf

Pyosmium is a developer-only dependency. Map viewers never download the extract
or perform geocoding. OSM tags establish mapped equipment, not current operation.
"""

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from armenia_enforcement import ARMENIA_BOUNDS, OSM_SOURCE_URL, SPEED_CATALOG_PATH


def speed_camera_record(node):
    tags = node.get('tags', {})
    if tags.get('highway') != 'speed_camera':
        return None, None
    if (tags.get('fixme') or any('fixme' in str(value).casefold()
                                for key, value in tags.items() if key.startswith('name'))
            or tags.get('disused') == 'yes' or tags.get('abandoned') == 'yes'):
        return None, 'Position or equipment status flagged for review'
    osm_id, lat, lon = node.get('id'), node.get('lat'), node.get('lon')
    if (isinstance(osm_id, bool) or not isinstance(osm_id, int) or osm_id <= 0
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in (lat, lon))
            or not ARMENIA_BOUNDS[0] <= lon <= ARMENIA_BOUNDS[2]
            or not ARMENIA_BOUNDS[1] <= lat <= ARMENIA_BOUNDS[3]):
        return None, 'Invalid or out-of-extract position'
    speed = tags.get('maxspeed', '')
    speed = int(speed) if re.fullmatch(r'\d{1,3}', speed) and 5 <= int(speed) <= 150 else None
    direction = tags.get('direction', '')
    directions = []
    if re.fullmatch(r'\d+(?:\.\d+)?(?:;\d+(?:\.\d+)?){0,3}', direction):
        values = [float(value) for value in direction.split(';')]
        if all(0 <= value <= 360 for value in values):
            directions = sorted(set(value % 360 for value in values))
    return {'osmNode': osm_id, 'lat': lat, 'lon': lon, 'locationKind': 'osm_mapped_node',
            'mappedLimitKmh': speed, 'directions': directions}, None


def read_speed_cameras(path):
    import osmium

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
            if node.tags.get('highway') != 'speed_camera':
                return
            self.count += 1
            record, reason = speed_camera_record({
                'id': node.id, 'lat': node.location.lat if node.location.valid() else None,
                'lon': node.location.lon if node.location.valid() else None, 'tags': dict(node.tags)})
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
    parser.add_argument('--output', type=Path, default=SPEED_CATALOG_PATH)
    args = parser.parse_args()
    cameras, timestamp = read_speed_cameras(args.osm_pbf)
    catalog = {
        'sourceUrl': OSM_SOURCE_URL, 'osmDataThrough': timestamp, 'osmLicence': 'ODbL 1.0',
        'tag': 'highway=speed_camera', 'checkedDate': dt.datetime.now(dt.timezone.utc).date().isoformat(),
        'osmInputSha256': hashlib.sha256(args.osm_pbf.read_bytes()).hexdigest(),
        'sourceNodes': cameras.count, 'locations': sorted(cameras.locations, key=lambda row: row['osmNode']),
        'omitted': cameras.omitted,
    }
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(cameras.locations)} mapped speed cameras; {len(cameras.omitted)} nodes need review')


if __name__ == '__main__':
    main()
