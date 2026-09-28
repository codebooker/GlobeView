"""Published French free-flow toll gantries, shown as plate-reader locations."""

import csv
import functools
import io
import math
import os


SOURCE_URL = 'https://www.data.gouv.fr/datasets/autoroutes-et-peages-en-flux-libre'
CATALOG_PATH = os.path.join(os.path.dirname(__file__), 'france-free-flow-gantries.csv')


def parse_gantries(content):
    rows = csv.DictReader(io.StringIO(content), delimiter=';')
    if not {'Nom', 'Coordonnées GPS', 'Autoroute', 'Type'}.issubset(rows.fieldnames or ()):
        raise ValueError('French toll-gantry catalog has unexpected columns')
    gantries = []
    seen = set()
    for row in rows:
        if row.get('Type', '').strip() != 'Flux libre':
            continue
        try:
            lat_text, lon_text = row['Coordonnées GPS'].split(',', 1)
            lat, lon = float(lat_text), float(lon_text)
        except (KeyError, ValueError, TypeError):
            continue
        if (not math.isfinite(lat) or not math.isfinite(lon)
                or not (41 <= lat <= 51.5 and -5.5 <= lon <= 10)):
            continue
        name = ' '.join((row.get('Nom') or '').split())[:100]
        road = ' '.join((row.get('Autoroute') or '').split())[:20]
        if not name or not road or (name, lat, lon) in seen:
            continue
        seen.add((name, lat, lon))
        gantries.append((len(gantries) + 1, name, road, lat, lon))
    if not gantries:
        raise ValueError('French toll-gantry catalog has no usable sites')
    return gantries


@functools.lru_cache(maxsize=1)
def _catalog():
    with open(CATALOG_PATH, encoding='utf-8') as source:
        return parse_gantries(source.read())


def gantries_for_bbox(bbox):
    min_lon, min_lat, max_lon, max_lat = bbox
    return [{
        'type': 'node', 'id': f'fr:freeflow:{site_id}', 'lat': lat, 'lon': lon,
        'title': f'Plate reader · {name}',
        'detail': (f'{road} free-flow toll gantry · published October 2025; '
                   'current operation unverified. Follow road signs and pay applicable tolls.'),
        'source': 'Maxime Lopes · French free-flow toll inventory',
        'source_url': SOURCE_URL,
    } for site_id, name, road, lat, lon in _catalog()
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat]
