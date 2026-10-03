"""Build Tbilisi district references from saved public OSM relations and label nodes."""

import argparse
import hashlib
import json
import math
from pathlib import Path

DISTRICTS = {
    'Samgori District': 11300436, 'Nadzaladevi District': 11300438,
    'Didube District': 11300445, 'Saburtalo District': 11300446,
    'Vake District': 11300449, 'Isani District': 13438808,
    'Krtsanisi District': 13438809, 'Chugureti District': 13438810,
    'Mtatsminda District': 13438811, 'Gldani District': 13438812,
}


def build_references(relations, nodes):
    output = []
    for name, relation_id in DISTRICTS.items():
        matches = [r for r in relations if r['type'] == 'relation' and r['id'] == relation_id]
        if len(matches) != 1:
            raise ValueError('Unique Tbilisi district relation missing: ' + name)
        relation = matches[0]
        tags = relation['tags']
        if (tags.get('name:en') != name or tags.get('boundary') != 'administrative'
                or tags.get('admin_level') != '10' or not tags.get('name', '').endswith(' რაიონი')):
            raise ValueError('Tbilisi district identity changed: ' + name)
        labels = [m['ref'] for m in relation['members'] if m['type'] == 'node' and m['role'] == 'label']
        if len(labels) != 1:
            raise ValueError('Unique Tbilisi district label missing: ' + name)
        matches = [n for n in nodes if n['type'] == 'node' and n['id'] == labels[0]]
        if len(matches) != 1:
            raise ValueError('Tbilisi district label node missing: ' + name)
        node = matches[0]
        lon, lat = node['lon'], node['lat']
        if (node['tags'].get('name') != tags['name'] or node['tags'].get('name:en') != name
                or not all(math.isfinite(v) for v in (lon, lat))
                or not 44.6 <= lon <= 45.05 or not 41.55 <= lat <= 41.9):
            raise ValueError('Invalid Tbilisi district label: ' + name)
        output.append({'id': str(relation_id), 'name': name.removesuffix(' District'),
                       'heading': tags['name'], 'coordinates': [lon, lat], 'osmNode': node['id']})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relations', type=Path, required=True)
    parser.add_argument('--nodes', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'tbilisi-outage-locations.json')
    args = parser.parse_args()
    relations, nodes = json.loads(args.relations.read_text()), json.loads(args.nodes.read_text())
    data = {'source': 'https://www.openstreetmap.org/copyright', 'licence': 'ODbL 1.0',
            'dataThrough': nodes['osm3s']['timestamp_osm_base'],
            'relationsSha256': hashlib.sha256(args.relations.read_bytes()).hexdigest(),
            'nodesSha256': hashlib.sha256(args.nodes.read_bytes()).hexdigest(),
            'locationKind': 'approximate_district_reference',
            'locations': build_references(relations['elements'], nodes['elements'])}
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(data["locations"])} Tbilisi district references')


if __name__ == '__main__':
    main()
