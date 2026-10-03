"""Refresh the four operator-linked Shahdag panoramas in the webcam catalog.

Sources: https://www.shahdag.az/en/live-camera and PANOMAX's public player
configuration. The operator offers these URLs as interactive iframe embeds.
This is a manual catalog refresh, not an upstream request per GlobeView user.
"""

import datetime as dt
import json
import math
from pathlib import Path
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
CAMERAS = [
    (3010381, 'central-viewpoint', 'Shahdag - Central Viewpoint'),
    (2986174, 'alpine-horizon', 'Shahdag - Alpine Horizon'),
    (3005584, 'lake-view', 'Shahdag - Lake View'),
    (2987403, '3', 'Shahdag - 360 Panorama Eye'),
]


def refresh():
    rows = []
    for instance_id, path, name in CAMERAS:
        location_source = f'https://api.panomax.com/1.0/instances/{instance_id}/config/en-GB'
        request = urllib.request.Request(location_source, headers={'User-Agent': 'GlobeView catalog updater/1.0'})
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError('Panorama metadata exceeded size limit')
        instance = json.loads(body)['instance']
        if instance.get('id') != instance_id or instance.get('name') != name or instance.get('passwordProtected'):
            raise ValueError('Unexpected or protected panorama')
        camera = instance['cam']
        lat, lon = float(camera['latitude']), float(camera['longitude'])
        if not all(math.isfinite(v) for v in (lat, lon)) or not (41.25 < lat < 41.4 and 48.0 < lon < 48.3):
            raise ValueError('Unexpected camera location outside Shahdag resort')
        rows.append({'id': f'panomax-shahdag-{instance_id}', 'lat': lat, 'lon': lon, 'name': name,
                     'city': 'Shahdag Mountain Resort, Qusar', 'country': 'Azerbaijan',
                     'source': 'Shahdag Mountain Resort / PANOMAX',
                     'url': 'https://shahdag.panomax.com/' + path,
                     'location_source_url': location_source,
                     'operator_source_url': 'https://www.shahdag.az/en/live-camera',
                     'catalog_checked': dt.date.today().isoformat()})
    target = ROOT / 'global-cameras.json'
    catalog = json.loads(target.read_text(encoding='utf-8'))
    catalog['cameras'] = [row for row in catalog['cameras']
                          if not str(row.get('id', '')).startswith('panomax-shahdag-')] + rows
    target.write_text(json.dumps(catalog, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'Refreshed {len(rows)} Shahdag panoramas; {len(catalog["cameras"])} total webcams')


if __name__ == '__main__':
    refresh()
