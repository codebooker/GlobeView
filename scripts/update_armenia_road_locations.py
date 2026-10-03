"""Build approximate numbered-road references near Armenian settlements, offline."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re


def road_refs(value):
    value = value.translate(str.maketrans({'Մ': 'M', 'М': 'M', 'Հ': 'H', 'Տ': 'T'}))
    return sorted(set(re.findall(r'(?<!\w)([MHT]\s*[- ]?\s*\d+(?:-\d+)*)', value)))


def canonical(value):
    return re.sub(r'\s+', '', value).replace('-', '', 1)


def distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def join_locations(locations, nodes):
    grid = {}
    for node in nodes:
        x, y = node['coordinates']
        grid.setdefault((math.floor(x * 10), math.floor(y * 10)), []).append(node)
    output = []
    for place in locations:
        x, y = place['coordinates']
        cell = (math.floor(x * 10), math.floor(y * 10))
        roads = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for node in grid.get((cell[0] + dx, cell[1] + dy), []):
                    metres = distance(place['coordinates'], node['coordinates'])
                    if metres > 3000:
                        continue
                    for ref in node['refs']:
                        if ref not in roads or metres < roads[ref]['distanceMetres']:
                            roads[ref] = {'coordinates': node['coordinates'], 'osmNode': node['id'],
                                          'osmWay': node['way'], 'distanceMetres': metres}
        if roads:
            for row in roads.values():
                row['distanceMetres'] = round(row['distanceMetres'])
            output.append({**place, 'roads': dict(sorted(roads.items()))})
    return output


def read_nodes(path):
    import osmium  # Developer-only dependency; not needed by the app server.

    class Roads(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.nodes = {}

        def way(self, way):
            if way.tags.get('highway') not in {'trunk', 'primary', 'secondary', 'tertiary',
                                             'trunk_link', 'primary_link', 'secondary_link'}:
                return
            refs = {canonical(ref) for ref in road_refs(way.tags.get('ref', ''))}
            if not refs:
                return
            for node in way.nodes:
                if not node.location.valid():
                    continue
                lon, lat = node.lon, node.lat
                if not (43.4 <= lon <= 46.7 and 38.8 <= lat <= 41.4):
                    continue
                key = (node.ref, way.id)
                self.nodes[key] = {'id': node.ref, 'way': way.id, 'coordinates': [lon, lat],
                                   'refs': sorted(refs)}

    handler = Roads()
    handler.apply_file(str(path), locations=True)
    with osmium.io.Reader(str(path)) as reader:
        timestamp = reader.header().get('osmosis_replication_timestamp')
    return list(handler.nodes.values()), timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--osm-pbf', type=Path, required=True)
    parser.add_argument('--gazetteer', type=Path, default=Path(__file__).resolve().parents[1] / 'armenia-outage-locations.json')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'armenia-road-locations.json')
    args = parser.parse_args()
    gazetteer = json.loads(args.gazetteer.read_text())
    nodes, timestamp = read_nodes(args.osm_pbf)
    places = join_locations(gazetteer['locations'], nodes)
    data = {'osmSource': 'https://download.geofabrik.de/asia/armenia.html', 'osmLicence': 'ODbL 1.0',
            'osmDataThrough': timestamp, 'osmSha256': hashlib.sha256(args.osm_pbf.read_bytes()).hexdigest(),
            'gazetteerSource': 'https://download.geonames.org/export/dump/AM.zip',
            'gazetteerLicence': 'CC BY 4.0', 'gazetteerSha256': hashlib.sha256(args.gazetteer.read_bytes()).hexdigest(),
            'maxDistanceMetres': 3000, 'locationKind': 'approximate_numbered_road_reference', 'locations': places}
    args.output.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(f'{len(places)} settlements with {sum(len(p["roads"]) for p in places)} numbered-road references')


if __name__ == '__main__':
    main()
