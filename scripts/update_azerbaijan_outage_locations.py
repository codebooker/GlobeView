"""Build offline place references from GeoNames' public AZ.txt gazetteer.

Usage: python scripts/update_azerbaijan_outage_locations.py /path/to/AZ.txt
Download: https://download.geonames.org/export/dump/AZ.zip (CC BY 4.0).
No network requests or geocoding are performed by this script.
"""

import csv
import datetime as dt
import json
import pathlib
import re
import sys
import unicodedata


def normalize(value):
    value = value.lower().replace('ə', 'e').replace('ı', 'i')
    value = ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c))
    return ' '.join(value.split())


def build(path):
    with open(path, encoding='utf-8') as stream:
        rows = list(csv.reader(stream, delimiter='\t'))
    networks, locations = {}, []
    for row in rows:
        if len(row) != 19 or row[8] != 'AZ':
            raise ValueError('Expected the GeoNames Azerbaijan gazetteer')
        aliases = {normalize(v) for v in [row[1], row[2], *row[3].split(',')]}
        aliases = {v for v in aliases if len(v) >= 3 and re.fullmatch(r"[a-z][a-z '\-]*", v)}
        if row[7] == 'ADM1':
            for alias in aliases:
                base = re.sub(r'\s+(?:rayon(?:u)?|district|city|sahari|seheri|shahari|sehri)$', '', alias)
                networks.setdefault(base, set()).add(row[10])
        if row[7] in {'PPL', 'PPLA', 'PPLA2', 'PPLC', 'PPLX'} and aliases:
            locations.append({'id': row[0], 'name': row[1], 'kind': row[7], 'admin1': row[10],
                              'coordinates': [float(row[5]), float(row[4])], 'aliases': sorted(aliases)})
    # Utility districts in Baku share names with unrelated villages elsewhere.
    # Their scope is verified in the operator's published network directory.
    baku = ['Binəqədi', 'Nərimanov', 'Xətai', 'Qaradağ', 'Nəsimi', 'Xəzər', 'Nizami',
            'Yasamal', 'Sabunçu', 'Səbail', 'Suraxanı', 'Zirə', 'Maştağa', 'Biləcəri']
    for name in baku:
        networks[normalize(name)] = {'09'}
    # Named settlement networks retain the settlement's GeoNames district.
    networks['ceyranbatan'] = {'01'}
    networks['xudat'] = {'60'}
    return {'source': 'https://download.geonames.org/export/dump/AZ.zip',
            'license': 'CC BY 4.0', 'license_url': 'https://creativecommons.org/licenses/by/4.0/',
            'retrieved': dt.date.today().isoformat(),
            'network_scope_source': 'https://www.azerishiq.az/menu/unvanlar',
            'description': 'Named place references only; no customer outage boundaries. Unknown networks are omitted.',
            'networks': {key: sorted(value) for key, value in sorted(networks.items())},
            'locations': sorted(locations, key=lambda item: int(item['id']))}


if __name__ == '__main__':
    result = build(sys.argv[1])
    target = pathlib.Path(__file__).resolve().parent.parent / 'azerbaijan-outage-locations.json'
    target.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'{len(result["locations"])} place references written to {target.name}')
