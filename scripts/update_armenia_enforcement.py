#!/usr/bin/env python3
"""Match the MIA junction inventory against an offline Geofabrik Armenia extract.

Use an isolated tools environment with osmium installed; production needs no
extra dependency. No viewer performs geocoding or downloads the source files.

    python scripts/update_armenia_enforcement.py --docx /tmp/junctions.docx \
        --osm-pbf /tmp/armenia.osm.pbf

Sources: https://mia.gov.am/certificate/ and
https://download.geofabrik.de/asia/armenia.html (OpenStreetMap, ODbL 1.0).
"""

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from armenia_enforcement import CATALOG_PATH, DOCUMENT_MODIFIED, DOCUMENT_URL, SOURCE_URL

_NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
       'dcterms': 'http://purl.org/dc/terms/'}
_UNITS = r'(?:փողոց(?:ների|ներ|ի|ն)?|պողոտա(?:ների|ներ|յի|ն)?|խճուղի(?:ների|ներ|ն)?|մայրուղի(?:ների|ներ|ն)?)'
_WORD_FORMS = {'խորենացու': 'խորենացի', 'հերացու': 'հերացի',
               'էրեբունու': 'էրեբունի', 'րաֆֆու': 'րաֆֆի',
               'դավթի': 'դավիթ', 'լուսավորչի': 'լուսավորիչ',
               'ուլնեցու': 'ուլնեցի', 'քանաքեռցու': 'քանաքեռցի'}
# Spelling variants in this ministry inventory, retaining all personal initials.
_INVENTORY_SPELLINGS = {'զաքիյան': 'զաքյան', 'բյուզանդ': 'բուզանդ',
                      'գյուբենկյան': 'գյուլբենկյան'}


def normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold().replace('և', 'եւ')
    return re.sub(r'[^ա-ֆ0-9]+', ' ', value).strip()


def word_root(value):
    value = _WORD_FORMS.get(value, value)
    if value.endswith('սկու'):
        value = value[:-2] + 'ի'
    if value.endswith(('այի', 'ոյի')):
        value = value[:-2]
    if value.endswith('իի'):
        value = value[:-1]
    value = value[:-1] if len(value) > 3 and value.endswith('ի') else value
    return _INVENTORY_SPELLINGS.get(value, value)


def street_descriptor(value):
    """Retain personal-name qualifiers so matching surnames cannot swap roads."""
    value = value.casefold().replace('և', 'եւ')
    initials = re.findall(r'\b([ա-ֆ]{1,2})\.', value)
    value = re.sub(r'\b[ա-ֆ]{1,2}\.', '', value)
    value = re.sub(_UNITS, '', value)
    value = re.sub(r'(\d+)[-–](?:ին|րդ|ի)\b', r'\1', value)
    parts = normalize(value).split()
    if not parts:
        return '', []
    if len(parts) >= 2 and parts[-2:] in (['սայաթ', 'նովա'], ['սայաթ', 'նովայի']):
        return 'սայաթ նովա', initials
    if parts[-1].isdigit():
        return (' '.join([word_root(parts[-2]), parts[-1]]) if len(parts) > 1 else ''), initials or parts[:-2]
    return word_root(parts[-1]), initials or parts[:-1]


def address_streets(address):
    address = address.casefold().replace('և', 'եւ')
    address = re.sub(r'սայաթ[-–]նովա', 'սայաթ նովա', address)
    address = re.sub(r'(\d+)[-–](?:ին|րդ|ի)\b', r'\1', address)
    address = re.split(r'\s+խաչմերուկ', address)[0]
    return [street_descriptor(part) for part in re.split('[-–]', address)]


def qualified_road(descriptor, road_name):
    key, qualifiers = descriptor
    road_key, road_qualifiers = street_descriptor(road_name)
    if key != road_key:
        return False
    if not qualifiers:
        return True
    # The ministry abbreviates Admiral Isakov with Ծ. (naval/admiral rank),
    # whereas OSM spells the rank Ադմիրալ. Require that exact road name.
    if qualifiers == ['ծ'] and key == 'իսակով':
        return normalize(road_name) == 'ադմիրալ իսակովի պողոտա'
    if len(qualifiers) != len(road_qualifiers):
        return False
    return all(normalize(actual).startswith(normalize(expected))
               for expected, actual in zip(qualifiers, road_qualifiers))


def road_names(road):
    """Use only explicitly mapped Armenian names, including recorded renamings."""
    names = set()
    for key in ('name', 'name:hy', 'official_name', 'alt_name', 'old_name'):
        for name in road['tags'].get(key, '').split(';'):
            name = name.strip()
            if (re.search(_UNITS + '|հրապարակ', name)
                    and not any(part in name for part in ('նրբանցք', 'փակուղի', 'անցում', 'աստիճան', 'թաղամաս'))):
                names.add(name)
    return sorted(names)


def distinct_ways(groups, used=frozenset()):
    """Every published road must have its own way at the shared junction."""
    if not groups:
        return True
    return any(distinct_ways(groups[1:], used | {way})
               for way in groups[0] - used)


def read_inventory(path):
    with zipfile.ZipFile(path) as archive:
        for name in ('word/document.xml', 'docProps/core.xml'):
            if archive.getinfo(name).file_size > 4 * 1024 * 1024:
                raise ValueError('Oversized inventory document')
        core = ET.fromstring(archive.read('docProps/core.xml'))
        modified = core.find('dcterms:modified', _NS)
        if modified is None or modified.text != DOCUMENT_MODIFIED:
            raise ValueError('Inventory document changed; review it before updating the catalog')
        root = ET.fromstring(archive.read('word/document.xml'))
    lines = [''.join(p.itertext()) for p in root.findall('.//w:p', _NS)]
    # itertext includes only XML content, not numbering generated by Word.
    if not lines or 'Երևան քաղաքի խաչմերուկներում գործող տեսադիտարկման սարքերի ցանկ' not in lines[0]:
        raise ValueError('Input is not the Yerevan road-camera inventory')
    entries = [line.strip() for line in lines if 'տեսախցիկ' in line]
    if len(entries) != 158:
        raise ValueError('Inventory entry count changed')
    return entries


def read_osm_roads(path):
    import osmium  # developer-only tools dependency

    class Roads(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.rows = []

        def way(self, way):
            if not way.tags.get('highway') or not way.tags.get('name'):
                return
            if way.tags['highway'] in ('service', 'path', 'footway', 'steps', 'track'):
                return
            if not road_names({'tags': dict(way.tags)}):
                return
            nodes = [{'id': n.ref, 'lat': n.lat, 'lon': n.lon}
                     for n in way.nodes if n.location.valid()]
            if nodes and any(40.08 <= n['lat'] <= 40.28 and 44.40 <= n['lon'] <= 44.63 for n in nodes):
                self.rows.append({'id': way.id, 'tags': dict(way.tags), 'nodes': nodes})

    reader = osmium.io.Reader(str(path))
    data_through = reader.header().get('osmosis_replication_timestamp')
    reader.close()
    if not data_through:
        raise ValueError('OSM extract has no data timestamp')
    handler = Roads()
    handler.apply_file(str(path), locations=True)
    return handler.rows, data_through


def match_inventory(entries, roads):
    by_name = collections.defaultdict(list)
    for road in roads:
        for name in road_names(road):
            by_name[street_descriptor(name)[0]].append((road, name))
    locations, omitted = [], []
    for index, line in enumerate(entries, 1):
        address = line.split('(')[0].strip()
        reason = None
        if 'անշարժ' not in line:
            reason = 'No fixed camera explicitly listed'
        elif 'խաչմերուկ' not in address or any(
                word in address for word in ('ճանապարհահատված', 'հիվանդանոց', 'դպրոց')):
            reason = 'Not solely a named junction'
        parts = address_streets(address) if reason is None else []
        keys = [part[0] for part in parts]
        if reason is None and (len(keys) < 2 or len(set(keys)) != len(keys) or not all(keys)):
            reason = 'Unrecognized junction address'
        matched = [[(road, name) for road, name in by_name[key] if qualified_road(part, name)]
                   for key, part in zip(keys, parts)] if reason is None else []
        if reason is None and not all(matched):
            reason = 'An explicitly named road is unmatched'
        candidates = {}
        if reason is None:
            for key, rows in zip(keys, matched):
                for road, matched_name in rows:
                    for node in road['nodes']:
                        if not (40.08 <= node['lat'] <= 40.28 and 44.40 <= node['lon'] <= 44.63):
                            continue
                        item = candidates.setdefault(node['id'], {
                            **node, 'keys': set(), 'ways': set(), 'names': set(), 'matched_names': set(),
                            'key_ways': collections.defaultdict(set)})
                        item['keys'].add(key)
                        item['ways'].add(road['id'])
                        item['key_ways'][key].add(road['id'])
                        item['names'].add(road['tags'].get('name:en') or road['tags']['name'])
                        item['matched_names'].add(matched_name)
            candidates = [item for item in candidates.values() if len(item['keys']) == len(keys)
                          and distinct_ways([item['key_ways'][key] for key in keys])]
            if not candidates:
                reason = 'Named roads have no shared junction node'
        spread = 0
        if reason is None:
            spread = max(math.hypot((a['lon'] - b['lon']) * 85000, (a['lat'] - b['lat']) * 111000)
                         for a in candidates for b in candidates)
            if spread > 100:
                reason = 'Multiple or extended junction matches'
        if reason is not None:
            omitted.append({'sourceIndex': index, 'sourceAddress': address, 'reason': reason})
            continue
        lat = sum(item['lat'] for item in candidates) / len(candidates)
        lon = sum(item['lon'] for item in candidates) / len(candidates)
        node = min(candidates, key=lambda item: (item['lat'] - lat) ** 2 + (item['lon'] - lon) ** 2)
        locations.append({
            'id': f'am:mia:surveillance:{index}', 'sourceIndex': index,
            'sourceAddress': address, 'roadNames': sorted(node['names']),
            'lat': node['lat'], 'lon': node['lon'], 'locationKind': 'junction_reference',
            'osmNode': node['id'], 'osmWays': sorted(node['ways']),
            'matchedRoadNames': sorted(node['matched_names']),
            'matchSpreadMetres': round(spread),
        })
    return locations, omitted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docx', type=Path, required=True)
    parser.add_argument('--osm-pbf', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=CATALOG_PATH)
    args = parser.parse_args()
    entries = read_inventory(args.docx)
    roads, timestamp = read_osm_roads(args.osm_pbf)
    locations, omitted = match_inventory(entries, roads)
    catalog = {
        'sourceUrl': SOURCE_URL, 'documentUrl': DOCUMENT_URL,
        'documentModified': DOCUMENT_MODIFIED,
        'documentSha256': hashlib.sha256(args.docx.read_bytes()).hexdigest(),
        'checkedDate': dt.datetime.now(dt.timezone.utc).date().isoformat(),
        'osmSource': 'https://download.geofabrik.de/asia/armenia.html',
        'osmDataThrough': timestamp, 'osmLicence': 'ODbL 1.0',
        'sourceEntries': len(entries), 'locations': locations, 'omitted': omitted,
    }
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(locations)} verified junction references; {len(omitted)} entries need further review')


if __name__ == '__main__':
    main()
