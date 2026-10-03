"""Build offline ENA place references from a downloaded GeoNames AM.txt."""

import datetime as dt
import json
from pathlib import Path
import re
import sys
import unicodedata


def normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold().replace('և', 'եւ')
    return ' '.join(re.sub(r'[-–—]', ' ', value).split())


def build(path):
    regions = {'արարատի': '02', 'սյունիքի': '08', 'վայոց ձորի': '10', 'արագածոտնի': '01',
               'արմավիրի': '03', 'գեղարքունիքի': '04', 'կոտայքի': '05', 'լոռու': '06',
               'շիրակի': '07', 'տավուշի': '09'}
    # GeoNames ADM2 records, explicitly corresponding to ENA's ward headings.
    wards = {'կենտրոն': '13156599', 'արաբկիր': '616205', 'աջափնյակ': '13156594',
             'ավան': '13156595', 'դավթաշեն': '13156596', 'էրեբունի': '13156597',
             'քանաքեռ զեյթուն': '13156598', 'մալաթիա սեբաստիա': '13156600',
             'նորք մարաշ': '13156601', 'նոր նորք': '13156602',
             'նուբարաշեն': '13156603', 'շենգավիթ': '13156604'}
    places, districts = [], {}
    # ENA uses these spellings absent from the current GeoNames aliases. Keep
    # each addition pinned to the gazetteer ID, province, and place name.
    extra_aliases = {'174706': ('Goravan', '02', ['գոռավան']),
                     '823814': ('Ayntap', '02', ['այնթափ']),
                     '174908': ('Herher', '10', ['հեր հեր'])}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        r = line.split('\t')
        if len(r) != 19 or r[8] != 'AM':
            raise ValueError('Expected GeoNames Armenia gazetteer')
        aliases = {normalize(a) for a in (r[1], *r[3].split(','))}
        aliases = {a for a in aliases if re.fullmatch(r'[ա-ֆ][ա-ֆ\s]*', a)}
        if r[0] in extra_aliases:
            name, province, extra = extra_aliases[r[0]]
            if r[1] != name or r[10] != province or r[7] != 'PPL':
                raise ValueError('Unexpected ENA spelling reference')
            aliases.update(extra)
        item = {'id': r[0], 'name': r[1], 'admin1': r[10], 'kind': r[7],
                'coordinates': [float(r[5]), float(r[4])], 'aliases': sorted(aliases)}
        if r[0] in wards.values():
            if r[7] != 'ADM2' or r[10] != '11':
                raise ValueError('Unexpected Yerevan district reference')
            districts[r[0]] = item
        if r[7] in {'PPL', 'PPLA', 'PPLA2', 'PPLC'} and aliases:
            places.append(item)
    if set(districts) != set(wards.values()):
        raise ValueError('Missing Yerevan district reference')
    return {'source': 'https://download.geonames.org/export/dump/AM.zip',
            'license': 'CC BY 4.0', 'retrieved': dt.date.today().isoformat(),
            'description': 'Approximate named place and utility ward references; no customer outage footprints.',
            'regions': regions, 'wards': wards, 'districts': districts, 'locations': places,
            'alias_reference': 'https://www.ena.am/Info.aspx?id=5&lang=1',
            'municipal_centres': ['616785'],
            'municipal_reference': 'https://www.charentsavan.am/Upload/hr/CompanyPhoto/260514103852_Kanonadrutyun.pdf'}


if __name__ == '__main__':
    result = build(sys.argv[1])
    target = Path(__file__).resolve().parent.parent / 'armenia-outage-locations.json'
    target.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(f'{len(result["locations"])} settlements and {len(result["districts"])} districts written')
