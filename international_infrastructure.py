"""Public road and electricity feeds outside North America, normalized for the map."""

import concurrent.futures
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

from pyproj import Transformer


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
_BELGIUM_TO_WGS84 = Transformer.from_crs('EPSG:31370', 'EPSG:4326', always_xy=True)
FRANCE_SENSOR_SOURCE = ('https://transport.data.gouv.fr/datasets/'
                        'etat-de-circulation-en-temps-reel-sur-le-reseau-national-routier-non-concede')
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
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
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
    lon, lat = _BELGIUM_TO_WGS84.transform(x, y)
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
    if layer not in {'signs', 'incidents', 'construction', 'sensors'}:
        raise ValueError('Unknown road layer')
    snapshot = _snapshot('roads')
    features = [item for rows in snapshot['sources'].values() for item in rows if item['properties']['layer'] == layer]
    if bbox is not None:
        west, south, east, north = bbox
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError('Invalid road bounds')
        features = [item for item in features if west <= item['geometry']['coordinates'][0] <= east
                    and south <= item['geometry']['coordinates'][1] <= north]
    return {'type': 'FeatureCollection', 'features': features, 'sourceErrors': snapshot['errors'],
            'sources': list(snapshot['sources'])}


def power_snapshot():
    snapshot = _snapshot('power')
    features = [item for rows in snapshot['sources'].values() for item in rows]
    return {'type': 'FeatureCollection', 'features': features, 'sourceErrors': snapshot['errors'],
            'sources': list(snapshot['sources'])}
