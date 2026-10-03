"""Build Energo-Pro's offline named-place references from GeoNames GE.txt."""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    return ' '.join(value.replace('–', '-').replace('—', '-').split())


def georgian_aliases(row):
    return {normalize(a) for a in (row[1], *row[3].split(','))
            if re.fullmatch(r'[ა-ჰ][ა-ჰ\s]*', normalize(a))}


def _ring_contains(point, ring):
    x, y = point
    inside = False
    for a, b in zip(ring, ring[1:]):
        # Boundary touches do not establish a unique municipality.
        cross = (x - a[0]) * (b[1] - a[1]) - (y - a[1]) * (b[0] - a[0])
        if (abs(cross) < 1e-10 and min(a[0], b[0]) <= x <= max(a[0], b[0])
                and min(a[1], b[1]) <= y <= max(a[1], b[1])):
            return None
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def _polygon_contains(point, polygon):
    return _ring_contains(point, polygon[0]) is True and all(_ring_contains(point, hole) is False for hole in polygon[1:])


def add_boundary_scopes(places, scopes, boundaries, metadata):
    if (metadata.get('boundaryID') != 'GEO-ADM2-92138335' or metadata.get('boundaryISO') != 'GEO'
            or metadata.get('boundaryType') != 'ADM2' or metadata.get('boundaryYearRepresented') != '2007'
            or metadata.get('boundaryLicense') != 'Public Domain'):
        raise ValueError('Georgia reference-boundary provenance changed')
    aliases = {'Dedoplis Tskaro': '611448', 'Tetri Sqaro': '611676', 'Akhalgori': '613233',
               'Qvareli': '613582', 'Chkhorotsku': '615141', 'Tkibuli': '828313'}
    catalog_names = {s['name'].replace(' Municipality', '').casefold(): s['id'] for s in scopes.values()}
    shapes = []
    features = boundaries['features']
    if boundaries.get('type') != 'FeatureCollection' or len(features) != 68:
        raise ValueError('Georgia reference-boundary catalog changed')
    names = set()
    for feature in features:
        prop, geometry = feature['properties'], feature['geometry']
        name = prop['shapeName']
        if prop.get('shapeGroup') != 'GEO' or prop.get('shapeType') != 'ADM2' or name in names:
            raise ValueError('Duplicate or foreign municipality reference')
        names.add(name)
        scope_id = aliases.get(name) or catalog_names.get(name.casefold())
        if geometry['type'] not in {'Polygon', 'MultiPolygon'}:
            raise ValueError('Unsupported municipality geometry')
        polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
        if not polygons:
            raise ValueError('Empty municipality geometry')
        for polygon in polygons:
            if not polygon or any(len(ring) < 4 or ring[0] != ring[-1] for ring in polygon):
                raise ValueError('Open municipality reference ring')
            points = [p for ring in polygon for p in ring]
            if any(len(p) != 2 or not 39.8 <= p[0] <= 46.8 or not 41 <= p[1] <= 44 for p in points):
                raise ValueError('Invalid municipality geometry')
            bounds = (min(p[0] for p in points), min(p[1] for p in points),
                      max(p[0] for p in points), max(p[1] for p in points))
            if scope_id:
                # Validate every feature, but don't guess urban/old district joins.
                shapes.append((scope_id, bounds, polygon))
    for place in places:
        if place['admin2']:
            continue
        x, y = place['coordinates']
        matches = {sid for sid, (west, south, east, north), polygon in shapes
                   if west < x < east and south < y < north
                   and place['admin1'] in {scopes[sid]['admin1'], '00'} and _polygon_contains([x, y], polygon)}
        if len(matches) == 1:
            place['reference_admin2'] = next(iter(matches))


def build(path, cities, alerts, boundaries, metadata):
    raw = Path(path).read_bytes()
    rows = [line.split('\t') for line in raw.decode('utf-8').splitlines()]
    if any(len(r) != 19 or r[8] != 'GE' for r in rows):
        raise ValueError('Expected GeoNames Georgia gazetteer')
    by_id = {r[0]: r for r in rows}
    if len(by_id) != len(rows):
        raise ValueError('Duplicate gazetteer ID')
    # Missing Georgian aliases and urban administrative links are pinned to
    # named GeoNames records. No coordinates are generated or interpolated.
    additions = {'612126': ('Samtredia', '66', '612124', ['სამტრედია']),
                 '615607': ('Baghdati', '66', '613054', ['ბაღდათი']),
                 '615893': ('Akhalkalaki', '72', '615891', ['ახალქალაქი']),
                 '7667751': ('Kharagauli', '66', '614003', ['ხარაგაული']),
                 '7669163': ('Kareli', '73', '614130', ['ქარელი']),
                 '611674': ('Tetritsqaro', '68', '611676', ['თეთრიწყარო'])}
    city_links = {'615532': ('Batumi', '04', '13216140'),
                  '613607': ('Kutaisi', '66', '828310'),
                  '613971': ('Khelvachauri', '04', '613970'),
                  '615914': ('Akhalgori', '69', '613233')}
    place_aliases = {'610991': ('Zemo Alvani', '67', ['ზ.ალვანი']),
                     '613502': ('Kvemo Alvani', '67', ['ქვ.ალვანი']),
                     '612397': ('P’irveli Obcha', '66', ['i ობჩა']),
                     '613007': ('Meore Obcha', '66', ['ii ობჩა']),
                     '800900': ('Zemokheti', '65', ['ზემოხეთი']),
                     '612574': ('Giorgeti', '00', ['გიორგეთი'])}
    places, scopes = [], {}
    for r in rows:
        if r[7] == 'ADM2' and ('Municipality' in r[1] or r[0] in {v[2] for v in city_links.values()}):
            aliases = set()
            for a in georgian_aliases(r):
                root = re.sub(r'\s+(?:მუნიციპალიტეტი|რაიონი)$', '', a)
                aliases.update((a, root, root + ' რაიონი', root + ' რ-ნი'))
            scopes[r[0]] = {'id': r[0], 'name': r[1], 'admin1': r[10], 'aliases': aliases}
    for r in rows:
        if r[7] not in {'PPL', 'PPLA', 'PPLA2', 'PPLC'}:
            continue
        aliases, admin2 = georgian_aliases(r), r[11]
        if r[0] in place_aliases:
            name, province, extra = place_aliases[r[0]]
            if (r[1], r[10], r[7]) != (name, province, 'PPL'):
                raise ValueError('Named settlement spelling reference changed')
            aliases.update(extra)
        if r[0] in additions:
            name, province, district, extra = additions[r[0]]
            if (r[1], r[10], r[11]) != (name, province, district):
                raise ValueError('Named municipality centre changed')
            aliases.update(extra)
        if r[0] in city_links:
            name, province, district = city_links[r[0]]
            if (r[1], r[10]) != (name, province) or district not in scopes:
                raise ValueError('Named urban reference changed')
            admin2 = district
        if not aliases:
            continue
        item = {'id': r[0], 'name': r[1], 'admin1': r[10], 'admin2': admin2,
                'kind': r[7], 'aliases': sorted(aliases),
                'coordinates': [float(r[5]), float(r[4])]}
        places.append(item)
        if r[7] in {'PPLA', 'PPLA2'} and admin2 in scopes:
            scopes[admin2]['aliases'].update(aliases)
            # These are administrative centres, not affected locations unless
            # the source address explicitly names the town itself.
            scopes[admin2]['centre'] = r[0]
    # Utility district inflections absent from the gazetteer, pinned to exact
    # ADM2 records and already visible in the public affected-area strings.
    district_aliases = {'616020': ['აბაშის'], '615631': ['წყალტუბოს'],
                        '828313': ['ტყიბულის'], '613761': ['ქობულეთის'],
                        '613970': ['ხელვაჩაურის'], '612124': ['სამტრედიის'],
                        '7667581': ['თელავის'], '613341': ['ლაგოდეხის']}
    for district, names in district_aliases.items():
        for name in names:
            scopes[district]['aliases'].update((name + ' რაიონი', name + ' რ-ნი', name + ' მუნიციპალიტეტი'))
    scopes['tbilisi'] = {'id': 'tbilisi', 'name': 'Tbilisi', 'admin1': '51',
                         'aliases': {'თბილისი', 'ქალაქი თბილისი'}, 'centre': '611717'}
    for scope in scopes.values():
        scope['aliases'] = sorted(scope['aliases'])
    add_boundary_scopes(places, scopes, boundaries, metadata)
    city_rows = cities.get('data')
    source_rows = [r for result in alerts['results'].values() for r in (result.get('data') or [])]
    if not isinstance(city_rows, list) or cities.get('status') != 200 or alerts.get('errors'):
        raise ValueError('Public city/source audit incomplete')
    queries = {r['nameGe'] for r in city_rows if r.get('disabled') is False}
    queries.update(r['scName'] for r in source_rows)
    if not 1 <= len(queries) <= 100 or any(not re.fullmatch(r'[ა-ჰ\s-]{1,80}', q) for q in queries):
        raise ValueError('Unexpected public service-centre names')
    return {'source': 'https://download.geonames.org/export/dump/GE.zip', 'license': 'CC BY 4.0',
            'retrieved': dt.date.today().isoformat(), 'input_sha256': hashlib.sha256(raw).hexdigest(),
            'description': 'Approximate named settlements; not electricity footprints or equipment locations.',
            'query_source': 'https://my.energo-pro.ge/ow/#/disconns',
            'scope_source': metadata['gjDownloadURL'], 'scope_year': '2007',
            'scope_attribution': 'geoBoundaries / Wikimedia Commons (public-domain source)',
            'scope_input_sha256': hashlib.sha256(json.dumps(boundaries, sort_keys=True).encode()).hexdigest(),
            'queries': sorted(queries), 'scopes': sorted(scopes.values(), key=lambda s: s['id']),
            'places': sorted(places, key=lambda p: int(p['id']))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geonames', required=True)
    parser.add_argument('--cities', required=True)
    parser.add_argument('--alerts', required=True)
    parser.add_argument('--boundaries', required=True)
    parser.add_argument('--boundary-metadata', required=True)
    parser.add_argument('--output', default='georgia-outage-locations.json')
    args = parser.parse_args()
    data = build(args.geonames, json.loads(Path(args.cities).read_text()), json.loads(Path(args.alerts).read_text()),
                 json.loads(Path(args.boundaries).read_text()), json.loads(Path(args.boundary_metadata).read_text()))
    Path(args.output).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'{len(data["places"])} places, {len(data["scopes"])} administrative scopes, {len(data["queries"])} public searches')
