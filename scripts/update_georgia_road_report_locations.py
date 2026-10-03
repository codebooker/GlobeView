"""Build a named road-report point from an offline OSM node/way/route export."""

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

NODE, WAY, RELATION = 824101262, 779659168, 19687627
IDENTITY_SOURCE = 'https://georgia.travel/the-most-beautiful-road-passes-of-georgia'


def build_reference(osm):
    rows = osm['elements']
    if len({(r['type'], r['id']) for r in rows}) != len(rows):
        raise ValueError('Duplicate OSM road-report entity')
    def one(kind, osm_id):
        matches = [r for r in rows if r['type'] == kind and r['id'] == osm_id]
        if len(matches) != 1:
            raise ValueError('Verified OSM road-report entity missing')
        return matches[0]
    node, way, route = one('node', NODE), one('way', WAY), one('relation', RELATION)
    tags = node['tags']
    point = [node['lon'], node['lat']]
    if (tags.get('name:en') != 'Abano Pass' or tags.get('mountain_pass') != 'yes'
            or tags.get('natural') != 'saddle' or tags.get('fixme')
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point)
            or not 45.49 <= point[0] <= 45.53 or not 42.26 <= point[1] <= 42.30):
        raise ValueError('Abano Pass identity or point changed')
    if (way['tags'].get('highway') != 'tertiary' or way['tags'].get('ref') != 'შ 44'
            or NODE not in way['nodes'] or route['tags'].get('route') != 'road'
            or route['tags'].get('name:en') != 'Pshaveli-Abano-Omalo'
            or not any(m.get('type') == 'way' and m.get('ref') == WAY for m in route['members'])):
        raise ValueError('Pass is not on the verified named road')
    stamp = dt.datetime.fromisoformat(osm['osm3s']['timestamp_osm_base'].replace('Z', '+00:00'))
    if stamp.utcoffset() != dt.timedelta(0):
        raise ValueError('OSM timestamp must be UTC')
    return {'id': 'pshaveli-abano-omalo', 'name': 'Pshaveli–Abano–Omalo · Abano Pass',
            'coordinates': point, 'osmNode': NODE, 'osmRoadWay': WAY, 'osmRoadRelation': RELATION,
            'identitySource': IDENTITY_SOURCE,
            'referenceNote': 'Named pass on the road; not the published kilometre limits'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--osm', type=Path, required=True)
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'georgia-road-report-locations.json')
    args = parser.parse_args()
    raw = args.osm.read_bytes()
    osm = json.loads(raw)
    data = {'source': 'https://www.openstreetmap.org/copyright', 'licence': 'ODbL 1.0',
            'locationKind': 'approximate_named_road_reference',
            'dataThrough': osm['osm3s']['timestamp_osm_base'], 'osmSha256': hashlib.sha256(raw).hexdigest(),
            'locations': [build_reference(osm)]}
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Verified Abano Pass node on the Pshaveli–Abano–Omalo road')


if __name__ == '__main__':
    main()
