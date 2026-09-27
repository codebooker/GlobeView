"""Small, curated ArcGIS FeatureServer catalog for viewport map layers."""

import json
import math
import urllib.parse
import urllib.request


LAYERS = {
    'transmission': {
        'url': 'https://arcgis.netl.doe.gov/server/rest/services/Hosted/Energy_Transition_Atlas_493d6/FeatureServer/18',
        'fields': 'objectid_1,id,type,status,owner,voltage,volt_class',
    },
    'gas_pipelines': {
        'url': 'https://arcgis.netl.doe.gov/server/rest/services/Hosted/Natural_Gas_Pipelines/FeatureServer/10',
        'fields': 'objectid,typepipe,operator',
    },
    'wind_turbines': {
        'url': 'https://energy.usgs.gov/arcgis/rest/services/Hosted/uswtdbDyn/FeatureServer/0',
        'fields': 'objectid,case_id,p_name,p_year,t_manu,t_model,t_cap,t_state',
    },
    'solar_sites': {
        'url': 'https://energy.usgs.gov/arcgis/rest/services/Hosted/uspvdbDyn/FeatureServer/0',
        'fields': 'objectid,case_id,p_name,p_year,p_cap_ac,p_state,p_area',
    },
    'power_plants': {
        'url': 'https://services.arcgis.com/XSeYKQzfXnEgju9o/arcgis/rest/services/Global%20Power%20Plant%20Database/FeatureServer/0',
        'fields': 'gppd_idnr,name,country_long,capacity_mw,primary_fuel,owner,year_of_capacity_data',
        'max_records': 1000,
    },
    'active_faults': {
        'url': 'https://services.arcgis.com/ue9rwulIoeLEI9bj/ArcGIS/rest/services/Active_fault/FeatureServer/0',
        'fields': 'OBJECTID,catalog_id,catalog_na,name,slip_type',
    },
    'impact_sites': {
        'url': 'https://services.arcgis.com/e8gGAYmR5kxEFApE/ArcGIS/rest/services/Meteorites/FeatureServer/0',
        'fields': 'OBJECTID,FEAT_NAME,CLASS,COUNTRY,REGION,AGE_DESC,AGE_BEST,DIA_QUANT,IMPACTOR',
        'max_records': 1000,
    },
    'rail_world': {
        'url': 'https://services1.arcgis.com/DbPykcCwUUYq5zKg/ArcGIS/rest/services/railroads/FeatureServer/0',
        'fields': 'FID,featurecla,continent,scalerank',
    },
    'rail_narn': {
        'url': 'https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_North_American_Rail_Network_Lines/FeatureServer/0',
        'fields': 'OBJECTID,RROWNER1,DIVISION,SUBDIV,TRACKS,COUNTRY,NET',
        'simplify': True,
    },
    'shipping_routes': {
        'url': 'https://services6.arcgis.com/22wyIskRdsHsTOJF/ArcGIS/rest/services/World_Shipping_Lanes/FeatureServer/407',
        'fields': 'FID,Type',
        'max_records': 10,
        'global': True,
    },
}


def parse_bbox(value):
    if len(value) > 100:
        raise ValueError('Invalid bounding box')
    parts = [float(part) for part in value.split(',')]
    if len(parts) != 4 or not all(map(math.isfinite, parts)):
        raise ValueError('Invalid bounding box')
    west, south, east, north = parts
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError('Invalid bounding box')
    if east - west > 80 or north - south > 65:
        raise ValueError('Bounding box is too large')
    return tuple(round(part, 3) for part in parts)


def _query(url, params):
    request = urllib.request.Request(
        url + '/query?' + urllib.parse.urlencode(params),
        headers={'User-Agent': 'GlobalMap/1.0 (public ArcGIS layer viewer)', 'Accept': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError('ArcGIS service returned an error')
        data = json.load(response)
    if 'error' in data:
        raise RuntimeError(str(data['error'].get('message', 'ArcGIS query failed')))
    return data


def arcgis_viewport(layer, bbox):
    if layer not in LAYERS:
        raise ValueError('Unknown ArcGIS layer')
    config = LAYERS[layer]
    params = {
        'where': '1=1',
        'f': 'json', 'returnCountOnly': 'true',
    }
    if not config.get('global'):
        bbox = parse_bbox(bbox)
        params.update({
            'geometry': ','.join(map(str, bbox)),
            'geometryType': 'esriGeometryEnvelope',
            'inSR': '4326', 'outSR': '4326',
            'spatialRel': 'esriSpatialRelIntersects',
        })
    count = _query(config['url'], params).get('count')
    if not isinstance(count, int) or count < 0:
        raise RuntimeError('ArcGIS count unavailable')
    max_records = config.get('max_records', 2000)
    if count > max_records:
        return {'type': 'FeatureCollection', 'features': [], 'count': count, 'too_many': True}
    params.update({
        'f': 'geojson', 'returnCountOnly': 'false', 'returnGeometry': 'true',
        'outFields': config['fields'], 'geometryPrecision': '4' if config.get('global') else '5',
        'resultRecordCount': str(max_records),
    })
    if config.get('simplify'):
        # Keep detailed geometry at close zoom while limiting payloads for wide views.
        offset = max(0.00001, min(0.001, (bbox[2] - bbox[0]) / 1500))
        params['maxAllowableOffset'] = f'{offset:.6f}'
    result = _query(config['url'], params)
    if result.get('type') != 'FeatureCollection' or not isinstance(result.get('features'), list):
        raise RuntimeError('ArcGIS geometry unavailable')
    return {'type': 'FeatureCollection', 'features': result['features'], 'count': count, 'too_many': False}
