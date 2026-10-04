"""Dated French Interior Ministry fixed-enforcement-camera inventory."""

import csv
import datetime as dt
import functools
import io
import math
import os
import re


SOURCE_URL = 'https://www.data.gouv.fr/datasets/liste-des-radars-fixes-en-france/'
CATALOG_PATH = os.path.join(os.path.dirname(__file__), 'france-fixed-radars-2025.csv')
PUBLISHED = dt.date(2025, 12, 30)
REGIONS = (
    (-5.5, 41, 10, 51.5),     # Metropolitan France
    (-63, 14, -60, 18),       # Guadeloupe and Martinique
    (-55, 2, -51, 6),         # French Guiana
    (54, -23, 56, -20),       # Réunion
)
RADAR_TYPES = {
    'ETF': 'Fixed speed camera',
    'ETD': 'Vehicle-class speed camera',
    'ETT': 'New-generation enforcement camera',
    'ETU': 'Urban enforcement camera',
    'ETVM': 'Average-speed camera',
    'ETFR': 'Red-light camera',
    'ETPN': 'Level-crossing camera',
}


def region_visible(bbox):
    west, south, east, north = bbox
    return any(west <= right and east >= left and south <= top and north >= bottom
               for left, bottom, right, top in REGIONS)


def parse_radars(content):
    rows = csv.DictReader(io.StringIO(content.decode('cp1252')), delimiter=';')
    if {name.strip() for name in rows.fieldnames or ()} != {
        'Numéro de radar', 'Type de radar', 'Date de mise en service', 'VMA', 'Latitude', 'Longitude'
    }:
        raise ValueError('French fixed-radar catalog has unexpected columns')
    radars, seen = [], set()
    for raw in rows:
        row = {key.strip(): (value or '').strip() for key, value in raw.items() if key}
        identifier = row['Numéro de radar']
        kind = row['Type de radar']
        if not re.fullmatch(r'[A-Za-z0-9]{1,20}', identifier) or identifier in seen or kind not in RADAR_TYPES:
            continue
        try:
            lat, lon = float(row['Latitude']), float(row['Longitude'])
            commissioned = dt.datetime.strptime(row['Date de mise en service'], '%d/%m/%Y %H:%M').date()
        except (TypeError, ValueError):
            continue
        if (not math.isfinite(lat) or not math.isfinite(lon) or commissioned > PUBLISHED
                or not region_visible((lon, lat, lon, lat))):
            continue
        speed_text = row['VMA']
        speed = int(speed_text) if speed_text.isdecimal() and 20 <= int(speed_text) <= 130 else None
        seen.add(identifier)
        radars.append((identifier, kind, lat, lon, commissioned.isoformat(), speed))
    if not radars:
        raise ValueError('French fixed-radar catalog has no usable sites')
    return radars


@functools.lru_cache(maxsize=1)
def _catalog():
    with open(CATALOG_PATH, 'rb') as source:
        return parse_radars(source.read())


def radars_for_bbox(bbox):
    west, south, east, north = bbox
    result = []
    for identifier, kind, lat, lon, commissioned, speed in _catalog():
        if not (west <= lon <= east and south <= lat <= north):
            continue
        detail = [f'{speed} km/h published limit' if speed else None,
                  f'commissioned {commissioned}', 'inventory dated 2025-12-30',
                  'current operation and plate reading unverified']
        result.append({
            'type': 'node', 'id': f'fr:interior:radar:{identifier}', 'lat': lat, 'lon': lon,
            'title': RADAR_TYPES[kind], 'detail': ' · '.join(part for part in detail if part),
            'source': 'Ministère de l’Intérieur · Licence Ouverte 2.0', 'source_url': SOURCE_URL,
        })
    return result
