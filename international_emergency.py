"""Small, attributed set of official emergency feeds outside the US/Canada."""

import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache
from zoneinfo import ZoneInfo

from pyproj import Transformer
from armenia_reports import rescue_reports


NSW_URL = 'https://www.rfs.nsw.gov.au/feeds/majorIncidents.json'
VIC_URL = 'https://data.emergency.vic.gov.au/Show?pageId=getIncidentJSON'
QLD_URL = 'https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com/content/Feeds/BushfireCurrentIncidents/bushfireAlert_capau.xml'
NZ_URL = 'https://alerthub.civildefence.govt.nz/atom/pwp'
ENGLAND_URL = 'https://environment.data.gov.uk/flood-monitoring/id/floods.json?min-severity=3'
BURGENLAND_URL = 'https://einsatz.lsz-b.at/'
UPPER_AUSTRIA_URL = 'https://cf-einsaetze.ooelfv.at/webext2/rss/json_laufend.txt'
UPPER_AUSTRIA_SOURCE = 'https://einsaetze.ooelfv.at/einsatz/aktuell'
ICELAND_URL = 'https://api.vedur.is/capbroker/active/detailed/all'
PORTUGAL_SOURCE = 'https://dados.gov.pt/en/datasets/prociv-ocorrencias-em-aberto'
CATALONIA_FIRE_SOURCE = 'https://interior.gencat.cat/ca/arees_dactuacio/bombers/actuacions-de-bombers/'
ZARAGOZA_FIRE_SOURCE = 'https://www.zaragoza.es/sede/portal/bomberos/servicios/servicio/bomberos/?tipo=10'
ZARAGOZA_FIRE_URL = 'https://www.zaragoza.es/sede/servicio/bomberos.json?tipo={kind}&rows=500'
ZARAGOZA_STREETS_URL = ('https://idezar-sig.zaragoza.es/servicios/geoserver/urbanismo/wfs?'
                        'service=WFS&version=2.0.0&request=GetFeature&typeName=urbanismo:Vias&'
                        'outputFormat=application/json&srsName=EPSG:4326&'
                        'propertyName=nombre,nombre_publico,tipo_via,geom')
CATALONIA_FIRE_URL = ('https://services7.arcgis.com/ZCqVt1fRXwwK6GF4/arcgis/rest/services/'
                      'ACTUACIONS_URGENTS_online_PRO_AMB_FASE_VIEW/FeatureServer/0/query?'
                      'where=1%3D1&outFields=GlobalID%2CESRI_OID%2CTAL_DESC_ALARMA2%2C'
                      'ACT_DAT_FI%2CACT_URGENT%2CMUNICIPI_DPX%2CDATA_ACT%2CCOM_FASE&'
                      'returnGeometry=true&outSR=4326&resultRecordCount=1000&f=geojson')
SWEDEN_VMA_URL = 'https://vmaapi.sr.se/api/v3/alerts'
SWEDEN_POLICE_URL = 'https://polisen.se/api/events'
NORWAY_POLICE_URL = ('https://api.politiloggen.politiet.no/messagethreads?'
                     'TimeSpanType=LastDay&SortByEnum=LastMessageOn&Take=500')
POLAND_RSO_URL = 'https://komunikaty.tvp.pl/komunikatyxml/wszystkie/ogolne/0?_format=json'
POLAND_RSO_SOURCE = 'https://komunikaty.tvp.pl/komunikaty/wszystkie/ogolne'
NETHERLANDS_P2000_URL = 'https://zwaailicht.nl/api/v1/alerts?limit=100'
NETHERLANDS_P2000_SOURCE = 'https://zwaailicht.nl/'
LUXEMBOURG_ALERT_CATALOG = 'https://data.public.lu/api/1/datasets/67aca67bcaea3ae62308114f/'
LUXEMBOURG_ALERT_SOURCE = 'https://data.public.lu/en/datasets/alertes-du-systeme-lu-alert/'
USTI_EMERGENCY_URL = 'https://pkr.kr-ustecky.cz/pkr/zasahy-jednotek-pozarni-ochrany/'
PORTUGAL_URL = ('https://services-eu1.arcgis.com/VlrHb7fn5ewYhX6y/arcgis/rest/services/'
                'OcorrenciasSite/FeatureServer/0/query?where=1%3D1&outFields='
                'ID_oc%2CNumero%2CEstadoAgrupado%2CNatureza%2CConcelho%2CRegiao%2C'
                'Operacionais%2CMeiosTerrestres%2CMeiosAereos%2CDataDosDados&'
                'returnGeometry=true&outSR=4326&f=json')
THAILAND_DDPM_WFS = 'https://wgeo.disaster.go.th/geoserver/wfs'
THAILAND_DDPM_SOURCE = 'https://ddc.disaster.go.th/'
INDONESIA_BNPB_URL = 'https://gis.bnpb.go.id/databencana/tabel/pencarian.php'
_ATOM = '{http://www.w3.org/2005/Atom}'
_CAP = '{urn:oasis:names:tc:emergency:cap:1.2}'
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'value': None, 'sources': {}, 'source_times': {}}
_REFRESH_LOCK = threading.Lock()
_SWEDEN_POLICE_LOCK = threading.Lock()
_SWEDEN_POLICE_CACHE = {'until': 0, 'items': None}
_P2000_HTTP_CACHE = {'etag': None, 'payload': None}
_P2000_HTTP_LOCK = threading.Lock()
_ZARAGOZA_STREETS_CACHE = {'until': 0, 'lookup': None}
_ZARAGOZA_STREETS_LOCK = threading.Lock()
_JTSK_TO_WGS84 = Transformer.from_crs('EPSG:5514', 'EPSG:4326', always_xy=True)


def _get(url, context=None):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public emergency feed reader)', 'Accept': 'application/json, application/atom+xml, application/xml'})
    with urllib.request.urlopen(request, timeout=15, context=context) as response:
        return response.read(8 * 1024 * 1024 + 1)


def _json(url, context=None):
    body = _get(url, context=context)
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('Emergency feed exceeded 8 MB')
    return json.loads(body)


def _xml(url):
    body = _get(url)
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('Emergency feed exceeded 8 MB')
    return ET.fromstring(body)


def _html(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public emergency feed reader)', 'Accept': 'text/html'})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError('Emergency dispatch page exceeded 1 MB')
    return body


def _clean(value, limit=300):
    value = re.sub(r'<[^>]*>', ' ', str(value or ''))
    return ' '.join(html.unescape(value).split())[:limit]


def _iso(value, zone='UTC'):
    if not value:
        return ''
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(zone))
        return parsed.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return ''


def _valid(lon, lat):
    try:
        lon, lat = float(lon), float(lat)
        return -180 <= lon <= 180 and -90 <= lat <= 90
    except (ValueError, TypeError):
        return False


def _item(key, lon, lat, title, detail, source, source_url, observed, category='warning'):
    return {'id': key, 'lon': float(lon), 'lat': float(lat), 'title': _clean(title, 140),
            'detail': _clean(detail), 'source': source, 'sourceUrl': source_url,
            'observed': observed, 'category': category}


NEPAL_BIPAD_API = 'https://bipadportal.gov.np/api/v1/incident/'
NEPAL_BIPAD_SOURCE = 'https://bipadportal.gov.np/incidents/'


def parse_nepal_bipad(payload, now=None):
    """Recent, approved BIPAD disaster reports with supplied Nepal coordinates."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = payload.get('results') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError('Nepal BIPAD incident page is invalid')
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('verified') is not True or row.get('approved') is not True:
            continue
        identifier = row.get('id')
        if not isinstance(identifier, int) or identifier < 1 or identifier in seen:
            continue
        reported = _iso(row.get('reportedOn'))
        occurred = _iso(row.get('incidentOn'))
        if not reported or not occurred:
            continue
        reported_at = dt.datetime.fromisoformat(reported.replace('Z', '+00:00'))
        occurred_at = dt.datetime.fromisoformat(occurred.replace('Z', '+00:00'))
        if not (dt.timedelta(minutes=-5) <= now - reported_at <= dt.timedelta(days=3)):
            continue
        if not (dt.timedelta(minutes=-5) <= now - occurred_at <= dt.timedelta(days=7)):
            continue
        point = (row.get('point') or {}).get('coordinates') if isinstance(row.get('point'), dict) else None
        if not isinstance(point, list) or len(point) < 2 or not _valid(point[0], point[1]):
            continue
        lon, lat = float(point[0]), float(point[1])
        if not (80 <= lon <= 88.5 and 26 <= lat <= 31):
            continue
        title = _clean(row.get('title'), 140)
        if not title:
            continue
        label = title.lower()
        category = ('fire' if 'fire' in label else
                    'traffic' if 'road accident' in label or 'vehicle accident' in label else
                    'medical' if 'snake bite' in label else 'warning')
        seen.add(identifier)
        output.append(_item(f'np:bipad:{identifier}', lon, lat, title,
                            'Reported incident · mapped location may be approximate',
                            'Nepal BIPAD', f'{NEPAL_BIPAD_SOURCE}{identifier}/', reported, category))
    return output


def _nepal_bipad():
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).strftime('%Y-%m-%d')
    items, seen = [], set()
    for offset in range(0, 2000, 500):
        query = urllib.parse.urlencode({'reported_on__gt': cutoff, 'ordering': '-reported_on',
                                        'limit': 500, 'offset': offset})
        payload = _json(f'{NEPAL_BIPAD_API}?{query}')
        rows = payload.get('results') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError('Nepal BIPAD incident feed is invalid')
        for item in parse_nepal_bipad(payload):
            if item['id'] not in seen:
                seen.add(item['id'])
                items.append(item)
        if len(rows) < 500:
            return items
    raise ValueError('Nepal BIPAD incident feed exceeds supported page count')


def parse_thailand_ddpm(payload, now=None):
    """Recent, still-open disaster reports from Thailand's public DDPM map."""
    now = now or dt.datetime.now(dt.timezone.utc)
    features = payload.get('features') if isinstance(payload, dict) else None
    if not isinstance(features, list) or len(features) >= 1000:
        raise ValueError('Thailand DDPM disaster feed is invalid')
    output = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get('properties') or {}
        if str(props.get('area_status')) != '1' or props.get('disaster_end_date'):
            continue
        identifier = props.get('area_id')
        if not isinstance(identifier, int) or identifier <= 0:
            continue
        point = _first_point(feature.get('geometry'))
        if not point or not (97 <= float(point[0]) <= 106 and 5 <= float(point[1]) <= 21):
            continue
        updated = _iso(props.get('last_upd_date'))
        started = _iso(props.get('disaster_start_date'))
        if not updated or not started:
            continue
        updated_at = dt.datetime.fromisoformat(updated.replace('Z', '+00:00'))
        started_at = dt.datetime.fromisoformat(started.replace('Z', '+00:00'))
        if not (dt.timedelta(minutes=-5) <= now - updated_at <= dt.timedelta(days=3) and
                dt.timedelta(minutes=-5) <= now - started_at <= dt.timedelta(days=30)):
            continue
        kind = _clean(props.get('disaster_type_name'), 80)
        province = _clean(props.get('province_name'), 80)
        district = _clean(props.get('amphur_name'), 80)
        if not kind or not province:
            continue
        category = 'fire' if 'อัคคีภัย' in kind or 'ไฟป่า' in kind else 'warning'
        location = ', '.join(part for part in (district, province) if part)
        output.append(_item(f'th:ddpm:{identifier}', *point, f'{kind} · {province}',
                            f'Open disaster report near {location}. Reported location; verify with DDPM.',
                            'Thailand DDPM', THAILAND_DDPM_SOURCE, updated, category))
    return output


def _thailand_ddpm():
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%S')
    query = urllib.parse.urlencode({
        'service': 'WFS', 'version': '1.1.0', 'request': 'GetFeature',
        'typeName': 'ddpm:gis_disaster_poi', 'outputFormat': 'application/json',
        'srsName': 'EPSG:4326', 'maxFeatures': 1000,
        'CQL_FILTER': f"area_status = 1 AND last_upd_date >= '{cutoff}'",
    })
    # DDPM currently sends the leaf certificate without this public intermediate.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(Path(__file__).with_name('certs') /
                                             'globalsign-rsa-ov-ssl-ca-2018.pem'))
    return parse_thailand_ddpm(_json(f'{THAILAND_DDPM_WFS}?{query}', context=context))


class _IndonesiaDisasterRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_table = self.in_body = self.in_row = self.in_cell = False
        self.cells = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'table' and 'datatab' in attributes.get('class', '').split():
            self.in_table = True
        elif self.in_table and tag == 'tbody':
            self.in_body = True
        elif self.in_body and tag == 'tr':
            self.in_row = True
            self.cells = []
        elif self.in_row and tag == 'td':
            self.in_cell = True
            self.cells.append('')

    def handle_data(self, data):
        if self.in_cell and self.cells:
            self.cells[-1] += data

    def handle_endtag(self, tag):
        if tag == 'td':
            self.in_cell = False
        elif tag == 'tr' and self.in_row:
            if len(self.cells) >= 16:
                self.rows.append(self.cells)
            self.in_row = False
        elif tag == 'tbody':
            self.in_body = False
        elif tag == 'table':
            self.in_table = self.in_body = self.in_row = self.in_cell = False


@lru_cache(maxsize=1)
def _indonesia_regencies():
    path = Path(__file__).with_name('indonesia-regency-points.json')
    return json.loads(path.read_text())['points']


def parse_indonesia_bnpb(page, now=None):
    """BNPB event dates mapped to the agency's approximate regency points."""
    now = now or dt.datetime.now(dt.timezone.utc)
    parser = _IndonesiaDisasterRows()
    parser.feed(page.decode('utf-8', errors='replace') if isinstance(page, bytes) else page)
    if not parser.rows or len(parser.rows) > 500:
        raise ValueError('Indonesia BNPB disaster table is unavailable')
    regencies = _indonesia_regencies()
    output = []
    seen = set()
    def name_key(value):
        return re.sub(r'[^A-Z0-9]', '', str(value).upper().replace('KAB.', '').replace('KOTA', ''))
    for cells in parser.rows:
        code = _clean(cells[2], 10)
        point = regencies.get(code)
        if not point:
            continue
        try:
            event_day = dt.date.fromisoformat(_clean(cells[3], 10))
            observed = dt.datetime.combine(event_day, dt.time.min, ZoneInfo('Asia/Jakarta'))
        except ValueError:
            continue
        if not dt.timedelta(hours=-18) <= now - observed.astimezone(dt.timezone.utc) <= dt.timedelta(days=7):
            continue
        kind = _clean(cells[4], 80)
        place = _clean(cells[5], 140)
        regency = _clean(cells[6], 100)
        province = _clean(cells[7], 100)
        if (not kind or not regency or not province or
                name_key(regency) != name_key(point[2]) or
                name_key(province) != name_key(point[3])):
            continue
        fingerprint = hashlib.sha256(f'{event_day}|{code}|{kind}|{place}'.encode()).hexdigest()[:12]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        category = 'fire' if 'KEBAKARAN' in kind.upper() else 'warning'
        output.append(_item(f'id:bnpb:{fingerprint}', point[0], point[1],
                            f'{kind.title()} · {regency}',
                            f'Reported {event_day.isoformat()} in {regency}, {province}. '
                            'Marker is a representative regency point, not the incident site.',
                            'Indonesia BNPB · disaster report', INDONESIA_BNPB_URL,
                            observed.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
                            category))
    return output[:250]


def parse_poland_rso(payload, now=None):
    """Public RSO advisories with supplied points; these are not dispatch calls."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = payload.get('newses') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('Poland RSO advisory feed is invalid')
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get('id')
        if not isinstance(identifier, int) or identifier <= 0:
            continue
        lon, lat = row.get('longitude'), row.get('latitude')
        if not _valid(lon, lat) or not (14 <= float(lon) <= 24.3 and 48.8 <= float(lat) <= 55.2):
            continue
        start = _iso(row.get('valid_from'), 'Europe/Warsaw')
        end = _iso(row.get('valid_to'), 'Europe/Warsaw')
        updated = _iso(row.get('updated_at'), 'Europe/Warsaw')
        if not start or not end or not updated:
            continue
        start_time = dt.datetime.fromisoformat(start.replace('Z', '+00:00'))
        end_time = dt.datetime.fromisoformat(end.replace('Z', '+00:00'))
        updated_time = dt.datetime.fromisoformat(updated.replace('Z', '+00:00'))
        if not (start_time <= now <= end_time and
                dt.timedelta(minutes=-5) <= now - updated_time <= dt.timedelta(days=30)):
            continue
        title = _clean(row.get('title'), 140)
        detail = _clean(row.get('shortcut') or row.get('content'), 280)
        if not title or not detail:
            continue
        output.append(_item(f'pl:rso:{identifier}', lon, lat,
                            f'Advisory · {title}', detail,
                            'Poland RSO · public advisory', POLAND_RSO_SOURCE,
                            updated, 'warning'))
    return output


def _first_point(geometry):
    if not isinstance(geometry, dict):
        return None
    if geometry.get('type') == 'Point':
        pair = geometry.get('coordinates') or []
        return pair[:2] if len(pair) >= 2 and _valid(*pair[:2]) else None
    for child in geometry.get('geometries') or []:
        point = _first_point(child)
        if point:
            return point
    return None


def parse_nsw(payload):
    output = []
    for feature in payload.get('features') or []:
        props = feature.get('properties') or {}
        if str(props.get('category', '')).lower() == 'planned burn':
            continue
        point = _first_point(feature.get('geometry'))
        if not point:
            continue
        guid = str(props.get('guid') or '')
        incident_id = guid.rstrip('/').split('/')[-1]
        if not incident_id.isdigit():
            continue
        try:
            local = dt.datetime.strptime(props.get('pubDate', ''), '%d/%m/%Y %I:%M:%S %p')
            observed = _iso(local.isoformat(), 'Australia/Sydney')
        except ValueError:
            observed = ''
        detail = props.get('description') or props.get('category')
        incident_type = re.search(r'TYPE:\s*([^<]+)', str(detail), re.I)
        incident_type = incident_type.group(1).strip().lower() if incident_type else ''
        if re.search(r'FIRE:\s*Yes', str(detail), re.I) or 'fire' in incident_type:
            category = 'fire'
        elif any(word in incident_type for word in ('mva', 'transport', 'crash', 'vehicle')):
            category = 'traffic'
        elif any(word in incident_type for word in ('medical', 'rescue')):
            category = 'medical'
        else:
            category = 'warning'
        output.append(_item(f'nsw:{incident_id}', *point, props.get('title') or 'NSW RFS incident',
                            detail, 'NSW Rural Fire Service', 'https://www.rfs.nsw.gov.au/fire-information/fires-near-me',
                            observed, category))
    return output[:250]


def parse_victoria(payload, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    output = []
    for row in payload.get('results') or []:
        if row.get('feedType') != 'incident' or not row.get('incidentNo') or not _valid(row.get('longitude'), row.get('latitude')):
            continue
        milliseconds = row.get('lastUpdatedDt')
        try:
            observed_at = dt.datetime.fromtimestamp(float(milliseconds) / 1000, dt.timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
        if observed_at < now - dt.timedelta(hours=6) or observed_at > now + dt.timedelta(minutes=30):
            continue
        category = str(row.get('category1') or '').lower()
        incident_type = str(row.get('incidentType') or '').lower()
        output.append(_item(f'vic:{row["incidentNo"]}', row['longitude'], row['latitude'],
                            row.get('incidentType') or row.get('category1') or 'Victorian incident',
                            ' · '.join(filter(None, [row.get('name'), row.get('incidentLocation'), row.get('incidentStatus'), row.get('agency')])),
                            'Emergency Management Victoria', 'https://www.emergency.vic.gov.au/respond/',
                            observed_at.isoformat().replace('+00:00', 'Z'),
                            'fire' if 'fire' in category and 'false alarm' not in incident_type else 'warning'))
    return output[:300]


def _cap_centroid(info):
    for area in info.findall(f'{_CAP}area'):
        polygon = area.findtext(f'{_CAP}polygon') or ''
        points = []
        for pair in polygon.split():
            try:
                lat, lon = map(float, pair.split(','))
            except ValueError:
                continue
            if _valid(lon, lat):
                points.append((lon, lat))
        if points:
            if len(points) > 1 and points[0] == points[-1]:
                points.pop()
            return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)
        circle = area.findtext(f'{_CAP}circle') or ''
        if circle:
            try:
                lat, lon = map(float, circle.split()[0].split(','))
                if _valid(lon, lat):
                    return lon, lat
            except ValueError:
                pass
    return None


def parse_nz_cap(root, source_url=NZ_URL, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if root.findtext(f'{_CAP}status') != 'Actual' or root.findtext(f'{_CAP}scope') != 'Public':
        return []
    if root.findtext(f'{_CAP}msgType') in {'Cancel', 'Error', 'Test'}:
        return []
    identifier = root.findtext(f'{_CAP}identifier') or ''
    observed = _iso(root.findtext(f'{_CAP}sent'))
    if not observed:
        return []
    sent_at = dt.datetime.fromisoformat(observed.replace('Z', '+00:00'))
    if sent_at > now + dt.timedelta(minutes=30):
        return []
    output = []
    for index, info in enumerate(root.findall(f'{_CAP}info')):
        expiry = _iso(info.findtext(f'{_CAP}expires'))
        if expiry and dt.datetime.fromisoformat(expiry.replace('Z', '+00:00')) <= now:
            continue
        if not expiry and sent_at < now - dt.timedelta(days=2):
            continue
        point = _cap_centroid(info)
        if not point:
            continue
        output.append(_item(f'nz:{identifier}:{index}', *point,
                            info.findtext(f'{_CAP}headline') or info.findtext(f'{_CAP}event') or 'NZ emergency alert',
                            info.findtext(f'{_CAP}description') or info.findtext(f'{_CAP}instruction'),
                            info.findtext(f'{_CAP}senderName') or 'New Zealand Emergency Mobile Alert',
                            source_url, observed, 'warning'))
    return output


def fetch_nz():
    feed = _xml(NZ_URL)
    urls = []
    for entry in feed.findall(f'{_ATOM}entry')[:60]:
        for link in entry.findall(f'{_ATOM}link'):
            url = link.get('href') or ''
            if link.get('type') == 'application/cap+xml' and url.startswith('https://alerthub.civildefence.govt.nz/'):
                urls.append(url)
                break
    output = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for url, result in zip(urls, executor.map(_xml, urls)):
            output.extend(parse_nz_cap(result, url))
    return output


def parse_queensland(root, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    output = []
    for alert in root.findall(f'.//{_CAP}alert'):
        if alert.findtext(f'{_CAP}status') != 'Actual' or alert.findtext(f'{_CAP}scope') != 'Public':
            continue
        if alert.findtext(f'{_CAP}msgType') in {'Cancel', 'Error', 'Test'}:
            continue
        identifier = alert.findtext(f'{_CAP}identifier') or ''
        if not identifier:
            continue
        observed = _iso(alert.findtext(f'{_CAP}sent'))
        for index, info in enumerate(alert.findall(f'{_CAP}info')):
            expiry = _iso(info.findtext(f'{_CAP}expires'))
            if not expiry or dt.datetime.fromisoformat(expiry.replace('Z', '+00:00')) <= now:
                continue
            point = _cap_centroid(info)
            if not point:
                continue
            output.append(_item(f'qld:{identifier}:{index}', *point,
                                info.findtext(f'{_CAP}headline') or 'Queensland fire warning',
                                info.findtext(f'{_CAP}description'),
                                'Queensland Fire Department', 'https://www.fire.qld.gov.au/Current-Incidents',
                                observed, 'fire'))
    return output[:250]


@lru_cache(maxsize=4096)
def _england_area(area_id):
    base = f'https://environment.data.gov.uk/flood-monitoring/id/floodAreas/{area_id}'
    try:
        return _json(base + '.json').get('items') or {}
    except (OSError, ValueError):
        # The format-neutral route sometimes returns 503 for individual areas
        # while the documented full-view JSON representation remains available.
        return _json(base + '?_view=full').get('items') or {}


def parse_england(payload):
    rows = [row for row in (payload.get('items') or [])
            if row.get('severityLevel') in (1, 2, 3)
            and re.fullmatch(r'[A-Za-z0-9]+', str(row.get('floodAreaID') or ''))]
    def build(row):
        area_id = row['floodAreaID']
        area = _england_area(area_id)
        if not _valid(area.get('long'), area.get('lat')):
            return None
        severity = row.get('severity') or 'Flood alert'
        return _item(f'england-flood:{area_id}', area['long'], area['lat'],
                     f'{severity}: {row.get("description") or area.get("label") or area_id}',
                     row.get('message') or row.get('description'),
                     'Environment Agency · England', 'https://check-for-flooding.service.gov.uk/',
                     _iso(row.get('timeMessageChanged') or row.get('timeRaised'), 'Europe/London'), 'warning')
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        return [item for item in executor.map(build, rows[:250]) if item]


class _BurgenlandOperations(HTMLParser):
    """Read only the dispatch page's ongoing operations tab."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.district = ''
        self.operation = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'wbr'}:
            return
        attrs = dict(attrs)
        classes = set(attrs.get('class', '').split())
        pane = attrs.get('id') == 'current-pane' or bool(self.stack and self.stack[-1]['pane'])
        frame = {'tag': tag, 'classes': classes, 'pane': pane, 'text': [], 'icons': set()}
        self.stack.append(frame)
        if pane and 'district-operations' in classes:
            self.district = ''
        if pane and 'operation' in classes:
            self.operation = {'district': self.district}

    def handle_data(self, data):
        if self.stack:
            self.stack[-1]['text'].append(data)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        frame = self.stack.pop()
        if frame['tag'] != tag:
            return
        value = ' '.join(''.join(frame['text']).split())
        classes = frame['classes']
        if frame['pane']:
            if 'fw-bold' in classes and 'col' in classes and self.operation is None:
                self.district = value
            if self.operation is not None:
                if 'avatar' in classes:
                    self.operation['code'] = value
                if 'small' in classes and 'fa-location-dot' in frame['icons']:
                    self.operation['place'] = value
                if 'small' in classes and 'fa-alarm-clock' in frame['icons']:
                    self.operation['time'] = value
                if 'operation' in classes:
                    self.rows.append(self.operation)
                    self.operation = None
        if self.stack:
            self.stack[-1]['text'].append(value)
            self.stack[-1]['icons'].update(frame['icons'])
            if tag == 'i':
                self.stack[-1]['icons'].update(classes)


@lru_cache(maxsize=1)
def _burgenland_municipalities():
    path = Path(__file__).with_name('burgenland-municipalities.json')
    return json.loads(path.read_text(encoding='utf-8'))


def parse_burgenland(page, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    page = page.decode('utf-8', 'replace') if isinstance(page, bytes) else str(page)
    stamp = re.search(r'Zuletzt aktualisiert am\s*(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2})', page)
    if not stamp:
        raise ValueError('Burgenland dispatch update time missing')
    published = dt.datetime.strptime(' '.join(stamp.groups()), '%d.%m.%Y %H:%M').replace(tzinfo=ZoneInfo('Europe/Vienna'))
    published_utc = published.astimezone(dt.timezone.utc)
    if published_utc < now - dt.timedelta(minutes=20) or published_utc > now + dt.timedelta(minutes=5):
        raise ValueError('Burgenland dispatch page is stale')
    parser = _BurgenlandOperations()
    parser.feed(page)
    if 'id="current-pane"' not in page:
        raise ValueError('Burgenland ongoing dispatch tab missing')
    towns = _burgenland_municipalities()
    output = []
    for row in parser.rows[:100]:
        place, code, clock = (row.get(key, '').strip() for key in ('place', 'code', 'time'))
        point = towns.get(place.casefold())
        if not point or not re.fullmatch(r'[A-Z]{1,4}\d{0,2}', code) or not re.fullmatch(r'\d{2}:\d{2}', clock):
            continue
        try:
            observed_local = dt.datetime.combine(published.date(), dt.time.fromisoformat(clock), published.tzinfo)
        except ValueError:
            continue
        if observed_local > published + dt.timedelta(minutes=5):
            observed_local -= dt.timedelta(days=1)
        if observed_local < published - dt.timedelta(days=1):
            continue
        observed = observed_local.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        district = row.get('district', '')
        source = 'LSZ Burgenland'
        output.append(_item(f'burgenland:{district}:{place}:{code}:{observed}', *point,
                            f'{code} · {place}', f'Ongoing fire brigade dispatch · {district} · municipality center, approximate location',
                            source, BURGENLAND_URL, observed, 'fire' if code.startswith('B') else 'warning'))
    return output


def parse_upper_austria(payload, now=None):
    """Map current, public fire-brigade dispatches at the publisher's approximate points."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(payload, dict) or payload.get('webext2') is not True or payload.get('title') != 'laufend':
        raise ValueError('Upper Austria ongoing dispatch feed is invalid')
    try:
        published = email.utils.parsedate_to_datetime(payload['pubDate']).astimezone(dt.timezone.utc)
    except (KeyError, TypeError, ValueError):
        raise ValueError('Upper Austria dispatch update time missing') from None
    if not -dt.timedelta(minutes=5) <= now - published <= dt.timedelta(minutes=20):
        raise ValueError('Upper Austria dispatch feed is stale')
    incidents = payload.get('einsaetze')
    if not isinstance(incidents, dict):
        raise ValueError('Upper Austria dispatch records are invalid')
    output = []
    for wrapper in list(incidents.values())[:250]:
        row = wrapper.get('einsatz') if isinstance(wrapper, dict) else None
        if not isinstance(row, dict) or row.get('status') != 'offen':
            continue
        kind = row.get('einsatzart')
        # SELBST covers self-initiated operations and exercises; it is not a
        # reliable public emergency call.
        if kind not in ('BRAND', 'PERSON', 'TEE'):
            continue
        incident_id = str(row.get('num1') or '')
        if not re.fullmatch(r'E\d{9}', incident_id):
            continue
        point = row.get('wgs84') or {}
        if not isinstance(point, dict):
            continue
        lon, lat = point.get('lng'), point.get('lat')
        if not _valid(lon, lat) or not (12.6 <= float(lon) <= 15.1 and 47.4 <= float(lat) <= 48.9):
            continue
        try:
            started = email.utils.parsedate_to_datetime(row['startzeit']).astimezone(dt.timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if not dt.timedelta(minutes=-5) <= now - started <= dt.timedelta(days=2):
            continue
        district_data = row.get('bezirk') or {}
        address_data = row.get('adresse') or {}
        dispatch_data = row.get('einsatztyp') or {}
        if not all(isinstance(value, dict) for value in (district_data, address_data, dispatch_data)):
            continue
        district = _clean(district_data.get('text'), 60)
        municipality = _clean(address_data.get('emun'), 80)
        dispatch = _clean(dispatch_data.get('text'), 90)
        if not municipality or not dispatch:
            continue
        detail = f'Ongoing fire brigade dispatch · {municipality}'
        if district:
            detail += f' · {district}'
        detail += ' · approximate public location'
        observed = started.isoformat().replace('+00:00', 'Z')
        output.append(_item(f'upper-austria:{incident_id}', lon, lat,
                            dispatch, detail, 'OÖ Landes-Feuerwehrverband',
                            UPPER_AUSTRIA_SOURCE, observed,
                            'fire' if kind == 'BRAND' else 'warning'))
    return output


def parse_iceland(payload, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(payload, list):
        raise ValueError('Iceland CAP response is not a list')
    output = []
    for row in payload[:150]:
        if not isinstance(row, dict) or row.get('msgtype') not in {'Alert', 'Update'}:
            continue
        identifier = str(row.get('identifier') or '')
        expiry = _iso(row.get('expires'))
        sent = _iso(row.get('sent'))
        if not identifier or not expiry or not sent:
            continue
        if dt.datetime.fromisoformat(expiry.replace('Z', '+00:00')) <= now:
            continue
        if dt.datetime.fromisoformat(sent.replace('Z', '+00:00')) > now + dt.timedelta(minutes=10):
            continue
        polygons = row.get('polygon') or []
        if isinstance(polygons, str):
            polygons = [polygons]
        points = []
        for polygon in polygons[:10]:
            for pair in str(polygon).split()[:1000]:
                try:
                    lat, lon = map(float, pair.split(','))
                except ValueError:
                    continue
                if _valid(lon, lat):
                    points.append((lon, lat))
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if not points:
            continue
        lon = sum(point[0] for point in points) / len(points)
        lat = sum(point[1] for point in points) / len(points)
        event = row.get('event_en') or 'Hazard alert'
        headline = row.get('headline_en') or event
        detail = row.get('description_en') or event
        output.append(_item(f'iceland:{identifier}:{row.get("area_id", "")}', lon, lat,
                            headline, detail,
                            f'Icelandic Meteorological Office · downloaded {now.date().isoformat()} UTC',
                            'https://en.vedur.is/', sent, 'warning'))
    return output


def parse_portugal(payload, now=None):
    """Map ANEPC's public active-incident layer without street-level details."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(payload, dict) or not isinstance(payload.get('features'), list):
        raise ValueError('ANEPC incident response is invalid')
    if payload.get('exceededTransferLimit'):
        raise ValueError('ANEPC incident response was truncated')
    rows = payload['features']
    if not rows:
        return []
    try:
        published = max(float(row['attributes']['DataDosDados']) for row in rows) / 1000
    except (KeyError, TypeError, ValueError):
        raise ValueError('ANEPC publication time is missing')
    observed = dt.datetime.fromtimestamp(published, dt.timezone.utc)
    # ANEPC currently labels Portugal's local summer time as UTC.
    if dt.timedelta(minutes=30) < observed - now < dt.timedelta(minutes=90):
        observed -= dt.timedelta(hours=1)
    if observed < now - dt.timedelta(minutes=30) or observed > now + dt.timedelta(minutes=5):
        raise ValueError('ANEPC incident publication is stale')
    observed_text = observed.isoformat().replace('+00:00', 'Z')
    output = []
    for row in rows[:2000]:
        if not isinstance(row, dict):
            continue
        props = row.get('attributes') or {}
        geometry = row.get('geometry') or {}
        lon, lat = geometry.get('x'), geometry.get('y')
        incident_id = props.get('Numero') or props.get('ID_oc')
        if not incident_id or not _valid(lon, lat):
            continue
        nature = re.sub(r'^\d+\s*-\s*', '', str(props.get('Natureza') or '')).strip()
        municipality = props.get('Concelho') or props.get('Regiao') or ''
        state = props.get('EstadoAgrupado') or ''
        if state not in {'Em Curso', 'Em Despacho', 'Em Resolução', 'Em Conclusão'}:
            continue
        title = f'{nature or "Civil protection incident"} · {municipality}' if municipality else nature or 'Civil protection incident'
        responders = props.get('Operacionais') or 0
        ground_units = props.get('MeiosTerrestres') or 0
        detail = ' · '.join(filter(None, [state, str(props.get('Numero') or ''),
            f'{responders} responder{"s" if responders != 1 else ""}' if responders else '',
            f'{ground_units} ground unit{"s" if ground_units != 1 else ""}' if ground_units else '']))
        code = str(props.get('Natureza') or '')[:2]
        category = 'fire' if code == '31' else 'traffic' if code == '24' else 'warning'
        output.append(_item(f'portugal:{incident_id}', lon, lat, title, detail,
                            'Portugal ANEPC · CC BY 4.0', PORTUGAL_SOURCE, observed_text, category))
    return output


def parse_catalonia_fires(payload, now=None):
    """Show recent, non-extinguished vegetation fires from Catalonia's live map."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(payload, dict) or not isinstance(payload.get('features'), list):
        raise ValueError('Catalonia fire response is invalid')
    if (payload.get('exceededTransferLimit') or
            (payload.get('properties') or {}).get('exceededTransferLimit')):
        raise ValueError('Catalonia fire response was truncated')
    output = []
    for row in payload['features'][:1000]:
        if not isinstance(row, dict):
            continue
        props = row.get('properties') or {}
        point = (row.get('geometry') or {}).get('coordinates') or []
        try:
            lon, lat = float(point[0]), float(point[1])
            updated = dt.datetime.fromtimestamp(float(props['DATA_ACT']) / 1000, dt.timezone.utc)
        except (IndexError, KeyError, TypeError, ValueError, OverflowError):
            continue
        if not (0 <= lon <= 3.5 and 40.3 <= lat <= 42.9):
            continue
        if not now - dt.timedelta(hours=24) <= updated <= now + dt.timedelta(minutes=5):
            continue
        phase = str(props.get('COM_FASE') or '').strip()
        if phase not in {'Actiu', 'Estabilitzat', 'Controlat'}:
            continue
        if props.get('ACT_DAT_FI') or props.get('ACT_URGENT') != 'S':
            continue
        incident_id = str(props.get('GlobalID') or props.get('ESRI_OID') or '')
        if not re.fullmatch(r'[0-9a-fA-F-]{8,36}|\d{1,12}', incident_id):
            continue
        municipality = _clean(props.get('MUNICIPI_DPX') or '', 80)
        if not municipality:
            continue
        title = f'Vegetation fire · {municipality}'
        fire_type = _clean(props.get('TAL_DESC_ALARMA2') or '', 100)
        detail = ' · '.join(filter(None, [phase, fire_type]))
        output.append(_item(f'es:catalonia:fire:{incident_id}', lon, lat, title, detail,
                            'Bombers de la Generalitat de Catalunya', CATALONIA_FIRE_SOURCE,
                            updated.isoformat().replace('+00:00', 'Z'), 'fire'))
    return output


def _zaragoza_street_key(value):
    plain = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().upper()
    return ' '.join(re.sub(r'[^A-Z0-9]+', ' ', plain).split())


def _zaragoza_streets():
    with _ZARAGOZA_STREETS_LOCK:
        if _ZARAGOZA_STREETS_CACHE['lookup'] is not None and time.time() < _ZARAGOZA_STREETS_CACHE['until']:
            return _ZARAGOZA_STREETS_CACHE['lookup']
        data = _json(ZARAGOZA_STREETS_URL)
        features = data.get('features') if isinstance(data, dict) else None
        if not isinstance(features, list) or len(features) < 2500 or len(features) > 5000 or data.get('numberMatched') != len(features):
            raise ValueError('Zaragoza street catalog is incomplete')
        lookup = {}
        for feature in features:
            props = feature.get('properties') or {}
            key = _zaragoza_street_key(props.get('nombre'))
            if not key or props.get('tipo_via') in {'PG', 'EB'}:
                continue
            geometry = feature.get('geometry') or {}
            lines = geometry.get('coordinates') if geometry.get('type') == 'MultiLineString' else None
            if not isinstance(lines, list):
                continue
            points = [point for line in lines if isinstance(line, list) for point in line
                      if isinstance(point, list) and len(point) >= 2 and _valid(point[0], point[1])]
            if not points:
                continue
            west, east = min(point[0] for point in points), max(point[0] for point in points)
            south, north = min(point[1] for point in points), max(point[1] for point in points)
            # Long roads cannot be represented honestly by an approximate street point.
            if ((east - west) * 83) ** 2 + ((north - south) * 111) ** 2 > 3 ** 2:
                continue
            middle = ((west + east) / 2, (south + north) / 2)
            point = min(points, key=lambda p: ((p[0] - middle[0]) * 83) ** 2 +
                        ((p[1] - middle[1]) * 111) ** 2)
            lookup.setdefault(key, []).append((point[0], point[1],
                                               _clean(props.get('nombre_publico'), 100)))
        _ZARAGOZA_STREETS_CACHE.update(lookup=lookup, until=time.time() + 86400)
        return lookup


def parse_zaragoza_fire(open_payload, closed_payload, streets, now=None):
    """Same-day fire-service reports at approximate, unambiguous street centers."""
    now = now or dt.datetime.now(dt.timezone.utc)
    output = []
    for status, payload, max_age in (('Ongoing', open_payload, 24), ('Closed today', closed_payload, 12)):
        rows = payload.get('result') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 500 or payload.get('totalCount') != len(rows):
            raise ValueError('Zaragoza fire report is incomplete')
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = _clean(row.get('tipoSiniestro'), 100)
            address = _clean(row.get('direccion'), 120)
            if not kind or not address or _zaragoza_street_key(kind).startswith(('PRACTICAS', 'INSPECCION', 'EVALUACION')):
                continue
            observed = _iso(row.get('fecha'), 'Europe/Madrid')
            if not observed:
                continue
            stamp = dt.datetime.fromisoformat(observed.replace('Z', '+00:00'))
            if not dt.timedelta(minutes=-5) <= now - stamp <= dt.timedelta(hours=max_age):
                continue
            # The dispatch feed gives a street name, never an incident coordinate.
            street_name = re.sub(r'\s*\([^)]*\).*$', '', address).strip()
            candidates = streets.get(_zaragoza_street_key(street_name), [])
            if len(candidates) != 1:
                continue
            lon, lat, public_name = candidates[0]
            incident_key = hashlib.sha256(f'{observed}|{kind}|{address}'.encode()).hexdigest()[:20]
            category = 'fire' if 'INCENDIO' in _zaragoza_street_key(kind) else (
                'traffic' if 'TRAFICO' in _zaragoza_street_key(kind) else 'warning')
            output.append(_item(f'es:zaragoza:fire:{incident_key}', lon, lat,
                                f'{kind} · Zaragoza',
                                f'{status} · {public_name} · Approximate street center; incident location not published',
                                'Ayuntamiento de Zaragoza · Fire Service', ZARAGOZA_FIRE_SOURCE,
                                observed, category))
    return output


def _zaragoza_fire():
    open_payload = _json(ZARAGOZA_FIRE_URL.format(kind=10))
    closed_payload = _json(ZARAGOZA_FIRE_URL.format(kind=20))
    if not open_payload.get('result') and not closed_payload.get('result'):
        return []
    return parse_zaragoza_fire(open_payload, closed_payload, _zaragoza_streets())


@lru_cache(maxsize=1)
def _sweden_areas():
    path = Path(__file__).with_name('sweden-administrative-points.json')
    return json.loads(path.read_text(encoding='utf-8'))


def parse_sweden_vma(payload, now=None):
    """Map current public VMA notices to approximate administrative points."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(payload, dict) or not isinstance(payload.get('alerts'), list):
        raise ValueError('Swedish VMA response is invalid')
    areas_by_code = _sweden_areas()
    output = []
    seen = set()
    for alert in payload['alerts']:
        if not isinstance(alert, dict) or alert.get('status') != 'Actual' or alert.get('scope') != 'Public':
            continue
        if alert.get('msgType') not in {'Alert', 'Update'}:
            continue
        identifier = str(alert.get('identifier') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier):
            continue
        try:
            sent = dt.datetime.fromisoformat(str(alert['sent']).replace('Z', '+00:00'))
            if sent.tzinfo is None or sent > now + dt.timedelta(minutes=5):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        infos = [info for info in alert.get('info') or [] if isinstance(info, dict)]
        infos.sort(key=lambda info: not str(info.get('language') or '').lower().startswith('en'))
        before_count = len(output)
        for info in infos:
            try:
                expires = dt.datetime.fromisoformat(str(info['expires']).replace('Z', '+00:00'))
                if expires.tzinfo is None or expires <= now:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            areas = info.get('area') or []
            if not isinstance(areas, list):
                continue
            article = str(info.get('web') or '')
            source_url = (article if re.fullmatch(r'https://(?:www\.)?sverigesradio\.se/[^\s]+', article)
                          else f'https://vmaapi.sr.se/api/v3/alert/{identifier}')
            event = _clean(info.get('headline') or info.get('event') or 'Public warning', 100)
            message = _clean(info.get('description') or info.get('instruction'), 180)
            locations = []
            for area in areas:
                if not isinstance(area, dict):
                    continue
                for geocode in area.get('geocode') or []:
                    if not isinstance(geocode, dict):
                        continue
                    code = str(geocode.get('value') or '')
                    kind = geocode.get('valueName')
                    if kind == 'Kommun':
                        point = areas_by_code['points'].get(code)
                        rank, suffix = 0, 'municipality'
                    elif kind == 'Län':
                        point = areas_by_code['counties'].get(code)
                        rank, suffix = 1, 'county'
                    elif kind == 'Sverige' and code == '00':
                        point = areas_by_code['country']
                        rank, suffix = 2, 'country'
                    else:
                        continue
                    if point:
                        locations.append((rank, code, point, suffix))
            if not locations:
                continue
            finest = min(location[0] for location in locations)
            for rank, code, point, suffix in locations:
                if rank != finest:
                    continue
                key = f'se:vma:{identifier}:{code}'
                if key in seen:
                    continue
                seen.add(key)
                lon, lat, name = point
                detail = f'{name} {suffix} · approximate area marker. {message}'
                output.append(_item(key, lon, lat, f'{name} · {event}', detail,
                                    'Sveriges Radio VMA', source_url,
                                    sent.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')))
            # English text is preferred when available; do not duplicate translated areas.
            if len(output) > before_count:
                break
    return output


def parse_sweden_police(payload, now=None):
    """Map recent public police notices to their published area centroids."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(payload, list) or len(payload) > 500:
        raise ValueError('Swedish police events response is invalid')
    output = []
    seen = set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        event_id = row.get('id')
        if not isinstance(event_id, int) or event_id <= 0 or event_id in seen:
            continue
        try:
            # The API emits both one- and two-digit hours ("9:18" and "20:00").
            observed = dt.datetime.strptime(str(row['datetime']), '%Y-%m-%d %H:%M:%S %z')
            if observed.tzinfo is None or not now - dt.timedelta(hours=24) <= observed <= now + dt.timedelta(minutes=5):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        event_type = _clean(row.get('type'), 60)
        if not event_type or event_type.startswith('Sammanfattning') or event_type in {'Övrigt', 'Information', 'Trafikkontroll'}:
            continue
        location = row.get('location')
        if not isinstance(location, dict):
            continue
        match = re.fullmatch(r'\s*([0-9]{1,2}\.[0-9]+)\s*,\s*([0-9]{1,2}\.[0-9]+)\s*', str(location.get('gps') or ''))
        if not match:
            continue
        lat, lon = float(match[1]), float(match[2])
        if not (55 <= lat <= 70 and 10 <= lon <= 25):
            continue
        path = str(row.get('url') or '')
        if not re.fullmatch(r'/aktuellt/handelser/[a-z0-9/.-]{1,250}/', path):
            continue
        seen.add(event_id)
        category = ('fire' if 'brand' in event_type.casefold() else
                    'traffic' if 'trafikolycka' in event_type.casefold() else 'police')
        area = _clean(location.get('name'), 80)
        output.append(_item(f'se:police:{event_id}', lon, lat,
                            f'Police report · {event_type}',
                            f'{area} · approximate area center' if area else 'Approximate area center',
                            'Swedish Police · public events API', 'https://polisen.se' + path,
                            observed.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z'), category))
    return output


def _sweden_police():
    # The Police require at least ten seconds between requests and no more than 60/hour.
    # One process-wide ten-minute cache also prevents concurrent visitors from polling it.
    with _SWEDEN_POLICE_LOCK:
        now = time.time()
        if now < _SWEDEN_POLICE_CACHE['until']:
            if _SWEDEN_POLICE_CACHE['items'] is None:
                raise ValueError('Swedish police events are temporarily unavailable')
            return _SWEDEN_POLICE_CACHE['items']
        _SWEDEN_POLICE_CACHE['until'] = now + 600
        try:
            items = parse_sweden_police(_json(SWEDEN_POLICE_URL))
        except Exception:
            _SWEDEN_POLICE_CACHE['items'] = None
            raise
        _SWEDEN_POLICE_CACHE['items'] = items
        return items


@lru_cache(maxsize=1)
def _norway_municipality_centers():
    path = Path(__file__).with_name('norway-municipalities.json')
    return json.loads(path.read_text(encoding='utf-8'))['centers']


def parse_norway_police(payload, now=None):
    """Recent public police reports at approximate municipality points."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = payload.get('messageThreads') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError('Norwegian police feed is invalid')
    centers = _norway_municipality_centers()
    output, seen = [], set()
    categories = {'Brann', 'Ulykke', 'Redning', 'Savnet', 'Sjø', 'Vær',
                  'Voldshendelse', 'Innbrudd', 'Trafikk'}
    for row in rows:
        if not isinstance(row, dict) or row.get('category') not in categories:
            continue
        event_id = str(row.get('id') or '')
        if not re.fullmatch(r'[a-z0-9]{4,20}', event_id) or event_id in seen:
            continue
        municipality = _clean(row.get('municipality'), 80)
        point = centers.get(municipality.casefold())
        if not point or not (4 <= point[0] <= 32 and 57 <= point[1] <= 72):
            continue
        observed = _iso(row.get('lastMessageOn') or row.get('createdOn'))
        if not observed:
            continue
        age = now - dt.datetime.fromisoformat(observed.replace('Z', '+00:00'))
        if age < -dt.timedelta(minutes=5) or age > (dt.timedelta(hours=24) if row.get('isActive') else dt.timedelta(hours=6)):
            continue
        messages = row.get('messages') or []
        if not isinstance(messages, list):
            continue
        latest = next((_clean(message.get('text'), 220) for message in reversed(messages)
                       if isinstance(message, dict) and message.get('type') == 'Published'
                       and _clean(message.get('text'))), '')
        area = _clean(row.get('area'), 80)
        detail = ' · '.join(part for part in (
            f'{municipality}{" · " + area if area else ""}',
            'Ongoing' if row.get('isActive') else 'Recent report',
            'Approximate municipality point', latest) if part)
        category = ('fire' if row['category'] == 'Brann' else
                    'traffic' if row['category'] in {'Trafikk', 'Ulykke'} else 'police')
        seen.add(event_id)
        output.append(_item(f'no:police:{event_id}', *point,
                            f'Police report · {row["category"]}', detail,
                            'Norwegian Police · Politiloggen', 'https://www.politiet.no/politiloggen',
                            observed, category))
    return output


def parse_usti_emergencies(map_payload, rss_root, now=None):
    """Join the region's map coordinates with its status and update RSS feed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    result = map_payload.get('result') if isinstance(map_payload, dict) else None
    groups = map_payload.get('result_items') if isinstance(map_payload, dict) else None
    if not isinstance(result, dict) or not isinstance(groups, list) or result.get('batch_start') != 0:
        raise ValueError('Ústí fire brigade map response is invalid')
    try:
        count = int(result['total_items'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('Ústí fire brigade result count is invalid') from None
    if not 0 <= count <= 2000 or rss_root.tag != 'rss':
        raise ValueError('Ústí fire brigade publication is invalid')
    try:
        updated = email.utils.parsedate_to_datetime(rss_root.findtext('./channel/lastBuildDate'))
    except (TypeError, ValueError):
        raise ValueError('Ústí fire brigade RSS timestamp is missing') from None
    if updated.tzinfo is None or not -dt.timedelta(minutes=5) <= now - updated <= dt.timedelta(hours=24):
        raise ValueError('Ústí fire brigade RSS is stale')

    reports = {}
    for entry in rss_root.findall('./channel/item')[:100]:
        url = entry.findtext('link') or ''
        match = re.fullmatch(re.escape(USTI_EMERGENCY_URL) + r'(\d{1,9})/', url)
        if not match:
            continue
        try:
            published = email.utils.parsedate_to_datetime(entry.findtext('pubDate'))
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None or not -dt.timedelta(minutes=5) <= now - published <= dt.timedelta(hours=6):
            continue
        description = _clean(entry.findtext('description'), 300)
        status = re.search(r'\bstav:\s*([^ ]+)', description, re.IGNORECASE)
        state = status.group(1).casefold() if status else ''
        if state.startswith('ukon'):
            state_label = 'Completed report'
        elif state.startswith(('probíh', 'probih', 'trvaj')):
            state_label = 'Ongoing report'
        else:
            state_label = 'Recent report'
        reports[int(match.group(1))] = (published, _clean(entry.findtext('title'), 110), state_label)

    rows = groups[0].get('ret') if groups and isinstance(groups[0], dict) else []
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('Ústí fire brigade map items are invalid')
    output = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), int):
            continue
        incident_id = row['id']
        if incident_id not in reports:
            continue
        point = row.get('geom')
        if not isinstance(point, dict):
            continue
        try:
            lon, lat = _JTSK_TO_WGS84.transform(float(point['lon']), float(point['lat']))
        except (KeyError, TypeError, ValueError):
            continue
        if not 12.2 <= lon <= 14.9 or not 49.9 <= lat <= 51.2:
            continue
        published, rss_title, state_label = reports[incident_id]
        title = rss_title or _clean(row.get('name'), 110)
        if not title:
            continue
        category = ('fire' if title.casefold().startswith('požár') else
                    'traffic' if title.casefold().startswith('dopravní nehoda') else 'warning')
        output.append(_item(f'cz:usti:fire:{incident_id}', lon, lat, title,
                            f'{state_label} · Ústí nad Labem Region · public fire brigade record; reporting may be delayed',
                            'Ústí nad Labem Region · Crisis Management Portal', USTI_EMERGENCY_URL,
                            published.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z'), category))
    return output


def _usti_emergencies():
    return parse_usti_emergencies(_json(USTI_EMERGENCY_URL + '?fmt=json&nl=0'),
                                  _xml(USTI_EMERGENCY_URL + 'feed.xml'))


def parse_netherlands_p2000(payload, now=None):
    """Recent public pager alerts, with source links required by Zwaailicht.nl."""
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get('of') != 'alert':
        raise ValueError('Dutch pager publication is invalid')
    license_info = payload.get('license') or {}
    if license_info.get('holder') != 'Zwaailicht.nl':
        raise ValueError('Dutch pager attribution is missing')
    rows = payload.get('results')
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('Dutch pager publication has an invalid size')
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        alert_id = str(row.get('id') or '')
        if not re.fullmatch(r'[a-f0-9]{16}', alert_id):
            continue
        observed = _iso(row.get('observed_at'))
        try:
            age = now - dt.datetime.fromisoformat(observed.replace('Z', '+00:00')).timestamp()
        except ValueError:
            continue
        if not -300 <= age <= 30 * 60:
            continue
        location = row.get('location') or {}
        lon, lat = location.get('longitude'), location.get('latitude')
        if not _valid(lon, lat) or not (3.2 <= float(lon) <= 7.3 and 50.7 <= float(lat) <= 53.6):
            continue
        city = _clean(location.get('city'), 70)
        if not city:
            continue
        source_path = (row.get('_links') or {}).get('html')
        if not isinstance(source_path, str) or not re.fullmatch(r'/[a-z0-9/\-]+', source_path):
            continue
        service = (row.get('service') or {}).get('id')
        kind = (row.get('incident_type') or {}).get('id')
        category = ('traffic' if kind == 'traffic' else
                    'fire' if service == 'brandweer' else
                    'medical' if service in ('ambulance', 'lifeliner') else
                    'police' if service == 'politie' else 'warning')
        label = {'brandweer': 'Fire brigade', 'ambulance': 'Ambulance',
                 'lifeliner': 'Air ambulance', 'politie': 'Police',
                 'knrm': 'Lifeboat'}.get(service, 'Emergency service')
        priority = _clean(row.get('priority'), 12)
        detail = ' · '.join(part for part in (priority,
                           'Public pager alert; approximate location, incident unconfirmed') if part)
        items.append(_item(f'nl:p2000:{alert_id}', lon, lat,
                           f'{label} alert · {city}', detail,
                           'Zwaailicht.nl · P2000 alert', NETHERLANDS_P2000_SOURCE + source_path.lstrip('/'),
                           observed, category))
    return items


def _netherlands_p2000():
    with _P2000_HTTP_LOCK:
        headers = {'User-Agent': 'GlobeView/1.0 (+https://globeview.app/; public emergency map)',
                   'Accept': 'application/json'}
        if _P2000_HTTP_CACHE['etag']:
            headers['If-None-Match'] = _P2000_HTTP_CACHE['etag']
        request = urllib.request.Request(NETHERLANDS_P2000_URL, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read(1024 * 1024 + 1)
                etag = response.headers.get('ETag')
            if len(body) > 1024 * 1024:
                raise ValueError('Dutch pager publication exceeded size limit')
            payload = json.loads(body)
            rows = parse_netherlands_p2000(payload)
            _P2000_HTTP_CACHE.update(etag=etag if etag and len(etag) <= 200 else None,
                                     payload=payload)
            return rows
        except urllib.error.HTTPError as error:
            if error.code != 304 or _P2000_HTTP_CACHE['payload'] is None:
                raise
            return parse_netherlands_p2000(_P2000_HTTP_CACHE['payload'])


_LU_CAP = '{urn:oasis:names:tc:emergency:cap:1.2:profile:cap-lu:1.0}'


def parse_luxembourg_alerts(roots, now=None):
    """Show current public safety alerts, applying cancellations and updates first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    latest = {}
    for root in roots:
        if root.tag != _LU_CAP + 'alert':
            continue
        identifier = root.findtext(_LU_CAP + 'identifier') or ''
        match = re.fullmatch(r'LU-Alert\.\d+\.(\d{1,9})\.\d+', identifier)
        if not match or root.findtext(_LU_CAP + 'status') != 'Actual' or root.findtext(_LU_CAP + 'scope') != 'Public':
            continue
        sent_text = _iso(root.findtext(_LU_CAP + 'sent'))
        if not sent_text:
            continue
        sent = dt.datetime.fromisoformat(sent_text.replace('Z', '+00:00'))
        if sent > now + dt.timedelta(minutes=5) or sent < now - dt.timedelta(days=7):
            continue
        event_id = match.group(1)
        if event_id not in latest or sent > latest[event_id][0]:
            latest[event_id] = (sent, root, identifier)

    output = []
    categories = {'Fire': 'fire', 'Rescue': 'rescue', 'Safety': 'warning',
                  'Transport': 'traffic', 'Met': 'weather', 'Env': 'warning'}
    for event_id, (sent, root, identifier) in latest.items():
        if root.findtext(_LU_CAP + 'msgType') not in {'Alert', 'Update'}:
            continue
        for info in root.findall(_LU_CAP + 'info'):
            language = info.findtext(_LU_CAP + 'language') or ''
            if language and not language.startswith('fr'):
                continue
            category = categories.get(info.findtext(_LU_CAP + 'category'))
            if not category:
                continue  # Food/product recalls belong outside emergency reports.
            expires_text = _iso(info.findtext(_LU_CAP + 'expires'))
            if not expires_text or dt.datetime.fromisoformat(expires_text.replace('Z', '+00:00')) <= now:
                continue
            for index, area in enumerate(info.findall(_LU_CAP + 'area')):
                polygon = (area.findtext(_LU_CAP + 'polygon') or '').split()
                points = []
                for token in polygon[:2000]:
                    pair = token.split(',')
                    if len(pair) != 2 or not _valid(pair[1], pair[0]):
                        continue
                    lat, lon = float(pair[0]), float(pair[1])
                    if 49.4 <= lat <= 50.3 and 5.6 <= lon <= 6.6:
                        points.append((lon, lat))
                if len(points) < 3:
                    continue
                lon = (min(point[0] for point in points) + max(point[0] for point in points)) / 2
                lat = (min(point[1] for point in points) + max(point[1] for point in points)) / 2
                area_name = _clean(area.findtext(_LU_CAP + 'areaDesc'), 90)
                detail = f'{area_name} · Representative area point · {_clean(info.findtext(_LU_CAP + "description"), 180)}'
                output.append(_item(f'lu:alert:{event_id}:{index}', lon, lat,
                                    info.findtext(_LU_CAP + 'headline') or info.findtext(_LU_CAP + 'event') or 'Public alert',
                                    detail, 'Luxembourg LU-ALERT · CC BY', LUXEMBOURG_ALERT_SOURCE,
                                    sent.isoformat().replace('+00:00', 'Z'), category))
            break
    return output


@lru_cache(maxsize=128)
def _luxembourg_cap_resource(url):
    return _xml(url)


def _luxembourg_alerts():
    catalog = _json(LUXEMBOURG_ALERT_CATALOG)
    if not isinstance(catalog, dict) or catalog.get('license') != 'cc-by' or not isinstance(catalog.get('resources'), list):
        raise ValueError('Luxembourg alert catalog is invalid')
    now = dt.datetime.now(dt.timezone.utc)
    urls = []
    for resource in catalog['resources']:
        if not isinstance(resource, dict):
            continue
        updated_text = _iso(resource.get('last_modified'))
        if not updated_text:
            continue
        updated = dt.datetime.fromisoformat(updated_text.replace('Z', '+00:00'))
        if updated < now - dt.timedelta(days=7):
            continue
        url = resource.get('url') or ''
        if re.fullmatch(r'https://download\.data\.public\.lu/resources/alertes-du-systeme-lu-alert/[0-9-]+/dump-alert\.[0-9]+\.xml', url):
            urls.append(url)
        if len(urls) >= 60:
            break
    roots = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_luxembourg_cap_resource, url) for url in urls]
        for future in concurrent.futures.as_completed(futures):
            try:
                roots.append(future.result())
            except Exception:
                continue
    if urls and not roots:
        raise ValueError('Luxembourg alert resources are unavailable')
    return parse_luxembourg_alerts(roots, now)


_LOADERS = {
    'nsw_rfs': lambda: parse_nsw(_json(NSW_URL)),
    'victoria': lambda: parse_victoria(_json(VIC_URL)),
    'queensland': lambda: parse_queensland(_xml(QLD_URL)),
    'nz_alerts': fetch_nz,
    'england_floods': lambda: parse_england(_json(ENGLAND_URL)),
    'burgenland_fire': lambda: parse_burgenland(_html(BURGENLAND_URL)),
    'upper_austria_fire': lambda: parse_upper_austria(_json(UPPER_AUSTRIA_URL)),
    'iceland_imo': lambda: parse_iceland(_json(ICELAND_URL)),
    'portugal_anepc': lambda: parse_portugal(_json(PORTUGAL_URL)),
    'es_catalonia_fire': lambda: parse_catalonia_fires(_json(CATALONIA_FIRE_URL)),
    'es_zaragoza_fire': _zaragoza_fire,
    'sweden_vma': lambda: parse_sweden_vma(_json(SWEDEN_VMA_URL)),
    'sweden_police': _sweden_police,
    'norway_police': lambda: parse_norway_police(_json(NORWAY_POLICE_URL)),
    'poland_rso': lambda: parse_poland_rso(_json(POLAND_RSO_URL)),
    'cz_usti_fire': _usti_emergencies,
    'nl_p2000': _netherlands_p2000,
    'lu_alert': _luxembourg_alerts,
    'th_ddpm': _thailand_ddpm,
    'id_bnpb': lambda: parse_indonesia_bnpb(_html(INDONESIA_BNPB_URL)),
    'np_bipad': _nepal_bipad,
    'am_rescue': rescue_reports,
}


def international_emergency_snapshot():
    with _REFRESH_LOCK:
        return _international_emergency_snapshot()


def _international_emergency_snapshot():
    with _LOCK:
        if _CACHE['value'] is not None and time.time() < _CACHE['until']:
            return _CACHE['value']
    results, errors, stale_sources = {}, [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_LOADERS)) as executor:
        futures = {executor.submit(loader): name for name, loader in _LOADERS.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                errors.append(f'{name}: {exc}')
                with _LOCK:
                    cached = _CACHE['sources'].get(name)
                    cached_at = _CACHE['source_times'].get(name, 0)
                if cached is not None and time.time() - cached_at < 900:
                    results[name] = cached
                    stale_sources.append(name)
    if not results and _CACHE['value'] is not None and time.time() < _CACHE['until'] + 600:
        return _CACHE['value']
    if not results:
        raise ValueError('; '.join(errors))
    snapshot = {'items': [item for name in _LOADERS for item in results.get(name, [])],
                'sourceCounts': {name: len(results.get(name, [])) for name in _LOADERS},
                'sourceErrors': errors, 'staleSources': stale_sources,
                'retrieved': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')}
    with _LOCK:
        for name, items in results.items():
            if name not in stale_sources:
                _CACHE['sources'][name] = items
                _CACHE['source_times'][name] = time.time()
        _CACHE.update(value=snapshot, until=time.time() + 300)
    return snapshot
