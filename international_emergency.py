"""Small, attributed set of official emergency feeds outside the US/Canada."""

import concurrent.futures
import datetime as dt
import email.utils
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache
from zoneinfo import ZoneInfo


NSW_URL = 'https://www.rfs.nsw.gov.au/feeds/majorIncidents.json'
VIC_URL = 'https://data.emergency.vic.gov.au/Show?pageId=getIncidentJSON'
QLD_URL = 'https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com/content/Feeds/BushfireCurrentIncidents/bushfireAlert_capau.xml'
NZ_URL = 'https://alerthub.civildefence.govt.nz/atom/pwp'
ENGLAND_URL = 'https://environment.data.gov.uk/flood-monitoring/id/floods'
BURGENLAND_URL = 'https://einsatz.lsz-b.at/'
UPPER_AUSTRIA_URL = 'https://cf-einsaetze.ooelfv.at/webext2/rss/json_laufend.txt'
UPPER_AUSTRIA_SOURCE = 'https://einsaetze.ooelfv.at/einsatz/aktuell'
ICELAND_URL = 'https://api.vedur.is/capbroker/active/detailed/all'
PORTUGAL_SOURCE = 'https://dados.gov.pt/en/datasets/prociv-ocorrencias-em-aberto'
SWEDEN_VMA_URL = 'https://vmaapi.sr.se/api/v3/alerts'
SWEDEN_POLICE_URL = 'https://polisen.se/api/events'
PORTUGAL_URL = ('https://services-eu1.arcgis.com/VlrHb7fn5ewYhX6y/arcgis/rest/services/'
                'OcorrenciasSite/FeatureServer/0/query?where=1%3D1&outFields='
                'ID_oc%2CNumero%2CEstadoAgrupado%2CNatureza%2CConcelho%2CRegiao%2C'
                'Operacionais%2CMeiosTerrestres%2CMeiosAereos%2CDataDosDados&'
                'returnGeometry=true&outSR=4326&f=json')
_ATOM = '{http://www.w3.org/2005/Atom}'
_CAP = '{urn:oasis:names:tc:emergency:cap:1.2}'
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'value': None, 'sources': {}, 'source_times': {}}
_REFRESH_LOCK = threading.Lock()
_SWEDEN_POLICE_LOCK = threading.Lock()
_SWEDEN_POLICE_CACHE = {'until': 0, 'items': None}


def _get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public emergency feed reader)', 'Accept': 'application/json, application/atom+xml, application/xml'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read(8 * 1024 * 1024 + 1)


def _json(url):
    body = _get(url)
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
    return _json(f'https://environment.data.gov.uk/flood-monitoring/id/floodAreas/{area_id}').get('items') or {}


def parse_england(payload):
    rows = [row for row in (payload.get('items') or []) if re.fullmatch(r'[A-Za-z0-9]+', str(row.get('floodAreaID') or ''))]
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
    'sweden_vma': lambda: parse_sweden_vma(_json(SWEDEN_VMA_URL)),
    'sweden_police': _sweden_police,
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
