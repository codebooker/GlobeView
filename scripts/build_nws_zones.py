"""Build a compact lookup of NWS zone centroids from official GIS downloads.

Requires pyshp (`python3 -m pip install pyshp`). Run when NWS publishes new zones.
"""
import io
import json
import urllib.request
import zipfile
from pathlib import Path

import shapefile


SOURCES = {
    'public': 'https://www.weather.gov/source/gis/Shapefiles/WSOM/z_16ap26.zip',
    'county': 'https://www.weather.gov/source/gis/Shapefiles/County/c_16ap26.zip',
    'coastal': 'https://www.weather.gov/source/gis/Shapefiles/WSOM/mz16ap26.zip',
    'offshore': 'https://www.weather.gov/source/gis/Shapefiles/WSOM/oz16ap26.zip',
}
OUTPUT = Path(__file__).resolve().parents[1] / 'nws-zone-centroids.json'


def records_from_zip(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobalMap/1.0 (NWS zone catalog)'})
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read(40_000_001)
    if len(body) > 40_000_000:
        raise ValueError('NWS zone archive exceeded size limit')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        dbf = next(name for name in archive.namelist() if name.lower().endswith('.dbf'))
        return [record.as_dict() for record in shapefile.Reader(dbf=io.BytesIO(archive.read(dbf))).records()]


def main():
    zones = {}
    for kind, url in SOURCES.items():
        records = records_from_zip(url)
        for row in records:
            try:
                lon, lat = float(row['LON']), float(row['LAT'])
                if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    continue
                if kind == 'public':
                    code = f"{row['STATE']}Z{str(row['ZONE']).zfill(3)}"
                elif kind == 'county':
                    code = f"{row['STATE']}C{str(row['FIPS'])[-3:]}"
                else:
                    code = str(row['ID']).strip()
                if len(code) != 6:
                    continue
                zones[code] = [round(lon, 5), round(lat, 5), row.get('NAME') or row.get('COUNTYNAME') or code]
            except (KeyError, TypeError, ValueError):
                continue
        print(f'{kind}: {len(records)} records')
    OUTPUT.write_text(json.dumps({'source': 'NWS GIS zone centroids', 'valid_from': '2026-04-16',
                                  'sources': SOURCES, 'zones': dict(sorted(zones.items()))},
                                 separators=(',', ':')) + '\n')
    print(f'Wrote {len(zones)} zones to {OUTPUT}')


if __name__ == '__main__':
    main()
