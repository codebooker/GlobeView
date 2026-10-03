"""Dated public Tajikhydromet bulletins, represented at a capital reference."""
import datetime as dt
import hashlib
import re
import urllib.request
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

SOURCE = 'https://www.meteo.tj/ru'
_TZ = ZoneInfo('Asia/Dushanbe')
_MONTHS = dict(zip(('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
                   'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'),
                  range(1, 13)))
_DATES = re.compile(r'(?<!\d)(\d{1,2}(?:\s*(?:и|по|[-–—])\s*\d{1,2})*)\s+'
                    r'(' + '|'.join(_MONTHS) + r')\b', re.I)


class _Bulletin(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = None
        self.ready = False
        self.paragraph = None
        self.bulletins = []

    def handle_starttag(self, tag, attrs):
        if tag == 'h2':
            self.ready = False
            self.heading = []
        elif tag == 'p' and self.ready:
            self.paragraph = []
            self.ready = False
        elif tag == 'br' and self.paragraph is not None:
            self.paragraph.append(' ')

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        if self.paragraph is not None:
            self.paragraph.append(data)

    def handle_endtag(self, tag):
        if tag == 'h2' and self.heading is not None:
            self.ready = ''.join(self.heading).strip().lower() == 'предупреждение!'
            self.heading = None
        elif tag == 'p' and self.paragraph is not None:
            self.bulletins.append(' '.join(''.join(self.paragraph).split()))
            self.paragraph = None


def parse_alerts(document, now):
    parser = _Bulletin()
    parser.feed(document)
    if len(parser.bulletins) > 1:
        raise ValueError('Tajikhydromet bulletin layout changed')
    if not parser.bulletins:
        return []
    text = parser.bulletins[0]
    if not 20 <= len(text) <= 5000:
        raise ValueError('Tajikhydromet bulletin exceeded size limits')
    local_now = now.astimezone(_TZ)
    years = set(re.findall(r'\b(20\d{2})\s*(?:года|г\.)', text, re.I))
    if years:
        if years != {str(local_now.year)}:
            return []
        year = int(next(iter(years)))
    elif re.search(r'\bтекущего года\b', text, re.I):
        year = local_now.year
    else:
        # Do not guess a year for undated or archived notices.
        return []
    dates = []
    try:
        for match in _DATES.finditer(text):
            dates.extend(dt.date(year, _MONTHS[match[2].lower()], int(day))
                         for day in re.findall(r'\d+', match[1]))
    except ValueError:
        return []
    if not dates:
        return []
    first, last = min(dates), max(dates)
    if (last < local_now.date() or first > local_now.date() + dt.timedelta(days=7)
            or first < local_now.date() - dt.timedelta(days=7)
            or last - first > dt.timedelta(days=7)):
        return []
    ends = dt.datetime.combine(last + dt.timedelta(days=1), dt.time(), _TZ)
    if now >= ends:
        return []
    hazards = [name for pattern, name in (
        (r'осад|снег|дожд', 'precipitation'),
        (r'понижен\w* температур', 'cooling'),
        (r'ветр', 'wind'),
        (r'\bсел(?:ев\w*|и|ь)\b', 'mudflows'),
        (r'пыл', 'dust'),
    ) if re.search(pattern, text, re.I)]
    summary = ('Bulletin covers ' + ', '.join(hazards) + '.' if hazards
               else 'Regional weather bulletin.')
    return [{
        'id': 'tj:hydromet:' + hashlib.sha256(text.encode()).hexdigest()[:20],
        'title': 'Tajikistan weather advisory', 'country': 'Tajikistan',
        'source': 'Tajikhydromet', 'sourceUrl': SOURCE,
        # OSM city relation 4479735; this is not an incident or hazard location.
        'lon': 68.760331, 'lat': 38.5856814,
        'locationKind': 'national advisory reference point',
        'area': 'Regional bulletin', 'severity': 'Unknown',
        'starts': dt.datetime.combine(first, dt.time(), _TZ).isoformat(),
        'ends': ends.isoformat(),
        'advice': summary + ' See source for affected areas and advice.',
    }]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Tajikhydromet bulletin redirected')


_OPENER = urllib.request.build_opener(_NoRedirect())


def alerts():
    request = urllib.request.Request(SOURCE, headers={
        'User-Agent': 'GlobeView/1.0 (https://globeview.app)', 'Accept': 'text/html',
    })
    with _OPENER.open(request, timeout=15) as response:
        if response.status != 200 or response.url != SOURCE:
            raise ValueError('Tajikhydromet bulletin response changed')
        if response.headers.get('Content-Type', '').split(';')[0].lower() != 'text/html':
            raise ValueError('Tajikhydromet bulletin is not HTML')
        body = response.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError('Tajikhydromet page exceeded size limit')
    return parse_alerts(body.decode('utf-8'), dt.datetime.now(dt.timezone.utc))
