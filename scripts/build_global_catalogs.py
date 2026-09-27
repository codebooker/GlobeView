"""Refresh the small, static search and port catalogs from attributed public data."""
import io
import json
from pathlib import Path
import urllib.parse
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PORT_SERVICE = 'https://services3.arcgis.com/9nfxWATFamVUTTGb/arcgis/rest/services/World_Port_Index_2024/FeatureServer/0/query'


def get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobalMap catalog builder/1.0'})
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read()


def ports():
    fields = ','.join([
        'ObjectId', 'World_Port_Index_Number', 'Main_Port_Name', 'Country_Code',
        'UN_LOCODE', 'Harbor_Size', 'Harbor_Type', 'World_Water_Body',
        'Latitude', 'Longitude', 'Facilities___Container', 'Facilities___LNG_Terminal',
    ])
    rows = []
    for offset in range(0, 6000, 1000):
        params = urllib.parse.urlencode({
            'where': '1=1', 'outFields': fields, 'returnGeometry': 'false',
            'resultOffset': offset, 'resultRecordCount': 1000, 'f': 'json',
        })
        data = json.loads(get(f'{PORT_SERVICE}?{params}'))
        if data.get('error'):
            raise RuntimeError(data['error'])
        batch = data.get('features', [])
        for feature in batch:
            a = feature['attributes']
            if a.get('Latitude') is None or a.get('Longitude') is None or not a.get('Main_Port_Name'):
                continue
            rows.append({
                'id': int(a['ObjectId']), 'n': a['Main_Port_Name'], 'c': a.get('Country_Code') or '',
                'lat': round(float(a['Latitude']), 5), 'lon': round(float(a['Longitude']), 5),
                'wpi': a.get('World_Port_Index_Number'), 'locode': a.get('UN_LOCODE'),
                'size': a.get('Harbor_Size'), 'kind': a.get('Harbor_Type'),
                'water': a.get('World_Water_Body'),
                'container': a.get('Facilities___Container'), 'lng': a.get('Facilities___LNG_Terminal'),
            })
        if len(batch) < 1000:
            break
    if len(rows) < 3000:
        raise RuntimeError(f'Port catalog unexpectedly small: {len(rows)}')
    rows.sort(key=lambda row: (row['n'], row['id']))
    (ROOT / 'ports.json').write_text(json.dumps(rows, ensure_ascii=False, separators=(',', ':')))
    return rows


def places(port_rows):
    countries = {}
    for line in get('https://download.geonames.org/export/dump/countryInfo.txt').decode().splitlines():
        if line.startswith('#') or not line.strip():
            continue
        cells = line.split('\t')
        countries[cells[0]] = cells[4]
    archive = zipfile.ZipFile(io.BytesIO(get('https://download.geonames.org/export/dump/cities15000.zip')))
    city_file = next(name for name in archive.namelist() if name.endswith('.txt'))
    records = []
    with archive.open(city_file) as source:
        for line in io.TextIOWrapper(source, encoding='utf-8'):
            cells = line.rstrip('\n').split('\t')
            if len(cells) < 15:
                continue
            try:
                lat, lon, population = float(cells[4]), float(cells[5]), int(cells[14] or 0)
            except ValueError:
                continue
            records.append([cells[1], countries.get(cells[8], cells[8]), round(lat, 5), round(lon, 5), population, 'city'])
    for port in port_rows:
        records.append([port['n'], countries.get(port['c'], port['c']), port['lat'], port['lon'], 0, 'port'])
    (ROOT / 'places.json').write_text(json.dumps(records, ensure_ascii=False, separators=(',', ':')))
    return records


if __name__ == '__main__':
    port_rows = ports()
    place_rows = places(port_rows)
    print(f'Wrote {len(port_rows)} ports and {len(place_rows)} searchable places')
