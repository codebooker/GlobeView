"""Published Uzbek enforcement locations; inventory only, without public video."""

import functools
import hashlib
import json
import math
from html.parser import HTMLParser
from pathlib import Path


SOURCE_URL = 'https://gov.uz/oz/iiv/news/view/34435'
SOURCE_DATE = '2025-01-01'
CATALOG_PATH = Path(__file__).with_name('uzbekistan-enforcement.json')
_KINDS = {'Стационар камера': 'camera', 'Стационар радар': 'radar'}
_HEADERS = ['Viloyat', 'Tuman', 'Joylashgan joyi', 'Turi', 'Kenglik', 'Uzunlik']


class _InventoryTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.table = []
        elif tag == 'tr' and self.table is not None:
            self.row = []
        elif tag in {'td', 'th'} and self.row is not None:
            self.cell = []

    def handle_data(self, text):
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in {'td', 'th'} and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def parse_inventory(html):
    """Normalize the source's full rows and one-cell latitude continuations.

    Some regional tables put longitude in column six and latitude on the next
    row. Accept only that explicit layout, never infer a missing coordinate.
    """
    parser = _InventoryTable()
    parser.feed(html)
    tables = [table for table in parser.tables if table and table[0] == _HEADERS]
    if len(tables) != 1:
        raise ValueError('Uzbek enforcement inventory columns changed')
    rows = tables[0]
    records = {}
    rejected = 0
    for index, row in enumerate(rows[1:], start=1):
        if len(row) == 1:
            continue
        if len(row) != 6 or row[3] not in _KINDS:
            rejected += 1
            continue
        lat_text, lon_text = row[4:]
        if not lat_text and index + 1 < len(rows) and len(rows[index + 1]) == 1:
            lat_text = rows[index + 1][0]
        try:
            lat = float(lat_text.replace(',', '.'))
            lon = float(lon_text.replace(',', '.'))
        except ValueError:
            rejected += 1
            continue
        if (not math.isfinite(lat) or not math.isfinite(lon)
                or not 37.18 <= lat <= 45.60 or not 55.99 <= lon <= 73.22
                or not all(row[:3])):
            rejected += 1
            continue
        kind = _KINDS[row[3]]
        key = f'{kind}:{lat:.6f}:{lon:.6f}'
        records.setdefault(key, {
            'id': 'uz:iiv:' + hashlib.sha256(key.encode()).hexdigest()[:16],
            'kind': kind, 'lat': lat, 'lon': lon,
            'region': row[0], 'district': row[1], 'address': row[2],
        })
    if not records:
        raise ValueError('Uzbek enforcement inventory has no usable locations')
    return list(records.values()), rejected


@functools.lru_cache(maxsize=1)
def enforcement_catalog():
    with CATALOG_PATH.open(encoding='utf-8') as source:
        catalog = json.load(source)
    if catalog.get('sourceUrl') != SOURCE_URL or catalog.get('sourceDate') != SOURCE_DATE:
        raise ValueError('Unexpected Uzbek enforcement inventory provenance')
    return catalog['locations']


def enforcement_for_bbox(bbox):
    west, south, east, north = bbox
    return [{
        'type': 'node', 'id': item['id'], 'lat': item['lat'], 'lon': item['lon'],
        'title': ('Road enforcement camera' if item['kind'] == 'camera'
                  else 'Speed enforcement radar'),
        'detail': f"{item['address']} · {item['district']} · {item['region']}",
        'source': 'Uzbekistan IIV · Jan 2025 inventory · status unverified',
        'source_url': SOURCE_URL,
        'inventory_date': SOURCE_DATE,
        'record_kind': 'enforcement_' + item['kind'],
    } for item in enforcement_catalog()
        if west <= item['lon'] <= east and south <= item['lat'] <= north]
