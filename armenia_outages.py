"""ENA's dated planned outages, at approximate district/settlement references."""

import datetime as dt
import functools
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
import time
import unicodedata
import urllib.request

SOURCE_URL = 'https://www.ena.am/Info.aspx?id=5&lang=1'
_ZONE = dt.timezone(dt.timedelta(hours=4))
_UTC = dt.timezone.utc
_LOCK = threading.Lock()
_CACHE = {'until': 0, 'blocks': []}
_MONTHS = {'հունվարի': 1, 'փետրվարի': 2, 'մարտի': 3, 'ապրիլի': 4, 'մայիսի': 5,
           'հունիսի': 6, 'հուլիսի': 7, 'օգոստոսի': 8, 'սեպտեմբերի': 9,
           'հոկտեմբերի': 10, 'նոյեմբերի': 11, 'դեկտեմբերի': 12}
_DATE = re.compile(r'տեղեկացնում է, որ\s+(?:(\d{4})\s*(?:թվականի|թ\.)\s*)?('
                   + '|'.join(_MONTHS) + r')\s+(\d{1,2})\s*[–-]ին\b')
_CLOCK = re.compile(r'^(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})\s+(.+)$')
_TIME_START = re.compile(r'^\d{1,2}:\d{2}\s*[–—-]\s*\d{1,2}:\d{2}(?:\s|$)')


def normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold().replace('և', 'եւ')
    return ' '.join(re.sub(r'[-–—]', ' ', value).split())


class _PlannedPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth, self.parts, self.blocks, self.found = 0, [], [], False
        self.group, self.groups = 0, []

    def flush(self):
        value = ' '.join(''.join(self.parts).split())
        self.parts = []
        if value:
            self.blocks.append(value)
            self.groups.append(self.group)

    def handle_starttag(self, tag, attrs):
        if dict(attrs).get('id') == 'ctl00_ContentPlaceHolder1_attenbody':
            self.depth, self.found = 1, True
        elif self.depth and tag not in {'br', 'img', 'hr', 'input', 'meta', 'link'}:
            self.depth += 1
        if self.depth and tag in {'p', 'br'}:
            self.flush()
            if tag == 'p':
                self.group += 1

    def handle_endtag(self, tag):
        if self.depth and tag not in {'br', 'img', 'hr', 'input', 'meta', 'link'}:
            if tag == 'p' or self.depth == 1:
                self.flush()
                self.group += 1
            self.depth -= 1

    def handle_data(self, value):
        if self.depth:
            self.parts.append(value)


def planned_blocks(html):
    parser = _PlannedPage()
    parser.feed(html)
    parser.flush()
    if not parser.found or not any('պլանային' in p for p in parser.blocks):
        raise ValueError('ENA planned schedule section changed')
    if len(parser.blocks) > 2000:
        raise ValueError('ENA planned schedule exceeded row limit')
    blocks, groups = [], []
    for block, group in zip(parser.blocks, parser.groups):
        boundary = (_TIME_START.match(block) or 'տեղեկացնում է, որ' in block
                    or re.search(r'(?:մարզ|վարչական շրջան)\s*[՝:`՛]?$', block))
        if blocks and groups[-1] == group and _TIME_START.match(blocks[-1]) and not boundary:
            blocks[-1] += ' ' + block
        else:
            blocks.append(block)
            groups.append(group)
    return blocks


@functools.lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name('armenia-outage-locations.json').read_text(encoding='utf-8'))


def locations_for(scope, address, refs):
    text = normalize(address)
    selected = {}
    ward = refs['wards'].get(scope)
    if ward:
        selected[ward] = refs['districts'][ward]
    code = '11' if ward else refs['regions'].get(scope)
    if not code:
        return []
    matcher_key = '*' if ward else code
    matchers = refs.setdefault('_location_matchers', {})
    if matcher_key not in matchers:
        names = {}
        for place in refs['locations']:
            if not ward and place['admin1'] != code:
                continue
            for alias in place['aliases']:
                names.setdefault(alias, {})[place['id']] = place
        expression = r'(?<!\w)(?:' + '|'.join(re.escape(a) for a in sorted(names, key=len, reverse=True)) + r')(?!\w)'
        matchers[matcher_key] = (re.compile(expression), names)
    pattern, names = matchers[matcher_key]
    for match in pattern.finditer(text):
        tail = text[match.end():]
        explicit = re.match(r'\s+(գյուղ|քաղաք|համայնք)[ա-ֆ]*\b', tail)
        # Comma-separated village lists share a final village qualifier. A
        # named street list, business, or road cannot supply that qualifier.
        qualifier = re.search(r'գյուղ|քաղաք|փողոց|պողոտա|խճուղի|սպը|աձ|[;։]', tail)
        village_list = tail.lstrip().startswith(',') and qualifier and qualifier[0] == 'գյուղ'
        if not explicit and not village_list:
            continue
        candidates = list(names[match[0]].values())
        if explicit and explicit[1] == 'քաղաք':
            cities = [p for p in candidates if p['kind'] in {'PPLC', 'PPLA', 'PPLA2'}]
            candidates = cities or candidates
        if explicit and explicit[1] == 'գյուղ' and len(candidates) > 1:
            # Martuni's village and district-seat city share a name and region.
            # Only use this distinction when it identifies one populated place.
            villages = [p for p in candidates if p['kind'] == 'PPL']
            if len(villages) == 1:
                candidates = villages
        if explicit and explicit[1] == 'համայնք':
            candidates = [p for p in candidates if p['id'] in refs.get('municipal_centres', [])]
            if 'թաղամաս' not in tail or 'գյուղ' in tail:
                continue
        # Village names under a Yerevan utility heading must be unique in the
        # whole country; their own province supplies the coordinate reference.
        if ward and not (explicit and explicit[1] == 'գյուղ'):
            continue
        if len(candidates) == 1:
            place = candidates[0]
            selected[place['id']] = place
    return list(selected.values())


def parse_schedule(blocks, now=None, refs=None):
    now = now or dt.datetime.now(_UTC)
    if now.tzinfo is None:
        raise ValueError('ENA outage clock needs a time zone')
    refs = refs or catalog()
    local = now.astimezone(_ZONE)
    day, scope, result, seen = None, None, [], set()
    for block in blocks:
        if 'տեղեկացնում է, որ' in block:
            day, scope = None, None
            match = _DATE.search(block)
            if match:
                years = [int(match[1])] if match[1] else [local.year - 1, local.year, local.year + 1]
                candidates = []
                for year in years:
                    try:
                        candidate = dt.date(year, _MONTHS[match[2]], int(match[3]))
                        if local.date() <= candidate <= local.date() + dt.timedelta(days=7):
                            candidates.append(candidate)
                    except ValueError:
                        pass
                if len(candidates) == 1:
                    day = candidates[0]
            continue
        heading = normalize(block).strip(' ՝:`՛')
        if 'մարզ' in heading or 'վարչական շրջան' in heading:
            scope = None  # Unknown heading must not inherit the previous region.
            if heading.endswith(' մարզ'):
                name = heading[:-5].strip()
                if name in refs['regions']:
                    scope = name
            match = re.fullmatch(r'երեւանի\s+(.+)\s+վարչական շրջան', heading)
            if match and match[1] in refs['wards']:
                scope = match[1]
            continue
        match = _CLOCK.fullmatch(block)
        if not day or not scope or not match:
            continue
        try:
            start = dt.datetime.combine(day, dt.time(int(match[1]), int(match[2])), _ZONE)
            end = start.replace(hour=int(match[3]), minute=int(match[4]))
        except ValueError:
            continue
        if not start < end or end <= now:
            continue
        address = match[5]
        for place in locations_for(scope, address, refs):
            key = hashlib.sha256(f'{day}:{match[0]}:{scope}:{place["id"]}'.encode()).hexdigest()[:20]
            if key in seen:
                continue
            seen.add(key)
            result.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': place['coordinates']},
                           'properties': {
                               'key': 'am:ena:planned:' + key, 'provider': 'ENA · planned power work',
                               'area_name': place['name'] + ' · Approximate area reference',
                               'status': ('Scheduled' if now < start else 'Planned work window')
                                         + f' · {day:%d %b} · {start:%H:%M}–{end:%H:%M} UTC+4',
                               'customers_affected': None, 'planned': True, 'location_kind': 'area',
                               'source_address': address[:1000], 'source_district': scope,
                               'source_label': 'Electric Networks of Armenia · GeoNames (CC BY 4.0)',
                               'source_url': SOURCE_URL,
                               'location_source_url': 'https://www.geonames.org/' + place['id'] + '/',
                               'starts_at': start.astimezone(_UTC).isoformat(),
                               'ends_at': end.astimezone(_UTC).isoformat(),
                               'valid_until': min(end.timestamp(), now.timestamp() + 900),
                           }})
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError('ENA schedule redirects are unsupported')


def _read_schedule():
    request = urllib.request.Request(SOURCE_URL, headers={'User-Agent': 'GlobeView/1.0 (https://globeview.app)'})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
        if response.status != 200 or response.geturl() != SOURCE_URL:
            raise ValueError('Unexpected ENA schedule response')
        body = response.read(2000001)
    if len(body) > 2000000:
        raise ValueError('ENA schedule exceeded size limit')
    return planned_blocks(body.decode('utf-8-sig'))


def ena_planned_outages(now=None):
    with _LOCK:
        if time.time() >= _CACHE['until']:
            blocks = _read_schedule()
            _CACHE.update(until=time.time() + 3600, blocks=blocks)
        blocks = _CACHE['blocks']
    return parse_schedule(blocks, now)
