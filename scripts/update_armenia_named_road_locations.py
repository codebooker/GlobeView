"""Build verified named-road references for Armenian construction reports, offline."""

import argparse
import hashlib
import json
from pathlib import Path

from scripts.update_armenia_road_locations import distance


def build_references(ways, landmarks, places):
    gyumri = next(p for p in places if p['id'] == '616635' and p['name'] == 'Gyumri')
    candidates = []
    for way in ways:
        if way['tags'].get('name') != 'Շահումյան փողոց':
            continue
        for node in way['nodes']:
            metres = distance(gyumri['coordinates'], node['coordinates'])
            if metres <= 3000:
                candidates.append((metres, node['id'], way['id'], node['coordinates']))
    if not candidates:
        raise ValueError('Gyumri Shahumyan Street reference missing')
    metres, node_id, way_id, coordinates = min(candidates)
    output = [{'id': 'gyumri-shahumyan', 'name': 'Shahumyan Street · Gyumri',
               'aliases': ['Շահումյան փողոց'], 'context': ['Գյումր'],
               'coordinates': coordinates, 'osmNode': node_id, 'osmWays': [way_id],
               'geonamesId': gyumri['id'], 'referenceDistanceMetres': round(metres)}]
    passes = [n for n in landmarks if n['tags'].get('mountain_pass') == 'yes'
              and n['tags'].get('name') == 'Պուշկինի լեռնանցք']
    if len(passes) != 1:
        raise ValueError('Unique mapped Pushkin Pass missing')
    landmark = passes[0]
    memberships = [w['id'] for w in ways if any(n['id'] == landmark['id'] for n in w['nodes'])]
    if not memberships:
        raise ValueError('Mapped pass does not lie on a road node')
    output.append({'id': 'pushkin-pass', 'name': 'Pushkin Pass',
                   'aliases': ['Պուշկինի լեռնանցք', 'Պուշկինյան լեռնանցք'], 'context': [],
                   'coordinates': landmark['coordinates'], 'osmNode': landmark['id'],
                   'osmWays': sorted(memberships), 'referenceDistanceMetres': 0})
    return output


def read_osm(path):
    import osmium  # Developer-only dependency; production uses the saved JSON.

    class Roads(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.ways, self.landmarks = [], []

        def node(self, node):
            tags = dict(node.tags)
            if tags.get('mountain_pass') == 'yes' and tags.get('name') == 'Պուշկինի լեռնանցք':
                self.landmarks.append({'id': node.id, 'tags': tags,
                                       'coordinates': [node.location.lon, node.location.lat]})

        def way(self, way):
            tags = dict(way.tags)
            if not tags.get('highway') or tags['highway'] in {'footway', 'path', 'steps', 'cycleway', 'proposed'}:
                return
            pass_ids = {n['id'] for n in self.landmarks}
            if tags.get('name') != 'Շահումյան փողոց' and not any(n.ref in pass_ids for n in way.nodes):
                return
            nodes = [{'id': n.ref, 'coordinates': [n.lon, n.lat]} for n in way.nodes if n.location.valid()]
            self.ways.append({'id': way.id, 'tags': tags, 'nodes': nodes})

    roads = Roads()
    roads.apply_file(str(path), locations=True)
    with osmium.io.Reader(str(path)) as reader:
        timestamp = reader.header().get('osmosis_replication_timestamp')
    return roads.ways, roads.landmarks, timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--osm-pbf', type=Path, required=True)
    parser.add_argument('--gazetteer', type=Path, default=Path(__file__).resolve().parents[1] / 'armenia-outage-locations.json')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'armenia-named-road-locations.json')
    args = parser.parse_args()
    ways, landmarks, timestamp = read_osm(args.osm_pbf)
    data = {'osmSource': 'https://download.geofabrik.de/asia/armenia.html', 'osmLicence': 'ODbL 1.0',
            'osmDataThrough': timestamp, 'osmSha256': hashlib.sha256(args.osm_pbf.read_bytes()).hexdigest(),
            'gazetteerSource': 'https://download.geonames.org/export/dump/AM.zip', 'gazetteerLicence': 'CC BY 4.0',
            'gazetteerSha256': hashlib.sha256(args.gazetteer.read_bytes()).hexdigest(),
            'locationKind': 'approximate_named_road_reference',
            'locations': build_references(ways, landmarks, json.loads(args.gazetteer.read_text())['locations'])}
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(data["locations"])} named-road references')


if __name__ == '__main__':
    main()
