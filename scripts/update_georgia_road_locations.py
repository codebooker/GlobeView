"""Build ODbL road references from OSM, checked against the department's named route."""

import argparse
import hashlib
import heapq
import json
import math
from pathlib import Path

from pyproj import Transformer

ROADMAP_URL = 'https://api.georoad.gov.ge/api/v2/roadmap'
ANCHORS = (11971967205, 12011876655)


def distance(a, b):
    x = math.radians(b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    return 6371000 * math.hypot(x, math.radians(b[1] - a[1]))


def build_reference(osm, roadmap):
    rows = roadmap['roadmap']['data']
    matches = [r for r in rows if r.get('id') == 88 and r.get('status') == 1]
    if len(matches) != 1:
        raise ValueError('Unique department road-map record missing')
    gis = json.loads(matches[0]['json_data'])
    if gis.get('spatialReference', {}).get('wkid') != 32638:
        raise ValueError('Department road-map projection changed')
    sections = [f for f in gis['features'] if f['attributes'].get('Section_Na') == 'Batumi bypass']
    if len(sections) != 1 or sections[0]['attributes'].get('FID') != 1:
        raise ValueError('Unique named Batumi bypass section missing')
    paths = sections[0]['geometry']['paths']
    if len(paths) != 1 or not 20 <= len(paths[0]) <= 5000:
        raise ValueError('Batumi bypass reference geometry changed')
    transform = Transformer.from_crs(32638, 4326, always_xy=True)
    reference = [list(transform.transform(*p)) for p in paths[0]]
    if not all(41.6 <= p[0] <= 41.75 and 41.57 <= p[1] <= 41.71 for p in reference):
        raise ValueError('Batumi bypass reference outside verified bounds')
    nodes, graph = {}, {}
    ways = [w for w in osm['elements'] if w.get('type') == 'way'
            and w.get('tags', {}).get('highway') == 'trunk']
    if len({w['id'] for w in ways}) != len(ways):
        raise ValueError('Duplicate OSM road way')
    for way in ways:
        ids, geometry = way['nodes'], way['geometry']
        if len(ids) != len(geometry) or len(ids) < 2:
            raise ValueError('OSM road geometry missing')
        coordinates = [[p['lon'], p['lat']] for p in geometry]
        if not all(math.isfinite(v) for p in coordinates for v in p):
            raise ValueError('Invalid OSM road coordinate')
        for node, point in zip(ids, coordinates):
            if node in nodes and nodes[node] != point:
                raise ValueError('Conflicting OSM road node')
            nodes[node] = point
        for a, b in zip(ids, ids[1:]):
            length = distance(nodes[a], nodes[b])
            graph.setdefault(a, []).append((b, length, way['id']))
            graph.setdefault(b, []).append((a, length, way['id']))
    start, end = ANCHORS
    if any(node not in nodes or distance(nodes[node], point) > 350
           for node, point in zip(ANCHORS, (reference[0], reference[-1]))):
        raise ValueError('Verified bypass end references missing or moved')
    # Constrain the graph to the named bypass corridor, excluding the older city route.
    eligible = {node for node, point in nodes.items()
                if min(distance(point, p) for p in reference) <= 350}
    queue, best, previous = [(0, start)], {start: 0}, {}
    while queue:
        length, node = heapq.heappop(queue)
        if length != best[node]:
            continue
        if node == end:
            break
        for next_node, step, way_id in graph.get(node, []):
            candidate = length + step
            if next_node in eligible and candidate < best.get(next_node, float('inf')):
                best[next_node], previous[next_node] = candidate, (node, way_id)
                heapq.heappush(queue, (candidate, next_node))
    if end not in best or not 11500 <= best[end] <= 15500:
        raise ValueError('Connected Batumi bypass road path missing')
    path, used_ways = [end], set()
    while path[-1] != start:
        node, way_id = previous[path[-1]]
        used_ways.add(way_id)
        path.append(node)
    path.reverse()
    midpoint = min(path, key=lambda node: abs(best[node] - best[end] / 2))
    return {'id': 'batumi-bypass', 'name': 'Batumi bypass', 'coordinates': nodes[midpoint],
            'osmNode': midpoint, 'osmWays': sorted(used_ways), 'osmEndNodes': list(ANCHORS),
            'roadSegments': [[nodes[node] for node in path]], 'lengthMetres': round(best[end]),
            'referenceSource': ROADMAP_URL, 'referenceRecord': 88, 'referenceFeature': 1,
            'referencePublication': matches[0]['publish_date']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--osm', type=Path, required=True)
    parser.add_argument('--roadmap', type=Path, required=True)
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'georgia-road-locations.json')
    args = parser.parse_args()
    osm, roadmap = json.loads(args.osm.read_text()), json.loads(args.roadmap.read_text())
    data = {'source': 'https://www.openstreetmap.org/copyright', 'licence': 'ODbL 1.0',
            'locationKind': 'approximate_named_road_reference',
            'dataThrough': osm['osm3s']['timestamp_osm_base'],
            'osmSha256': hashlib.sha256(args.osm.read_bytes()).hexdigest(),
            'roadmapSha256': hashlib.sha256(args.roadmap.read_bytes()).hexdigest(),
            'locations': [build_reference(osm, roadmap)]}
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print('Built one validated Batumi bypass reference and OSM path')


if __name__ == '__main__':
    main()
