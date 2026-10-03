"""Public road and electricity feeds outside North America, normalized for the map."""

import concurrent.futures
import base64
import csv
import datetime as dt
import email.utils
import functools
import gzip
import hashlib
import html
import io
import json
import http.cookiejar
import math
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

from pyproj import Transformer
from PIL import Image, UnidentifiedImageError
import shapefile

from kyrgyzstan_outages import bishkek_planned_outages, issyk_kul_planned_outages
from azerbaijan_outages import azerishiq_planned_outages
from kyrgyzstan_roads import bishkek_roadworks
from kyrgyztelecom_cameras import camera_features as kyrgyztelecom_cameras, camera_segment


FINTRAFFIC_BASE = 'https://tie.digitraffic.fi'
TII_TRAFFIC_BASE = 'https://iretg.carsprogram.org'
TII_TRAFFIC_SOURCE = 'https://traffic.tii.ie/'
HONG_KONG_CAMERAS_URL = ('https://static.data.gov.hk/td/traffic-snapshot-images/'
                         'code/Traffic_Camera_Locations_En.xml')
HONG_KONG_CAMERAS_SOURCE = 'https://data.gov.hk/en-data/dataset/hk-td-tis_2-traffic-snapshot-images'
HONG_KONG_WORKS_URL = 'https://resource.data.one.gov.hk/td/roadworks-location/get_all_the_roadworks.geojson'
HONG_KONG_WORKS_SOURCE = 'https://data.gov.hk/en-data/dataset/hk-td-tis_18-roadworks-location'
HONG_KONG_SENSORS_URL = 'https://resource.data.one.gov.hk/td/traffic-detectors/rawSpeedVol-all.xml'
HONG_KONG_SENSOR_LOCATIONS_URL = ('https://static.data.gov.hk/td/traffic-data-strategic-major-roads/'
                                  'info/traffic_speed_volume_occ_info.csv')
HONG_KONG_SENSORS_SOURCE = 'https://data.gov.hk/en-data/dataset/hk-td-sm_4-traffic-data-strategic-major-roads'
HONG_KONG_SENSOR_MAX_AGE = 20 * 60
SINGAPORE_CAMERAS_URL = 'https://api.data.gov.sg/v1/transport/traffic-images'
SINGAPORE_CAMERAS_SOURCE = 'https://data.gov.sg/datasets/d_6cdb6b405b25aaaacbaf7689bcc6fae0/view'
TAIPEI_WORKS_URL = 'https://tpnco.blob.core.windows.net/blobfs/Todaywork.json'
TAIPEI_WORKS_SOURCE = 'https://data.gov.tw/en/datasets/145614'
TAIPEI_CMS_STATIC_URL = 'https://tcgbusfs.blob.core.windows.net/blobtisv/CMS.xml'
TAIPEI_CMS_LIVE_URL = 'https://tcgbusfs.blob.core.windows.net/blobtisv/CMSLive.xml'
TAIPEI_CMS_SOURCE = 'https://data.gov.tw/en/datasets/129029'
TAIWAN_HIGHWAY_CCTV_URL = 'https://cctv-maintain.thb.gov.tw/opendataCCTVs.xml'
TAIWAN_HIGHWAY_CMS_STATIC_URL = 'https://thbapp.thb.gov.tw/opendata/cms/info/CMSList.xml'
TAIWAN_HIGHWAY_CMS_LIVE_URL = 'https://thbapp.thb.gov.tw/opendata/cms/two/CMSLiveList.xml'
TAIWAN_HIGHWAY_VD_STATIC_URL = 'https://thbapp.thb.gov.tw/opendata/vd/info/VDList.xml'
TAIWAN_HIGHWAY_VD_LIVE_URL = 'https://thbapp.thb.gov.tw/opendata/vd/one/VDLiveList.xml'
TAIWAN_HIGHWAY_SOURCE = 'https://data.gov.tw/en/datasets/29817'
DUBLIN_CLOSURES_URL = ('https://www.dublincity.ie/travel-and-transport/'
                       'read-latest-traffic-news/current-road-closures')
COPENHAGEN_WORKS_BASE = 'https://wfs-kbhkort.kk.dk/k101/ows'
COPENHAGEN_WORKS_SOURCE = 'https://www.opendata.dk/city-of-copenhagen/raden-over-vej-med-historik'
VEJLE_WORKS_URL = 'https://kortservice.vejle.dk/gis/rest/services/OPENDATA/Vejle/MapServer/25/query'
VEJLE_WORKS_FALLBACK_URL = ('https://kortservice.vejle.dk/gis/rest/services/'
                            'Vej_Trafik/Vejdrift_simplekort/MapServer/33/query')
VEJLE_WORKS_SOURCE = 'https://www.opendata.dk/city-of-vejle/gravetilladelser1'
ZAGREB_CLOSURES_URL = ('https://data.zagreb.hr/dataset/7ff5514d-0a1f-4f6c-86bd-8ed9a3c55eee/'
                       'resource/e48b6992-add0-45a1-ae95-c5d97d8db259/download/data.json')
ZAGREB_CLOSURES_SOURCE = 'https://data.zagreb.hr/dataset/prometnice'
MADRID_BASE = 'https://informo.madrid.es/informo/tmadrid/'
MADRID_SOURCE = 'https://datos.madrid.es/dataset/202062-0-trafico-incidencias-viapublica'
MADRID_CAMERAS_SOURCE = 'https://datos.madrid.es/dataset/202088-0-trafico-camaras'
ZARAGOZA_ROADS_URL = 'https://www.zaragoza.es/sede/servicio/via-publica/incidencia.json?rows=500&srsname=wgs84'
ZARAGOZA_ROADS_SOURCE = 'https://www.zaragoza.es/sede/servicio/catalogo/67'
LYON_CAMERAS_SOURCE = 'https://www.data.gouv.fr/datasets/cameras-web-criter-de-la-metropole-de-lyon'
LYON_WORKS_SOURCE = 'https://data.grandlyon.com/portail/fr/jeux-de-donnees/chantiers-perturbants-metropole-lyon/info'
LYON_WORKS_METADATA_URL = 'https://www.data.gouv.fr/api/1/datasets/chantiers-perturbants-de-la-metropole-de-lyon/'
LYON_WORKS_URL = ('https://data.grandlyon.com/geoserver/ogc/features/v1/collections/'
                  'metropole-de-lyon%3Apvo_patrimoine_voirie.pvochantierperturbant/items?'
                  'f=application%2Fgeo%2Bjson&limit=1000')
LYON_CAMERAS_URL = ('https://data.grandlyon.com/geoserver/metropole-de-lyon/ows?'
                   'SERVICE=WFS&VERSION=2.0.0&request=GetFeature&'
                   'typename=metropole-de-lyon:pvo_patrimoine_voirie.pvocameracriter&'
                   'outputFormat=application/json&SRSNAME=EPSG:4326')
LYON_CAMERA_BASE = ('https://download.data.grandlyon.com/files/rdata/'
                    'pvo_patrimoine_voirie.pvocameracriter/')
VITORIA_CAMERAS_URL = 'https://www.vitoria-gasteiz.org/c11-01w/cameras'
VITORIA_CAMERAS_SOURCE = 'https://datos.gob.es/es/catalogo/l01010590-camaras-de-trafico-en-tiempo-real'
VIGO_CAMERAS_URL = 'https://datos.vigo.org/data/trafico/camaras-trafico.geojson'
VIGO_CAMERA_BASE = 'https://camaras.vigo.org/webcam/camv2.php'
VIGO_CAMERAS_SOURCE = 'https://datos.gob.es/es/catalogo/l01360577-camaras-de-trafico'
VIGO_UNAVAILABLE_SHA256 = '3549123ffcf6e0f9ccec7d91426a9fd8c3b6409c49cb83c4c9d0c04e496530f7'
MADRID_SIGNS_SOURCE = 'https://datos.madrid.es/dataset/202078-0-trafico-paneles-superficie'
MADRID_SIGN_LOCATIONS = ('https://datos.madrid.es/dataset/202535-0-paneles-informacion-variable/'
                         'resource/202535-2-paneles-informacion-variable-csv/download/'
                         '202535-2-paneles-informacion-variable-csv.csv')
SOUTH_TYROL_ROADS_URL = ('https://datex.api.opendatahub.com/datex/2/'
                         'province-bz/situation-publication.xml')
SOUTH_TYROL_SOURCE = 'https://docs.opendatahub.com/use-data/datexii-api/reference/'
A22_ANNOUNCEMENTS_URL = 'https://tourism.api.opendatahub.com/v1/Announcement'
A22_ANNOUNCEMENTS_SOURCE = 'https://databrowser.opendatahub.com/'
BERLIN_ROADS_URL = 'https://api.viz.berlin.de/tic3/baustellen_sperrungen_tic.json'
BERLIN_ROADS_SOURCE = ('https://daten.berlin.de/datensaetze/'
                       'baustellen-sperrungen-und-sonstige-storungen-von-besonderem-verkehrlichem-interesse')
GDYNIA_ROADS_BASE = 'https://api.zdiz.gdynia.pl/ri/rest/'
GDYNIA_ROADS_SOURCE = ('https://otwartedane.gdynia.pl/dataset/fc4f3a7c-b877-4fe1-ab22-3fb54e6513f7/'
                       'resource/a426e7b7-7261-4f43-8acb-59a5eca167aa/download/tristar_api.pdf')
FINTRAFFIC_CAMERAS_SOURCE = 'https://www.digitraffic.fi/en/road-traffic/'
ICELAND_CAMERAS_URL = 'https://gagnaveita.vegagerdin.is/api/vefmyndavelar2014_1'
ICELAND_CAMERAS_SOURCE = 'https://www.vegagerdin.is/vegagerdin/gagnasafn/vefthjonustur/vefmyndavelar'
ICELAND_ROADS_URL = 'https://datex.vegagerdin.is/situationpublication3_1/SituationService/pullsnapshotdata'
ICELAND_ROADS_SOURCE = 'https://www.vegagerdin.is/vegagerdin/gagnasafn/vefthjonustur/datexii-2'
TFL_URL = 'https://api.tfl.gov.uk/Road/all/Disruption'
TFL_CAMERAS_URL = 'https://api.tfl.gov.uk/Place/Type/JamCam'
TFL_CAMERA_SOURCE = 'https://tfl.gov.uk/info-for/open-data-users/our-open-data'
TFL_CAMERA_BASE = 'https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/'
TRAFFICWATCH_BASE = 'https://www.trafficwatchni.com/twni/'
TRAFFICWATCH_SOURCE = 'https://www.trafficwatchni.com/twni/cameras'
UKPN_BASE = 'https://ukpowernetworks.opendatasoft.com'
UKPN_STREETWORKS_DATASET = 'ukpn-open-streetworks'
NPG_BASE = 'https://northernpowergrid.opendatasoft.com'
NGED_OUTAGES_URL = ('https://connecteddata.nationalgrid.co.uk/dataset/'
                    'd6672e1e-c684-4cea-bb78-c7e5248b62a2/resource/'
                    '292f788f-4339-455b-8cc0-153e14509d4d/download/power_outage_ext.csv')
SSEN_OUTAGES_URL = 'https://external.distribution.prd.ssen.co.uk/opendataportal-prd/v4/api/getallfaults'
NIE_OUTAGES_URL = 'https://powercheck.nienetworks.co.uk/NIEPowerCheckerWebAPI/api/faults'
NIE_OUTAGES_SOURCE = 'https://powercheck.nienetworks.co.uk/'
LIANDER_OUTAGES_URL = ('https://services1.arcgis.com/v6W5HAVrpgSg3vts/ArcGIS/rest/services/'
                       'IStoringen_Productie_V7/FeatureServer/0/query')
LIANDER_OUTAGES_SOURCE = 'https://data.overheid.nl/dataset/storingsdata-liander-actuele-storingen'
AZHK_OUTAGES_URL = 'https://www.azhk.kz/Map/get_tp_to_map.txt'
AZHK_OUTAGES_SOURCE = 'https://www.azhk.kz/ru/spetsialnye-razdely/avarijnye-otklyucheniya'
KAZTOLL_CAMERAS_SOURCE = 'https://kaztoll.kz/'
QAJ_RESTRICTIONS_URL = 'https://geoportal.kaztoll.kz/adm-layers/features/attrtable'
QAJ_RESTRICTIONS_SOURCE = 'https://ru.qaj.kz/s/'
ELCAT_CAMERAS_SOURCE = 'https://kg.camera/ru/'
# Operator-published road/city views and coordinates, audited 3 October 2026.
# Park, resort and ski views are excluded from the road-camera layer.
ELCAT_CAMERAS = {
    'Too-Ashu_Tunnel_North': (73.815231, 42.356377, 'Тоо-Ашуу тоннель, Северный въезд'),
    'sulukta': (70.81949463558253, 40.06139369913987, 'г. Баткен'),
    'Too-Ashu_Tunnel_South_25_11_2019': (73.816799, 42.333043, 'Тоо-Ашуу тоннель, Южный въезд'),
    'Bishkek_Ala-Too_Square': (74.60415201293377, 42.87423182287145, 'г.Бишкек, Площадь Ала-Тоо'),
    'Suusamyr': (73.696887, 42.21877, 'Суусамыр, АЗС ГазПром'),
    'Razzakov-Center': (69.53278529459216, 39.83647649154401, 'г.Раззаков, Центр'),
    'Kemin': (75.77979012399476, 42.76784564948717, 'Бишкек - Балыкчи, 101км, кольцо'),
    'Osh-Sulaiman-Too': (72.7904057578351, 40.52780275009203, 'г. Ош, гора Сулайман-Тоо'),
    'Panorama': (74.56289400548606, 42.87868487498132, 'г. Бишкек, перекрёсток Чуй Фучика'),
    'Kyzyl-Kiya': (72.133443, 40.271733, 'г. Кызыл-Кия'),
    'Cholpon-Ata_Center_07_03_2022': (77.0892, 42.65049, 'г. Чолпон-Ата, Центр'),
    'Karakol_Vezd_25_11_2019': (78.38667, 42.499564, 'Въезд в г. Каракол'),
    'Jalalbad_Meria': (73.00113353558235, 40.92749203955725, 'г. Манас, Мэрия'),
    'Kadamjay': (71.722846, 40.129884, 'г. Кадамжай'),
    'Kok-Art': (73.764563, 41.168792, 'Кок-Арт туннель южный въезд'),
    'Balykchi': (76.160666, 42.45367, 'Въезд в г. Балыкчи'),
    'Jalalbad_Administration': (72.98088889708781, 40.929261599945285, 'г. Манас, Новое здание администрации в городе Манас'),
    'Naryn': (75.99690148465871, 41.4284660309621, 'г. Нарын Центральная Площадь'),
    'Talas-Center': (72.24887817116468, 42.521249480331875, 'г. Tалас центр'),
}
_ELCAT_SNAPSHOT_SLOTS = threading.BoundedSemaphore(2)
# Published camera names matched to named OSM toll plazas, not town centers.
# Other KazToll clips lack verified coordinates and remain catalog candidates.
KAZTOLL_CAMERAS = {
    'jjvezd': {'name': 'Жибек жолы — Въезд', 'road': 'Астана – Темиртау',
               'lon': 71.8074804, 'lat': 51.0573963, 'osm_node': 6273637658},
    'ttvezd': {'name': 'Темиртау — Въезд', 'road': 'Астана – Темиртау',
               'lon': 72.8984722, 'lat': 50.1488416, 'osm_node': 6273637676},
}
_KAZTOLL_TRANSCODE_SLOT = threading.BoundedSemaphore(1)
WALES_RSS_BASE = 'https://traffic.wales/feeds'
NATIONAL_HIGHWAYS_ROADWORKS_DATASET = ('https://www.data.gov.uk/dataset/'
                                       '5b3267d8-4307-4eef-a9af-3a4c28224694/'
                                       'highways_agency_planned_roadworks')
NATIONAL_HIGHWAYS_ROADWORKS_CATALOG = ('https://ckan.publishing.service.gov.uk/api/3/action/'
                                       'package_show?id=highways_agency_planned_roadworks')
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
NDW_SENSORS_FILE = 'snelheden_en_intensiteiten_meetgegevens_en_configuratie_meetlocaties.xml.gz'
NDW_MSI_FILE = 'Matrixsignaalinformatie.xml.gz'
NDW_MSI_SHAPES_FILE = 'ndw_msi_shapefiles_latest.zip'
NDW_MSI_SOURCE = 'https://docs.ndw.nu/en/producten/msi/'
_NDW_MSI_SHAPES = {'until': 0, 'locations': {}}
_NDW_MSI_SHAPES_LOCK = threading.Lock()
DGT_BASE = 'https://nap.dgt.es/datex2/v3/dgt/'
DGT_CAMERAS_SOURCE = 'https://nap.dgt.es/es/dataset/camaras-dgt-datex2-v3-7'
DGT_INCIDENTS_SOURCE = 'https://nap.dgt.es/es/dataset/incidencias-dgt-datex2-v3-7'
DGT_SIGNS_SOURCE = 'https://nap.dgt.es/es/dataset/paneles-dgt-tiempo-real-datex2-v3-7'
SCT_BASE = 'https://www.gencat.cat/transit/opendata/'
SCT_INCIDENTS_SOURCE = 'https://analisi.transparenciacatalunya.cat/Transport/Incid-ncies-vi-ries-en-temps-real-a-Catalunya/uyam-bs37'
SCT_CAMERAS_SOURCE = 'https://analisi.transparenciacatalunya.cat/Transport/C-meres-de-tr-nsit-a-les-carreteres-de-Catalunya/3tzz-6b9y'
POLAND_ROADS_URL = 'https://www.archiwum.gddkia.gov.pl/dane/zima_html/utrdane.xml'
CYPRUS_ROADS_URL = 'https://www.traffic4cyprus.org.cy/swarco3/api/Data/SituationPublication'
CYPRUS_ROADS_SOURCE = ('https://www.traffic4cyprus.org.cy/en/dataset/trafficevents/'
                       'resource/38f28237-79c7-4618-837a-977ef34f6480')
CYPRUS_WAZE_URL = 'https://fixcyprus.cy/gnosis/open/api/nap/datasets/waze_alerts/'
CYPRUS_WAZE_SOURCE = 'https://www.traffic4cyprus.org.cy/en/dataset/waze_alerts'
POLAND_ROADS_SOURCE = 'https://www.gov.pl/web/gddkia/dane-xml'
AUTOBAHN_BASE = 'https://verkehr.autobahn.de/o/autobahn/'
AUTOBAHN_SOURCE = 'https://www.autobahn.de/betrieb-verkehr/verkehrsmeldungen'
HAMBURG_ROADS_URL = ('https://api.hamburg.de/datasets/v1/verkehrsinformation/'
                     'collections/hauptmeldungen_aktuell/items?f=json&limit=500')
HAMBURG_ROADS_SOURCE = 'https://suche.transparenz.hamburg.de/dataset/aktuelle-verkehrsinformationen-polizei-hamburg4'
FRANCE_SENSOR_SOURCE = ('https://transport.data.gouv.fr/datasets/'
                        'etat-de-circulation-en-temps-reel-sur-le-reseau-national-routier-non-concede')
BORDEAUX_FLOW_SOURCE = 'https://www.data.gouv.fr/datasets/etat-du-trafic-en-temps-reel-3'
BORDEAUX_FLOW_API = ('https://datahub.bordeaux-metropole.fr/api/explore/v2.1/catalog/'
                     'datasets/ci_trafi_l/records')
BORDEAUX_WORKS_BASE = ('https://datahub.bordeaux-metropole.fr/api/explore/v2.1/catalog/'
                       'datasets/ci_chantier')
BORDEAUX_WORKS_SOURCE = 'https://datahub.bordeaux-metropole.fr/explore/dataset/ci_chantier/'
STRASBOURG_FLOW_SOURCE = 'https://opendata.strasbourg.eu/explore/dataset/sirac_flux_trafic/'
STRASBOURG_FLOW_BASE = ('https://opendata.strasbourg.eu/api/explore/v2.1/catalog/'
                        'datasets/sirac_flux_trafic')
RENNES_FLOW_SOURCE = 'https://data.rennesmetropole.fr/explore/dataset/etat-du-trafic-en-temps-reel/'
RENNES_FLOW_BASE = ('https://data.rennesmetropole.fr/api/explore/v2.1/catalog/'
                    'datasets/etat-du-trafic-en-temps-reel')
BISON_FLOW_SOURCE = ('https://transport.data.gouv.fr/datasets/'
                     'etat-de-circulation-en-temps-reel-sur-le-reseau-national-routier-non-concede')
BISON_FLOW_BASE = 'https://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/'
BISON_FLOW_CITIES = ('TraficCaen', 'TraficDirmc', 'TraficErato', 'TraficGentiane',
                     'TraficHyrondelle', 'TraficLimoges', 'TraficLyon', 'TraficMarius',
                     'TraficRouen')
BORDEAUX_SIGNS_BASE = ('https://datahub.bordeaux-metropole.fr/api/explore/v2.1/'
                      'catalog/datasets/pc_pmv_p')
BORDEAUX_SIGNS_SOURCE = 'https://datahub.bordeaux-metropole.fr/explore/dataset/pc_pmv_p/'
PARIS_WORKS_BASE = ('https://opendata.paris.fr/api/explore/v2.1/catalog/'
                    'datasets/chantiers-perturbants')
PARIS_WORKS_SOURCE = 'https://opendata.paris.fr/explore/dataset/chantiers-perturbants/'
TOULOUSE_WORKS_BASE = ('https://data.toulouse-metropole.fr/api/explore/v2.1/catalog/'
                       'datasets/chantiers-en-cours')
TOULOUSE_WORKS_SOURCE = 'https://data.toulouse-metropole.fr/explore/dataset/chantiers-en-cours/'
FLORENCE_TRAM_WORKS_URL = 'https://datigis.comune.fi.it/json/tram_cantieri_321_now.json'
FLORENCE_TRAM_WORKS_SOURCE = 'https://opendata.comune.fi.it/page_dataset_show?id=tramvia-cantieri-321-odierna'
PARIS_EVENTS_BASE = ('https://opendata.paris.fr/api/explore/v2.1/catalog/'
                     'datasets/circulation_evenement')
PARIS_EVENTS_SOURCE = 'https://opendata.paris.fr/explore/dataset/circulation_evenement/'
BRUSSELS_COUNTERS_SOURCE = 'https://data.mobility.brussels/fr/info/traffic_live_geom/'
VALENCIA_FLOW_SOURCE = 'https://opendata.vlci.valencia.es/en/dataset/estat-transit-temps-real-estado-trafico-tiempo-real'
VALENCIA_FLOW_URL = ('https://geoportal.valencia.es/server/rest/services/OPENDATA/Trafico/'
                     'MapServer/192/query?where=1%3D1&outFields=gid%2Cdenominacion%2Cestado&f=geojson')
VALENCIA_COUNTERS_SOURCE = ('https://opendata.vlci.valencia.es/dataset/'
                            'intensidad-de-los-puntos-de-medida-de-trafico-espiras-electromagneticas')
VALENCIA_COUNTERS_URL = ('https://geoportal.valencia.es/server/rest/services/OPENDATA/Trafico/'
                         'MapServer/208/query?where=1%3D1&outFields=gid%2Cidpm%2Cih%2C'
                         'fecha_actualizacion%2Clast_edited_date&outSR=4326&f=json')
VALENCIA_OCCUPANCY_SOURCE = 'https://opendata.vlci.valencia.es/dataset/ocupacio-via-publica-ocupacion-via-publica'
VALENCIA_OCCUPANCY_URL = ('https://geoportal.valencia.es/server/rest/services/OPENDATA/Trafico/'
                          'MapServer/209/query?where=1%3D1&outFields=id_incidencia%2Ctipo_incidencia%2C'
                          'desc_calle%2Ctipo_afectacion%2Cfecha_inicio%2Cfecha_fin&f=geojson')
BRUSSELS_COUNTERS_URL = ('https://data.mobility.brussels/geoserver/bm_traffic/wfs?'
                          'service=WFS&version=1.1.0&request=GetFeature&'
                          'typeName=bm_traffic:traffic_live_geom&outputFormat=json&srsName=EPSG:4326')
BRUSSELS_EVENTS_SOURCE = 'https://data.mobility.brussels/fr/info/events/'
BRUSSELS_EVENTS_URL = ('https://data.mobility.brussels/geoserver/bm_traffic/wfs?'
                       'service=WFS&version=1.1.0&request=GetFeature&'
                       'typeName=bm_traffic:events&outputFormat=json&srsName=EPSG:4326')
BRUSSELS_SIGNS_SOURCE = 'https://data.mobility.brussels/fr/info/a020de91-8071-443d-95f3-efa34fea4df2/'
BRUSSELS_SIGN_LOCATIONS_URL = ('https://data.mobility.brussels/geoserver/bm_mobiris/wfs?'
                               'service=WFS&version=1.1.0&request=GetFeature&'
                               'typeName=bm_mobiris:pmv&outputFormat=json&srsName=EPSG:4326')
BRUSSELS_SIGN_DISPLAYS_URL = ('https://api.mobility.brussels/datasets/v1/traffic/'
                              'collections/pmv_display/items/?limit=1000&f=json')
ZURICH_ROADWORKS_URL = ('https://maps.zh.ch/wfs/TbaBaustellenZHWFS?SERVICE=WFS&REQUEST=GetFeature'
                        '&VERSION=2.0.0&TYPENAMES=ms:baustellen-uebersicht'
                        '&OUTPUTFORMAT=application%2Fjson&SRSNAME=EPSG:4326')
ZURICH_ROADWORKS_SOURCE = 'https://data.stadt-zuerich.ch/dataset/d991a4a2-32ea-4f7a-93b5-0f31a016d71c'
GENEVA_ROADWORKS_URL = ('https://app2.ge.ch/tergeoservices/rest/services/Hosted/'
                        'INFOMOB_CHANTIER_POINT/FeatureServer/0/query')
GENEVA_ROADWORKS_SOURCE = 'https://sitg.ge.ch/donnees/infomob-chantier-point'
GENEVA_CAMERAS_URL = ('https://app2.ge.ch/tergeoservices/rest/services/Hosted/'
                      'INFOMOB_CAMERA/FeatureServer/0/query')
GENEVA_CAMERAS_SOURCE = 'https://sitg.ge.ch/donnees/infomob-camera'
GENEVA_CAMERA_IMAGE_BASE = 'https://app2.ge.ch/tercameras/CAM_'
VIENNA_ROADWORKS_BASE = ('https://data.wien.gv.at/daten/geo?service=WFS&request=GetFeature'
                         '&version=1.1.0&srsName=EPSG:4326&outputFormat=json&maxFeatures=1000&typeName=')
VIENNA_ROADWORKS_SOURCE = 'https://data.wien.gv.at/daten/geo?service=WFS&request=GetCapabilities'
ZURICH_COUNTERS_URL = ('https://maps.zh.ch/wfs/TBAVMSZHWFS?SERVICE=WFS&REQUEST=GetFeature'
                       '&VERSION=2.0.0&TYPENAMES=ms:verkehrszaehlstellen'
                       '&OUTPUTFORMAT=application%2Fjson&SRSNAME=EPSG:4326')
ZURICH_COUNTER_CONFIG_URL = 'https://vdp.zh.ch/pws/public-service/readCollectorsCfg'
ZURICH_COUNTER_SOURCE = 'https://datenkatalog.statistik.zh.ch/datasets/692@tiefbauamt-kanton-zuerich'
NORWAY_WFS_URL = 'https://ogckart-sn1.atlas.vegvesen.no/datex_3_1/ows'
NORWAY_SOURCE = 'https://www.vegvesen.no/trafikk/kart'
LUXEMBOURG_ROADS_URL = 'https://cita.lu/info_trafic/datex/situationrecord36'
LUXEMBOURG_ROADS_SOURCE = 'https://data.public.lu/en/datasets/cita-evenements-trafic-en-datex-ii-v3-6/'
LUXEMBOURG_CAMERAS_URL = 'https://www.cita.lu/kml/cameras.kml'
LUXEMBOURG_CAMERAS_SOURCE = 'https://data.public.lu/en/datasets/cita-cameras-autoroute/'
LUXEMBOURG_TRAFFIC_BASE = 'https://www.cita.lu/info_trafic/datex/trafficstatus_'
LUXEMBOURG_TRAFFIC_SOURCE = 'https://data.public.lu/en/datasets/cita-donnees-trafic-en-datex-ii/'
LITHUANIA_CAMERA_TABLE_URL = 'https://eismoinfo.lt/eismoinfo-backend/camera-info-table'
LITHUANIA_CAMERA_SOURCE = 'https://eismoinfo.lt/'
LITHUANIA_ROAD_WEATHER_URL = 'https://eismoinfo.lt/eismoinfo-backend/osi-info-table'
ESTONIA_RESTRICTIONS_URL = (
    'https://tarktee.transpordiamet.ee/tarktee/rest/services/'
    'restrictions_traffic/MapServer/0/query'
)
ESTONIA_RESTRICTIONS_SOURCE = 'https://tarktee.transpordiamet.ee/'
ESTONIA_CAMERAS_BASE = 'https://tarktee.transpordiamet.ee/api/v1/datex/'
ESTONIA_CAMERAS_ARCGIS = (
    'https://tarktee.transpordiamet.ee/tarktee/rest/services/road_cameras/MapServer/0/query?'
    'where=1%3D1&outFields=site_name%2Cimage_path&returnGeometry=true&outSR=4326&f=geojson'
)
CZ_NDIC_ROADS_URL = 'https://gis.brno.cz/ags3/rest/services/PUBLIC/uzavirky_ndic/MapServer/0/query'
CZ_NDIC_ROADS_SOURCE = 'https://gis.brno.cz/ost/edas/public/3c5ff253-35f3-4ac0-86ab-db9e06586552'
PRAGUE_ROADS_URL = 'https://opravujeme.to/api/action'
BRNO_WAZE_URL = 'https://gis.brno.cz/ags1/rest/services/Hosted/WazeAlerts/FeatureServer/0/query'
BRNO_WAZE_SOURCE = 'https://gis.brno.cz/ags1/rest/services/Hosted/WazeAlerts/FeatureServer/0'
BRATISLAVA_WORKS_URL = ('https://services8.arcgis.com/pRlN1m0su5BYaFAS/arcgis/rest/services/'
                       'Rozkopavky_CSO_view/FeatureServer/0/query')
BRATISLAVA_WORKS_SOURCE = ('https://bratislava.sk/doprava-a-komunikacie/'
                           'sprava-a-udrzba-komunikacii/obmedzenia-a-poruchy')
LITHUANIA_RESTRICTIONS_URL = ('https://eismoinfo.lt/eismoinfo-backend/'
                              'layer-dynamic-features/EAL?lks=true')
UKPN_DATASET = 'ukpn-live-faults'
NPG_DATASET = 'live-power-cuts-data'
_LOCKS = {'roads': threading.Lock(), 'power': threading.Lock()}
_REFRESH_DONE = {'roads': threading.Event(), 'power': threading.Event()}
_CACHE = {
    'roads': {'until': 0, 'sources': {}, 'source_times': {}, 'errors': [], 'refreshing': False},
    'power': {'until': 0, 'sources': {}, 'source_times': {}, 'errors': [], 'refreshing': False},
}
_STALE_SECONDS = 900
_COLD_WAIT_SECONDS = 12
_BORDEAUX_FLOW_LOCK = threading.Lock()
_BORDEAUX_FLOW_CACHE = {'until': 0, 'data': None}
_BORDEAUX_WORKS_CACHE = {'until': 0, 'metadata': None, 'rows': None, 'lock': threading.Lock()}
_STRASBOURG_FLOW_LOCK = threading.Lock()
_STRASBOURG_FLOW_CACHE = {'until': 0, 'data': None}
_RENNES_FLOW_LOCK = threading.Lock()
_RENNES_FLOW_CACHE = {'until': 0, 'data': None}
_BISON_FLOW_LOCK = threading.Lock()
_BISON_FLOW_CACHE = {'until': 0, 'data': None}
_VALENCIA_FLOW_LOCK = threading.Lock()
_VALENCIA_FLOW_CACHE = {'until': 0, 'data': None}
_BISON_FLOW_TRANSFORMER = Transformer.from_crs('EPSG:2154', 'EPSG:4326', always_xy=True)
_FLORENCE_TRANSFORMER = Transformer.from_crs('EPSG:3003', 'EPSG:4326', always_xy=True)
_PARIS_WORKS_CACHE = {'until': 0, 'rows': [], 'metadata': None, 'lock': threading.Lock()}
_TOULOUSE_WORKS_CACHE = {'until': 0, 'data': None, 'lock': threading.Lock()}
_PRAGUE_ROADS_CACHE = {'until': 0, 'payload': None, 'lock': threading.Lock()}
_SRWR_CACHE = {'until': 0, 'archive': '', 'activities': []}
_NH_ROADWORKS_CACHE = {'until': 0, 'url': '', 'published': '', 'activities': []}
_FRANCE_SENSOR_REFERENCES = {'until': 0, 'points': {}}
_GIPOD_TILE_CACHE = {}
_GIPOD_TILE_LOCKS = {}
_GIPOD_CACHE_LOCK = threading.Lock()
_AUTOBAHN_CACHE = {service: {'until': 0, 'roads': {}, 'lock': threading.Lock()}
                   for service in ('roadworks', 'warning', 'closure')}
_DGT_METADATA_CACHE = {service: {'until': 0, 'root': None, 'lock': threading.Lock()}
                       for service in ('cameras', 'sign_locations')}
_LITHUANIA_CAMERA_CATALOG = {'until': 0, 'rows': [], 'lock': threading.Lock()}
_HONG_KONG_CAMERA_CATALOG = {'until': 0, 'rows': [], 'lock': threading.Lock()}
_SINGAPORE_CAMERA_CATALOG = {'until': 0, 'rows': {}, 'lock': threading.Lock()}
_TAIPEI_WORKS_CACHE = {'until': 0, 'rows': [], 'lock': threading.Lock()}
_TAIPEI_CMS_LOCATIONS = {'until': 0, 'rows': {}, 'lock': threading.Lock()}
_TAIWAN_HIGHWAY_CATALOGS = {name: {'until': 0, 'rows': {}, 'lock': threading.Lock()}
                            for name in ('cameras', 'signs', 'sensors')}
_HONG_KONG_SENSOR_LOCATIONS = {'until': 0, 'rows': {}, 'lock': threading.Lock()}
_HONG_KONG_CAMERA_HEALTH = {'until': {}, 'unavailable': set(), 'lock': threading.Lock()}
_GDYNIA_CATALOGS = {name: {'until': 0, 'rows': [], 'lock': threading.Lock()}
                    for name in ('vms', 'road_segments', 'weather_stations')}
_LITHUANIA_TRANSFORMER = Transformer.from_crs('EPSG:3346', 'EPSG:4326', always_xy=True)
_NIE_TRANSFORMER = Transformer.from_crs('EPSG:29903', 'EPSG:4326', always_xy=True)
_TAIPEI_TRANSFORMER = Transformer.from_crs('EPSG:3826', 'EPSG:4326', always_xy=True)


def _get_json(url, fintraffic=False, extra_headers=None):
    headers = {'User-Agent': 'GlobeView/1.0 (public map feed reader)', 'Accept': 'application/json'}
    if fintraffic:
        headers.update({'Accept-Encoding': 'gzip', 'Digitraffic-User': 'GlobeView/1.0'})
    if extra_headers:
        headers.update(extra_headers)
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


def _get_xml(url, max_bytes=2 * 1024 * 1024, timeout=15, extra_headers=None):
    headers = {'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'application/rss+xml, application/xml'}
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('Road feed exceeded size limit')
    return ET.fromstring(body)


def _sct_xml(filename, now=None):
    """Read SCT's current publication and reject an old cached copy."""
    now = time.time() if now is None else now
    request = urllib.request.Request(SCT_BASE + filename, headers={
        'User-Agent': 'GlobeView/1.0 (public map feed reader)', 'Accept': 'application/xml'})
    with urllib.request.urlopen(request, timeout=15) as response:
        modified = response.headers.get('Last-Modified')
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError('SCT publication exceeded size limit')
    if not modified:
        raise ValueError('SCT publication has no freshness date')
    published = email.utils.parsedate_to_datetime(modified).timestamp()
    if not -300 <= now - published <= 20 * 60:
        raise ValueError('SCT publication is stale')
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


def _get_dgt_xml(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)',
        'Accept': 'application/xml', 'Accept-Encoding': 'gzip'})
    with urllib.request.urlopen(request, timeout=20) as response:
        compressed = response.headers.get('Content-Encoding') == 'gzip'
        body = response.read((2 if compressed else 8) * 1024 * 1024 + 1)
    if len(body) > (2 if compressed else 8) * 1024 * 1024:
        raise ValueError('DGT publication exceeded size limit')
    if compressed:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as archive:
            body = archive.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError('DGT publication exceeded expanded size limit')
    return ET.fromstring(body)


def _dgt_static_xml(service, url):
    cache = _DGT_METADATA_CACHE[service]
    with cache['lock']:
        now = time.time()
        if cache['root'] is not None and now < cache['until']:
            return cache['root']
        root = _get_dgt_xml(url)
        cache.update({'root': root, 'until': now + 3600})
        return root


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
        timestamp = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', timestamp)
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


def _parse_copenhagen_roadworks(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(rows, list) or not 50 <= len(rows) <= 3000
            or not isinstance(payload.get('totalFeatures'), int)
            or payload['totalFeatures'] != len(rows)):
        raise ValueError('Copenhagen roadwork publication is incomplete')
    cases = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        properties = row.get('properties') or {}
        case = str(properties.get('sagsnr') or '')
        work = str(properties.get('gravetype') or '')
        closed = str(properties.get('veje_spaerret_for_biltrafik') or '').casefold() == 'true'
        start = _timestamp(properties.get('projekt_start'))
        end = _timestamp(properties.get('projekt_slut'))
        geometry = row.get('geometry') or {}
        point = _point(geometry)
        if (not re.fullmatch(r'\d{4,12}', case)
                or properties.get('sagstype') != 'Gravetilladelser'
                or ('kørebane' not in work.casefold() and not closed)
                or start is None or end is None or not start <= now <= end
                or not point or not (12.3 <= point[0] <= 12.75 and 55.58 <= point[1] <= 55.83)):
            continue
        priority = {'Point': 3, 'LineString': 2, 'Polygon': 1}.get(geometry.get('type'), 0)
        if not priority or case in cases and cases[case][0] >= priority:
            continue
        title = _clean(properties.get('lokation'), 100) or 'Copenhagen roadwork'
        category = _clean(properties.get('kategori'), 80)
        end_date = dt.datetime.fromtimestamp(end, ZoneInfo('Europe/Copenhagen')).strftime('%-d %b %Y')
        detail = ('Road closure permit' if closed else 'Roadway excavation permit')
        if category:
            detail += f' · {category}'
        detail += f' · scheduled through {end_date}; actual road conditions may differ'
        cases[case] = (priority, _feature(point, {
            'key': f'dk:copenhagen:works:{case}', 'layer': 'construction',
            'title': title, 'detail': detail,
            'source': 'Københavns Kommune · CC BY 4.0',
            'source_url': COPENHAGEN_WORKS_SOURCE,
        }))
    return [feature for _, feature in cases.values()]


def _copenhagen_roadworks():
    query = urllib.parse.urlencode({
        'service': 'WFS', 'version': '1.0.0', 'request': 'GetFeature',
        'typeName': 'k101:raaden_over_vej_events_anonym_aktuelt',
        'outputFormat': 'json', 'SRSNAME': 'EPSG:4326',
        'propertyName': ('sagsnr,sagstype,lokation,projekt_start,projekt_slut,'
                         'kategori,gravetype,veje_spaerret_for_biltrafik,wkb_geometry'),
        'CQL_FILTER': ("sagstype = 'Gravetilladelser' AND "
                       "(gravetype ILIKE '%Kørebane%' OR veje_spaerret_for_biltrafik = 'True')"),
    })
    return _parse_copenhagen_roadworks(_get_json(f'{COPENHAGEN_WORKS_BASE}?{query}'))


def _parse_vejle_roadworks(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') if isinstance(payload, dict) else None
    if (not isinstance(rows, list) or len(rows) > 200
            or payload.get('spatialReference', {}).get('wkid') != 4326
            or payload.get('exceededTransferLimit')):
        raise ValueError('Vejle roadwork publication is incomplete')
    today = dt.datetime.fromtimestamp(now, ZoneInfo('Europe/Copenhagen')).date()
    cases = {}
    for row in rows:
        fields = row.get('attributes') or {}
        case = str(fields.get('serialnumber') or '')
        rings = (row.get('geometry') or {}).get('rings') or []
        ring = rings[0] if rings else []
        point = _point({'coordinates': ring[len(ring) // 2]}) if ring else None
        try:
            start = dt.date.fromisoformat(fields.get('fromdate'))
            end = dt.date.fromisoformat(fields.get('todate'))
        except (TypeError, ValueError):
            continue
        if (not re.fullmatch(r'\d{6,12}', case)
                or fields.get('datestatus') != 'Aktiv'
                or fields.get('externalstatustranslated') != 'Tilladelse'
                or fields.get('traficstatus') != 'Trafikudmeldt'
                or not start <= today <= end or not point
                or not (9.0 <= point[0] <= 10.0 and 55.4 <= point[1] <= 56.1)
                or case in cases):
            continue
        cases[case] = _feature(point, {
            'key': f'dk:vejle:works:{case}', 'layer': 'construction',
            'title': 'Road excavation permit · Vejle',
            'detail': f'Traffic notice published · scheduled through {end:%-d %b %Y}; on-site status unverified',
            'source': 'Vejle Kommune · CC BY 4.0', 'source_url': VEJLE_WORKS_SOURCE,
        })
    return list(cases.values())


def _vejle_roadworks():
    query = urllib.parse.urlencode({
        'where': "datestatus = 'Aktiv' AND traficstatus = 'Trafikudmeldt'",
        'outFields': ('serialnumber,fromdate,todate,externalstatustranslated,'
                      'datestatus,traficstatus'),
        'returnGeometry': 'true', 'outSR': 4326, 'f': 'json',
    })
    error = None
    for url in (VEJLE_WORKS_URL, VEJLE_WORKS_FALLBACK_URL):
        try:
            return _parse_vejle_roadworks(_get_json(f'{url}?{query}'))
        except (OSError, ValueError) as exc:
            error = exc
    raise RuntimeError('Vejle roadwork layers are unavailable') from error


def _parse_zagreb_closures(rows, published, now=None):
    now = time.time() if now is None else now
    if (not isinstance(rows, list) or len(rows) > 1000 or published is None
            or not -300 <= now - published <= 15 * 60):
        raise ValueError('Zagreb road-closure publication is stale or incomplete')
    features = []
    for row in rows:
        if not isinstance(row, dict) or row.get('type') != 'ROAD_CLOSED':
            continue
        start = _timestamp(row.get('expectedStartTime'))
        end = _timestamp(row.get('expectedEndTime'))
        if start is None or end is None or not start <= now <= end:
            continue
        polyline = str(row.get('polyline') or '').split()
        if not 4 <= len(polyline) <= 1000 or len(polyline) % 2:
            continue
        try:
            latitude = float(polyline[(len(polyline) // 4) * 2])
            longitude = float(polyline[(len(polyline) // 4) * 2 + 1])
        except ValueError:
            continue
        if not (15.6 <= longitude <= 16.3 and 45.5 <= latitude <= 46.0):
            continue
        street = _clean(row.get('street'), 90) or 'Road'
        is_work = row.get('subtype') == 'ROAD_CLOSED_CONSTRUCTION'
        direction = ('both directions' if row.get('direction') == 'BOTH_DIRECTIONS'
                     else 'one direction' if row.get('direction') == 'ONE_DIRECTION'
                     else 'direction unspecified')
        end_label = dt.datetime.fromtimestamp(end, ZoneInfo('Europe/Zagreb')).strftime('%-d %b %H:%M')
        identity = hashlib.sha256((str(row.get('street')) + '|' + str(row.get('polyline')) + '|' +
                                   str(row.get('expectedStartTime'))).encode()).hexdigest()[:16]
        features.append(_feature([longitude, latitude], {
            'key': f'hr:zagreb:closure:{identity}',
            'layer': 'construction' if is_work else 'incidents',
            'title': f'{street} · road closure',
            'detail': f'{direction} · expected until {end_label}; check local conditions',
            'source': 'Grad Zagreb · Open Licence', 'source_url': ZAGREB_CLOSURES_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(published, dt.timezone.utc).isoformat(),
        }))
    return features


def _zagreb_closures():
    request = urllib.request.Request(ZAGREB_CLOSURES_URL, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        published = email.utils.parsedate_to_datetime(response.headers['Last-Modified']).timestamp()
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError('Zagreb road-closure publication exceeded size limit')
    return _parse_zagreb_closures(json.loads(body), published)


def _parse_dublin_closures(page, now=None):
    now = time.time() if now is None else now
    windows = {}
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', page, re.S | re.I):
        link = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*current-roadworks__view-more', row, re.I)
        times = re.findall(r'<time\b[^>]*datetime="([^"]+)"', row, re.I)
        if link and len(times) == 2:
            start, end = (_timestamp(value) for value in times)
            if start is not None and end is not None and start <= now <= end:
                windows[html.unescape(link.group(1))] = end

    markers = list(re.finditer(r'<div\b[^>]*class="[^"]*\bgeolocation-location\b[^"]*"[^>]*>',
                               page, re.I))
    features = []
    for index, marker in enumerate(markers):
        opening = marker.group(0)
        body = page[marker.end():markers[index + 1].start() if index + 1 < len(markers)
                    else marker.end() + 3000][:3000]
        lat = re.search(r'\bdata-lat="([^"]+)"', opening)
        lon = re.search(r'\bdata-lng="([^"]+)"', opening)
        link = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*current-roadworks__view-more', body, re.I)
        title = re.search(r'<h2\b[^>]*class="location-title"[^>]*>(.*?)</h2>', body, re.S | re.I)
        if not (lat and lon and link and title):
            continue
        path = html.unescape(link.group(1))
        if not re.fullmatch(r'/travel-and-transport/read-latest-traffic-news/'
                            r'current-road-closures/[a-z0-9-]+', path):
            continue
        end = windows.get(path)
        if end is None:
            continue
        try:
            point = [float(lon.group(1)), float(lat.group(1))]
        except ValueError:
            continue
        if not (-6.5 <= point[0] <= -6.05 and 53.2 <= point[1] <= 53.5):
            continue
        features.append(_feature(point, {
            'key': f'ie:dublin:closure:{path.rsplit("/", 1)[-1]}',
            'layer': 'construction', 'title': _clean(title.group(1), 100),
            'detail': ('Scheduled road closure · published end '
                       f'{dt.datetime.fromtimestamp(end, ZoneInfo("Europe/Dublin")):%-d %b, %-I:%M %p}'),
            'source': 'Dublin City Council · PSI Licence',
            'source_url': 'https://www.dublincity.ie' + path,
        }))
    return features


def _dublin_closures():
    request = urllib.request.Request(DUBLIN_CLOSURES_URL,
                                     headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=15) as response:
        page = response.read(500_001)
    if len(page) > 500_000:
        raise ValueError('Dublin closure publication exceeded size limit')
    return _parse_dublin_closures(page.decode('utf-8'))


def _parse_vitoria_cameras(payload):
    if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection' or not isinstance(payload.get('features'), list):
        shape = (','.join(sorted(str(key) for key in payload)[:4])
                 if isinstance(payload, dict) else type(payload).__name__)
        raise ValueError(f'Vitoria camera catalog is invalid ({shape})')
    features = []
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        camera_id = str(row.get('id') or '')
        if not re.fullmatch(r'CM\d{2}(?:_ROI_[1-4])?', camera_id):
            continue
        props = row.get('properties') or {}
        if not isinstance(props, dict):
            continue
        point = _point(row.get('geometry'))
        if not point or not (-2.8 <= point[0] <= -2.55 and 42.75 <= point[1] <= 42.95):
            continue
        if props.get('commStatusCode') != 'Conectado':
            continue
        expected_url = VITORIA_CAMERAS_URL + '?' + urllib.parse.urlencode({'action': 'get', 'id': camera_id})
        if props.get('imagen') != expected_url:
            continue
        features.append(_feature(point, {
            'key': f'es:vitoria:camera:{camera_id}', 'layer': 'cameras',
            'title': _clean(props.get('nombre'), 110) or f'Vitoria road camera {camera_id}',
            'detail': 'Current traffic camera still',
            'snapshot_url': f'/vitoria-camera/{camera_id}', 'snapshot_refresh_ms': 60000,
            'source': 'Vitoria-Gasteiz City Council · CC BY 4.0',
            'source_url': VITORIA_CAMERAS_SOURCE,
        }))
    return features


def _vitoria_cameras():
    url = VITORIA_CAMERAS_URL + '?action=list&format=GEOJSON'
    try:
        return _parse_vitoria_cameras(_get_json(url))
    except ValueError:
        # The city occasionally responds with a JSON payload other than the
        # camera collection; retry once before dropping the shared snapshot.
        return _parse_vitoria_cameras(_get_json(url))


def vitoria_camera_snapshot(camera_id):
    if not re.fullmatch(r'CM\d{2}(?:_ROI_[1-4])?', str(camera_id)):
        raise ValueError('Invalid Vitoria camera ID')
    url = VITORIA_CAMERAS_URL + '?' + urllib.parse.urlencode({'action': 'get', 'id': camera_id})
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=12) as response:
        if urllib.parse.urlsplit(response.url).hostname != 'www.vitoria-gasteiz.org':
            raise ValueError('Unexpected Vitoria camera redirect')
        image = response.read(1_000_001)
    if len(image) > 1_000_000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('Vitoria camera returned no JPEG still')
    return image, 'image/jpeg'


def _parse_vigo_cameras(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('features'), list):
        raise ValueError('Vigo camera catalog is invalid')
    features = []
    for row in payload['features']:
        if not isinstance(row, dict) or not isinstance(row.get('properties'), dict):
            continue
        props = row['properties']
        camera_id = str(props.get('id') or '')
        if not re.fullmatch(r'\d{1,3}', camera_id):
            continue
        point = _point(row.get('geometry'))
        if not point or not (-8.9 <= point[0] <= -8.55 and 42.1 <= point[1] <= 42.4):
            continue
        expected_url = 'http://camaras.vigo.org/webcam/camv2.php?id=' + camera_id
        if props.get('url') != expected_url:
            continue
        features.append(_feature(point, {
            'key': f'es:vigo:camera:{camera_id}', 'layer': 'cameras',
            'title': _clean(props.get('nombre'), 110) or f'Vigo road camera {camera_id}',
            'detail': 'Current traffic camera still',
            'snapshot_url': f'/vigo-camera/{camera_id}', 'snapshot_refresh_ms': 60000,
            'source': 'Fonte dos datos: Concello de Vigo', 'source_url': VIGO_CAMERAS_SOURCE,
        }))
    return features


_VIGO_CAMERA_HEALTH = {'until': 0, 'unavailable': set(), 'lock': threading.Lock()}


def _vigo_cameras():
    features = _parse_vigo_cameras(_get_json(VIGO_CAMERAS_URL))
    now = time.time()
    with _VIGO_CAMERA_HEALTH['lock']:
        if now >= _VIGO_CAMERA_HEALTH['until']:
            def unavailable(item):
                camera_id = item['properties']['key'].rsplit(':', 1)[-1]
                try:
                    vigo_camera_snapshot(camera_id)
                    return None
                except (OSError, ValueError):
                    return camera_id
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                bad = {camera_id for camera_id in executor.map(unavailable, features) if camera_id}
            _VIGO_CAMERA_HEALTH.update(until=now + 3600, unavailable=bad)
        bad = _VIGO_CAMERA_HEALTH['unavailable']
    return [item for item in features if item['properties']['key'].rsplit(':', 1)[-1] not in bad]


def vigo_camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{1,3}', str(camera_id)):
        raise ValueError('Invalid Vigo camera ID')
    url = VIGO_CAMERA_BASE + '?id=' + camera_id
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=12) as response:
        if urllib.parse.urlsplit(response.url).hostname != 'camaras.vigo.org':
            raise ValueError('Unexpected Vigo camera redirect')
        image = response.read(1_000_001)
        modified = response.headers.get('Last-Modified')
    if len(image) > 1_000_000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('Vigo camera returned no JPEG still')
    if hashlib.sha256(image).hexdigest() == VIGO_UNAVAILABLE_SHA256:
        raise FileNotFoundError('Vigo camera is unavailable')
    if modified:
        try:
            age = time.time() - email.utils.parsedate_to_datetime(modified).timestamp()
        except (TypeError, ValueError):
            raise ValueError('Vigo camera timestamp is invalid')
        if not -300 <= age <= 30 * 60:
            raise FileNotFoundError('Vigo camera still is stale')
    return image, 'image/jpeg'


def _trafficwatch_map_data():
    """One shared read of the DfI public map, including its session CSRF token."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    request = urllib.request.Request(TRAFFICWATCH_SOURCE + '?viewby=mapCheck&d=CCTV_CAMERAS',
                                     headers={'User-Agent': 'GlobeView/1.0 (public map feed reader)'})
    with opener.open(request, timeout=15) as response:
        page = response.read(500_001)
    if len(page) > 500_000:
        raise ValueError('TrafficWatchNI map page exceeded size limit')
    page = page.decode('utf-8')
    token = re.search(r'<meta name="_csrf" content="([A-Za-z0-9_-]{20,200})"', page)
    header = re.search(r'<meta name="_csrf_header" content="([A-Za-z0-9_-]{3,40})"', page)
    if not token or not header or header.group(1) != 'X-CSRF-TOKEN':
        raise ValueError('TrafficWatchNI CSRF token unavailable')
    payload = urllib.parse.urlencode({'selectedTypes': 'CCTV_CAMERAS,ROAD_WORKS,MESSAGE_SIGNS',
                                      'roadworksEndDateFilter': ''}).encode()
    request = urllib.request.Request(TRAFFICWATCH_BASE + 'map/mapData', data=payload,
                                     headers={header.group(1): token.group(1),
                                              'Referer': TRAFFICWATCH_SOURCE,
                                              'User-Agent': 'GlobeView/1.0 (public map feed reader)'})
    with opener.open(request, timeout=15) as response:
        body = response.read(1_500_001)
    if len(body) > 1_500_000:
        raise ValueError('TrafficWatchNI map data exceeded size limit')
    result = json.loads(body)
    data = result.get('mapData') if isinstance(result, dict) else None
    if not isinstance(data, dict) or any(not isinstance(data.get(key), list) for key in
                                          ('CCTV_CAMERAS', 'ROAD_WORKS', 'MESSAGE_SIGNS')):
        raise ValueError('TrafficWatchNI map data is incomplete')
    return data


def _trafficwatch_datetime(value):
    try:
        return dt.datetime.strptime(value, '%a, %d %b %Y %H:%M').replace(
            tzinfo=ZoneInfo('Europe/London')).timestamp()
    except (TypeError, ValueError):
        return None


def _trafficwatch_date(value):
    try:
        return dt.datetime.strptime(value, '%a, %d %b %Y').date()
    except (TypeError, ValueError):
        return None


def _parse_trafficwatch(data, now=None):
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now, ZoneInfo('Europe/London')).date()
    features = []
    for row in data['CCTV_CAMERAS']:
        if not isinstance(row, dict):
            continue
        camera_id = str(row.get('id') or '')
        point = _point({'coordinates': [row.get('longitude'), row.get('latitude')]})
        if not camera_id.isdecimal() or not point or not (54 <= point[1] <= 55.5 and -8.3 <= point[0] <= -5.3):
            continue
        features.append(_feature(point, {
            'key': f'uk:ni:camera:{camera_id}', 'layer': 'cameras',
            'title': _clean(row.get('summary') or 'Traffic camera', 100),
            'detail': 'Latest available still',
            'snapshot_url': f'/northern-ireland-camera/{camera_id}', 'snapshot_refresh_ms': 60000,
            'source': 'DfI Traffic Information and Control Centre · OGL',
            'source_url': TRAFFICWATCH_BASE + f'cameras/static?id={camera_id}',
        }))
    for row in data['ROAD_WORKS']:
        if not isinstance(row, dict):
            continue
        details = row.get('details') or {}
        road_id = str(row.get('id') or '')
        point = _point({'coordinates': [row.get('longitude'), row.get('latitude')]})
        start, end = _trafficwatch_date(details.get('start')), _trafficwatch_date(details.get('end'))
        if not road_id.isdecimal() or not point or start is None or end is None or not start <= today <= end:
            continue
        features.append(_feature(point, {
            'key': f'uk:ni:roadworks:{road_id}', 'layer': 'construction',
            'title': _clean(row.get('summary') or 'Roadworks', 125),
            'detail': _clean(' · '.join(str(value or '') for value in
                                      (details.get('locationSummary'), details.get('description'))), 280),
            'source': 'DfI Traffic Information and Control Centre · OGL',
            'source_url': TRAFFICWATCH_SOURCE,
            'updated_at': row.get('lastUpdated') or '',
        }))
    for row in data['MESSAGE_SIGNS']:
        if not isinstance(row, dict):
            continue
        point = _point({'coordinates': [row.get('longitude'), row.get('latitude')]})
        details = row.get('details') or {}
        message = _clean(details.get('message'), 180)
        published = _trafficwatch_datetime(row.get('lastUpdated'))
        if not point or not (54 <= point[1] <= 55.5 and -8.3 <= point[0] <= -5.3):
            continue  # The same map also carries signs in the Republic of Ireland.
        if not message or message.casefold() == 'sign not set' or published is None or not -300 <= now - published <= 1800:
            continue
        name = _clean(row.get('summary'), 80)
        if not name:
            continue
        features.append(_feature(point, {
            'key': f'uk:ni:sign:{name}:{point[0]:.5f}:{point[1]:.5f}', 'layer': 'signs',
            'title': f'{name} · {message}', 'detail': message,
            'source': 'DfI Traffic Information and Control Centre · OGL',
            'source_url': TRAFFICWATCH_SOURCE,
            'updated_at': row.get('lastUpdated') or '',
        }))
    return features


def _trafficwatch_roads():
    return _parse_trafficwatch(_trafficwatch_map_data())


def northern_ireland_camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{1,5}', str(camera_id)):
        raise ValueError('Invalid TrafficWatchNI camera ID')
    request = urllib.request.Request(TRAFFICWATCH_BASE + f'cameras/cctvMapPopup?id={camera_id}',
                                     headers={'User-Agent': 'GlobeView/1.0 (public camera viewer)'})
    with urllib.request.urlopen(request, timeout=15) as response:
        page = response.read(20_001)
    if len(page) > 20_000:
        raise ValueError('TrafficWatchNI camera page exceeded size limit')
    match = re.search(r'<img[^>]+class="[^"]*cctvImage[^"]*"[^>]+src="(https://cctv\.trafficwatchni\.com/[A-Za-z0-9_-]+\.jpg\?cache=\d+)"',
                      page.decode('utf-8'))
    if not match:
        raise FileNotFoundError('Camera still unavailable')
    image_request = urllib.request.Request(match.group(1), headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': TRAFFICWATCH_SOURCE})
    with urllib.request.urlopen(image_request, timeout=15) as response:
        if urllib.parse.urlsplit(response.url).hostname != 'cctv.trafficwatchni.com':
            raise ValueError('Unexpected camera redirect')
        image = response.read(2_000_001)
    if len(image) > 2_000_000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('TrafficWatchNI returned no JPEG still')
    return image, 'image/jpeg'


def _madrid_xml(filename):
    request = urllib.request.Request(MADRID_BASE + filename,
                                     headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=15) as response:
        modified = response.headers.get('Last-Modified')
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError('Madrid feed exceeded size limit')
    published = email.utils.parsedate_to_datetime(modified).timestamp() if modified else None
    if published is None or not -300 <= time.time() - published <= 25 * 60:
        raise ValueError('Madrid feed is stale')
    return ET.fromstring(body), published


def _madrid_time(value):
    try:
        normalized = re.sub(r'(\.\d{6})\d+', r'\1', str(value))
        return dt.datetime.fromisoformat(normalized).replace(
            tzinfo=ZoneInfo('Europe/Madrid')).timestamp()
    except (TypeError, ValueError):
        return None


def _parse_madrid_incidents(root, published, now=None):
    now = time.time() if now is None else now
    features = []
    for item in root.findall('Incidencia'):
        incident_id = item.findtext('id_incidencia') or ''
        if not incident_id.isdecimal() or item.findtext('incid_estado') not in {'1', '4'}:
            continue
        try:
            point = [float(item.findtext('longitud')), float(item.findtext('latitud'))]
        except (TypeError, ValueError):
            continue
        if not (-3.9 <= point[0] <= -3.45 and 40.25 <= point[1] <= 40.65):
            continue
        start = _madrid_time(item.findtext('fh_inicio'))
        end = _madrid_time(item.findtext('fh_final'))
        if start is None or start > now or (end and end > start and end < now):
            continue
        is_work = item.findtext('es_obras') == 'S'
        layer = 'construction' if is_work else 'incidents'
        title = _clean(item.findtext('nom_tipo_incidencia'), 90) or ('Roadworks' if is_work else 'Road incident')
        features.append(_feature(point, {
            'key': f'es:madrid:incident:{incident_id}', 'layer': layer,
            'title': title, 'detail': _clean(item.findtext('descripcion'), 240),
            'source': 'Madrid City Council · CC BY 4.0', 'source_url': MADRID_SOURCE,
            'updated_at': published,
        }))
    return features


def _madrid_incidents():
    root, published = _madrid_xml('incid_aytomadrid.xml')
    return _parse_madrid_incidents(root, published)


def _parse_valencia_road_occupancy(payload, now=None):
    """Only currently scheduled permits affecting a carriageway or lane."""
    now = time.time() if now is None else now
    rows = payload.get('features') if isinstance(payload, dict) else None
    if (not isinstance(rows, list) or payload.get('type') != 'FeatureCollection'
            or not 1 <= len(rows) < 2000):
        raise ValueError('Valencia street-occupation publication is invalid or incomplete')
    groups = {}
    for row in rows:
        try:
            props = row['properties']
            incident_id = int(props['id_incidencia'])
            kind = str(props['tipo_incidencia']).upper()
            effect = _clean(props['tipo_afectacion'], 120)
            street = _clean(props['desc_calle'], 100)
            start = int(props['fecha_inicio']) / 1000
            end = int(props['fecha_fin']) / 1000
            geometry = row['geometry']
            if geometry['type'] != 'Point':
                continue
            lon, lat = map(float, geometry['coordinates'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (incident_id <= 0 or kind not in {'OBRAS', 'INCIDENCIAS', 'FESTEJOS'}
                or not street or not start <= now <= end
                or not -0.65 <= lon <= -0.1 or not 39.2 <= lat <= 39.65):
            continue
        if not re.search(r'CALZADA|CARRIL|CORTE DE CALLE|ROTONDA|CIRCULACI[OÓ]N|TR[AÁ]FICO', effect, re.I):
            continue
        groups.setdefault(incident_id, []).append((lon, lat, kind, street, effect, end))
    features = []
    for incident_id, points in groups.items():
        center_lon = sum(point[0] for point in points) / len(points)
        center_lat = sum(point[1] for point in points) / len(points)
        lon, lat, kind, street, effect, end = min(
            points, key=lambda point: (point[0] - center_lon) ** 2 + (point[1] - center_lat) ** 2)
        end_date = dt.datetime.fromtimestamp(end, ZoneInfo('Europe/Madrid')).strftime('%d %b %Y')
        label = 'Road works permit' if kind == 'OBRAS' else 'Road occupation permit'
        features.append(_feature([lon, lat], {
            'key': f'es:valencia:road-occupation:{incident_id}', 'layer': 'construction',
            'title': f'{label} · {street}',
            'detail': f'Scheduled through {end_date} · {effect} · Permit, not confirmed active closure',
            'source': 'Valencia City Council · CC BY 4.0',
            'source_url': VALENCIA_OCCUPANCY_SOURCE,
        }))
    return features


def _valencia_road_occupancy():
    return _parse_valencia_road_occupancy(_get_json(VALENCIA_OCCUPANCY_URL))


def _parse_valencia_counters(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(rows, list) or not 100 <= len(rows) <= 2000):
        raise ValueError('Valencia traffic-counter publication is incomplete')
    features = []
    seen = set()
    for row in rows:
        try:
            properties = row['properties']
            station = int(properties['idpm'])
            intensity = int(properties['ih'])
            edited = float(properties['last_edited_date']) / 1000
            point = _point(row['geometry'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (station <= 0 or station in seen or not point
                or not (-0.65 <= point[0] <= -0.1 and 39.2 <= point[1] <= 39.65)
                or not 0 <= intensity <= 10000
                or not -300 <= now - edited <= 20 * 60):
            continue
        seen.add(station)
        features.append(_feature(point, {
            'key': f'es:valencia:counter:{station}', 'layer': 'sensors',
            'title': 'Valencia traffic counter',
            'detail': f'{intensity:,} vehicles/hour · latest published reading',
            'source': 'Ajuntament de València · CC BY 4.0',
            'source_url': VALENCIA_COUNTERS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(edited, dt.timezone.utc).isoformat(),
        }))
    if not features:
        raise ValueError('Valencia traffic-counter readings are stale or empty')
    return features


def _valencia_counters():
    # ArcGIS intermittently serves a two-hour-old replica; accept only fresh
    # records and retry before considering the source unavailable.
    for _ in range(3):
        payload = _get_json(f'{VALENCIA_COUNTERS_URL}&_gv={time.time_ns()}')
        if payload.get('spatialReference', {}).get('wkid') != 4326:
            raise ValueError('Valencia traffic-counter coordinates are not WGS84')
        rows = payload.get('features')
        if not isinstance(rows, list):
            raise ValueError('Valencia traffic-counter publication is invalid')
        geojson = {'type': 'FeatureCollection', 'features': [
            {'properties': row.get('attributes'),
             'geometry': {'type': 'Point', 'coordinates': [
                 (row.get('geometry') or {}).get('x'),
                 (row.get('geometry') or {}).get('y')]}}
            for row in rows if isinstance(row, dict)]}
        try:
            return _parse_valencia_counters(geojson)
        except ValueError as error:
            if 'stale or empty' not in str(error):
                raise
    raise ValueError('Valencia traffic-counter readings are stale or empty')


def _parse_madrid_cameras(root, published):
    namespace = {'k': 'http://earth.google.com/kml/2.2'}
    features = []
    for mark in root.findall('.//k:Placemark', namespace):
        data = {node.get('name'): node.findtext('k:Value', namespaces=namespace)
                for node in mark.findall('.//k:Data', namespace)}
        camera_id = str(data.get('Numero') or '')
        if not re.fullmatch(r'\d{4,6}', camera_id):
            continue
        coordinates = (mark.findtext('.//k:coordinates', namespaces=namespace) or '').strip().split(',')
        try:
            point = [float(coordinates[0]), float(coordinates[1])]
        except (IndexError, ValueError):
            continue
        if not (-3.9 <= point[0] <= -3.45 and 40.25 <= point[1] <= 40.65):
            continue
        features.append(_feature(point, {
            'key': f'es:madrid:camera:{camera_id}', 'layer': 'cameras',
            'title': _clean(data.get('Nombre'), 110) or f'Madrid road camera {camera_id}',
            'detail': 'Latest available still · normally updated every 5 min',
            'snapshot_url': f'/madrid-camera/{camera_id}',
            'snapshot_fallback_url': _madrid_camera_url(camera_id),
            'snapshot_refresh_ms': 300000,
            'source': 'Madrid City Council · CC BY 4.0', 'source_url': MADRID_CAMERAS_SOURCE,
            'updated_at': published,
        }))
    return features


def _madrid_cameras():
    root, published = _madrid_xml('CCTV.kml')
    return _parse_madrid_cameras(root, published)


_MADRID_CAMERA_HEALTH = {}
_MADRID_CAMERA_HEALTH_LOCK = threading.Lock()
_MADRID_CAMERA_AUDIT_LOCK = threading.Lock()
_MADRID_CAMERA_PLACEHOLDER_BYTES = 17803
_MADRID_CAMERA_STILLS = {}


def _jpeg_dimensions(image):
    if not image.startswith(b'\xff\xd8'):
        return None
    offset = 2
    while offset + 9 < len(image):
        if image[offset] != 0xff:
            return None
        while offset < len(image) and image[offset] == 0xff:
            offset += 1
        if offset >= len(image):
            return None
        marker = image[offset]
        offset += 1
        if marker in (0xd8, 0x01) or 0xd0 <= marker <= 0xd7:
            continue
        if marker in (0xd9, 0xda) or offset + 2 > len(image):
            return None
        length = int.from_bytes(image[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(image):
            return None
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            if length < 7:
                return None
            height = int.from_bytes(image[offset + 3:offset + 5], 'big')
            width = int.from_bytes(image[offset + 5:offset + 7], 'big')
            return width, height
        offset += length
    return None


def _madrid_camera_image_available(image):
    dimensions = _jpeg_dimensions(image)
    return dimensions is not None and dimensions[0] >= 600 and dimensions[1] >= 350


def _camera_transport_error(error):
    return isinstance(error, (TimeoutError, urllib.error.URLError)) and (
        not isinstance(error, urllib.error.HTTPError) or error.code in (502, 503, 504))


def _mark_madrid_camera_unavailable(camera_id):
    with _MADRID_CAMERA_HEALTH_LOCK:
        _MADRID_CAMERA_HEALTH[camera_id] = (time.time() + 60, False)
        _MADRID_CAMERA_STILLS.pop(camera_id, None)


def _madrid_camera_url(camera_id):
    return f'https://informo.madrid.es/cameras/Camara{camera_id}.jpg'


def _madrid_camera_headers_available(response, now):
    if urllib.parse.urlsplit(response.url).hostname != 'informo.madrid.es':
        return False
    headers = response.headers
    if headers.get('Content-Type', '').split(';')[0] != 'image/jpeg':
        return False
    if headers.get('Content-Length') == str(_MADRID_CAMERA_PLACEHOLDER_BYTES):
        return False
    modified = headers.get('Last-Modified')
    if not modified:
        return False
    try:
        age = now - email.utils.parsedate_to_datetime(modified).timestamp()
    except (TypeError, ValueError):
        return False
    return -300 <= age <= 30 * 60


def _madrid_unavailable_cameras(cameras):
    camera_ids = {item['properties']['key'].rsplit(':', 1)[-1]
                  for item in cameras if item['properties']['key'].startswith('es:madrid:camera:')}
    if not camera_ids:
        return set()
    with _MADRID_CAMERA_AUDIT_LOCK:
        with _MADRID_CAMERA_HEALTH_LOCK:
            now = time.time()
            pending = [camera_id for camera_id in camera_ids
                       if _MADRID_CAMERA_HEALTH.get(camera_id, (0, False))[0] <= now]

        def available(camera_id):
            request = urllib.request.Request(_madrid_camera_url(camera_id),
                                             headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    if not _madrid_camera_headers_available(response, now):
                        return camera_id, None
                    image = response.read(2_000_001)
                    if len(image) > 2_000_000 or not _madrid_camera_image_available(image):
                        return camera_id, None
                    return camera_id, image
            except (TimeoutError, urllib.error.URLError) as error:
                if _camera_transport_error(error):
                    return camera_id, 'transport-error'
                return camera_id, None
            except (OSError, ValueError):
                return camera_id, None

        if pending:
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(available, pending))
        else:
            results = []
        # Serve the same verified still that made a marker eligible. A camera can
        # switch to the provider's unavailable JPEG between the audit and a click.
        with _MADRID_CAMERA_HEALTH_LOCK:
            expires = time.time() + 300
            for camera_id, image in results:
                if image == 'transport-error':
                    cached = _MADRID_CAMERA_STILLS.get(camera_id)
                    usable = bool(cached and cached[0] + 120 > time.time())
                    _MADRID_CAMERA_HEALTH[camera_id] = (time.time() + 60, usable)
                    continue
                _MADRID_CAMERA_HEALTH[camera_id] = (expires, image is not None)
                if image is not None:
                    _MADRID_CAMERA_STILLS[camera_id] = (expires, image)
                else:
                    _MADRID_CAMERA_STILLS.pop(camera_id, None)
            return {f'es:madrid:camera:{camera_id}' for camera_id in camera_ids
                    if not _MADRID_CAMERA_HEALTH.get(camera_id, (0, False))[1]}


def madrid_camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{4,6}', str(camera_id)):
        raise ValueError('Invalid Madrid camera ID')
    now = time.time()
    with _MADRID_CAMERA_HEALTH_LOCK:
        cached = _MADRID_CAMERA_STILLS.get(camera_id)
        if cached and cached[0] > now:
            return cached[1], 'image/jpeg'
    request = urllib.request.Request(_madrid_camera_url(camera_id), headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    try:
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    if not _madrid_camera_headers_available(response, time.time()):
                        raise FileNotFoundError('Madrid camera still is unavailable or stale')
                    image = response.read(2_000_001)
                break
            except urllib.error.HTTPError as error:
                if error.code not in (502, 503, 504) or attempt:
                    raise
            except (TimeoutError, urllib.error.URLError):
                if attempt:
                    raise
        if len(image) > 2_000_000 or not _madrid_camera_image_available(image):
            raise ValueError('Madrid camera returned no JPEG still')
        return image, 'image/jpeg'
    except (OSError, ValueError) as error:
        # A brief transport failure should not blank a camera that was just
        # verified by the map audit. Never reuse it for a genuine 404, stale
        # origin timestamp, or the authority's unavailable JPEG.
        transient = _camera_transport_error(error)
        if transient and cached and cached[0] + 120 > time.time():
            with _MADRID_CAMERA_HEALTH_LOCK:
                _MADRID_CAMERA_HEALTH[camera_id] = (time.time() + 60, True)
            return cached[1], 'image/jpeg'
        _mark_madrid_camera_unavailable(camera_id)
        raise


def _lyon_camera_url(camera_id):
    if not re.fullmatch(r'CW[A-Z0-9]{3,10}', str(camera_id)):
        raise ValueError('Invalid Lyon camera ID')
    return f'{LYON_CAMERA_BASE}{camera_id}.JPG'


def _parse_lyon_cameras(payload, now=None):
    now = time.time() if now is None else now
    rows = []
    for item in payload.get('features', []):
        props = item.get('properties') or {}
        coordinates = (item.get('geometry') or {}).get('coordinates') or []
        camera_id = str(props.get('numeromaintenance') or '')
        try:
            lon, lat = map(float, coordinates[:2])
            observed = dt.datetime.fromisoformat(props['last_update']).timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        if not (4.65 <= lon <= 5.15 and 45.55 <= lat <= 46.05):
            continue
        if not -300 <= now - observed <= 10 * 60:
            continue
        try:
            expected_url = _lyon_camera_url(camera_id)
        except ValueError:
            continue
        if props.get('url') != expected_url:
            continue
        title = _clean(props.get('libellelong') or props.get('nom'), 100)
        rows.append(_feature([lon, lat], {
            'key': f'fr:lyon:camera:{camera_id}', 'layer': 'cameras',
            'title': title or f'Lyon road camera {camera_id}',
            'detail': 'Latest available still · updated about every minute',
            'snapshot_url': f'/lyon-camera/{camera_id}', 'snapshot_refresh_ms': 60000,
            'source': 'Métropole de Lyon · Licence Ouverte 2.0',
            'source_url': LYON_CAMERAS_SOURCE, 'updated_at': observed,
        }))
    if not rows:
        raise ValueError('Lyon camera catalog contains no fresh camera stills')
    return rows


def _lyon_cameras():
    request = urllib.request.Request(LYON_CAMERAS_URL,
                                     headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = response.read(200_001)
    if len(payload) > 200_000:
        raise ValueError('Lyon camera catalog exceeded size limit')
    rows = _parse_lyon_cameras(json.loads(payload))

    def image_available(row):
        camera_id = row['properties']['key'].rsplit(':', 1)[-1]
        request = urllib.request.Request(_lyon_camera_url(camera_id),
                                         headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return (response.url == _lyon_camera_url(camera_id)
                        and response.headers.get('Content-Type', '').split(';')[0] == 'image/jpeg'
                        and response.read(3) == b'\xff\xd8\xff')
        except (OSError, ValueError):
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        return [row for row, available in zip(rows, executor.map(image_available, rows)) if available]


def _parse_lyon_roadworks(metadata, publication, now=None):
    now = time.time() if now is None else now
    resources = metadata.get('resources') if isinstance(metadata, dict) else None
    geojson = next((item for item in resources or [] if item.get('format') == 'geojson'), None)
    published = geojson.get('last_modified') if geojson else None
    modified = _timestamp(published)
    if modified is None or not -300 <= now - modified <= 2 * 86400:
        raise ValueError('Lyon roadworks publication is stale')
    rows = publication.get('features') if isinstance(publication, dict) else None
    count = publication.get('numberMatched') if isinstance(publication, dict) else None
    if (not isinstance(publication, dict) or publication.get('type') != 'FeatureCollection'
            or not isinstance(rows, list)
            or not isinstance(count, int) or not 0 < count <= 1000
            or publication.get('numberReturned') != count or len(rows) != count):
        raise ValueError('Lyon roadworks publication is incomplete')
    today = dt.datetime.fromtimestamp(now, ZoneInfo('Europe/Paris')).date()
    features = []
    seen = set()
    for row in rows:
        props = row.get('properties') or {}
        work_id = props.get('gid')
        if not isinstance(work_id, int) or work_id <= 0 or work_id in seen:
            continue
        try:
            start = dt.date.fromisoformat(props['debutchantier'][:10])
            end = dt.date.fromisoformat(props['finchantier'][:10])
        except (KeyError, TypeError, ValueError):
            continue
        if (not start <= today <= end or props.get('avancement') != 'Chantier en cours'
                or not str(props.get('typeperturbation') or '').startswith('Circulation ')):
            continue
        point = _point(row.get('geometry'))
        if not point or not 4.6 <= point[0] <= 5.2 or not 45.5 <= point[1] <= 46.1:
            continue
        seen.add(work_id)
        street = _clean(props.get('nom'), 85) or 'Lyon road'
        effect = _clean(props.get('typeperturbation'), 75)
        place = _clean(props.get('commune1'), 65)
        detail = _clean(props.get('nomchantier'), 110)
        features.append(_feature(point, {
            'key': f'fr:lyon:works:{work_id}', 'layer': 'construction',
            'title': f'Roadworks · {street}',
            'detail': ' · '.join(filter(None, (effect, detail, place,
                                               f'Scheduled through {end:%d %b %Y}'))),
            'source': 'Métropole de Lyon · Licence Ouverte 2.0',
            'source_url': LYON_WORKS_SOURCE, 'updated_at': published,
        }))
    return features


def _lyon_roadworks():
    metadata = _get_json(LYON_WORKS_METADATA_URL)
    publication = _get_json(LYON_WORKS_URL)
    return _parse_lyon_roadworks(metadata, publication)


def lyon_camera_snapshot(camera_id):
    url = _lyon_camera_url(camera_id)
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=10) as response:
        if (response.url != url or
                response.headers.get('Content-Type', '').split(';')[0] != 'image/jpeg'):
            raise FileNotFoundError('Lyon camera still is unavailable')
        image = response.read(2_000_001)
    if len(image) > 2_000_000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('Lyon camera returned no JPEG still')
    return image, 'image/jpeg'


_BORDEAUX_ROAD_EFFECTS = (
    'circulation interdite', 'mise en impasse', 'interruption de circulation',
    'neutralisation de voie', 'rétrécissement', 'déviation',
    'limitation de vitesse', 'circulation alternée', 'circulation inversée',
    'sens interdit', 'interdiction de tourner', 'stop et cédez-le-passage',
)


def _parse_bordeaux_roadworks(metadata, rows, now=None):
    now = time.time() if now is None else now
    info = (metadata.get('metas') or {}).get('default') or {}
    published = info.get('data_processed')
    processed = _timestamp(published)
    if processed is None or not -300 <= now - processed <= 48 * 3600:
        raise ValueError('Bordeaux roadworks publication is stale')
    if info.get('license') != 'Licence Ouverte':
        raise ValueError('Bordeaux roadworks licence changed')
    total = info.get('records_count')
    if not isinstance(rows, list) or not isinstance(total, int) or not 0 < total <= 1000 or len(rows) != total:
        raise ValueError('Bordeaux roadworks publication is incomplete')
    today = dt.datetime.fromtimestamp(now, ZoneInfo('Europe/Paris')).date()
    features = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        work_id = row.get('gid')
        point = row.get('geo_point_2d') or {}
        try:
            lon, lat = float(point['lon']), float(point['lat'])
        except (TypeError, KeyError, ValueError):
            continue
        if (not isinstance(work_id, int) or work_id <= 0 or work_id in seen
                or not -0.95 <= lon <= -0.35 or not 44.65 <= lat <= 45):
            continue
        starts = str(row.get('date_debut') or '').split('#')
        ends = str(row.get('date_fin') or '').split('#')
        effects = str(row.get('libelle') or '').split('#')
        if not len(starts) == len(ends) == len(effects):
            continue
        current = []
        for start_value, end_value, effect_value in zip(starts, ends, effects):
            try:
                start = dt.date.fromisoformat(start_value[:10])
                end = dt.date.fromisoformat(end_value[:10])
            except ValueError:
                continue
            if not start <= today <= end:
                continue
            impacts = [effect.strip() for effect in re.split(r'[;/]', effect_value)
                       if any(phrase in effect.casefold() for phrase in _BORDEAUX_ROAD_EFFECTS)]
            if impacts:
                current.append((end, impacts))
        if not current:
            continue
        seen.add(work_id)
        location = _clean(row.get('localisation'), 115)
        city = 'Mérignac' if '(Mérignac)' in location else 'Bordeaux'
        impact = ', '.join(dict.fromkeys(effect for _, impacts in current for effect in impacts))[:125]
        work = _clean(row.get('alias_nature_n1'), 65)
        latest_end = max(end for end, _ in current)
        features.append(_feature([lon, lat], {
            'key': f'fr:bordeaux:works:{work_id}', 'layer': 'construction',
            'title': f'Roadworks · {city}',
            'detail': ' · '.join(filter(None, (impact, work, location,
                                               f'Scheduled through {latest_end:%d %b %Y}'))),
            'source': 'Bordeaux Métropole · Licence Ouverte',
            'source_url': BORDEAUX_WORKS_SOURCE, 'updated_at': published,
        }))
    return features


def _bordeaux_roadworks():
    cache = _BORDEAUX_WORKS_CACHE
    with cache['lock']:
        now = time.time()
        if now >= cache['until'] or cache['rows'] is None:
            metadata = _get_json(BORDEAUX_WORKS_BASE)
            total = ((metadata.get('metas') or {}).get('default') or {}).get('records_count')
            if not isinstance(total, int) or not 0 < total <= 1000:
                raise ValueError('Bordeaux roadworks publication is incomplete')
            rows = []
            for offset in range(0, total, 100):
                query = urllib.parse.urlencode({
                    'select': 'gid,geo_point_2d,date_debut,date_fin,libelle,alias_nature_n1,localisation',
                    'order_by': 'gid', 'limit': 100, 'offset': offset,
                })
                page = _get_json(f'{BORDEAUX_WORKS_BASE}/records?{query}')
                page_rows = page.get('results') if isinstance(page, dict) else None
                if (not isinstance(page, dict) or page.get('total_count') != total or not isinstance(page_rows, list)
                        or len(page_rows) != min(100, total - offset)):
                    raise ValueError('Bordeaux roadworks publication is incomplete')
                rows.extend(page_rows)
            _parse_bordeaux_roadworks(metadata, rows, now)
            cache.update(metadata=metadata, rows=rows, until=now + 900)
        return _parse_bordeaux_roadworks(cache['metadata'], cache['rows'], now)


def _parse_bordeaux_flow(rows, now=None):
    now = time.time() if now is None else now
    features = []
    for row in rows:
        state = str(row.get('etat') or '').upper()
        if state not in {'FLUIDE', 'DENSE', 'EMBOUTEILLE', 'IMPOSSIBLE'}:
            continue
        try:
            updated = dt.datetime.fromisoformat(row['mdate']).timestamp()
            geometry = row['geo_shape']['geometry']
            if geometry['type'] != 'LineString':
                continue
            line = geometry['coordinates']
            gid = int(row['gid'])
            coordinates = [[float(point[0]), float(point[1])] for point in line]
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if not -300 <= now - updated <= 30 * 60:
            continue
        if not (2 <= len(coordinates) <= 150 and all(
                -0.9 <= lon <= -0.3 and 44.6 <= lat <= 45.1 for lon, lat in coordinates)):
            continue
        features.append({'type': 'Feature', 'id': gid,
                         'geometry': {'type': 'LineString', 'coordinates': coordinates},
                         'properties': {'state': state, 'updated_at': updated}})
    if not features:
        raise ValueError('Bordeaux traffic feed contains no current road states')
    return {'type': 'FeatureCollection', 'features': features,
            'source': 'Bordeaux Métropole · Licence Ouverte',
            'source_url': BORDEAUX_FLOW_SOURCE}


def _fetch_bordeaux_flow_page(offset):
    query = urllib.parse.urlencode({'limit': 100, 'offset': offset,
                                    'select': 'geo_shape,gid,etat,mdate'})
    request = urllib.request.Request(f'{BORDEAUX_FLOW_API}?{query}',
                                     headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read(150_001)
    if len(body) > 150_000:
        raise ValueError('Bordeaux traffic page exceeded size limit')
    return json.loads(body)


def _current_bordeaux_flow(data, now, max_age=30 * 60):
    features = [feature for feature in data['features']
                if -300 <= now - feature['properties']['updated_at'] <= max_age]
    if not features:
        raise ValueError('Traffic cache has no current road states')
    return dict(data, features=features)


def bordeaux_flow_snapshot():
    with _BORDEAUX_FLOW_LOCK:
        now = time.time()
        if now < _BORDEAUX_FLOW_CACHE['until'] and _BORDEAUX_FLOW_CACHE['data']:
            return _current_bordeaux_flow(_BORDEAUX_FLOW_CACHE['data'], now)
        try:
            first = _fetch_bordeaux_flow_page(0)
            count = int(first['total_count'])
            if not 1 <= count <= 1000:
                raise ValueError('Bordeaux traffic record count is invalid')
            pages = []
            offsets = list(range(100, count, 100))
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                pages = list(executor.map(_fetch_bordeaux_flow_page, offsets))
            records = list(first['results'])
            for page in pages:
                if int(page['total_count']) != count:
                    raise ValueError('Bordeaux traffic pages changed during fetch')
                records.extend(page['results'])
            if len(records) != count:
                raise ValueError('Bordeaux traffic feed is incomplete')
            data = _parse_bordeaux_flow(records, now)
            _BORDEAUX_FLOW_CACHE.update(until=now + 300, data=data)
            return data
        except (OSError, ValueError, KeyError, TypeError):
            if _BORDEAUX_FLOW_CACHE['data'] and now < _BORDEAUX_FLOW_CACHE['until'] + 600:
                return _current_bordeaux_flow(_BORDEAUX_FLOW_CACHE['data'], now)
            raise


def _parse_strasbourg_flow(metadata, rows, now=None):
    now = time.time() if now is None else now
    processed = (metadata.get('metas') or {}).get('default', {}).get('data_processed')
    published = _timestamp(processed)
    if published is None or not -300 <= now - published <= 15 * 60:
        raise ValueError('Strasbourg traffic publication is stale')
    states = {1: 'FLUIDE', 2: 'DENSE', 3: 'EMBOUTEILLE'}
    features = []
    for row in rows:
        try:
            state = states.get(int(row.get('etat')))
            if not state or str(row.get('name') or '').lower().startswith('cycl'):
                continue
            updated = _timestamp(row.get('ts'))
            if updated is None or not -300 <= now - updated <= 15 * 60:
                continue
            geometry = row['geo_shape']['geometry']
            if geometry['type'] != 'LineString':
                continue
            coordinates = [[float(lon), float(lat)] for lon, lat in geometry['coordinates']]
            segment_id = int(row['ident'])
            if not (2 <= len(coordinates) <= 100 and all(
                    7.5 <= lon <= 8.0 and 48.4 <= lat <= 48.8 for lon, lat in coordinates)):
                continue
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        features.append({'type': 'Feature', 'id': f'strasbourg:{segment_id}',
                         'geometry': {'type': 'LineString', 'coordinates': coordinates},
                         'properties': {'state': state, 'updated_at': updated}})
    if not features:
        raise ValueError('Strasbourg traffic feed contains no current road states')
    return {'type': 'FeatureCollection', 'features': features,
            'source': 'Eurométropole de Strasbourg · Licence Ouverte',
            'source_url': STRASBOURG_FLOW_SOURCE}


def _fetch_strasbourg_flow_page(offset):
    query = urllib.parse.urlencode({'limit': 100, 'offset': offset,
                                    'select': 'ident,name,etat,ts,geo_shape'})
    return _get_json(f'{STRASBOURG_FLOW_BASE}/records?{query}')


def strasbourg_flow_snapshot():
    with _STRASBOURG_FLOW_LOCK:
        now = time.time()
        if now < _STRASBOURG_FLOW_CACHE['until'] and _STRASBOURG_FLOW_CACHE['data']:
            return _current_bordeaux_flow(_STRASBOURG_FLOW_CACHE['data'], now, 15 * 60)
        try:
            metadata = _get_json(STRASBOURG_FLOW_BASE)
            first = _fetch_strasbourg_flow_page(0)
            count = int(first['total_count'])
            if not 1 <= count <= 1000:
                raise ValueError('Strasbourg traffic record count is invalid')
            offsets = list(range(100, count, 100))
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                pages = list(executor.map(_fetch_strasbourg_flow_page, offsets))
            records = list(first['results'])
            for page in pages:
                if int(page['total_count']) != count:
                    raise ValueError('Strasbourg traffic pages changed during fetch')
                records.extend(page['results'])
            if len(records) != count:
                raise ValueError('Strasbourg traffic feed is incomplete')
            data = _parse_strasbourg_flow(metadata, records, now)
            _STRASBOURG_FLOW_CACHE.update(until=now + 180, data=data)
            return data
        except (OSError, ValueError, KeyError, TypeError):
            if _STRASBOURG_FLOW_CACHE['data'] and now < _STRASBOURG_FLOW_CACHE['until'] + 600:
                return _current_bordeaux_flow(_STRASBOURG_FLOW_CACHE['data'], now, 15 * 60)
            raise


def _parse_rennes_flow(metadata, publication, now=None):
    now = time.time() if now is None else now
    processed = (metadata.get('metas') or {}).get('default', {}).get('data_processed')
    published = _timestamp(processed)
    if published is None or not -300 <= now - published <= 15 * 60:
        raise ValueError('Rennes traffic publication is stale')
    rows = publication.get('features')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError('Rennes traffic export is invalid')
    states = {'freeFlow': 'FLUIDE', 'heavy': 'DENSE', 'congested': 'EMBOUTEILLE',
              'impossible': 'IMPOSSIBLE'}
    features = []
    for row in rows:
        try:
            properties = row['properties']
            state = states.get(properties.get('trafficstatus'))
            if not state:
                continue
            updated = _timestamp(properties.get('datetime'))
            if updated is None or not -300 <= now - updated <= 15 * 60:
                continue
            geometry = row['geometry']
            if geometry['type'] != 'LineString':
                continue
            coordinates = [[float(lon), float(lat)] for lon, lat in geometry['coordinates']]
            segment_id = str(properties['predefinedlocationreference'])
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}', segment_id):
                continue
            if not (2 <= len(coordinates) <= 100 and all(
                    -2.0 <= lon <= -1.4 and 47.9 <= lat <= 48.3 for lon, lat in coordinates)):
                continue
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        features.append({'type': 'Feature', 'id': f'rennes:{segment_id}',
                         'geometry': {'type': 'LineString', 'coordinates': coordinates},
                         'properties': {'state': state, 'updated_at': updated}})
    if not features:
        raise ValueError('Rennes traffic feed contains no current road states')
    return {'type': 'FeatureCollection', 'features': features,
            'source': 'Rennes Métropole · ODbL', 'source_url': RENNES_FLOW_SOURCE}


def rennes_flow_snapshot():
    with _RENNES_FLOW_LOCK:
        now = time.time()
        if now < _RENNES_FLOW_CACHE['until'] and _RENNES_FLOW_CACHE['data']:
            return _current_bordeaux_flow(_RENNES_FLOW_CACHE['data'], now, 15 * 60)
        try:
            metadata = _get_json(RENNES_FLOW_BASE)
            query = urllib.parse.urlencode({'select': 'datetime,predefinedlocationreference,trafficstatus,geo_shape'})
            publication = _get_json(f'{RENNES_FLOW_BASE}/exports/geojson?{query}')
            data = _parse_rennes_flow(metadata, publication, now)
            _RENNES_FLOW_CACHE.update(until=now + 180, data=data)
            return data
        except (OSError, ValueError, KeyError, TypeError):
            if _RENNES_FLOW_CACHE['data'] and now < _RENNES_FLOW_CACHE['until'] + 600:
                return _current_bordeaux_flow(_RENNES_FLOW_CACHE['data'], now, 15 * 60)
            raise


def _bison_local_timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo('Europe/Paris'))
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _parse_bison_stations(body):
    rows = list(csv.reader(io.StringIO(body.decode('utf-8-sig')), delimiter=';'))
    expected = ('code_pme', 'source', 'source_2', 'code_insee_commune', 'axe',
                'pr_debut', 'abscisse_debut', 'pr_fin', 'abscisse_fin',
                'sens_gestionnaire', 'sens_cardinal', 'sens_migratoire',
                'sens_giratoire', 'longueur', 'nb_voies', 'x_deb', 'y_deb',
                'x_fin', 'y_fin', 'code_traficolor')
    if not rows or tuple(rows[0]) != expected:
        raise ValueError('Bison Futé station table has changed')
    # The current CSV header includes code_insee_commune but every data row
    # omits that field. Accept a corrected 20-column row as well.
    stations = {}
    for values in rows[1:]:
        if len(values) == 19:
            fields = dict(zip((name for name in expected if name != 'code_insee_commune'), values))
        elif len(values) == 20:
            fields = dict(zip(expected, values))
        else:
            continue
        try:
            points = [float(fields[name]) for name in ('x_deb', 'y_deb', 'x_fin', 'y_fin')]
            length = math.hypot(points[0] - points[2], points[1] - points[3])
            if not 10 <= length <= 3000:
                continue
            start = _BISON_FLOW_TRANSFORMER.transform(points[0], points[1])
            end = _BISON_FLOW_TRANSFORMER.transform(points[2], points[3])
            if not all(-5.5 <= lon <= 9.8 and 41.2 <= lat <= 51.3 for lon, lat in (start, end)):
                continue
            station_id = fields['code_pme']
            if not re.fullmatch(r'[A-Za-z0-9.\-]{2,32}', station_id):
                continue
            stations[station_id] = [list(start), list(end)]
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not stations:
        raise ValueError('Bison Futé station table has no usable locations')
    return stations


def _parse_bison_flow(root, stations, city, now=None):
    now = time.time() if now is None else now
    if root.tag.rsplit('}', 1)[-1] != 'd2LogicalModel':
        raise ValueError('Bison Futé traffic publication is invalid')
    published = _bison_local_timestamp(root.findtext('.//{*}publicationTime'))
    if published is None or not -300 <= now - published <= 20 * 60:
        raise ValueError('Bison Futé traffic publication is stale')
    states = {'freeFlow': 'FLUIDE', 'heavy': 'DENSE',
              'congested': 'EMBOUTEILLE', 'impossible': 'IMPOSSIBLE'}
    features = []
    for site in root.findall('.//{*}siteMeasurements'):
        reference = site.find('.//{*}measurementSiteReference')
        station_id = reference.get('id') if reference is not None else None
        state = states.get(site.findtext('.//{*}trafficStatusValue'))
        updated = _bison_local_timestamp(site.findtext('.//{*}measurementTimeDefault'))
        if not station_id or station_id not in stations or not state or updated is None:
            continue
        if not -300 <= now - updated <= 20 * 60:
            continue
        features.append({'type': 'Feature', 'id': f'bison:{city}:{station_id}',
                         'geometry': {'type': 'LineString', 'coordinates': stations[station_id]},
                         'properties': {'state': state, 'updated_at': updated}})
    return features


def _bison_read(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=12) as response:
        if not response.url.startswith(BISON_FLOW_BASE):
            raise ValueError('Unexpected Bison Futé redirect')
        body = response.read(limit + 1)
        modified = response.headers.get('Last-Modified')
    if len(body) > limit:
        raise ValueError('Bison Futé response exceeded size limit')
    return body, modified


def _bison_city_flow(city, stations, now):
    base = f'{BISON_FLOW_BASE}TRAFICOLOR-DIR/{city}/'
    listing, _ = _bison_read(base, 100_000)
    filenames = re.findall(rb'href="([A-Za-z0-9_]+_DataTRT_\d{8}_\d{6}\.xml)"', listing)
    filenames = [name for name in filenames if name.startswith((city + '_').encode())]
    if not filenames:
        raise ValueError(f'Bison Futé {city} has no traffic publication')
    filename = max(filenames, key=lambda name: name[-19:-4])
    body, _ = _bison_read(base + filename.decode('ascii'), 600_000)
    return _parse_bison_flow(ET.fromstring(body), stations, city, now)


def bison_flow_snapshot():
    now = time.time()
    with _BISON_FLOW_LOCK:
        if now < _BISON_FLOW_CACHE['until'] and _BISON_FLOW_CACHE['data']:
            return _current_bordeaux_flow(_BISON_FLOW_CACHE['data'], now, 20 * 60)
        try:
            table, modified = _bison_read(BISON_FLOW_BASE + 'QTV-DIR/refDir.csv', 250_000)
            if not modified or now - email.utils.parsedate_to_datetime(modified).timestamp() > 30 * 86400:
                raise ValueError('Bison Futé station reference is stale')
            stations = _parse_bison_stations(table)
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                futures = [executor.submit(_bison_city_flow, city, stations, now)
                           for city in BISON_FLOW_CITIES]
                groups = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        groups.extend(future.result())
                    except (OSError, ValueError, ET.ParseError):
                        continue
            if not groups:
                raise ValueError('Bison Futé has no current located traffic states')
            data = {'type': 'FeatureCollection', 'features': groups,
                    'source': 'Bison Futé · Licence Ouverte', 'source_url': BISON_FLOW_SOURCE}
            _BISON_FLOW_CACHE.update(until=now + 300, data=data)
            return data
        except (OSError, ValueError, ET.ParseError):
            if _BISON_FLOW_CACHE['data'] and now < _BISON_FLOW_CACHE['until'] + 600:
                return _current_bordeaux_flow(_BISON_FLOW_CACHE['data'], now, 20 * 60)
            raise


def _parse_valencia_flow(payload):
    rows = payload.get('features') if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection' or not isinstance(rows, list) or not 100 <= len(rows) <= 2000:
        raise ValueError('Valencia traffic publication is incomplete')
    states = {0: 'FLUIDE', 1: 'DENSE', 2: 'EMBOUTEILLE', 3: 'IMPOSSIBLE',
              5: 'FLUIDE', 6: 'DENSE', 7: 'EMBOUTEILLE', 8: 'IMPOSSIBLE'}
    features = []
    for row in rows:
        try:
            props = row['properties']
            state = states.get(int(props['estado']))
            if not state:
                continue  # The authority uses 4 and 9 for "no data".
            segment_id = int(props['gid'])
            geometry = row['geometry']
            if geometry['type'] != 'LineString':
                continue
            coordinates = [[float(lon), float(lat)] for lon, lat in geometry['coordinates']]
            if not 2 <= len(coordinates) <= 150 or not all(
                    -0.65 <= lon <= -0.1 and 39.2 <= lat <= 39.65 for lon, lat in coordinates):
                continue
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        features.append({'type': 'Feature', 'id': f'valencia:{segment_id}',
                         'geometry': {'type': 'LineString', 'coordinates': coordinates},
                         'properties': {'state': state}})
    if not features:
        raise ValueError('Valencia traffic publication has no known road states')
    return {'type': 'FeatureCollection', 'features': features,
            'source': 'Valencia City Council · CC BY 4.0', 'source_url': VALENCIA_FLOW_SOURCE}


def valencia_flow_snapshot():
    with _VALENCIA_FLOW_LOCK:
        now = time.time()
        if now < _VALENCIA_FLOW_CACHE['until'] and _VALENCIA_FLOW_CACHE['data']:
            return _VALENCIA_FLOW_CACHE['data']
        data = _parse_valencia_flow(_get_json(VALENCIA_FLOW_URL))
        _VALENCIA_FLOW_CACHE.update(until=now + 180, data=data)
        return data


def international_traffic_snapshot():
    sources = (('fr_bordeaux', bordeaux_flow_snapshot),
               ('fr_strasbourg', strasbourg_flow_snapshot),
               ('fr_rennes', rennes_flow_snapshot),
               ('fr_bison', bison_flow_snapshot),
               ('es_valencia', valencia_flow_snapshot))
    features, errors, active = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as executor:
        futures = {executor.submit(loader): name for name, loader in sources}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                data = future.result()
                features.extend(data['features'])
                active.append(name)
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(f'{name}: {error}')
    if not active:
        raise ValueError('No current European traffic flow is available')
    return {'type': 'FeatureCollection', 'features': features,
            'sources': active, 'sourceErrors': errors}


def _parse_bordeaux_signs(metadata, publication, now=None):
    now = time.time() if now is None else now
    processed = (metadata.get('metas') or {}).get('default', {}).get('data_processed')
    published = _timestamp(processed)
    if published is None or not -300 <= now - published <= 20 * 60:
        raise ValueError('Bordeaux sign publication is stale')
    rows = publication.get('results')
    if not isinstance(rows, list) or publication.get('total_count') != len(rows) or len(rows) > 100:
        raise ValueError('Bordeaux sign publication is incomplete')
    features = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sign_id = str(row.get('ident') or '')
        point = row.get('geo_point_2d') or {}
        if not re.fullmatch(r'[A-Z0-9]{3,20}', sign_id) or not isinstance(point, dict):
            continue
        try:
            lon, lat = float(point['lon']), float(point['lat'])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-0.9 <= lon <= -0.3 and 44.6 <= lat <= 45.1):
            continue
        pages = [_clean(row.get(key), 90) for key in ('page1', 'page2')]
        pages = [page for page in pages if page]
        if not pages:
            continue
        features.append(_feature([lon, lat], {
            'key': f'fr:bordeaux:sign:{sign_id}', 'layer': 'signs',
            'title': ' / '.join(pages)[:180], 'detail': f'Bordeaux sign {sign_id}',
            'source': 'Bordeaux Métropole · Licence Ouverte',
            'source_url': BORDEAUX_SIGNS_SOURCE, 'updated_at': processed,
        }))
    return features


def _bordeaux_signs():
    metadata = _get_json(BORDEAUX_SIGNS_BASE)
    query = urllib.parse.urlencode({
        'limit': 100, 'select': 'ident,geo_point_2d,page1,page2,mdate'})
    publication = _get_json(f'{BORDEAUX_SIGNS_BASE}/records?{query}')
    return _parse_bordeaux_signs(metadata, publication)


def _parse_paris_roadworks(metadata, rows, now=None):
    now = time.time() if now is None else now
    published = (metadata.get('metas') or {}).get('default', {}).get('data_processed')
    processed = _timestamp(published)
    if processed is None or not -300 <= now - processed <= 14 * 86400:
        raise ValueError('Paris roadworks publication is stale')
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError('Paris roadworks records are invalid')
    today = dt.datetime.fromtimestamp(now, ZoneInfo('Europe/Paris')).date()
    impact_names = {'RESTREINTE': 'Restricted traffic', 'SENS_UNIQUE': 'One-way traffic',
                    'BARRAGE_TOTAL': 'Road closed', 'IMPASSE': 'No through road'}
    features = []
    for row in rows:
        if not isinstance(row, dict) or row.get('statut') not in (2, 4):
            continue
        work_id = str(row.get('identifiant') or '')
        point = row.get('geo_point_2d') or {}
        if not re.fullmatch(r'CP\d{6}', work_id) or not isinstance(point, dict):
            continue
        try:
            lon, lat = float(point['lon']), float(point['lat'])
            start = dt.date.fromisoformat(row['date_debut'])
            end = dt.date.fromisoformat(row['date_fin'])
        except (KeyError, TypeError, ValueError):
            continue
        if not (2.1 <= lon <= 2.55 and 48.75 <= lat <= 49.0 and start <= today <= end):
            continue
        street = _clean(row.get('voie'), 90) or 'Paris street'
        impact = impact_names.get(row.get('impact_circulation'), 'Traffic affected')
        description = (_clean(row.get('impact_circulation_detail'), 140)
                       or _clean(row.get('description'), 140))
        detail = ' · '.join(filter(None, (impact, description, f'Until {end:%d %b %Y}')))
        features.append(_feature([lon, lat], {
            'key': f'fr:paris:works:{work_id}', 'layer': 'construction',
            'title': f'Roadworks · {street}', 'detail': detail,
            'source': 'Ville de Paris · ODbL', 'source_url': PARIS_WORKS_SOURCE,
            'updated_at': published,
        }))
    return features


def _paris_roadworks():
    cache = _PARIS_WORKS_CACHE
    with cache['lock']:
        now = time.time()
        if now >= cache['until'] or cache['metadata'] is None:
            metadata = _get_json(PARIS_WORKS_BASE)
            select = ('identifiant,voie,description,date_debut,date_fin,statut,'
                      'impact_circulation,impact_circulation_detail,geo_point_2d')
            def page(offset):
                query = urllib.parse.urlencode({'limit': 100, 'offset': offset, 'select': select})
                return _get_json(f'{PARIS_WORKS_BASE}/records?{query}')
            first = page(0)
            count = first.get('total_count')
            if not isinstance(count, int) or not 0 <= count <= 500:
                raise ValueError('Paris roadworks record count is invalid')
            rows = list(first.get('results') or [])
            if len(rows) != min(100, count):
                raise ValueError('Paris roadworks first page is incomplete')
            for offset in range(100, count, 100):
                next_page = page(offset)
                if next_page.get('total_count') != count:
                    raise ValueError('Paris roadworks changed during pagination')
                rows.extend(next_page.get('results') or [])
            if len(rows) != count:
                raise ValueError('Paris roadworks publication is incomplete')
            _parse_paris_roadworks(metadata, rows, now)
            cache.update(until=now + 3600, rows=rows, metadata=metadata)
        return _parse_paris_roadworks(cache['metadata'], cache['rows'], now)


def _parse_toulouse_roadworks(metadata, publication, now=None):
    now = time.time() if now is None else now
    meta = (metadata.get('metas') or {}).get('default') or {}
    published = meta.get('data_processed')
    processed = _timestamp(published)
    if processed is None or not -300 <= now - processed <= 2 * 86400:
        raise ValueError('Toulouse roadworks publication is stale')
    rows = publication.get('features')
    count = meta.get('records_count')
    if (not isinstance(rows, list) or not isinstance(count, int) or
            not 0 < count <= 5000 or len(rows) != count):
        raise ValueError('Toulouse roadworks export is incomplete')
    today = dt.datetime.fromtimestamp(now, ZoneInfo('Europe/Paris')).date()
    road_effects = ('rue barrée', 'occupation de 1 file', 'occupation de 2 files',
                    'alternat', 'rue sens unique', 'rue traversée',
                    'occupation de la contre allée', 'occupation de couloir de bus')
    features = []
    for row in rows:
        try:
            props = row['properties']
            work_id = str(props['numero'])
            if not re.fullmatch(r'T\d{2}[A-Z]{3}\d{5}', work_id):
                continue
            start = dt.date.fromisoformat(props['datedebut'][:10])
            end = dt.date.fromisoformat(props['datefin'][:10])
            impact = _clean(props.get('circulation'), 170)
            point = props['geo_point_2d']
            lon, lat = float(point['lon']), float(point['lat'])
            if not (start <= today <= end and any(word in impact.lower() for word in road_effects)
                    and 1.1 <= lon <= 1.8 and 43.3 <= lat <= 43.9):
                continue
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        street = _clean(str(props.get('voie') or '').split('|', 1)[0], 90) or 'Toulouse Métropole road'
        commune = _clean(props.get('commune'), 60)
        features.append(_feature([lon, lat], {
            'key': f'fr:toulouse:works:{work_id}', 'layer': 'construction',
            'title': f'Roadworks · {street}',
            'detail': ' · '.join(filter(None, (impact, commune,
                                               f'Scheduled through {end:%d %b %Y}'))),
            'source': 'Toulouse Métropole · Licence Ouverte',
            'source_url': TOULOUSE_WORKS_SOURCE, 'updated_at': published,
        }))
    return features


def _toulouse_roadworks():
    cache = _TOULOUSE_WORKS_CACHE
    with cache['lock']:
        now = time.time()
        if now >= cache['until'] or cache['data'] is None:
            metadata = _get_json(TOULOUSE_WORKS_BASE)
            publication = _get_json(f'{TOULOUSE_WORKS_BASE}/exports/geojson')
            _parse_toulouse_roadworks(metadata, publication, now)
            cache.update(until=now + 3600, data=(metadata, publication))
        return _parse_toulouse_roadworks(*cache['data'], now)


def _parse_florence_tram_works(publication, now=None):
    now = time.time() if now is None else now
    if (not isinstance(publication, dict) or publication.get('type') != 'FeatureCollection'
            or publication.get('crs', {}).get('properties', {}).get('name')
            != 'urn:ogc:def:crs:EPSG::3003'):
        raise ValueError('Florence tram works publication is invalid')
    published = _timestamp(publication.get('timeStamp'))
    if published is None or not -300 <= now - published <= 36 * 3600:
        raise ValueError('Florence tram works publication is stale')
    rows = publication.get('features')
    if (not isinstance(rows, list) or not 0 < len(rows) <= 1000
            or publication.get('numberReturned') != len(rows)
            or publication.get('totalFeatures') != len(rows)):
        raise ValueError('Florence tram works publication is incomplete')
    today = dt.datetime.fromtimestamp(now, ZoneInfo('Europe/Rome')).date()
    groups = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        props = row.get('properties') or {}
        try:
            start = dt.date.fromisoformat(props['data_ini'])
            end = dt.date.fromisoformat(props['data_fine'])
            if not start <= today <= end:
                continue
            geometry = row['geometry']
            if geometry['type'] != 'MultiPolygon':
                continue
            vertices = [point for polygon in geometry['coordinates'] for ring in polygon
                        for point in ring if isinstance(point, list) and len(point) >= 2]
            if not vertices or len(vertices) > 10000:
                continue
            xs, ys = [point[0] for point in vertices], [point[1] for point in vertices]
            lon, lat = _FLORENCE_TRANSFORMER.transform((min(xs) + max(xs)) / 2,
                                                        (min(ys) + max(ys)) / 2)
            if not (11.1 <= lon <= 11.4 and 43.65 <= lat <= 43.85):
                continue
            street = _clean(props.get('descrizione'), 90)
            stage = _clean(props.get('subcantiere'), 20)
            phase = _clean(props.get('fase'), 20)
            if not street:
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        identity = (street, stage, phase, start, end)
        groups.setdefault(identity, []).append((lon, lat))
    features = []
    for (street, stage, phase, start, end), points in groups.items():
        identity = '|'.join((street, stage, phase, start.isoformat(), end.isoformat()))
        key = hashlib.sha1(identity.encode('utf-8')).hexdigest()[:16]
        detail = [f'Scheduled through {end:%d %b %Y}']
        if stage:
            detail.append(f'Stage {stage}' + (f' · phase {phase}' if phase else ''))
        features.append(_feature([sum(point[0] for point in points) / len(points),
                                  sum(point[1] for point in points) / len(points)], {
            'key': f'it:florence:tram-works:{key}', 'layer': 'construction',
            'title': f'Tram construction · {street}', 'detail': ' · '.join(detail),
            'source': 'Comune di Firenze · CC BY 4.0',
            'source_url': FLORENCE_TRAM_WORKS_SOURCE,
            'updated_at': publication['timeStamp'],
        }))
    return features


def _florence_tram_works():
    return _parse_florence_tram_works(_get_json(FLORENCE_TRAM_WORKS_URL))


def _parse_paris_traffic_events(metadata, rows, now=None):
    now = time.time() if now is None else now
    published = (metadata.get('metas') or {}).get('default', {}).get('data_processed')
    processed = _timestamp(published)
    if processed is None or not -300 <= now - processed <= 20 * 60:
        raise ValueError('Paris traffic events publication is stale')
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('Paris traffic events publication is invalid')
    features = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        event_id = str(row.get('id') or '')
        kind = row.get('type')
        subtype = row.get('subtype')
        if not re.fullmatch(r'[A-Za-z0-9_-]{5,80}', event_id):
            continue
        if kind == 'CONSTRUCTION':
            layer, label, max_days = 'construction', 'Roadworks', 365
        elif kind == 'ROAD_CLOSED' and subtype == 'ROAD_CLOSED_CONSTRUCTION':
            layer, label, max_days = 'construction', 'Road closed for work', 365
        elif kind == 'ROAD_CLOSED' and subtype == 'ROAD_CLOSED_EVENT':
            layer, label, max_days = 'incidents', 'Road closure', 30
        else:
            continue
        start, end = _timestamp(row.get('starttime')), _timestamp(row.get('endtime'))
        if start is None or end is None or not start <= now < end or end - start > max_days * 86400:
            continue
        coordinates = str(row.get('polyline') or '').split()
        if len(coordinates) < 4 or len(coordinates) > 200 or len(coordinates) % 2:
            continue
        try:
            values = [float(value) for value in coordinates]
        except ValueError:
            continue
        points = list(zip(values[::2], values[1::2]))  # Paris publishes latitude, longitude.
        if not all(math.isfinite(lat) and math.isfinite(lon)
                   and 48.75 <= lat <= 49.0 and 2.1 <= lon <= 2.55 for lat, lon in points):
            continue
        mid = (len(points) - 1) // 2
        lat = (points[mid][0] + points[mid + 1][0]) / 2
        lon = (points[mid][1] + points[mid + 1][1]) / 2
        street = _clean(str(row.get('street') or '').replace('_', ' '), 90) or 'Paris street'
        description = _clean(row.get('description'), 180)
        until = dt.datetime.fromtimestamp(end, ZoneInfo('Europe/Paris')).strftime('%d %b %Y, %H:%M')
        features.append(_feature([lon, lat], {
            'key': f'fr:paris:event:{event_id}', 'layer': layer,
            'title': f'{label} · {street}',
            'detail': ' · '.join(filter(None, (description, f'Until {until} Paris time'))),
            'source': 'Ville de Paris · Licence Ouverte', 'source_url': PARIS_EVENTS_SOURCE,
            'updated_at': published,
        }))
    return features


def _paris_traffic_events():
    metadata = _get_json(PARIS_EVENTS_BASE)
    processed = _timestamp((metadata.get('metas') or {}).get('default', {}).get('data_processed'))
    if processed is None or not -300 <= time.time() - processed <= 20 * 60:
        raise ValueError('Paris traffic events publication is stale')
    fields = 'id,starttime,endtime,description,type,subtype,street,polyline'
    rows = []
    total = None
    for offset in range(0, 1000, 100):
        query = urllib.parse.urlencode({'limit': 100, 'offset': offset, 'select': fields})
        page = _get_json(f'{PARIS_EVENTS_BASE}/records?{query}')
        if total is None:
            total = page.get('total_count')
            if not isinstance(total, int) or not 0 <= total <= 1000:
                raise ValueError('Paris traffic events count is invalid')
        if page.get('total_count') != total or not isinstance(page.get('results'), list):
            raise ValueError('Paris traffic events changed during pagination')
        rows.extend(page['results'])
        if len(rows) >= total:
            break
    if len(rows) != total:
        raise ValueError('Paris traffic events publication is incomplete')
    return _parse_paris_traffic_events(metadata, rows)


def _parse_madrid_signs(locations, root, published):
    if root.findtext('HEAD/RESULT') != 'OK':
        raise ValueError('Madrid sign publication failed')
    points = {}
    for row in locations:
        sign_id = row.get('nombre') or ''
        if not re.fullmatch(r'CPMV\d{5}', sign_id):
            continue
        try:
            point = [float(row['longitud']), float(row['latitud'])]
        except (KeyError, TypeError, ValueError):
            continue
        if -3.9 <= point[0] <= -3.45 and 40.25 <= point[1] <= 40.65:
            points[sign_id] = point
    body = root.find('BODY')
    if body is None:
        raise ValueError('Madrid sign publication has no body')
    messages = {}
    for line in body.findall('LINES'):
        sign_id = line.findtext('VMS_ID')
        phase = line.findtext('PHASE_NUMBER') or ''
        try:
            order = int(line.findtext('LINE_NUMBER'))
        except (TypeError, ValueError):
            continue
        message = _clean(line.findtext('LINE'), 100)
        if sign_id in points and message:
            messages.setdefault(sign_id, {}).setdefault(phase, []).append((order, message))
    features = []
    for device in body.findall('DEVICES'):
        sign_id = device.findtext('VMS_ID') or ''
        if sign_id not in points or sign_id not in messages:
            continue
        phases = []
        for phase in sorted(messages[sign_id]):
            lines = [message for _, message in sorted(messages[sign_id][phase])]
            if lines:
                phases.append(' / '.join(lines))
        detail = '  •  '.join(dict.fromkeys(phases))[:280]
        if not detail:
            continue
        features.append(_feature(points[sign_id], {
            'key': f'es:madrid:sign:{sign_id}', 'layer': 'signs',
            'title': _clean(device.findtext('VMS_DESCRIPTION'), 100) or 'Madrid road sign',
            'detail': detail, 'source': 'Madrid City Council · CC BY 4.0',
            'source_url': MADRID_SIGNS_SOURCE, 'updated_at': published,
        }))
    return features


def _madrid_signs():
    root, published = _madrid_xml('pmv_aytomadrid.xml')
    request = urllib.request.Request(MADRID_SIGN_LOCATIONS,
                                     headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(256 * 1024 + 1)
    if len(body) > 256 * 1024:
        raise ValueError('Madrid sign locations exceeded size limit')
    locations = csv.DictReader(io.StringIO(body.decode('utf-8-sig')), delimiter=';')
    return _parse_madrid_signs(locations, root, published)


def _parse_south_tyrol_roads(root, now=None):
    now = time.time() if now is None else now
    publication = root.findtext('.//{*}publicationTime')
    published = _timestamp(publication)
    if published is None or not -300 <= now - published <= 20 * 60:
        raise ValueError('South Tyrol road publication is stale')
    features = []
    for situation in root.findall('.//{*}situation'):
        situation_id = situation.get('id') or ''
        if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', situation_id):
            continue
        record = situation.find('{*}situationRecord')
        if record is None:
            continue
        status = record.findtext('.//{*}validityStatus')
        start = _timestamp(record.findtext('.//{*}overallStartTime'))
        end = _timestamp(record.findtext('.//{*}overallEndTime'))
        if status not in {'active', 'definedByValidityTimeSpec'}:
            continue
        if status == 'definedByValidityTimeSpec' and start is None:
            continue
        if (start is not None and start > now + 300) or (end is not None and end <= now):
            continue
        try:
            lat = float(record.findtext('.//{*}pointByCoordinates/{*}pointCoordinates/{*}latitude'))
            lon = float(record.findtext('.//{*}pointByCoordinates/{*}pointCoordinates/{*}longitude'))
        except (TypeError, ValueError):
            continue
        if not (10 <= lon <= 13 and 46 <= lat <= 48):
            continue
        comments = record.findall('.//{*}generalPublicComment/{*}comment/{*}values/{*}value')
        comment = next((value.text for value in comments if value.get('lang') == 'it' and value.text), '')
        if not comment:
            comment = next((value.text for value in comments if value.text), '')
        detail = _clean(comment, 260)
        record_type = record.get('{http://www.w3.org/2001/XMLSchema-instance}type') or ''
        is_work = record_type == 'MaintenanceWorks' or bool(re.search(r'\b(cantiere|lavori|baustelle|bauarbeiten)\b', detail, re.I))
        layer = 'construction' if is_work else 'incidents'
        features.append(_feature([lon, lat], {
            'key': f'it:south-tyrol:road:{situation_id}', 'layer': layer,
            'title': 'Roadworks' if is_work else 'Road event',
            'detail': detail or _clean(record_type, 80),
            'source': 'Open Data Hub · Province of Bolzano',
            'source_url': SOUTH_TYROL_SOURCE, 'updated_at': publication,
        }))
    return features


def _south_tyrol_roads():
    return _parse_south_tyrol_roads(_get_xml(SOUTH_TYROL_ROADS_URL))


def _parse_a22_announcements(payload, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or not isinstance(payload.get('Items'), list):
        raise ValueError('A22 announcements are invalid')
    if payload.get('TotalResults', 0) > len(payload['Items']):
        raise ValueError('A22 announcements are incomplete')
    features = []
    for item in payload['Items']:
        if not isinstance(item, dict) or item.get('Source') != 'a22' or item.get('Active') is not True:
            continue
        license_info = item.get('LicenseInfo') or {}
        if license_info.get('ClosedData') or license_info.get('License') != 'CC0':
            continue
        tags = item.get('TagIds') or []
        if 'announcement:traffic-event' not in tags:
            continue
        event_id = str(item.get('Id') or '').removeprefix('urn:announcements:a22:')
        if not re.fullmatch(r'[0-9a-f-]{36}', event_id):
            continue
        start = _timestamp(item.get('StartTime'))
        end = _timestamp(item.get('EndTime'))
        changed = _timestamp(item.get('LastChange'))
        if (start is None or start > now + 60 or (end is not None and end <= now)
                or changed is None or changed > now + 300):
            continue
        # The API marks some years-old, open-ended records active. Require a
        # recent source change when no end time constrains their validity.
        if end is None and now - changed > 7 * 86400:
            continue
        position = (item.get('Geo') or {}).get('position') or {}
        try:
            lon, lat = float(position['Longitude']), float(position['Latitude'])
        except (KeyError, TypeError, ValueError):
            continue
        if not (10.5 <= lon <= 12 and 44.3 <= lat <= 47.1):
            continue
        detail = item.get('Detail') or {}
        detail_text = _clean((detail.get('en') or {}).get('BaseText')
                             or (detail.get('it') or {}).get('BaseText'), 280)
        title = _clean(item.get('Shortname'), 110) or 'A22 road event'
        is_work = ('traffic-event:road-work' in tags
                   or bool(re.search(r'\b(lavori|pavimentazione|cantiere)\b', detail_text, re.I)))
        features.append(_feature([lon, lat], {
            'key': f'it:a22:road:{event_id}',
            'layer': 'construction' if is_work else 'incidents',
            'title': title, 'detail': detail_text,
            'source': 'Autostrada del Brennero · Open Data Hub · CC0',
            'source_url': A22_ANNOUNCEMENTS_SOURCE,
            'updated_at': item['LastChange'],
        }))
    return features


def _a22_announcements():
    now = dt.datetime.now(dt.timezone.utc)
    params = urllib.parse.urlencode({
        'source': 'a22', 'begin': now.isoformat(timespec='seconds'),
        'end': (now + dt.timedelta(minutes=1)).isoformat(timespec='seconds'),
        'pagesize': 200,
    })
    return _parse_a22_announcements(_get_json(f'{A22_ANNOUNCEMENTS_URL}?{params}'), now.timestamp())


def _norway_wfs(layer, cql_filter=None):
    params = {'service': 'WFS', 'version': '1.0.0', 'request': 'GetFeature',
              'typeName': f'datex_3_1:{layer}', 'outputFormat': 'application/json',
              'maxFeatures': '2000'}
    if cql_filter:
        params['cql_filter'] = cql_filter
    return _get_json(f'{NORWAY_WFS_URL}?{urllib.parse.urlencode(params)}')


def _norway_timestamp(value):
    return _timestamp(re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', str(value or '')))


def _norway_publication_current(items, now, max_age=30 * 60):
    if not items:
        return True
    published = _norway_timestamp((items[0].get('properties') or {}).get('endJsonTime'))
    if published is None or not -300 <= now - published <= max_age:
        raise ValueError('Norwegian WFS publication is stale or invalid')
    return True


def _parse_norway_roads(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') or []
    _norway_publication_current(rows, now, max_age=75 * 60)
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


def _parse_norway_weather(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') or []
    _norway_publication_current(rows, now)
    features = []
    for item in rows:
        p = item.get('properties') or {}
        point = _point(item.get('geometry'))
        observed = _norway_timestamp(p.get('measurementTime'))
        if not point or not (4 <= point[0] <= 32 and 57 <= point[1] <= 72):
            continue
        if observed is None or not -300 <= now - observed <= 60 * 60:
            continue
        station_id = str(p.get('referenceId') or '')
        if not station_id.isdecimal():
            continue
        readings = []
        for field, label, unit in (('roadSurfaceTemperature', 'Road', '°C'),
                                   ('airTemperature', 'Air', '°C'),
                                   ('windSpeed', 'Wind', ' m/s'),
                                   ('maximumWindSpeed', 'Gust', ' m/s'),
                                   ('precipitationIntensity', 'Precipitation', ' mm/h')):
            try:
                value = float(p.get(field))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and -100 <= value <= 1000:
                readings.append(f'{label} {value:g}{unit}')
        if not readings:
            continue
        features.append(_feature(point, {
            'key': f'no:weather:{station_id}', 'layer': 'sensors',
            'title': _clean(p.get('locationDescription'), 120) or 'Road weather station',
            'detail': ' · '.join(readings),
            'updated_at': dt.datetime.fromtimestamp(observed, dt.timezone.utc).strftime('%d %b %H:%M UTC'),
            'source': 'Statens vegvesen', 'source_url': NORWAY_SOURCE,
        }))
    return features


def _norway_weather():
    return _parse_norway_weather(_norway_wfs('WeatherSimple'))


def _parse_norway_travel_times(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') or []
    _norway_publication_current(rows, now)
    features = []
    for item in rows:
        p = item.get('properties') or {}
        coordinates = (item.get('geometry') or {}).get('coordinates') or []
        if (item.get('geometry') or {}).get('type') != 'LineString' or not coordinates:
            continue
        point = _point({'coordinates': coordinates[len(coordinates) // 2]})
        measured = _norway_timestamp(p.get('validAtTime'))
        if not point or not (4 <= point[0] <= 32 and 57 <= point[1] <= 72):
            continue
        if measured is None or not -300 <= now - measured <= 20 * 60 or p.get('missingData') is not False:
            continue
        station_id = str(p.get('referenceId') or '')
        try:
            actual = float(p.get('actualTime'))
            expected = float(p.get('expectedTime'))
        except (TypeError, ValueError):
            continue
        if not station_id.isdecimal() or not (0 < actual < 86400 and 0 < expected < 86400):
            continue
        detail = f'Reported {actual / 60:.1f} min · expected {expected / 60:.1f} min'
        status = str(p.get('trafficStatusValue') or '')
        if status not in ('', 'unknown'):
            detail += f' · {_clean(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", status).capitalize(), 40)}'
        features.append(_feature(point, {
            'key': f'no:travel:{station_id}', 'layer': 'sensors',
            'title': _clean(p.get('locationDescription'), 120) or 'Road travel time',
            'detail': detail + ' · segment midpoint',
            'updated_at': dt.datetime.fromtimestamp(measured, dt.timezone.utc).strftime('%d %b %H:%M UTC'),
            'source': 'Statens vegvesen', 'source_url': NORWAY_SOURCE,
        }))
    return features


def _norway_travel_times():
    return _parse_norway_travel_times(_norway_wfs('TravelTimeSimple'))


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


def _parse_geneva_roadworks(payload, now=None):
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(payload.get('features'), list)
            or payload.get('exceededTransferLimit') or len(payload['features']) >= 2000):
        raise ValueError('Geneva roadworks publication is invalid or incomplete')
    today = dt.datetime.fromtimestamp(time.time() if now is None else now,
                                      ZoneInfo('Europe/Zurich')).date()
    features = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict) or not isinstance(row.get('properties'), dict):
            continue
        props = row['properties']
        point = _point(row.get('geometry'))
        if not point or not (5.95 <= point[0] <= 6.35 and 46.10 <= point[1] <= 46.38):
            continue
        work_id = str(props.get('globalid') or '').strip('{}').lower()
        if not re.fullmatch(r'[a-f0-9-]{36}', work_id) or work_id in seen:
            continue
        if props.get('date_statut') != 'Ouvert':
            continue
        try:
            start = dt.datetime.strptime(str(props.get('date_debut')), '%Y%m%d').date()
            end = dt.datetime.strptime(str(props.get('date_fin')), '%Y%m%d').date()
        except ValueError:
            continue
        if not start <= today <= end:
            continue
        seen.add(work_id)
        address = _clean(props.get('adresse'), 95)
        disruption = _clean(props.get('perturbation'), 200)
        features.append(_feature(point, {
            'key': f'ch:ge:work:{work_id}', 'layer': 'construction',
            'title': 'Roadworks' + (f' · {address}' if address else ' · Geneva'),
            'detail': ' · '.join(part for part in (
                disruption, f'Scheduled through {end:%d %b %Y}') if part),
            'source': f'SITG · Canton of Geneva · retrieved {today:%d %b %Y}',
            'source_url': GENEVA_ROADWORKS_SOURCE,
        }))
    return features


def _geneva_roadworks():
    query = urllib.parse.urlencode({
        'where': "date_statut = 'Ouvert'", 'outFields':
        'globalid,date_debut,date_fin,date_statut,adresse,perturbation',
        'returnGeometry': 'true', 'outSR': '4326', 'resultRecordCount': '2000',
        'f': 'geojson',
    })
    return _parse_geneva_roadworks(_get_json(GENEVA_ROADWORKS_URL + '?' + query))


def _parse_geneva_cameras(payload, available, now=None):
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(payload.get('features'), list)
            or payload.get('exceededTransferLimit') or len(payload['features']) >= 1000):
        raise ValueError('Geneva camera catalog is invalid or incomplete')
    retrieved = dt.datetime.fromtimestamp(time.time() if now is None else now,
                                          ZoneInfo('Europe/Zurich')).strftime('%d %b %Y')
    features = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict) or not isinstance(row.get('properties'), dict):
            continue
        props = row['properties']
        point = _point(row.get('geometry'))
        if not point or not (5.95 <= point[0] <= 6.35 and 46.10 <= point[1] <= 46.38):
            continue
        url = str(props.get('image_aller') or '')
        match = re.fullmatch(r'https://app2\.ge\.ch/tercameras/CAM_(\d{1,3})\.jpg', url)
        if not match:
            continue
        camera_id = match.group(1)
        if camera_id in seen or camera_id not in available:
            continue
        seen.add(camera_id)
        modified = available[camera_id]
        features.append(_feature(point, {
            'key': f'ch:ge:camera:{camera_id}', 'layer': 'cameras',
            'title': _clean(props.get('nom'), 95) or f'Geneva road camera {camera_id}',
            'snapshot_url': f'/geneva-camera/{camera_id}', 'snapshot_refresh_ms': 60000,
            'source': f'SITG · Canton of Geneva · retrieved {retrieved}',
            'source_url': GENEVA_CAMERAS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(modified, dt.timezone.utc).strftime('%d %b %H:%M UTC'),
        }))
    return features


def _geneva_cameras():
    query = urllib.parse.urlencode({
        'where': '1=1', 'outFields': 'nom,image_aller', 'returnGeometry': 'true',
        'outSR': '4326', 'resultRecordCount': '1000', 'f': 'geojson',
    })
    payload = _get_json(GENEVA_CAMERAS_URL + '?' + query)
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(payload.get('features'), list) or len(payload['features']) >= 1000):
        raise ValueError('Geneva camera catalog is invalid or incomplete')
    ids = set()
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        url = str((row.get('properties') or {}).get('image_aller') or '')
        match = re.fullmatch(r'https://app2\.ge\.ch/tercameras/CAM_(\d{1,3})\.jpg', url)
        if match:
            ids.add(match.group(1))
    now = time.time()

    def probe(camera_id):
        request = urllib.request.Request(GENEVA_CAMERA_IMAGE_BASE + camera_id + '.jpg',
                                         method='HEAD', headers={'User-Agent': 'GlobeView/1.0'})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if (urllib.parse.urlsplit(response.url).hostname != 'app2.ge.ch'
                        or response.headers.get('Content-Type', '').split(';')[0] != 'image/jpeg'):
                    return None
                modified = email.utils.parsedate_to_datetime(response.headers['Last-Modified']).timestamp()
                return (camera_id, modified) if -300 <= now - modified <= 15 * 60 else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        available = dict(result for result in executor.map(probe, ids) if result)
    return _parse_geneva_cameras(payload, available, now)


def geneva_camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{1,3}', str(camera_id)):
        raise ValueError('Invalid Geneva camera ID')
    request = urllib.request.Request(GENEVA_CAMERA_IMAGE_BASE + str(camera_id) + '.jpg',
                                     headers={'User-Agent': 'GlobeView/1.0'})
    with urllib.request.urlopen(request, timeout=12) as response:
        if (urllib.parse.urlsplit(response.url).hostname != 'app2.ge.ch'
                or response.headers.get('Content-Type', '').split(';')[0] != 'image/jpeg'):
            raise ValueError('Unexpected Geneva camera response')
        modified = email.utils.parsedate_to_datetime(response.headers['Last-Modified']).timestamp()
        if not -300 <= time.time() - modified <= 15 * 60:
            raise FileNotFoundError('Geneva camera still is stale')
        image = response.read(1_000_001)
    if len(image) > 1_000_000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('Geneva camera returned no JPEG still')
    return image, 'image/jpeg'


def _parse_vienna_roadworks(publications, now=None):
    today = dt.datetime.fromtimestamp(time.time() if now is None else now,
                                       ZoneInfo('Europe/Vienna')).date()
    features = []
    for kind, data in publications.items():
        if not isinstance(data, dict) or data.get('type') != 'FeatureCollection':
            raise ValueError('Vienna roadworks feed is invalid')
        rows = data.get('features')
        if not isinstance(rows, list):
            raise ValueError('Vienna roadworks feed is invalid')
        if data.get('totalFeatures') != len(rows) or len(rows) >= 1000:
            raise ValueError('Vienna roadworks feed is incomplete')
        for item in rows:
            if not isinstance(item, dict):
                continue
            properties = item.get('properties') or {}
            road_id = str(properties.get('OBJECTID') or '')
            geometry = item.get('geometry') or {}
            if not road_id.isdecimal() or geometry.get('type') != kind:
                continue
            point = _point(geometry)
            if not point or not (16.0 <= point[0] <= 16.7 and 48.0 <= point[1] <= 48.4):
                continue
            try:
                start = dt.date.fromisoformat(str(properties.get('OBJEKT_BEGINN'))[:10])
                end = dt.date.fromisoformat(str(properties.get('OBJEKT_ENDE'))[:10])
            except ValueError:
                continue
            if not start <= today <= end:
                continue
            road = _clean(properties.get('BEZEICHNUNG'), 110)
            if not road:
                continue
            impact = _clean(properties.get('BEHINDERUNGSART'), 75)
            description = _clean(properties.get('PRESSETEXT'), 180)
            features.append(_feature(point, {
                'key': f'at:vienna:roadwork:{kind}:{road_id}', 'layer': 'construction',
                'title': f'Roadworks · {road}',
                'detail': _clean(' · '.join(part for part in (
                    impact, description, f'Scheduled through {end.isoformat()}') if part), 290),
                'source': 'Datenquelle: Stadt Wien – data.wien.gv.at · CC BY 4.0',
                'source_url': VIENNA_ROADWORKS_SOURCE,
            }))
    return features


def _vienna_roadworks():
    publications = {}
    for kind, layer in (('Point', 'BAUSTELLENPKTOGD'), ('LineString', 'BAUSTELLENLINOGD')):
        publications[kind] = _get_json(VIENNA_ROADWORKS_BASE + 'ogdwien:' + layer)
    return _parse_vienna_roadworks(publications)


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
        speed = str(props.get('displayValue') or '').strip()
        if not message and not (sign_type == 'SPEEDLIMIT' and speed.isdigit()):
            continue
        if sign_type == 'SPEEDLIMIT':
            title = f'Variable speed limit · {speed} km/h' if speed.isdigit() else 'Variable speed limit'
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


def _parse_fintraffic_cameras(metadata, observations, now=None):
    now = time.time() if now is None else now
    if (not isinstance(metadata, dict) or metadata.get('type') != 'FeatureCollection'
            or not isinstance(metadata.get('features'), list)
            or not isinstance(observations, dict) or not isinstance(observations.get('stations'), list)):
        raise ValueError('Fintraffic camera catalog is invalid')
    published = _timestamp(observations.get('dataUpdatedTime'))
    if published is None or not -600 <= now - published <= 30 * 60:
        raise ValueError('Fintraffic camera observations are stale')
    recent = {station.get('id'): station for station in observations['stations']
              if isinstance(station, dict) and isinstance(station.get('id'), str)}
    features = []
    for camera in metadata['features']:
        if not isinstance(camera, dict):
            continue
        properties = camera.get('properties') or {}
        if not isinstance(properties, dict):
            continue
        camera_id = str(properties.get('id') or '')
        if (not re.fullmatch(r'C\d{5}', camera_id) or properties.get('collectionStatus') != 'GATHERING'
                or properties.get('state') in {'REPAIR_INTERRUPTED', 'REPAIR_REQUEST_POSTED'}):
            continue
        point = _point(camera.get('geometry'))
        if not point or not (19 <= point[0] <= 32 and 59 <= point[1] <= 71):
            continue
        observation = recent.get(camera_id) or {}
        timestamps = {preset.get('id'): preset.get('measuredTime')
                      for preset in observation.get('presets') or [] if isinstance(preset, dict)}
        choices = []
        for preset in properties.get('presets') or []:
            if not isinstance(preset, dict):
                continue
            preset_id = str(preset.get('id') or '')
            observed_text = timestamps.get(preset_id)
            observed = _timestamp(observed_text)
            if (preset.get('inCollection') is True and re.fullmatch(r'C\d{7}', preset_id)
                    and observed is not None and -600 <= now - observed <= 30 * 60):
                choices.append((observed, preset_id, observed_text))
        if not choices:
            continue
        _, preset_id, observed_text = max(choices)
        name = _clean(properties.get('name'), 90).replace('_', ' ')
        features.append(_feature(point, {
            'key': f'fi:camera:{camera_id}', 'layer': 'cameras',
            'title': name or f'Road weather camera {camera_id}',
            'detail': 'Recent road weather camera still',
            'snapshot_url': f'https://weathercam.digitraffic.fi/{preset_id}.jpg',
            'snapshot_refresh_ms': 600000,
            'source': 'Fintraffic / Digitraffic · CC BY 4.0',
            'source_url': FINTRAFFIC_CAMERAS_SOURCE, 'updated_at': observed_text,
        }))
    return features


def _fintraffic_cameras():
    metadata = _get_json(f'{FINTRAFFIC_BASE}/api/weathercam/v1/stations', fintraffic=True)
    observations = _get_json(f'{FINTRAFFIC_BASE}/api/weathercam/v1/stations/data', fintraffic=True)
    return _parse_fintraffic_cameras(metadata, observations)


_ICELAND_CAMERA_HEALTH = {'until': 0, 'verified': {}}
_ICELAND_CAMERA_HEALTH_LOCK = threading.Lock()
_ICELAND_CAMERA_URL = re.compile(
    r'https://www\.vegagerdin\.is/vgdata/vefmyndavelar/[A-Za-z0-9_-]+\.jpg')


def _iceland_camera_groups(rows):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('Iceland camera catalog is invalid or oversized')
    groups = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        station = str(row.get('Maelist_nr') or '')
        url = str(row.get('Slod') or '')
        point = _point({'coordinates': [row.get('Lengd'), row.get('Breidd')]})
        if (not re.fullmatch(r'\d{1,6}', station) or not _ICELAND_CAMERA_URL.fullmatch(url)
                or not point or not (-25 <= point[0] <= -13 and 63 <= point[1] <= 67.5)):
            continue
        group = groups.setdefault(station, {
            'point': point, 'name': _clean(row.get('Myndavel'), 90) or 'Road camera',
            'road': _clean(row.get('Vegheiti'), 70), 'views': [],
        })
        if url not in {view['url'] for view in group['views']}:
            group['views'].append({'url': url,
                                   'label': _clean(row.get('Skyring'), 90) or f'View {len(group["views"]) + 1}'})
    if not groups:
        raise ValueError('Iceland camera catalog has no usable locations')
    return groups


def _iceland_verified_cameras(groups):
    with _ICELAND_CAMERA_HEALTH_LOCK:
        now = time.time()
        if now < _ICELAND_CAMERA_HEALTH['until']:
            return _ICELAND_CAMERA_HEALTH['verified']

        def verify(entry):
            station, group = entry
            for view in group['views']:
                request = urllib.request.Request(view['url'], method='HEAD', headers={
                    'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
                try:
                    with urllib.request.urlopen(request, timeout=8) as response:
                        if (urllib.parse.urlsplit(response.url).hostname != 'www.vegagerdin.is'
                                or response.headers.get('Content-Type', '').split(';')[0] != 'image/jpeg'
                                or int(response.headers.get('Content-Length') or 0) < 1000):
                            continue
                        modified = email.utils.parsedate_to_datetime(
                            response.headers['Last-Modified']).timestamp()
                        if -300 <= now - modified <= 30 * 60:
                            return station, (view['url'], modified)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            return station, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            verified = dict(executor.map(verify, groups.items()))
        verified = {station: result for station, result in verified.items() if result}
        _ICELAND_CAMERA_HEALTH.update(until=now + 900, verified=verified)
        return verified


def _parse_iceland_cameras(rows, verified=None):
    groups = _iceland_camera_groups(rows)
    features = []
    for station, group in groups.items():
        if verified is not None and station not in verified:
            continue
        primary, modified = verified[station] if verified is not None else (group['views'][0]['url'], None)
        if primary not in {view['url'] for view in group['views']}:
            continue
        views = sorted(group['views'], key=lambda view: view['url'] != primary)
        features.append(_feature(group['point'], {
            'key': f'is:irca:camera:{station}', 'layer': 'cameras',
            'title': group['name'],
            'detail': ' · '.join(filter(None, [group['road'], f'{len(views)} views'])),
            'snapshot_url': primary, 'camera_views': views, 'snapshot_refresh_ms': 600000,
            'source': 'Vegagerðin · CC BY 4.0', 'source_url': ICELAND_CAMERAS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(modified, dt.timezone.utc).strftime('%H:%M UTC') if modified else '',
        }))
    return features


def _iceland_cameras():
    rows = _get_json(ICELAND_CAMERAS_URL)
    groups = _iceland_camera_groups(rows)
    return _parse_iceland_cameras(rows, _iceland_verified_cameras(groups))


def _parse_iceland_roads(root, now=None):
    now = time.time() if now is None else now
    published_text = root.findtext('.//{*}publicationTime')
    published = _timestamp(published_text)
    if published is None or not -300 <= now - published <= 30 * 60:
        raise ValueError('Iceland road publication is stale or invalid')
    categories = {
        'MaintenanceWorks': ('construction', 'Roadworks'),
        'RoadOrCarriagewayOrLaneManagement': ('incidents', 'Road restriction'),
        'GeneralObstruction': ('incidents', 'Road obstruction'),
        'NonWeatherRelatedRoadConditions': ('incidents', 'Road surface hazard'),
        'EnvironmentalObstruction': ('incidents', 'Environmental obstruction'),
        'Accident': ('incidents', 'Crash'),
        'AnimalPresenceObstruction': ('incidents', 'Animals on road'),
        'PoorEnvironmentConditions': ('incidents', 'Hazardous weather'),
    }
    features = []
    for record in root.findall('.//{*}situationRecord'):
        record_id = record.get('id') or ''
        kind = (record.get(_DATEX_TYPE) or '').split(':')[-1]
        category = categories.get(kind)
        if not category or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', record_id):
            continue
        status = record.findtext('.//{*}validityStatus')
        start = _timestamp(record.findtext('.//{*}overallStartTime'))
        end = _timestamp(record.findtext('.//{*}overallEndTime'))
        if status not in {'definedByValidityTimeSpec', 'active'} or start is None or start > now or (end and end <= now):
            continue
        lat = record.findtext('.//{*}coordinatesForDisplay/{*}latitude')
        lon = record.findtext('.//{*}coordinatesForDisplay/{*}longitude')
        point = _point({'coordinates': [lon, lat]})
        if not point or not (-25 <= point[0] <= -13 and 63 <= point[1] <= 67.5):
            continue
        comments = record.findall('.//{*}generalPublicComment/{*}comment/{*}values/{*}value')
        detail = next((_clean(node.text, 280) for node in comments if node.get('lang') == 'en' and node.text), '')
        if not detail:
            detail = next((_clean(node.text, 280) for node in comments if node.text), '')
        layer, title = category
        features.append(_feature(point, {
            'key': f'is:irca:road:{record_id}', 'layer': layer,
            'title': title, 'detail': detail,
            'source': 'Vegagerðin · CC BY 4.0', 'source_url': ICELAND_ROADS_SOURCE,
            'updated_at': _clean(record.findtext('.//{*}situationRecordVersionTime'), 40) or published_text,
        }))
    return features


def _iceland_roads():
    return _parse_iceland_roads(_get_xml(ICELAND_ROADS_URL))


ICELAND_MEASUREMENT_SITES_URL = ('https://datex.vegagerdin.is/measurementsitetablepublication3_1/'
                                 'MeasurementSiteTablePublicationService/pullsnapshotdata')
ICELAND_MEASUREMENTS_URL = ('https://datex.vegagerdin.is/measureddatapublication3_1/'
                            'MeasureDataService/pullsnapshotdata')
_ICELAND_SITES_CACHE = {'until': 0, 'root': None, 'lock': threading.Lock()}


def _iceland_measurement_sites():
    cache = _ICELAND_SITES_CACHE
    with cache['lock']:
        if time.time() >= cache['until'] or cache['root'] is None:
            cache['root'] = _get_xml(ICELAND_MEASUREMENT_SITES_URL)
            cache['until'] = time.time() + 6 * 3600
        return cache['root']


def _parse_iceland_sensors(sites_root, data_root, now=None):
    now = time.time() if now is None else now
    if sites_root.tag != 'messageContainer' or data_root.tag != 'messageContainer':
        raise ValueError('Iceland measurement publication is invalid')
    published_text = data_root.findtext('.//{*}publicationTime')
    published = _timestamp(published_text)
    if published is None or not -300 <= now - published <= 20 * 60:
        raise ValueError('Iceland measurement publication is stale')
    sites = {}
    for site in sites_root.findall('.//{*}measurementSite'):
        site_id = site.get('id') or ''
        if not re.fullmatch(r'IRCA_MP_\d+', site_id):
            continue
        coordinates = site.find('.//{*}coordinatesForDisplay')
        if coordinates is None:
            continue
        try:
            point = [float(coordinates.findtext('{*}longitude')),
                     float(coordinates.findtext('{*}latitude'))]
        except (TypeError, ValueError):
            continue
        if not (-25 <= point[0] <= -13 and 63 <= point[1] <= 67.5):
            continue
        name = _clean(site.findtext('.//{*}measurementSiteName/{*}values/{*}value'), 90)
        sites[site_id] = point, name
    if not sites:
        raise ValueError('Iceland measurement sites are missing')
    features = []
    for observation in data_root.findall('.//{*}siteMeasurements'):
        reference = observation.find('{*}measurementSiteReference')
        site_id = reference.get('id') if reference is not None else None
        if site_id not in sites:
            continue
        measured_text = observation.findtext('{*}measurementTimeDefault/{*}timeValue')
        measured = _timestamp(measured_text)
        if measured is None or not -300 <= now - measured <= 30 * 60:
            continue
        quantities = {row.get('index'): row for row in observation.findall('{*}physicalQuantity')
                      if row.find('.//{*}physicalQuantityFault') is None}
        def reading(index, path, low, high):
            row = quantities.get(index)
            if row is None:
                return None
            try:
                value = float(row.findtext(path))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) and low <= value <= high else None
        flow = reading('9', './/{*}vehicleFlowPer10Minute/{*}vehicleFlowRate', 0, 5000)
        air = reading('4', './/{*}airTemperature/{*}temperature', -80, 60)
        road = reading('5', './/{*}roadSurfaceTemperature/{*}temperature', -80, 90)
        details = []
        if flow is not None:
            details.append(f'{flow:,.0f} {"vehicle" if flow == 1 else "vehicles"} / 10 min')
        if air is not None:
            details.append(f'Air {air:.1f}°C')
        if road is not None:
            details.append(f'Road {road:.1f}°C')
        if not details:
            continue
        point, name = sites[site_id]
        features.append(_feature(point, {
            'key': f'is:irca:sensor:{site_id}', 'layer': 'sensors',
            'title': f'Roadside station · {name}' if name else 'Roadside station',
            'detail': ' · '.join(details),
            'source': 'Vegagerðin · CC BY 4.0', 'source_url': ICELAND_ROADS_SOURCE,
            'updated_at': measured_text,
        }))
    return features


def _iceland_sensors():
    sites = _iceland_measurement_sites()
    data = _get_xml(ICELAND_MEASUREMENTS_URL)
    return _parse_iceland_sensors(sites, data)


ICELAND_ROAD_CONDITIONS_URL = ('https://datex.vegagerdin.is/situationpublication3_1/'
                                'RoadConditionService/pullsnapshotdata')
ICELAND_ROAD_SECTIONS_URL = ('https://datex.vegagerdin.is/predefinedlocationspublication3_1/'
                              'PredefinedLocationsPublicationService/pullsnapshotdata')
_ICELAND_SECTIONS_CACHE = {'until': 0, 'root': None, 'lock': threading.Lock()}


def _iceland_road_sections():
    cache = _ICELAND_SECTIONS_CACHE
    with cache['lock']:
        if time.time() >= cache['until'] or cache['root'] is None:
            cache['root'] = _get_xml(ICELAND_ROAD_SECTIONS_URL, max_bytes=6 * 1024 * 1024)
            cache['until'] = time.time() + 12 * 3600
        return cache['root']


def _parse_iceland_road_conditions(sections_root, data_root, now=None):
    now = time.time() if now is None else now
    if sections_root.tag != 'messageContainer' or data_root.tag != 'messageContainer':
        raise ValueError('Iceland road conditions publication is invalid')
    published_text = data_root.findtext('.//{*}publicationTime')
    published = _timestamp(published_text)
    if published is None or not -300 <= now - published <= 20 * 60:
        raise ValueError('Iceland road conditions publication is stale')
    transformer = Transformer.from_crs('EPSG:3057', 'EPSG:4326', always_xy=True)
    sections = {}
    for section in sections_root.findall('.//{*}predefinedLocationReference'):
        section_id = section.get('id') or ''
        if not re.fullmatch(r'IRCA_PredefinedLocation_segments_\d+', section_id):
            continue
        positions = []
        for line in section.findall('.//{*}gmlLineString'):
            if not (line.get('srsName') or '').endswith('#3057'):
                continue
            try:
                values = [float(value) for value in (line.findtext('{*}posList') or '').split()]
            except ValueError:
                continue
            if len(values) < 4 or len(values) % 2:
                continue
            positions.extend(zip(values[::2], values[1::2]))
        if not positions:
            continue
        x = sum(point[0] for point in positions) / len(positions)
        y = sum(point[1] for point in positions) / len(positions)
        lon, lat = transformer.transform(x, y)
        if not (math.isfinite(lon) and math.isfinite(lat) and -25 <= lon <= -13 and 63 <= lat <= 67.5):
            continue
        name = _clean(section.findtext('{*}predefinedLocationGroupName/{*}values/{*}value')
                      or section.findtext('{*}predefinedLocationName/{*}values/{*}value'), 100)
        sections[section_id] = [lon, lat], name
    if not sections:
        raise ValueError('Iceland road condition sections are missing')
    labels = {'roadClosed': 'Road condition',
              'closedPermanentlyForTheWinter': 'Winter road closure',
              'fog': 'Fog', 'slushOnRoad': 'Slush on road',
              'looseChippings': 'Loose chippings'}
    features = []
    for record in data_root.findall('.//{*}situationRecord'):
        record_id = record.get('id') or ''
        if not re.fullmatch(r'IRCA_ROADCONDITIONS_\d+_\d+', record_id):
            continue
        reference = record.find('.//{*}predefinedLocationReference')
        section_id = reference.get('id') if reference is not None else None
        if section_id not in sections:
            continue
        status = (record.findtext('.//{*}validityStatus') or '').strip()
        start = _timestamp(record.findtext('.//{*}overallStartTime'))
        end = _timestamp(record.findtext('.//{*}overallEndTime'))
        if (status not in {'active', 'definedByValidityTimeSpec'}
                or (status == 'definedByValidityTimeSpec' and start is None)
                or (start and start > now) or (end and end <= now)):
            continue
        condition = next((record.findtext('.//{*}' + key) for key in (
            'roadOrCarriagewayOrLaneManagementType', 'weatherRelatedRoadConditionType',
            'poorEnvironmentType', 'nonWeatherRelatedRoadConditionType')
            if record.findtext('.//{*}' + key) in labels), None)
        if condition is None:
            continue
        comments = record.findall('.//{*}generalPublicComment/{*}comment/{*}values/{*}value')
        comment = next((_clean(item.text, 180) for item in comments if item.get('lang') == 'en'), '')
        if not comment:
            comment = next((_clean(item.text, 180) for item in comments), '')
        label = comment if condition == 'roadClosed' and comment in {
            'Mountain vehicles', 'Easily passable', 'Impassable'} else labels[condition]
        point, name = sections[section_id]
        features.append(_feature(point, {
            'key': f'is:irca:condition:{record_id}', 'layer': 'incidents',
            'title': f'{label} · {name}' if name else label,
            'detail': ' · '.join(part for part in (
                comment if comment != label else '', 'Approximate section location') if part),
            'source': 'Vegagerðin · CC BY 4.0', 'source_url': ICELAND_ROADS_SOURCE,
            'updated_at': published_text,
        }))
    return features


def _iceland_road_conditions():
    sections = _iceland_road_sections()
    conditions = _get_xml(ICELAND_ROAD_CONDITIONS_URL)
    return _parse_iceland_road_conditions(sections, conditions)


def _parse_fintraffic_sensors(kind, metadata, observations, now=None):
    if kind not in {'tms', 'weather'}:
        raise ValueError('Unknown Fintraffic sensor type')
    now = time.time() if now is None else now
    if (not isinstance(metadata, dict) or metadata.get('type') != 'FeatureCollection'
            or not isinstance(metadata.get('features'), list)
            or not isinstance(observations, dict) or not isinstance(observations.get('stations'), list)):
        raise ValueError('Fintraffic sensor feed is invalid')
    published = _timestamp(observations.get('dataUpdatedTime'))
    if published is None or not -600 <= now - published <= 20 * 60:
        raise ValueError('Fintraffic sensor publication is stale')
    recent = {station.get('id'): station for station in observations['stations']
              if isinstance(station, dict) and isinstance(station.get('id'), int)}
    result = []
    for station in metadata['features']:
        if not isinstance(station, dict):
            continue
        props = station.get('properties') or {}
        if not isinstance(props, dict):
            continue
        station_id = props.get('id')
        if (not isinstance(station_id, int) or props.get('collectionStatus') != 'GATHERING'
                or props.get('state') in {'REPAIR_INTERRUPTED', 'REPAIR_REQUEST_POSTED', 'FAULT_DOUBT'}):
            continue
        point = _point(station.get('geometry'))
        if not point or not (19 <= point[0] <= 32 and 59 <= point[1] <= 71):
            continue
        observation = recent.get(station_id) or {}
        values = {}
        for sensor in observation.get('sensorValues') or []:
            if not isinstance(sensor, dict):
                continue
            sensor_id = sensor.get('id')
            measured = _timestamp(sensor.get('measuredTime'))
            if measured is None or not -600 <= now - measured <= 15 * 60:
                continue
            try:
                value = float(sensor.get('value'))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values[sensor_id] = (value, measured, sensor['measuredTime'])
        detail = []
        used_measurements = []
        if kind == 'tms':
            for direction, speed_id, flow_id in ((1, 5122, 5116), (2, 5125, 5119)):
                measurements = []
                speed = values.get(speed_id)
                flow = values.get(flow_id)
                if speed and 0 <= speed[0] <= 200:
                    measurements.append(f'{speed[0]:.0f} km/h')
                    used_measurements.append(speed)
                if flow and 0 <= flow[0] <= 10000:
                    measurements.append(f'{flow[0]:,.0f} veh/h')
                    used_measurements.append(flow)
                if measurements:
                    detail.append(f'Direction {direction}: {", ".join(measurements)}')
            title = 'Traffic counter'
            source_url = 'https://www.digitraffic.fi/en/road-traffic/lam/'
        else:
            for sensor_id, label, low, high in ((1, 'Air', -60, 70), (3, 'Road', -60, 80)):
                measurement = values.get(sensor_id)
                if measurement and low <= measurement[0] <= high:
                    detail.append(f'{label} {measurement[0]:.1f}°C')
                    used_measurements.append(measurement)
            if not any(item.startswith('Road ') for item in detail):
                road = values.get(5)
                if road and -60 <= road[0] <= 80:
                    detail.append(f'Road {road[0]:.1f}°C')
                    used_measurements.append(road)
            title = 'Road weather station'
            source_url = FINTRAFFIC_CAMERAS_SOURCE
        if not detail:
            continue
        name = _clean(props.get('name'), 90).replace('_', ' ')
        measured_text = max(used_measurements, key=lambda item: item[1])[2]
        result.append(_feature(point, {
            'key': f'fi:{kind}:{station_id}', 'layer': 'sensors',
            'title': f'{title} · {name}' if name else f'{title} {station_id}',
            'detail': ' · '.join(detail),
            'source': 'Fintraffic / Digitraffic · CC BY 4.0',
            'source_url': source_url, 'updated_at': measured_text,
        }))
    return result


def _fintraffic_sensors(kind):
    metadata = _get_json(f'{FINTRAFFIC_BASE}/api/{kind}/v1/stations', fintraffic=True)
    observations = _get_json(f'{FINTRAFFIC_BASE}/api/{kind}/v1/stations/data', fintraffic=True)
    return _parse_fintraffic_sensors(kind, metadata, observations)


_TFL_CAMERA_HEALTH = {}
_TFL_CAMERA_HEALTH_LOCK = threading.Lock()


def _tfl_camera_url(camera_id):
    if not re.fullmatch(r'\d{5}\.\d{5}', str(camera_id)):
        raise ValueError('Invalid TfL camera ID')
    return f'{TFL_CAMERA_BASE}{camera_id}.jpg'


def _parse_tfl_cameras(payload):
    if not isinstance(payload, list):
        raise ValueError('TfL camera catalog is invalid')
    features = []
    seen = set()
    for row in payload:
        if not isinstance(row, dict) or row.get('placeType') != 'JamCam':
            continue
        match = re.fullmatch(r'JamCams_(\d{5}\.\d{5})', str(row.get('id') or ''))
        if not match or match.group(1) in seen:
            continue
        camera_id = match.group(1)
        try:
            point = [float(row['lon']), float(row['lat'])]
        except (KeyError, TypeError, ValueError):
            continue
        if not (-0.55 <= point[0] <= 0.4 and 51.25 <= point[1] <= 51.75):
            continue
        props = {item.get('key'): item.get('value') for item in (row.get('additionalProperties') or [])
                 if isinstance(item, dict)}
        if props.get('available') != 'true' or props.get('imageUrl') != _tfl_camera_url(camera_id):
            continue
        seen.add(camera_id)
        features.append(_feature(point, {
            'key': f'uk:tfl:camera:{camera_id}', 'layer': 'cameras',
            'title': _clean(row.get('commonName'), 100) or 'London traffic camera',
            'detail': 'Recent camera still · normally updated every few minutes',
            'snapshot_url': f'/tfl-camera/{camera_id}', 'snapshot_refresh_ms': 120000,
            'source': 'Transport for London', 'source_url': TFL_CAMERA_SOURCE,
        }))
    if not features:
        raise ValueError('TfL camera catalog has no usable locations')
    return features


def _tfl_cameras():
    return _parse_tfl_cameras(_get_json(TFL_CAMERAS_URL))


def _tfl_camera_headers_available(response, camera_id, now):
    url = urllib.parse.urlsplit(response.url)
    if (url.scheme != 'https' or url.hostname != 's3-eu-west-1.amazonaws.com'
            or url.path != f'/jamcams.tfl.gov.uk/{camera_id}.jpg'):
        return False
    headers = response.headers
    if headers.get('Content-Type', '').split(';')[0] != 'image/jpeg':
        return False
    try:
        size = int(headers.get('Content-Length') or 0)
        modified = email.utils.parsedate_to_datetime(headers['Last-Modified']).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    return 5000 <= size <= 300000 and -300 <= now - modified <= 14 * 60


def _tfl_unavailable_cameras(features):
    cameras = {item['properties']['snapshot_url'].rsplit('/', 1)[-1]
               for item in features if item['properties']['key'].startswith('uk:tfl:camera:')}
    if not cameras:
        return set()
    now = time.time()
    def available(camera_id):
        request = urllib.request.Request(_tfl_camera_url(camera_id), method='HEAD', headers={
            'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return _tfl_camera_headers_available(response, camera_id, now)
        except (OSError, ValueError):
            return False

    with _TFL_CAMERA_HEALTH_LOCK:
        pending = [camera_id for camera_id in cameras
                   if _TFL_CAMERA_HEALTH.get(camera_id, (0, False))[0] <= now]
        if pending:
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                checked = dict(zip(pending, executor.map(available, pending)))
            for camera_id, good in checked.items():
                _TFL_CAMERA_HEALTH[camera_id] = (now + (120 if good else 60), good)
        return {f'uk:tfl:camera:{camera_id}' for camera_id in cameras
                if not _TFL_CAMERA_HEALTH.get(camera_id, (0, False))[1]}


def tfl_camera_snapshot(camera_id):
    request = urllib.request.Request(_tfl_camera_url(camera_id), headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if not _tfl_camera_headers_available(response, camera_id, time.time()):
                raise FileNotFoundError('TfL camera still is unavailable or stale')
            image = response.read(300001)
        if len(image) > 300000 or not _camera_has_visible_scene(image):
            raise FileNotFoundError('TfL camera still has no visible scene')
        with _TFL_CAMERA_HEALTH_LOCK:
            _TFL_CAMERA_HEALTH[camera_id] = (time.time() + 120, True)
        return image, 'image/jpeg'
    except (OSError, ValueError):
        with _TFL_CAMERA_HEALTH_LOCK:
            _TFL_CAMERA_HEALTH[camera_id] = (time.time() + 60, False)
        raise


def _tfl_disruptions():
    data = _get_json(TFL_URL)
    if isinstance(data, dict) and isinstance(data.get('features'), list):
        # TfL sometimes serves a GeoJSON catalog rather than RoadDisruption records.
        # Resolve its IDs in batches so works keep their category and current update.
        rows = []
        catalog = data['features']
        for offset in range(0, min(len(catalog), 200), 40):
            batch = catalog[offset:offset + 40]
            ids = [str(item.get('id')) for item in batch if isinstance(item, dict)
                   and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(item.get('id') or ''))]
            details = None
            if ids:
                try:
                    details = _get_json(f'{TFL_URL}/{",".join(ids)}')
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            if isinstance(details, list):
                rows.extend(item for item in details if isinstance(item, dict))
            else:
                # Keep the geolocated active markers if the detail lookup fails.
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    rows.append({**(item.get('properties') or {}), 'id': item.get('id'),
                                 'geography': item.get('geometry'), 'status': 'Active'})
        for item in catalog[200:]:
            if isinstance(item, dict):
                rows.append({**(item.get('properties') or {}), 'id': item.get('id'),
                             'geography': item.get('geometry'), 'status': 'Active'})
        data = rows
    if not isinstance(data, list):
        raise ValueError('Unexpected TfL disruption response')
    features = []
    for item in data:
        if not isinstance(item, dict):
            continue
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


def _parse_national_highways_roadworks(root, published):
    if root.tag.rsplit('}', 1)[-1] != 'Report':
        raise ValueError('National Highways roadworks publication is invalid')
    transformer = Transformer.from_crs('EPSG:27700', 'EPSG:4326', always_xy=True)
    activities = []
    for work in root.findall('.//{*}HE_PLANNED_WORKS'):
        event_id = work.get('NEW_EVENT_NUMBER', '')
        position = work.find('.//{*}EASTNORTH[@CENTRE_EASTING]')
        if (not re.fullmatch(r'\d{8}-\d{3}', event_id)
                or work.get('STATUS') != 'Published' or position is None):
            continue
        try:
            start = dt.datetime.strptime(work.get('SDATE', ''), '%d-%b-%Y %H:%M').replace(
                tzinfo=ZoneInfo('Europe/London')).timestamp()
            end = dt.datetime.strptime(work.get('EDATE', ''), '%d-%b-%Y %H:%M').replace(
                tzinfo=ZoneInfo('Europe/London')).timestamp()
            east = float(position.get('CENTRE_EASTING'))
            north = float(position.get('CENTRE_NORTHING'))
            if not (0 <= east <= 700_000 and 0 <= north <= 1_300_000 and end > start):
                continue
            lon, lat = transformer.transform(east, north)
        except (TypeError, ValueError):
            continue
        if not (-9 <= lon <= 3 and 49 <= lat <= 59):
            continue
        road = work.find('.//{*}ROAD')
        road_name = _clean(road.get('ROAD_NUMBER') if road is not None else '', 20)
        description = _clean(work.get('DESCRIPTION'), 220)
        delay = _clean(work.get('EXPDEL'), 50)
        detail = ' · '.join(part for part in ('Scheduled, not confirmed live', description,
                                              f'Expected delay: {delay}' if delay else '') if part)
        activities.append((start, end, _feature([lon, lat], {
            'key': f'uk:nh:roadworks:{event_id}', 'layer': 'construction',
            'title': f'Scheduled roadworks · {road_name}' if road_name else 'Scheduled roadworks',
            'detail': detail, 'source': 'National Highways · OGL v3.0',
            'source_url': NATIONAL_HIGHWAYS_ROADWORKS_DATASET,
            'updated_at': published,
        })))
    if not activities:
        raise ValueError('National Highways roadworks publication has no usable records')
    return activities


def _national_highways_roadworks():
    now = time.time()
    if now >= _NH_ROADWORKS_CACHE['until']:
        listing = _get_json(NATIONAL_HIGHWAYS_ROADWORKS_CATALOG)
        if listing.get('success') is not True:
            raise ValueError('National Highways roadworks catalog is unavailable')
        resources = listing.get('result', {}).get('resources', [])
        candidates = []
        for resource in resources:
            url = str(resource.get('url') or '')
            if not re.fullmatch(
                    r'https://s3\.eu-west-2\.amazonaws\.com/webdata\.nationalhighways\.co\.uk/'
                    r'ha-roadworks/nh_roadworks_20\d{2}_\d{1,2}_\d{1,2}\.xml', url):
                continue
            published = _timestamp(resource.get('created'))
            if published is not None:
                candidates.append((published, url, resource.get('created')))
        if not candidates:
            raise ValueError('National Highways roadworks file is unavailable')
        published, url, published_text = max(candidates)
        if not -300 <= now - published <= 12 * 86400:
            raise ValueError('National Highways roadworks file is stale')
        if url != _NH_ROADWORKS_CACHE['url']:
            request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
            with urllib.request.urlopen(request, timeout=25) as response:
                body = response.read(5 * 1024 * 1024 + 1)
            if len(body) > 5 * 1024 * 1024:
                raise ValueError('National Highways roadworks file exceeded 5 MB')
            activities = _parse_national_highways_roadworks(ET.fromstring(body), published_text)
            _NH_ROADWORKS_CACHE.update(url=url, published=published_text, activities=activities)
        _NH_ROADWORKS_CACHE['until'] = now + 6 * 3600
    return [item for start, end, item in _NH_ROADWORKS_CACHE['activities'] if start <= now < end]


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


def _parse_ukpn_streetworks(metadata, rows, now=None):
    now = time.time() if now is None else now
    meta = (metadata.get('metas') or {}).get('default') or {}
    published = meta.get('data_processed')
    processed = _timestamp(published)
    if processed is None or not -300 <= now - processed <= 6 * 3600:
        raise ValueError('UK Power Networks streetworks publication is stale')
    if (not isinstance(rows, list) or not 0 < len(rows) <= 5000
            or meta.get('records_count') != len(rows)):
        raise ValueError('UK Power Networks streetworks export is incomplete')
    today = dt.datetime.fromtimestamp(now, ZoneInfo('Europe/London')).date()
    features = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('permit_status') not in {'granted', 'issued'}:
            continue
        reference = str(row.get('permit_ref') or '')
        if not re.fullmatch(r'[A-Za-z0-9-]{8,40}', reference) or reference in seen:
            continue
        try:
            start = dt.date.fromisoformat(row['actualstartdate'])
            end = dt.date.fromisoformat(row['odp_end_date'])
            if not start <= today <= end:
                continue
            lon = float(row['geo_point_2d']['lon'])
            lat = float(row['geo_point_2d']['lat'])
            if not (-2.5 <= lon <= 2 and 50 <= lat <= 54):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(reference)
        location = _clean(row.get('location'), 120) or 'UK Power Networks work site'
        work = _clean(row.get('works_description'), 100)
        features.append(_feature([lon, lat], {
            'key': f'uk:ukpn:streetworks:{reference}', 'layer': 'construction',
            'title': f'Utility streetworks · {location}',
            'detail': ' · '.join(filter(None, (work, f'Published work window through {end:%d %b %Y}'))),
            'source': 'UK Power Networks · CC BY 4.0',
            'source_url': f'{UKPN_BASE}/explore/dataset/{UKPN_STREETWORKS_DATASET}/',
            'updated_at': published,
        }))
    return features


def _ukpn_streetworks():
    base = f'{UKPN_BASE}/api/explore/v2.1/catalog/datasets/{UKPN_STREETWORKS_DATASET}'
    metadata = _get_json(base)
    rows = _get_json(base + '/exports/json')
    return _parse_ukpn_streetworks(metadata, rows)


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


def _nie_local_time(value, now):
    """Powercheck omits the year from its local start and update timestamps."""
    if not isinstance(value, str):
        return None
    zone = ZoneInfo('Europe/London')
    year = dt.datetime.fromtimestamp(now, zone).year
    dates = []
    for candidate_year in (year - 1, year, year + 1):
        try:
            local = dt.datetime.strptime(f'{value.strip()} {candidate_year}',
                                         '%I:%M %p, %d %b %Y').replace(tzinfo=zone)
            dates.append(local.timestamp())
        except ValueError:
            continue
    return min(dates, key=lambda stamp: abs(stamp - now)) if dates else None


def _parse_nie_outages(payload, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or not isinstance(payload.get('outageMessage'), list):
        raise ValueError('NIE Powercheck returned an invalid outage payload')
    features = []
    seen = set()
    for row in payload['outageMessage']:
        if not isinstance(row, dict):
            continue
        outage_id = str(row.get('outageId') or '')
        if not re.fullmatch(r'\d{1,12}', outage_id) or outage_id in seen:
            continue
        kind = str(row.get('outageType') or '').lower()
        if kind not in {'fault', 'planned'}:
            continue
        updated = _nie_local_time(row.get('updatedTimeStamp'), now)
        start = _nie_local_time(row.get('startTime'), now)
        if (updated is None or not -600 <= now - updated <= 2 * 3600
                or start is None or start > now + 600):
            continue
        raw_point = row.get('point') or {}
        try:
            easting, northing = (float(part) for part in raw_point['coordinates'].split(','))
            lon, lat = _NIE_TRANSFORMER.transform(easting, northing)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if not (-8.3 <= lon <= -5.3 and 54 <= lat <= 55.5):
            continue
        seen.add(outage_id)
        try:
            affected = max(0, min(1_000_000, int(row.get('numCustAffected') or 0)))
        except (TypeError, ValueError):
            affected = 0
        postcode = _clean(row.get('postCode'), 80).replace(' ;', ',')
        estimate = row.get('estRestoreFullDateTime') or ''
        try:
            etr = (dt.datetime.strptime(estimate, '%I:%M %p, %d %b %Y')
                   .replace(tzinfo=ZoneInfo('Europe/London'))
                   .astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z'))
        except (TypeError, ValueError):
            etr = ''
        reason = _clean(f"{_clean(row.get('causeMessage'))} "
                        f"{_clean(row.get('statusMessage'))}", 180)
        features.append(_feature([lon, lat], {
            'key': f'uk:nie:{outage_id}', 'provider': 'NIE Networks',
            'area_name': postcode or 'Northern Ireland',
            'customers_affected': affected,
            'status': 'Planned power cut' if kind == 'planned' else 'Power cut',
            'reason': reason, 'etr': etr,
            'source_label': 'NIE Networks Powercheck', 'source_url': NIE_OUTAGES_SOURCE,
            'source_updated': dt.datetime.fromtimestamp(updated, dt.timezone.utc)
                             .isoformat().replace('+00:00', 'Z'),
        }))
    return features


def _nie_outages():
    return _parse_nie_outages(_get_json(NIE_OUTAGES_URL))


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


def _parse_liander_outages(payload, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get('error') or not isinstance(payload.get('features'), list):
        raise ValueError('Liander returned an invalid outage payload')
    if payload.get('exceededTransferLimit'):
        raise ValueError('Liander outage response was incomplete')
    features = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        fields = row.get('attributes') or {}
        center = row.get('centroid') or {}
        try:
            outage_id = int(fields['STORING_NUMMER'])
            lon, lat = float(center['x']), float(center['y'])
            reported = float(fields['STORING_DATUM_GEMELD']) / 1000
            updated = float(fields['STORING_SERVICE_UPDATE']) / 1000
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (outage_id in seen or not 3 <= lon <= 8 or not 50 <= lat <= 54
                or fields.get('STORING_TYPE') != 'S'
                or fields.get('STORING_ENERGIESOORT') != 'Elektriciteit'
                or fields.get('STORING_DATUM_EIND') is not None
                or not str(fields.get('STORING_STATUS') or '').strip()
                or str(fields['STORING_STATUS']).casefold() == 'opgelost'
                or not -300 <= now - reported <= 7 * 86400
                or not -300 <= now - updated <= 86400):
            continue
        seen.add(outage_id)
        impact = _clean(fields.get('STORING_GETROFFEN_KLANTEN'), 30)
        features.append(_feature([lon, lat], {
            'key': f'nl:liander:{outage_id}', 'provider': 'Liander',
            'area_name': _clean(fields.get('STORING_GETROFFEN_PLAATSEN'), 100) or 'Liander service area',
            'customers_affected': (impact if re.fullmatch(r'<\s*\d+', impact)
                                   else int(impact) if impact.isdecimal() else 0),
            'status': 'Unplanned power outage',
            'reason': _clean(fields.get('STORING_STATUS'), 80) + ' · approximate area center',
            'etr': '', 'source_label': 'Liander · CC BY 4.0',
            'source_url': LIANDER_OUTAGES_SOURCE,
            'source_updated': dt.datetime.fromtimestamp(updated, dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
        }))
    return features


def _liander_outages():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    where = ("STORING_TYPE = 'S' AND STORING_ENERGIESOORT = 'Elektriciteit' "
             "AND STORING_DATUM_EIND IS NULL AND STORING_STATUS <> 'opgelost' "
             f"AND STORING_DATUM_GEMELD >= timestamp '{cutoff:%Y-%m-%d %H:%M:%S}'")
    query = urllib.parse.urlencode({
        'where': where, 'outFields': ','.join((
            'STORING_NUMMER', 'STORING_TYPE', 'STORING_ENERGIESOORT', 'STORING_STATUS',
            'STORING_DATUM_GEMELD', 'STORING_DATUM_EIND', 'STORING_SERVICE_UPDATE',
            'STORING_GETROFFEN_KLANTEN', 'STORING_GETROFFEN_PLAATSEN')),
        'returnGeometry': 'false', 'returnCentroid': 'true', 'outSR': 4326,
        'resultRecordCount': 2000, 'f': 'json',
    })
    return _parse_liander_outages(_get_json(f'{LIANDER_OUTAGES_URL}?{query}'))


def _parse_azhk_outages(payload, now=None):
    """Public transformer interruption points, excluding old and finished work."""
    now = time.time() if now is None else now
    if not isinstance(payload, list) or len(payload) > 10000 or any(not isinstance(row, dict) for row in payload):
        raise ValueError('Alatau Zharyk returned an invalid outage catalog')
    if not payload:
        return []
    zone = ZoneInfo('Asia/Almaty')

    def local_time(value):
        try:
            return dt.datetime.strptime(str(value), '%d.%m.%Y %H:%M').replace(tzinfo=zone)
        except (TypeError, ValueError):
            return None

    # AZHK puts the snapshot timestamp on only one catalog row.
    publications = [stamp.timestamp() for row in payload if (stamp := local_time(row.get('date')))]
    published = max(publications, default=0)
    if not -300 <= now - published <= 900:
        raise ValueError('Alatau Zharyk outage snapshot is stale or undated')
    updated = dt.datetime.fromtimestamp(published, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    features, seen = [], set()
    for row in payload:
        reference = str(row.get('id') or '')
        kind = row.get('type_r')
        if not reference.isdecimal() or reference in seen or kind not in {'plan', 'crash'}:
            continue
        try:
            lon, lat = float(row['Dolgota']), float(row['Shirota'])
        except (KeyError, TypeError, ValueError):
            continue
        if not (74 <= lon <= 81 and 42 <= lat <= 47):
            continue
        info = _clean(row.get('Information'), 2000)
        match = re.search(r'Время отключения\s*-\s*(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})', info)
        start = local_time(match[1]) if match else None
        if not start or not 0 <= now - start.timestamp() <= 48 * 3600:
            continue
        expires = min(published + 900, start.timestamp() + 48 * 3600)
        etr = ''
        if kind == 'plan':
            clock = str(row.get('time_on') or '')
            if not re.fullmatch(r'\d{2}:\d{2}', clock):
                continue
            try:
                hour, minute = (int(part) for part in clock.split(':'))
                end = start.replace(hour=hour, minute=minute)
            except ValueError:
                continue
            if end <= start:
                end += dt.timedelta(days=1)
            if now >= end.timestamp():
                continue
            expires = min(expires, end.timestamp())
            etr = end.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        area = re.search(r'Район погашения:\s*(.*?)\s*Причина отключения', info)
        reason = re.search(r'Причина отключения\s*-\s*(.*?)\s*Ожидаемое время включения', info)
        seen.add(reference)
        features.append(_feature([lon, lat], {
            'key': f'kz:azhk:{reference}', 'provider': 'Alatau Zharyk Company',
            'provider_key': 'kz:azhk',
            'area_name': _clean(area[1], 200) if area else 'Almaty service area',
            'customers_affected': 0,
            'status': 'Reported planned outage' if kind == 'plan' else 'Reported unplanned outage',
            'reason': ' · '.join(filter(None, [
                _clean(reason[1], 120) if reason else '',
                f'Reported {start.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")}',
                'Utility-reported transformer point',
            ])),
            # Numeric crash time_on values are relative estimates, not timestamps.
            'etr': etr, 'source_label': 'Alatau Zharyk Company · public outage map',
            'source_url': AZHK_OUTAGES_SOURCE, 'source_updated': updated,
            'valid_until': expires,
        }))
    return features


def _azhk_outages():
    return _parse_azhk_outages(_get_json(AZHK_OUTAGES_URL))


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


def _parse_luxembourg_roads(root, now=None):
    now = time.time() if now is None else now
    published_text = root.findtext('.//{*}publicationTime')
    published = _timestamp(published_text)
    if published is None or not -600 <= now - published <= 20 * 60:
        raise ValueError('Luxembourg road publication is stale or invalid')
    categories = {
        'MaintenanceWorks': ('construction', 'Road maintenance'),
        'ConstructionWorks': ('construction', 'Roadworks'),
        'Accident': ('incidents', 'Crash'),
        'GeneralObstruction': ('incidents', 'Road obstruction'),
        'VehicleObstruction': ('incidents', 'Vehicle obstruction'),
        'AbnormalTraffic': ('incidents', 'Traffic delay'),
        'WeatherRelatedRoadConditions': ('incidents', 'Weather road hazard'),
        'EquipmentOrSystemFault': ('incidents', 'Road equipment fault'),
    }
    features = []
    for record in root.findall('.//{*}situationRecord'):
        category = categories.get(record.get(_DATEX_TYPE, '').split(':')[-1])
        if category is None or not record.get('id'):
            continue
        start = _timestamp(record.findtext('.//{*}overallStartTime'))
        end = _timestamp(record.findtext('.//{*}overallEndTime'))
        status = record.findtext('.//{*}validityStatus')
        if status in {'suspended', 'cancelled', 'inactive'} or (start is not None and start > now) or (end is not None and end < now):
            continue
        lat = record.findtext('.//{*}pointCoordinates/{*}latitude')
        lon = record.findtext('.//{*}pointCoordinates/{*}longitude')
        point = _point({'coordinates': [lon, lat]})
        if not point or not (5.5 <= point[0] <= 6.6 and 49.35 <= point[1] <= 50.2):
            continue
        road = _clean(record.findtext('.//{*}roadName'), 24)
        direction = _clean(record.findtext('.//{*}roadDestination'), 100)
        comments = [_clean(node.text, 160) for node in record.findall(
            './/{*}generalPublicComment/{*}comment/{*}values/{*}value')]
        comments = [comment for comment in comments if comment]
        restricted = record.findtext('.//{*}numberOfLanesRestricted')
        lanes = f'{restricted} lane(s) restricted' if restricted and restricted.isdigit() and int(restricted) > 0 else ''
        layer, label = category
        features.append(_feature(point, {
            'key': f'lu:cita:{record.get("id")}', 'layer': layer,
            'title': f'{label} · {road}' if road else label,
            'detail': _clean(' · '.join(filter(None, [direction, *comments[:2], lanes])), 280),
            'source': 'Luxembourg CITA · CC0', 'source_url': LUXEMBOURG_ROADS_SOURCE,
            'updated_at': published_text,
        }))
    return features


def _luxembourg_roads():
    try:
        root = _get_xml(LUXEMBOURG_ROADS_URL)
    except ET.ParseError:
        # Retry a blank response from the minute-refreshing publication once.
        time.sleep(0.5)
        root = _get_xml(LUXEMBOURG_ROADS_URL.replace('://cita.lu/', '://www.cita.lu/'))
    return _parse_luxembourg_roads(root)


def _parse_luxembourg_cameras(root):
    if root.tag != '{http://www.opengis.net/kml/2.2}kml':
        raise ValueError('Luxembourg camera catalog is not KML')
    features = []
    for camera in root.findall('.//{*}Placemark'):
        match = re.fullmatch(r'camera_(\d{1,8})', camera.get('id', ''))
        coordinates = camera.findtext('.//{*}Point/{*}coordinates', default='').split(',')
        point = _point({'coordinates': coordinates})
        if not match or not point or not (5.5 <= point[0] <= 6.6 and 49.35 <= point[1] <= 50.2):
            continue
        name = _clean(camera.findtext('{*}name'), 90)
        camera_id = match.group(1)
        features.append(_feature(point, {
            'key': f'lu:cita:camera:{camera_id}', 'layer': 'cameras',
            'title': name or f'Motorway camera {camera_id}',
            'detail': 'Recent still image',
            'snapshot_url': f'/luxembourg-camera/{camera_id}',
            'snapshot_refresh_ms': 120000,
            'source': 'Luxembourg CITA · CC0 catalog', 'source_url': LUXEMBOURG_CAMERAS_SOURCE,
        }))
    if not features:
        raise ValueError('Luxembourg camera catalog contains no usable cameras')
    return features


def _luxembourg_cameras():
    cameras = _parse_luxembourg_cameras(_get_xml(LUXEMBOURG_CAMERAS_URL))
    unavailable = _luxembourg_unavailable_cameras(cameras)
    usable = [camera for camera in cameras if camera['properties']['key'] not in unavailable]
    if not usable:
        raise ValueError('Luxembourg camera stills are all unavailable')
    return usable


_LUXEMBOURG_CAMERA_HEALTH = {'until': 0, 'unavailable': set()}
_LUXEMBOURG_CAMERA_HEALTH_LOCK = threading.Lock()


def _luxembourg_camera_url(camera_id):
    if not re.fullmatch(r'\d{1,8}', str(camera_id)):
        raise ValueError('Invalid Luxembourg camera ID')
    return f'https://www.cita.lu/info_trafic/cameras/images/cccam_{camera_id}.jpg'


def _luxembourg_camera_headers_usable(response, now):
    if urllib.parse.urlsplit(response.url).hostname != 'www.cita.lu':
        return False
    headers = response.headers
    if headers.get('Content-Type', '').split(';')[0] != 'image/jpeg':
        return False
    try:
        size = int(headers.get('Content-Length', '0'))
        age = now - email.utils.parsedate_to_datetime(headers['Last-Modified']).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    # CITA serves small but valid JPEGs saying "camera unavailable" or "No video".
    # The 90 working images checked on 28 Sep 2026 were all over 33 KB.
    return 12_000 <= size <= 2_000_000 and -300 <= age <= 30 * 60


def _luxembourg_unavailable_cameras(cameras):
    with _LUXEMBOURG_CAMERA_HEALTH_LOCK:
        now = time.time()
        if now < _LUXEMBOURG_CAMERA_HEALTH['until']:
            return set(_LUXEMBOURG_CAMERA_HEALTH['unavailable'])

    def unavailable(camera):
        key = camera['properties']['key']
        camera_id = key.rsplit(':', 1)[-1]
        request = urllib.request.Request(_luxembourg_camera_url(camera_id), method='HEAD',
                                         headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return None if _luxembourg_camera_headers_usable(response, now) else key
        except (OSError, ValueError):
            return key

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        unavailable_ids = {key for key in executor.map(unavailable, cameras) if key}
    with _LUXEMBOURG_CAMERA_HEALTH_LOCK:
        _LUXEMBOURG_CAMERA_HEALTH.update(until=now + 600, unavailable=unavailable_ids)
    return unavailable_ids


def luxembourg_camera_snapshot(camera_id):
    request = urllib.request.Request(_luxembourg_camera_url(camera_id), headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if not _luxembourg_camera_headers_usable(response, time.time()):
                raise FileNotFoundError('Luxembourg camera still is unavailable or stale')
            image = response.read(2_000_001)
        if not 12_000 <= len(image) <= 2_000_000 or not image.startswith(b'\xff\xd8\xff'):
            raise ValueError('Luxembourg camera returned no usable JPEG still')
        return image, 'image/jpeg'
    except (OSError, ValueError):
        with _LUXEMBOURG_CAMERA_HEALTH_LOCK:
            _LUXEMBOURG_CAMERA_HEALTH['unavailable'] = (
                set(_LUXEMBOURG_CAMERA_HEALTH['unavailable']) | {f'lu:cita:camera:{camera_id}'})
        raise


def _parse_luxembourg_traffic(root, road, now=None):
    now = time.time() if now is None else now
    published = _timestamp(root.findtext('.//{*}publicationTime'))
    if published is None or not -600 <= now - published <= 20 * 60:
        raise ValueError(f'Luxembourg {road.upper()} traffic publication is stale or invalid')
    features = []
    for site in root.findall('.//{*}siteMeasurements'):
        reference = site.find('{*}measurementSiteReference')
        site_id = reference.get('id', '') if reference is not None else ''
        measured_text = site.findtext('{*}measurementTimeDefault')
        measured = _timestamp(measured_text)
        if not site_id or measured is None or not -600 <= now - measured <= 30 * 60:
            continue
        lat = site.findtext('.//{*}locationForDisplay/{*}latitude')
        lon = site.findtext('.//{*}locationForDisplay/{*}longitude')
        point = _point({'coordinates': [lon, lat]})
        if not point or not (5.5 <= point[0] <= 6.6 and 49.35 <= point[1] <= 50.2):
            continue
        speed_text = site.findtext('.//{*}averageVehicleSpeed/{*}speed')
        flow_text = site.findtext('.//{*}vehicleFlow/{*}vehicleFlowRate')
        try:
            speed = float(speed_text) if speed_text is not None else None
            flow = int(flow_text) if flow_text is not None else None
        except ValueError:
            continue
        speed = speed if speed is not None and 0 <= speed <= 240 else None
        flow = flow if flow is not None and 0 <= flow <= 20000 else None
        if speed is None and flow is None:
            continue
        number = _clean(site.findtext('.//{*}roadNumber'), 12)
        detail = ' · '.join(filter(None, [
            f'Average speed {speed:g} km/h' if speed is not None else '',
            f'{flow:,} vehicles/hour' if flow is not None else '',
        ]))
        features.append(_feature(point, {
            'key': f'lu:cita:sensor:{site_id}', 'layer': 'sensors',
            'title': f'{number or road.upper()} traffic sensor', 'detail': detail,
            'source': 'Luxembourg CITA · CC0', 'source_url': LUXEMBOURG_TRAFFIC_SOURCE,
            'updated_at': measured_text,
        }))
    return features


def _luxembourg_traffic():
    roads = ('a1', 'a3', 'a4', 'a6', 'a7', 'a13', 'b40')
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_get_xml, LUXEMBOURG_TRAFFIC_BASE + road): road for road in roads}
        results = []
        for future in concurrent.futures.as_completed(futures):
            road = futures[future]
            try:
                results.extend(_parse_luxembourg_traffic(future.result(), road))
            except (OSError, ValueError, ET.ParseError):
                continue
    if not results:
        raise ValueError('Luxembourg traffic measurements are unavailable or stale')
    return results


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


def _parse_brussels_counters(payload, now=None):
    now = time.time() if now is None else now
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(payload.get('features'), list)
            or (isinstance(payload.get('totalFeatures'), int)
                and payload['totalFeatures'] > len(payload['features']))
            or (isinstance(payload.get('numberReturned'), int)
                and payload['numberReturned'] != len(payload['features']))):
        raise ValueError('Brussels traffic-counter feed is incomplete or invalid')
    features = []
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        properties = row.get('properties') or {}
        if properties.get('is_active') != 1:
            continue
        station = str(properties.get('traverse_name') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{2,40}', station):
            continue
        point = _point(row.get('geometry'))
        if not point or not (4.2 <= point[0] <= 4.5 and 50.7 <= point[1] <= 51.0):
            continue
        measured_at = properties.get('end_time_1m_a')
        measured = _timestamp(measured_at)
        if measured is None or not -300 <= now - measured <= 15 * 60:
            continue
        try:
            count = float(properties.get('count_1m_a'))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(count) and 0 <= count <= 1000):
            continue
        try:
            speed = float(properties.get('speed_1m_a'))
        except (TypeError, ValueError):
            speed = None
        try:
            occupancy = float(properties.get('occupancy_1m_a'))
        except (TypeError, ValueError):
            occupancy = None
        detail = f'{count:.0f} {"vehicle" if count == 1 else "vehicles"}/min'
        if speed is not None and math.isfinite(speed) and 0 < speed <= 200:
            detail += f' · {speed:.0f} km/h average'
        if occupancy is not None and math.isfinite(occupancy) and 0 <= occupancy <= 100:
            detail += f' · {occupancy:.0f}% occupancy'
        features.append(_feature(point, {
            'key': f'be:brussels:counter:{station}', 'layer': 'sensors',
            'title': 'Brussels traffic counter', 'detail': detail,
            'source': 'Brussels Mobility · CC0', 'source_url': BRUSSELS_COUNTERS_SOURCE,
            'updated_at': measured_at,
        }))
    return features


def _brussels_counters():
    return _parse_brussels_counters(_get_json(BRUSSELS_COUNTERS_URL))


def _brussels_event_time(value):
    """The public WFS appends Z to timestamps that are Brussels wall time."""
    try:
        parsed = dt.datetime.fromisoformat(str(value or '').removesuffix('Z'))
        return parsed.replace(tzinfo=ZoneInfo('Europe/Brussels')).timestamp()
    except (TypeError, ValueError):
        return None


def _parse_brussels_events(payload, now=None):
    now = time.time() if now is None else now
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(payload.get('features'), list)
            or (isinstance(payload.get('numberReturned'), int)
                and payload['numberReturned'] != len(payload['features']))):
        raise ValueError('Brussels road-event feed is incomplete or invalid')
    features = []
    recent_publication = False
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        properties = row.get('properties') or {}
        published = _brussels_event_time(properties.get('last_layer_update'))
        if published is None or not -300 <= now - published <= 2 * 3600:
            continue
        recent_publication = True
        if properties.get('is_active') is not True:
            continue
        identifier = str(properties.get('fid') or row.get('id') or '')
        if not re.fullmatch(r'\d{1,10}', identifier):
            continue
        point = _point(row.get('geometry'))
        if not point or not (4.2 <= point[0] <= 4.5 and 50.7 <= point[1] <= 51.0):
            continue
        start = _brussels_event_time(properties.get('start_time'))
        end = _brussels_event_time(properties.get('end_time'))
        if start is None or start > now or (end is not None and end < now):
            continue
        code = str(properties.get('datex_codes') or '').strip().upper()
        is_work = code == 'RWK' or 'travaux' in str(properties.get('type_fr') or '').lower()
        layer = 'construction' if is_work else 'incidents'
        location = _clean(properties.get('location_fr') or properties.get('location_nl'), 100)
        event_type = _clean(properties.get('type_fr') or properties.get('type_nl'), 70)
        title = 'Road work' if is_work else 'Road closed' if code == 'RCA' else event_type or 'Road disruption'
        details = [_clean(properties.get('consequences_fr') or properties.get('consequences_nl'), 140),
                   _clean(properties.get('direction_fr') or properties.get('direction_nl'), 65)]
        features.append(_feature(point, {
            'key': f'be:brussels:event:{identifier}', 'layer': layer,
            'title': f'{title} · {location}' if location else f'{title} · Brussels',
            'detail': ' · '.join(part for part in details if part) or event_type or title,
            'source': 'Brussels Mobility · CC0', 'source_url': BRUSSELS_EVENTS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(published, dt.timezone.utc).isoformat(),
        }))
    if payload['features'] and not recent_publication:
        raise ValueError('Brussels road-event publication is stale')
    return features


def _brussels_events():
    return _parse_brussels_events(_get_json(BRUSSELS_EVENTS_URL))


def _parse_brussels_signs(locations, displays, now=None):
    now = time.time() if now is None else now
    if (not isinstance(locations, dict) or locations.get('type') != 'FeatureCollection'
            or not isinstance(locations.get('features'), list)
            or (isinstance(locations.get('totalFeatures'), int)
                and locations['totalFeatures'] != len(locations['features']))
            or not isinstance(displays, dict) or displays.get('type') != 'FeatureCollection'
            or not isinstance(displays.get('features'), list)
            or displays.get('numberMatched') != len(displays['features'])):
        raise ValueError('Brussels sign catalog is incomplete or invalid')
    points = {}
    for row in locations['features']:
        props = row.get('properties') or {}
        identifier = str(props.get('id_mobigis') or '')
        point = _point(row.get('geometry'))
        if (re.fullmatch(r'[A-Za-z0-9_-]{2,32}', identifier) and point
                and 4.2 <= point[0] <= 4.5 and 50.7 <= point[1] <= 51.0):
            points[identifier] = (point, props)
    features = []
    recent = False
    for row in displays['features']:
        props = row.get('properties') or {}
        updated = _brussels_event_time(props.get('last_update'))
        if updated is None or not -300 <= now - updated <= 20 * 60:
            continue
        recent = True
        identifier = str(props.get('id_mobigis') or '')
        if props.get('status') != 'OK' or identifier not in points:
            continue
        messages = []
        for language in ('fr', 'nl'):
            lines = [_clean(props.get(f'{language}_l{i}'), 70) for i in range(1, 5)]
            message = _clean(' / '.join(line for line in lines if line), 150)
            if message and message not in messages:
                messages.append(message)
        if not messages:
            continue
        point, site = points[identifier]
        location = _clean(site.get('localisation') or props.get('localisation'), 70)
        direction = _clean(site.get('sens') or props.get('sens'), 50)
        features.append(_feature(point, {
            'key': f'be:brussels:sign:{identifier}', 'layer': 'signs',
            'title': 'Message sign' + (f' · {location}' if location else ''),
            'detail': ' · '.join(part for part in (direction, *messages) if part)[:280],
            'source': 'Brussels Mobility · CC0', 'source_url': BRUSSELS_SIGNS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(updated, dt.timezone.utc).isoformat(),
        }))
    if displays['features'] and not recent:
        raise ValueError('Brussels sign displays are stale')
    return features


def _brussels_signs():
    locations = _get_json(BRUSSELS_SIGN_LOCATIONS_URL)
    for attempt in range(3):
        displays = _get_json(BRUSSELS_SIGN_DISPLAYS_URL)
        try:
            return _parse_brussels_signs(locations, displays)
        except ValueError as error:
            if attempt == 2 or str(error) != 'Brussels sign displays are stale':
                raise


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


def _parse_ndw_bridge_openings(root, now=None):
    now = time.time() if now is None else now
    payload = next((item for item in root.iter()
                    if item.tag.rsplit('}', 1)[-1] == 'payload'
                    and item.get(_DATEX_TYPE, '').endswith('SituationPublication')), None)
    if payload is None:
        raise ValueError('NDW bridge opening publication is invalid')
    published = payload.findtext('{*}publicationTime')
    observed = _timestamp(published)
    if observed is None or not -600 <= now - observed <= 20 * 60:
        raise ValueError('NDW bridge opening publication is stale')
    features = []
    for situation in payload.findall('{*}situation'):
        for record in situation.findall('{*}situationRecord'):
            if record.findtext('{*}generalNetworkManagementType') != 'bridgeSwingInOperation':
                continue
            start = _timestamp(record.findtext('.//{*}overallStartTime'))
            end = _timestamp(record.findtext('.//{*}overallEndTime'))
            if start is None or end is None or not start <= now <= end or end - start > 3 * 3600:
                continue
            point = _ndw_record_point(record)
            if point is None:
                continue
            identifier = str(record.get('id') or '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{5,120}', identifier):
                continue
            end_label = dt.datetime.fromtimestamp(end, dt.timezone.utc).strftime('%H:%M UTC')
            features.append(_feature(point, {
                'key': f'nl:ndw:bridge:{identifier}', 'layer': 'incidents',
                'title': 'Scheduled bridge opening · Netherlands',
                'detail': f'Road traffic may pause until {end_label} · schedule, not a confirmed closure',
                'source': 'NDW Open Data · bridge schedule', 'source_url': NDW_BASE,
                'updated_at': published,
            }))
    return features


def _ndw_bridge_openings():
    return _parse_ndw_bridge_openings(_get_gzip_xml(NDW_BASE + 'planningsfeed_brugopeningen.xml.gz'))


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


def _ndw_msi_locations(now=None):
    """Cache NDW's monthly WGS84 sign positions across road snapshot refreshes."""
    now = time.time() if now is None else now
    with _NDW_MSI_SHAPES_LOCK:
        if now < _NDW_MSI_SHAPES['until'] and _NDW_MSI_SHAPES['locations']:
            return _NDW_MSI_SHAPES['locations']
        request = urllib.request.Request(NDW_BASE + NDW_MSI_SHAPES_FILE, headers={
            'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'application/zip'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read(3 * 1024 * 1024 + 1)
            if len(body) > 3 * 1024 * 1024:
                raise ValueError('NDW MSI location archive exceeded size limit')
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                names = ('MSI/shapes.shp', 'MSI/shapes.shx', 'MSI/shapes.dbf')
                if any(archive.getinfo(name).file_size > 8 * 1024 * 1024 for name in names):
                    raise ValueError('NDW MSI location table exceeded size limit')
                shp, shx, dbf = (io.BytesIO(archive.read(name)) for name in names)
            locations = {}
            with shapefile.Reader(shp=shp, shx=shx, dbf=dbf) as reader:
                if len(reader) > 30000:
                    raise ValueError('NDW MSI location count exceeded limit')
                for item in reader.iterShapeRecords():
                    record = item.record.as_dict()
                    identifier = record.get('uuid')
                    point = item.shape.points
                    if not identifier or len(point) != 1:
                        continue
                    lonlat = _point({'coordinates': list(point[0])})
                    if lonlat and 3.0 <= lonlat[0] <= 7.4 and 50.6 <= lonlat[1] <= 53.8:
                        locations[identifier] = (lonlat, record)
            if len(locations) < 10000:
                raise ValueError('NDW MSI location archive has too few signs')
            _NDW_MSI_SHAPES.update({'locations': locations, 'until': now + 24 * 3600})
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, shapefile.ShapefileException):
            if not _NDW_MSI_SHAPES['locations']:
                raise
            _NDW_MSI_SHAPES['until'] = now + 30 * 60
        return _NDW_MSI_SHAPES['locations']


def _parse_ndw_msi_signs(root, locations, published, now=None, min_states=10000):
    """Join current lane displays to NDW's individual sign positions."""
    now = time.time() if now is None else now
    if published is None or not -300 <= now - published <= 20 * 60:
        raise ValueError('NDW MSI publication is stale or invalid')
    groups = {}
    displays = 0
    for event in root.findall('.//{*}event'):
        display = event.find('{*}display')
        if display is None or not list(display):
            continue
        displays += 1
        identifier = event.findtext('{*}sign_id/{*}uuid')
        if identifier not in locations:
            continue
        state = list(display)[0]
        kind = state.tag.rsplit('}', 1)[-1]
        if kind == 'speedlimit' and re.fullmatch(r'\d{2,3}', state.text or ''):
            message = f'{state.text} km/h limit'
        elif kind == 'lane_closed':
            message = 'Lane closed'
        elif kind == 'lane_closed_ahead':
            direction = next(iter(state), None)
            direction = direction.tag.rsplit('}', 1)[-1].replace('_', ' ') if direction is not None else ''
            message = 'Lane closed ahead' + (f' · {direction}' if direction else '')
        else:
            continue  # Blank/open/end states add no useful marker.
        point, record = locations[identifier]
        road = _clean(record.get('road'), 16)
        carriageway = _clean(record.get('carriagew0'), 4)
        km = record.get('km')
        lane = record.get('lane')
        if not road or not isinstance(km, (int, float)) or not isinstance(lane, int):
            continue
        key = (road, carriageway, round(km, 3), round(point[0], 5), round(point[1], 5))
        group = groups.setdefault(key, {'point': point, 'messages': []})
        group['messages'].append((lane, message))
    if displays < min_states:
        raise ValueError('NDW MSI publication has too few sign states')
    features = []
    for (road, carriageway, km, lon, lat), group in groups.items():
        messages = sorted(set(group['messages']))
        detail = '; '.join(f'Lane {lane}: {message}' for lane, message in messages)
        features.append(_feature(group['point'], {
            'key': f'nl:ndw:msi:{road}:{carriageway}:{km}:{lon}:{lat}', 'layer': 'signs',
            'title': f'{road} · km {km:g}', 'detail': detail,
            'source': 'NDW Open Data · lane signs', 'source_url': NDW_MSI_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(published, dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
        }))
    return features


def _ndw_msi_signs():
    request = urllib.request.Request(NDW_BASE + NDW_MSI_FILE, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'application/gzip'})
    with urllib.request.urlopen(request, timeout=20) as response:
        modified = response.headers.get('Last-Modified')
        compressed = response.read(2 * 1024 * 1024 + 1)
    if len(compressed) > 2 * 1024 * 1024:
        raise ValueError('NDW MSI publication exceeded compressed size limit')
    if not modified:
        raise ValueError('NDW MSI publication has no freshness date')
    published = email.utils.parsedate_to_datetime(modified).timestamp()
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
        body = archive.read(12 * 1024 * 1024 + 1)
    if len(body) > 12 * 1024 * 1024:
        raise ValueError('NDW MSI publication exceeded expanded size limit')
    return _parse_ndw_msi_signs(ET.fromstring(body), _ndw_msi_locations(), published)


def _parse_ndw_sensors(compressed, now=None):
    """Stream NDW's combined DATEX site table and current speed/flow readings."""
    now = time.time() if now is None else now
    if len(compressed) > 5 * 1024 * 1024:
        raise ValueError('NDW sensor publication exceeded compressed size limit')
    sites = {}
    features = []
    published = []
    site_count = reading_count = 0
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
        for _, element in ET.iterparse(archive, events=('end',)):
            if archive.tell() > 260 * 1024 * 1024:
                raise ValueError('NDW sensor publication exceeded expanded size limit')
            kind = element.tag.rsplit('}', 1)[-1]
            if kind == 'publicationTime':
                published.append(_timestamp(element.text))
            elif kind == 'measurementSite':
                site_count += 1
                if site_count > 25000:
                    raise ValueError('NDW sensor site count exceeded limit')
                site_id = element.get('id', '')
                try:
                    lon = float(element.findtext('.//{*}longitude'))
                    lat = float(element.findtext('.//{*}latitude'))
                except (TypeError, ValueError):
                    lon = lat = float('nan')
                if site_id and 3 <= lon <= 7.5 and 50.5 <= lat <= 54:
                    indexes = {'trafficFlow': set(), 'trafficSpeed': set()}
                    for characteristic in element.findall('{*}measurementSpecificCharacteristics'):
                        value_type = characteristic.findtext('.//{*}specificMeasurementValueType')
                        if (value_type in indexes
                                and characteristic.findtext('.//{*}vehicleType') == 'anyVehicle'):
                            indexes[value_type].add(characteristic.get('index'))
                    if any(indexes.values()):
                        sites[site_id] = ([lon, lat],
                                          _clean(element.findtext('.//{*}measurementSiteName/{*}values/{*}value'), 100),
                                          indexes)
                element.clear()
            elif kind == 'siteMeasurements':
                reading_count += 1
                if reading_count > 25000:
                    raise ValueError('NDW sensor reading count exceeded limit')
                reference = element.find('{*}measurementSiteReference')
                site = sites.get(reference.get('id')) if reference is not None else None
                observed_text = element.findtext('.//{*}measurementTimeDefault/{*}timeValue')
                observed = _timestamp(observed_text)
                if site and observed is not None and -300 <= now - observed <= 15 * 60:
                    point, name, indexes = site
                    flows, speeds = [], []
                    for quantity in element.findall('{*}physicalQuantity'):
                        index = quantity.get('index')
                        if quantity.findtext('.//{*}dataError') == 'true':
                            continue
                        if index in indexes['trafficFlow']:
                            value = quantity.findtext('.//{*}vehicleFlowRate')
                            try:
                                value = float(value)
                                if math.isfinite(value) and 0 <= value <= 10000:
                                    flows.append(value)
                            except (TypeError, ValueError):
                                pass
                        elif index in indexes['trafficSpeed']:
                            value = quantity.findtext('.//{*}speed')
                            try:
                                value = float(value)
                                if math.isfinite(value) and 0 <= value <= 200:
                                    speeds.append(value)
                            except (TypeError, ValueError):
                                pass
                    if speeds or flows:
                        detail = []
                        if speeds:
                            detail.append(f'Average lane speed {round(sum(speeds) / len(speeds))} km/h')
                        if flows:
                            detail.append(f'Flow {round(sum(flows))} vehicles/hour')
                        features.append(_feature(point, {
                            'key': f'nl:ndw:sensor:{_clean(reference.get("id"), 100)}',
                            'layer': 'sensors', 'title': name or 'Traffic sensor',
                            'detail': ' · '.join(detail), 'source': 'NDW Open Data · traffic measurements',
                            'source_url': NDW_BASE, 'updated_at': observed_text,
                        }))
                element.clear()
    if not site_count or not reading_count or not published or any(
            timestamp is None or not -300 <= now - timestamp <= 20 * 60 for timestamp in published):
        raise ValueError('NDW sensor publication is missing or stale')
    if not features:
        raise ValueError('NDW sensor publication has no current readings')
    return features


def _ndw_sensors():
    request = urllib.request.Request(NDW_BASE + NDW_SENSORS_FILE, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=25) as response:
        compressed = response.read(5 * 1024 * 1024 + 1)
    return _parse_ndw_sensors(compressed)


def _parse_hamburg_roads(payload, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection':
        raise ValueError('Hamburg road publication is invalid')
    features = payload.get('features')
    if not isinstance(features, list) or len(features) > 500:
        raise ValueError('Hamburg road publication has an invalid size')
    matched = payload.get('numberMatched')
    # Hamburg's live collection is not an atomic snapshot: its reported counts
    # can differ slightly from the actual feature array while incidents change.
    # A next link or a larger gap still means we have only part of the feed.
    has_next = any(isinstance(link, dict) and link.get('rel') == 'next'
                   for link in payload.get('links', []))
    tolerance = max(10, len(features) // 20)
    if (not isinstance(matched, int) or matched > 500 or has_next
            or matched - len(features) > tolerance):
        raise ValueError('Hamburg road publication is incomplete')
    published = _timestamp(payload.get('timeStamp'))
    if published is None or not -300 <= now - published <= 20 * 60:
        raise ValueError('Hamburg road publication is stale')
    works = {'ConstructionWorks', 'MaintenanceWorks'}
    incidents = {'RoadOrCarriagewayOrLaneManagement', 'AbnormalTraffic',
                 'Accident', 'Conditions', 'EquipmentOrSystemFault'}
    rows = []
    seen = set()
    for item in features:
        if not isinstance(item, dict):
            continue
        properties = item.get('properties')
        if not isinstance(properties, dict):
            continue
        kind = properties.get('art')
        if kind not in works | incidents:
            continue
        identifier = str(item.get('id') or '')
        if not re.fullmatch(r'\d{1,12}', identifier) or identifier in seen:
            continue
        point = _point(item.get('geometry'))
        if point is None or not (9.4 <= point[0] <= 10.4 and 53.3 <= point[1] <= 53.9):
            continue
        start = _timestamp(properties.get('start'))
        end = _timestamp(properties.get('end'))
        if start is None or start > now or (end is not None and end <= now):
            continue
        if end is None and now - start > (90 * 86400 if kind in works else 24 * 3600):
            # The publisher's “current” collection retains some months-old,
            # open-ended crash and closure reports. Avoid presenting them as live.
            continue
        seen.add(identifier)
        layer = 'construction' if kind in works else 'incidents'
        title = {'ConstructionWorks': 'Roadworks', 'MaintenanceWorks': 'Road maintenance',
                 'RoadOrCarriagewayOrLaneManagement': 'Road restriction',
                 'AbnormalTraffic': 'Traffic alert', 'Accident': 'Crash',
                 'Conditions': 'Road condition', 'EquipmentOrSystemFault': 'Traffic equipment fault'}[kind]
        rows.append(_feature(point, {
            'key': f'de:hamburg:road:{identifier}', 'layer': layer,
            'title': f'Hamburg {title.lower()}',
            'detail': _clean(properties.get('description'), 220),
            'source': 'Freie und Hansestadt Hamburg, Polizei Hamburg · DL-DE/BY 2.0',
            'source_url': HAMBURG_ROADS_SOURCE,
            'updated_at': payload['timeStamp'],
        }))
    return rows


def _hamburg_roads():
    return _parse_hamburg_roads(_get_json(HAMBURG_ROADS_URL))


def _berlin_local_time(value):
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, '%d.%m.%Y %H:%M').replace(
            tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
    except (TypeError, ValueError):
        return None


def _parse_berlin_roads(payload, published, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection':
        raise ValueError('Berlin road publication is invalid')
    features = payload.get('features')
    if not isinstance(features, list) or len(features) > 2000:
        raise ValueError('Berlin road publication has an invalid size')
    if published is None or not -300 <= now - published <= 3 * 3600:
        raise ValueError('Berlin road publication is stale')
    rows = []
    seen = set()
    for item in features:
        if not isinstance(item, dict):
            continue
        props = item.get('properties') or {}
        identifier = str(props.get('id') or '')
        if not 1 <= len(identifier) <= 100 or identifier in seen or props.get('objectState') == 'deleted':
            continue
        kind = props.get('subtype')
        if kind not in {'Baustelle', 'Sperrung', 'Gefahr'}:
            continue
        geometry = item.get('geometry') or {}
        if geometry.get('type') == 'GeometryCollection':
            point_geometry = next((part for part in geometry.get('geometries', [])
                                   if isinstance(part, dict) and part.get('type') == 'Point'), None)
        else:
            point_geometry = geometry if geometry.get('type') == 'Point' else None
        point = _point(point_geometry)
        if point is None or not (13.05 <= point[0] <= 13.8 and 52.3 <= point[1] <= 52.75):
            continue
        validity = props.get('validity') or {}
        start_text, end_text = validity.get('from'), validity.get('to')
        start, end = _berlin_local_time(start_text), _berlin_local_time(end_text)
        if (start_text and start is None) or (end_text and end is None):
            continue
        if start is not None and start > now or end is not None and end <= now:
            continue
        reported = _timestamp(props.get('tstore'))
        if reported is None or reported > now + 300:
            continue
        if end is None and now - reported > 7 * 86400:
            continue
        seen.add(identifier)
        title = {'Baustelle': 'Berlin roadworks', 'Sperrung': 'Berlin road closure',
                 'Gefahr': 'Berlin road alert'}[kind]
        detail = ' · '.join(filter(None, [_clean(props.get('street'), 170),
                                          _clean(props.get('content'), 190)]))
        rows.append(_feature(point, {
            'key': 'de:berlin:road:' + hashlib.sha1(identifier.encode()).hexdigest()[:16],
            'layer': 'construction' if kind == 'Baustelle' else 'incidents',
            'title': title, 'detail': detail,
            'source': ('Digitale Plattform Stadtverkehr Berlin / Baustellen, Sperrungen und '
                       'sonstige Störungen von besonderem verkehrlichem Interesse · DL-DE/BY 2.0'),
            'source_url': BERLIN_ROADS_SOURCE, 'updated_at': props['tstore'],
        }))
    return rows


def _berlin_roads():
    request = urllib.request.Request(BERLIN_ROADS_URL, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        modified = response.headers.get('Last-Modified')
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError('Berlin road publication exceeded size limit')
    try:
        published = email.utils.parsedate_to_datetime(modified).timestamp() if modified else None
    except (TypeError, ValueError):
        published = None
    return _parse_berlin_roads(json.loads(body), published)


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


def _dgt_publication_time(root, max_age, now=None):
    now = time.time() if now is None else now
    if root.tag.rsplit('}', 1)[-1] != 'payload':
        raise ValueError('DGT publication is invalid')
    published_text = root.findtext('{*}publicationTime')
    published = _timestamp(published_text)
    if published is None or not -600 <= now - published <= max_age:
        raise ValueError('DGT publication is stale')
    return published_text


def _dgt_point(node):
    coordinates = node.find('.//{*}pointCoordinates')
    if coordinates is None:
        return None
    try:
        lat = float(coordinates.findtext('{*}latitude'))
        lon = float(coordinates.findtext('{*}longitude'))
    except (TypeError, ValueError):
        return None
    return [lon, lat] if -19 <= lon <= 5 and 27 <= lat <= 45 else None


def _dgt_devices(root, device_type):
    result = {}
    for device in root.findall('{*}device'):
        device_id = device.get('id')
        if (not device_id or not device_id.isdigit()
                or device.findtext('{*}typeOfDevice') != device_type):
            continue
        point = _dgt_point(device)
        if point:
            result[device_id] = (device, point)
    return result


def _parse_dgt_cameras(root, now=None):
    published = _dgt_publication_time(root, 3 * 3600, now)
    features = []
    for device_id, (device, point) in _dgt_devices(root, 'camera').items():
        image_url = device.findtext('.//{*}deviceUrl') or ''
        image_match = re.fullmatch(r'https://etraffic\.dgt\.es/camarasEtraffic/(\d{1,7})\.jpg', image_url)
        if not image_match:
            continue
        image_id = image_match.group(1)
        road = _clean(device.findtext('.//{*}roadName'), 40)
        province = _clean(device.findtext('.//{*}province'), 55)
        km = _clean(device.findtext('.//{*}kilometerPoint'), 16)
        features.append(_feature(point, {
            'key': f'es:dgt:camera:{device_id}', 'layer': 'cameras',
            'title': f'Traffic camera · {road}' if road else 'Traffic camera',
            'detail': ' · '.join(part for part in (f'km {km}' if km else '', province,
                                              'Latest available still') if part),
            'snapshot_url': f'/dgt-camera/{image_id}', 'snapshot_refresh_ms': 120000,
            'source': 'Spain DGT · CC BY', 'source_url': DGT_CAMERAS_SOURCE,
            'updated_at': published,
        }))
    if not features:
        raise ValueError('DGT camera catalog contains no usable images')
    return features


_DGT_CAMERA_HEALTH = {}
_DGT_CAMERA_HEALTH_LOCK = threading.Lock()
_DGT_CAMERA_AUDIT_LOCK = threading.Lock()
_DGT_CAMERA_PLACEHOLDER_BYTES = {'32634', '9422'}
_DGT_CAMERA_STILLS = {}


def _prune_dgt_stills_locked(now):
    for camera_id, (expires, _) in list(_DGT_CAMERA_STILLS.items()):
        if expires + 120 <= now:
            _DGT_CAMERA_STILLS.pop(camera_id, None)
    total = sum(len(image) for _, image in _DGT_CAMERA_STILLS.values())
    while total > 64 * 1024 * 1024:
        oldest = next(iter(_DGT_CAMERA_STILLS))
        total -= len(_DGT_CAMERA_STILLS.pop(oldest)[1])


def _dgt_camera_url(camera_id):
    return f'https://etraffic.dgt.es/camarasEtraffic/{camera_id}.jpg'


def _dgt_camera_headers_available(response, now):
    if urllib.parse.urlsplit(response.url).hostname != 'etraffic.dgt.es':
        return False
    headers = response.headers
    if headers.get('Content-Type', '').split(';')[0] != 'image/jpeg':
        return False
    if headers.get('Content-Length') in _DGT_CAMERA_PLACEHOLDER_BYTES:
        return False
    try:
        if int(headers.get('Content-Length', '0')) < 12_000:
            return False
    except ValueError:
        return False
    modified = headers.get('Last-Modified')
    if not modified:
        return False
    try:
        age = now - email.utils.parsedate_to_datetime(modified).timestamp()
    except (TypeError, ValueError):
        return False
    return -300 <= age <= 30 * 60


def _camera_has_visible_scene(image):
    """Reject fresh JPEGs that contain only a black no-signal frame."""
    try:
        with Image.open(io.BytesIO(image)) as still:
            if still.format != 'JPEG' or not (320 <= still.width <= 4096 and 180 <= still.height <= 4096):
                return False
            still.draft('L', (96, 96))
            gray = still.convert('L')
            width, height = gray.size
            center = gray.crop((width // 8, height // 8, width * 7 // 8, height * 7 // 8))
            histogram = center.histogram()
            return sum(histogram[30:]) >= center.width * center.height * .02
    except (OSError, ValueError, UnidentifiedImageError):
        return False


def _dgt_unavailable_cameras(features):
    """Check only visible DGT stills; a fresh JPEG header can hide a bad body."""
    cameras = {item['properties']['snapshot_url'].rsplit('/', 1)[-1]
               for item in features if item['properties']['key'].startswith('es:dgt:camera:')}
    if not cameras:
        return set()
    now = time.time()

    def available(camera_id):
        request = urllib.request.Request(_dgt_camera_url(camera_id), headers={
            'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                if not _dgt_camera_headers_available(response, now):
                    return camera_id, None
                image = response.read(2_000_001)
                if len(image) > 2_000_000 or not _camera_has_visible_scene(image):
                    return camera_id, None
                return camera_id, image
        except (TimeoutError, urllib.error.URLError) as error:
            if _camera_transport_error(error):
                return camera_id, 'transport-error'
            return camera_id, None
        except (OSError, ValueError):
            return camera_id, None

    with _DGT_CAMERA_AUDIT_LOCK:
        with _DGT_CAMERA_HEALTH_LOCK:
            pending = [camera_id for camera_id in cameras
                       if _DGT_CAMERA_HEALTH.get(camera_id, (0, False))[0] <= now]
        if pending:
            with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
                results = list(executor.map(available, pending))
            with _DGT_CAMERA_HEALTH_LOCK:
                for camera_id, image in results:
                    if image == 'transport-error':
                        cached = _DGT_CAMERA_STILLS.get(camera_id)
                        usable = bool(cached and cached[0] + 120 > time.time())
                        _DGT_CAMERA_HEALTH[camera_id] = (time.time() + 60, usable)
                        continue
                    _DGT_CAMERA_HEALTH[camera_id] = (now + 300, image is not None)
                    if image is not None:
                        _DGT_CAMERA_STILLS[camera_id] = (now + 300, image)
                    else:
                        _DGT_CAMERA_STILLS.pop(camera_id, None)
                _prune_dgt_stills_locked(now)
        with _DGT_CAMERA_HEALTH_LOCK:
            return {item['properties']['key'] for item in features
                    if item['properties']['key'].startswith('es:dgt:camera:')
                    and not _DGT_CAMERA_HEALTH[item['properties']['snapshot_url'].rsplit('/', 1)[-1]][1]}


def dgt_camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{1,7}', str(camera_id)):
        raise ValueError('Invalid DGT camera ID')
    with _DGT_CAMERA_HEALTH_LOCK:
        cached = _DGT_CAMERA_STILLS.get(camera_id)
        if cached and cached[0] > time.time():
            return cached[1], 'image/jpeg'
    request = urllib.request.Request(_dgt_camera_url(camera_id), headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    try:
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    if not _dgt_camera_headers_available(response, time.time()):
                        raise FileNotFoundError('DGT camera still is unavailable or stale')
                    image = response.read(2_000_001)
                break
            except urllib.error.HTTPError:
                raise
            except (TimeoutError, urllib.error.URLError):
                if attempt:
                    raise
        if len(image) > 2_000_000 or not _camera_has_visible_scene(image):
            raise FileNotFoundError('DGT camera still has no visible scene')
        with _DGT_CAMERA_HEALTH_LOCK:
            now = time.time()
            _DGT_CAMERA_HEALTH[camera_id] = (now + 300, True)
            _DGT_CAMERA_STILLS[camera_id] = (now + 300, image)
            _prune_dgt_stills_locked(now)
        return image, 'image/jpeg'
    except (OSError, ValueError) as error:
        if _camera_transport_error(error) and cached and cached[0] + 120 > time.time():
            with _DGT_CAMERA_HEALTH_LOCK:
                _DGT_CAMERA_HEALTH[camera_id] = (time.time() + 60, True)
            return cached[1], 'image/jpeg'
        with _DGT_CAMERA_HEALTH_LOCK:
            _DGT_CAMERA_HEALTH[camera_id] = (time.time() + (60 if _camera_transport_error(error) else 180), False)
            _DGT_CAMERA_STILLS.pop(camera_id, None)
        raise


def _dgt_cameras():
    root = _dgt_static_xml('cameras', DGT_BASE + 'DevicePublication/camaras_datex2_v37.xml')
    return _parse_dgt_cameras(root)


def _parse_dgt_incidents(root, now=None):
    now = time.time() if now is None else now
    published = _dgt_publication_time(root, 20 * 60, now)
    features = []
    situations = root.findall('{*}situation')
    if not situations:
        raise ValueError('DGT situation publication has no situations')
    causes = {'roadMaintenance': 'Roadworks', 'accident': 'Crash',
              'vehicleObstruction': 'Vehicle obstruction',
              'environmentalObstruction': 'Road obstruction',
              'infrastructureDamageObstruction': 'Damaged road infrastructure',
              'abnormalTraffic': 'Traffic disruption', 'publicEvent': 'Public event'}
    for situation in situations:
        for record in situation.findall('{*}situationRecord'):
            record_id = record.get('id')
            if not record_id or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', record_id):
                continue
            status = record.findtext('.//{*}validityStatus')
            start = _timestamp(record.findtext('.//{*}overallStartTime'))
            end = _timestamp(record.findtext('.//{*}overallEndTime'))
            if status != 'active' or (start is not None and start > now + 600) or (end is not None and end <= now):
                continue
            point = _dgt_point(record)
            if not point:
                continue
            cause = record.findtext('.//{*}causeType') or ''
            kind = 'construction' if cause == 'roadMaintenance' or record.find('.//{*}roadMaintenanceType') is not None else 'incidents'
            label = causes.get(cause, 'Road incident')
            road = _clean(record.findtext('.//{*}roadName'), 40)
            municipality = _clean(record.findtext('.//{*}municipality'), 70)
            province = _clean(record.findtext('.//{*}province'), 55)
            management = _clean(record.findtext('.//{*}roadOrCarriagewayOrLaneManagementType'), 65)
            management = re.sub(r'([a-z])([A-Z])', r'\1 \2', management).replace('_', ' ').lower()
            detail = ' · '.join(part for part in (management, municipality, province) if part)
            features.append(_feature(point, {
                'key': f'es:dgt:incident:{record_id}', 'layer': kind,
                'title': f'{label} · {road}' if road else label, 'detail': detail,
                'source': 'Spain DGT · CC BY', 'source_url': DGT_INCIDENTS_SOURCE,
                'updated_at': published,
            }))
    return features


def _dgt_incidents():
    root = _get_dgt_xml(DGT_BASE + 'SituationPublication/datex2_v37.xml')
    return _parse_dgt_incidents(root)


def _parse_dgt_signs(locations, statuses, now=None):
    now = time.time() if now is None else now
    _dgt_publication_time(locations, 3 * 3600, now)
    _dgt_publication_time(statuses, 20 * 60, now)
    devices = _dgt_devices(locations, 'vms')
    features = []
    for controller in statuses.findall('{*}vmsControllerStatus'):
        reference = controller.find('.//{*}vmsControllerReference')
        device_id = reference.get('id') if reference is not None else None
        if device_id not in devices:
            continue
        messages = []
        for message in controller.iter():
            if message.tag.rsplit('}', 1)[-1] != 'vmsMessage':
                continue
            set_text = message.findtext('{*}timeLastSet')
            set_at = _timestamp(set_text)
            if set_at is None or not -600 <= now - set_at <= 24 * 3600:
                continue
            lines = [_clean(line.text, 120) for line in message.iter()
                     if line.tag.rsplit('}', 1)[-1] == 'textLine' and line.text and line.text.strip()]
            text = ' / '.join(dict.fromkeys(line for line in lines if line))
            if text and re.search(r'[A-Za-zÀ-ÿ0-9]', text):
                messages.append((set_at, set_text, text))
        if not messages:
            continue
        device, point = devices[device_id]
        road = _clean(device.findtext('.//{*}roadName'), 40)
        province = _clean(device.findtext('.//{*}province'), 55)
        latest = max(messages, key=lambda item: item[0])
        message_text = ' / '.join(dict.fromkeys(item[2] for item in messages))[:280]
        features.append(_feature(point, {
            'key': f'es:dgt:sign:{device_id}', 'layer': 'signs',
            'title': f'Road message sign · {road}' if road else 'Road message sign',
            'detail': ' · '.join(part for part in (message_text, province) if part),
            'source': 'Spain DGT · CC BY', 'source_url': DGT_SIGNS_SOURCE,
            'updated_at': latest[1],
        }))
    return features


def _dgt_signs():
    locations = _dgt_static_xml('sign_locations', DGT_BASE + 'DevicePublication/vms_datex2_v37.xml')
    statuses = _get_dgt_xml(DGT_BASE + 'VmsPublication/datex2_v37.xml')
    return _parse_dgt_signs(locations, statuses)


def _sct_point(member):
    raw = member.findtext('.//{*}Point/{*}coordinates') or ''
    parts = raw.strip().split(',')
    try:
        lon, lat = float(parts[0]), float(parts[1])
    except (IndexError, ValueError):
        return None
    return [lon, lat] if -0.1 <= lon <= 3.5 and 40.3 <= lat <= 42.9 else None


def _parse_sct_incidents(root):
    features = []
    seen = set()
    for member in root.findall('.//{*}featureMember'):
        incident_id = member.findtext('.//{*}identificador') or ''
        point = _sct_point(member)
        if not re.fullmatch(r'\d{1,16}', incident_id) or incident_id in seen or not point:
            continue
        seen.add(incident_id)
        kind_text = _clean(member.findtext('.//{*}descripcio_tipus'), 50)
        layer = 'construction' if kind_text.casefold() == 'obres' else 'incidents'
        road = _clean(member.findtext('.//{*}carretera'), 32)
        cause = _clean(member.findtext('.//{*}causa'), 90)
        description = _clean(member.findtext('.//{*}descripcio'), 170)
        direction = _clean(member.findtext('.//{*}sentit'), 40)
        kilometer = _clean(member.findtext('.//{*}pk_inici'), 16)
        detail = ' · '.join(part for part in (description, cause if cause != description else '',
                                            direction, f'km {kilometer}' if kilometer else '') if part)
        features.append(_feature(point, {
            'key': f'es:sct:incident:{incident_id}', 'layer': layer,
            'title': ' · '.join(part for part in (road, kind_text or 'Road event') if part),
            'detail': detail, 'source': 'Catalonia SCT', 'source_url': SCT_INCIDENTS_SOURCE,
        }))
    return features


def _sct_incidents():
    return _parse_sct_incidents(_sct_xml('incidenciesGML.xml'))


def _parse_sct_cameras(root):
    features = []
    seen = set()
    for member in root.findall('.//{*}featureMember'):
        point = _sct_point(member)
        link = (member.findtext('.//{*}link') or '').strip()
        match = re.fullmatch(r'http://mct\.gencat\.cat/mct2bo/RenderService\?sctidcam=([A-Za-z0-9_-]{1,24})\.gif', link)
        if not point or not match:
            continue
        camera_id = match.group(1)
        if camera_id in seen:
            continue
        seen.add(camera_id)
        road = _clean(member.findtext('.//{*}carretera'), 32)
        town = _clean(member.findtext('.//{*}municipi'), 70)
        kilometer = _clean(member.findtext('.//{*}pk'), 16)
        features.append(_feature(point, {
            'key': f'es:sct:camera:{camera_id}', 'layer': 'cameras',
            'title': f'Traffic camera · {road}' if road else 'Traffic camera',
            'detail': ' · '.join(part for part in (town, f'km {kilometer}' if kilometer else '',
                                               'Latest available still') if part),
            'snapshot_url': f'/catalonia-camera/{camera_id}', 'snapshot_refresh_ms': 180000,
            'source': 'Catalonia SCT', 'source_url': SCT_CAMERAS_SOURCE,
        }))
    return features


def _sct_cameras():
    return _parse_sct_cameras(_sct_xml('cameres.xml'))


def _parse_poland_roads(root, now=None):
    now = time.time() if now is None else now
    published = _timestamp(root.attrib.get('gen'))
    if root.tag != 'utrudnienia' or published is None or not -300 <= now - published <= 20 * 60:
        raise ValueError('Poland GDDKiA road publication is stale or invalid')
    features = []
    for event in root.findall('utr'):
        kind = event.findtext('typ')
        if kind not in {'U', 'W', 'I', 'K'}:
            continue
        start = _timestamp(event.findtext('data_powstania'))
        end = _timestamp(event.findtext('data_likwidacji'))
        if start is None or end is None or not start <= now <= end:
            continue
        try:
            lat, lon = float(event.findtext('geo_lat')), float(event.findtext('geo_long'))
        except (TypeError, ValueError):
            continue
        if not 48.8 <= lat <= 55.2 or not 14 <= lon <= 24.3:
            continue
        road = _clean(event.findtext('nr_drogi'), 18)
        section = _clean(event.findtext('nazwa_odcinka'), 90)
        description = _clean(event.findtext('objazd'), 210)
        effect = _clean(event.findtext('skutki'), 90)
        code = _clean(event.findtext('rodzaj/poz'), 12)
        category = 'Roadworks' if kind == 'U' else 'Road incident'
        label = ' · '.join(part for part in (road, category) if part)
        detail = ' · '.join(part for part in (section, description, effect if effect != description else '') if part)
        identity = '|'.join((kind, road, str(lat), str(lon), event.findtext('data_powstania') or '', code))
        event_id = hashlib.sha1(identity.encode('utf-8')).hexdigest()[:16]
        features.append(_feature([lon, lat], {
            'key': f'pl:gddkia:{event_id}', 'layer': 'construction' if kind == 'U' else 'incidents',
            'title': label, 'detail': detail, 'source': 'Poland GDDKiA',
            'source_url': POLAND_ROADS_SOURCE, 'updated_at': root.attrib['gen'],
        }))
    return features


def _poland_roads():
    return _parse_poland_roads(_get_xml(POLAND_ROADS_URL, max_bytes=2 * 1024 * 1024))


def _cyprus_road_time(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo('Asia/Nicosia'))
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _parse_cyprus_roads(root, now=None):
    now = time.time() if now is None else now
    situation = '{http://datex2.eu/schema/3/situation}'
    common = '{http://datex2.eu/schema/3/common}'
    location = '{http://datex2.eu/schema/3/locationReferencing}'
    published_text = root.findtext(common + 'publicationTime')
    published = _timestamp(published_text)
    if (root.tag != '{http://datex2.eu/schema/3/d2Payload}payload'
            or published is None or not -300 <= now - published <= 20 * 60):
        raise ValueError('Cyprus road publication is stale or invalid')
    features = []
    seen = set()
    for record in root.iter(situation + 'situationRecord'):
        record_id = record.get('id')
        if not record_id or record_id in seen:
            continue
        seen.add(record_id)
        status = record.findtext('.//' + common + 'validityStatus')
        if status != 'definedByValidityTimeSpec':
            continue
        start = _cyprus_road_time(record.findtext('.//' + common + 'overallStartTime'))
        end_text = record.findtext('.//' + common + 'overallEndTime')
        end = _cyprus_road_time(end_text) if end_text else None
        if start is None or start > now or (end is not None and end < now):
            continue
        # Open-ended reports must be updated recently; old records can remain in the feed.
        if end is None:
            revised = _cyprus_road_time(record.findtext('.//' + situation + 'situationRecordVersionTime'))
            if revised is None or now - revised > 36 * 3600:
                continue
        try:
            lat = float(record.findtext('.//' + location + 'latitude'))
            lon = float(record.findtext('.//' + location + 'longitude'))
        except (TypeError, ValueError):
            continue
        if not 34.4 <= lat <= 35.8 or not 31.9 <= lon <= 34.8:
            continue
        event_type = record.findtext('.//eventTypeId')
        subtype = _clean(record.findtext('.//subtype'), 60)
        description = _clean(record.findtext('.//description'), 210)
        is_work = event_type == '25' or 'work' in subtype.lower()
        layer = 'construction' if is_work else 'incidents'
        title = 'Roadworks' if is_work else ('Road closure' if 'clos' in subtype.lower() else 'Road incident')
        detail = ' · '.join(part for part in (subtype, description) if part and part.lower() != title.lower())
        features.append(_feature([lon, lat], {
            'key': f'cy:nap:{hashlib.sha1(record_id.encode("utf-8")).hexdigest()[:16]}',
            'layer': layer, 'title': title, 'detail': detail,
            'source': 'Cyprus Public Works Department', 'source_url': CYPRUS_ROADS_SOURCE,
            'updated_at': published_text,
        }))
    return features


def _cyprus_roads():
    return _parse_cyprus_roads(_get_xml(CYPRUS_ROADS_URL, max_bytes=2 * 1024 * 1024))


def _parse_cyprus_waze_alerts(root, now=None):
    now = time.time() if now is None else now
    published = _timestamp(root.findtext('.//{*}publicationTime'))
    # The publisher's clock has been observed roughly five minutes ahead of UTC.
    if (not root.tag.endswith('}d2LogicalModel') or published is None
            or not -600 <= now - published <= 20 * 60):
        raise ValueError('Cyprus Waze publication is stale or invalid')
    features = []
    seen = set()
    titles = {'ROAD_CLOSED': 'Reported road closure', 'ACCIDENT': 'Reported crash',
              'HAZARD': 'Reported road hazard', 'JAM': 'Reported congestion'}
    for event in root.findall('.//{*}trafficElement'):
        event_id = event.findtext('{*}id')
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        comments = {}
        for comment in event.findall('.//{*}comment'):
            kind = comment.findtext('{*}commentType')
            if kind in {'type', 'subtype', 'street', 'report_time'}:
                comments[kind] = comment.findtext('{*}value')
        event_type = comments.get('type')
        if event_type not in titles:
            continue
        reported = _cyprus_road_time(comments.get('report_time'))
        if reported is None or not -300 <= now - reported <= 3 * 3600:
            continue
        try:
            lat = float(event.findtext('.//{*}latitude'))
            lon = float(event.findtext('.//{*}longitude'))
        except (TypeError, ValueError):
            continue
        if not 34.4 <= lat <= 35.8 or not 31.9 <= lon <= 34.8:
            continue
        street = _clean(comments.get('street'), 90)
        subtype = _clean(comments.get('subtype'), 80).replace('_', ' ').capitalize()
        features.append(_feature([lon, lat], {
            'key': f'cy:waze:{hashlib.sha1(event_id.encode("utf-8")).hexdigest()[:16]}',
            'layer': 'incidents', 'title': titles[event_type],
            'detail': ' · '.join(part for part in (street, subtype) if part),
            'source': 'Waze via Cyprus National Access Point · CC BY 4.0',
            'source_url': CYPRUS_WAZE_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(reported, dt.timezone.utc).isoformat(),
        }))
    return features


def _cyprus_waze_alerts():
    return _parse_cyprus_waze_alerts(_get_xml(CYPRUS_WAZE_URL, max_bytes=2 * 1024 * 1024))


def _gdynia_catalog(name):
    cache = _GDYNIA_CATALOGS[name]
    with cache['lock']:
        if time.time() < cache['until']:
            return cache['rows']
        payload = _get_json(GDYNIA_ROADS_BASE + name)
        field = 'weatherStations' if name == 'weather_stations' else name
        rows = payload.get(field) if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError(f'Gdynia {name} catalog is empty or invalid')
        cache.update(until=time.time() + 24 * 3600, rows=rows)
        return rows


def _gdynia_time(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo('Europe/Warsaw'))
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None


def _gdynia_sign_pages(content):
    if not content or len(content) > 128 * 1024:
        return []
    try:
        root = ET.fromstring('<Pages>' + content + '</Pages>')
    except ET.ParseError:
        return []
    pages = []
    for display in root.findall('DisplayValue'):
        lines = [_clean(node.text, 90) for node in display.findall('.//Text/Value')]
        message = ' · '.join(line for line in lines if line)
        if message and message not in pages:
            pages.append(message)
    return pages


@functools.lru_cache(maxsize=256)
def _gdynia_message_pages(message_id):
    if not re.fullmatch(r'\d{1,12}', str(message_id)):
        raise ValueError('Invalid Gdynia sign message ID')
    request = urllib.request.Request(
        f'https://api.zdiz.gdynia.pl/ri/vms/messages/{message_id}',
        headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=10) as response:
        if urllib.parse.urlsplit(response.url).hostname != 'api.zdiz.gdynia.pl':
            raise ValueError('Unexpected Gdynia sign redirect')
        body = response.read(128 * 1024 + 1)
    if len(body) > 128 * 1024:
        raise ValueError('Gdynia sign message exceeded size limit')
    return _gdynia_sign_pages(body.decode('utf-8-sig'))


def _parse_gdynia_signs(devices, messages, pages_for_id):
    if not isinstance(messages, list):
        raise ValueError('Gdynia sign list is invalid')
    locations = {str(row.get('id')): _point(row.get('location'))
                 for row in devices if isinstance(row, dict)}
    features = []
    for row in messages:
        if not isinstance(row, dict):
            continue
        message_id = str(row.get('id') or '')
        sign_id = str(row.get('vmsId') or '')
        if (not re.fullmatch(r'\d{1,12}', message_id)
                or not re.fullmatch(r'\d{1,8}', sign_id)
                or row.get('contentUrl') != f'/ri/vms/messages/{message_id}'):
            continue
        point = locations.get(sign_id)
        if not point or not (18.3 <= point[0] <= 18.9 and 54.2 <= point[1] <= 54.7):
            continue
        pages = pages_for_id(message_id)
        if not pages:
            continue
        changed = _gdynia_time(row.get('insertTime'))
        features.append(_feature(point, {
            'key': f'pl:gdynia:sign:{sign_id}', 'layer': 'signs',
            'title': f'Gdynia message sign {sign_id}',
            'detail': ' / '.join(pages)[:500],
            'source': 'Gdynia ZDiZ · TRISTAR', 'source_url': GDYNIA_ROADS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(changed, dt.timezone.utc).isoformat() if changed else '',
        }))
    return features


def _gdynia_signs():
    devices = _gdynia_catalog('vms')
    messages = _get_json(GDYNIA_ROADS_BASE + 'vms_messages')
    if not isinstance(messages, list):
        raise ValueError('Gdynia sign list is invalid')
    ids = {str(row.get('id')) for row in messages if isinstance(row, dict)
           and row.get('contentUrl') == f"/ri/vms/messages/{row.get('id')}"
           and re.fullmatch(r'\d{1,12}', str(row.get('id')))}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {message_id: executor.submit(_gdynia_message_pages, message_id)
                   for message_id in ids}
        pages = {}
        failures = 0
        for message_id, future in futures.items():
            try:
                pages[message_id] = future.result()
            except (OSError, ValueError, UnicodeError) as error:
                failures += 1
                pages[message_id] = []
    if failures and failures == len(ids):
        raise ValueError('Gdynia sign content is unavailable')
    return _parse_gdynia_signs(devices, messages, lambda message_id: pages.get(message_id, []))


def _parse_gdynia_sensors(segments, speeds, intensities, now=None):
    now = time.time() if now is None else now
    if not isinstance(speeds, list) or not isinstance(intensities, list):
        raise ValueError('Gdynia traffic measurements are invalid')
    points = {str(row.get('id')): _point(row.get('geometry'))
              for row in segments if isinstance(row, dict)}
    readings = {}
    for rows, field, upper in ((speeds, 'speed', 130), (intensities, 'intensity', 10000)):
        for row in rows:
            if not isinstance(row, dict):
                continue
            segment_id = str(row.get('roadSegmentId') or '')
            point = points.get(segment_id)
            measured = _gdynia_time(row.get('measureTime'))
            try:
                value = float(row.get(field))
            except (TypeError, ValueError):
                continue
            if (not point or not (18.3 <= point[0] <= 18.9 and 54.2 <= point[1] <= 54.7)
                    or measured is None or not -300 <= now - measured <= 20 * 60
                    or not 0 <= value <= upper):
                continue
            record = readings.setdefault(segment_id, {'point': point, 'measured': measured})
            record[field] = value
            record['measured'] = max(record['measured'], measured)
    if not readings:
        raise ValueError('Gdynia traffic measurements are stale or empty')
    features = []
    for segment_id, record in readings.items():
        speed = record.get('speed')
        intensity = record.get('intensity')
        detail = ' · '.join(part for part in (
            f'{speed:.0f} km/h average speed' if speed is not None else '',
            f'{intensity:.0f} vehicles/h' if intensity is not None else '') if part)
        features.append(_feature(record['point'], {
            'key': f'pl:gdynia:sensor:{segment_id}', 'layer': 'sensors',
            'title': 'Gdynia traffic sensor', 'detail': detail,
            'source': 'Gdynia ZDiZ · TRISTAR', 'source_url': GDYNIA_ROADS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(record['measured'], dt.timezone.utc).isoformat(),
        }))
    return features


def _gdynia_sensors():
    segments = _gdynia_catalog('road_segments')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        speeds = executor.submit(_get_json, GDYNIA_ROADS_BASE + 'traffic_speeds')
        intensities = executor.submit(_get_json, GDYNIA_ROADS_BASE + 'traffic_intensities')
        return _parse_gdynia_sensors(segments, speeds.result(), intensities.result())


def _parse_gdynia_road_weather(stations, readings, now=None):
    now = time.time() if now is None else now
    if not isinstance(readings, list):
        raise ValueError('Gdynia road weather measurements are invalid')
    locations = {}
    for station in stations:
        if not isinstance(station, dict):
            continue
        point = _point(station.get('location'))
        station_id = str(station.get('id') or '')
        if (point and 18.3 <= point[0] <= 18.9 and 54.2 <= point[1] <= 54.7
                and re.fullmatch(r'\d{1,8}', station_id)):
            locations[station_id] = (point, _clean(station.get('street'), 100))
    features = []
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        station_id = str(reading.get('weatherStationId') or '')
        location = locations.get(station_id)
        measured = _gdynia_time(reading.get('measureTime'))
        if not location or measured is None or not -300 <= now - measured <= 20 * 60:
            continue
        def bounded(field, low, high):
            try:
                value = float(reading[field])
            except (KeyError, TypeError, ValueError):
                return None
            return value if math.isfinite(value) and low <= value <= high else None
        air = bounded('airTemperature', -60, 60)
        surface = bounded('surfaceTemperature', -60, 80)
        visibility = bounded('visibility', 0, 50000)
        wind = bounded('windSpeed', 0, 60)
        detail = ' · '.join(part for part in (
            f'Air {air:.1f}°C' if air is not None else '',
            f'Road surface {surface:.1f}°C' if surface is not None else '',
            f'Visibility {visibility:.0f} m' if visibility is not None else '',
            f'Wind {wind:.1f} m/s' if wind is not None else '') if part)
        if not detail:
            continue
        features.append(_feature(location[0], {
            'key': f'pl:gdynia:weather:{station_id}', 'layer': 'sensors',
            'title': 'Gdynia road weather',
            'detail': ' · '.join(part for part in (location[1], detail) if part),
            'source': 'Gdynia ZDiZ · TRISTAR', 'source_url': GDYNIA_ROADS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(measured, dt.timezone.utc).isoformat(),
        }))
    return features


def _gdynia_road_weather():
    stations = _gdynia_catalog('weather_stations')
    readings = _get_json(GDYNIA_ROADS_BASE + 'weather_stations_data')
    return _parse_gdynia_road_weather(stations, readings)


_TII_CAMERA_CACHE = {'until': 0, 'items': {}}
_TII_CAMERA_LOCK = threading.Lock()
_TII_CAMERA_HEALTH = {'until': 0, 'unavailable': set()}
_TII_CAMERA_HEALTH_LOCK = threading.Lock()
_TII_SIGN_IMAGE_CACHE = {}
_TII_SIGN_IMAGE_LOCK = threading.Lock()


def _tii_point(location):
    if not isinstance(location, dict):
        return None
    try:
        lat, lon = float(location['latitude']), float(location['longitude'])
    except (KeyError, TypeError, ValueError):
        return None
    return [lon, lat] if -11 <= lon <= -5 and 51 <= lat <= 56 else None


def _tii_camera_catalog():
    with _TII_CAMERA_LOCK:
        if time.time() < _TII_CAMERA_CACHE['until']:
            return _TII_CAMERA_CACHE['items']
        payload = _get_json(TII_TRAFFIC_BASE + '/cameras_v1/api/cameras')
        if not isinstance(payload, list):
            raise ValueError('TII camera catalog is invalid')
        items = {}
        for row in payload:
            if not isinstance(row, dict) or not row.get('active') or not row.get('public'):
                continue
            camera_id = str(row.get('id') or '')
            if not re.fullmatch(r'\d{1,5}', camera_id) or not _tii_point(row.get('location')):
                continue
            views = row.get('views') or []
            image = next((view.get('url') for view in views if isinstance(view, dict)
                          and view.get('type') == 'STILL_IMAGE' and
                          re.fullmatch(r'https://irecam\.carsprogram\.org/(?:Vaisala|Kapsch|IBI)/[A-Za-z0-9_-]+\.jpe?g',
                                       str(view.get('url') or ''))), None)
            if image:
                items[camera_id] = (row, image)
        if not items:
            raise ValueError('TII camera catalog has no usable stills')
        _TII_CAMERA_CACHE.update(until=time.time() + 300, items=items)
        return items


def _tii_cameras():
    catalog = _tii_camera_catalog()
    unavailable = _tii_unavailable_cameras(catalog)
    features = []
    for camera_id, (row, _image) in catalog.items():
        if camera_id in unavailable:
            continue
        location = row['location']
        road = _clean(location.get('routeId'), 30)
        name = _clean(row.get('name'), 90)
        features.append(_feature(_tii_point(location), {
            'key': f'ie:tii:camera:{camera_id}', 'layer': 'cameras',
            'title': ' · '.join(part for part in (road, name) if part) or 'Irish road camera',
            'detail': 'Latest available still',
            'snapshot_url': f'/ireland-camera/{camera_id}', 'snapshot_refresh_ms': 300000,
            'source': 'TII · CC BY 4.0',
            'source_url': TII_TRAFFIC_SOURCE + 'list/cameras',
        }))
    return features


def _tii_unavailable_cameras(catalog):
    with _TII_CAMERA_HEALTH_LOCK:
        now = time.time()
        if now < _TII_CAMERA_HEALTH['until']:
            return _TII_CAMERA_HEALTH['unavailable']

        def unavailable(entry):
            camera_id, (_row, url) = entry
            request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'GlobeView/1.0'})
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    if urllib.parse.urlsplit(response.url).hostname != 'irecam.carsprogram.org':
                        return camera_id
                    modified = response.headers.get('Last-Modified')
                    if response.headers.get('Content-Type', '').split(';')[0] != 'image/jpeg' or not modified:
                        return camera_id
                    age = now - email.utils.parsedate_to_datetime(modified).timestamp()
                    return camera_id if not -300 <= age <= 30 * 60 else None
            except urllib.error.HTTPError as error:
                return camera_id if error.code == 404 else None
            except (OSError, ValueError):
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            unavailable_ids = {camera_id for camera_id in executor.map(unavailable, catalog.items()) if camera_id}
        _TII_CAMERA_HEALTH.update(until=now + 3600, unavailable=unavailable_ids)
        return unavailable_ids


def tii_camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{1,5}', str(camera_id)):
        raise ValueError('Invalid TII camera ID')
    entry = _tii_camera_catalog().get(str(camera_id))
    if not entry:
        raise FileNotFoundError('TII camera is not in the public active catalog')
    request = urllib.request.Request(entry[1], headers={'User-Agent': 'GlobeView/1.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        if urllib.parse.urlsplit(response.url).hostname != 'irecam.carsprogram.org':
            raise ValueError('Unexpected TII camera redirect')
        modified = response.headers.get('Last-Modified')
        if modified:
            age = time.time() - email.utils.parsedate_to_datetime(modified).timestamp()
            if not -300 <= age <= 30 * 60:
                raise FileNotFoundError('TII camera still is stale')
        image = response.read(2_000_001)
    if len(image) > 2_000_000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('TII camera returned no JPEG still')
    return image, 'image/jpeg'


def _parse_tii_events(payload, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, list):
        raise ValueError('TII road events are invalid')
    features = []
    for row in payload:
        if not isinstance(row, dict) or not row.get('active'):
            continue
        event_id = str(row.get('id') or '')
        if not re.fullmatch(r'IRE-[A-Za-z0-9-]{4,32}', event_id):
            continue
        location = row.get('location') or {}
        primary = location.get('primaryPoint') or {}
        point = _tii_point({'latitude': primary.get('lat'), 'longitude': primary.get('lon')})
        if not point:
            continue
        begin = (row.get('beginTime') or {}).get('time')
        end = (row.get('endTime') or {}).get('time')
        try:
            if not float(begin) / 1000 <= now <= float(end) / 1000:
                continue
        except (TypeError, ValueError):
            continue
        description = row.get('eventDescription') or {}
        category = _clean(description.get('headlinePhrase'), 40)
        layer = 'construction' if category.casefold() == 'roadworks' else 'incidents'
        title = _clean(description.get('descriptionHeader'), 120) or category or 'Road event'
        detail = _clean(description.get('locationDescription') or description.get('descriptionBrief'), 180)
        updated_ms = (row.get('updateTime') or {}).get('time')
        try:
            updated = dt.datetime.fromtimestamp(float(updated_ms) / 1000, dt.timezone.utc).strftime('%H:%M UTC')
        except (TypeError, ValueError, OverflowError):
            updated = ''
        features.append(_feature(point, {
            'key': f'ie:tii:event:{event_id}', 'layer': layer,
            'title': title, 'detail': detail,
            'source': 'TII · CC BY 4.0',
            'source_url': TII_TRAFFIC_SOURCE + 'list/events', 'updated_at': updated,
        }))
    return features


def _tii_events():
    return _parse_tii_events(_get_json(TII_TRAFFIC_BASE + '/events_v1/api/eventReports'))


def _tii_sign_image(url):
    if not re.fullmatch(r'https://crc-public-eu-west-1-s3\.s3\.eu-west-1\.amazonaws\.com/ire/prod/signs/[A-Za-z0-9_-]+\.PNG', url):
        return None
    now = time.time()
    with _TII_SIGN_IMAGE_LOCK:
        cached = _TII_SIGN_IMAGE_CACHE.get(url)
        if cached and now < cached['until']:
            return cached['image']
    image = None
    updated = None
    try:
        request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'GlobeView/1.0'})
        with urllib.request.urlopen(request, timeout=8) as response:
            if urllib.parse.urlsplit(response.url).hostname != 'crc-public-eu-west-1-s3.s3.eu-west-1.amazonaws.com':
                raise ValueError('Unexpected TII sign redirect')
            updated = email.utils.parsedate_to_datetime(response.headers['Last-Modified']).timestamp()
            if not -300 <= now - updated <= 30 * 60 or int(response.headers.get('Content-Length', '0')) > 100_000:
                updated = None
        if updated is not None:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0'}), timeout=8) as response:
                if urllib.parse.urlsplit(response.url).hostname != 'crc-public-eu-west-1-s3.s3.eu-west-1.amazonaws.com':
                    raise ValueError('Unexpected TII sign redirect')
                body = response.read(100_001)
            if len(body) <= 100_000 and body.startswith(b'\x89PNG\r\n\x1a\n'):
                image = (base64.b64encode(body).decode(), updated)
    except (OSError, ValueError, KeyError):
        pass
    with _TII_SIGN_IMAGE_LOCK:
        _TII_SIGN_IMAGE_CACHE[url] = {'until': now + (180 if image else 1800), 'image': image}
    return image


def _parse_tii_signs(payload, image_loader=_tii_sign_image):
    if not isinstance(payload, list):
        raise ValueError('TII signs are invalid')
    candidates = []
    for row in payload:
        if not isinstance(row, dict) or row.get('status') != 'DISPLAYING_MESSAGE' or (row.get('properties') or {}).get('signType') != 'VMS_IMAGE':
            continue
        sign_id = str(row.get('id') or '')
        point = _tii_point(row.get('location'))
        if not re.fullmatch(r'irelanddot\*[A-Za-z0-9_-]{4,90}', sign_id) or not point:
            continue
        pages = (row.get('display') or {}).get('pages') or []
        url = next((line for page in pages if isinstance(page, dict)
                    for line in page.get('lines', []) if isinstance(line, str)
                    and line.startswith('https://crc-public-eu-west-1-s3.')), None)
        if url:
            candidates.append((row, point, url))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        images = list(executor.map(lambda item: image_loader(item[2]), candidates))
    features = []
    for (row, point, _url), image in zip(candidates, images):
        if not image:
            continue
        image_data, updated = image
        features.append(_feature(point, {
            'key': f'ie:tii:sign:{row["id"]}', 'layer': 'signs',
            'title': _clean(row.get('name'), 110) or 'Irish road sign',
            'detail': 'Current sign display', 'image_data': image_data,
            'source': 'TII · CC BY 4.0',
            'source_url': TII_TRAFFIC_SOURCE + 'list/signs',
            'updated_at': dt.datetime.fromtimestamp(updated, dt.timezone.utc).strftime('%H:%M UTC'),
        }))
    return features


def _tii_signs():
    return _parse_tii_signs(_get_json(TII_TRAFFIC_BASE + '/signs_v1/api/signs'))


def _lithuania_camera_rows():
    cache = _LITHUANIA_CAMERA_CATALOG
    with cache['lock']:
        now = time.time()
        if now < cache['until']:
            return cache['rows']
        rows = _get_json(LITHUANIA_CAMERA_TABLE_URL)
        if not isinstance(rows, list) or not rows:
            raise ValueError('Lithuania camera table is empty')
        cache.update(until=now + 180, rows=rows)
        return rows


def _parse_lithuania_cameras(rows, now=None):
    now = time.time() if now is None else now
    features = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        camera_id = str(row.get('id') or '')
        if (not re.fullmatch(r'\d{1,6}', camera_id) or camera_id in seen or
                row.get('image') != f'https://eismoinfo.lt/eismoinfo-backend/image-provider/camera/last?id={camera_id}'):
            continue
        try:
            captured = float(row['date']) / 1000
            x, y = float(row['x']), float(row['y'])
            lon, lat = _LITHUANIA_TRANSFORMER.transform(x, y)
        except (KeyError, TypeError, ValueError):
            continue
        if not (-300 <= now - captured <= 30 * 60 and
                20.8 <= lon <= 26.9 and 53.8 <= lat <= 56.5):
            continue
        seen.add(camera_id)
        road = _clean(row.get('roadNr'), 24)
        km = row.get('km')
        captured_text = dt.datetime.fromtimestamp(captured, dt.timezone.utc).strftime('%H:%M UTC')
        detail = ' · '.join(part for part in (road, f'km {km}' if km is not None else '',
                                               f'Still {captured_text}') if part)
        features.append(_feature([lon, lat], {
            'key': f'lt:eismoinfo:camera:{camera_id}', 'layer': 'cameras',
            'title': _clean(row.get('name'), 110) or f'Lithuania road camera {camera_id}',
            'detail': detail, 'snapshot_url': f'/lithuania-camera/{camera_id}',
            'snapshot_refresh_ms': 180000,
            'source': 'Via Lietuva · Eismoinfo', 'source_url': LITHUANIA_CAMERA_SOURCE,
        }))
    return features


def _lithuania_cameras():
    return _parse_lithuania_cameras(_lithuania_camera_rows())


def _parse_lithuania_road_weather(rows, now=None):
    if not isinstance(rows, list):
        raise ValueError('Lithuania road weather table is invalid')
    now = time.time() if now is None else now
    features = []
    seen = set()
    surface_labels = {'Sausa': 'Dry', 'Drėgna': 'Damp', 'Šlapia': 'Wet',
                      'Apledėjusi': 'Icy', 'Slidi': 'Slippery'}
    for row in rows:
        if not isinstance(row, dict):
            continue
        station_id = str(row.get('id') or '')
        if not re.fullmatch(r'\d{1,6}', station_id) or station_id in seen:
            continue
        try:
            observed = float(row['date']) / 1000
            lon, lat = _LITHUANIA_TRANSFORMER.transform(float(row['x']), float(row['y']))
        except (KeyError, TypeError, ValueError):
            continue
        if not (-300 <= now - observed <= 30 * 60 and
                20.8 <= lon <= 26.9 and 53.8 <= lat <= 56.5):
            continue
        seen.add(station_id)
        surface = _clean(row.get('surfaceCondition'), 32)
        detail = [f'Surface {surface_labels.get(surface, surface)}' if surface else '']
        for key, label in (('roadTemperature', 'Road'), ('airTemperature', 'Air')):
            try:
                value = float(row[key])
                if math.isfinite(value) and -80 <= value <= 80:
                    detail.append(f'{label} {value:g}°C')
            except (KeyError, TypeError, ValueError):
                pass
        detail.append('Observed ' + dt.datetime.fromtimestamp(observed, dt.timezone.utc).strftime('%H:%M UTC'))
        features.append(_feature([lon, lat], {
            'key': f'lt:eismoinfo:weather:{station_id}', 'layer': 'sensors',
            'title': _clean(row.get('name'), 100) or f'Road weather station {station_id}',
            'detail': ' · '.join(part for part in detail if part),
            'source': 'Via Lietuva · Eismoinfo', 'source_url': LITHUANIA_CAMERA_SOURCE,
        }))
    return features


def _lithuania_road_weather():
    return _parse_lithuania_road_weather(_get_json(LITHUANIA_ROAD_WEATHER_URL))


_ESTONIA_CAMERA_INDEX = {'until': 0, 'entries': {}, 'lock': threading.Lock()}
_ESTONIA_CAMERA_LOCATIONS = {'until': 0, 'source': None, 'payload': None,
                             'read_at': 0, 'lock': threading.Lock()}
_ESTONIA_VMS_SITES = {'until': 0, 'root': None, 'read_at': 0, 'lock': threading.Lock()}
_ESTONIA_WEATHER_SITES = {'until': 0, 'root': None, 'read_at': 0, 'lock': threading.Lock()}


def _estonia_xml(path):
    api_key = os.getenv('TARKTEE_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('Estonian Tark Tee live DATEX feeds require TARKTEE_API_KEY')
    for attempt in range(2):
        try:
            return _get_xml(ESTONIA_CAMERAS_BASE + path, timeout=15,
                            extra_headers={'X-DATEX-API-KEY': api_key})
        except (TimeoutError, urllib.error.URLError):
            if attempt:
                raise


def _estonia_json(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname not in {'tarktee.transpordiamet.ee', 'tarktee.ee'}:
        raise ValueError('Unexpected Estonia feed host')
    if not parsed.path.startswith('/api/v1/datex/'):
        return _get_json(url)
    api_key = os.getenv('TARKTEE_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('Estonian Tark Tee live DATEX feeds require TARKTEE_API_KEY')
    for attempt in range(2):
        try:
            return _get_json(url, extra_headers={'X-DATEX-API-KEY': api_key})
        except (TimeoutError, urllib.error.URLError):
            if attempt:
                raise


def _estonia_publication_time(root, now):
    if root.tag.rsplit('}', 1)[-1] != 'd2LogicalModel':
        raise ValueError('Estonia camera publication is invalid')
    published = _timestamp(root.findtext('.//{*}payloadPublication/{*}publicationTime'))
    if published is None or not -300 <= now - published <= 30 * 60:
        raise ValueError('Estonia camera publication is stale')
    return published


def _parse_estonia_camera_index(root, now=None):
    now = time.time() if now is None else now
    _estonia_publication_time(root, now)
    entries = {}
    for view in root.findall('.//{*}trafficView'):
        match = re.fullmatch(r'(\d{1,6})-\d+', view.get('id') or '')
        reference = view.find('.//{*}linearPredefinedLocationReference')
        url = (view.findtext('.//{*}urlLinkAddress') or '').strip()
        captured = _timestamp(view.findtext('{*}trafficViewTime'))
        if not match or reference is None or not reference.get('id') or captured is None:
            continue
        camera_id = match.group(1)
        if (not re.fullmatch(r'https://tarktee\.transpordiamet\.ee/images/'
                             + re.escape(camera_id) + r'/' + re.escape(camera_id)
                             + r'_\d{12}\.jpg', url)
                or view.findtext('.//{*}urlLinkType') != 'image'
                or not -300 <= now - captured <= 30 * 60):
            continue
        entries[camera_id] = (reference.get('id'), url, captured)
    if not entries:
        raise ValueError('Estonia camera publication contains no current images')
    return entries


def _estonia_camera_index():
    cache = _ESTONIA_CAMERA_INDEX
    with cache['lock']:
        now = time.time()
        if now < cache['until']:
            return cache['entries']
        try:
            root = _estonia_xml('roadCameraImages')
            entries = _parse_estonia_camera_index(root, now)
        except (OSError, ValueError):
            recent = {key: value for key, value in cache['entries'].items()
                      if -300 <= now - value[2] <= 30 * 60}
            if not recent:
                raise
            cache.update(until=now + 60, entries=recent)
            return recent
        cache.update(until=time.time() + 600, entries=entries)
        return entries


def _parse_estonia_cameras(root, entries, now=None):
    now = time.time() if now is None else now
    _estonia_publication_time(root, now)
    locations = {}
    for location in root.findall('.//{*}predefinedLocation'):
        reference = location.get('id')
        try:
            lat = float(location.findtext('.//{*}pointCoordinates/{*}latitude'))
            lon = float(location.findtext('.//{*}pointCoordinates/{*}longitude'))
        except (TypeError, ValueError):
            continue
        if reference and 57.4 <= lat <= 59.9 and 21.5 <= lon <= 28.3:
            name = _clean(location.findtext('.//{*}predefinedLocationName/{*}values/{*}value'), 90)
            locations[reference] = ([lon, lat], name)
    features = []
    for camera_id, (reference, _, _) in entries.items():
        if reference not in locations:
            continue
        point, name = locations[reference]
        features.append(_feature(point, {
            'key': f'ee:tarktee:camera:{camera_id}', 'layer': 'cameras',
            'title': f'Traffic camera · {name}' if name else 'Traffic camera',
            'detail': 'Road camera still · updated periodically',
            'snapshot_url': f'/estonia-camera/{camera_id}',
            'snapshot_refresh_ms': 600000,
            'source': 'Estonian Transport Administration · Tark Tee',
            'source_url': ESTONIA_RESTRICTIONS_SOURCE,
        }))
    if not features:
        raise ValueError('Estonia camera locations have no current images')
    return features


def _parse_estonia_arcgis_cameras(payload, entries):
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(payload.get('features'), list) or payload.get('exceededTransferLimit')):
        raise ValueError('Estonia camera location catalog is invalid')
    locations = {}
    for item in payload['features']:
        if not isinstance(item, dict):
            continue
        props = item.get('properties') or {}
        if not isinstance(props, dict):
            continue
        match = re.fullmatch(r'(\d{1,6})/\1_\d{12}\.jpg', str(props.get('image_path') or ''))
        point = _point(item.get('geometry'))
        if match and point and 21.5 <= point[0] <= 28.3 and 57.4 <= point[1] <= 59.9:
            locations[match.group(1)] = (point, _clean(props.get('site_name'), 90))
    features = []
    for camera_id in entries:
        if camera_id not in locations:
            continue
        point, name = locations[camera_id]
        features.append(_feature(point, {
            'key': f'ee:tarktee:camera:{camera_id}', 'layer': 'cameras',
            'title': f'Traffic camera · {name}' if name else 'Traffic camera',
            'detail': 'Road camera still · updated periodically',
            'snapshot_url': f'/estonia-camera/{camera_id}',
            'snapshot_refresh_ms': 600000,
            'source': 'Estonian Transport Administration · Tark Tee',
            'source_url': ESTONIA_RESTRICTIONS_SOURCE,
        }))
    if not features:
        raise ValueError('Estonia camera location catalog has no current images')
    return features


def _estonia_cameras():
    entries = _estonia_camera_index()
    cache = _ESTONIA_CAMERA_LOCATIONS
    with cache['lock']:
        if time.time() >= cache['until']:
            read_at = time.time()
            try:
                payload = _estonia_xml('roadCameraLocations')
                _estonia_publication_time(payload, read_at)
                source = 'datex'
            except (OSError, ValueError):
                payload = _estonia_json(ESTONIA_CAMERAS_ARCGIS)
                source = 'arcgis'
            cache.update(until=time.time() + 6 * 3600, source=source,
                         payload=payload, read_at=read_at)
        if cache['source'] == 'datex':
            return _parse_estonia_cameras(cache['payload'], entries, cache['read_at'])
        return _parse_estonia_arcgis_cameras(cache['payload'], entries)


def estonia_camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{1,6}', str(camera_id)):
        raise ValueError('Invalid Estonia camera ID')
    entry = _estonia_camera_index().get(str(camera_id))
    if not entry:
        raise FileNotFoundError('Estonia camera has no recent still')
    _, url, captured = entry
    if not -300 <= time.time() - captured <= 30 * 60:
        raise FileNotFoundError('Estonia camera still is stale')
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=12) as response:
        if (response.url != url or response.headers.get('Content-Type', '').split(';')[0] != 'image/jpeg'):
            raise ValueError('Estonia camera returned an unexpected image')
        image = response.read(1_000_001)
    if len(image) > 1_000_000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('Estonia camera returned no JPEG still')
    return image, 'image/jpeg'


def _parse_estonia_signs(sites_root, messages_root, sites_read_at, now=None):
    now = time.time() if now is None else now
    _estonia_publication_time(sites_root, sites_read_at)
    _estonia_publication_time(messages_root, now)
    sites = {}
    for record in sites_root.findall('.//{*}vmsUnitRecord'):
        sign_id = record.get('id')
        try:
            lat = float(record.findtext('.//{*}vmsLocation/{*}pointByCoordinates/{*}pointCoordinates/{*}latitude'))
            lon = float(record.findtext('.//{*}vmsLocation/{*}pointByCoordinates/{*}pointCoordinates/{*}longitude'))
        except (TypeError, ValueError):
            continue
        if sign_id and 57.4 <= lat <= 59.9 and 21.5 <= lon <= 28.3:
            sites[sign_id] = ([lon, lat], _clean(record.findtext('{*}vmsUnitIdentifier'), 100))
    features = []
    seen = set()
    for unit in messages_root.findall('.//{*}vmsUnit'):
        reference = unit.find('{*}vmsUnitReference')
        sign_id = reference.get('id') if reference is not None else None
        if sign_id not in sites or sign_id in seen:
            continue
        pages = []
        for display in unit.findall('{*}vms'):
            if display.findtext('.//{*}vmsWorking') != 'true':
                continue
            for page in display.findall('.//{*}textPage'):
                lines = [_clean(line.text, 80) for line in page.findall('.//{*}vmsTextLine')
                         if (line.text or '').strip()]
                message = ' / '.join(line for line in lines if line)
                if message and message not in pages:
                    pages.append(message)
        if not pages:
            continue
        seen.add(sign_id)
        point, name = sites[sign_id]
        features.append(_feature(point, {
            'key': f'ee:tarktee:sign:{sign_id}', 'layer': 'signs',
            'title': _clean(pages[0], 110),
            'detail': ' · '.join(part for part in (name, ' | '.join(pages[1:])) if part),
            'source': 'Estonian Transport Administration · Tark Tee',
            'source_url': ESTONIA_RESTRICTIONS_SOURCE,
        }))
    return features


def _estonia_signs():
    cache = _ESTONIA_VMS_SITES
    with cache['lock']:
        if time.time() >= cache['until']:
            read_at = time.time()
            root = _estonia_xml('vmsSites')
            _estonia_publication_time(root, read_at)
            cache.update(until=time.time() + 6 * 3600, root=root, read_at=read_at)
        sites_root, read_at = cache['root'], cache['read_at']
    messages_root = _estonia_xml('vms')
    return _parse_estonia_signs(sites_root, messages_root, read_at)


def _parse_estonia_weather(sites_root, weather_root, sites_read_at, now=None):
    now = time.time() if now is None else now
    _estonia_publication_time(sites_root, sites_read_at)
    _estonia_publication_time(weather_root, now)
    sites = {}
    for table in sites_root.findall('.//{*}measurementSiteTable'):
        if table.get('id') != 'WEATHER_STATION_SITES':
            continue
        for record in table.findall('{*}measurementSiteRecord'):
            station_id = record.get('id')
            try:
                lat = float(record.findtext('.//{*}pointCoordinates/{*}latitude'))
                lon = float(record.findtext('.//{*}pointCoordinates/{*}longitude'))
            except (TypeError, ValueError):
                continue
            if station_id and 57.4 <= lat <= 59.9 and 21.5 <= lon <= 28.3:
                name = _clean(record.findtext('.//{*}measurementSiteName/{*}values/{*}value'), 90)
                sites[station_id] = ([lon, lat], name)
    features = []
    for measurement in weather_root.findall('.//{*}siteMeasurements'):
        reference = measurement.find('{*}measurementSiteReference')
        station_id = reference.get('id') if reference is not None else None
        if station_id not in sites:
            continue
        observed = _timestamp(measurement.findtext('{*}measurementTimeDefault'))
        if observed is None or not -300 <= now - observed <= 30 * 60:
            continue
        def value(path, low, high):
            try:
                number = float(measurement.findtext(path))
            except (TypeError, ValueError):
                return None
            return number if math.isfinite(number) and low <= number <= high else None
        air = value('.//{*}airTemperature/{*}temperature', -80, 60)
        road = value('.//{*}roadSurfaceTemperature/{*}temperature', -80, 90)
        wind = value('.//{*}windSpeed/{*}speed', 0, 100)
        surface = _clean(measurement.findtext('.//{*}weatherRelatedRoadConditionType'), 50)
        if air is None and road is None and wind is None and not surface:
            continue
        point, name = sites[station_id]
        observed_label = dt.datetime.fromtimestamp(observed, dt.timezone.utc).strftime('%H:%M UTC')
        detail = [f'Air {air:g}°C' if air is not None else '',
                  f'Road {road:g}°C' if road is not None else '',
                  surface.replace('slipperyRoad', 'Slippery').replace('snowOnTheRoad', 'Snow on road').capitalize(),
                  f'Wind {wind:g} m/s' if wind is not None else '', observed_label]
        features.append(_feature(point, {
            'key': f'ee:tarktee:weather:{station_id}', 'layer': 'sensors',
            'title': f'Road weather · {name}' if name else 'Road weather station',
            'detail': ' · '.join(item for item in detail if item),
            'source': 'Estonian Transport Administration · Tark Tee',
            'source_url': ESTONIA_RESTRICTIONS_SOURCE,
        }))
    return features


def _estonia_weather():
    cache = _ESTONIA_WEATHER_SITES
    with cache['lock']:
        if time.time() >= cache['until']:
            read_at = time.time()
            root = _estonia_xml('measurementSites')
            _estonia_publication_time(root, read_at)
            cache.update(until=time.time() + 6 * 3600, root=root, read_at=read_at)
        sites_root, read_at = cache['root'], cache['read_at']
    weather_root = _estonia_xml('weatherData')
    return _parse_estonia_weather(sites_root, weather_root, read_at)


def _parse_estonia_restrictions(payload, now=None):
    if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection' or not isinstance(payload.get('features'), list):
        raise ValueError('Estonia traffic restrictions feed is invalid')
    if payload.get('exceededTransferLimit'):
        raise ValueError('Estonia traffic restrictions result is incomplete')
    now = time.time() if now is None else now
    roadworks = {'CONSTRUCTION', 'UTILITY_COMS_CONSTRUCTION', 'GRAVEL_ROAD_REPAIRS',
                 'CULVERT_REPAIRS', 'PAVING', 'ROAD_SURFACE_MARKING', 'BARRIER_WORKS',
                 'STORAGE_OF_MATERIALS'}
    effects = {'COMPLETE_CLOSURE': 'Road closed', 'ONE_WAY_CLOSED': 'One direction closed',
               'LANE_CLOSED': 'Lane closed', 'SPEED_LIMITED': 'Reduced speed limit'}
    features = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        props = row.get('properties') or {}
        if not isinstance(props, dict):
            continue
        event_id = props.get('objectid')
        point = _point(row.get('geometry'))
        if (not isinstance(event_id, int) or event_id < 1 or event_id in seen or not point
                or not (21.5 <= point[0] <= 28.3 and 57.4 <= point[1] <= 59.9)):
            continue
        try:
            start = float(props['date_from']) / 1000
            end = float(props['date_to']) / 1000
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end) and start <= now < end):
            continue
        seen.add(event_id)
        cause = str(props.get('cause') or '').upper()
        layer = 'construction' if cause in roadworks else 'incidents'
        road = _clean(props.get('road_name'), 90)
        road_nr = props.get('road_nr')
        if not road and isinstance(road_nr, int):
            road = f'Road {road_nr}'
        title = ('Roadworks' if layer == 'construction' else 'Road restriction')
        if road:
            title += f' · {road}'
        effect = effects.get(str(props.get('effect') or '').upper(), '')
        extra = _clean(props.get('extra_info'), 280)
        end_label = dt.datetime.fromtimestamp(end, dt.timezone.utc).strftime('%d %b %Y')
        features.append(_feature(point, {
            'key': f'ee:tarktee:restriction:{event_id}', 'layer': layer,
            'title': title,
            'detail': ' · '.join(part for part in (effect, extra, f'Until {end_label}') if part),
            'source': 'Estonian Transport Administration · Tark Tee',
            'source_url': ESTONIA_RESTRICTIONS_SOURCE,
        }))
    return features


def _estonia_restrictions():
    # Request a small recent window; verify each record's actual start/end below.
    cutoff = dt.datetime.fromtimestamp(time.time() - 86400, dt.timezone.utc)
    where = "date_to > TIMESTAMP '" + cutoff.strftime('%Y-%m-%d %H:%M:%S') + "'"
    query = urllib.parse.urlencode({
        'where': where, 'outFields': 'objectid,road_nr,road_name,cause,effect,extra_info,date_from,date_to',
        'returnGeometry': 'true', 'outSR': '4326', 'resultRecordCount': '2000', 'f': 'geojson',
    })
    return _parse_estonia_restrictions(_estonia_json(ESTONIA_RESTRICTIONS_URL + '?' + query))


def _parse_brno_waze_alerts(payload, now=None):
    now = time.time() if now is None else now
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(payload.get('features'), list)
            or not isinstance(payload.get('properties'), dict)
            or payload['properties'].get('exceededTransferLimit')
            or len(payload['features']) > 500):
        raise ValueError('Brno road alert publication is invalid or incomplete')
    output = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict):
            continue
        props = row.get('properties') or {}
        point = _point(row.get('geometry'))
        if not isinstance(props, dict) or not point or not (16.2 <= point[0] <= 16.9 and 48.9 <= point[1] <= 49.5):
            continue
        alert_id = str(props.get('uuid') or '')
        if not re.fullmatch(r'[a-fA-F0-9-]{36}', alert_id) or alert_id in seen:
            continue
        try:
            published = float(props['pubMillis']) / 1000
        except (KeyError, TypeError, ValueError):
            continue
        if not -300 <= now - published <= 6 * 3600:
            continue
        kind = str(props.get('type') or '')
        subtype = str(props.get('subtype') or '')
        if kind == 'JAM':
            continue  # Traffic flow already has its own layer.
        if kind == 'ACCIDENT':
            title, layer = 'Reported crash', 'incidents'
        elif kind == 'ROAD_CLOSED':
            title, layer = 'Reported road closure', 'incidents'
        elif kind == 'HAZARD':
            labels = {
                'HAZARD_ON_ROAD_CONSTRUCTION': ('Reported roadworks', 'construction'),
                'HAZARD_ON_ROAD_LANE_CLOSED': ('Reported lane closure', 'incidents'),
                'HAZARD_ON_ROAD_TRAFFIC_LIGHT_FAULT': ('Reported traffic signal fault', 'incidents'),
                'HAZARD_ON_SHOULDER_CAR_STOPPED': ('Reported stopped vehicle', 'incidents'),
                'HAZARD_ON_ROAD_OBJECT': ('Reported road obstruction', 'incidents'),
                'HAZARD_ON_ROAD_POT_HOLE': ('Reported pothole', 'incidents'),
            }
            title, layer = labels.get(subtype, ('Reported road hazard', 'incidents'))
        else:
            continue
        seen.add(alert_id)
        street = _clean(props.get('street'), 80)
        city = _clean(props.get('city'), 60)
        description = _clean(props.get('reportDescription'), 120)
        output.append(_feature(point, {
            'key': f'cz:brno:waze:{alert_id}', 'layer': layer,
            'title': title + (f' · {street}' if street else ''),
            'detail': ' · '.join(part for part in (city, description, 'User report · unverified') if part),
            'source': 'Waze · City of Brno · CC BY 4.0', 'source_url': BRNO_WAZE_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(published, dt.timezone.utc).strftime('%d %b %H:%M UTC'),
        }))
    return output


def _brno_waze_alerts():
    cutoff = int((time.time() - 6 * 3600) * 1000)
    query = urllib.parse.urlencode({
        'where': f'pubMillis >= {cutoff}',
        'outFields': 'uuid,pubMillis,type,subtype,street,city,reportDescription',
        'returnGeometry': 'true', 'outSR': '4326', 'resultRecordCount': 500,
        'f': 'geojson',
    })
    return _parse_brno_waze_alerts(_get_json(BRNO_WAZE_URL + '?' + query))


def _parse_bratislava_roadworks(payload, now=None):
    now = time.time() if now is None else now
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(payload.get('features'), list)
            or payload.get('exceededTransferLimit') or len(payload['features']) > 2000):
        raise ValueError('Bratislava road restriction publication is invalid or incomplete')
    features = []
    seen = set()
    for row in payload['features']:
        if not isinstance(row, dict) or not isinstance(row.get('properties'), dict):
            continue
        props = row['properties']
        point = _point(row.get('geometry'))
        if not point or not (16.95 <= point[0] <= 17.35 and 48.03 <= point[1] <= 48.30):
            continue
        work_id = str(props.get('OBJECTID') or '')
        if not work_id.isdecimal() or work_id in seen or props.get('zobrazovanie') != 'Zobrazovat':
            continue
        closure = props.get('uzavierka')
        if closure not in {'čiastočná', 'úplná'}:
            continue
        affected = str(props.get('vplyv_obmedzenia') or '').split(',')
        if not {'Auta', 'Verejna_doprava'}.intersection(affected):
            continue
        try:
            start = float(props.get('potvrdeny_termin_realizacie') or props['datum_vzniku']) / 1000
            end = float(props['termin_finalnej_upravy']) / 1000
        except (KeyError, TypeError, ValueError):
            continue
        if not start <= now <= end or end <= start:
            continue
        seen.add(work_id)
        street = _clean(props.get('adresa_rozkopavky'), 90)
        subject = _clean(props.get('predmet_nadpis'), 110)
        affected_labels = [label for code, label in (('Auta', 'drivers'),
                                                   ('Verejna_doprava', 'public transport'))
                           if code in affected]
        end_date = dt.datetime.fromtimestamp(end, ZoneInfo('Europe/Bratislava')).strftime('%d %b %Y')
        detail = ' · '.join(part for part in (
            subject, f'Affects {" and ".join(affected_labels)}',
            f'Permit ends {end_date}', 'Scheduled permit; not confirmed live',
        ) if part)
        features.append(_feature(point, {
            'key': f'sk:bratislava:works:{work_id}', 'layer': 'construction',
            'title': f'{"Full" if closure == "úplná" else "Partial"} road closure'
                     + (f' · {street}' if street else ''),
            'detail': detail, 'source': 'City of Bratislava',
            'source_url': BRATISLAVA_WORKS_SOURCE,
        }))
    return features


def _bratislava_roadworks():
    now = time.time()
    stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    query = urllib.parse.urlencode({
        'where': f"termin_finalnej_upravy >= TIMESTAMP '{stamp}' "
                 "AND zobrazovanie = 'Zobrazovat'",
        'outFields': ('OBJECTID,datum_vzniku,potvrdeny_termin_realizacie,'
                      'termin_finalnej_upravy,uzavierka,zobrazovanie,'
                      'vplyv_obmedzenia,predmet_nadpis,adresa_rozkopavky'),
        'returnGeometry': 'true', 'outSR': '4326', 'resultRecordCount': '2000',
        'f': 'geojson',
    })
    return _parse_bratislava_roadworks(_get_json(BRATISLAVA_WORKS_URL + '?' + query), now)


def _cz_ndic_time(value):
    try:
        return dt.datetime.strptime(str(value), '%d.%m.%Y %H:%M').replace(
            tzinfo=ZoneInfo('Europe/Prague')).timestamp()
    except (TypeError, ValueError):
        return None


def _parse_cz_ndic_roads(pages, now=None):
    now = time.time() if now is None else now
    if not pages or any(not isinstance(page, dict) or page.get('type') != 'FeatureCollection'
                        or not isinstance(page.get('features'), list) for page in pages):
        raise ValueError('Czech road restriction feed is invalid')
    if pages[-1].get('exceededTransferLimit'):
        raise ValueError('Czech road restriction feed is incomplete')
    rows = [row for page in pages for row in page['features']]
    if not rows:
        raise ValueError('Czech road restriction feed is empty')
    try:
        published_times = {_cz_ndic_time(row['properties']['datum_aktualizace']) for row in rows}
    except (KeyError, TypeError):
        raise ValueError('Czech road restriction publication is invalid') from None
    if len(published_times) != 1 or None in published_times:
        raise ValueError('Czech road restriction publication changed during fetch')
    published = published_times.pop()
    if not -300 <= now - published <= 3600:
        raise ValueError('Czech road restriction feed is stale')
    features = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        props = row.get('properties') or {}
        if not isinstance(props, dict):
            continue
        event_id = str(props.get('msgid') or '')
        point = _point(row.get('geometry'))
        if (not re.fullmatch(r'[a-fA-F0-9-]{36}', event_id) or event_id in seen
                or not point or not (12 <= point[0] <= 19 and 48.4 <= point[1] <= 51.2)):
            continue
        start = _cz_ndic_time(props.get('zacatek'))
        end = _cz_ndic_time(props.get('konec'))
        if start is None or end is None or not start <= now < end:
            continue
        seen.add(event_id)
        categories = {props.get(f'trida_popis{index}') for index in (1, 2, 3)}
        layer = 'construction' if 'Práce na silnici' in categories else 'incidents'
        road = _clean(props.get('cislo_silnice') or props.get('mesto'), 75)
        event = _clean(props.get('txtmce') or props.get('event_popis1'), 100)
        place = _clean(props.get('txpl_text'), 125)
        until = dt.datetime.fromtimestamp(end, ZoneInfo('Europe/Prague')).strftime('%d %b %Y')
        features.append(_feature(point, {
            'key': f'cz:ndic:road:{event_id}', 'layer': layer,
            'title': ('Roadworks' if layer == 'construction' else 'Road restriction')
                     + (f' · {road}' if road else ''),
            'detail': ' · '.join(part for part in (event, place, f'Until {until}') if part),
            'source': 'Czech NDIC · GIS Brno', 'source_url': CZ_NDIC_ROADS_SOURCE,
            'updated_at': dt.datetime.fromtimestamp(published, dt.timezone.utc).strftime('%d %b %H:%M UTC'),
        }))
    return features


def _cz_ndic_roads():
    pages = []
    for offset in range(0, 5000, 1000):
        query = urllib.parse.urlencode({
            'where': '1=1', 'outFields': 'ogc_fid,msgid,zacatek,konec,datum_aktualizace,'
            'trida_popis1,trida_popis2,trida_popis3,event_popis1,txtmce,txpl_text,cislo_silnice,mesto',
            'returnGeometry': 'true', 'outSR': '4326', 'orderByFields': 'ogc_fid',
            'resultOffset': offset, 'resultRecordCount': 1000, 'f': 'geojson',
        })
        page = _get_json(CZ_NDIC_ROADS_URL + '?' + query)
        pages.append(page)
        if not isinstance(page, dict) or not page.get('exceededTransferLimit'):
            break
    return _parse_cz_ndic_roads(pages)


def _prague_time(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo('Europe/Prague'))
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _parse_prague_roadworks(payload, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise ValueError('Prague road restriction feed is invalid')
    rows = payload['data']
    if not rows or len(rows) > 3000:
        raise ValueError('Prague road restriction feed has an invalid size')
    features = []
    for row in rows:
        if not isinstance(row, dict) or row.get('state') != 'ACTIVE' or row.get('approved') is not True or row.get('hidden') is True:
            continue
        start, end = _prague_time(row.get('start')), _prague_time(row.get('end'))
        if start is None or end is None or not start <= now <= end:
            continue
        try:
            event_id = int(row['id'])
            point = [float(row['lon']), float(row['lat'])]
        except (KeyError, TypeError, ValueError):
            continue
        if event_id <= 0 or not (13.9 <= point[0] <= 14.8 and 49.8 <= point[1] <= 50.3):
            continue
        road = _clean(row.get('street'), 80)
        name = _clean(row.get('name'), 140)
        subject = row.get('subject')
        layer = 'construction' if subject == 'CONSTRUCTION' else 'incidents'
        until = dt.datetime.fromtimestamp(end, ZoneInfo('Europe/Prague')).strftime('%d %b %Y')
        detail = ' · '.join(part for part in ('Scheduled road restriction', name, f'Until {until}') if part)
        features.append(_feature(point, {
            'key': f'cz:prague:road:{event_id}', 'layer': layer,
            'title': ('Roadworks' if layer == 'construction' else 'Road restriction')
                     + (f' · {road}' if road else ''),
            'detail': detail,
            'source': 'City of Prague · opravujeme.to',
            'source_url': f'https://opravujeme.to/action/{event_id}/',
        }))
    return features


def _prague_roadworks():
    cache = _PRAGUE_ROADS_CACHE
    with cache['lock']:
        if cache['payload'] is None or time.time() >= cache['until']:
            payload = _get_json(PRAGUE_ROADS_URL)
            if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                raise ValueError('Prague road restriction feed is invalid')
            cache.update(payload=payload, until=time.time() + 1800)
        payload = cache['payload']
    return _parse_prague_roadworks(payload)


def lithuania_camera_snapshot(camera_id):
    camera_id = str(camera_id)
    if not re.fullmatch(r'\d{1,6}', camera_id):
        raise ValueError('Invalid Lithuania camera ID')
    if not any(item['properties']['key'].endswith(f':{camera_id}') for item in _lithuania_cameras()):
        raise FileNotFoundError('Lithuania camera has no recent still')
    url = f'https://eismoinfo.lt/eismoinfo-backend/image-provider/camera/last?id={camera_id}'
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'image/jpeg'})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.url != url or response.headers.get('Content-Type', '').split(';')[0] != 'image/jpeg':
            raise ValueError('Lithuania camera returned an unexpected response')
        image = response.read(2_000_001)
    if len(image) > 2_000_000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('Lithuania camera returned no JPEG still')
    return image, 'image/jpeg'


def _parse_lithuania_restrictions(payload):
    if (not isinstance(payload, list) or len(payload) != 1 or
            not isinstance(payload[0], dict) or payload[0].get('layer') != 'EAL'):
        raise ValueError('Lithuania restrictions response is invalid')
    rows = payload[0].get('features')
    if not isinstance(rows, list):
        raise ValueError('Lithuania restrictions have no feature list')
    features = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        event_id = str(row.get('id') or '')
        if not re.fullmatch(r'(MJ|OB):\d{1,7}', event_id) or event_id in seen:
            continue
        positions = row.get('points') or []
        try:
            x, y = map(float, positions[0]['point'][:2])
            lon, lat = _LITHUANIA_TRANSFORMER.transform(x, y)
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if not (20.8 <= lon <= 26.9 and 53.8 <= lat <= 56.5):
            continue
        seen.add(event_id)
        works = event_id.startswith('MJ:')
        features.append(_feature([lon, lat], {
            'key': f'lt:eismoinfo:event:{event_id}',
            'layer': 'construction' if works else 'incidents',
            'title': 'Roadworks' if works else 'Road obstruction',
            'detail': 'Reported temporary restriction',
            'lithuania_event_id': event_id,
            'source': 'Via Lietuva · Eismoinfo', 'source_url': LITHUANIA_CAMERA_SOURCE,
        }))
    return features


def _lithuania_restrictions():
    return _parse_lithuania_restrictions(_get_json(LITHUANIA_RESTRICTIONS_URL))


def lithuania_event_detail(event_id, now=None):
    if not re.fullmatch(r'(MJ|OB):\d{1,7}', str(event_id)):
        raise ValueError('Invalid Lithuania road event ID')
    url = ('https://eismoinfo.lt/eismoinfo-backend/feature-info/EAL/'
           + urllib.parse.quote(event_id, safe=''))
    payload = _get_json(url)
    if not isinstance(payload, dict) or not isinstance(payload.get('info'), list):
        raise ValueError('Lithuania road event details are invalid')
    info = next((part for part in payload['info'] if isinstance(part, dict)), None)
    if not info:
        raise FileNotFoundError('Lithuania road event details are missing')
    fields = {item.get('key'): _clean(item.get('value'), 180)
              for item in info.get('keyValue', []) if isinstance(item, dict)}
    period = fields.get('Date', '')
    end_label = ''
    if ' - ' in period:
        try:
            start_text, end_text = period.split(' - ', 1)
            start = dt.datetime.strptime(start_text, '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Europe/Vilnius'))
            end = dt.datetime.strptime(end_text, '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Europe/Vilnius'))
            current = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc)
            if not start <= current <= end:
                raise FileNotFoundError('Lithuania road event is outside its stated validity')
            end_label = f'Until {end:%-d %b}'
        except ValueError:
            pass
    description = _clean(str(info.get('text') or '').split('Darbų vykdytojas')[0], 180)
    return {'title': _clean(payload.get('name'), 90) or 'Road restriction',
            'detail': ' · '.join(part for part in (fields.get('Place', ''), description, end_label) if part)}


def _parse_zaragoza_roadworks(payload, now=None):
    """Map only current, located city-reported closures and clear road impacts."""
    now = dt.datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo('Europe/Madrid')).date()
    rows = payload.get('result') if isinstance(payload, dict) else None
    if (not isinstance(rows, list) or not isinstance(payload.get('totalCount'), int)
            or payload['totalCount'] != len(rows) or len(rows) > 500):
        raise ValueError('Zaragoza roadwork publication is incomplete')
    features = []
    seen = set()
    road_impact = re.compile(r'\b(?:calzada|carril|tr[aá]fico|circulaci[oó]n|cruce|'
                             r'asfalt\w*|paviment\w*|rotonda|puente|isleta)\b', re.I)
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get('id')
        category = (row.get('tipo') or {}).get('id') if isinstance(row.get('tipo'), dict) else None
        if not isinstance(identifier, int) or not 0 < identifier < 1_000_000_000 or identifier in seen:
            continue
        if category not in (1, 2):
            continue
        reason = _clean(row.get('motivo'), 140)
        effect = _clean(row.get('observaciones'), 180)
        if category == 2 and not road_impact.search(f'{reason} {effect}'):
            continue
        try:
            start = dt.datetime.fromisoformat(str(row.get('inicio'))).date()
            end = dt.datetime.fromisoformat(str(row.get('fin'))).date()
        except ValueError:
            continue
        if not start <= now <= end:
            continue
        point = _point(row.get('geometry'))
        if not point or not (-1.05 <= point[0] <= -0.70 and 41.50 <= point[1] <= 41.80):
            continue
        title = _clean(row.get('title') or row.get('calle'), 90)
        if not title:
            continue
        updated = None
        if row.get('lastUpdated'):
            try:
                updated = dt.datetime.fromisoformat(str(row['lastUpdated']))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=ZoneInfo('Europe/Madrid'))
                updated = updated.timestamp()
            except ValueError:
                pass
        detail = 'Scheduled road closure' if category == 1 else 'Scheduled road impact'
        if reason:
            detail += ' · ' + reason
        detail += ' · Through ' + end.isoformat()
        properties = {'key': f'zgz:{identifier}', 'layer': 'construction', 'title': title,
                      'detail': detail, 'source': 'Ayuntamiento de Zaragoza',
                      'source_url': f'https://www.zaragoza.es/sede/servicio/via-publica/incidencia/{identifier}'}
        if updated is not None:
            properties['updated_at'] = updated
        features.append(_feature(point, properties))
        seen.add(identifier)
    return features


def _zaragoza_roadworks():
    return _parse_zaragoza_roadworks(_get_json(ZARAGOZA_ROADS_URL))


def _hong_kong_time(value):
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=ZoneInfo('Asia/Hong_Kong')).timestamp()
    except ValueError:
        try:
            return dt.datetime.strptime(value, '%Y-%m-%d %H:%M').replace(
                tzinfo=ZoneInfo('Asia/Hong_Kong')).timestamp()
        except ValueError:
            return None


def _parse_hong_kong_roadworks(payload, now=None):
    now = time.time() if now is None else now
    rows = payload.get('features') if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection'
            or not isinstance(rows, list) or len(rows) > 2000):
        raise ValueError('Hong Kong roadworks publication is invalid')
    features = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('properties'), dict):
            continue
        props = row['properties']
        work_id = str(props.get('roadworks_id') or '')
        if not re.fullmatch(r'\d{1,12}', work_id) or work_id in seen:
            continue
        start = _hong_kong_time(props.get('starttime'))
        end = _hong_kong_time(props.get('endtime'))
        updated = _hong_kong_time(props.get('lastupdatetime'))
        if (start is None or end is None or updated is None or
                not start <= now <= end or not -600 <= now - updated <= 36 * 3600):
            continue
        if props.get('worksstatus') != 'In Progress':
            continue
        geometry = row.get('geometry') or {}
        if geometry.get('type') != 'Point':
            continue
        point = _point(geometry)
        if not point or not (113.8 <= point[0] <= 114.5 and 22.1 <= point[1] <= 22.6):
            continue
        road = _clean(props.get('roadname'), 100)
        work = _clean(props.get('workstype'), 160)
        lane = _clean(props.get('lane'), 100)
        location = _clean(props.get('locdesc'), 120)
        detail = ' · '.join(part for part in (work, location, lane) if part)
        features.append(_feature(point, {
            'key': f'hk:td:works:{work_id}', 'layer': 'construction',
            'title': road or 'Hong Kong roadworks', 'detail': detail,
            'source': 'Hong Kong Transport Department / Highways Department · DATA.GOV.HK',
            'source_url': HONG_KONG_WORKS_SOURCE,
            'updated_at': props['lastupdatetime'] + ' HKT',
        }))
        seen.add(work_id)
    return features


def _hong_kong_roadworks():
    return _parse_hong_kong_roadworks(_get_json(HONG_KONG_WORKS_URL))


def _parse_hong_kong_sensor_locations(rows):
    locations = {}
    for row in rows:
        detector_id = str(row.get('AID_ID_Number') or '').strip()
        if not re.fullmatch(r'(?:AID\d{5}|TDS[A-Z0-9]{5,16})', detector_id):
            continue
        try:
            lat, lon = float(row['Latitude']), float(row['Longitude'])
        except (TypeError, ValueError, KeyError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon) and
                22.1 <= lat <= 22.6 and 113.8 <= lon <= 114.5):
            continue
        locations[detector_id] = ([lon, lat], _clean(row.get('Road_EN'), 110))
    if len(locations) < 100:
        raise ValueError('Hong Kong traffic-detector locations are incomplete')
    return locations


def _hong_kong_sensor_locations():
    cache = _HONG_KONG_SENSOR_LOCATIONS
    with cache['lock']:
        if time.time() < cache['until']:
            return cache['rows']
    rows = _parse_hong_kong_sensor_locations(_get_csv(HONG_KONG_SENSOR_LOCATIONS_URL))
    with cache['lock']:
        cache.update(until=time.time() + 6 * 3600, rows=rows)
    return rows


def _parse_hong_kong_sensors(root, locations, now=None):
    now = time.time() if now is None else now
    if root.tag != 'raw_speed_volume_list':
        raise ValueError('Hong Kong traffic-detector publication is invalid')
    date = root.findtext('date')
    periods = root.findall('./periods/period')
    if not date or not periods or len(periods) > 4:
        raise ValueError('Hong Kong traffic-detector publication is incomplete')
    selected = max(periods, key=lambda period: period.findtext('period_to') or '')
    try:
        measured = dt.datetime.strptime(f'{date} {selected.findtext("period_to")}',
                                        '%Y-%m-%d %H:%M:%S').replace(
                                            tzinfo=ZoneInfo('Asia/Hong_Kong'))
    except (TypeError, ValueError) as exc:
        raise ValueError('Hong Kong traffic-detector timestamp is invalid') from exc
    age = now - measured.timestamp()
    if not -120 <= age <= HONG_KONG_SENSOR_MAX_AGE:
        raise ValueError(f'Hong Kong traffic-detector publication is stale ({age / 60:.0f} min old)')
    features = []
    seen = set()
    for detector in selected.findall('./detectors/detector'):
        detector_id = detector.findtext('detector_id') or ''
        if detector_id not in locations or detector_id in seen:
            continue
        lanes = []
        for lane in detector.findall('./lanes/lane'):
            if lane.findtext('valid') != 'Y':
                continue
            try:
                speed = float(lane.findtext('speed'))
                occupancy = float(lane.findtext('occupancy'))
                volume = int(lane.findtext('volume'))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(speed) and math.isfinite(occupancy) and
                    0 <= speed <= 200 and 0 <= occupancy <= 100 and 0 <= volume <= 1000):
                continue
            lanes.append((speed, occupancy, volume))
        if not lanes:
            continue
        total = sum(volume for _, _, volume in lanes)
        mean_speed = (sum(speed * volume for speed, _, volume in lanes) / total) if total else None
        mean_occupancy = sum(occupancy for _, occupancy, _ in lanes) / len(lanes)
        point, road = locations[detector_id]
        detail = f'{total} vehicle{"s" if total != 1 else ""} / 30 sec'
        if mean_speed is not None:
            detail += f' · {mean_speed:.0f} km/h average'
        detail += f' · {mean_occupancy:.0f}% lane occupancy'
        features.append(_feature(point, {
            'key': f'hk:td:sensor:{detector_id}', 'layer': 'sensors',
            'title': road or 'Hong Kong traffic detector', 'detail': detail,
            'source': 'Hong Kong Transport Department · DATA.GOV.HK',
            'source_url': HONG_KONG_SENSORS_SOURCE,
            'updated_at': measured.isoformat(),
        }))
        seen.add(detector_id)
    return features


def _hong_kong_sensors():
    return _parse_hong_kong_sensors(_get_xml(HONG_KONG_SENSORS_URL),
                                    _hong_kong_sensor_locations())


def _singapore_camera_image_url(value):
    parsed = urllib.parse.urlsplit(str(value or ''))
    if (parsed.scheme != 'https' or parsed.netloc != 'images.data.gov.sg' or
            parsed.query or parsed.fragment or not re.fullmatch(
                r'/api/traffic-images/\d{4}/\d{2}/[0-9a-f-]{36}\.jpg', parsed.path)):
        raise ValueError('Invalid Singapore traffic image URL')
    return parsed.geturl()


def _parse_singapore_cameras(payload, now=None):
    now = time.time() if now is None else now
    items = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError('Singapore traffic-image publication is invalid')
    cameras = items[0].get('cameras') if isinstance(items[0], dict) else None
    if not isinstance(cameras, list) or not 1 <= len(cameras) <= 500:
        raise ValueError('Singapore traffic-image publication is incomplete')
    rows = {}
    for camera in cameras:
        if not isinstance(camera, dict):
            continue
        camera_id = str(camera.get('camera_id') or '')
        location = camera.get('location') or {}
        if not re.fullmatch(r'\d{3,6}', camera_id) or not isinstance(location, dict):
            continue
        try:
            lon, lat = float(location['longitude']), float(location['latitude'])
            image_url = _singapore_camera_image_url(camera.get('image'))
        except (KeyError, TypeError, ValueError):
            continue
        measured = _timestamp(camera.get('timestamp'))
        if (not math.isfinite(lon) or not math.isfinite(lat) or
                not (103.6 <= lon <= 104.1 and 1.15 <= lat <= 1.5) or
                measured is None or not -120 <= now - measured <= 15 * 60):
            continue
        rows[camera_id] = ([lon, lat], image_url, camera['timestamp'])
    if not rows:
        raise ValueError('Singapore traffic images are unavailable or stale')
    return rows


def _singapore_camera_catalog():
    cache = _SINGAPORE_CAMERA_CATALOG
    with cache['lock']:
        if time.time() < cache['until']:
            return cache['rows']
    rows = _parse_singapore_cameras(_get_json(SINGAPORE_CAMERAS_URL))
    with cache['lock']:
        cache.update(until=time.time() + 90, rows=rows)
    return rows


def _singapore_image_usable(url):
    try:
        request = urllib.request.Request(_singapore_camera_image_url(url), method='HEAD',
                                         headers={'User-Agent': 'GlobeView/1.0 (public camera reader)'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return (urllib.parse.urlsplit(response.url).hostname == 'images.data.gov.sg'
                    and response.status == 200
                    and 1000 <= int(response.headers.get('Content-Length', '0')) <= 2 * 1024 * 1024)
    except (OSError, ValueError):
        return False


def _singapore_cameras():
    rows = _singapore_camera_catalog()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        checks = dict(zip(rows, executor.map(
            lambda camera_id: _singapore_image_usable(rows[camera_id][1]), rows)))
    return [_feature(point, {
        'key': f'sg:lta:camera:{camera_id}', 'layer': 'cameras',
        'title': f'Singapore traffic camera {camera_id}', 'detail': 'Recent traffic still',
        'snapshot_url': f'/singapore-camera/{camera_id}', 'snapshot_refresh_ms': 120000,
        'source': 'Singapore Land Transport Authority · data.gov.sg',
        'source_url': SINGAPORE_CAMERAS_SOURCE, 'updated_at': measured,
    }) for camera_id, (point, _, measured) in rows.items() if checks[camera_id]]


def singapore_camera_snapshot(camera_id):
    if not re.fullmatch(r'\d{3,6}', str(camera_id)):
        raise ValueError('Invalid Singapore camera ID')
    row = _singapore_camera_catalog().get(str(camera_id))
    if row is None or not -120 <= time.time() - _timestamp(row[2]) <= 15 * 60:
        raise FileNotFoundError('Singapore traffic image is unavailable or stale')
    request = urllib.request.Request(row[1], headers={
        'User-Agent': 'GlobeView/1.0 (public camera reader)'})
    with urllib.request.urlopen(request, timeout=15) as response:
        if urllib.parse.urlsplit(response.url).hostname != 'images.data.gov.sg':
            raise ValueError('Singapore traffic image changed origin')
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024 or not body.startswith(b'\xff\xd8\xff'):
        raise ValueError('Singapore traffic image is invalid')
    try:
        with Image.open(io.BytesIO(body)) as picture:
            if picture.format != 'JPEG' or min(picture.size) < 240:
                raise ValueError('Singapore traffic image has unexpected dimensions')
            picture.verify()
    except UnidentifiedImageError as exc:
        raise ValueError('Singapore traffic image is invalid') from exc
    return body, 'image/jpeg'


def _taipei_roc_date(value):
    match = re.fullmatch(r'(\d{2,3})/(\d{2})/(\d{2})', str(value or ''))
    if not match:
        return None
    try:
        result = dt.date(int(match[1]) + 1911, int(match[2]), int(match[3]))
    except ValueError:
        return None
    return result if 2000 <= result.year <= 2100 else None


def _parse_taipei_roadworks(payload, now=None, published_at=''):
    today = (now or dt.datetime.now(ZoneInfo('Asia/Taipei'))).date()
    rows = payload.get('features') if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection' or not isinstance(rows, list) or not 100 <= len(rows) <= 10000:
        raise ValueError('Taipei roadwork publication is incomplete')
    features = []
    seen = set()
    work_types = {'0': 'Construction', '3': 'Road milling', '4': 'Emergency repair',
                  '5': 'Road maintenance', '6': 'Manhole work', 'B': 'Utility restoration'}
    for row in rows:
        props = row.get('properties') if isinstance(row, dict) else None
        geometry = row.get('geometry') if isinstance(row, dict) else None
        coords = geometry.get('coordinates') if isinstance(geometry, dict) else None
        if not isinstance(props, dict) or not isinstance(coords, list) or len(coords) != 2:
            continue
        reference = str(props.get('Ac_no') or '')
        sequence = str(props.get('sno') or '')
        start, end = _taipei_roc_date(props.get('Cb_Da')), _taipei_roc_date(props.get('Ce_Da'))
        if (props.get('IsBlock') != '是' or not re.fullmatch(r'\d{6,12}(?:-\d{1,3})?', reference) or
                not re.fullmatch(r'\d{1,4}', sequence) or not start or not end or
                not start <= today <= end):
            continue
        key = f'tw:taipei:work:{reference}:{sequence}'
        if key in seen:
            continue
        try:
            east, north = float(coords[0]), float(coords[1])
            lon, lat = _TAIPEI_TRANSFORMER.transform(east, north)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon) and math.isfinite(lat) and 121.35 <= lon <= 121.75 and 24.9 <= lat <= 25.25):
            continue
        address = _clean(props.get('Addr'), 95)
        purpose = _clean(props.get('NPurp'), 110)
        kind = work_types.get(str(props.get('AppMode') or ''), 'Road work')
        detail = f'{kind} · affects traffic · through {end.isoformat()}'
        if purpose:
            detail += f' · {purpose}'
        features.append(_feature([lon, lat], {
            'key': key, 'layer': 'construction',
            'title': f'Road work · {address}' if address else 'Road work · Taipei',
            'detail': _clean(detail, 260),
            'source': f'Public Works Department, Taipei City Government · {today.year} Taipei City Today\'s Construction Information',
            'source_url': TAIPEI_WORKS_SOURCE,
            'updated_at': published_at,
        }))
        seen.add(key)
    return features


def _taipei_roadworks():
    cache = _TAIPEI_WORKS_CACHE
    with cache['lock']:
        if time.time() < cache['until']:
            return cache['rows']
    request = urllib.request.Request(TAIPEI_WORKS_URL, headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=25) as response:
        if urllib.parse.urlsplit(response.url).hostname != 'tpnco.blob.core.windows.net':
            raise ValueError('Taipei roadwork feed changed origin')
        modified = email.utils.parsedate_to_datetime(response.headers.get('Last-Modified', ''))
        if not -120 <= time.time() - modified.timestamp() <= 2 * 3600:
            raise ValueError('Taipei roadwork publication is stale')
        body = response.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('Taipei roadwork publication exceeded 8 MB')
    rows = _parse_taipei_roadworks(json.loads(body), published_at=modified.isoformat())
    with cache['lock']:
        cache.update(until=time.time() + 300, rows=rows)
    return rows


def _parse_taipei_cms_locations(root, min_rows=100):
    if root.tag.rsplit('}', 1)[-1] != 'CMSList':
        raise ValueError('Unexpected Taipei sign catalog')
    locations = {}
    for row in root.findall('.//{*}CMS'):
        sign_id = (row.findtext('{*}CMSID') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9]{3,24}', sign_id):
            continue
        try:
            lon = float(row.findtext('{*}PositionLon'))
            lat = float(row.findtext('{*}PositionLat'))
        except (TypeError, ValueError):
            continue
        if not (121.35 <= lon <= 121.75 and 24.9 <= lat <= 25.25):
            continue
        locations[sign_id] = ([lon, lat], _clean(row.findtext('{*}RoadName'), 80))
    if len(locations) < min_rows:
        raise ValueError('Taipei sign catalog is incomplete')
    return locations


def _parse_taipei_cms_live(root, locations, now=None, min_rows=100):
    if root.tag.rsplit('}', 1)[-1] != 'CMSLiveList':
        raise ValueError('Unexpected Taipei live sign feed')
    now = (now or dt.datetime.now(dt.timezone.utc)).timestamp()
    published = _timestamp(root.findtext('{*}UpdateTime'))
    if published is None or not -120 <= now - published <= 10 * 60:
        raise ValueError('Taipei live sign publication is stale')
    rows = root.findall('.//{*}CMSLive')
    if len(rows) < min_rows:
        raise ValueError('Taipei live sign feed is incomplete')
    features = []
    seen = set()
    for row in rows:
        sign_id = (row.findtext('{*}CMSID') or '').strip()
        if sign_id in seen or sign_id not in locations:
            continue
        # Taiwan's CMS standard defines 0 as a healthy device; 1 means a
        # communication fault and 3 means a device fault.
        if row.findtext('{*}Status') != '0' or row.findtext('{*}MessageStatus') != '1':
            continue
        collected_at = row.findtext('{*}DataCollectTime') or ''
        collected = _timestamp(collected_at)
        if collected is None or not -120 <= now - collected <= 10 * 60:
            continue
        messages = list(dict.fromkeys(_clean(message.text, 150) for message in row.findall('.//{*}Text')))
        messages = [message for message in messages if message and message not in {'-99', 'null'}]
        if not messages:
            continue
        coordinates, road_name = locations[sign_id]
        features.append(_feature(coordinates, {
            'key': f'tw:taipei:sign:{sign_id}', 'layer': 'signs',
            'title': f'Road sign · {road_name}' if road_name else 'Road sign · Taipei',
            'detail': _clean(' / '.join(messages), 400),
            'source': 'Department of Transportation Engineering and Management, Taipei City',
            'source_url': TAIPEI_CMS_SOURCE,
            'updated_at': collected_at,
            'alert': any(word in message for message in messages
                         for word in ('施工', '封閉', '事故', '改道', '管制')),
        }))
        seen.add(sign_id)
    return features


def _taipei_cms_locations():
    cache = _TAIPEI_CMS_LOCATIONS
    with cache['lock']:
        if time.time() < cache['until']:
            return cache['rows']
    locations = _parse_taipei_cms_locations(_get_xml(TAIPEI_CMS_STATIC_URL))
    with cache['lock']:
        cache.update(until=time.time() + 6 * 3600, rows=locations)
    return locations


def _taipei_cms_signs():
    return _parse_taipei_cms_live(_get_xml(TAIPEI_CMS_LIVE_URL), _taipei_cms_locations())


def _taiwan_highway_bytes(url, max_bytes):
    # Taiwan's source chain fails Python/OpenSSL's strict CA extension check on
    # the production host. curl validates the chain and hostname with the OS CA
    # store; keep this path limited to the official THB hosts.
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.hostname not in {'thbapp.thb.gov.tw', 'cctv-maintain.thb.gov.tw'}
            | {f'cctv-ss{i:02d}.thb.gov.tw' for i in range(1, 9)}):
        raise ValueError('Unexpected Taiwan highway source')
    try:
        result = subprocess.run(
            ['curl', '--fail', '--silent', '--show-error', '--max-time', '20',
             '--max-filesize', str(max_bytes), '--proto', '=https', url],
            capture_output=True, check=True, timeout=22)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise OSError('Taiwan highway source unavailable') from exc
    if len(result.stdout) > max_bytes:
        raise ValueError('Taiwan highway source exceeded size limit')
    return result.stdout


def _taiwan_highway_root(url, name, max_age, max_bytes=2 * 1024 * 1024, min_rows=500, now=None):
    root = ET.fromstring(_taiwan_highway_bytes(url, max_bytes))
    if root.tag.rsplit('}', 1)[-1] != name:
        raise ValueError('Unexpected Taiwan highway publication')
    age = (now or time.time()) - (_timestamp(root.findtext('{*}UpdateTime')) or 0)
    if not -300 <= age <= max_age:
        raise ValueError('Taiwan highway publication is stale')
    if len(root.findall('.//{*}' + name.removesuffix('List'))) < min_rows:
        raise ValueError('Taiwan highway publication is incomplete')
    return root


def _taiwan_highway_catalog(name, url, root_name, min_rows, max_bytes=2 * 1024 * 1024):
    cache = _TAIWAN_HIGHWAY_CATALOGS[name]
    with cache['lock']:
        if time.time() < cache['until']:
            return cache['rows']
    root = _taiwan_highway_root(url, root_name, 48 * 3600, max_bytes, min_rows)
    rows = {}
    singular = {'cameras': 'CCTV', 'signs': 'CMS', 'sensors': 'VD'}[name]
    id_tag = {'cameras': 'CCTVID', 'signs': 'CMSID', 'sensors': 'VDID'}[name]
    for row in root.findall('.//{*}' + singular):
        ident = (row.findtext('{*}' + id_tag) or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9-]{6,40}', ident):
            continue
        try:
            lon, lat = float(row.findtext('{*}PositionLon')), float(row.findtext('{*}PositionLat'))
        except (TypeError, ValueError):
            continue
        if not (119 <= lon <= 123 and 21 <= lat <= 26):
            continue
        rows[ident] = {'point': [lon, lat], 'road': _clean(row.findtext('{*}RoadName'), 80),
                       'description': _clean(row.findtext('{*}SurveillanceDescription'), 120)}
        if name == 'cameras':
            image_url = row.findtext('{*}VideoImageURL') or ''
            parsed = urllib.parse.urlsplit(image_url)
            if (parsed.scheme != 'https' or not re.fullmatch(r'cctv-ss0[1-8]\.thb\.gov\.tw', parsed.hostname or '')
                    or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                    or parsed.username or parsed.password or not parsed.path.endswith('/snapshot')
                    or '..' in parsed.path or len(image_url) > 250):
                rows.pop(ident)
                continue
            rows[ident]['url'] = image_url
    if len(rows) < min_rows * 0.6:
        raise ValueError('Taiwan highway catalog has too few located devices')
    with cache['lock']:
        cache.update(until=time.time() + 6 * 3600, rows=rows)
    return rows


def _taiwan_highway_cameras():
    rows = _taiwan_highway_catalog('cameras', TAIWAN_HIGHWAY_CCTV_URL, 'CCTVList', 1500)
    return [_feature(row['point'], {
        'key': f'tw:thb:camera:{ident}', 'layer': 'cameras',
        'title': row['description'] or f"Road camera · {row['road'] or 'Taiwan highway'}",
        'detail': 'Provincial highway traffic still',
        'snapshot_url': f'/taiwan-highway-camera/{ident}', 'snapshot_refresh_ms': 60000,
        'source': 'Taiwan Highway Bureau', 'source_url': TAIWAN_HIGHWAY_SOURCE,
    }) for ident, row in rows.items()]


def taiwan_highway_camera_snapshot(camera_id):
    if not re.fullmatch(r'[A-Za-z0-9-]{6,40}', camera_id):
        raise ValueError('Invalid Taiwan highway camera ID')
    row = _taiwan_highway_catalog('cameras', TAIWAN_HIGHWAY_CCTV_URL, 'CCTVList', 1500).get(camera_id)
    if not row:
        raise FileNotFoundError('Taiwan highway camera is not in the official catalog')
    image = _taiwan_highway_bytes(row['url'], 2 * 1024 * 1024)
    if not 4000 <= len(image) <= 2 * 1024 * 1024 or not image.startswith(b'\xff\xd8\xff'):
        raise FileNotFoundError('Taiwan highway camera returned no usable JPEG')
    try:
        with Image.open(io.BytesIO(image)) as decoded:
            if decoded.format != 'JPEG' or decoded.width < 240 or decoded.height < 160:
                raise ValueError('Taiwan highway camera returned an unexpected image')
            decoded.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('Taiwan highway camera returned an invalid image') from exc
    return image, 'image/jpeg'


def _taiwan_highway_signs():
    locations = _taiwan_highway_catalog('signs', TAIWAN_HIGHWAY_CMS_STATIC_URL, 'CMSList', 800)
    root = _taiwan_highway_root(TAIWAN_HIGHWAY_CMS_LIVE_URL, 'CMSLiveList', 10 * 60, min_rows=600)
    now = time.time()
    features = []
    for row in root.findall('.//{*}CMSLive'):
        ident = row.findtext('{*}CMSID') or ''
        location = locations.get(ident)
        if not location or row.findtext('{*}Status') != '0' or row.findtext('{*}MessageStatus') != '1':
            continue
        stamp = row.findtext('{*}DataCollectTime') or ''
        if not -120 <= now - (_timestamp(stamp) or 0) <= 10 * 60:
            continue
        messages = list(dict.fromkeys(_clean(item.text, 150) for item in row.findall('.//{*}Text')))
        messages = [item for item in messages if item and item not in {'-99', 'null'}]
        if messages:
            features.append(_feature(location['point'], {
                'key': f'tw:thb:sign:{ident}', 'layer': 'signs',
                'title': f"Road sign · {location['road'] or 'Taiwan highway'}",
                'detail': _clean(' / '.join(messages), 400), 'updated_at': stamp,
                'source': 'Taiwan Highway Bureau', 'source_url': TAIWAN_HIGHWAY_SOURCE,
            }))
    return features


def _taiwan_highway_sensors():
    locations = _taiwan_highway_catalog('sensors', TAIWAN_HIGHWAY_VD_STATIC_URL, 'VDList', 1000)
    root = _taiwan_highway_root(TAIWAN_HIGHWAY_VD_LIVE_URL, 'VDLiveList', 10 * 60,
                                max_bytes=3 * 1024 * 1024, min_rows=1000)
    now = time.time()
    features = []
    for row in root.findall('.//{*}VDLive'):
        ident = row.findtext('{*}VDID') or ''
        location = locations.get(ident)
        if not location or row.findtext('{*}Status') != '0':
            continue
        stamp = row.findtext('{*}DataCollectTime') or ''
        if not -120 <= now - (_timestamp(stamp) or 0) <= 10 * 60:
            continue
        lanes = row.findall('.//{*}Lane')
        speeds = []
        volume = 0
        for lane in lanes:
            try:
                speed = float(lane.findtext('{*}Speed'))
                if 0 < speed <= 180:
                    speeds.append(speed)
                volume += sum(max(0, int(item.text or 0)) for item in lane.findall('.//{*}Volume'))
            except (TypeError, ValueError):
                continue
        if not speeds and not volume:
            continue
        detail = f"Average speed {sum(speeds) / len(speeds):.0f} km/h" if speeds else 'Speed unavailable'
        detail += f' · {volume} vehicles in reporting interval'
        features.append(_feature(location['point'], {
            'key': f'tw:thb:sensor:{ident}', 'layer': 'sensors',
            'title': f"Road detector · {location['road'] or 'Taiwan highway'}",
            'detail': detail, 'updated_at': stamp,
            'source': 'Taiwan Highway Bureau', 'source_url': TAIWAN_HIGHWAY_SOURCE,
        }))
    return features


def _hong_kong_camera_url(camera_id):
    if not re.fullmatch(r'[A-Z0-9]{2,20}', str(camera_id)):
        raise ValueError('Invalid Hong Kong camera ID')
    return f'https://tdcctv.data.one.gov.hk/{camera_id}.JPG'


def _parse_hong_kong_cameras(root):
    if root.tag != 'image-list' or not 50 <= len(root) <= 2000:
        raise ValueError('Hong Kong camera catalog is incomplete')
    features = []
    seen = set()
    for row in root:
        camera_id = row.findtext('key') or ''
        if camera_id in seen:
            continue
        try:
            expected_url = _hong_kong_camera_url(camera_id)
            point = [float(row.findtext('longitude')), float(row.findtext('latitude'))]
        except (TypeError, ValueError):
            continue
        if (row.findtext('url') != expected_url or not
                (113.8 <= point[0] <= 114.5 and 22.1 <= point[1] <= 22.6)):
            continue
        features.append(_feature(point, {
            'key': f'hk:td:camera:{camera_id}', 'layer': 'cameras',
            'title': _clean(row.findtext('description'), 110) or 'Hong Kong road camera',
            'detail': 'Recent traffic still · normally updated every 2 minutes',
            'snapshot_url': f'/hong-kong-camera/{camera_id}',
            'snapshot_refresh_ms': 120000,
            'source': 'Hong Kong Transport Department · DATA.GOV.HK',
            'source_url': HONG_KONG_CAMERAS_SOURCE,
        }))
        seen.add(camera_id)
    if len(features) < 50:
        raise ValueError('Hong Kong camera catalog has no usable locations')
    return features


def _hong_kong_cameras():
    cache = _HONG_KONG_CAMERA_CATALOG
    with cache['lock']:
        if time.time() < cache['until']:
            return cache['rows']
    rows = _parse_hong_kong_cameras(_get_xml(HONG_KONG_CAMERAS_URL))
    with cache['lock']:
        cache.update(until=time.time() + 6 * 3600, rows=rows)
    return rows


def _hong_kong_camera_headers_usable(response, now):
    if urllib.parse.urlsplit(response.url).hostname != 'tdcctv.data.one.gov.hk':
        return False
    headers = response.headers
    if headers.get('Content-Type', '').split(';')[0] != 'image/jpeg':
        return False
    try:
        size = int(headers['Content-Length'])
        age = now - email.utils.parsedate_to_datetime(headers['Last-Modified']).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    return 5000 <= size <= 200000 and -300 <= age <= 20 * 60


def _hong_kong_camera_usable(camera_id):
    now = time.time()
    cache = _HONG_KONG_CAMERA_HEALTH
    with cache['lock']:
        if now < cache['until'].get(camera_id, 0):
            return camera_id not in cache['unavailable']
    request = urllib.request.Request(_hong_kong_camera_url(camera_id), method='HEAD',
                                     headers={'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            usable = _hong_kong_camera_headers_usable(response, now)
    except (OSError, ValueError):
        usable = False
    with cache['lock']:
        cache['until'][camera_id] = now + 300
        if usable:
            cache['unavailable'].discard(camera_id)
        else:
            cache['unavailable'].add(camera_id)
    return usable


def hong_kong_camera_snapshot(camera_id):
    if camera_id not in {feature['properties']['key'].rsplit(':', 1)[-1]
                         for feature in _hong_kong_cameras()}:
        raise FileNotFoundError('Hong Kong camera is not in the official catalog')
    request = urllib.request.Request(_hong_kong_camera_url(camera_id), headers={
        'User-Agent': 'GlobeView/1.0 (public road feed reader)'})
    with urllib.request.urlopen(request, timeout=8) as response:
        if not _hong_kong_camera_headers_usable(response, time.time()):
            raise FileNotFoundError('Hong Kong camera still is unavailable or stale')
        image = response.read(200001)
    if not 5000 <= len(image) <= 200000 or not image.startswith(b'\xff\xd8\xff'):
        raise ValueError('Hong Kong camera returned no usable JPEG still')
    try:
        with Image.open(io.BytesIO(image)) as decoded:
            if decoded.format != 'JPEG' or decoded.size != (320, 240):
                raise ValueError('Hong Kong camera returned an unexpected image')
            decoded.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('Hong Kong camera returned an invalid image') from exc
    return image, 'image/jpeg'


def _elcat_url(camera_id, asset):
    if camera_id not in ELCAT_CAMERAS or asset not in {'tracks-v1/mono.m3u8', 'preview.mp4'}:
        raise ValueError('Unknown ElCat camera or asset')
    return f'https://webcam.elcat.kg/{camera_id}/{asset}'


def _elcat_playlist_stamp(content, now):
    if not content.startswith('#EXTM3U') or '#EXT-X-ENDLIST' in content or content.count('#EXTINF:') < 2:
        return None
    stamps = [_timestamp(line.partition(':')[2]) for line in content.splitlines()
              if line.startswith('#EXT-X-PROGRAM-DATE-TIME:')]
    stamps = [stamp for stamp in stamps if stamp is not None]
    stamp = max(stamps) if stamps else None
    return stamp if stamp is not None and -120 <= now - stamp <= 180 else None


def _elcat_camera(camera_id):
    url = _elcat_url(camera_id, 'tracks-v1/mono.m3u8')
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public camera reader)'})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            if response.url != url or response.status != 200:
                return None
            body = response.read(256 * 1024 + 1)
        if len(body) > 256 * 1024:
            return None
        stamp = _elcat_playlist_stamp(body.decode('utf-8'), time.time())
        if stamp is None:
            return None
    except (OSError, ValueError):
        return None
    lon, lat, title = ELCAT_CAMERAS[camera_id]
    return _feature([lon, lat], {
        'key': f'kg:elcat:camera:{camera_id}', 'layer': 'cameras', 'title': title,
        'detail': 'Live road / city camera · about 20 seconds delayed · operator-published location',
        'updated_at': dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(),
        'valid_until': stamp + 600,
        'snapshot_url': f'/elcat-camera/{camera_id}', 'snapshot_refresh_ms': 60000,
        'video_url': url, 'video_format': 'hls',
        'source': '© ElCat', 'source_url': f'{ELCAT_CAMERAS_SOURCE}camera/{camera_id}',
    })


def _elcat_cameras():
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        return [point for point in executor.map(_elcat_camera, ELCAT_CAMERAS) if point is not None]


def _public_camera_snapshot_frame(body):
    """Decode one frame from bounded operator video bytes, without network IO."""
    import imageio_ffmpeg
    if not _ELCAT_SNAPSHOT_SLOTS.acquire(timeout=2):
        raise OSError('Camera preview conversion is busy')
    try:
        result = subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-protocol_whitelist', 'pipe', '-threads', '1', '-i', 'pipe:0',
            '-map', '0:v:0', '-an', '-sn', '-dn', '-frames:v', '1',
            '-vf', 'scale=640:-2', '-c:v', 'mjpeg', '-threads', '1',
            '-fs', str(2 * 1024 * 1024), '-f', 'image2pipe', 'pipe:1',
        ], input=body, capture_output=True, check=True, timeout=12)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise OSError('Camera preview conversion failed') from exc
    finally:
        _ELCAT_SNAPSHOT_SLOTS.release()
    image = result.stdout
    if not 64 <= len(image) <= 2 * 1024 * 1024 or not image.startswith(b'\xff\xd8'):
        raise ValueError('Camera preview conversion returned an invalid image')
    return image


def elcat_camera_snapshot(camera_id):
    url = _elcat_url(camera_id, 'preview.mp4')
    request = urllib.request.Request(url, headers={'User-Agent': 'GlobeView/1.0 (public camera reader)'})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.url != url or response.status != 200:
            raise ValueError('ElCat preview redirected or was incomplete')
        if response.headers.get('Content-Type', '').split(';')[0].lower() != 'video/mp4':
            raise ValueError('ElCat preview did not return a video')
        try:
            stamp = email.utils.parsedate_to_datetime(response.headers['Last-Modified']).timestamp()
            size = int(response.headers['Content-Length'])
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            raise ValueError('ElCat preview metadata is invalid') from exc
        if not 64 <= size <= 4 * 1024 * 1024 or not -120 <= time.time() - stamp <= 180:
            raise FileNotFoundError('ElCat preview is stale or oversized')
        body = response.read(4 * 1024 * 1024 + 1)
    if len(body) != size or body[4:8] != b'ftyp':
        raise ValueError('ElCat preview is invalid')
    return _public_camera_snapshot_frame(body), 'image/jpeg'


def kyrgyztelecom_camera_snapshot(camera_id):
    return _public_camera_snapshot_frame(camera_segment(camera_id)), 'image/jpeg'


def _qaj_road_segments(wkt):
    """Read the official map's EPSG:3857 multilines, with bounded geometry."""
    if not isinstance(wkt, str) or len(wkt) > 300000:
        return []
    match = re.fullmatch(r'MULTILINESTRING\s*\(\s*(.*?)\s*\)', wkt.strip(), re.S)
    if not match:
        return []
    groups = re.findall(r'\(([^()]+)\)', match[1])
    if not groups or len(groups) > 40 or re.sub(r'\([^()]+\)', '', match[1]).strip(' ,\r\n\t'):
        return []
    segments, total = [], 0
    for group in groups:
        coordinates = []
        pairs = group.split(',')
        total += len(pairs)
        if total > 5000:
            return []
        for pair in pairs:
            try:
                x, y = map(float, pair.split())
            except (ValueError, TypeError):
                return []
            if not math.isfinite(x) or not math.isfinite(y) or abs(x) > 20037509 or abs(y) > 20037509:
                return []
            lon = math.degrees(x / 6378137)
            lat = math.degrees(math.atan(math.sinh(y / 6378137)))
            if not (46 <= lon <= 88 and 40 <= lat <= 56):
                return []
            point = [round(lon, 7), round(lat, 7)]
            if not coordinates or coordinates[-1] != point:
                coordinates.append(point)
        if len(coordinates) < 2:
            return []
        segments.append(coordinates)
    return segments


def _qaj_notice_window(message):
    # A dated "from HH:MM until HH:MM" notice is finite, even if the source
    # forgot to reset its closed flag. Undated/daily windows are not inferred.
    if re.search(r'ежедневно|каждый\s+день', message, re.I):
        return None
    match = re.match(r'\s*(\d{2}\.\d{2}\.\d{4})\s*г\.?\s*с\s*(\d{2})[.:](\d{2})\s*до\s*(\d{2})[.:](\d{2})', message)
    if not match:
        return None
    try:
        day = dt.datetime.strptime(match[1], '%d.%m.%Y').replace(tzinfo=ZoneInfo('Asia/Almaty'))
        start = day.replace(hour=int(match[2]), minute=int(match[3]))
        end = day.replace(hour=int(match[4]), minute=int(match[5]))
        if end <= start:
            end += dt.timedelta(days=1)
    except ValueError:
        return None
    return start.timestamp(), end.timestamp()


def _parse_qaj_restrictions(data, now=None):
    now = time.time() if now is None else now
    rows = data.get('features') if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('Unexpected QazAvtoJol restriction catalog')
    categories = {1: ('All vehicles restricted', '#e07877'),
                  2: ('Trucks / trailers restricted', '#e3a477'),
                  3: ('Diesel / public transport restricted', '#e0bc76'),
                  4: ('Passenger cars restricted', '#71c5e8')}
    result, seen = [], set()
    for item in rows:
        props = item.get('attributes') if isinstance(item, dict) else None
        if not isinstance(props, dict) or type(props.get('closed')) is not int or props['closed'] not in categories:
            continue
        identifier = str(item.get('gid', ''))
        if not re.fullmatch(r'\d{1,12}', identifier) or identifier in seen:
            continue
        start = _timestamp(props.get('start_date'))
        end = _timestamp(props.get('end_date')) if props.get('end_date') else None
        if start is None or start > now + 300 or (props.get('end_date') and (end is None or end <= now)):
            continue
        message = _clean(props.get('message_ru') or props.get('message_kz') or props.get('message_en'), 1800)
        if not message:
            continue
        window = _qaj_notice_window(message)
        if window and not window[0] <= now < window[1]:
            continue
        segments = _qaj_road_segments(props.get('geom_wkt'))
        if not segments:
            continue
        # Use an actual vertex along the affected road, not a town centroid.
        longest = max(segments, key=len)
        position = longest[len(longest) // 2]
        points = [point for segment in segments for point in segment]
        bounds = [min(point[0] for point in points), min(point[1] for point in points),
                  max(point[0] for point in points), max(point[1] for point in points)]
        category, color = categories[props['closed']]
        reasons = props.get('reasons')
        reason_text = reasons.get('value', '') if isinstance(reasons, dict) else ''
        if not isinstance(reason_text, str) or len(reason_text) > 5000:
            reason_text = ''
        repair = bool(re.search(r'ремонт|дорожн\w*\s+работ|строитель|repair|construction|maintenance',
                                message + ' ' + reason_text, re.I))
        expires = min([now + 900] + ([end] if end else []) + ([window[1]] if window else []))
        notice_date = dt.datetime.fromtimestamp(start, ZoneInfo('Asia/Almaty')).strftime('%d %b %Y')
        result.append(_feature(position, {
            'key': f'kz:qaj:restriction:{identifier}', 'layer': 'construction' if repair else 'incidents',
            'title': f"{category} · {_clean(props.get('name_ru') or props.get('name_kz'), 170)}",
            'detail': f'{message} · Source record: {notice_date}',
            'restriction': props['closed'], 'road_segments': segments, 'road_segment_bounds': bounds,
            'segment_color': color, 'valid_until': expires,
            'source': 'QazAvtoJol situation map', 'source_url': QAJ_RESTRICTIONS_SOURCE,
        }))
        seen.add(identifier)
    return result


def _qaj_restrictions():
    # This is the exact public layer and read-only request used by qaj.kz/s/.
    body = {'attributes': [], 'criteria': '', 'criteriaParam': [], 'layerId': 447,
            'layerName': 'worklyrs.closed_roads_weather', 'limit': 1000, 'offset': 0,
            'orderByColumn': 'end_date'}
    request = urllib.request.Request(QAJ_RESTRICTIONS_URL, data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'User-Agent': 'GlobeView/1.0 (public road restriction reader)',
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        content = response.read(6 * 1024 * 1024 + 1)
    if len(content) > 6 * 1024 * 1024:
        raise ValueError('QazAvtoJol restriction catalog exceeded size limit')
    return _parse_qaj_restrictions(json.loads(content))


def _kaztoll_camera_url(camera_id):
    if camera_id not in KAZTOLL_CAMERAS:
        raise ValueError('Unknown KazToll camera')
    return f'https://kaztoll.kz/livestream/{camera_id}.mp4'


def _kaztoll_clip_info(response, camera_id, now):
    if response.url != _kaztoll_camera_url(camera_id):
        raise ValueError('KazToll clip redirected away from the published file')
    headers = response.headers
    if headers.get('Content-Type', '').split(';')[0].lower() != 'video/mp4':
        raise ValueError('KazToll camera did not return an MP4 clip')
    try:
        modified = email.utils.parsedate_to_datetime(headers['Last-Modified']).timestamp()
        size = int(headers['Content-Length'])
        if response.status == 206:
            match = re.fullmatch(r'bytes 0-31/(\d{1,10})', headers.get('Content-Range', ''))
            if not match or size != 32:
                raise ValueError('Invalid clip probe range')
            size = int(match[1])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError('KazToll camera has invalid clip metadata') from exc
    if not 64 <= size <= 8 * 1024 * 1024 or not -300 <= now - modified <= 600:
        raise FileNotFoundError('KazToll camera clip is stale or oversized')
    return modified, size


def _kaztoll_camera(camera_id):
    request = urllib.request.Request(_kaztoll_camera_url(camera_id), headers={
        'User-Agent': 'GlobeView/1.0 (public road camera reader)', 'Range': 'bytes=0-31',
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            modified, _ = _kaztoll_clip_info(response, camera_id, time.time())
            prefix = response.read(32)
            if len(prefix) != 32 or prefix[4:8] != b'ftyp':
                raise ValueError('KazToll camera did not return an MP4 file')
    except (OSError, ValueError):
        return None
    camera = KAZTOLL_CAMERAS[camera_id]
    return _feature([camera['lon'], camera['lat']], {
        'key': f'kz:kaztoll:{camera_id}', 'layer': 'cameras',
        'title': camera['name'],
        'detail': f"{camera['road']} · Recent video clip · Toll-plaza location; camera position approximate",
        'video_url': f'/kaztoll-camera/{camera_id}', 'video_format': 'mp4',
        'video_refresh_ms': 60000, 'updated_at': modified, 'valid_until': modified + 600,
        'source': 'KazToll / © OpenStreetMap contributors',
        'source_url': KAZTOLL_CAMERAS_SOURCE,
        'location_source_url': f"https://www.openstreetmap.org/node/{camera['osm_node']}",
    })


def _kaztoll_cameras():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        return [point for point in executor.map(_kaztoll_camera, KAZTOLL_CAMERAS) if point]


def kaztoll_camera_clip(camera_id):
    request = urllib.request.Request(_kaztoll_camera_url(camera_id), headers={
        'User-Agent': 'GlobeView/1.0 (public road camera reader)',
    })
    with urllib.request.urlopen(request, timeout=18) as response:
        _, size = _kaztoll_clip_info(response, camera_id, time.time())
        if response.status != 200:
            raise ValueError('KazToll returned an incomplete clip')
        body = response.read(8 * 1024 * 1024 + 1)
    if len(body) != size or body[4:8] != b'ftyp':
        raise ValueError('KazToll camera returned an invalid clip')
    if b'hvc1' in body or b'hev1' in body:
        body = _kaztoll_browser_clip(body)
    return body, 'video/mp4'


def _kaztoll_browser_clip(body):
    """Convert HEVC clips once per shared cache fill, with bounded resources."""
    import imageio_ffmpeg
    if not _KAZTOLL_TRANSCODE_SLOT.acquire(timeout=1):
        raise OSError('Camera conversion is busy')
    try:
        result = subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-protocol_whitelist', 'pipe', '-threads', '1', '-i', 'pipe:0',
            '-map', '0:v:0', '-an', '-sn', '-dn', '-t', '12',
            '-vf', 'fps=10,scale=640:-2', '-c:v', 'libx264', '-threads', '1',
            '-preset', 'ultrafast', '-crf', '27', '-pix_fmt', 'yuv420p',
            '-movflags', 'frag_keyframe+empty_moov', '-fs', str(4 * 1024 * 1024),
            '-f', 'mp4', 'pipe:1',
        ], input=body, capture_output=True, check=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise OSError('Camera conversion failed') from exc
    finally:
        _KAZTOLL_TRANSCODE_SLOT.release()
    clip = result.stdout
    if not 64 <= len(clip) <= 4 * 1024 * 1024 or clip[4:8] != b'ftyp' or b'avc1' not in clip:
        raise ValueError('Camera conversion returned an invalid clip')
    return clip


_FETCHERS = {
    'roads': {
        'ie_dublin_closures': _dublin_closures,
        'ie_tii_cameras': _tii_cameras,
        'ie_tii_events': _tii_events,
        'ie_tii_signs': _tii_signs,
        'dk_copenhagen_roadworks': _copenhagen_roadworks,
        'dk_vejle_roadworks': _vejle_roadworks,
        'hr_zagreb_closures': _zagreb_closures,
        'fi_signs': _fintraffic_signs,
        'fi_cameras': _fintraffic_cameras,
        'lt_eismoinfo_cameras': _lithuania_cameras,
        'lt_eismoinfo_road_weather': _lithuania_road_weather,
        'lt_eismoinfo_restrictions': _lithuania_restrictions,
        'ee_tarktee_cameras': _estonia_cameras,
        'ee_tarktee_signs': _estonia_signs,
        'ee_tarktee_weather': _estonia_weather,
        'ee_tarktee_restrictions': _estonia_restrictions,
        'cz_ndic_roads': _cz_ndic_roads,
        'cz_prague_roadworks': _prague_roadworks,
        'cz_brno_waze_alerts': _brno_waze_alerts,
        'sk_bratislava_roadworks': _bratislava_roadworks,
        'is_road_cameras': _iceland_cameras,
        'is_road_events': _iceland_roads,
        'is_road_sensors': _iceland_sensors,
        'is_road_conditions': _iceland_road_conditions,
        'fi_traffic_sensors': lambda: _fintraffic_sensors('tms'),
        'fi_weather_sensors': lambda: _fintraffic_sensors('weather'),
        'es_dgt_cameras': _dgt_cameras,
        'es_dgt_incidents': _dgt_incidents,
        'es_dgt_signs': _dgt_signs,
        'es_sct_incidents': _sct_incidents,
        'es_sct_cameras': _sct_cameras,
        'es_madrid_incidents': _madrid_incidents,
        'es_zaragoza_roadworks': _zaragoza_roadworks,
        'hk_td_roadworks': _hong_kong_roadworks,
        'hk_td_cameras': _hong_kong_cameras,
        'hk_td_sensors': _hong_kong_sensors,
        'sg_lta_cameras': _singapore_cameras,
        'tw_taipei_roadworks': _taipei_roadworks,
        'tw_taipei_cms_signs': _taipei_cms_signs,
        'tw_highway_cameras': _taiwan_highway_cameras,
        'tw_highway_cms_signs': _taiwan_highway_signs,
        'tw_highway_sensors': _taiwan_highway_sensors,
        'es_valencia_road_occupancy': _valencia_road_occupancy,
        'es_valencia_counters': _valencia_counters,
        'es_madrid_cameras': _madrid_cameras,
        'fr_lyon_cameras': _lyon_cameras,
        'fr_lyon_roadworks': _lyon_roadworks,
        'fr_bordeaux_roadworks': _bordeaux_roadworks,
        'fr_paris_roadworks': _paris_roadworks,
        'fr_toulouse_roadworks': _toulouse_roadworks,
        'fr_paris_traffic_events': _paris_traffic_events,
        'fr_bordeaux_signs': _bordeaux_signs,
        'es_madrid_signs': _madrid_signs,
        'es_vitoria_cameras': _vitoria_cameras,
        'es_vigo_cameras': _vigo_cameras,
        'it_south_tyrol_roads': _south_tyrol_roads,
        'it_a22_announcements': _a22_announcements,
        'it_florence_tram_works': _florence_tram_works,
        'pl_gddkia_roads': _poland_roads,
        'cy_nap_events': _cyprus_roads,
        'cy_waze_alerts': _cyprus_waze_alerts,
        'pl_gdynia_signs': _gdynia_signs,
        'pl_gdynia_sensors': _gdynia_sensors,
        'pl_gdynia_road_weather': _gdynia_road_weather,
        'fi_incidents': lambda: _fintraffic_messages('incidents'),
        'fi_construction': lambda: _fintraffic_messages('construction'),
        'uk_london': _tfl_disruptions,
        'uk_london_cameras': _tfl_cameras,
        'uk_national_highways_roadworks': _national_highways_roadworks,
        'uk_ukpn_streetworks': _ukpn_streetworks,
        'uk_ni_trafficwatch': _trafficwatch_roads,
        'uk_wales_incidents': lambda: _wales_feed('incidents'),
        'uk_wales_construction': lambda: _wales_feed('construction'),
        'uk_scotland_construction': _scotland_roadworks,
        'fr_national_roads': _france_roads,
        'de_hamburg_roads': _hamburg_roads,
        'de_berlin_roads': _berlin_roads,
        'lu_cita_roads': _luxembourg_roads,
        'lu_cita_cameras': _luxembourg_cameras,
        'lu_cita_traffic': _luxembourg_traffic,
        'fr_traffic_sensors': _france_sensors,
        'be_brussels_counters': _brussels_counters,
        'be_brussels_events': _brussels_events,
        'be_brussels_signs': _brussels_signs,
        'be_flemish_roads': _belgium_roads,
        'nl_ndw_roads': _ndw_roads,
        'nl_ndw_bridge_openings': _ndw_bridge_openings,
        'nl_ndw_signs': _ndw_signs,
        'nl_ndw_lane_signs': _ndw_msi_signs,
        'nl_ndw_sensors': _ndw_sensors,
        'ch_zurich_roadworks': _zurich_roadworks,
        'ch_geneva_roadworks': _geneva_roadworks,
        'ch_geneva_cameras': _geneva_cameras,
        'at_vienna_roadworks': _vienna_roadworks,
        'ch_zurich_sensors': _zurich_sensors,
        'no_road_events': _norway_roads,
        'no_road_cameras': _norway_cameras,
        'no_road_weather': _norway_weather,
        'no_travel_times': _norway_travel_times,
        'kz_kaztoll_cameras': _kaztoll_cameras,
        'kg_elcat_cameras': _elcat_cameras,
        'kg_kyrgyztelecom_cameras': kyrgyztelecom_cameras,
        'kg_bishkek_roadworks': bishkek_roadworks,
        'kz_qaj_restrictions': _qaj_restrictions,
    },
    'power': {'ukpn': _ukpn_outages, 'npg': _npg_outages, 'ssen': _ssen_outages,
              'nged': _nged_outages, 'nie': _nie_outages,
              'nl_liander': _liander_outages, 'kz_azhk': _azhk_outages,
              'kg_bipes_planned': bishkek_planned_outages,
              'kg_ipes_planned': issyk_kul_planned_outages,
              'az_azerishiq_planned': azerishiq_planned_outages},
}


def _refresh_snapshot(kind):
    now = time.time()
    with _LOCKS[kind]:
        cache = _CACHE[kind]
        sources = dict(cache['sources'])
        source_times = dict(cache['source_times'])
    errors = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(_FETCHERS[kind])) as executor:
            futures = {executor.submit(loader): name for name, loader in _FETCHERS[kind].items()}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    sources[name] = future.result()
                    source_times[name] = now
                except Exception as error:
                    errors.append(f'{name}: {error}')
                    if name == 'fr_paris_traffic_events' or now - source_times.get(name, 0) > _STALE_SECONDS:
                        sources.pop(name, None)
                # A slow feed elsewhere in Europe must not hold every road layer
                # hostage after a deploy. Publish each completed source at once.
                with _LOCKS[kind]:
                    cache.update({'sources': dict(sources),
                                  'source_times': dict(source_times), 'errors': list(errors)})
        with _LOCKS[kind]:
            cache.update({'until': time.time() + (180 if kind == 'roads' else 300),
                          'sources': sources, 'source_times': source_times, 'errors': errors})
    finally:
        with _LOCKS[kind]:
            _CACHE[kind]['refreshing'] = False
            _REFRESH_DONE[kind].set()


def _snapshot(kind):
    with _LOCKS[kind]:
        cache = _CACHE[kind]
        if time.time() < cache['until'] or (cache['refreshing'] and cache['sources']):
            return {'sources': cache['sources'], 'errors': cache['errors'],
                    'loading': cache['refreshing'] and cache['until'] == 0}
        if not cache['refreshing']:
            cache['refreshing'] = True
            _REFRESH_DONE[kind].clear()
            threading.Thread(target=_refresh_snapshot, args=(kind,), daemon=True).start()
        if cache['sources']:
            return {'sources': cache['sources'], 'errors': cache['errors'], 'loading': False}
    _REFRESH_DONE[kind].wait(_COLD_WAIT_SECONDS)
    with _LOCKS[kind]:
        cache = _CACHE[kind]
        return {'sources': cache['sources'], 'errors': cache['errors'],
                'loading': cache['refreshing'] and cache['until'] == 0}


def _road_feature_in_bbox(item, bbox):
    west, south, east, north = bbox
    segment_bounds = item['properties'].get('road_segment_bounds')
    if (isinstance(segment_bounds, list) and len(segment_bounds) == 4
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in segment_bounds)):
        return (west <= segment_bounds[2] and east >= segment_bounds[0]
                and south <= segment_bounds[3] and north >= segment_bounds[1])
    lon, lat = item['geometry']['coordinates']
    return west <= lon <= east and south <= lat <= north


def road_snapshot(layer, bbox=None):
    if layer not in {'signs', 'incidents', 'construction', 'sensors', 'cameras'}:
        raise ValueError('Unknown road layer')
    if bbox is not None:
        west, south, east, north = bbox
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError('Invalid road bounds')
    snapshot = _snapshot('roads')
    now = time.time()
    features = [item for rows in snapshot['sources'].values() for item in rows
                if item['properties']['layer'] == layer and item['properties'].get('valid_until', float('inf')) > now]
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
        features = [item for item in features if _road_feature_in_bbox(item, bbox)]
    if layer == 'sensors':
        now = time.time()
        features = [item for item in features if not item['properties']['key'].startswith('hk:td:sensor:')
                    or ((stamp := _timestamp(item['properties'].get('updated_at'))) is not None
                        and -120 <= now - stamp <= HONG_KONG_SENSOR_MAX_AGE)]
        features = [item for item in features if not item['properties']['key'].startswith('tw:thb:sensor:')
                    or ((stamp := _timestamp(item['properties'].get('updated_at'))) is not None
                        and -120 <= now - stamp <= 10 * 60)]
    if layer == 'signs':
        now = time.time()
        features = [item for item in features if not item['properties']['key'].startswith('tw:taipei:sign:')
                    or ((stamp := _timestamp(item['properties'].get('updated_at'))) is not None
                        and -120 <= now - stamp <= 10 * 60)]
        features = [item for item in features if not item['properties']['key'].startswith('tw:thb:sign:')
                    or ((stamp := _timestamp(item['properties'].get('updated_at'))) is not None
                        and -120 <= now - stamp <= 10 * 60)]
    if layer == 'cameras':
        now = time.time()
        features = [item for item in features if not item['properties']['key'].startswith('sg:lta:camera:')
                    or ((stamp := _timestamp(item['properties'].get('updated_at'))) is not None
                        and -120 <= now - stamp <= 15 * 60)]
        hong_kong = [item for item in features if item['properties']['key'].startswith('hk:td:camera:')]
        if len(hong_kong) > 80:
            selected = {item['properties']['key'] for item in hong_kong[::math.ceil(len(hong_kong) / 80)]}
            features = [item for item in features if not item['properties']['key'].startswith('hk:td:camera:')
                        or item['properties']['key'] in selected]
            hong_kong = [item for item in hong_kong if item['properties']['key'] in selected]
        if hong_kong:
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
                usable = dict(zip((item['properties']['key'] for item in hong_kong), executor.map(
                    lambda item: _hong_kong_camera_usable(item['properties']['key'].rsplit(':', 1)[-1]),
                    hong_kong)))
            features = [item for item in features if usable.get(item['properties']['key'], True)]
        unavailable = _dgt_unavailable_cameras(features)
        unavailable.update(_tfl_unavailable_cameras(features))
        unavailable.update(_madrid_unavailable_cameras(features))
        with _LUXEMBOURG_CAMERA_HEALTH_LOCK:
            unavailable.update(_LUXEMBOURG_CAMERA_HEALTH['unavailable'])
        features = [item for item in features if item['properties']['key'] not in unavailable]
    return {'type': 'FeatureCollection', 'features': features, 'sourceErrors': errors,
            'sources': sources, 'loading': snapshot.get('loading', False)}


def power_snapshot():
    snapshot = _snapshot('power')
    now = time.time()
    features = [item for rows in snapshot['sources'].values() for item in rows
                if item['properties'].get('valid_until', float('inf')) > now]
    return {'type': 'FeatureCollection', 'features': features, 'sourceErrors': snapshot['errors'],
            'sources': list(snapshot['sources'])}
