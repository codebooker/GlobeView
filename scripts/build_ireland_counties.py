"""Build compact Irish county shapes for Met Éireann warning visualization.

One-time build dependency: pyproj and shapely. Source is Tailte Éireann's
CC BY 4.0 2019 generalized statutory county boundary GeoJSON (EPSG:2157).
"""
import json
import urllib.request
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform


SOURCE = ('https://data-osi.opendata.arcgis.com/api/download/v1/items/'
          '7ef9c5102d61424295e98505a00251ea/geojson?layers=0')
FIPS_COUNTIES = {
    'EI01': 'CARLOW', 'EI02': 'CAVAN', 'EI03': 'CLARE', 'EI04': 'CORK',
    'EI06': 'DONEGAL', 'EI07': 'DUBLIN', 'EI10': 'GALWAY', 'EI11': 'KERRY',
    'EI12': 'KILDARE', 'EI13': 'KILKENNY', 'EI14': 'LEITRIM', 'EI15': 'LAOIS',
    'EI16': 'LIMERICK', 'EI18': 'LONGFORD', 'EI19': 'LOUTH', 'EI20': 'MAYO',
    'EI21': 'MEATH', 'EI22': 'MONAGHAN', 'EI23': 'OFFALY', 'EI24': 'ROSCOMMON',
    'EI25': 'SLIGO', 'EI26': 'TIPPERARY', 'EI27': 'WATERFORD',
    'EI29': 'WESTMEATH', 'EI30': 'WEXFORD', 'EI31': 'WICKLOW',
}


def rounded(value):
    if isinstance(value, (list, tuple)):
        return [rounded(part) for part in value]
    return round(value, 5)


def main():
    with urllib.request.urlopen(SOURCE, timeout=30) as response:
        raw = response.read(7_000_001)
    if len(raw) > 7_000_000:
        raise ValueError('Irish county boundary source is unexpectedly large')
    by_name = {item['properties']['ENGLISH']: shape(item['geometry'])
               for item in json.loads(raw)['features']}
    if set(by_name) != set(FIPS_COUNTIES.values()):
        raise ValueError('Irish county boundary names have changed')
    to_wgs84 = Transformer.from_crs(2157, 4326, always_xy=True).transform
    counties = {}
    for code, name in sorted(FIPS_COUNTIES.items()):
        original = by_name[name]
        simplified = original.simplify(100, preserve_topology=True)
        if abs(simplified.area / original.area - 1) > 0.001:
            raise ValueError(f'{name} boundary changed too much during simplification')
        point = transform(to_wgs84, original.representative_point())
        geometry = mapping(transform(to_wgs84, simplified))
        counties[code] = {
            'name': name.title(), 'center': rounded([point.x, point.y]),
            'geometry': {'type': geometry['type'], 'coordinates': rounded(geometry['coordinates'])},
        }
    output = {
        'source': SOURCE, 'license': 'CC BY 4.0 · Tailte Éireann',
        'derived': 'EPSG:2157 to WGS84; geometry simplified by 100 metres for display',
        'counties': counties,
    }
    path = Path(__file__).resolve().parents[1] / 'ireland-counties-2019.json'
    path.write_text(json.dumps(output, separators=(',', ':'), ensure_ascii=False) + '\n')
    print(f'{len(counties)} counties; {path.stat().st_size} bytes')


if __name__ == '__main__':
    main()
