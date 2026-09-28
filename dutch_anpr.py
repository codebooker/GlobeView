"""Dutch police ANPR camera-plan locations from official Staatscourant publications."""

import hashlib
import html.parser
import math
import re
import xml.etree.ElementTree as ET


SEARCH_URL = ('https://repository.overheid.nl/sru?'
              'query=c.product-area%3D%3Dofficielepublicaties%20AND%20'
              'dt.title%20all%20%22Cameraplan%20ANPR%20Politie%22%20'
              'sortBy%20dt.modified%2Fsort.descending&maximumRecords=5')
PUBLICATION_BASE = 'https://zoek.officielebekendmakingen.nl/'
_SRU = '{http://docs.oasis-open.org/ns/search-ws/sruResponse}'
_DCTERMS = '{http://purl.org/dc/terms/}'


def latest_plan(search_xml):
    root = ET.fromstring(search_xml)
    for record in root.findall(f'.//{_SRU}record'):
        title = record.findtext(f'.//{_DCTERMS}title', default='')
        identifier = record.findtext(f'.//{_DCTERMS}identifier', default='')
        match = re.fullmatch(r'Cameraplan ANPR Politie Q([1-4])-(20\d{2})', title)
        if match and re.fullmatch(r'stcrt-20\d{2}-\d{1,7}', identifier):
            return PUBLICATION_BASE + identifier + '.html', f'Q{match[1]} {match[2]}'
    raise ValueError('No Dutch police ANPR camera plan was found')


class _PlanTable(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_cell = False
        self.cell = []
        self.row = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table' and {'zebra', 'portrait'} <= set(attrs.get('class', '').split()):
            self.in_table = True
        elif self.in_table and tag == 'tr':
            self.row = []
        elif self.in_table and self.row is not None and tag == 'td':
            self.in_cell = True
            self.cell = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.in_cell:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.in_cell = False
        elif tag == 'tr' and self.in_table and self.row is not None:
            if len(self.row) == 10:
                self.rows.append(self.row)
            self.row = None
        elif tag == 'table' and self.in_table:
            self.in_table = False


def parse_plan(page, source_url, quarter, *, minimum_rows=500):
    if not re.fullmatch(r'Q[1-4] 20\d{2}', quarter):
        raise ValueError('Invalid Dutch ANPR plan quarter')
    if not re.fullmatch(r'https://zoek\.officielebekendmakingen\.nl/stcrt-20\d{2}-\d{1,7}\.html', source_url):
        raise ValueError('Invalid Dutch ANPR source')
    parser = _PlanTable()
    parser.feed(page)
    if not minimum_rows <= len(parser.rows) <= 3000:
        raise ValueError('Dutch ANPR plan table is missing or incomplete')
    elements = []
    seen = set()
    for name, city, municipality, latitude, longitude, *_ in parser.rows:
        try:
            lat, lon = float(latitude), float(longitude)
        except ValueError:
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)
                and 50.7 <= lat <= 53.7 and 3.1 <= lon <= 7.3
                and 2 <= len(name) <= 110):
            continue
        identity = f'{name}|{city}|{latitude}|{longitude}'
        if identity in seen:
            continue
        seen.add(identity)
        short_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
        elements.append({
            'type': 'node', 'id': f'nl:politie:anpr:{short_id}', 'lat': lat, 'lon': lon,
            'title': name, 'detail': f'{city or municipality} · {quarter} camera plan · status unverified',
            'source': 'Dutch Police · published ANPR camera plan', 'source_url': source_url,
        })
    if len(elements) < minimum_rows * 0.9:
        raise ValueError('Dutch ANPR plan has too few valid locations')
    return elements


def plan_for_bbox(catalog, bbox):
    if not isinstance(catalog, list):
        raise ValueError('Dutch ANPR catalog is invalid')
    west, south, east, north = bbox
    return [row for row in catalog if west <= row['lon'] <= east and south <= row['lat'] <= north]
