"""Public road and electricity feeds outside North America, normalized for the map."""

import concurrent.futures
import base64
import csv
import datetime as dt
import gzip
import html
import io
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo


FINTRAFFIC_BASE = 'https://tie.digitraffic.fi'
TFL_URL = 'https://api.tfl.gov.uk/Road/all/Disruption'
UKPN_BASE = 'https://ukpowernetworks.opendatasoft.com'
NPG_BASE = 'https://northernpowergrid.opendatasoft.com'
NGED_OUTAGES_URL = ('https://connecteddata.nationalgrid.co.uk/dataset/'
                    'd6672e1e-c684-4cea-bb78-c7e5248b62a2/resource/'
                    '292f788f-4339-455b-8cc0-153e14509d4d/download/power_outage_ext.csv')
SSEN_OUTAGES_URL = 'https://external.distribution.prd.ssen.co.uk/opendataportal-prd/v4/api/getallfaults'
WALES_RSS_BASE = 'https://traffic.wales/feeds'
SRWR_BASE = 'https://downloads.srwr.scot/disruptions-export/api/v1'
FRANCE_ROADS_URL = ('https://tipi.bison-fute.gouv.fr/bison-fute-ouvert/'
                    'publicationsDIR/Evenementiel-DIR/grt/RRN/content.xml')
FRANCE_ROADS_SOURCE = ('https://transport.data.gouv.fr/datasets/'
                       'evenements-routiers-sur-le-reseau-routier-national-non-concede')
FRANCE_SENSOR_BASE = 'https://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/QTV-DIR/'
BELGIUM_ROADS_URL = 'https://www.verkeerscentrum.be/uitwisseling/datex2v3full'
BELGIUM_ROADS_SOURCE = 'https://www.verkeerscentrum.be/data'
GIPOD_POINT_URL = ('https://geo.api.vlaanderen.be/GIPOD/ogc/features/v1/'
                   'collections/HINDER_PUNT/items')
GIPOD_SOURCE = ('https://www.vlaanderen.be/datavindplaats/catalogus/'
                'geplande-innames-en-mobiliteitshinder-publieke-geo-informatie-uit-gipod')
NDW_BASE = 'https://opendata.ndw.nu/'
NDW_SOURCE = 'https://docs.ndw.nu/producten/werkzaamhedenenevenementen/'
AUTOBAHN_BASE = 'https://verkehr.autobahn.de/o/autobahn/'
AUTOBAHN_SOURCE = 'https://www.autobahn.de/betrieb-verkehr/verkehrsmeldungen'
FRANCE_SENSOR_SOURCE = ('https://transport.data.gouv.fr/datasets/'
                        'etat-de-circulation-en-temps-reel-sur-le-reseau-national-routier-non-concede')
ZURICH_ROADWORKS_URL = ('https://maps.zh.ch/wfs/TbaBaustellenZHWFS?SERVICE=WFS&REQUEST=GetFeature'
                        '&VERSION=2.0.0&TYPENAMES=ms:baustellen-uebersicht'
                        '&OUTPUTFORMAT=application%2Fjson&SRSNAME=EPSG:4326')
ZURICH_ROADWORKS_SOURCE = 'https://data.stadt-zuerich.ch/dataset/d991a4a2-32ea-4f7a-93b5-0f31a016d71c'
ZURICH_COUNTERS_URL = ('https://maps.zh.ch/wfs/TBAVMSZHWFS?SERVICE=WFS&REQUEST=GetFeature'
                       '&VERSION=2.0.0&TYPENAMES=ms:verkehrszaehlstellen'
                       '&OUTPUTFORMAT=application%2Fjson&SRSNAME=EPSG:4326')
ZURICH_COUNTER_CONFIG_URL = 'https://vdp.zh.ch/pws/public-service/readCollectorsCfg'
ZURICH_COUNTER_SOURCE = 'https://datenkatalog.statistik.zh.ch/datasets/692@tiefbauamt-kanton-zuerich'
NORWAY_WFS_URL = 'https://ogckart-sn1.atlas.vegvesen.no/datex_3_1/ows'
NORWAY_SOURCE = 'https://www.vegvesen.no/trafikk/kart'
UKPN_DATASET = 'ukpn-live-faults'
NPG_DATASET = 'live-power-cuts-data'
_LOCKS = {'roads': threading.Lock(), 'power': threading.Lock()}
_CACHE = {
    'roads': {'until': 0, 'sources': {}, 'source_times': {}, 'errors': []},
    'power': {'until': 0, 'sources': {}, 'source_times': {}, 'errors': []},
}
_STALE_SECONDS = 900
_SRWR_CACHE = {'until': 0, 'archive': '', 'activities': []}
_FRANCE_SENSOR_REFERENCES = {'until': 0, 'points': {}}
_GIPOD_TILE_CACHE = {}
_GIPOD_TILE_LOCKS = {}
_GIPOD_CACHE_LOCK = threading.Lock()
_AUTOBAHN_CACHE = {service: {'until': 0, 'roads': {}, 'lock': threading.Lock()}
                   for service in ('roadworks', 'warning', 'closure')}


def _get_json(url, fintraffic=False):
    headers = {'User-Agent': 'GlobalMap/1.0 (public map feed reader)', 'Accept': 'application/json'}
    if fintraffic:
        headers.update({'Accept-Encoding': 'gzip', 'Digitraffic-User': 'GlobalMap/1.0'})
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(12 * 1024 * 1024 + 1)
        if len(body) > 12 * 1024 * 1024:
            raise ValueError('Infrastructure feed exceeded 12 MB')
        if response.headers.get('Content-Encoding') == 'gzip':
            body = gzip.decompress(body)
            if len(body) > 12 * 1024 * 1024:
                raise ValueError('Infrastructure feed exceeded 12 MB after decompression')
    return json.loads(body)


def _get_xml(url, max_bytes=2 * 1024 * 1024):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'application/rss+xml, application/xml'})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('Road feed exceeded size limit')
    return ET.fromstring(body)


def _get_gzip_xml(url, max_compressed=4 * 1024 * 1024, max_uncompressed=12 * 1024 * 1024):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'application/gzip'})
    with urllib.request.urlopen(request, timeout=15) as response:
        compressed = response.read(max_compressed + 1)
    if len(compressed) > max_compressed:
        raise ValueError('Compressed road feed exceeded size limit')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
        body = archive.read(max_uncompressed + 1)
    if len(body) > max_uncompressed:
        raise ValueError('Expanded road feed exceeded size limit')
    return ET.fromstring(body)


def _get_csv(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public outage feed reader)', 'Accept': 'text/csv'})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError('Outage feed exceeded 2 MB')
    return list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))


def _clean(value, limit=280):
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', str(value or ''))).split())[:limit]


def _timestamp(value):
    try:
        timestamp = re.sub(r'(\.\d{6})\d+(?=Z|[+-]\d{2}:\d{2}$)', r'\1', str(value))
        parsed = dt.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None


def _point(geometry):
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get('coordinates')
    while isinstance(coordinates, list) and coordinates and isinstance(coordinates[0], list):
        coordinates = coordinates[len(coordinates) // 2]
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        lon, lat = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None
    return [lon, lat] if -180 <= lon <= 180 and -90 <= lat <= 90 else None


def _feature(lonlat, properties):
    return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': lonlat}, 'properties': properties}


def _norway_wfs(layer, cql_filter=None):
    params = {'service': 'WFS', 'version': '1.0.0', 'request': 'GetFeature',
              'typeName': f'datex_3_1:{layer}', 'outputFormat': 'application/json',
              'maxFeatures': '2000'}
    if cql_filter:
        params['cql_filter'] = cql_filter
    return _get_json(f'{NORWAY_WFS_URL}?{urllib.parse.urlencode(params)}')


def _norway_publication_current(items, now):
    if not items:
        return True
    timestamp = str((items[0].get('properties') or {}).get('endJsonTime') or '')
    published = _timestamp(re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', timestamp))
    if published is None or not -300 <= now - published <= 30 * 60:
        raise ValueError('Norwegian WFS publication is stale or invalid')
    return True


def _parse_norway_roads(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') or []
    _norway_publication_current(rows, now)
    features = []
    for item in rows:
        p = item.get('properties') or {}
        point = _point(item.get('geometry'))
        if not point or not (4 <= point[0] <= 32 and 57 <= point[1] <= 72):
            continue
        if p.get('isMainRecord') is not True or p.get('activePeriodAtLastUpdate') != 1:
            continue
        situation_id = str(p.get('situationId') or '')
        if not situation_id:
            continue
        kind = str(p.get('situationType') or '')
        layer = 'construction' if kind in {'MaintenanceWorks', 'ConstructionWorks'} else 'incidents'
        road = _clean(p.get('roadNumber'), 25)
        place = _clean(p.get('locationDescription'), 120)
        description = _clean(str(p.get('description') or '').replace('|', ' · '), 240)
        features.append(_feature(point, {
            'key': f'no:road:{situation_id}', 'layer': layer,
            'title': place or f'{road} · {"Roadworks" if layer == "construction" else "Road event"}',
            'detail': description or kind, 'source': 'Statens vegvesen',
            'source_url': NORWAY_SOURCE,
        }))
    return features


def _norway_roads():
    return _parse_norway_roads(_norway_wfs('SituationSimple',
                              'isMainRecord=true AND activePeriodAtLastUpdate=1'))


def _parse_norway_cameras(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') or []
    _norway_publication_current(rows, now)
    features = []
    for item in rows:
        p = item.get('properties') or {}
        point = _point(item.get('geometry'))
        if not point or not (4 <= point[0] <= 32 and 57 <= point[1] <= 72):
            continue
        if p.get('status.stillImageAvailability') != 'videoOrImagesAvailable':
            continue
        camera_id = str(p.get('cameraId') or '')
        image = str(p.get('stillImageUrl') or '')
        parsed = urllib.parse.urlparse(image)
        if (not re.fullmatch(r'\d+_\d+', camera_id) or parsed.scheme != 'https'
                or parsed.hostname != 'kamera.atlas.vegvesen.no'
                or parsed.path != f'/api/images/{camera_id}'):
            continue
        name = _clean(p.get('description'), 90)
        orientation = _clean(p.get('orientationDescription'), 90)
        road = _clean(p.get('roadNumber'), 25)
        features.append(_feature(point, {
            'key': f'no:camera:{camera_id}', 'layer': 'cameras',
            'title': ' · '.join(part for part in (road, name) if part) or 'Road camera',
            'detail': orientation, 'snapshot_url': image,
            'source': 'Statens vegvesen', 'source_url': NORWAY_SOURCE,
        }))
    return features


def _norway_cameras():
    return _parse_norway_cameras(_norway_wfs('CctvSimple'))


def _parse_zurich_roadworks(items, now=None):
    today = dt.datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo('Europe/Zurich')).date()
    features = []
    for item in items:
        properties = item.get('properties') or {}
        point = _point(item.get('geometry'))
        if not point or not (8.35 <= point[0] <= 9 and 47.15 <= point[1] <= 47.7):
            continue
        if properties.get('status_baustelle') != 'aktiv (Bauzeit)':
            continue
        try:
            start = dt.date.fromisoformat(str(properties.get('datum_baubeginn'))[:10])
            end = dt.date.fromisoformat(str(properties.get('datum_bauende'))[:10])
        except ValueError:
            continue
        if not start <= today <= end:
            continue
        road_id = _clean(properties.get('strassenbez'), 20)
        km_start = _clean(properties.get('kmvon'), 20)
        road = _clean(properties.get('strassenname'), 100)
        municipality = _clean(properties.get('gemeindename'), 65)
        if not road_id or not km_start or not road:
            continue
        description = _clean(properties.get('beschreibung'), 150)
        guidance = _clean(properties.get('verkehrsfuehrung'), 150)
        features.append(_feature(point, {
            'key': f'ch:zh:roadwork:{road_id}:{km_start}:{start.isoformat()}',
            'layer': 'construction',
            'title': f'Roadworks · {road}' + (f', {municipality}' if municipality else ''),
            'detail': _clean(' · '.join(part for part in (description, guidance, f'Through {end.isoformat()}')
                                       if part), 280),
            'source': 'Kanton Zürich Tiefbauamt · CC0',
            'source_url': ZURICH_ROADWORKS_SOURCE,
        }))
    return features


def _zurich_roadworks():
    data = _get_json(ZURICH_ROADWORKS_URL)
    if data.get('type') != 'FeatureCollection':
        raise ValueError('Zurich roadworks feed is invalid')
    return _parse_zurich_roadworks(data.get('features') or [])


def _parse_zurich_sensors(locations, collectors):
    active = {str(item.get('uID', {}).get('id') or ''): item for item in collectors
              if item.get('collectorStatus') == 'ACTIVE'}
    features = []
    for item in locations:
        properties = item.get('properties') or {}
        point = _point(item.get('geometry'))
        if not point or not (8.3 <= point[0] <= 9.1 and 47.1 <= point[1] <= 47.8):
            continue
        try:
            collector_id = f'M{int(properties.get("messst_nr")):04d}'
        except (TypeError, ValueError):
            continue
        if collector_id not in active:
            continue
        name = _clean(active[collector_id].get('name'), 100)
        year = properties.get('dtv_bezugsjahr')
        daily = properties.get('dtv')
        detail = (f'{int(year)} average: {int(daily):,} vehicles/day'
                  if isinstance(year, (int, float)) and isinstance(daily, (int, float))
                  and 2000 <= year <= 2100 and daily >= 0 else 'Active traffic counter')
        features.append(_feature(point, {
            'key': f'ch:zh:sensor:{collector_id}', 'layer': 'sensors',
            'title': name or f'Zurich traffic counter {collector_id}',
            'detail': detail, 'sensor_id': collector_id,
            'source': 'Kanton Zürich Tiefbauamt · CC BY 4.0',
            'source_url': ZURICH_COUNTER_SOURCE,
        }))
    return features


def _zurich_sensors():
    locations = _get_json(ZURICH_COUNTERS_URL)
    collectors = _get_json(ZURICH_COUNTER_CONFIG_URL)
    if locations.get('type') != 'FeatureCollection' or not isinstance(collectors, list):
        raise ValueError('Zurich sensor catalog is invalid')
    return _parse_zurich_sensors(locations.get('features') or [], collectors)


def zurich_sensor_sample(collector_id):
    if not re.fullmatch(r'M\d{4}', collector_id):
        raise ValueError('Invalid Zurich collector ID')
    url = f'https://vdp.zh.ch/pws/public-service/readOnlineVbvData/{collector_id}?sampleOnly=true'
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)',
        'Accept': 'application/stream+json'})
    with urllib.request.urlopen(request, timeout=8) as response:
        sample = json.loads(response.readline(8192))
    if sample.get('uID', {}).get('id') != collector_id:
        raise ValueError('Zurich sensor response ID mismatch')
    observed = float(sample.get('effectiveTime')) / 1000
    if not -60 <= time.time() - observed <= 300:
        raise ValueError('Zurich sensor sample is stale')
    vehicle_classes = {
        'PW': 'Passenger car', 'PWA': 'Car with trailer', 'MR': 'Motorcycle',
        'BUS': 'Bus', 'LIEF': 'Delivery van', 'LW': 'Truck', 'LZ': 'Road train', 'SZ': 'Semi-trailer',
    }
    vehicle_code = str(sample.get('swiss10Class') or '').removeprefix('SWISS10_')
    return {'observed_at': dt.datetime.fromtimestamp(observed, dt.timezone.utc).isoformat(),
            'vehicle': vehicle_classes.get(vehicle_code, 'Vehicle'),
            'lane': str((sample.get('uID', {}).get('sub') or {}).get('id') or '')[:8]}


def _fintraffic_messages(layer):
    endpoint = 'roadworks' if layer == 'construction' else 'traffic-announcements'
    data = _get_json(f'{FINTRAFFIC_BASE}/api/traffic-message/v2/{endpoint}', fintraffic=True)
    now = time.time()
    features = []
    for item in data.get('features') or []:
        point = _point(item.get('geometry'))
        props = item.get('properties') or {}
        announcements = props.get('announcements') or []
        if not point or not announcements:
            continue
        announcement = next((entry for entry in announcements if entry.get('language') == 'en'), announcements[0])
        timing = announcement.get('timeAndDuration') or {}
        start, end = _timestamp(timing.get('startTime')), _timestamp(timing.get('endTime'))
        if (start and start > now) or (end and end < now):
            continue
        location = announcement.get('location') or {}
        title = _clean(announcement.get('title'))
        if layer == 'construction':
            title = f'Roadworks · {title}' if title else 'Roadworks'
        else:
            title = _clean(props.get('trafficAnnouncementType') or title or 'Traffic incident')
        features.append(_feature(point, {
            'key': f'fi:{layer}:{props.get("situationId") or len(features)}',
            'layer': layer, 'title': title,
            'detail': _clean(location.get('description') or announcement.get('comment')),
            'source': 'Fintraffic / Digitraffic · adapted, CC BY 4.0',
            'source_url': 'https://www.digitraffic.fi/en/road-traffic/',
            'updated_at': props.get('versionTime') or props.get('releaseTime') or '',
        }))
    return features


def _fintraffic_signs():
    data = _get_json(f'{FINTRAFFIC_BASE}/api/variable-sign/v1/signs', fintraffic=True)
    features = []
    now = time.time()
    for item in data.get('features') or []:
        point = _point(item.get('geometry'))
        props = item.get('properties') or {}
        updated = _timestamp(props.get('effectDate'))
        if not point or props.get('reliability') != 'NORMAL' or not updated or now - updated > 7 * 86400:
            continue
        sign_type = props.get('type') or ''
        rows = sorted(props.get('textRows') or [], key=lambda row: (row.get('screen') or 0, row.get('rowNumber') or 0))
        message = _clean(' / '.join(str(row.get('text') or '') for row in rows))
        if sign_type == 'SPEEDLIMIT':
            title = f'Variable speed limit · {props.get("displayValue")} km/h' if str(props.get('displayValue') or '').isdigit() else 'Variable speed limit'
        elif sign_type == 'WARNING':
            title = 'Variable warning sign'
        else:
            title = 'Road information sign'
        features.append(_feature(point, {
            'key': f'fi:sign:{props.get("id") or len(features)}', 'layer': 'signs',
            'title': title, 'detail': message or _clean(props.get('roadAddress')),
            'source': 'Fintraffic / Digitraffic · adapted, CC BY 4.0',
            'source_url': 'https://www.digitraffic.fi/en/road-traffic/',
            'updated_at': props.get('effectDate') or '',
        }))
    return features


def _tfl_disruptions():
    data = _get_json(TFL_URL)
    features = []
    for item in data:
        point = _point(item.get('geography'))
        if not point or not str(item.get('status') or '').startswith('Active'):
            continue
        layer = 'construction' if item.get('category') == 'Works' else 'incidents'
        features.append(_feature(point, {
            'key': f'uk:tfl:{item.get("id") or len(features)}', 'layer': layer,
            'title': _clean(item.get('subCategory') or item.get('category') or 'Road disruption'),
            'detail': _clean(item.get('comments') or item.get('currentUpdate')),
            'source': 'Transport for London',
            'source_url': 'https://tfl.gov.uk/traffic/status',
            'updated_at': item.get('currentUpdateDateTime') or item.get('lastModifiedTime') or '',
        }))
    return features


def _wales_feed(layer):
    feed_name = 'roadworks' if layer == 'construction' else 'incidents-events'
    root = _get_xml(f'{WALES_RSS_BASE}/{feed_name}/rss.xml')
    now = dt.datetime.now(dt.timezone.utc)
    features = []
    for item in root.findall('./channel/item'):
        coordinates = item.findtext('{http://www.georss.org/georss}point') or ''
        try:
            lat, lon = (float(value) for value in coordinates.split())
        except (TypeError, ValueError):
            continue
        point = _point({'coordinates': [lon, lat]})
        if not point:
            continue
        description = item.findtext('description') or ''
        if layer == 'construction':
            start_match = re.search(r'Start time:\s*(\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2})', description, re.I)
            end_match = re.search(r'End Date:\s*(\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2})', description, re.I)
            if not start_match or not end_match:
                continue
            try:
                zone = ZoneInfo('Europe/London')
                start = dt.datetime.strptime(start_match.group(1), '%d/%m/%Y %H:%M').replace(tzinfo=zone).astimezone(dt.timezone.utc)
                end = dt.datetime.strptime(end_match.group(1), '%d/%m/%Y %H:%M').replace(tzinfo=zone).astimezone(dt.timezone.utc)
            except ValueError:
                continue
            if not start <= now <= end:
                continue
        source_url = item.findtext('link') or ''
        if not source_url.startswith('https://traffic.wales/'):
            source_url = 'https://traffic.wales/'
        reference = _clean(item.findtext('guid') or source_url, 100)
        features.append(_feature(point, {
            'key': f'uk:wales:{layer}:{reference}', 'layer': layer,
            'title': _clean(item.findtext('title') or 'Traffic Wales road event'),
            'detail': _clean(description, 280), 'source': 'Traffic Wales',
            'source_url': source_url, 'updated_at': item.findtext('pubDate') or '',
        }))
    return features


def _parse_scotland_archive(body):
    activities = []
    with zipfile.ZipFile(io.BytesIO(body)) as zipped:
        member = zipped.getinfo('CurrentActivities.csv')
        if member.file_size > 50 * 1024 * 1024:
            raise ValueError('Scottish roadworks CSV exceeded 50 MB')
        csv.field_size_limit(8 * 1024 * 1024)
        with zipped.open(member) as raw:
            for row in csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig')):
                if row.get('ActivityStatus') not in {'In Progress', 'Commenced'} or row.get('Category') == 'Event':
                    continue
                point = _point({'coordinates': [row.get('Longitude'), row.get('Latitude')]})
                if not point:
                    continue
                start = _timestamp(row.get('StartDateTimeUTC'))
                end = _timestamp(row.get('EndDateTimeUTC'))
                if start is None or end is None or end < start:
                    continue
                reference = _clean(row.get('ActivityReference'), 100)
                if not reference:
                    continue
                activities.append((start, end, _feature(point, {
                    'key': f'uk:scotland:construction:{reference}', 'layer': 'construction',
                    'title': _clean(row.get('Street') or row.get('Location') or 'Roadworks', 120),
                    'detail': _clean(' · '.join(filter(None, [row.get('Town'), row.get('TrafficManagement'),
                                  row.get('TrafficImpact'), row.get('Description')])), 280),
                    'source': 'Scottish Road Works Register · OGL v3',
                    'source_url': 'https://roadworks.scot/opendata',
                    'updated_at': row.get('LastUpdatedDateTimeUTC') or '',
                })))
    return activities


def _scotland_roadworks():
    now = time.time()
    if now >= _SRWR_CACHE['until']:
        listing = _get_json(f'{SRWR_BASE}/files')
        archives = sorted((entry.get('name', '') for entry in listing.get('files', [])
                           if re.fullmatch(r'SRWRDisruptionsExport\d{8}\.zip', entry.get('name', ''))), reverse=True)
        if not archives:
            raise ValueError('Scottish roadworks archive is unavailable')
        archive = archives[0]
        archive_date = dt.datetime.strptime(archive[-12:-4], '%Y%m%d').date()
        if abs((dt.datetime.now(dt.timezone.utc).date() - archive_date).days) > 1:
            raise ValueError('Scottish roadworks archive is stale')
        if archive != _SRWR_CACHE['archive']:
            url = _get_json(f'{SRWR_BASE}/file/{archive}').get('url', '')
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname != 'srwrexport.blob.core.windows.net':
                raise ValueError('Unexpected Scottish roadworks download host')
            request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read(10 * 1024 * 1024 + 1)
            if len(body) > 10 * 1024 * 1024:
                raise ValueError('Scottish roadworks archive exceeded 10 MB')
            _SRWR_CACHE.update({'archive': archive, 'activities': _parse_scotland_archive(body)})
        _SRWR_CACHE['until'] = now + 3600
    return [item for start, end, item in _SRWR_CACHE['activities'] if start <= now <= end]


def _ods(base, dataset, where):
    rows = []
    while True:
        query = urllib.parse.urlencode({'where': where, 'limit': 100, 'offset': len(rows)})
        page = _get_json(f'{base}/api/explore/v2.1/catalog/datasets/{dataset}/records?{query}')
        batch = page.get('results') or []
        rows.extend(batch)
        total = page.get('total_count')
        if len(batch) < 100 or (isinstance(total, int) and len(rows) >= total):
            break
        if len(rows) >= 10000:
            raise ValueError(f'{dataset} has more than 10,000 active records')
    metadata = _get_json(f'{base}/api/explore/v2.1/catalog/datasets/{dataset}')
    return rows, (metadata.get('metas') or {}).get('default', {}).get('data_processed') or ''


def _ukpn_outages():
    rows, updated = _ods(UKPN_BASE, UKPN_DATASET, 'restoreddatetime is null')
    features = []
    now = time.time()
    for row in rows:
        point = _point({'coordinates': [row.get('geopoint', {}).get('lon'), row.get('geopoint', {}).get('lat')]}) if isinstance(row.get('geopoint'), dict) else None
        if not point:
            continue
        planned = str(row.get('powercuttype') or '').lower() == 'planned'
        if planned and (_timestamp(row.get('planneddate')) or 0) > now:
            continue
        count = row.get('nocustomeraffected') or row.get('noplannedcustomers') or 0
        features.append(_feature(point, {
            'key': f'uk:ukpn:{row.get("incidentreference") or len(features)}',
            'provider': 'UK Power Networks', 'area_name': _clean(row.get('operatingzone')),
            'customers_affected': count, 'status': 'Planned outage' if planned else 'Unplanned outage',
            'reason': _clean(row.get('incidentdescription') or row.get('incidentcategorycustomerfriendlydescription'), 180),
            'etr': row.get('estimatedrestorationdate') or '',
            'source_label': 'UK Power Networks · Live Faults · CC BY 4.0',
            'source_url': f'{UKPN_BASE}/explore/dataset/{UKPN_DATASET}/',
            'source_updated': updated,
        }))
    return features


def _npg_outages():
    rows, updated = _ods(NPG_BASE, NPG_DATASET, 'isaffected = 1')
    features = []
    seen = set()
    for row in rows:
        reference = str(row.get('reference') or row.get('id') or '')
        if not reference or reference in seen:
            continue
        point = _point({'coordinates': [row.get('lng'), row.get('lat')]})
        if not point:
            continue
        seen.add(reference)
        count = row.get('totalconfirmedpowercut') or row.get('totalpredictedpowercut') or 0
        features.append(_feature(point, {
            'key': f'uk:npg:{reference}', 'provider': 'Northern Powergrid',
            'area_name': _clean(row.get('area')),
            'customers_affected': count, 'status': _clean(row.get('natureofoutage')),
            'reason': _clean(row.get('reason'), 180),
            'etr': row.get('estimatedtimetillresolution') or '',
            'source_label': 'Northern Powergrid · Live Power Cut Data',
            'source_url': f'{NPG_BASE}/explore/dataset/{NPG_DATASET}/',
            'source_updated': updated,
        }))
    return features


def _ssen_outages():
    data = _get_json(SSEN_OUTAGES_URL)
    if not isinstance(data, dict) or not isinstance(data.get('faults'), list):
        raise ValueError('SSEN returned an invalid outage payload')
    features = []
    seen = set()
    for row in data['faults']:
        if not isinstance(row, dict):
            continue
        reference = str(row.get('reference') or '').strip()
        location = row.get('location') or {}
        point = _point({'coordinates': [location.get('longitude'), location.get('latitude')]}) if isinstance(location, dict) else None
        if not reference or reference in seen or not point:
            continue
        seen.add(reference)
        features.append(_feature(point, {
            'key': f'uk:ssen:{reference}', 'provider': 'SSEN Distribution',
            'area_name': _clean(row.get('title') or 'Power cut'),
            'customers_affected': row.get('customerCount') or 0,
            'status': 'Power cut',
            'reason': _clean(row.get('message') or row.get('type'), 180),
            'etr': row.get('estimatedRestorationTimeUtc') or '',
            'source_label': 'SSEN PowerTrack · CC BY 4.0',
            'source_url': 'https://powertrack.ssen.co.uk/powertrack',
            'source_updated': data.get('timestampUtc') or '',
        }))
    return features


def _nged_outages(now=None):
    rows = _get_csv(NGED_OUTAGES_URL)
    if not rows or 'Upload Date' not in rows[0] or 'Incident ID' not in rows[0]:
        raise ValueError('NGED returned an invalid outage file')
    now = now or dt.datetime.now(dt.timezone.utc)
    zone = ZoneInfo('Europe/London')
    features = []
    seen = set()
    for row in rows:
        try:
            uploaded = dt.datetime.fromisoformat(row['Upload Date']).replace(tzinfo=zone).astimezone(dt.timezone.utc)
        except (TypeError, ValueError):
            continue
        if not dt.timedelta(minutes=-5) <= now - uploaded <= dt.timedelta(hours=2):
            continue
        reference = (row.get('Incident ID') or '').strip()
        status = (row.get('Status') or '').strip()
        point = _point({'coordinates': [row.get('Location Longitude'), row.get('Location Latitude')]})
        if not reference or reference in seen or not point or status.lower() not in {'in progress', 'awaiting'}:
            continue
        if (row.get('Planned') or '').lower() == 'true':
            try:
                start = dt.datetime.fromisoformat(row.get('Start Time') or '').replace(tzinfo=zone).astimezone(dt.timezone.utc)
                if start > now:
                    continue
            except ValueError:
                continue
        seen.add(reference)
        try:
            count = max(0, int(row.get('Confirmed Off') or 0)) + max(0, int(row.get('Predicted Off') or 0))
        except ValueError:
            count = 0
        etr = ''
        if row.get('ETR'):
            try:
                etr = dt.datetime.fromisoformat(row['ETR']).replace(tzinfo=zone).astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                pass
        features.append(_feature(point, {
            'key': f'uk:nged:{reference}', 'provider': 'National Grid Electricity Distribution',
            'area_name': _clean(row.get('Region') or 'Power cut'),
            'customers_affected': count, 'status': status,
            'reason': _clean(row.get('Category'), 100), 'etr': etr,
            'source_label': 'Supported by NGED Open Data',
            'source_url': 'https://connecteddata.nationalgrid.co.uk/dataset/live-power-cuts',
            'source_updated': uploaded.isoformat().replace('+00:00', 'Z'),
        }))
    return features


_DATEX_NS = {'d': 'http://datex2.eu/schema/2/2_0'}
_DATEX_TYPE = '{http://www.w3.org/2001/XMLSchema-instance}type'
_FRANCE_WORK_TYPES = {'MaintenanceWorks', 'ConstructionWorks'}
_FRANCE_INCIDENT_TYPES = {'Accident', 'AbnormalTraffic', 'EnvironmentalObstruction',
                          'GeneralObstruction', 'InfrastructureDamageObstruction',
                          'PublicEvent', 'VehicleObstruction', 'WeatherRelatedRoadConditions'}
_FRANCE_MANAGEMENT_TYPES = {'GeneralNetworkManagement', 'ReroutingManagement',
                            'RoadOrCarriagewayOrLaneManagement', 'SpeedManagement'}


def _parse_france_roads(root, now=None):
    now = time.time() if now is None else now
    published = _timestamp(root.findtext('.//d:publicationTime', namespaces=_DATEX_NS))
    if published is None or not -600 <= now - published <= 4 * 3600:
        raise ValueError('French road publication is stale or invalid')
    features = []
    for record in root.findall('.//d:situationRecord', _DATEX_NS):
        kind = record.get(_DATEX_TYPE, '').split(':')[-1]
        if kind not in _FRANCE_WORK_TYPES | _FRANCE_INCIDENT_TYPES | _FRANCE_MANAGEMENT_TYPES:
            continue
        start = _timestamp(record.findtext('.//d:overallStartTime', namespaces=_DATEX_NS))
        end = _timestamp(record.findtext('.//d:overallEndTime', namespaces=_DATEX_NS))
        if (start is not None and start > now) or (end is not None and end < now):
            continue
        lat = record.findtext('.//d:pointCoordinates/d:latitude', namespaces=_DATEX_NS)
        lon = record.findtext('.//d:pointCoordinates/d:longitude', namespaces=_DATEX_NS)
        point = _point({'coordinates': [lon, lat]})
        if not point or not (-6 <= point[0] <= 10 and 41 <= point[1] <= 52):
            continue
        comments = [_clean(node.text, 220) for node in record.findall(
            './/d:generalPublicComment/d:comment/d:values/d:value', _DATEX_NS)]
        comments = [comment for comment in comments if comment]
        description = comments[0] if comments else ''
        if kind in _FRANCE_WORK_TYPES or (kind in _FRANCE_MANAGEMENT_TYPES and
                                         re.search(r'chantier|travaux|maintenance', ' '.join(comments), re.I)):
            layer, label = 'construction', 'Roadworks'
        else:
            layer, label = 'incidents', {
                'Accident': 'Crash', 'AbnormalTraffic': 'Traffic delay',
                'PublicEvent': 'Road event', 'WeatherRelatedRoadConditions': 'Weather road hazard',
                'VehicleObstruction': 'Vehicle obstruction',
            }.get(kind, 'Road disruption')
        road = _clean(record.findtext('.//d:roadNumber', namespaces=_DATEX_NS), 24)
        detail = _clean(' · '.join(filter(None, [road, *comments[:2]])), 280)
        features.append(_feature(point, {
            'key': f'fr:road:{record.get("id")}', 'layer': layer,
            'title': f'{label} · {road}' if road else label, 'detail': detail,
            'source': 'Bison Futé / DIR · Licence Ouverte 2.0',
            'source_url': FRANCE_ROADS_SOURCE,
            'updated_at': record.findtext('d:situationRecordVersionTime', default='', namespaces=_DATEX_NS),
        }))
    return features


def _france_roads():
    return _parse_france_roads(_get_xml(FRANCE_ROADS_URL, max_bytes=8 * 1024 * 1024))


def _lambert93_to_lonlat(x, y):
    """Convert the sensor reference's RGF93 / Lambert-93 metres to map coordinates."""
    a, flattening = 6378137.0, 1 / 298.257222101
    eccentricity = math.sqrt(2 * flattening - flattening * flattening)

    def t(latitude):
        sine = math.sin(latitude)
        return math.tan(math.pi / 4 - latitude / 2) * (
            (1 + eccentricity * sine) / (1 - eccentricity * sine)) ** (eccentricity / 2)

    def m(latitude):
        sine = math.sin(latitude)
        return math.cos(latitude) / math.sqrt(1 - eccentricity ** 2 * sine ** 2)

    north, south, origin = map(math.radians, (49, 44, 46.5))
    exponent = math.log(m(north) / m(south)) / math.log(t(north) / t(south))
    factor = m(north) / (exponent * t(north) ** exponent)
    origin_radius = a * factor * t(origin) ** exponent
    radius = math.hypot(x - 700000, origin_radius - (y - 6600000))
    angle = math.atan2(x - 700000, origin_radius - (y - 6600000))
    target_t = (radius / (a * factor)) ** (1 / exponent)
    latitude = math.pi / 2 - 2 * math.atan(target_t)
    for _ in range(8):
        sine = math.sin(latitude)
        latitude = math.pi / 2 - 2 * math.atan(target_t * (
            (1 - eccentricity * sine) / (1 + eccentricity * sine)) ** (eccentricity / 2))
    return [3 + math.degrees(angle / exponent), math.degrees(latitude)]


def _parse_france_sensor_references(csv_text):
    rows = csv.reader(io.StringIO(csv_text), delimiter=';')
    next(rows, None)
    points = {}
    for row in rows:
        # The published header includes code_insee_commune, but all current data rows omit it.
        if len(row) not in (19, 20):
            continue
        offset = len(row) - 19
        try:
            x1, y1, x2, y2 = (float(row[index + offset]) for index in (14, 15, 16, 17))
        except (ValueError, IndexError):
            continue
        point = _point({'coordinates': _lambert93_to_lonlat((x1 + x2) / 2, (y1 + y2) / 2)})
        if point and -6 <= point[0] <= 10 and 41 <= point[1] <= 52:
            points[row[0]] = (point, _clean(row[3 + offset], 24))
    return points


def _parse_france_sensors(root, references, now=None):
    now = time.time() if now is None else now
    published = _timestamp(root.findtext('.//d:publicationTime', namespaces=_DATEX_NS))
    if published is None or not -600 <= now - published <= 45 * 60:
        raise ValueError('French traffic sensor publication is stale or invalid')
    features = []
    for item in root.findall('.//d:siteMeasurements', _DATEX_NS):
        reference = item.find('d:measurementSiteReference', _DATEX_NS)
        station = reference.get('id') if reference is not None else None
        if station not in references:
            continue
        measured_at = item.findtext('d:measurementTimeDefault', namespaces=_DATEX_NS)
        measured = _timestamp(measured_at)
        if measured is None or not -600 <= now - measured <= 30 * 60:
            continue
        try:
            speed = float(item.findtext('.//d:averageVehicleSpeed/d:speed', namespaces=_DATEX_NS))
        except (TypeError, ValueError):
            speed = None
        try:
            flow = float(item.findtext('.//d:vehicleFlow/d:vehicleFlowRate', namespaces=_DATEX_NS))
        except (TypeError, ValueError):
            flow = None
        speed = speed if speed is not None and math.isfinite(speed) and 0 < speed <= 200 else None
        flow = flow if flow is not None and math.isfinite(flow) and 0 <= flow <= 10000 else None
        if speed is None and not flow:
            continue
        point, road = references[station]
        detail = ' · '.join(filter(None, [f'{speed:.0f} km/h' if speed is not None else '',
                                          f'{flow:.0f} vehicles/h' if flow is not None else '']))
        features.append(_feature(point, {
            'key': f'fr:sensor:{station}', 'layer': 'sensors',
            'title': f'{road} · road sensor' if road else 'Road sensor', 'detail': detail,
            'source': 'Bison Futé / DIR · Licence Ouverte 2.0',
            'source_url': FRANCE_SENSOR_SOURCE, 'updated_at': measured_at,
        }))
    return features


def _france_sensors():
    now = time.time()
    if now >= _FRANCE_SENSOR_REFERENCES['until']:
        request = urllib.request.Request(FRANCE_SENSOR_BASE + 'refDir.csv', headers={
            'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'text/csv'})
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError('French sensor reference exceeded 2 MB')
        references = _parse_france_sensor_references(body.decode('utf-8-sig'))
        if not references:
            raise ValueError('French sensor reference has no usable locations')
        _FRANCE_SENSOR_REFERENCES.update({'until': now + 12 * 3600, 'points': references})
    return _parse_france_sensors(_get_xml(FRANCE_SENSOR_BASE + 'qtvDir.xml'),
                                 _FRANCE_SENSOR_REFERENCES['points'], now)


def _lambert72_to_lonlat(x, y):
    """EPSG:31370 inverse LCC and BD72→WGS84 (3) seven-parameter transform."""
    radians = math.pi / 180
    a, flattening = 6378388.0, 1 / 297.0  # International 1924 ellipsoid
    eccentricity_sq = 2 * flattening - flattening * flattening
    eccentricity = math.sqrt(eccentricity_sq)

    def m(latitude):
        return math.cos(latitude) / math.sqrt(1 - eccentricity_sq * math.sin(latitude) ** 2)

    def t(latitude):
        sine = math.sin(latitude)
        return math.tan(math.pi / 4 - latitude / 2) * (
            (1 + eccentricity * sine) / (1 - eccentricity * sine)) ** (eccentricity / 2)

    first, second = 51.1666672333333 * radians, 49.8333339 * radians
    n = math.log(m(first) / m(second)) / math.log(t(first) / t(second))
    factor = m(first) / (n * t(first) ** n)
    dx, dy = x - 150000.013, 5400088.438 - y
    theta = math.atan2(dx, dy)
    projected_t = (math.hypot(dx, dy) / (a * factor)) ** (1 / n)
    latitude = math.pi / 2 - 2 * math.atan(projected_t)
    for _ in range(8):
        sine = math.sin(latitude)
        latitude = math.pi / 2 - 2 * math.atan(projected_t * (
            (1 - eccentricity * sine) / (1 + eccentricity * sine)) ** (eccentricity / 2))
    longitude = 4.36748666666667 * radians + theta / n

    prime_vertical = a / math.sqrt(1 - eccentricity_sq * math.sin(latitude) ** 2)
    X = prime_vertical * math.cos(latitude) * math.cos(longitude)
    Y = prime_vertical * math.cos(latitude) * math.sin(longitude)
    Z = prime_vertical * (1 - eccentricity_sq) * math.sin(latitude)
    # EPSG operation 15929 uses coordinate-frame rotations; signs below are
    # inverted for the equivalent position-vector form of the Helmert equation.
    rx, ry, rz = (angle * radians / 3600 for angle in (0.3366, -0.457, 1.8422))
    scale = 1 - 1.2747e-6
    X, Y, Z = (-106.8686 + scale * X - rz * Y + ry * Z,
               52.2978 + rz * X + scale * Y - rx * Z,
               -103.7239 - ry * X + rx * Y + scale * Z)

    wgs_a, wgs_flattening = 6378137.0, 1 / 298.257223563
    wgs_eccentricity_sq = 2 * wgs_flattening - wgs_flattening * wgs_flattening
    longitude = math.atan2(Y, X)
    distance = math.hypot(X, Y)
    latitude = math.atan2(Z, distance * (1 - wgs_eccentricity_sq))
    for _ in range(8):
        prime_vertical = wgs_a / math.sqrt(1 - wgs_eccentricity_sq * math.sin(latitude) ** 2)
        latitude = math.atan2(Z + wgs_eccentricity_sq * prime_vertical * math.sin(latitude), distance)
    return longitude / radians, latitude / radians


def _belgium_road_point(record):
    """DATEX v3 geometry is Belgian Lambert 72 (EPSG:31370), not decimal degrees."""
    line = record.find('.//{*}gmlLineString')
    if line is not None and line.get('srsName') == 'EPSG:31370':
        pos_list = line.findtext('{*}posList')
        try:
            values = [float(value) for value in pos_list.split()]
            if len(values) >= 2 and len(values) % 2 == 0:
                midpoint = (len(values) // 4) * 2
                x, y = values[midpoint:midpoint + 2]
            else:
                return None
        except (AttributeError, ValueError):
            return None
    else:
        try:
            x = float(record.findtext('.//{*}pointCoordinates/{*}longitude'))
            y = float(record.findtext('.//{*}pointCoordinates/{*}latitude'))
        except (TypeError, ValueError):
            return None
    if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 300000 and 0 <= y <= 300000):
        return None
    lon, lat = _lambert72_to_lonlat(x, y)
    return [lon, lat] if math.isfinite(lon) and math.isfinite(lat) and 2.3 <= lon <= 6.5 and 49.4 <= lat <= 51.6 else None


def _belgium_otap_road_names(root):
    names = {}
    for situation in root.findall('situation'):
        reference = situation.findtext('./key/situationReference', default='')
        match = re.search(r'(\d+)$', reference)
        road = _clean(situation.findtext('.//milestone/roadName'), 48)
        if match and road:
            names[match.group(1)] = road
    return names


def _parse_belgium_roads(root, road_names=None, now=None):
    now = time.time() if now is None else now
    published = _timestamp(root.findtext('{*}publicationTime'))
    if published is None or not -600 <= now - published <= 30 * 60:
        raise ValueError('Flemish road publication is stale or invalid')
    road_names = road_names or {}
    features = []
    work_types = {'MaintenanceWorks', 'ConstructionWorks'}
    incident_types = {'Accident', 'AbnormalTraffic', 'EnvironmentalObstruction',
                      'GeneralObstruction', 'InfrastructureDamageObstruction',
                      'VehicleObstruction', 'WeatherRelatedRoadConditions'}
    management_types = {'RoadOrCarriagewayOrLaneManagement', 'GeneralNetworkManagement',
                        'ReroutingManagement', 'SpeedManagement'}
    conditions = {'newRoadworksLayout': 'Roadworks layout', 'narrowLanes': 'Narrow lanes',
                  'roadClosed': 'Road closed', 'singleAlternateLineTraffic': 'Alternating traffic'}
    for situation in root.findall('{*}situation'):
        records = []
        for record in situation.findall('{*}situationRecord'):
            kind = record.get(_DATEX_TYPE, '').split(':')[-1]
            if kind not in work_types | incident_types | management_types:
                continue
            if record.findtext('.//{*}validityStatus') != 'active':
                continue
            start = _timestamp(record.findtext('.//{*}overallStartTime'))
            end = _timestamp(record.findtext('.//{*}overallEndTime'))
            if (start is not None and start > now) or (end is not None and end < now):
                continue
            records.append(record)
        if not records:
            continue
        point = next((p for record in records if (p := _belgium_road_point(record)) is not None), None)
        if point is None:
            continue
        kinds = {record.get(_DATEX_TYPE, '').split(':')[-1] for record in records}
        management = {record.findtext('.//{*}roadOrCarriagewayOrLaneManagementType') for record in records}
        is_work = bool(kinds & work_types or 'newRoadworksLayout' in management)
        layer = 'construction' if is_work else 'incidents'
        identifier = re.search(r'(\d+)$', situation.get('id', ''))
        road = road_names.get(identifier.group(1), '') if identifier else ''
        label = 'Roadworks' if is_work else 'Road disruption'
        details = [conditions[value] for value in conditions if value in management]
        if not details and kinds & work_types:
            details = ['Maintenance work']
        features.append(_feature(point, {
            'key': f'be:flemish:{situation.get("id")}', 'layer': layer,
            'title': f'{label} · {road}' if road else f'{label} · Flanders',
            'detail': ' · '.join(details) or label,
            'source': 'Vlaams Verkeerscentrum · Modellicentie Gratis Hergebruik',
            'source_url': BELGIUM_ROADS_SOURCE,
            'updated_at': situation.findtext('{*}situationVersionTime', default=''),
        }))
    return features


def _belgium_roads():
    feed = _get_xml(BELGIUM_ROADS_URL)
    try:
        names = _belgium_otap_road_names(_get_xml('https://www.verkeerscentrum.be/uitwisseling/otap'))
    except (OSError, ValueError, ET.ParseError):
        names = {}
    return _parse_belgium_roads(feed, names)


_GIPOD_ROAD_IMPACT = re.compile(
    r'rijstro|rijrichting|rijweg|gemotoriseerd verkeer|wisselend verkeer|'
    r'snelheidsbeperking|tweerichtingsverkeer', re.I)


def _parse_gipod_roadworks(items, now=None):
    now = time.time() if now is None else now
    features = []
    for item in items:
        properties = item.get('properties') or {}
        point = _point(item.get('geometry'))
        if not point or not (2.5 <= point[0] <= 6.5 and 49.5 <= point[1] <= 51.6):
            continue
        if properties.get('HindranceStatus') != 'Gevalideerd':
            continue
        cause = properties.get('HindranceConsequenceOf') or ''
        if '/groundworks/' not in cause and '/works/' not in cause:
            continue
        consequences = _clean(properties.get('Consequences'), 180)
        if not _GIPOD_ROAD_IMPACT.search(consequences):
            continue
        start = _timestamp(properties.get('HindranceStart'))
        end = _timestamp(properties.get('HindranceEnd'))
        if start is None or end is None or start > now or end < now:
            continue
        zone = _clean(properties.get('ZoneId'), 80)
        if not zone:
            continue
        description = _clean(properties.get('HindranceDescription'), 120)
        place = _clean(description.split(':', 1)[0], 65)
        details = [consequences.replace(';', ' · ')]
        if description:
            details.append(description)
        features.append(_feature(point, {
            'key': f'be:gipod:{zone}', 'layer': 'construction',
            'title': f'Road work · {place}' if place else 'Road work · Flanders',
            'detail': _clean(' · '.join(details), 280),
            'source': 'GIPOD · Digitaal Vlaanderen · Modellicentie Gratis Hergebruik',
            'source_url': properties.get('HindranceURI') if str(properties.get('HindranceURI') or '').startswith(
                'https://gipod.api.vlaanderen.be/api/v1/mobility-hindrances/') else GIPOD_SOURCE,
            'updated_at': properties.get('HindranceLastModifiedOn') or '',
        }))
    return features


def _gipod_tile(key):
    with _GIPOD_CACHE_LOCK:
        cached = _GIPOD_TILE_CACHE.get(key)
        if cached and cached['until'] > time.time():
            return cached['items']
        tile_lock = _GIPOD_TILE_LOCKS.setdefault(key, threading.Lock())
    with tile_lock:
        with _GIPOD_CACHE_LOCK:
            cached = _GIPOD_TILE_CACHE.get(key)
            if cached and cached['until'] > time.time():
                return cached['items']
        west, south = key[0] / 4, key[1] / 4
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        query_filter = (f"HindranceStart <= '{now}' AND HindranceEnd >= '{now}' AND "
                        "(Consequences LIKE '%rij%' OR Consequences LIKE '%Rij%' OR Consequences LIKE '%verkeer%' "
                        "OR Consequences LIKE '%Snelheidsbeperking%')")
        items = []
        for page in range(4):
            query = urllib.parse.urlencode({
                'f': 'json', 'limit': 500,
                'bbox': f'{west:.2f},{south:.2f},{west + .25:.2f},{south + .25:.2f}',
                'filter': query_filter, 'startIndex': page * 500,
            })
            data = _get_json(f'{GIPOD_POINT_URL}?{query}')
            rows = data.get('features')
            if not isinstance(rows, list):
                raise ValueError('GIPOD returned no feature list')
            items.extend(rows)
            if not any(link.get('rel') == 'next' for link in data.get('links') or []):
                break
        else:
            raise ValueError('GIPOD tile exceeded four pages')
        with _GIPOD_CACHE_LOCK:
            _GIPOD_TILE_CACHE[key] = {'until': time.time() + 300, 'items': items}
        return items


def _gipod_roadworks(bbox):
    west, south, east, north = bbox
    west, south, east, north = max(west, 2.5), max(south, 49.5), min(east, 6.5), min(north, 51.6)
    if west >= east or south >= north:
        return []
    keys = [(x, y) for x in range(math.floor(west * 4), math.floor((east - 1e-9) * 4) + 1)
            for y in range(math.floor(south * 4), math.floor((north - 1e-9) * 4) + 1)]
    if not keys:
        return []
    if len(keys) > 24:
        raise ValueError('GIPOD view is too wide; zoom closer')
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(keys))) as executor:
        pages = list(executor.map(_gipod_tile, keys))
    features = _parse_gipod_roadworks(item for page in pages for item in page)
    return [item for item in features if west <= item['geometry']['coordinates'][0] <= east
            and south <= item['geometry']['coordinates'][1] <= north]


def _ndw_record_point(record):
    latitude = record.findtext('.//{*}pointCoordinates/{*}latitude')
    longitude = record.findtext('.//{*}pointCoordinates/{*}longitude')
    point = _point({'coordinates': [longitude, latitude]}) if latitude and longitude else None
    if point is None:
        line = record.find('.//{*}gmlLineString')
        if line is None or line.get('srsName') != 'WGS 84':
            return None
        try:
            numbers = [float(value) for value in line.findtext('{*}posList').split()]
        except (AttributeError, ValueError):
            return None
        if len(numbers) < 2 or len(numbers) % 2:
            return None
        midpoint = (len(numbers) // 4) * 2
        point = _point({'coordinates': [numbers[midpoint + 1], numbers[midpoint]]})
    return point if point and 3.0 <= point[0] <= 7.4 and 50.6 <= point[1] <= 53.8 else None


def _parse_ndw_roads(root, now=None):
    now = time.time() if now is None else now
    payload = next((item for item in root if item.get(_DATEX_TYPE, '').endswith('SituationPublication')), None)
    if payload is None:
        raise ValueError('NDW current traffic payload is missing')
    published = _timestamp(payload.findtext('{*}publicationTime'))
    if published is None or not -600 <= now - published <= 20 * 60:
        raise ValueError('NDW current traffic publication is stale or invalid')
    features = []
    work_causes = {'roadMaintenance', 'constructionWork'}
    work_types = {'MaintenanceWorks', 'ConstructionWorks'}
    incident_types = {'Accident', 'VehicleObstruction', 'GeneralObstruction',
                      'EnvironmentalObstruction', 'PoorEnvironmentConditions',
                      'AbnormalTraffic', 'WeatherRelatedRoadConditions'}
    management_types = {'RoadOrCarriagewayOrLaneManagement', 'ReroutingManagement',
                        'SpeedManagement', 'GeneralNetworkManagement'}
    labels = {'carriagewayClosures': 'Carriageway closed', 'laneClosures': 'Lane closed',
              'roadClosed': 'Road closed', 'narrowLanes': 'Narrow lanes',
              'lanesDeviated': 'Lanes diverted', 'hardShoulderRunningInOperation': 'Hard shoulder open'}
    for situation in payload.findall('{*}situation'):
        records = []
        for record in situation.findall('{*}situationRecord'):
            kind = record.get(_DATEX_TYPE, '').split(':')[-1]
            if kind not in work_types | incident_types | management_types:
                continue
            if record.findtext('.//{*}validityStatus') not in {'active', 'definedByValidityTimeSpec'}:
                continue
            start = _timestamp(record.findtext('.//{*}overallStartTime'))
            end = _timestamp(record.findtext('.//{*}overallEndTime'))
            if (start is not None and start > now) or (end is not None and end < now):
                continue
            records.append(record)
        if not records:
            continue
        point = next((p for record in records if (p := _ndw_record_point(record)) is not None), None)
        if point is None:
            continue
        kinds = {record.get(_DATEX_TYPE, '').split(':')[-1] for record in records}
        causes = {record.findtext('.//{*}causeType') for record in records}
        is_work = bool(kinds & work_types or causes & work_causes)
        layer = 'construction' if is_work else 'incidents'
        management = [record.findtext('.//{*}roadOrCarriagewayOrLaneManagementType') for record in records]
        details = list(dict.fromkeys(labels[value] for value in management if value in labels))
        if not details:
            details = ['Road maintenance'] if is_work else ['Traffic obstruction'] if 'VehicleObstruction' in kinds else []
        title = ('Roadworks' if is_work else 'Crash' if 'Accident' in kinds else
                 'Vehicle obstruction' if 'VehicleObstruction' in kinds else
                 'Road restriction' if kinds & management_types else 'Road incident')
        updated = max((record.findtext('{*}situationRecordVersionTime', default='') for record in records),
                      default='')
        features.append(_feature(point, {
            'key': f'nl:ndw:{situation.get("id")}', 'layer': layer,
            'title': f'{title} · Netherlands', 'detail': ' · '.join(details) or title,
            'source': 'NDW Open Data', 'source_url': NDW_SOURCE, 'updated_at': updated,
        }))
    return features


def _ndw_roads():
    return _parse_ndw_roads(_get_gzip_xml(NDW_BASE + 'actueel_beeld.xml.gz'))


def _parse_ndw_signs(root, now=None):
    now = time.time() if now is None else now
    payloads = [item for item in root if item.tag.rsplit('}', 1)[-1] == 'payload']
    table = next((item for item in payloads if item.get(_DATEX_TYPE, '').endswith('VmsTablePublication')), None)
    statuses = next((item for item in payloads if item.get(_DATEX_TYPE, '').endswith('VmsPublication')), None)
    if table is None or statuses is None:
        raise ValueError('NDW sign table or current status is missing')
    published = _timestamp(statuses.findtext('{*}publicationTime'))
    if published is None or not -600 <= now - published <= 20 * 60:
        raise ValueError('NDW sign publication is stale or invalid')
    controllers = {}
    for controller in table.findall('.//{*}vmsController'):
        identifier = controller.get('id')
        latitude = controller.findtext('.//{*}pointCoordinates/{*}latitude')
        longitude = controller.findtext('.//{*}pointCoordinates/{*}longitude')
        point = _point({'coordinates': [longitude, latitude]}) if latitude and longitude else None
        if identifier and point and 3.0 <= point[0] <= 7.4 and 50.6 <= point[1] <= 53.8:
            controllers[identifier] = (point, _clean(controller.findtext('.//{*}value'), 70))
    features = []
    for status in statuses.findall('{*}vmsControllerStatus'):
        reference = status.find('{*}vmsControllerReference')
        identifier = reference.get('id') if reference is not None else None
        if identifier not in controllers or status.findtext('.//{*}workingStatus') != 'working':
            continue
        lines = [_clean(item.text, 80) for item in status.findall('.//{*}textLine')]
        lines = [line for line in lines if line]
        image = status.findtext('.//{*}imageData') or ''
        if not (500 <= len(image) <= 150000 and status.findtext('.//{*}imageFormat') == 'png'):
            image = ''
        else:
            try:
                if not base64.b64decode(image, validate=True).startswith(b'\x89PNG\r\n\x1a\n'):
                    image = ''
            except (ValueError, base64.binascii.Error):
                image = ''
        if not lines and not image:
            continue
        point, name = controllers[identifier]
        features.append(_feature(point, {
            'key': f'nl:ndw:sign:{identifier}', 'layer': 'signs',
            'title': name or 'Digital road sign',
            'detail': ' / '.join(lines) if lines else 'Current sign display',
            'image_data': image, 'source': 'NDW Open Data · dynamic road signs',
            'source_url': NDW_BASE, 'updated_at': status.findtext('{*}statusUpdateTime', default=''),
        }))
    return features


def _ndw_signs():
    return _parse_ndw_signs(_get_gzip_xml(NDW_BASE + 'dynamische_route_informatie_paneel.xml.gz'))


def _parse_autobahn_items(service, road, payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get(service, []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise ValueError('Autobahn road service returned an invalid list')
    features = []
    for row in rows:
        if not isinstance(row, dict) or row.get('future') is True:
            continue
        started = _timestamp(row.get('startTimestamp'))
        if started is not None and started > now:
            continue
        location = row.get('coordinate')
        if not isinstance(location, dict):
            continue
        point = _point({'coordinates': [location.get('long'), location.get('lat')]})
        if point is None or not 5.5 <= point[0] <= 15.5 or not 47 <= point[1] <= 55.1:
            continue
        identifier = str(row.get('identifier') or '')[:250]
        if not identifier:
            continue
        title = _clean(row.get('title'), 100) or road
        subtitle = _clean(row.get('subtitle'), 80).replace('->', '→')
        description = row.get('description')
        descriptions = [_clean(part, 180) for part in description if isinstance(part, str)] if isinstance(description, list) else []
        descriptions = [part for part in descriptions if part]
        if service == 'warning':
            event = _clean(row.get('abnormalTrafficType'), 50).replace('_', ' ').capitalize()
            note = ' · '.join(part for part in descriptions if part.startswith('- '))[:180]
            detail = ' · '.join(part for part in (event, subtitle, note or (descriptions[-1] if descriptions else '')) if part)
            layer, label = 'incidents', 'Traffic warning'
        else:
            detail = ' · '.join(part for part in (subtitle, descriptions[-1] if descriptions else '') if part)
            layer, label = ('incidents', 'Road closure') if service == 'closure' else ('construction', 'Roadworks')
        features.append(_feature(point, {
            'key': f'de:autobahn:{service}:{identifier}', 'layer': layer,
            'title': f'{label} · {title}', 'detail': detail,
            'source': 'Autobahn GmbH' + (' / INRIX' if row.get('source') == 'inrix' else ''),
            'source_url': AUTOBAHN_SOURCE,
        }))
    return features


def _autobahn_service(service):
    if service not in _AUTOBAHN_CACHE:
        raise ValueError('Unknown Autobahn service')
    cache = _AUTOBAHN_CACHE[service]
    with cache['lock']:
        now = time.time()
        if now < cache['until']:
            return [feature for rows in cache['roads'].values() for feature in rows]
        catalog = _get_json(AUTOBAHN_BASE)
        roads = catalog.get('roads', []) if isinstance(catalog, dict) else []
        roads = [road for road in roads if isinstance(road, str) and re.fullmatch(r'A\d{1,3}', road)]
        if not roads or len(roads) > 150:
            raise ValueError('Autobahn road catalog is invalid')
        next_roads = dict(cache['roads'])
        successes = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_get_json, AUTOBAHN_BASE + road + '/services/' + service): road
                       for road in roads}
            for future in concurrent.futures.as_completed(futures):
                road = futures[future]
                try:
                    next_roads[road] = _parse_autobahn_items(service, road, future.result(), now)
                    successes += 1
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        if successes < math.ceil(len(roads) * 0.8):
            raise ValueError('Autobahn road service is unavailable')
        cache['roads'] = {road: next_roads[road] for road in roads if road in next_roads}
        cache['until'] = now + (300 if service == 'warning' else 900)
        return [feature for rows in cache['roads'].values() for feature in rows]


_FETCHERS = {
    'roads': {
        'fi_signs': _fintraffic_signs,
        'fi_incidents': lambda: _fintraffic_messages('incidents'),
        'fi_construction': lambda: _fintraffic_messages('construction'),
        'uk_london': _tfl_disruptions,
        'uk_wales_incidents': lambda: _wales_feed('incidents'),
        'uk_wales_construction': lambda: _wales_feed('construction'),
        'uk_scotland_construction': _scotland_roadworks,
        'fr_national_roads': _france_roads,
        'fr_traffic_sensors': _france_sensors,
        'be_flemish_roads': _belgium_roads,
        'nl_ndw_roads': _ndw_roads,
        'nl_ndw_signs': _ndw_signs,
        'ch_zurich_roadworks': _zurich_roadworks,
        'ch_zurich_sensors': _zurich_sensors,
        'no_road_events': _norway_roads,
        'no_road_cameras': _norway_cameras,
    },
    'power': {'ukpn': _ukpn_outages, 'npg': _npg_outages, 'ssen': _ssen_outages,
              'nged': _nged_outages},
}


def _snapshot(kind):
    now = time.time()
    with _LOCKS[kind]:
        cache = _CACHE[kind]
        if now < cache['until']:
            return cache
        sources = dict(cache['sources'])
        source_times = dict(cache['source_times'])
        errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(_FETCHERS[kind])) as executor:
            futures = {executor.submit(loader): name for name, loader in _FETCHERS[kind].items()}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    sources[name] = future.result()
                    source_times[name] = now
                except Exception as error:
                    errors.append(f'{name}: {error}')
                    if now - source_times.get(name, 0) > _STALE_SECONDS:
                        sources.pop(name, None)
        cache.update({'until': now + (180 if kind == 'roads' else 300), 'sources': sources,
                      'source_times': source_times, 'errors': errors})
        return cache


def road_snapshot(layer, bbox=None):
    if layer not in {'signs', 'incidents', 'construction', 'sensors', 'cameras'}:
        raise ValueError('Unknown road layer')
    if bbox is not None:
        west, south, east, north = bbox
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError('Invalid road bounds')
    snapshot = _snapshot('roads')
    features = [item for rows in snapshot['sources'].values() for item in rows if item['properties']['layer'] == layer]
    errors = list(snapshot['errors'])
    sources = list(snapshot['sources'])
    if layer == 'construction' and bbox is not None:
        try:
            features.extend(_gipod_roadworks(bbox))
            sources.append('be_gipod_roadworks')
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f'be_gipod_roadworks: {error}')
    if bbox is not None and layer in {'incidents', 'construction'} and west <= 15.5 and east >= 5.5 and south <= 55.1 and north >= 47:
        services = ('warning', 'closure') if layer == 'incidents' else ('roadworks',)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(services)) as executor:
            futures = {executor.submit(_autobahn_service, service): service for service in services}
            for future in concurrent.futures.as_completed(futures):
                service = futures[future]
                try:
                    features.extend(future.result())
                    sources.append(f'de_autobahn_{service}')
                except (OSError, ValueError, KeyError, TypeError) as error:
                    errors.append(f'de_autobahn_{service}: {error}')
    if bbox is not None:
        features = [item for item in features if west <= item['geometry']['coordinates'][0] <= east
                    and south <= item['geometry']['coordinates'][1] <= north]
    return {'type': 'FeatureCollection', 'features': features, 'sourceErrors': errors,
            'sources': sources}


def power_snapshot():
    snapshot = _snapshot('power')
    features = [item for rows in snapshot['sources'].values() for item in rows]
    return {'type': 'FeatureCollection', 'features': features, 'sourceErrors': snapshot['errors'],
            'sources': list(snapshot['sources'])}
