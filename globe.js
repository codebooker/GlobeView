/* One MapLibre map renders the globe and every live layer. */
(async () => {
  'use strict';

  const statusBox = document.getElementById('status');
  const loadingBox = document.getElementById('app-loading');
  const loadingMessage = document.getElementById('app-loading-message');
  const loadingRetry = document.getElementById('app-loading-retry');
  const loadingStarted = performance.now();
  let loadingFinished = false;
  function showLoadingError(message) {
    if (!loadingBox || loadingFinished || loadingBox.classList.contains('has-error')) return;
    if (loadingMessage) loadingMessage.textContent = message;
    loadingBox.classList.add('has-error');
    if (loadingRetry) loadingRetry.hidden = false;
  }
  function finishLoading() {
    if (!loadingBox || loadingFinished) return;
    loadingFinished = true;
    const remaining = Math.max(0, 700 - (performance.now() - loadingStarted));
    setTimeout(() => {
      loadingBox.classList.add('is-hidden');
      loadingBox.setAttribute('aria-hidden', 'true');
      setTimeout(() => loadingBox.remove(), 600);
    }, remaining);
  }
  loadingRetry?.addEventListener('click', () => location.reload());
  const cyberMapKey = document.getElementById('cyber-map-key');
  const cyberMapCount = document.getElementById('cyber-map-count');
  const cyberMapUpdated = document.getElementById('cyber-map-updated');
  const cycloneGuidancePanel = document.getElementById('cyclone-guidance-panel');
  const cycloneGuidanceTitle = document.getElementById('cyclone-guidance-title');
  const cycloneGuidanceSummary = document.getElementById('cyclone-guidance-summary');
  const cycloneGuidanceLegend = document.getElementById('cyclone-guidance-legend');
  const cycloneGuidanceSource = document.getElementById('cyclone-guidance-source');
  let maplibregl;
  try {
    maplibregl = await import('./maplibre-gl.mjs');
  } catch (error) {
    console.error('MapLibre:', error);
    statusBox.textContent = 'MapLibre could not load. Check your connection and reload.';
    statusBox.classList.add('visible', 'error');
    showLoadingError('The map engine did not load. Check your connection and retry.');
    return;
  }
  window.maplibregl = maplibregl;

  const STYLE_URL = 'https://tiles.openfreemap.org/styles/dark';
  const HOME = { center: [0, 20], zoom: 1.55, bearing: 0, pitch: 0 };
  const EMPTY = { type: 'FeatureCollection', features: [] };
  function dayNightGeoJSON(now = new Date()) {
    const start = Date.UTC(now.getUTCFullYear(), 0, 0);
    const day = Math.floor((Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - start) / 86400000);
    const hour = now.getUTCHours() + now.getUTCMinutes() / 60 + now.getUTCSeconds() / 3600;
    const gamma = 2 * Math.PI / 365 * (day - 1 + (hour - 12) / 24);
    const decl = 0.006918 - 0.399912 * Math.cos(gamma) + 0.070257 * Math.sin(gamma) - 0.006758 * Math.cos(2 * gamma) + 0.000907 * Math.sin(2 * gamma) - 0.002697 * Math.cos(3 * gamma) + 0.00148 * Math.sin(3 * gamma);
    const features = [];
    for (let west = -180; west < 180; west += 0.5) {
      let south = null;
      for (let lat = -90; lat <= 90; lat += 0.5) {
        const night = Math.sin(lat * Math.PI / 180) * Math.sin(decl) + Math.cos(lat * Math.PI / 180) * Math.cos(decl) * Math.cos(((west + 0.25) * Math.PI / 180) - (12 - hour) * Math.PI / 12) < 0;
        if (night && south == null) south = lat;
        if ((!night || lat === 90) && south != null) {
          const north = night && lat === 90 ? 90 : lat;
          features.push({ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [[[west, south], [west + 0.5, south], [west + 0.5, north], [west, north], [west, south]]] } });
          south = null;
        }
      }
    }
    return { type: 'FeatureCollection', features };
  }
  const firmsDate = (() => { const date = new Date(Date.now() - 86400000); return date.toISOString().slice(0, 10); })();
  const POINT = {
    govair: { label: 'Government aircraft', color: '#c9a96b', glyph: '✈', minZoom: 0, refreshMs: 45000 },
    civair: { label: 'Civil aircraft', color: '#f3f5f4', glyph: '✈', minZoom: 0, refreshMs: 45000 },
    cameras: { label: 'Traffic camera', color: '#78ad92', glyph: '◉', minZoom: 10, refreshMs: 90000 },
    signs: { label: 'Message sign', color: '#bda168', glyph: '▣', minZoom: 10, refreshMs: 90000 },
    incidents: { label: 'Road incident', color: '#c78276', glyph: '!', minZoom: 10, refreshMs: 30000 },
    construction: { label: 'Construction', color: '#bd986b', glyph: '◆', minZoom: 10, refreshMs: 120000 },
    emergency: { label: 'Emergency call', color: '#c47673', glyph: '+', minZoom: 10, refreshMs: 30000 },
    international: { label: 'International emergency report', color: '#c47673', glyph: '!', minZoom: 10, refreshMs: 300000 },
    sensors: { label: 'Road sensor', color: '#7b9eb7', glyph: 'S', minZoom: 10, refreshMs: 300000 },
    temperature: { label: 'Temperature station', color: '#c49375', glyph: '°', minZoom: 10, refreshMs: 300000 },
    lpr: { label: 'Plate reader', color: '#a18cba', glyph: '◎', minZoom: 10, refreshMs: 300000 },
    power: { label: 'Power outage', color: '#bb84a1', glyph: 'ϟ', minZoom: 9, refreshMs: 300000 },
    vessels: { label: 'Live vessel', color: '#78aaa9', glyph: '▲', minZoom: 5, refreshMs: 15000 },
    webcams: { label: 'Public webcam', color: '#90a9bc', glyph: '◉', minZoom: 10, refreshMs: 86400000 },
    cyclones: { label: 'Tropical cyclone', color: '#b8a2c5', glyph: '◎', minZoom: 0, refreshMs: 900000 },
    earthquakes: { label: 'Earthquake', color: '#d1b16e', glyph: '◆', minZoom: 0, refreshMs: 120000 },
    nws_alerts: { label: 'NWS alert', color: '#e3b96c', glyph: '!', minZoom: 0, refreshMs: 120000 },
    world_alerts: { label: 'International weather alert', color: '#79bec1', glyph: '!', minZoom: 0, refreshMs: 180000 },
    fires: { label: 'Major wildfire', color: '#d58c6c', glyph: '▲', minZoom: 0, refreshMs: 1800000 },
    scans: { label: 'Observed scan source', color: '#bc8292', glyph: '✳', minZoom: 0, refreshMs: 3600000 },
    ports: { label: 'Port', color: '#8fbcb6', glyph: '⚓', minZoom: 3, refreshMs: 86400000 },
    floods: { label: 'Flood', color: '#80bbca', glyph: '≈', minZoom: 0, refreshMs: 1800000 },
    volcanoes: { label: 'Volcanic event', color: '#df9a78', glyph: '▲', minZoom: 0, refreshMs: 1800000 },
    gdelt_events: { label: 'GDELT media-coded event', color: '#e1a371', glyph: '⚑', minZoom: 2, refreshMs: 900000 }
  };
  const ROAD = {
    cameras: 'Cameras',
    signs: 'MessageSigns',
    incidents: 'Incidents',
    construction: 'Construction'
  };
  const RASTER = { traffic: { minZoom: 6 }, radar: { minZoom: 0 }, imagery: { minZoom: 0 }, goes: { minZoom: 0 }, fire_hotspots: { minZoom: 0 }, daynight: { minZoom: 0 } };
  const MODES = { marine: { minZoom: 0 }, radio: { minZoom: 0 } };
  const ARCGIS = {
    transmission: { label: 'Transmission lines', minZoom: 7, kind: 'line', color: '#efbb70', source: 'U.S. DOE / HIFLD 2022' },
    gas_pipelines: { label: 'Gas pipelines', minZoom: 7, kind: 'line', color: '#b89bd9', source: 'U.S. DOE / HIFLD 2019' },
    wind_turbines: { label: 'Wind turbines', minZoom: 9, kind: 'point', color: '#99d5c8', source: 'USGS Wind Turbine Database' },
    solar_sites: { label: 'Solar sites', minZoom: 8, kind: 'polygon', color: '#e2ca76', source: 'USGS Solar PV Database' },
    power_plants: { label: 'Power plants', minZoom: 5, kind: 'point', color: '#e7a577', source: 'WRI Global Power Plant Database' },
    active_faults: { label: 'Active faults', minZoom: 5, kind: 'line', color: '#e39283', source: 'GEM Global Active Faults' },
    impact_sites: { label: 'Impact structures', minZoom: 5, kind: 'point', color: '#9cb8e2', source: 'ArcGIS impact structures archive' },
    rail_world: { label: 'Rail corridors', minZoom: 5, kind: 'line', color: '#d6b7e6', source: 'Natural Earth railroads via ArcGIS · generalized coverage' },
    rail_narn: { label: 'Rail lines', minZoom: 8, kind: 'line', color: '#f4cc80', source: 'FRA / BTS North American Rail Network · 2026' },
    shipping_routes: { label: 'Shipping routes', minZoom: 0, kind: 'shipping', color: '#6fc7ce', source: 'CIA 2012 map, digitized dataset', global: true }
  };
  const ARCGIS_ATTRIBUTION = 'ArcGIS explorer: <a href="https://arcgis.netl.doe.gov/">DOE</a> / <a href="https://energy.usgs.gov/">USGS</a> / <a href="https://www.wri.org/research/global-database-power-plants">WRI</a> / <a href="https://www.globalquakemodel.org/product/active-faults-database">GEM (CC BY-SA 4.0)</a> / <a href="https://services.arcgis.com/e8gGAYmR5kxEFApE/ArcGIS/rest/services/Meteorites/FeatureServer/0">impact archive</a> / <a href="https://www.naturalearthdata.com/downloads/10m-cultural-vectors/railroads/">Natural Earth rail</a> / <a href="https://railroads.dot.gov/rail-network-development/maps-and-data/maps-geographic-information-system/maps-geographic">FRA / BTS rail</a> / <a href="https://zenodo.org/records/6361763">shipping lanes (CC BY 4.0)</a>';
  const defaultLayers = new Set([...document.querySelectorAll('[data-layer][data-default-on]')].map(button => button.dataset.layer));
  const enabled = Object.fromEntries([...Object.keys(POINT), ...Object.keys(RASTER), ...Object.keys(MODES), ...Object.keys(ARCGIS)].map(type => [type, defaultLayers.has(type)]));
  document.querySelectorAll('[data-layer]').forEach(button => button.setAttribute('aria-pressed', String(enabled[button.dataset.layer])));
  const layerGroups = new Map([...document.querySelectorAll('[data-layer-group]')].map(header => {
    const types = [];
    for (let sibling = header.nextElementSibling; sibling && !sibling.hasAttribute('data-layer-group'); sibling = sibling.nextElementSibling) {
      if (sibling.dataset.layer) types.push(sibling.dataset.layer);
    }
    return [header.dataset.layerGroup, { header, types }];
  }));
  function updateGroupControls() {
    for (const { header, types } of layerGroups.values()) {
      const active = types.filter(type => enabled[type]).length;
      const state = active === 0 ? 'false' : active === types.length ? 'true' : 'mixed';
      const button = header.querySelector('.group-toggle');
      button.setAttribute('aria-checked', state);
      button.title = `${active} of ${types.length} layers on · click to turn ${state === 'true' ? 'all off' : 'all on'}`;
    }
  }
  updateGroupControls();
  const refs = new Map(Object.keys(POINT).map(type => [type, new Map()]));
  const counts = new Map();
  const requests = new Map();
  const requestStartedAt = new Map();
  const arcgisQueryKeys = new Map();
  const fetchedAt = new Map();
  const roadCache = new Map();
  let roadRegions = [];
  let styleReady = false;
  setTimeout(() => { if (!styleReady) showLoadingError('The globe is taking too long to load. Please retry.'); }, 20000);
  let basemapGeometryLayers = [];
  let basemapImageryActive = false;
  let basemapLabelPaint = [];
  let globeProjection = true;
  let terrainEnabled = false;
  let rotationEnabled = !window.matchMedia('(prefers-reduced-motion: reduce)').matches &&
    !new URLSearchParams(location.search).has('z');
  let rotationFrame = 0;
  let rotationTime = 0;
  let cyberFocus = false;
  let activePointPopup = null;
  let cycloneGuidanceController = null;
  let selectedCycloneId = null;
  let cycloneGuidanceData = EMPTY;
  let statusTimer;
  let viewportTimer;
  let radarTime = 0;
  let radarProvider = '';
  let radarTileTemplate = '';
  let aircraftPromise = null;
  let aircraftSnapshot = [];
  let aircraftProvider = '';
  let aircraftQueryKey = '';
  let aircraftFetchedAt = 0;
  const aircraftRetryAt = { local: 0, world: 0 };
  let aircraftBusyNoticeAt = 0;
  let aircraftStaleNoticeAt = 0;
  let trackedAircraft = null;
  let trackedController = null;
  let trackedTimer = null;
  let trackedRetryAt = 0;
  let aircraftRouteController = null;
  let aircraftTrailController = null;
  let trackingFollow = true;
  let aircraftSearchController = null;
  let vesselPromise = null;
  const vesselClientId = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
  let webcamCatalog = null;
  let vesselKeyNoticeShown = false;
  let vesselCapacityNoticeShown = false;
  let placesPromise = null;
  let portCatalog = null;
  let portOverviewCount = 0;
  let radioPromise = null;
  let radioStations = new Map();
  let radioReportedId = null;
  let currentRadioId = null;
  let radioConnectTimer = null;
  const radioList = document.getElementById('radio-list');
  const radioListTitle = document.getElementById('radio-list-title');
  const radioListItems = document.getElementById('radio-list-items');
  const radioTarget = document.getElementById('radio-target');
  const radioTargetLabel = document.getElementById('radio-target-label');
  const radioNearbyButton = document.getElementById('radio-nearby');
  let nearbyRadioStations = [];
  const radioTargetToggle = document.getElementById('radio-target-toggle');
  let radioTargetEnabled = true;
  let radioTuneAfterMove = false;
  const radioPlayer = document.getElementById('radio-player');
  const radioAudio = document.getElementById('radio-audio');
  const radioPlayerStatus = document.getElementById('radio-player-status');
  let seaPopup = null;
  let imageryMode = 'detail';
  let imageryTileKey = '';
  const imageryOptions = document.getElementById('imagery-options');
  const imageryNote = document.getElementById('imagery-note');
  const goesOptions = document.getElementById('goes-options');
  const goesPlayButton = document.getElementById('goes-play');
  const goesFrameInput = document.getElementById('goes-frame');
  const goesTime = document.getElementById('goes-time');
  const goesSources = [
    { id: 'west-pacific', side: 'west', bounds: [110, -75, 180, 75] },
    { id: 'west-americas', side: 'west', bounds: [-180, -75, -80, 75] },
    { id: 'east', side: 'east', bounds: [-80, -75, 50, 75] },
  ];
  const goesFrames = { east: [], west: [] };
  let goesFrameIndex = 5;
  let goesFetchedAt = 0;
  let goesFetchPromise = null;
  let goesTimer = null;
  let goesPreload = false;
  function goesTileUrl(side, stamp = 'default') {
    return `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/GOES-${side === 'east' ? 'East' : 'West'}_ABI_GeoColor/default/${stamp}/GoogleMapsCompatible_Level7/{z}/{y}/{x}.png`;
  }
  function stopGoesPlayback() {
    if (goesTimer) clearInterval(goesTimer);
    goesTimer = null;
    goesPlayButton.textContent = '▶ Play';
    goesPlayButton.setAttribute('aria-label', 'Play GOES cloud frames');
  }
  function rebuildGoesLayers() {
    if (!map.getStyle()?.layers) return;
    for (const source of goesSources) for (let index = 0; index < 6; index++) {
      const id = `gm-goes-${source.id}-${index}`;
      if (map.getLayer(`${id}-layer`)) map.removeLayer(`${id}-layer`);
      if (map.getSource(id)) map.removeSource(id);
    }
    const firstLabel = map.getStyle().layers.find(layer => layer.type === 'symbol')?.id;
    for (const source of goesSources) for (let index = 0; index < goesFrames[source.side].length; index++) {
      const id = `gm-goes-${source.id}-${index}`;
      map.addSource(id, {
        type: 'raster', tiles: [goesTileUrl(source.side, goesFrames[source.side][index])],
        tileSize: 256, minzoom: 0, maxzoom: 7, bounds: source.bounds,
        attribution: '<a href="https://www.nesdis.noaa.gov/imagery/satellite-maps" target="_blank" rel="noopener">NOAA GOES-East/West GeoColor via NASA GIBS</a>'
      });
      map.addLayer({ id: `${id}-layer`, type: 'raster', source: id,
        paint: { 'raster-opacity': 1, 'raster-fade-duration': 0 }, layout: { visibility: 'none' } }, firstLabel);
    }
  }
  function renderGoesFrame() {
    const labels = [];
    for (const side of ['east', 'west']) {
      const stamp = goesFrames[side][goesFrameIndex];
      if (!stamp) continue;
      const time = new Date(stamp).toLocaleString([], { timeZone: 'UTC', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
      labels.push(`${side === 'east' ? 'East' : 'West'} ${time}`);
    }
    if (styleReady) for (const source of goesSources) for (let index = 0; index < 6; index++) {
      const layer = `gm-goes-${source.id}-${index}-layer`;
      if (!map.getLayer(layer)) continue;
      const visibility = enabled.goes && (goesPreload || index === goesFrameIndex) ? 'visible' : 'none';
      const opacity = index === goesFrameIndex ? 1 : 0;
      if (map.getPaintProperty(layer, 'raster-opacity') !== opacity) map.setPaintProperty(layer, 'raster-opacity', opacity);
      if (map.getLayoutProperty(layer, 'visibility') !== visibility) map.setLayoutProperty(layer, 'visibility', visibility);
    }
    goesTime.textContent = labels.length ? `${labels.join(' · ')} UTC` : 'Recent GOES imagery is unavailable.';
    goesFrameInput.value = String(goesFrameIndex);
  }
  async function getGoesFrames(side, day) {
    const layer = `GOES-${side === 'east' ? 'East' : 'West'}_ABI_GeoColor`;
    const url = `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/wmts.cgi?SERVICE=WMTS&REQUEST=DescribeDomains&VERSION=1.0.0&LAYER=${layer}&TILEMATRIXSET=GoogleMapsCompatible_Level7&TIME=${day}&refresh=${Math.floor(Date.now() / 600000)}`;
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`GOES ${side} metadata: ${response.status}`);
    const xml = new DOMParser().parseFromString(await response.text(), 'text/xml');
    const domain = xml.querySelector('DimensionDomain Domain')?.textContent || '';
    const ends = [...domain.matchAll(/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\/PT10M/g)].map(match => Date.parse(match[1]));
    const latest = Math.max(...ends);
    if (!Number.isFinite(latest) || Date.now() - latest > 6 * 3600000) throw new Error(`GOES ${side} has no recent frame`);
    return Array.from({ length: 6 }, (_, index) => new Date(latest - (5 - index) * 600000).toISOString().replace('.000Z', 'Z'));
  }
  async function refreshGoesFrames() {
    if (!enabled.goes || !styleReady || goesFetchPromise) return goesFetchPromise;
    if (Date.now() - goesFetchedAt < 5 * 60000 && goesFrames.east.length + goesFrames.west.length) {
      renderGoesFrame();
      return;
    }
    goesTime.textContent = 'Loading recent GOES frames…';
    goesFetchPromise = (async () => {
      const today = new Date().toISOString().slice(0, 10);
      const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
      const results = await Promise.allSettled(['east', 'west'].map(async side => {
        try { return await getGoesFrames(side, today); }
        catch { return getGoesFrames(side, yesterday); }
      }));
      if (!enabled.goes) return;
      const previous = JSON.stringify(goesFrames);
      ['east', 'west'].forEach((side, index) => {
        goesFrames[side] = results[index].status === 'fulfilled' ? results[index].value : [];
      });
      if (JSON.stringify(goesFrames) !== previous) {
        stopGoesPlayback();
        goesPreload = false;
        rebuildGoesLayers();
      }
      goesFetchedAt = Date.now();
      const available = goesFrames.east.length + goesFrames.west.length > 0;
      goesFrameInput.disabled = !available;
      goesPlayButton.disabled = !available;
      document.querySelector('[data-count="goes"]').textContent = goesFrames.east.length && goesFrames.west.length ? 'East + West'
        : goesFrames.east.length ? 'East' : goesFrames.west.length ? 'West' : 'Unavailable';
      if (!available) { stopGoesPlayback(); showStatus('Recent GOES imagery is unavailable.', true); }
      else if (!goesTimer) goesFrameIndex = 5;
      renderGoesFrame();
    })().catch(error => {
      console.warn('GOES imagery:', error);
      goesTime.textContent = 'Recent GOES imagery is unavailable.';
    }).finally(() => { goesFetchPromise = null; });
    return goesFetchPromise;
  }
  function imageryDate(mode = imageryMode) {
    return new Date(Date.now() - (mode === 'previous' ? 86400000 : 0)).toISOString().slice(0, 10);
  }
  function imageryTiles(mode = imageryMode) {
    const date = imageryDate(mode);
    // Current-day composites gain new passes during the day. Change the URL every 30 minutes
    // so a map left open can pick up new tiles instead of retaining MapLibre's first copy.
    const revision = mode === 'today' ? Math.floor(Date.now() / 1800000) : date;
    const key = `${mode}:${date}:${revision}`;
    return { key, url: `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_SNPP_CorrectedReflectance_TrueColor/default/${date}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpeg?v=${revision}` };
  }
  const registryCache = new Map();
  const AIRCRAFT_COLORS = { civil: '#f3f5f4', medical: '#ff6b6b', police: '#58a6ff',
    fire: '#ff9e43', military: '#f2cc60', gov: '#f85149' };
  const HELICOPTER_TYPES = new Set('R22 R44 R66 B06 B206 B212 B214 B222 B230 B407 B412 B427 B429 B430 B505 EC20 EC25 EC30 EC35 EC45 EC55 EC75 H125 H135 H145 H155 H160 H175 H215 H225 AS32 AS35 AS50 AS55 AS65 S300 S61 S76 S92 H500 H369 HUCO MD5 MD52 MD6 A109 A119 A139 A149 A169 A189 UH1 CH47 UH60 UH72 OH58 S269 S333 CABR F28'.split(' '));
  const query = new URLSearchParams(location.search);
  const queryLat = Number(query.get('lat'));
  const queryLon = Number(query.get('lon'));
  const queryZoom = Number(query.get('z'));
  const INITIAL = query.has('lat') && query.has('lon') && query.has('z') &&
    validCoordinate(queryLat, queryLon) && Number.isFinite(queryZoom)
    ? { center: [queryLon, queryLat], zoom: Math.max(0, Math.min(19, queryZoom)) }
    : HOME;

  const map = new maplibregl.Map({
    container: 'map', style: STYLE_URL, center: INITIAL.center, zoom: INITIAL.zoom,
    minZoom: 0, maxZoom: 19, maxPitch: 75, renderWorldCopies: false,
    attributionControl: false, antialias: false, fadeDuration: 0
  });
  const DEG_TO_RAD = Math.PI / 180;
  function setGlobeFrontVector() {
    const { lat, lng } = map.getCenter();
    const latitude = lat * DEG_TO_RAD;
    const longitude = lng * DEG_TO_RAD;
    map.setGlobalStateProperty('globe-front-x', Math.cos(latitude) * Math.cos(longitude));
    map.setGlobalStateProperty('globe-front-y', Math.cos(latitude) * Math.sin(longitude));
    map.setGlobalStateProperty('globe-front-z', Math.sin(latitude));
    map.setGlobalStateProperty('globe-horizon-enabled', globeProjection);
  }
  const globeFrontDot = ['+',
    ['*', ['global-state', 'globe-front-x'], ['number', ['get', 'globe-x'], 0]],
    ['*', ['global-state', 'globe-front-y'], ['number', ['get', 'globe-y'], 0]],
    ['*', ['global-state', 'globe-front-z'], ['number', ['get', 'globe-z'], 0]]
  ];
  const globeHorizonFilter = ['any', ['==', ['global-state', 'globe-horizon-enabled'], false], ['>=', globeFrontDot, 0.008]];
  function setPointLayerFilter(layerId, baseFilter = null) {
    if (!map.getLayer(layerId)) return;
    const filters = [globeHorizonFilter, ...(baseFilter ? [baseFilter] : [])];
    map.setFilter(layerId, filters.length === 1 ? filters[0] : ['all', ...filters]);
  }
  function withGlobeVector(properties, lon, lat) {
    const latitude = lat * DEG_TO_RAD;
    const longitude = lon * DEG_TO_RAD;
    return { ...properties, 'globe-x': Math.cos(latitude) * Math.cos(longitude),
      'globe-y': Math.cos(latitude) * Math.sin(longitude), 'globe-z': Math.sin(latitude) };
  }
  function addGlobeVectorsToPoints(collection) {
    for (const item of collection.features || []) {
      if (item.geometry?.type !== 'Point') continue;
      const [lon, lat] = item.geometry.coordinates;
      item.properties = withGlobeVector(item.properties || {}, lon, lat);
    }
    return collection;
  }
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: '<a href="https://maplibre.org/">MapLibre</a>' }), 'bottom-right');
  // MapLibre initially opens compact attribution so users can discover it.
  // Keep the Info bubble, but defer the source list until the user asks for it.
  const attributionDetails = map.getContainer().querySelector('.maplibregl-ctrl-attrib');
  attributionDetails?.classList.remove('maplibregl-compact-show');
  attributionDetails?.removeAttribute('open');
  const syncAttributionDisclosure = () => document.body.classList.toggle('attribution-expanded', Boolean(attributionDetails?.open));
  attributionDetails?.querySelector('summary')?.addEventListener('click', () => requestAnimationFrame(syncAttributionDisclosure));
  window.globalMap = map;
  import('./trip.js').catch(error => console.error('Trip planner:', error));
  import('./drawings.js').catch(error => console.error('Drawings:', error));

  maplibregl.addProtocol('gm-flow', async (request, abortController) => {
    const match = /^gm-flow:\/\/(\d+)\/(\d+)\/(\d+)$/.exec(request.url);
    if (!match) throw new Error('Invalid traffic tile URL');
    const [, z, x, y] = match;
    const response = await fetch(`/tile?x=${x}&y=${y}&z=${z}`, { signal: abortController.signal });
    if (!response.ok) throw new Error(`Traffic tile ${response.status}`);
    const bitmap = await createImageBitmap(await response.blob());
    const canvas = typeof OffscreenCanvas === 'function'
      ? new OffscreenCanvas(bitmap.width, bitmap.height)
      : Object.assign(document.createElement('canvas'), { width: bitmap.width, height: bitmap.height });
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0);
    bitmap.close();
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const data = pixels.data;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 10) continue;
      const r = data[i], g = data[i + 1], b = data[i + 2];
      if (r < 80 && g > 100) data[i + 3] = Math.round(data[i + 3] * 0.10);
      else if (r > 150 && g > 150 && b < 80) data[i + 3] = Math.round(data[i + 3] * 0.42);
    }
    ctx.putImageData(pixels, 0, 0);
    return { data: await createImageBitmap(canvas) };
  });

  maplibregl.addProtocol('gm-radar', async (request, abortController) => {
    const match = /^gm-radar:\/\/(\d+)\/(\d+)\/(\d+)\/(\d+)$/.exec(request.url);
    if (!match || !radarTileTemplate) throw new Error('Invalid radar tile URL');
    const [, , z, x, y] = match;
    const url = radarTileTemplate.replace('{z}', z).replace('{x}', x).replace('{y}', y);
    const response = await fetch(url, { signal: abortController.signal });
    if (!response.ok) throw new Error(`Radar tile ${response.status}`);
    const bitmap = await createImageBitmap(await response.blob());
    const canvas = typeof OffscreenCanvas === 'function'
      ? new OffscreenCanvas(bitmap.width, bitmap.height)
      : Object.assign(document.createElement('canvas'), { width: bitmap.width, height: bitmap.height });
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0);
    bitmap.close();
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const values = pixels.data;
    for (let i = 0; i < values.length; i += 4) {
      if (values[i + 3] < 8) continue;
      const r = values[i], g = values[i + 1], b = values[i + 2];
      if (b <= r + 20 || b <= g + 10 || b <= 80) continue;
      const intensity = Math.max(g, b);
      values[i] = Math.round(r * 0.35 + intensity * 0.12);
      values[i + 1] = Math.min(255, Math.round(intensity * 0.78 + 35));
      values[i + 2] = Math.round(r * 0.2 + intensity * 0.2);
    }
    ctx.putImageData(pixels, 0, 0);
    return { data: await createImageBitmap(canvas) };
  });

  function setMapPadding() {
    map.setPadding({ top: window.innerWidth <= 1100 ? 100 : 54, right: 0, bottom: 0,
      left: window.innerWidth > 900 && !document.body.classList.contains('panel-collapsed') ? 288 : 0 });
  }
  setMapPadding();

  function showStatus(message, error = false, hold = 4000) {
    clearTimeout(statusTimer);
    statusBox.textContent = message;
    statusBox.classList.toggle('error', error);
    statusBox.classList.add('visible');
    if (hold) statusTimer = setTimeout(() => statusBox.classList.remove('visible'), hold);
  }

  function setPanelOpen(open) {
    const mobile = window.innerWidth <= 900;
    document.body.classList.toggle('panel-collapsed', !mobile && !open);
    document.getElementById('panel').classList.toggle('open', mobile && open);
    const button = document.getElementById('panel-toggle');
    button.setAttribute('aria-expanded', String(open));
    button.textContent = open ? 'Hide Menu' : 'Menu';
    setMapPadding();
  }
  setPanelOpen(window.innerWidth > 900);
  let wasMobile = window.innerWidth <= 900;
  window.addEventListener('resize', () => {
    const mobile = window.innerWidth <= 900;
    if (mobile !== wasMobile) {
      wasMobile = mobile;
      setPanelOpen(!mobile);
    } else setMapPadding();
  });

  function setCount(type, value) {
    counts.set(type, value);
    const node = document.querySelector(`[data-count="${type}"]`);
    if (node) node.textContent = value == null || !enabled[type] ? '' : Number(value).toLocaleString();
  }

  function zoomHint(type) {
    const minZoom = POINT[type]?.minZoom ?? RASTER[type]?.minZoom ?? MODES[type]?.minZoom ?? ARCGIS[type]?.minZoom ?? 0;
    return enabled[type] && map.getZoom() < minZoom ? `Zoom ${minZoom}+` : '';
  }

  function updateToggle(type) {
    if (!styleReady) return;
    const visible = enabled[type] && !zoomHint(type) && (!cyberFocus || type === 'scans');
    if (ARCGIS[type]) {
      const id = `gm-arcgis-${type}-layer`;
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
    } else if (POINT[type]) {
      const layerIds = type === 'power'
        ? ['gm-power-fill', 'gm-power-line', 'gm-power-points']
        : type === 'govair' || type === 'civair' ? [`gm-${type}-points`]
        : type === 'cyclones' ? ['gm-cyclones-track-line', 'gm-cyclones-points']
        : type === 'nws_alerts' ? ['gm-nws-alerts-area-fill', 'gm-nws-alerts-area-line', 'gm-nws_alerts-points']
        : type === 'world_alerts' ? ['gm-world-alerts-area-fill', 'gm-world-alerts-area-line', 'gm-world_alerts-points']
        : type === 'scans' ? ['gm-scans-density', 'gm-scans-top-points', 'gm-scans-points']
        : [`gm-${type}-points`];
      for (const id of layerIds) if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
      if (type === 'cyclones') for (const id of ['gm-cyclone-ensemble-lines', 'gm-cyclone-model-casing', 'gm-cyclone-model-lines', 'gm-cyclone-consensus-casing', 'gm-cyclone-consensus-line', 'gm-cyclone-official-casing', 'gm-cyclone-official-line']) {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible && selectedCycloneId ? 'visible' : 'none');
      }
      if (type === 'ports' && map.getLayer('gm-ports-points')) {
        setPointLayerFilter('gm-ports-points', map.getZoom() < 6
          ? ['in', ['get', 'size'], ['literal', ['Large', 'Medium']]] : null);
      }
    } else if (type === 'traffic' && map.getLayer('gm-traffic-layer')) {
      for (const id of ['gm-traffic-layer', 'gm-bordeaux-traffic-layer']) {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
      }
    } else if (type === 'radio') {
      if (map.getLayer('gm-radio-points')) map.setLayoutProperty('gm-radio-points', 'visibility', visible ? 'visible' : 'none');
      radioTarget.hidden = !visible || !radioTargetEnabled;
      if (visible && radioTargetEnabled) positionRadioTarget();
    } else if (type === 'radar' && map.getLayer('gm-radar-layer')) {
      map.setLayoutProperty('gm-radar-layer', 'visibility', visible ? 'visible' : 'none');
    } else if (type === 'goes') {
      renderGoesFrame();
      goesOptions.hidden = !enabled.goes;
    } else if (type === 'fire_hotspots' && map.getLayer('gm-fire-hotspots-layer')) {
      map.setLayoutProperty('gm-fire-hotspots-layer', 'visibility', visible ? 'visible' : 'none');
    } else if (type === 'daynight' && map.getLayer('gm-daynight-fill')) {
      map.setLayoutProperty('gm-daynight-fill', 'visibility', visible ? 'visible' : 'none');
    } else if (type === 'imagery' && map.getLayer('gm-imagery-layer')) {
      if (basemapImageryActive !== visible) {
        for (const { id, visibility } of basemapGeometryLayers) {
          map.setLayoutProperty(id, 'visibility', visible ? 'none' : visibility);
        }
        for (const entry of basemapLabelPaint) {
          if (visible) {
            map.setPaintProperty(entry.id, 'text-color', '#f4f7f6');
            map.setPaintProperty(entry.id, 'text-halo-color', '#111a20');
            map.setPaintProperty(entry.id, 'text-halo-width', 2.2);
            map.setPaintProperty(entry.id, 'text-halo-blur', 0.25);
            if (entry.id.startsWith('road_oneway') || entry.id === 'highway_name_other') map.setLayoutProperty(entry.id, 'visibility', 'none');
          } else {
            for (const property of ['text-color', 'text-halo-color', 'text-halo-width', 'text-halo-blur']) map.setPaintProperty(entry.id, property, entry.paint[property]);
            map.setLayoutProperty(entry.id, 'visibility', entry.layout.visibility);
          }
        }
        basemapImageryActive = visible;
      }
      map.setLayoutProperty('gm-imagery-layer', 'visibility', visible && (imageryMode === 'today' || imageryMode === 'previous') ? 'visible' : 'none');
      map.setLayoutProperty('gm-detail-imagery-layer', 'visibility', visible && imageryMode === 'detail' ? 'visible' : 'none');
      map.setLayoutProperty('gm-esri-imagery-layer', 'visibility', visible && imageryMode === 'esri' ? 'visible' : 'none');
      const regionalDetail = visible && (imageryMode === 'detail' || imageryMode === 'esri');
      map.setLayoutProperty('gm-europe-imagery-layer', 'visibility', regionalDetail ? 'visible' : 'none');
      map.setLayoutProperty('gm-us-imagery-layer', 'visibility', regionalDetail ? 'visible' : 'none');
      updateAircraftContrast();
      imageryOptions.hidden = !enabled.imagery;
    }
    const node = document.querySelector(`[data-count="${type}"]`);
    if (node) node.textContent = zoomHint(type) || (type === 'goes' && enabled.goes ? goesFrames.east.length && goesFrames.west.length ? 'East + West' : goesFrames.east.length ? 'East' : goesFrames.west.length ? 'West' : 'Loading' : type === 'radar' && enabled.radar ? radarProvider : type === 'imagery' && enabled.imagery ? imageryMode === 'detail' ? 'HD' : imageryMode === 'esri' ? 'Esri' : imageryDate() : type === 'ports' && enabled.ports && map.getZoom() < 6 && portOverviewCount ? portOverviewCount.toLocaleString() : enabled[type] && counts.get(type) != null ? Number(counts.get(type)).toLocaleString() : '');
  }

  function refreshImagery() {
    if (imageryMode === 'today' || imageryMode === 'previous') {
      const { key, url } = imageryTiles();
      if (styleReady && enabled.imagery && key !== imageryTileKey) {
        map.getSource('gm-imagery')?.setTiles([url]);
        imageryTileKey = key;
      }
    }
    imageryNote.textContent = imageryMode === 'detail'
      ? 'Free imagery · deep zoom in the contiguous U.S. and parts of Europe. Elsewhere the global image stops sharpening around zoom 12. Image dates vary; this is not live.'
      : imageryMode === 'esri'
        ? 'Esri World Imagery · detailed aerial and satellite views in many areas; resolution and image date vary by location. Noncommercial use unless separately licensed.'
      : imageryMode === 'today'
        ? `${imageryDate()} UTC · New passes appear as NASA processes them. Black areas have no current-day imagery yet; clouds may obscure the surface.`
        : `${imageryDate()} UTC · More complete daily composite. Captured at different times across the globe.`;
    for (const button of document.querySelectorAll('[data-imagery-mode]')) {
      button.setAttribute('aria-pressed', String(button.dataset.imageryMode === imageryMode));
    }
    if (styleReady) updateToggle('imagery');
  }

  function validCoordinate(lat, lon) {
    return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
  }

  function feature(type, ref, lon, lat, extra = {}) {
    return { type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: withGlobeVector({ ref, ...extra }, lon, lat) };
  }

  function setPoints(type, features, records) {
    refs.set(type, records);
    map.getSource(`gm-${type}`)?.setData({ type: 'FeatureCollection', features });
    setCount(type, features.length);
    fetchedAt.set(type, Date.now());
    updateToggle(type);
  }

  function iconImage(name, color, glyph) {
    const canvas = document.createElement('canvas');
    canvas.width = 48; canvas.height = 48;
    const ctx = canvas.getContext('2d');
    const satelliteAircraft = /-sat-icon$/.test(name);
    const type = name.replace(/^gm-/, '').replace(/-icon$/, '').replace(/-sat$/, '').replace(/-top$/, '');
    const path = (points, close = false) => {
      ctx.beginPath();
      points.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
      if (close) ctx.closePath();
    };
    ctx.translate(24, 24);
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.shadowColor = '#0d181c'; ctx.shadowBlur = 5;
    ctx.strokeStyle = '#142328'; ctx.fillStyle = color; ctx.lineWidth = 4.5;
    const badge = ['cameras', 'webcams', 'signs', 'sensors', 'lpr', 'temperature', 'construction', 'incidents', 'scans', 'power'].includes(type);
    if (badge) {
      ctx.fillStyle = '#1a2a2e';
      ctx.beginPath(); ctx.roundRect(-14, -14, 28, 28, type === 'scans' ? 8 : 5); ctx.fill();
      ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.stroke();
    }
    ctx.fillStyle = color; ctx.strokeStyle = color; ctx.lineWidth = 3;
    if (name.startsWith('gm-aircraft-') || type === 'govair' || type === 'civair' || type === 'tracked') {
      const helicopter = name.includes('-helicopter-');
      // The original AmericaMap 24-unit silhouettes, enlarged for MapLibre's
      // two-pixel-ratio sprite. Satellite mode adds a dark edge and shadow.
      ctx.save();
      ctx.scale(1.65, 1.65);
      ctx.translate(-12, -12);
      ctx.fillStyle = color;
      ctx.strokeStyle = satelliteAircraft ? '#07151c' : '#142328';
      ctx.lineWidth = satelliteAircraft ? 1.6 : 0.5;
      ctx.shadowColor = '#061118';
      ctx.shadowBlur = satelliteAircraft ? 5 : 3;
      ctx.shadowOffsetY = satelliteAircraft ? 1.2 : 0.5;
      const silhouette = () => { ctx.stroke(); ctx.fill(); };
      if (helicopter) {
        ctx.beginPath(); ctx.arc(12, 10, 9, 0, Math.PI * 2);
        ctx.globalAlpha = 0.16; ctx.fill(); ctx.globalAlpha = 1;
        ctx.strokeStyle = color; ctx.lineWidth = 0.9; ctx.stroke();
        ctx.shadowBlur = 0;
        ctx.lineCap = 'round';
        ctx.strokeStyle = satelliteAircraft ? '#07151c' : '#142328';
        ctx.lineWidth = satelliteAircraft ? 2.6 : 2;
        ctx.beginPath(); ctx.moveTo(3, 10); ctx.lineTo(21, 10); ctx.stroke();
        ctx.strokeStyle = color; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(3, 10); ctx.lineTo(21, 10); ctx.stroke();
        ctx.shadowBlur = satelliteAircraft ? 5 : 3;
        ctx.strokeStyle = satelliteAircraft ? '#07151c' : '#142328';
        ctx.lineWidth = satelliteAircraft ? 1.6 : 0.5;
        ctx.beginPath(); ctx.moveTo(12, 4);
        ctx.bezierCurveTo(15.5, 4, 17, 7.5, 17, 11);
        ctx.bezierCurveTo(17, 14.5, 15, 16.5, 12, 17.5);
        ctx.bezierCurveTo(9, 16.5, 7, 14.5, 7, 11);
        ctx.bezierCurveTo(7, 7.5, 8.5, 4, 12, 4); ctx.closePath(); silhouette();
        ctx.beginPath(); ctx.roundRect(11.2, 17, 1.6, 5.5, 0.8); silhouette();
        ctx.beginPath(); ctx.ellipse(12, 22.5, 3.5, 1.1, 0, 0, Math.PI * 2); silhouette();
        ctx.shadowBlur = 0;
        ctx.beginPath(); ctx.arc(12, 10, 1.3, 0, Math.PI * 2); ctx.fill();
      } else {
        ctx.beginPath(); ctx.ellipse(12, 12, 1.8, 8.5, 0, 0, Math.PI * 2); silhouette();
        path([[12,7],[22,17],[19,17.5],[12,13],[5,17.5],[2,17]], true); silhouette();
        path([[12,18.5],[16,23],[8,23]], true); silhouette();
      }
      ctx.restore();
    } else if (type === 'vessels') {
      path([[-14,2],[14,2],[10,10],[0,14],[-10,10]], true); ctx.fill();
      path([[-4,0],[-4,-9],[7,-9],[7,0]]); ctx.stroke();
      ctx.lineWidth = 2; path([[-10,16],[-5,14],[0,16],[5,14],[10,16]]); ctx.stroke();
    } else if (type === 'ports') {
      ctx.beginPath(); ctx.arc(0,-9,2,0,Math.PI*2); ctx.stroke();
      path([[0,-6],[0,11],[-9,6],[-11,3]]); ctx.stroke();
      path([[0,11],[9,6],[11,3]]); ctx.stroke();
      path([[-6,1],[6,1]]); ctx.stroke();
    } else if (type === 'cyclones') {
      ctx.fillStyle = '#19242d'; ctx.strokeStyle = '#f1e6f3'; ctx.lineWidth = 2.6;
      ctx.beginPath(); ctx.arc(0, 0, 17, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.strokeStyle = color; ctx.lineWidth = 3.8;
      ctx.beginPath(); ctx.arc(0, 0, 11, -2.05, 0.8); ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, 11, 1.1, 3.95); ctx.stroke();
      ctx.fillStyle = '#fff8ff'; ctx.beginPath(); ctx.arc(0, 0, 2.8, 0, Math.PI * 2); ctx.fill();
    } else if (type === 'earthquakes') {
      path([[0,-15],[15,0],[0,15],[-15,0]],true); ctx.stroke();
      path([[-10,1],[-4,1],[-1,-6],[3,7],[6,-2],[10,-2]]); ctx.stroke();
    } else if (type === 'nws_alerts' || type === 'world_alerts') {
      path([[0,-14],[11,-10],[10,4],[0,14],[-10,4],[-11,-10]],true); ctx.fill();
      ctx.fillStyle='#243033'; ctx.fillRect(-1.5,-7,3,12);
      ctx.beginPath(); ctx.arc(0,9,2,0,Math.PI*2); ctx.fill();
    } else if (type === 'fires') {
      ctx.beginPath(); ctx.moveTo(1,-16); ctx.bezierCurveTo(9,-7,14,-2,9,8); ctx.bezierCurveTo(5,15,-5,15,-9,7); ctx.bezierCurveTo(-12,1,-8,-5,-2,-8); ctx.bezierCurveTo(-2,-2,1,-4,1,-16); ctx.fill();
      ctx.fillStyle='#20282b'; ctx.beginPath(); ctx.moveTo(0,-1); ctx.quadraticCurveTo(5,4,2,10); ctx.quadraticCurveTo(-4,10,-3,5); ctx.closePath(); ctx.fill();
    } else if (type === 'floods') {
      path([[-13,-5],[-8,-8],[-3,-5],[2,-8],[7,-5],[12,-8]]); ctx.stroke();
      path([[-13,2],[-8,-1],[-3,2],[2,-1],[7,2],[12,-1]]); ctx.stroke();
      path([[-13,9],[-8,6],[-3,9],[2,6],[7,9],[12,6]]); ctx.stroke();
    } else if (type === 'volcanoes') {
      path([[-15,12],[-4,-8],[1,-4],[6,-8],[15,12]],true); ctx.fill();
      ctx.strokeStyle='#263137'; ctx.lineWidth=2.2; path([[-4,-8],[-1,-1],[1,-4],[3,-1],[6,-8]]); ctx.stroke();
      ctx.fillStyle=color; ctx.beginPath(); ctx.arc(-4,-13,2,0,Math.PI*2); ctx.arc(4,-16,2,0,Math.PI*2); ctx.fill();
    } else if (type === 'cameras' || type === 'webcams') {
      ctx.fillRect(-10,-5,16,12); path([[6,-2],[12,-6],[12,8],[6,4]],true); ctx.fill();
      ctx.fillStyle='#1a2a2e'; ctx.beginPath(); ctx.arc(-2,1,3.3,0,Math.PI*2); ctx.fill();
    } else if (type === 'scans') {
      ctx.beginPath(); ctx.arc(0,0,2,0,Math.PI*2); ctx.fill();
      ctx.beginPath(); ctx.arc(0,0,7,-1.1,1.2); ctx.stroke();
      ctx.beginPath(); ctx.arc(0,0,11,-1.2,1.3); ctx.stroke();
      path([[0,0],[9,-8]]); ctx.stroke();
    } else if (type === 'power') {
      path([[2,-11],[-4,0],[1,0],[-2,11],[7,-2],[2,-2]],true); ctx.fill();
    } else if (type === 'signs' || type === 'signs-alert') {
      ctx.fillRect(-10,-7,20,14); ctx.fillRect(-1,7,2,5);
      ctx.fillStyle='#1a2a2e'; ctx.font='bold 11px Arial'; ctx.textAlign='center'; ctx.textBaseline='middle'; ctx.fillText(type === 'signs-alert' ? '!' : '→',0,0);
    } else if (type === 'construction') {
      path([[0,-12],[12,10],[-12,10]],true); ctx.fill();
      ctx.fillStyle='#1a2a2e'; ctx.fillRect(-1.5,-3,3,7); ctx.fillRect(-1.5,6,3,3);
    } else if (type === 'incidents') {
      path([[0,-10],[10,8],[-10,8]],true); ctx.fill();
      ctx.fillStyle='#1a2a2e'; ctx.fillRect(-1.5,-3,3,6); ctx.fillRect(-1.5,5,3,2);
    } else if (type === 'emergency-fire' || type === 'international-fire') {
      ctx.beginPath();ctx.moveTo(0,-12);ctx.bezierCurveTo(9,-3,9,4,4,10);ctx.quadraticCurveTo(-5,15,-8,6);ctx.quadraticCurveTo(-9,1,-3,-5);ctx.quadraticCurveTo(-2,1,0,-12);ctx.fill();
    } else if (['emergency-police', 'emergency-patrol', 'international-police', 'international-patrol'].includes(type)) {
      path([[0,-11],[9,-7],[8,4],[0,12],[-8,4],[-9,-7]],true);ctx.fill();
      ctx.fillStyle='#1a2a2e';ctx.beginPath();ctx.arc(0,-1,2.5,0,Math.PI*2);ctx.fill();
    } else if (['emergency-traffic', 'emergency-warning', 'international-traffic', 'international-warning'].includes(type)) {
      path([[0,-12],[12,10],[-12,10]],true);ctx.fill();
      ctx.fillStyle='#1a2a2e';ctx.fillRect(-1.5,-4,3,7);ctx.fillRect(-1.5,5,3,2);
    } else if (['emergency', 'emergency-medical', 'international-medical'].includes(type)) {
      ctx.fillRect(-3,-11,6,22); ctx.fillRect(-11,-3,22,6);
    } else if (type === 'sensors') {
      ctx.beginPath();ctx.arc(0,0,3,0,Math.PI*2);ctx.fill();
      ctx.beginPath();ctx.arc(0,0,9,-2.3,-.8);ctx.stroke();ctx.beginPath();ctx.arc(0,0,9,.8,2.3);ctx.stroke();
    } else if (type === 'temperature') {
      ctx.fillRect(-2,-10,4,15);ctx.beginPath();ctx.arc(0,8,5,0,Math.PI*2);ctx.fill();
    } else if (type === 'lpr') {
      ctx.strokeRect(-10,-6,20,12);ctx.fillRect(-6,-1,12,2);
    } else {
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.font = 'bold 21px Arial, sans-serif'; ctx.fillText(glyph || '•', 0, 0);
    }
    map.addImage(name, ctx.getImageData(0, 0, 48, 48), { pixelRatio: 2 });
    const row = document.querySelector(`[data-layer="${type}"] .row-icon`);
    if (row && !satelliteAircraft) row.style.backgroundImage = `url(${canvas.toDataURL()})`;
  }

  function updateAircraftContrast() {
    const satellite = enabled.imagery;
    const size = satellite
      ? ['interpolate', ['linear'], ['zoom'], 0, 0.52, 5, 0.76, 6, 1.06, 7, 1.28, 8, 1.55, 12, 1.7]
      : ['interpolate', ['linear'], ['zoom'], 0, 0.45, 5, 0.65, 6, 0.95, 7, 1.15, 8, 1.4, 12, 1.5];
    for (const type of ['civair', 'govair']) {
      for (const id of [`gm-${type}-points`]) {
        if (!map.getLayer(id)) continue;
        map.setLayoutProperty(id, 'icon-image', ['concat', 'gm-aircraft-', ['get', 'service'], '-', ['get', 'shape'], satellite ? '-sat-icon' : '-icon']);
        map.setLayoutProperty(id, 'icon-size', size);
      }
    }
    if (map.getLayer('gm-tracked-plane')) {
      map.setLayoutProperty('gm-tracked-plane', 'icon-image', ['concat', 'gm-aircraft-', ['get', 'service'], '-', ['get', 'shape'], satellite ? '-sat-icon' : '-icon']);
      map.setLayoutProperty('gm-tracked-plane', 'icon-size', satellite ? 1.7 : 1.55);
    }
  }

  function addPointLayer(type) {
    const cfg = POINT[type];
    const sourceId = `gm-${type}`;
    const attribution = type === 'govair' || type === 'civair'
      ? 'Aircraft: <a href="https://opensky-network.org/">OpenSky</a> / <a href="https://www.adsb.lol/">ADSB.lol</a>'
      : type === 'vessels' ? 'Vessels: <a href="https://aisstream.io/">AISStream</a>'
      : type === 'webcams' ? 'Webcam catalog: <a href="https://github.com/simplifaisoul/osiris">OSIRIS</a> / camera operators'
      : type === 'lpr' ? 'Plate-reader locations: <a href="https://deflock.me/">DeFlock / OpenStreetMap</a> / <a href="https://zoek.officielebekendmakingen.nl/stcrt-2026-23725.html">Dutch Police</a> / <a href="https://gis.ktvis.lt/arcgis/rest/services/LAKD/EISMOINFO_SLUOKSNIAI/MapServer/13">Via Lietuva</a> / <a href="https://dati.comune.milano.it/dataset/ds959-varchi-areab">Comune di Milano Area B</a> / <a href="https://dati.comune.milano.it/dataset/ds82_infogeo_varchi_elettronici_localizzazione_">Area C</a> (CC BY)'
      : type === 'cyclones' ? 'Cyclones: <a href="https://eonet.gsfc.nasa.gov/">NASA EONET</a>'
      : type === 'earthquakes' ? 'Earthquakes: <a href="https://earthquake.usgs.gov/">USGS</a>'
      : type === 'nws_alerts' ? 'Weather alerts: <a href="https://api.weather.gov/alerts/active">National Weather Service</a>'
      : type === 'world_alerts' ? 'Weather alerts: <a href="https://api.weather.gc.ca/collections/weather-alerts">Environment and Climate Change Canada</a> (<a href="https://eccc-msc.github.io/open-data/licence/readme_en/">licence</a>) / <a href="https://alerts.metservice.com/cap/rss">© Meteorological Service of New Zealand Limited</a> (<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>) / <a href="https://api.met.no/weatherapi/metalerts/2.0/documentation">Norwegian Meteorological Institute</a> (<a href="https://api.met.no/doc/TermsOfService">CC BY 4.0</a>) / <a href="https://data.gov.ie/dataset/weather-warnings">Met Éireann</a> (custom open data licence) / <a href="https://data.gov.ie/dataset/counties-national-statutory-boundaries-2019-generalised-20m1">Tailte Éireann county boundaries</a> (CC BY 4.0) / <a href="https://opendata.dwd.de/weather/alerts/cap/DISTRICT_DWD_STAT/">Deutscher Wetterdienst</a> (<a href="https://www.dwd.de/copyright">licence</a>; district geometry © GeoBasis-DE / BKG) / <a href="https://www.prociv.azores.gov.pt/developer/">Azores Civil Protection</a>'
      : type === 'fires' ? 'Wildfires: <a href="https://www.gdacs.org/">Global Disaster Alert and Coordination System, GDACS</a>'
      : type === 'international' ? 'Emergency reports: <a href="https://www.rfs.nsw.gov.au/">NSW RFS</a> / <a href="https://www.fire.qld.gov.au/">Queensland Fire</a> / <a href="https://www.emergency.vic.gov.au/">VicEmergency</a> / <a href="https://www.civildefence.govt.nz/">NZ NEMA</a> / <a href="https://environment.data.gov.uk/flood-monitoring/doc/reference">Environment Agency</a> / <a href="https://einsatz.lsz-b.at/">LSZ Burgenland</a> / <a href="https://api.vedur.is/">Icelandic Meteorological Office</a> / <a href="https://dados.gov.pt/en/datasets/prociv-ocorrencias-em-aberto">Portugal ANEPC</a> / <a href="https://vmaapi.sr.se/">Sveriges Radio VMA</a> (municipality points: <a href="https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/digitala-granser/">SCB CC0</a>) / <a href="https://polisen.se/om-polisen/om-webbplatsen/oppna-data/api-over-polisens-handelser/">Swedish Police</a> (approximate area centers) / <a href="https://zwaailicht.nl/voorwaarden">Zwaailicht.nl P2000</a> (source-derived pager alerts)'
      : type === 'floods' || type === 'volcanoes' ? 'Hazards: <a href="https://www.gdacs.org/">Global Disaster Alert and Coordination System, GDACS</a>'
      : type === 'ports' ? 'Ports: <a href="https://msi.nga.mil/Publications/WPI">NGA World Port Index, 2024 snapshot</a>'
      : type === 'scans' ? 'Scan reports: <a href="https://isc.sans.edu/">SANS Internet Storm Center</a> / <a href="https://stat.ripe.net/">RIPEstat</a>' : '';
    map.addSource(sourceId, { type: 'geojson', data: EMPTY, attribution });
    iconImage(`${sourceId}-icon`, cfg.color, cfg.glyph);
    if (type === 'civair' || type === 'govair') iconImage(`${sourceId}-sat-icon`, type === 'civair' ? '#f3f5f4' : '#ffe2a0', cfg.glyph);
    if (type === 'civair') for (const [service, aircraftColor] of Object.entries(AIRCRAFT_COLORS)) {
      for (const shape of ['plane', 'helicopter']) {
        iconImage(`gm-aircraft-${service}-${shape}-icon`, aircraftColor, '');
        iconImage(`gm-aircraft-${service}-${shape}-sat-icon`, aircraftColor, '');
      }
    }
    let iconExpression = `${sourceId}-icon`;
    if (type === 'civair' || type === 'govair') iconExpression = ['concat', 'gm-aircraft-', ['get', 'service'], '-', ['get', 'shape'], enabled.imagery ? '-sat-icon' : '-icon'];
    if (type === 'scans') {
      iconImage('gm-scans-top-icon', '#e5af83', cfg.glyph);
      iconExpression = ['case', ['<=', ['get', 'rank'], 10], 'gm-scans-top-icon', 'gm-scans-icon'];
      const firstLabel = map.getStyle().layers.find(layer => layer.type === 'symbol')?.id;
      map.addLayer({
        id: 'gm-scans-density', type: 'heatmap', source: sourceId, maxzoom: 3.5,
        layout: { visibility: 'none' },
        paint: {
          'heatmap-weight': 1,
          'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 0, 24, 2, 36, 3.5, 52],
          'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 0, 1.1, 3.5, 1.55],
          'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'],
            0, 'rgba(0,0,0,0)', 0.05, 'rgba(100,56,83,0.30)',
            0.2, 'rgba(170,85,112,0.58)', 0.5, 'rgba(217,133,113,0.78)',
            1, 'rgba(239,194,145,0.92)'],
          'heatmap-opacity': ['interpolate', ['linear'], ['zoom'], 0, 0.95, 2.5, 0.9, 3.5, 0]
        }
      }, firstLabel);
    }
    if (type === 'signs') {
      iconImage('gm-signs-alert-icon', '#c78276', '!');
      iconExpression = ['case', ['boolean', ['get', 'alert'], false], 'gm-signs-alert-icon', `${sourceId}-icon`];
    }
    if (type === 'emergency' || type === 'international') {
      for (const [category, color, glyph] of [
        ['fire', '#bd896b', 'F'], ['medical', '#bd7778', '+'],
        ['police', '#7b9eb7', 'P'], ['traffic', '#bda168', 'T'],
        ['patrol', '#8fa7b4', 'P'], ['warning', '#baa06a', '!']
      ]) iconImage(`${sourceId}-${category}`, color, glyph);
      iconExpression = ['concat', `${sourceId}-`, ['get', 'category']];
    }
    const pointLayer = {
      id: `${sourceId}-points`, type: 'symbol', source: sourceId,
      ...(type === 'scans' ? { minzoom: 3.5 } : {}),
      layout: {
        'icon-image': iconExpression,
        'icon-size': type === 'earthquakes'
          ? ['interpolate', ['linear'], ['coalesce', ['get', 'magnitude'], 2.5], 2.5, 0.5, 5, 0.8, 7, 1.1]
          : type === 'nws_alerts' || type === 'world_alerts'
          ? ['interpolate', ['linear'], ['zoom'], 0, 0.55, 5, 0.8, 9, 1.05]
          : type === 'scans'
          ? ['interpolate', ['linear'], ['coalesce', ['get', 'rank'], 100], 1, 1.15, 100, 0.68]
          : type === 'civair' || type === 'govair'
          ? ['interpolate', ['linear'], ['zoom'], 0, 0.45, 5, 0.65, 6, 0.95, 7, 1.15, 8, 1.4, 12, 1.5]
          : ['interpolate', ['linear'], ['zoom'], 0, 0.88, 10, 1.1, 15, 1.22],
        'icon-allow-overlap': type !== 'sensors',
        'icon-ignore-placement': type !== 'sensors',
        'icon-rotate': ['coalesce', ['get', 'heading'], 0],
        visibility: 'none'
      }
    };
    map.addLayer(pointLayer);
    setPointLayerFilter(`${sourceId}-points`);
    if (type === 'scans') map.addLayer({
      id: 'gm-scans-top-points', type: 'symbol', source: sourceId, maxzoom: 3.5,
      filter: ['<=', ['get', 'rank'], 10],
      layout: { 'icon-image': 'gm-scans-top-icon', 'icon-size': 1.06,
        'icon-allow-overlap': true, 'icon-ignore-placement': true, visibility: 'none' },
    });
    if (type === 'scans') setPointLayerFilter('gm-scans-top-points', ['<=', ['get', 'rank'], 10]);
    for (const layerId of type === 'scans' ? ['gm-scans-points', 'gm-scans-top-points']
      : [`${sourceId}-points`]) {
      map.on('mouseenter', layerId, () => { if (!cyberFocus || type === 'scans') map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', layerId, () => { map.getCanvas().style.cursor = ''; });
      map.on('click', layerId, event => {
        if (enabled.radio && map.getLayer('gm-radio-points') &&
            map.queryRenderedFeatures(event.point, { layers: ['gm-radio-points'] }).length) return;
        const item = event.features?.[0];
        if (!item) return;
        if (type === 'govair' || type === 'civair') {
          if (window.globalMapTripPicking || window.globalMapDrawing) return;
          const ref = String(item.properties.ref || '').toLowerCase();
          const aircraft = aircraftSnapshot.find(row => String(row.hex || '').toLowerCase() === ref)
            || { hex: ref, flight: item.properties.flight || '', r: item.properties.registration || '',
              lat: item.geometry.coordinates[1], lon: item.geometry.coordinates[0] };
          activePointPopup?.remove();
          startTracking(aircraft, true);
          return;
        }
        openPopup(type, item.properties.ref, item.geometry.coordinates);
      });
    }
  }

  function addPowerGeometry() {
    map.addSource('gm-power', { type: 'geojson', data: EMPTY });
    iconImage('gm-power-icon', POINT.power.color, POINT.power.glyph);
    map.addLayer({
      id: 'gm-power-fill', type: 'fill', source: 'gm-power',
      filter: ['in', ['geometry-type'], ['literal', ['Polygon', 'MultiPolygon']]],
      paint: { 'fill-color': ['coalesce', ['get', 'color'], POINT.power.color], 'fill-opacity': 0.24 },
      layout: { visibility: 'none' }
    });
    map.addLayer({
      id: 'gm-power-line', type: 'line', source: 'gm-power',
      filter: ['in', ['geometry-type'], ['literal', ['Polygon', 'MultiPolygon']]],
      paint: { 'line-color': ['coalesce', ['get', 'color'], POINT.power.color], 'line-width': 1.6, 'line-opacity': 0.9 },
      layout: { visibility: 'none' }
    });
    map.addLayer({
      id: 'gm-power-points', type: 'symbol', source: 'gm-power',
      filter: ['==', ['geometry-type'], 'Point'],
      layout: { 'icon-image': 'gm-power-icon', 'icon-allow-overlap': true, 'icon-ignore-placement': true, visibility: 'none' }
    });
    setPointLayerFilter('gm-power-points', ['==', ['geometry-type'], 'Point']);
    for (const id of ['gm-power-fill', 'gm-power-line', 'gm-power-points']) {
      map.on('click', id, event => {
        const item = event.features?.[0];
        if (item) openPopup('power', item.properties.ref, event.lngLat.toArray());
      });
      map.on('mouseenter', id, () => { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', id, () => { map.getCanvas().style.cursor = ''; });
    }
  }

  function textElement(tag, className, value) {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = value || '';
    return node;
  }

  function appendLink(root, label, url) {
    try {
      const parsed = new URL(url, location.href);
      if (!['http:', 'https:'].includes(parsed.protocol)) return;
      const link = textElement('a', 'popup-link', label);
      link.href = parsed.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      root.append(link);
    } catch { /* Feed did not provide a valid URL. */ }
  }

  async function fetchTooltip(meta) {
    const params = new URLSearchParams({ state: meta.region.code, layer: meta.endpoint, id: String(meta.item.itemId) });
    const response = await fetch(`/511tooltip?${params}`);
    if (!response.ok) throw new Error(`Detail unavailable (${response.status})`);
    return response.json();
  }

  let hlsScriptPromise;
  function loadHlsScript() {
    if (!hlsScriptPromise) hlsScriptPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://cdn.jsdelivr.net/npm/hls.js@1.6.16/dist/hls.min.js';
      script.onload = resolve;
      script.onerror = () => reject(new Error('Video player could not load'));
      document.head.append(script);
    });
    return hlsScriptPromise;
  }

  function attachCameraMedia(root, meta, detail, popup) {
    const snapshot = detail?.snapshot_url || meta.item.expando?.snapshotUrl;
    const fallback = detail?.snapshot_fallback_url || meta.item.expando?.snapshotFallbackUrl;
    if (snapshot) {
      let img = document.createElement('img');
      img.className = 'popup-media';
      if (/^\/tfl-camera\//.test(snapshot)) img.classList.add('tfl-camera-image');
      img.alt = 'Latest road camera snapshot';
      // Popups are positioned inside MapLibre's transformed map container. Safari can
      // defer lazy images there even while the popup is visible, so load on selection.
      img.loading = 'eager';
      img.referrerPolicy = 'no-referrer';
      img.hidden = true;
      const status = textElement('span', 'popup-detail', 'Loading camera image…');
      let retries = 0;
      let usingFallback = false;
      let retryTimer;
      let loading = false;
      let lastLoadedAt = 0;
      let failureNonce = 0;
      let checkedFailedCamera = false;
      const retryButton = textElement('button', 'popup-play', 'Retry camera image');
      retryButton.type = 'button';
      const loadImage = () => {
        if (!popup.isOpen() || !img.isConnected || loading) return;
        loading = true;
        const url = usingFallback ? fallback : snapshot;
        const separator = url.includes('?') ? '&' : '?';
        const nextImage = document.createElement('img');
        nextImage.className = img.className;
        nextImage.alt = img.alt;
        nextImage.loading = 'eager';
        nextImage.referrerPolicy = 'no-referrer';
        // Keep the pending image connected: mobile Safari can defer images
        // created inside a transformed map popup until they enter the DOM.
        nextImage.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none';
        nextImage.setAttribute('aria-hidden', 'true');
        root.append(nextImage);
        nextImage.onload = () => {
          // Madrid's origin sometimes returns a small "unavailable" JPEG with
          // HTTP 200. Never present that as a working fallback camera view.
          if (usingFallback && /^https:\/\/informo\.madrid\.es\/cameras\/Camara\d+\.jpg$/.test(url)
              && (nextImage.naturalWidth < 600 || nextImage.naturalHeight < 350)) {
            nextImage.onerror();
            return;
          }
          loading = false;
          if (!popup.isOpen() || !img.isConnected) { nextImage.remove(); return; }
          nextImage.style.cssText = '';
          nextImage.removeAttribute('aria-hidden');
          nextImage.hidden = false;
          img.replaceWith(nextImage);
          img = nextImage;
          lastLoadedAt = Date.now();
          usingFallback = false;
          retries = 0;
          failureNonce = 0;
          status.remove();
          retryButton.remove();
        };
        nextImage.onerror = () => {
          loading = false;
          nextImage.remove();
          if (!popup.isOpen() || !img.isConnected) return;
          if (!checkedFailedCamera && /^\/(?:madrid|dgt|tfl)-camera\//.test(snapshot)) {
            checkedFailedCamera = true;
            // The server marks a failed official still unavailable. Refresh the
            // markers now so a dead camera does not stay clickable until the
            // normal road-layer poll.
            loadRoad('cameras').catch(error => console.warn('Camera catalog refresh:', error));
          }
          if (lastLoadedAt && Date.now() - lastLoadedAt > 10 * 60 * 1000) img.hidden = true;
          if (fallback && !usingFallback) {
            usingFallback = true;
            loadImage();
            return;
          }
          usingFallback = false;
          failureNonce = Date.now();
          if (retries < 4) {
            status.textContent = img.hidden ? 'Camera image delayed · retrying…'
              : 'Camera refresh delayed · showing last snapshot';
            if (!status.isConnected) root.append(status);
            retryTimer = window.setTimeout(loadImage, [1500, 5000, 15000, 30000][retries++]);
          } else {
            status.textContent = img.hidden ? 'Camera image unavailable right now.'
              : 'Camera refresh unavailable · showing last snapshot';
            if (!status.isConnected) root.append(status);
            if (!retryButton.isConnected) root.append(retryButton);
          }
        };
        // Share an image URL for each refresh window so browsers and the CDN can
        // reuse one still across visitors instead of hitting the provider per click.
        const refreshWindow = Math.max(60000, Number(meta.item.expando?.snapshotRefreshMs) || 60000);
        const version = Math.floor(Date.now() / refreshWindow);
        nextImage.src = `${url}${separator}v=${version}${failureNonce ? `&retry=${failureNonce}` : ''}`;
      };
      retryButton.addEventListener('click', () => {
        window.clearTimeout(retryTimer);
        retries = 0;
        usingFallback = false;
        failureNonce = Date.now();
        retryButton.remove();
        status.textContent = 'Loading camera image…';
        loadImage();
      });
      root.append(status, img);
      loadImage();
      const refreshMs = Number(meta.item.expando?.snapshotRefreshMs);
      if (refreshMs >= 60000) {
        const refresh = window.setInterval(() => {
          retries = 0;
          usingFallback = false;
          loadImage();
        }, refreshMs);
        popup.on('close', () => window.clearInterval(refresh));
      }
      popup.on('close', () => window.clearTimeout(retryTimer));
    }
    if (!(detail?.video_enabled || meta.item.expando?.videoEnabled)) return;
    const button = textElement('button', 'popup-play', '▶ Play live camera');
    button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true;
      button.textContent = 'Loading live video…';
      try {
        let streamUrl = detail?.video_url || meta.item.expando?.videoUrl;
        if (/^\d+$/.test(String(meta.item.itemId))) {
          try {
            const tokenResponse = await fetch(`/video-token?id=${encodeURIComponent(meta.item.itemId)}&state=${encodeURIComponent(meta.region.code)}`);
            if (tokenResponse.ok) streamUrl = (await tokenResponse.json()).url || streamUrl;
          } catch { /* Try the feed URL below. */ }
        }
        if (!streamUrl) throw new Error('Live stream unavailable');
        root.querySelectorAll('img.popup-media').forEach(image => image.remove());
        const video = document.createElement('video');
        video.className = 'popup-media';
        video.controls = true;
        video.autoplay = true;
        video.playsInline = true;
        root.append(video);
        if (video.canPlayType('application/vnd.apple.mpegurl')) {
          video.src = streamUrl;
        } else {
          await loadHlsScript();
          if (!window.Hls?.isSupported()) throw new Error('Live stream is unsupported in this browser');
          const hls = new window.Hls({ enableWorker: true });
          hls.loadSource(streamUrl);
          hls.attachMedia(video);
          hls.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
          popup.on('close', () => hls.destroy());
        }
        button.remove();
        video.play().catch(() => {});
      } catch (error) {
        button.disabled = false;
        button.textContent = 'Live video unavailable';
        console.warn(error);
      }
    });
    root.append(button);
  }

  function attachCameraGallery(root, views, refreshMs, popup) {
    const image = document.createElement('img');
    image.className = 'popup-media camera-gallery-image';
    image.alt = 'Latest road camera snapshot';
    const caption = textElement('span', 'popup-media-caption', '');
    const controls = document.createElement('div');
    controls.className = 'popup-camera-views';
    const previous = textElement('button', '', '← Previous');
    const next = textElement('button', '', 'Next →');
    previous.type = next.type = 'button';
    let index = 0;
    function show(position, refresh = false) {
      index = (position + views.length) % views.length;
      const view = views[index];
      image.hidden = false;
      caption.textContent = `${index + 1} / ${views.length} · ${view.label}`;
      image.src = refresh ? `${view.url}${view.url.includes('?') ? '&' : '?'}v=${Date.now()}` : view.url;
    }
    image.addEventListener('error', () => {
      image.hidden = true;
      caption.textContent = `View ${index + 1} unavailable · try another view`;
    });
    previous.addEventListener('click', () => show(index - 1));
    next.addEventListener('click', () => show(index + 1));
    controls.append(previous, next);
    root.append(caption, controls, image);
    show(0);
    if (refreshMs >= 60000) {
      const refresh = window.setInterval(() => show(index, true), refreshMs);
      popup.on('close', () => window.clearInterval(refresh));
    }
  }

  function webcamMedia(camera) {
    try {
      const url = new URL(camera.url);
      if (camera.source === 'SkylineWebcams' && url.hostname === 'www.skylinewebcams.com') {
        const id = camera.id.match(/-(\d+)$/)?.[1];
        if (id) return { snapshotUrl: `https://embed.skylinewebcams.com/img/${id}.jpg`, refreshMs: 300000,
          caption: 'Camera image · updates about every 5 min' };
      }
      if (url.hostname === 'www.ipcamlive.com' && /^\/[a-z0-9_-]+\/?$/i.test(url.pathname)) {
        const alias = url.pathname.split('/')[1];
        return { snapshotUrl: `https://www.ipcamlive.com/player/snapshot.php?alias=${encodeURIComponent(alias)}`,
          embedUrl: `https://www.ipcamlive.com/player/player.php?alias=${encodeURIComponent(alias)}&autoplay=0`,
          refreshMs: 120000, caption: 'Camera preview · updates about every 2 min' };
      }
      if (url.hostname === 'rtsp.me' && /^\/embed\/[a-z0-9]+\/?$/i.test(url.pathname)) {
        return { embedUrl: url.href };
      }
    } catch { /* Some catalog entries may only have a stream URL. */ }
    return {};
  }

  function attachWebcamMedia(root, meta, popup) {
    const media = webcamMedia(meta);
    let snapshot;
    let snapshotTimer;
    if (media.snapshotUrl) {
      snapshot = document.createElement('img');
      snapshot.className = 'popup-media';
      snapshot.alt = `Latest image from ${meta.title}`;
      snapshot.loading = 'lazy';
      const refresh = () => {
        const separator = media.snapshotUrl.includes('?') ? '&' : '?';
        snapshot.src = `${media.snapshotUrl}${separator}v=${Math.floor(Date.now() / media.refreshMs)}`;
      };
      snapshot.addEventListener('error', () => {
        if (!snapshot.isConnected) return;
        snapshot.hidden = true;
        caption.textContent = 'Camera preview unavailable';
      });
      snapshot.addEventListener('load', () => {
        snapshot.hidden = false;
        caption.textContent = media.caption;
      });
      if (meta.url && media.snapshotUrl && !media.embedUrl) {
        const imageLink = document.createElement('a');
        imageLink.href = meta.url;
        imageLink.target = '_blank';
        imageLink.rel = 'noopener noreferrer';
        imageLink.title = 'Watch live video at the operator site';
        imageLink.append(snapshot);
        root.append(imageLink);
      } else {
        root.append(snapshot);
      }
      const caption = textElement('span', 'popup-media-caption', media.caption);
      root.append(caption);
      refresh();
      snapshotTimer = window.setInterval(refresh, media.refreshMs);
      popup.on('close', () => window.clearInterval(snapshotTimer));
    }
    if (meta.streamUrl) {
      attachCameraMedia(root, { item: { itemId: meta.id, expando: { videoEnabled: true, videoUrl: meta.streamUrl } } }, null, popup);
    } else if (media.embedUrl) {
      const button = textElement('button', 'popup-play', '▶ Play live camera');
      button.type = 'button';
      button.addEventListener('click', () => {
        button.disabled = true;
        button.textContent = 'Loading live player…';
        const frame = document.createElement('iframe');
        frame.className = 'popup-media popup-video-frame';
        frame.title = `Live camera: ${meta.title}`;
        frame.allow = 'autoplay; fullscreen';
        frame.allowFullscreen = true;
        frame.referrerPolicy = 'strict-origin-when-cross-origin';
        frame.hidden = true;
        let loaded = false;
        const timeout = window.setTimeout(() => {
          if (loaded || !popup.isOpen()) return;
          frame.remove();
          button.disabled = false;
          button.textContent = 'Retry live player';
        }, 12000);
        popup.on('close', () => window.clearTimeout(timeout));
        frame.addEventListener('load', () => {
          try {
            if (frame.contentDocument?.URL === 'about:blank') return;
          } catch { /* A cross-origin player has loaded. */ }
          if (!popup.isOpen()) return;
          loaded = true;
          window.clearTimeout(timeout);
          window.clearInterval(snapshotTimer);
          snapshot?.remove();
          root.querySelector('.popup-media-caption')?.remove();
          frame.hidden = false;
          button.remove();
        });
        root.insertBefore(frame, button);
        frame.src = media.embedUrl;
      });
      root.append(button);
    }
    if (meta.url) appendLink(root, media.snapshotUrl && !media.embedUrl ? 'Watch live at operator site ↗' : 'Open camera at operator site ↗', meta.url);
  }

  function openPopup(type, ref, coordinates) {
    if (window.globalMapTripPicking || window.globalMapDrawing) return;
    if (!enabled[type] || zoomHint(type) || (cyberFocus && type !== 'scans')) return;
    const meta = refs.get(type)?.get(ref);
    if (!meta) return;
    activePointPopup?.remove();
    const root = document.createElement('div');
    const title = textElement('strong', 'popup-title', meta.title || POINT[type].label);
    const detail = textElement('span', 'popup-detail', meta.detail || '');
    root.append(textElement('span', 'popup-kicker', meta.source || POINT[type].label), title);
    if (meta.detail) root.append(detail);
    if ((type === 'nws_alerts' || type === 'world_alerts') && meta.advice) root.append(textElement('span', 'popup-detail', meta.advice));
    if (type === 'signs' && meta.signImage) {
      const preview = document.createElement('img');
      preview.className = 'popup-media sign-preview';
      preview.alt = 'Current display on digital road sign';
      preview.src = `data:image/png;base64,${meta.signImage}`;
      root.append(preview);
    }
    const point = map.project(coordinates);
    const visibleLeft = window.innerWidth > 900 && !document.body.classList.contains('panel-collapsed') ? 288 : 0;
    // Keep phone popups inside the map even when their marker is near an edge.
    const verticalAnchor = point.y < map.getCanvas().clientHeight * (type === 'webcams' || type === 'cameras' ? 0.6 : 0.5)
      ? 'top' : 'bottom';
    const mobileWidth = map.getCanvas().clientWidth;
    const mobileAnchor = point.x < 174 ? `${verticalAnchor}-left`
      : point.x > mobileWidth - 174 ? `${verticalAnchor}-right` : verticalAnchor;
    const anchor = mobileWidth < 600 ? mobileAnchor
      : point.x < visibleLeft + 180 ? 'left' : point.x > map.getCanvas().clientWidth - 180 ? 'right'
        : verticalAnchor;
    const popup = new maplibregl.Popup({ closeButton: true, maxWidth: '315px', offset: 14, anchor })
      .setLngLat(coordinates).setDOMContent(root).addTo(map);
    if (type === 'cameras' && (anchor.startsWith('top') || anchor.startsWith('bottom'))) {
      const room = anchor.startsWith('top') ? map.getCanvas().clientHeight - point.y : point.y;
      popup.getElement().querySelector('.maplibregl-popup-content').style.maxHeight = `${Math.max(150, Math.floor(room - 30))}px`;
    }
    activePointPopup = popup;
    if (type === 'webcams') {
      attachWebcamMedia(root, meta, popup);
      return;
    }
    if (meta.sourceUrl) appendLink(root,
      meta.sourceUrl.startsWith('https://zwaailicht.nl/') ? 'Zwaailicht.nl ↗' : 'View source ↗',
      meta.sourceUrl);
    if (meta.lithuaniaEventId && (type === 'construction' || type === 'incidents')) {
      const controller = new AbortController();
      popup.on('close', () => controller.abort());
      fetch(`/lithuania-road-event/${encodeURIComponent(meta.lithuaniaEventId)}`, {
        signal: controller.signal
      }).then(async response => {
        if (!response.ok) throw new Error(`Lithuania road event: ${response.status}`);
        return response.json();
      }).then(info => {
        if (!popup.isOpen()) return;
        title.textContent = info.title || meta.title;
        detail.textContent = info.detail || meta.detail;
      }).catch(error => {
        if (!controller.signal.aborted) console.warn('Lithuania road event detail:', error);
      });
    }
    if (type === 'sensors' && meta.sensorId) {
      const live = textElement('span', 'popup-detail', 'Checking for a recent vehicle detection…');
      root.append(live);
      const controller = new AbortController();
      popup.on('close', () => controller.abort());
      fetch(`/international-sensor-sample?${new URLSearchParams({ id: meta.sensorId })}`, {
        signal: controller.signal
      }).then(async response => {
        if (!response.ok) throw new Error(`Sensor sample: ${response.status}`);
        return response.json();
      }).then(sample => {
        if (!popup.isOpen()) return;
        const observed = new Date(sample.observed_at);
        live.textContent = `Recent detection: ${sample.vehicle} · ${observed.toLocaleTimeString([], {
          hour: 'numeric', minute: '2-digit'
        })}${sample.lane ? ` · lane ${sample.lane}` : ''}`;
      }).catch(error => {
        if (controller.signal.aborted || !popup.isOpen()) return;
        console.warn('Zurich sensor sample:', error);
        live.textContent = 'No recent vehicle detection available.';
      });
    }
    if (type === 'cyclones') showCycloneGuidance(ref, meta, coordinates, popup);
    if (type === 'ports') {
      const button = textElement('button', 'popup-play', 'Check sea conditions');
      button.type = 'button';
      button.addEventListener('click', () => openSeaConditions(coordinates));
      root.append(button);
    }
    if (type === 'cameras' && meta.snapshotUrl) {
      if (meta.cameraViews?.length > 1) {
        attachCameraGallery(root, meta.cameraViews, meta.snapshotRefreshMs, popup);
        return;
      }
      attachCameraMedia(root, { item: { expando: { snapshotUrl: meta.snapshotUrl,
        snapshotFallbackUrl: meta.snapshotFallbackUrl,
        snapshotRefreshMs: meta.snapshotRefreshMs } } }, null, popup);
      return;
    }
    if (!meta.region || !meta.item) return;
    if (type === 'cameras') {
      attachCameraMedia(root, meta, null, popup);
      fetchTooltip(meta).then(info => {
        if (!popup.isOpen()) return;
        title.textContent = info.name || info.msg || meta.title || 'Traffic camera';
        if (root.querySelector('video')) return;
        root.querySelectorAll('.popup-media, .popup-play').forEach(node => node.remove());
        attachCameraMedia(root, meta, info, popup);
      }).catch(error => console.warn('Camera detail:', error));
    } else if (type === 'signs' || type === 'incidents' || type === 'construction') {
      fetchTooltip(meta).then(info => {
        if (!popup.isOpen()) return;
        if (info.name && !meta.title) title.textContent = info.name;
        const description = [info.msg, info.severity, info.timestamp].filter(Boolean).join(' · ');
        if (description) {
          detail.textContent = description;
          if (!detail.isConnected) root.append(detail);
        }
      }).catch(error => console.warn('Road detail:', error));
    }
  }

  function currentBounds() {
    const b = map.getBounds();
    return { west: b.getWest(), south: b.getSouth(), east: b.getEast(), north: b.getNorth() };
  }

  function regionVisible(region, b) {
    const r = region.bounds;
    if (r.minLat > b.north || r.maxLat < b.south) return false;
    return b.west <= b.east ? r.minLon <= b.east && r.maxLon >= b.west : r.minLon <= b.east || r.maxLon >= b.west;
  }

  function inBounds(lat, lon, b) {
    if (lat < b.south - 0.2 || lat > b.north + 0.2) return false;
    return b.west <= b.east ? lon >= b.west - 0.2 && lon <= b.east + 0.2 : lon >= b.west || lon <= b.east;
  }

  async function fetchRoad(region, endpoint, refreshMs, signal) {
    const key = `${region.code}/${endpoint}`;
    const cached = roadCache.get(key);
    if (cached && Date.now() - cached.at < refreshMs) return cached.items;
    const response = await fetch(`/511/${region.code}/${endpoint}`, { signal });
    if (!response.ok) throw new Error(`${region.name} ${endpoint}: ${response.status}`);
    const data = await response.json();
    const items = Array.isArray(data.item2) ? data.item2 : [];
    roadCache.set(key, { at: Date.now(), items });
    return items;
  }

  async function fetchInternationalRoad(type, bounds, signal) {
    const bbox = [bounds.west, bounds.south, bounds.east, bounds.north].map(value => Number(value.toFixed(4))).join(',');
    const response = await fetch(`/international-roads?${new URLSearchParams({ layer: type, bbox })}`, { signal });
    if (!response.ok) throw new Error(`International ${type}: ${response.status}`);
    return response.json();
  }

  async function loadRoad(type) {
    if (!styleReady || !enabled[type] || zoomHint(type)) return;
    requests.get(type)?.abort();
    const controller = new AbortController();
    requests.set(type, controller);
    requestStartedAt.set(type, Date.now());
    const bounds = currentBounds();
    const internationalVisible = ['signs', 'incidents', 'construction', 'cameras', 'sensors'].includes(type) && [
      { bounds: { minLon: 4, maxLon: 32, minLat: 57, maxLat: 72 } },
      { bounds: { minLon: 19, maxLon: 32, minLat: 59, maxLat: 71 } },
      { bounds: { minLon: -9, maxLon: 3, minLat: 49, maxLat: 61.5 } },
      { bounds: { minLon: -25, maxLon: -13, minLat: 63, maxLat: 67.5 } },
      { bounds: { minLon: -6, maxLon: 10, minLat: 41, maxLat: 52 } },
      { bounds: { minLon: 3, maxLon: 7.4, minLat: 50.6, maxLat: 53.8 } },
      { bounds: { minLon: 5.5, maxLon: 15.5, minLat: 47, maxLat: 55.1 } },
      { bounds: { minLon: 9, maxLon: 17, minLat: 46, maxLat: 49 } },
      { bounds: { minLon: 14, maxLon: 24.3, minLat: 48.8, maxLat: 55.2 } },
      { bounds: { minLon: 16.7, maxLon: 22.6, minLat: 47.7, maxLat: 49.7 } },
      { bounds: { minLon: 20.8, maxLon: 26.9, minLat: 53.8, maxLat: 56.5 } },
      { bounds: { minLon: 10, maxLon: 13, minLat: 46, maxLat: 48 } },
      { bounds: { minLon: -19, maxLon: 5, minLat: 27, maxLat: 45 } },
      { bounds: { minLon: 31.9, maxLon: 34.8, minLat: 34.4, maxLat: 35.8 } }
    ].some(region => regionVisible(region, bounds));
    const internationalPromise = internationalVisible
      ? fetchInternationalRoad(type, bounds, controller.signal).catch(error => { console.warn('International road feed:', error); return null; })
      : Promise.resolve(null);
    const regions = roadRegions.filter(region => regionVisible(region, bounds)).slice(0, 18);
    const jobs = regions.flatMap(region => type === 'incidents'
      ? (region.incidentLayers || ['Incidents']).map(endpoint => ({ region, endpoint }))
      : [{ region, endpoint: ROAD[type] }]);
    let next = 0;
    let failures = 0;
    const rows = [];
    await Promise.all(Array.from({ length: Math.min(5, jobs.length) }, async () => {
      while (next < jobs.length && !controller.signal.aborted) {
        const { region, endpoint } = jobs[next++];
        try {
          const items = await fetchRoad(region, endpoint, POINT[type].refreshMs, controller.signal);
          rows.push(...items.map(item => ({ item, region, endpoint })));
        } catch (error) {
          if (!controller.signal.aborted) { failures += 1; console.warn('Road feed:', error); }
        }
      }
    }));
    if (controller.signal.aborted || !enabled[type]) return;
    const features = [];
    const records = new Map();
    const seen = new Set();
    for (const row of rows) {
      const lat = Number(row.item.location?.[0]);
      const lon = Number(row.item.location?.[1]);
      if (!validCoordinate(lat, lon) || !inBounds(lat, lon, bounds)) continue;
      const ref = `${row.region.code}:${row.endpoint}:${row.item.itemId || row.item.title}:${lat}:${lon}`;
      if (seen.has(ref)) continue;
      seen.add(ref);
      const expando = row.item.expando || {};
      const title = row.item.title || (type === 'cameras' ? 'Traffic camera' : type === 'signs' ? 'Message sign' : type === 'construction' ? 'Construction' : expando.feedLabel || 'Road incident');
      const detail = [expando.description || expando.message, expando.severity, expando.timestamp].filter(Boolean).join(' · ');
      records.set(ref, { title, detail, source: `${row.region.name} 511`, item: row.item, region: row.region, endpoint: row.endpoint });
      features.push(feature(type, ref, lon, lat, { alert: type === 'signs' && /\b(alert|closed|warning)\b/i.test(expando.message || '') }));
    }
    const international = await internationalPromise;
    if (controller.signal.aborted || requests.get(type) !== controller || !enabled[type]) return;
    for (const item of international?.features || []) {
      const [lon, lat] = item.geometry?.coordinates || [];
      if (!validCoordinate(lat, lon) || !inBounds(lat, lon, bounds)) continue;
      const p = item.properties || {};
      const ref = String(p.key || `abroad:${type}:${lat}:${lon}`);
      if (seen.has(ref)) continue;
      seen.add(ref);
      const updatedAt = Number.isFinite(p.updated_at) && p.updated_at > 1e9
        ? `${new Date(p.updated_at * 1000).toLocaleString('en-US', {
          month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC'
        })} UTC`
        : p.updated_at;
      records.set(ref, {
        title: p.title || POINT[type].label,
        detail: [p.detail, updatedAt ? `Updated ${updatedAt}` : ''].filter(Boolean).join(' · '),
        source: p.source || 'Public road authority', sourceUrl: p.source_url,
        signImage: type === 'signs' ? p.image_data || '' : '',
        snapshotUrl: type === 'cameras' ? p.snapshot_url || '' : '',
        snapshotFallbackUrl: type === 'cameras' ? p.snapshot_fallback_url || '' : '',
        lithuaniaEventId: p.lithuania_event_id || '',
        cameraViews: type === 'cameras' && Array.isArray(p.camera_views) ? p.camera_views : [],
        snapshotRefreshMs: type === 'cameras' ? Number(p.snapshot_refresh_ms) || 0 : 0
      });
      features.push(feature(type, ref, lon, lat, { alert: type === 'signs' && /warning/i.test(p.title || '') }));
    }
    if (international?.sourceErrors?.length) console.warn('Some international road feeds are unavailable:', international.sourceErrors);
    if ((failures || (internationalVisible && !international)) && !features.length) {
      // Keep the last working markers when an upstream request briefly fails.
      // Retry sooner than the normal refresh without starting a request every tick.
      fetchedAt.set(type, Date.now() - POINT[type].refreshMs + 30000);
      showStatus(`${POINT[type].label} feed is temporarily unavailable. Retrying.`, true);
      return;
    }
    setPoints(type, features, records);
    if (international?.loading) {
      // The server is still filling its first road snapshot after a restart.
      // Pick up additional camera and road sources without waiting for the
      // normal layer refresh interval.
      fetchedAt.set(type, Date.now() - POINT[type].refreshMs + 10000);
    }
  }

  const GOV_CALLSIGNS = /^(CBP|FED|DOJ|DEA|FBI|ATF|FAMS|HSI|ALEA|GSP|MHP|FHP|FLHP|OPD|OCSO|PCSO|HCSO|BCSO|SCSO|LCSO|MCSO|FLPD|OIPD|SHERIFF|TROOPER|POLICE|PATROL|RESCUE|JOLLY|PEDRO|KING|REACH|EVAC|MEDEVAC|DUSTOFF)/i;
  const GOV_TERMS = /\b(MEDIC|MEDEVAC|LIFEFLIGHT|LIFE FLIGHT|AIR METHODS|SHERIFF|POLICE|FIRE|RESCUE|PATROL|MILITARY|ARMY|NAVY|COAST GUARD|STATE OF|DEPARTMENT OF PUBLIC SAFETY)\b/i;
  const MEDICAL_AIR = /\b(EMS|MEDIC|MEDEVAC|LIFE ?FLIGHT|AIR MEDICAL|AIR AMBULANCE|AIR METHODS|PHI AIR|AIR EVAC|CAREFLITE|CARE FLIGHT|LIFESTAR|LIFENET|AIRLIFE|GUARDIAN FLIGHT)\b/i;
  const FIRE_AIR = /\b(FIRE|RESCUE|SEARCH AND RESCUE)\b/i;
  const POLICE_AIR = /\b(SHERIFF|POLICE|HIGHWAY PATROL|STATE PATROL|TROOPER|PATROL|LAW ENFORCEMENT|BORDER PATROL|CUSTOMS|HOMELAND SECURITY|DEPARTMENT OF PUBLIC SAFETY)\b/i;
  function aircraftService(item) {
    const callsign = String(item.flight || '').trim();
    const owner = `${registryCache.get(String(item.hex || '').toLowerCase()) || ''} ${item.ownOp || ''}`;
    if (/^ae/i.test(item.hex || '') || (Number(item.dbFlags || 0) & 1) ||
        /\b(US ARMY|US NAVY|US AIR FORCE|US MARINE|NATIONAL GUARD|COAST GUARD|DEPARTMENT OF DEFENSE)\b/i.test(owner) ||
        /^(JOLLY|PEDRO|KING|REACH|EVAC|DUSTOFF)/i.test(callsign)) return 'military';
    if (MEDICAL_AIR.test(`${callsign} ${owner}`)) return 'medical';
    if (FIRE_AIR.test(`${callsign} ${owner}`)) return 'fire';
    if (POLICE_AIR.test(`${callsign} ${owner}`) || GOV_CALLSIGNS.test(callsign)) return 'police';
    if (GOV_TERMS.test(owner)) return 'gov';
    return 'civil';
  }
  function aircraftShape(item) {
    const type = String(item.t || '').toUpperCase();
    const description = String(item.desc || '').toUpperCase();
    return String(item.category || '').toUpperCase() === 'A7' || HELICOPTER_TYPES.has(type) ||
      /\b(HELICOPTER|ROTORCRAFT|ROBINSON|SIKORSKY|EUROCOPTER|AGUSTA|AW109|AW119|AW139|AW169|AW189|EC135|EC145|AS350|BK117)\b/.test(description)
      ? 'helicopter' : 'plane';
  }
  function aircraftAltitudeMeters(item) {
    if (!item || item.ground || item.alt_baro === 'ground') return null;
    // ADS-B geometric altitude is closer to sea-level height than pressure altitude.
    const feet = Number.isFinite(item.alt_geom) ? item.alt_geom : item.alt_baro;
    return Number.isFinite(feet) && feet > 0 && feet < 65000 ? Math.round(feet * 0.3048) : null;
  }
  function governmentAircraft(item) {
    return aircraftService(item) !== 'civil';
  }

  function renderAircraft(items) {
      const zoom = map.getZoom();
      const bounds = currentBounds();
      const visible = zoom < 3.5 ? items : items.filter(item => inBounds(Number(item.lat), Number(item.lon), bounds));
      const civilCount = visible.reduce((total, item) => total + !governmentAircraft(item), 0);
      const civilStep = Math.max(1, Math.ceil(civilCount / 4500));
      for (const type of ['govair', 'civair']) {
        const features = [];
        const records = new Map();
        const overviewCells = new Set();
        let civilIndex = 0;
        for (const item of visible) {
          const lat = Number(item.lat), lon = Number(item.lon);
          if (!validCoordinate(lat, lon) || item.alt_baro === 'ground' || item.ground) continue;
          const government = governmentAircraft(item);
          if (government !== (type === 'govair')) continue;
          if (!government && zoom < 3.5) {
            const cellSize = zoom < 2.5 ? 5 : 3;
            const cell = `${Math.floor(lat / cellSize)}:${Math.floor(lon / cellSize)}`;
            if (overviewCells.has(cell)) continue;
            overviewCells.add(cell);
          } else if (!government && civilIndex++ % civilStep !== 0) continue;
          const ref = String(item.hex || `${lat}:${lon}:${item.flight || ''}`);
          const title = String(item.flight || item.r || item.hex || 'Aircraft').trim();
          const detail = [item.r, typeof item.alt_baro === 'number' ? `${Math.round(item.alt_baro).toLocaleString()} ft` : '', typeof item.gs === 'number' ? `${Math.round(item.gs)} kt` : '', item.hex ? `ICAO ${item.hex}` : ''].filter(Boolean).join(' · ');
          const service = aircraftService(item);
          const shape = aircraftShape(item);
          const kind = service === 'civil' ? 'Civil' : ({ medical: 'Medical', police: 'Law enforcement', fire: 'Fire / rescue', military: 'Military', gov: 'Government' })[service];
          const extra = [item.desc, item.ownOp, item.squawk ? `Squawk ${item.squawk}` : ''].filter(Boolean).join(' · ');
          records.set(ref, { title, detail: [kind + (shape === 'helicopter' ? ' helicopter' : ' aircraft'), detail, extra].filter(Boolean).join(' · '), source: `${aircraftProvider} · observed position` });
          features.push(feature(type, ref, lon, lat, { heading: Number(item.track) || 0, service, shape,
            flight: String(item.flight || ''), registration: String(item.r || ''),
            altitude_m: aircraftAltitudeMeters(item) }));
        }
        setPoints(type, features, records);
      }
  }

  async function enrichAircraft(items) {
    if (map.getZoom() < 6) return;
    const bounds = currentBounds();
    const candidates = items.filter(item => {
      const lat = Number(item.lat), lon = Number(item.lon);
      if (!validCoordinate(lat, lon) || !inBounds(lat, lon, bounds)) return false;
      if (!String(item.r || '').startsWith('N') || aircraftShape(item) !== 'helicopter') return false;
      return Boolean(item.hex) && !registryCache.has(String(item.hex).toLowerCase());
    }).slice(0, 36);
    let next = 0;
    await Promise.all(Array.from({ length: Math.min(3, candidates.length) }, async () => {
      while (next < candidates.length) {
        const item = candidates[next++];
        const hex = String(item.hex).toLowerCase();
        try {
          const response = await fetch(`/registry?icao=${encodeURIComponent(hex)}`);
          if (!response.ok) throw new Error(`${response.status}`);
          const info = await response.json();
          registryCache.set(hex, info?.response?.aircraft?.registered_owner || '');
        } catch { registryCache.set(hex, ''); }
      }
    }));
    if (enabled.govair || enabled.civair) renderAircraft(items);
  }

  async function loadAircraft() {
    if (!styleReady || (!enabled.govair && !enabled.civair) || document.hidden) return;
    if (aircraftPromise) return aircraftPromise;
    const local = map.getZoom() >= 6;
    const retryScope = local ? 'local' : 'world';
    if (Date.now() < aircraftRetryAt[retryScope]) {
      if (aircraftSnapshot.length) renderAircraft(aircraftSnapshot);
      return;
    }
    if (local && trackedAircraft && trackingFollow && aircraftSnapshot.length && Date.now() - aircraftFetchedAt < 45000) {
      renderAircraft(aircraftSnapshot);
      return;
    }
    const center = map.getCenter();
    const bounds = currentBounds();
    const halfLat = Math.abs(bounds.north - bounds.south) * 55.5;
    const halfLon = Math.abs(bounds.east - bounds.west) * 55.5 * Math.max(0.15, Math.cos(center.lat * Math.PI / 180));
    const dist = Math.min(2000, Math.max(100, Math.ceil(Math.hypot(halfLat, halfLon) * 1.3 / 100) * 100));
    const queryLat = Math.max(-90, Math.min(90, Math.round(center.lat * 4) / 4));
    const queryLon = Math.round(((((center.lng + 180) % 360 + 360) % 360) - 180) * 4) / 4;
    const key = local ? `local:${queryLat}:${queryLon}:${dist}` : 'world';
    const ttl = local ? 45000 : 1800000;
    if (aircraftQueryKey === key && Date.now() - aircraftFetchedAt < ttl) {
      renderAircraft(aircraftSnapshot);
      return;
    }
    aircraftPromise = (async () => {
      const query = local ? new URLSearchParams({ scope: 'local', lat: String(queryLat), lon: String(queryLon), dist: String(dist) }) : new URLSearchParams({ scope: 'world' });
      const response = await fetch(`/aircraft?${query}`);
      if (response.status === 429) {
        const retryAt = Date.now() + retryAfterMs(response);
        if (aircraftSnapshot.length) renderAircraft(aircraftSnapshot);
        const reason = (await response.json().catch(() => ({}))).error;
        aircraftRetryAt[retryScope] = retryAt;
        if (reason === 'aircraft_request_limit') aircraftRetryAt[local ? 'world' : 'local'] = retryAt;
        if (!aircraftSnapshot.length && Date.now() - aircraftBusyNoticeAt > 600000) {
          aircraftBusyNoticeAt = Date.now();
          showStatus(reason === 'aircraft_request_limit'
            ? 'Too many aircraft requests from this connection. Pausing updates.'
            : 'Aircraft feed temporarily unavailable. Retrying shortly.', true);
        }
        return;
      }
      if (!response.ok) throw new Error(`Aircraft feed: ${response.status}`);
      const data = await response.json();
      aircraftSnapshot = data.aircraft || [];
      aircraftProvider = data.source || 'Aircraft telemetry';
      aircraftQueryKey = key;
      aircraftFetchedAt = Date.now();
      renderAircraft(aircraftSnapshot);
      if (data.stale && Date.now() - aircraftStaleNoticeAt > 1800000) {
        aircraftStaleNoticeAt = Date.now();
        showStatus('Aircraft provider delayed · showing last reported positions.', true, 6000);
      }
      await enrichAircraft(aircraftSnapshot);
    })().catch(error => { console.warn(error); showStatus('Aircraft feed is unavailable.', true); })
      .finally(() => { aircraftPromise = null; });
    return aircraftPromise;
  }

  const aircraftTrackPanel = document.getElementById('aircraft-track');
  const aircraftTrackTitle = document.getElementById('aircraft-track-title');
  const aircraftTrackId = document.getElementById('aircraft-track-id');
  const aircraftTrackDetail = document.getElementById('aircraft-track-detail');
  const aircraftTrackMore = document.getElementById('aircraft-track-more');
  const aircraftTrailStatus = document.getElementById('aircraft-trail-status');
  const aircraftFitTrail = document.getElementById('aircraft-fit-trail');
  const aircraftTrackRoute = document.getElementById('aircraft-track-route');
  const aircraftFitRoute = document.getElementById('aircraft-fit-route');
  const aircraftTrackStatus = document.getElementById('aircraft-track-status');
  const aircraftFollowButton = document.getElementById('aircraft-follow');
  function aircraftAgeLabel(ageSeconds) {
    if (!Number.isFinite(ageSeconds)) return '';
    if (ageSeconds < 60) return `${Math.max(0, Math.round(ageSeconds))}s ago`;
    const minutes = Math.floor(ageSeconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m ago`;
  }
  function retryAfterMs(response) {
    const seconds = Number(response.headers.get('Retry-After'));
    return Math.min(86400000, Math.max(60000, Number.isFinite(seconds) ? seconds * 1000 : 60000));
  }
  function showTrackedLastPosition(message) {
    if (!trackedAircraft) return;
    const age = trackedAircraft.lastObservedAt ? Math.max(0, Math.round((Date.now() - trackedAircraft.lastObservedAt) / 1000)) : null;
    trackedAircraft.stale = age == null || age > 90;
    aircraftTrackStatus.textContent = age == null ? message : `${message} · ${aircraftAgeLabel(age)}`;
    updateTrackedMarker();
  }
  function updateTrackedMarker() {
    const source = map.getSource('gm-tracked-aircraft');
    if (source) {
      const position = trackedAircraft?.lastPosition;
      source.setData(position ? { type: 'FeatureCollection', features: [feature('tracked', trackedAircraft.hex,
        position.lon, position.lat, { heading: Number(trackedAircraft.item?.track) || 0, stale: trackedAircraft.stale,
          service: aircraftService(trackedAircraft.item || {}), shape: aircraftShape(trackedAircraft.item || {}),
          altitude_m: aircraftAltitudeMeters(trackedAircraft.item) })] } : EMPTY);
    }
    const visibility = trackedAircraft && !cyberFocus ? 'visible' : 'none';
    for (const id of ['gm-tracked-halo', 'gm-tracked-plane']) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visibility);
    }
    updateAircraftRoute();
    updateAircraftTrail();
  }

  function updateAircraftTrail() {
    const source = map.getSource('gm-aircraft-trail');
    if (!source) return;
    if (!trackedAircraft || cyberFocus) { source.setData(EMPTY); return; }
    const points = [...(trackedAircraft.historyPoints || []), ...(trackedAircraft.livePoints || [])]
      .filter(row => Array.isArray(row) && row.length >= 3 && validCoordinate(Number(row[1]), Number(row[0])))
      .sort((a, b) => a[2] - b[2]);
    aircraftFitTrail.hidden = points.length < 2;
    const segments = [];
    let current = [];
    let previous = null;
    for (const point of points) {
      if (previous && (point[2] - previous[2] > 1800 || Math.abs(point[0] - previous[0]) > 180)) {
        if (current.length > 1) segments.push(current);
        current = [];
      }
      if (!previous || Math.abs(point[0] - previous[0]) + Math.abs(point[1] - previous[1]) > 0.0001) {
        current.push([point[0], point[1]]);
      }
      previous = point;
    }
    if (current.length > 1) segments.push(current);
    source.setData({ type: 'FeatureCollection', features: segments.map(coordinates => ({
      type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates }
    })) });
  }

  async function loadAircraftTrail(hex) {
    aircraftTrailController?.abort();
    const controller = new AbortController();
    aircraftTrailController = controller;
    aircraftTrailStatus.textContent = 'Loading flown trail…';
    try {
      const response = await fetch(`/aircraft/track?id=${encodeURIComponent(hex)}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`Aircraft trail: ${response.status}`);
      const data = await response.json();
      if (controller.signal.aborted || trackedAircraft?.hex !== hex) return;
      trackedAircraft.historyPoints = data.status === 'available' ? data.points || [] : [];
      aircraftTrailStatus.textContent = trackedAircraft.historyPoints.length > 1
        ? `Trail · ${trackedAircraft.historyPoints.length} past points`
        : 'No past trail available';
      updateAircraftTrail();
      updateAircraftRoute();
    } catch (error) {
      if (controller.signal.aborted || trackedAircraft?.hex !== hex) return;
      aircraftTrailStatus.textContent = 'Past trail unavailable';
    } finally {
      if (!controller.signal.aborted && trackedAircraft?.hex === hex) {
        trackedAircraft.trailLookupFinished = true;
        maybeAutoFitTrackedPath();
      }
      if (aircraftTrailController === controller) aircraftTrailController = null;
    }
  }

  function updateAircraftRoute() {
    const source = map.getSource('gm-aircraft-route');
    if (!source) return;
    const route = trackedAircraft?.route;
    const position = trackedAircraft?.lastPosition;
    if (!route || !position || cyberFocus) { source.setData(EMPTY); return; }
    const airportPoint = (place, role) => feature('aircraft-route', `${role}:${place.code}`, place.lon, place.lat,
      { role, code: place.code });
    const segment = (from, to, role) => ({ type: 'Feature', properties: { role },
      geometry: { type: 'LineString', coordinates: [[from.lon, from.lat],
        [from.lon + ((((to.lon - from.lon) + 540) % 360) - 180), to.lat]] } });
    const hasObservedTrail = (trackedAircraft.historyPoints || []).length > 1;
    source.setData({ type: 'FeatureCollection', features: [
      ...(hasObservedTrail ? [] : [segment(route.origin, position, 'origin')]),
      segment(position, route.destination, 'destination'),
      airportPoint(route.origin, 'origin'), airportPoint(route.destination, 'destination')
    ] });
  }

  async function loadAircraftRoute(item) {
    const callsign = String(item.flight || '').trim().toUpperCase();
    const hex = trackedAircraft?.hex;
    aircraftRouteController?.abort();
    if (trackedAircraft) {
      trackedAircraft.routeLookupCallsign = callsign;
      trackedAircraft.routeLookupFinished = false;
      trackedAircraft.route = null;
      aircraftFitRoute.hidden = true;
      updateAircraftRoute();
    }
    if (!/^[A-Z]{3}[A-Z0-9]{1,7}$/.test(callsign)) {
      aircraftTrackRoute.textContent = 'Route unavailable';
      if (trackedAircraft) {
        trackedAircraft.routeLookupFinished = true;
        maybeAutoFitTrackedPath();
      }
      return;
    }
    const controller = new AbortController();
    aircraftRouteController = controller;
    aircraftTrackRoute.textContent = 'Checking route…';
    try {
      const params = new URLSearchParams({ callsign, lat: String(item.lat), lon: String(item.lon) });
      const response = await fetch(`/aircraft/route?${params}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`Route lookup: ${response.status}`);
      const data = await response.json();
      if (trackedAircraft?.hex !== hex || controller.signal.aborted) return;
      if (!data.route) { aircraftTrackRoute.textContent = 'No route found'; return; }
      trackedAircraft.route = data.route;
      aircraftFitRoute.hidden = false;
      const origin = data.route.origin, destination = data.route.destination;
      aircraftTrackRoute.replaceChildren(
        textElement('strong', '', `${origin.code || 'Origin'} → ${destination.code || 'Destination'}`),
        textElement('small', '', `${origin.name || 'Departure airport'} → ${destination.name || 'Arrival airport'}`),
        textElement('small', '', 'Estimated route · dashed line to destination')
      );
      updateAircraftRoute();
    } catch (error) {
      if (controller.signal.aborted || trackedAircraft?.hex !== hex) return;
      aircraftTrackRoute.textContent = 'Route unavailable';
    } finally {
      if (!controller.signal.aborted && trackedAircraft?.hex === hex) {
        trackedAircraft.routeLookupFinished = true;
        maybeAutoFitTrackedPath();
      }
      if (aircraftRouteController === controller) aircraftRouteController = null;
    }
  }

  function setTrackingFollow(next) {
    trackingFollow = next;
    aircraftFollowButton.setAttribute('aria-pressed', String(next));
    aircraftFollowButton.textContent = next ? 'Following · pause' : 'Follow aircraft';
    if (next && trackedAircraft?.lastPosition) {
      const { lon, lat } = trackedAircraft.lastPosition;
      map.easeTo({ center: [lon, lat], offset: mobilePanelOffset(aircraftTrackPanel),
        zoom: Math.max(map.getZoom(), 7), duration: 700, essential: true });
    }
  }

  function mobilePanelOffset(panel) {
    if (window.innerWidth > 700 || panel.hidden) return [0, 0];
    const mapBox = map.getCanvas().getBoundingClientRect();
    const panelTop = panel.getBoundingClientRect().top - mapBox.top;
    const targetY = Math.max(55, Math.min(mapBox.height * 0.45, panelTop - 30));
    return [0, targetY - mapBox.height / 2];
  }

  function keepTrackedAircraftClearOfPanel() {
    if (window.innerWidth > 700 || !trackedAircraft?.lastPosition || aircraftTrackPanel.hidden) return;
    const { lon, lat } = trackedAircraft.lastPosition;
    const point = map.project([lon, lat]);
    const mapBox = map.getCanvas().getBoundingClientRect();
    const cardBox = aircraftTrackPanel.getBoundingClientRect();
    if (point.x + mapBox.left < cardBox.left - 24 || point.x + mapBox.left > cardBox.right + 24 ||
        point.y + mapBox.top < cardBox.top - 28) return;
    map.easeTo({ center: [lon, lat], offset: mobilePanelOffset(aircraftTrackPanel),
      duration: 450, essential: true });
  }

  function pauseTrackingForNavigation() {
    if (trackedAircraft && trackingFollow) setTrackingFollow(false);
  }

  function stopTracking() {
    trackedController?.abort();
    aircraftRouteController?.abort();
    aircraftTrailController?.abort();
    aircraftRouteController = null;
    aircraftTrailController = null;
    trackedController = null;
    clearInterval(trackedTimer);
    trackedTimer = null;
    trackedRetryAt = 0;
    trackedAircraft = null;
    aircraftFitRoute.hidden = true;
    aircraftFitTrail.hidden = true;
    aircraftTrackPanel.hidden = true;
    document.body.classList.remove('tracking-aircraft');
    updateTrackedMarker();
  }

  function updateTrackedAircraft(item, initial = false, snapshotAt = null, provider = '') {
    if (!trackedAircraft) return;
    const lat = Number(item.lat), lon = Number(item.lon);
    if (!validCoordinate(lat, lon)) return;
    const previous = trackedAircraft.lastPosition;
    const seen = Number(item.seen_pos ?? item.seen);
    const snapshotAge = Number.isFinite(Number(snapshotAt)) && snapshotAt ? Math.max(0, Date.now() / 1000 - Number(snapshotAt)) : 0;
    const age = Number.isFinite(seen) ? Math.max(0, seen > 1e9 ? Date.now() / 1000 - seen : seen + snapshotAge) : snapshotAge;
    const previousItem = trackedAircraft.item || {};
    item = { ...previousItem, ...item,
      flight: item.flight || previousItem.flight || '',
      r: item.r || previousItem.r || '',
      t: item.t || previousItem.t || '' };
    trackedAircraft.item = item;
    if (provider) trackedAircraft.provider = provider;
    const currentCallsign = String(item.flight || '').trim().toUpperCase();
    if (currentCallsign !== trackedAircraft.routeLookupCallsign) loadAircraftRoute(item);
    trackedAircraft.lastPosition = { lat, lon };
    trackedAircraft.lastObservedAt = Date.now() - (age || 0) * 1000;
    trackedAircraft.stale = age != null && age > 90;
    if (!trackedAircraft.stale && (!previous || Math.abs(lat - previous.lat) + Math.abs(lon - previous.lon) > 0.0001)) {
      trackedAircraft.livePoints.push([lon, lat, Math.round(trackedAircraft.lastObservedAt / 1000)]);
      if (trackedAircraft.livePoints.length > 1500) trackedAircraft.livePoints.shift();
    }
    aircraftTrackTitle.textContent = String(item.flight || item.r || trackedAircraft.title || item.hex).trim();
    aircraftTrackId.textContent = `ICAO ${trackedAircraft.hex.toUpperCase()}${item.r ? ` · ${item.r}` : ''}`;
    aircraftTrackDetail.textContent = [item.alt_baro === 'ground' || item.ground ? 'On ground' : typeof item.alt_baro === 'number' ? `${Math.round(item.alt_baro).toLocaleString()} ft` : '',
      typeof item.gs === 'number' ? `${Math.round(item.gs)} kt` : '', item.t || ''].filter(Boolean).join(' · ');
    const verticalRate = typeof item.baro_rate === 'number' ? `${item.baro_rate > 0 ? '+' : ''}${Math.round(item.baro_rate)} ft/min` : '';
    aircraftTrackMore.textContent = [
      ({ civil: 'Civil', medical: 'Medical', police: 'Law enforcement', fire: 'Fire / rescue', military: 'Military', gov: 'Government' })[aircraftService(item)],
      typeof item.track === 'number' ? `Heading ${Math.round(item.track)}°` : '',
      item.squawk ? `Squawk ${item.squawk}` : '', verticalRate
    ].filter(Boolean).join(' · ');
    aircraftTrackStatus.textContent = initial ? 'Locating…' : trackedAircraft.stale
      ? `Last seen ${aircraftAgeLabel(age)} · stale`
      : `Updated ${aircraftAgeLabel(age)}`;
    updateTrackedMarker();
    if (trackingFollow && !trackedAircraft.stale && (initial || !previous || Math.abs(lat - previous.lat) + Math.abs(lon - previous.lon) > 0.002)) {
      map.easeTo({ center: [lon, lat], offset: mobilePanelOffset(aircraftTrackPanel),
        zoom: Math.max(map.getZoom(), 7), duration: initial ? 950 : 650, essential: true });
    }
  }

  async function refreshTrackedAircraft() {
    if (!trackedAircraft || trackedController || document.hidden) return;
    if (Date.now() < trackedRetryAt) {
      showTrackedLastPosition('Feed busy');
      return;
    }
    const hex = trackedAircraft.hex;
    const controller = new AbortController();
    trackedController = controller;
    try {
      const response = await fetch(`/aircraft?scope=hex&id=${encodeURIComponent(hex)}`, { signal: controller.signal, cache: 'no-store' });
      if (response.status === 429) {
        trackedRetryAt = Date.now() + retryAfterMs(response);
        showTrackedLastPosition('Feed busy');
        return;
      }
      if (!response.ok) throw new Error(`Aircraft lookup: ${response.status}`);
      const data = await response.json();
      if (trackedAircraft?.hex !== hex) return;
      const item = (data.aircraft || []).find(row => String(row.hex || '').toLowerCase() === hex);
      if (item) updateTrackedAircraft(item, false, data.updated_at, data.source);
      else showTrackedLastPosition('Signal lost');
    } catch (error) {
      if (controller.signal.aborted || trackedAircraft?.hex !== hex) return;
      console.warn('Tracked aircraft:', error);
      showTrackedLastPosition('Position refresh unavailable');
    } finally {
      if (trackedController === controller) trackedController = null;
    }
  }

  function startTracking(item, autoShowRoute = false) {
    const hex = String(item.hex || '').toLowerCase();
    if (!/^[0-9a-f]{6}$/.test(hex) || !validCoordinate(Number(item.lat), Number(item.lon))) {
      showStatus('This aircraft has no trackable ICAO position.', true);
      return;
    }
    stopTracking();
    if (cyberFocus) setCyberFocus(false);
    const layer = governmentAircraft(item) ? 'govair' : 'civair';
    if (!enabled[layer]) setLayerEnabled(layer, true, true);
    trackedAircraft = { hex, layer, title: String(item.flight || item.r || hex).trim(),
      item: null, lastPosition: null, lastObservedAt: null, stale: false, route: null,
      routeLookupCallsign: null, routeLookupFinished: false, trailLookupFinished: false,
      autoFitPending: autoShowRoute, historyPoints: [], livePoints: [] };
    aircraftFitRoute.hidden = true;
    aircraftTrackPanel.hidden = false;
    document.body.classList.add('tracking-aircraft');
    setTrackingFollow(true);
    updateTrackedAircraft(item, true, null, aircraftProvider);
    loadAircraftTrail(hex);
    trackedTimer = setInterval(refreshTrackedAircraft, 20000);
    refreshTrackedAircraft();
    if (window.innerWidth < 901) setPanelOpen(false);
  }

  document.getElementById('aircraft-stop').addEventListener('click', stopTracking);
  function fitTrackedTrail() {
    if (!trackedAircraft) return;
    const points = [...trackedAircraft.historyPoints, ...trackedAircraft.livePoints];
    if (points.length < 2) return;
    setTrackingFollow(false);
    const anchor = points[points.length - 1][0];
    const bounds = new maplibregl.LngLatBounds();
    for (const point of points) bounds.extend([anchor + ((((point[0] - anchor) + 540) % 360) - 180), point[1]]);
    const desktop = window.innerWidth > 900;
    if (!desktop) map.once('moveend', keepTrackedAircraftClearOfPanel);
    map.fitBounds(bounds, { padding: desktop
      ? { top: 90, right: 310, bottom: 75, left: document.body.classList.contains('panel-collapsed') ? 45 : 310 }
      : { top: 75, right: 45, bottom: window.innerWidth < 701 ? 180 : 75, left: 45 },
      maxZoom: 12, duration: 900, essential: true });
  }
  function fitTrackedRoute() {
    if (!trackedAircraft?.route || !trackedAircraft.lastPosition) return;
    setTrackingFollow(false);
    const places = [trackedAircraft.route.origin, trackedAircraft.lastPosition, trackedAircraft.route.destination];
    const anchor = places[1].lon;
    const lons = places.map(point => anchor + ((((point.lon - anchor) + 540) % 360) - 180));
    const lats = places.map(point => point.lat);
    const canvas = map.getCanvas();
    const panelWidth = window.innerWidth > 900 && !document.body.classList.contains('panel-collapsed') ? 288 : 0;
    const usableWidth = Math.max(180, canvas.clientWidth - panelWidth - 90);
    const mercatorY = lat => Math.log(Math.tan(Math.PI / 4 + Math.max(-85, Math.min(85, lat)) * Math.PI / 360)) / (2 * Math.PI);
    const lonSpan = Math.max(0.5, Math.max(...lons) - Math.min(...lons));
    const latSpan = Math.max(0.005, Math.max(...lats.map(mercatorY)) - Math.min(...lats.map(mercatorY)));
    const zoom = Math.max(1.5, Math.min(8,
      Math.log2(usableWidth * 360 / (512 * lonSpan)),
      Math.log2(Math.max(180, canvas.clientHeight - 130) / (512 * latSpan))) - 0.45);
    const degreesPerPixel = 360 / (512 * 2 ** zoom);
    const centerLon = (Math.min(...lons) + Math.max(...lons)) / 2 - panelWidth / 2 * degreesPerPixel * 0.35;
    const centerLat = (Math.min(...lats) + Math.max(...lats)) / 2;
    if (window.innerWidth <= 700) map.once('moveend', keepTrackedAircraftClearOfPanel);
    map.easeTo({ center: [centerLon, centerLat], zoom, bearing: 0, pitch: 0,
      duration: 950, essential: true });
  }
  function maybeAutoFitTrackedPath() {
    if (!trackedAircraft?.autoFitPending || !trackedAircraft.routeLookupFinished) return;
    if (trackedAircraft.route) {
      trackedAircraft.autoFitPending = false;
      fitTrackedRoute();
    } else if (trackedAircraft.trailLookupFinished) {
      trackedAircraft.autoFitPending = false;
      if (trackedAircraft.historyPoints.length > 1) fitTrackedTrail();
    }
  }
  aircraftFitTrail.addEventListener('click', fitTrackedTrail);
  aircraftFitRoute.addEventListener('click', fitTrackedRoute);
  aircraftFollowButton.addEventListener('click', () => setTrackingFollow(!trackingFollow));
  map.on('dragstart', () => {
    if (trackedAircraft) { trackedAircraft.autoFitPending = false; setTrackingFollow(false); }
  });
  map.on('zoomstart', event => {
    if (trackedAircraft && event.originalEvent) {
      trackedAircraft.autoFitPending = false;
      setTrackingFollow(false);
    }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshTrackedAircraft(); });

  async function loadVessels() {
    if (!styleReady || !enabled.vessels || zoomHint('vessels') || document.hidden || vesselPromise) return;
    const bounds = currentBounds();
    vesselPromise = (async () => {
      const bbox = [bounds.south, bounds.west, bounds.north, bounds.east].join(',');
      const response = await fetch(`/vessels?${new URLSearchParams({ bbox, client: vesselClientId })}`);
      if (!response.ok) throw new Error(`Vessel feed: ${response.status}`);
      const data = await response.json();
      if (!enabled.vessels) return;
      const features = [];
      const records = new Map();
      for (const vessel of data.vessels || []) {
        const lat = Number(vessel.lat), lon = Number(vessel.lon);
        if (!validCoordinate(lat, lon) || !inBounds(lat, lon, bounds)) continue;
        const ref = String(vessel.mmsi);
        const age = Math.max(0, Math.floor((Date.now() / 1000 - Number(vessel.updated_at)) / 60));
        records.set(ref, {
          title: vessel.name || `MMSI ${ref}`,
          detail: [`MMSI ${ref}`, typeof vessel.speed === 'number' && vessel.speed < 102.2 ? `${vessel.speed.toFixed(1)} kt` : '', `Last report ${age} min ago`].filter(Boolean).join(' · '),
          source: 'AISStream · observed vessel position'
        });
        features.push(feature('vessels', ref, lon, lat, { heading: Number(vessel.heading) || 0 }));
      }
      setPoints('vessels', features, records);
      const count = document.querySelector('[data-count="vessels"]');
      if (data.status === 'needs_key') {
        if (count) count.textContent = 'KEY';
        if (!vesselKeyNoticeShown) showStatus('Add AISSTREAM_API_KEY to .env to enable live vessels.', true, 7000);
        vesselKeyNoticeShown = true;
      } else if (data.status === 'capacity') {
        if (count) count.textContent = 'BUSY';
        if (!vesselCapacityNoticeShown) showStatus('Vessel coverage is at capacity. Try this area shortly.', true, 6000);
        vesselCapacityNoticeShown = true;
      } else if (data.status !== 'live' && count) count.textContent = '…';
      else {
        vesselKeyNoticeShown = false;
        vesselCapacityNoticeShown = false;
      }
    })().catch(error => { console.warn(error); showStatus('Vessel feed is unavailable. Zoom closer or retry.', true); })
      .finally(() => { vesselPromise = null; });
    return vesselPromise;
  }

  async function loadWebcams() {
    if (!styleReady || !enabled.webcams) return;
    try {
      if (!webcamCatalog) {
        const response = await fetch('/global-cameras.json');
        if (!response.ok) throw new Error(`Webcam catalog: ${response.status}`);
        webcamCatalog = (await response.json()).cameras || [];
      }
      if (!enabled.webcams) return;
      const features = [];
      const records = new Map();
      for (const camera of webcamCatalog) {
        const lat = Number(camera.lat), lon = Number(camera.lon);
        if (!validCoordinate(lat, lon)) continue;
        const ref = String(camera.id);
        records.set(ref, { id: ref, title: camera.name, detail: [camera.city, camera.country].filter(Boolean).join(', '),
          source: camera.source || 'Public webcam', url: camera.url, streamUrl: camera.stream_url });
        features.push(feature('webcams', ref, lon, lat));
      }
      setPoints('webcams', features, records);
    } catch (error) { console.warn(error); showStatus('Global webcam catalog is unavailable.', true); }
  }

  function convertStatic(type, data) {
    const features = [];
    const records = new Map();
    if (type === 'emergency') {
      for (const item of data.features || []) {
        const [lon, lat] = item.geometry?.coordinates || [];
        if (!validCoordinate(lat, lon)) continue;
        const p = item.properties || {};
        const ref = String(p.key || `${lat}:${lon}`);
        const category = String(p.icon || 'warning').replace('.png', '');
        const known = ['fire', 'medical', 'police', 'traffic', 'patrol', 'warning'].includes(category) ? category : 'warning';
        features.push(feature(type, ref, lon, lat, { category: known }));
        records.set(ref, { title: p.description || 'Emergency call', detail: [p.location, p.time].filter(Boolean).join(' · '), source: p.source || 'Emergency feed' });
      }
    } else if (type === 'sensors') {
      for (const item of data.features || []) {
        const p = item.attributes || {};
        const lat = Number(p.LATITUDE), lon = Number(p.LNGITUDE);
        if (!validCoordinate(lat, lon)) continue;
        const ref = String(p.IDSTR || `${lat}:${lon}`);
        const weather = p.SENSOR_TYPE === 'road_weather';
        const detail = weather
          ? [p.ROAD_STATE, p.ROAD_TEMP_F == null ? '' : `Road ${p.ROAD_TEMP_F}°F`, p.AIR_TEMP_F == null ? '' : `Air ${p.AIR_TEMP_F}°F`, p.OBS_TIME_LOCAL].filter(Boolean).join(' · ')
          : [p.CURAVSPD == null ? '' : `${Math.round(p.CURAVSPD)} mph`, p.MAXSPEEDR == null ? '' : `${p.MAXSPEEDR} mph limit`].filter(Boolean).join(' · ');
        features.push(feature(type, ref, lon, lat));
        records.set(ref, { title: p.LOCALNAM || 'Road sensor', detail, source: weather ? 'Road weather sensor' : 'Traffic speed sensor' });
      }
    } else if (type === 'temperature') {
      for (const item of data.features || []) {
        const [lon, lat] = item.geometry?.coordinates || [];
        if (!validCoordinate(lat, lon)) continue;
        const p = item.properties || {};
        const ref = String(p.station || `${lat}:${lon}`);
        features.push(feature(type, ref, lon, lat));
        records.set(ref, { title: p.name || ref, detail: [p.tmpf == null ? '' : `${Math.round(p.tmpf)}°F`, p.relh == null ? '' : `${Math.round(p.relh)}% humidity`, p.sknt == null ? '' : `${Math.round(p.sknt)} kt wind`, p.utc_valid].filter(Boolean).join(' · '), source: 'Weather station' });
      }
    } else if (type === 'lpr') {
      for (const item of data.elements || []) {
        const lat = Number(item.lat ?? item.center?.lat), lon = Number(item.lon ?? item.center?.lon);
        if (!validCoordinate(lat, lon)) continue;
        const ref = String(item.id || `${lat}:${lon}`);
        const tags = item.tags || {};
        features.push(feature(type, ref, lon, lat));
        records.set(ref, { title: item.title || 'Mapped plate reader', detail: item.detail || [tags.operator || tags.manufacturer, tags.surveillance_zone || tags['surveillance:zone'], tags.direction ? `Direction ${tags.direction}` : ''].filter(Boolean).join(' · '), source: item.source || 'DeFlock / OpenStreetMap · mapped location, status unverified', sourceUrl: item.source_url || (/^\d+$/.test(ref) ? `https://www.openstreetmap.org/node/${ref}` : 'https://deflock.me/') });
      }
    }
    return { features, records };
  }

  async function loadStatic(type) {
    if (!styleReady || !enabled[type] || zoomHint(type) || document.hidden) return;
    requests.get(type)?.abort();
    const controller = new AbortController();
    requests.set(type, controller);
    requestStartedAt.set(type, Date.now());
    const path = { emergency: '/emergency', sensors: '/sensors', temperature: '/temperature-stations', lpr: '/lpr' }[type];
    let paths = [path];
    if (type === 'lpr') {
      const b = currentBounds();
      const west = ((b.west + 180) % 360 + 360) % 360 - 180;
      const east = west + Math.min(360, b.east - b.west);
      const south = Math.max(-90, b.south), north = Math.min(90, b.north);
      const boxes = east <= 180 ? [[west, south, east, north]]
        : [[west, south, 180, north], [-180, south, east - 360, north]];
      paths = boxes.map(box => `${path}?${new URLSearchParams({ bbox: box.join(','), limit: '10000' })}`);
    }
    try {
      const internationalSensorsVisible = type === 'sensors' && [
        { bounds: { minLon: -6, maxLon: 10, minLat: 41, maxLat: 52 } },
        { bounds: { minLon: 3, maxLon: 7.4, minLat: 50.6, maxLat: 53.8 } },
        { bounds: { minLon: 4, maxLon: 32, minLat: 57, maxLat: 72 } },
        { bounds: { minLon: -25, maxLon: -13, minLat: 63, maxLat: 67.5 } },
        { bounds: { minLon: 14, maxLon: 24.3, minLat: 48.8, maxLat: 55.2 } }
      ].some(region => regionVisible(region, currentBounds()));
      const northAmericaSensorsVisible = type !== 'sensors' || regionVisible({ bounds: {
        minLon: -170, maxLon: -50, minLat: 15, maxLat: 72 } }, currentBounds());
      const staticPromise = northAmericaSensorsVisible ? Promise.all(paths.map(async url => {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`${POINT[type].label}: ${response.status}`);
        return response.json();
      })) : Promise.resolve([{}]);
      const internationalPromise = internationalSensorsVisible
        ? fetchInternationalRoad('sensors', currentBounds(), controller.signal) : Promise.resolve(null);
      const [staticResult, internationalResult] = await Promise.allSettled([staticPromise, internationalPromise]);
      if (staticResult.status === 'rejected' && (internationalResult.status === 'rejected' || !internationalResult.value)) {
        throw staticResult.reason;
      }
      const payloads = staticResult.status === 'fulfilled' ? staticResult.value : [];
      const data = type === 'lpr' ? { elements: payloads.flatMap(payload => payload.elements || []) } : payloads[0];
      if (controller.signal.aborted || !enabled[type]) return;
      const { features, records } = convertStatic(type, data || {});
      if (internationalResult.status === 'fulfilled' && internationalResult.value) {
        for (const item of internationalResult.value.features || []) {
          const [lon, lat] = item.geometry?.coordinates || [];
          const p = item.properties || {};
          if (!validCoordinate(lat, lon) || !p.key) continue;
          const ref = String(p.key);
          features.push(feature(type, ref, lon, lat));
          records.set(ref, { title: p.title || 'Road sensor',
            detail: [p.detail, p.updated_at ? `Updated ${p.updated_at}` : ''].filter(Boolean).join(' · '),
            source: p.source || 'Public road authority', sourceUrl: p.source_url,
            sensorId: p.sensor_id || '' });
        }
      } else if (internationalResult.status === 'rejected' && !controller.signal.aborted) {
        console.warn('International road sensors:', internationalResult.reason);
      }
      setPoints(type, features, records);
    } catch (error) {
      if (controller.signal.aborted) return;
      console.warn(error);
      showStatus(`${POINT[type].label} feed is unavailable.`, true);
    }
  }

  async function loadPower() {
    if (!styleReady || !enabled.power || zoomHint('power') || document.hidden) return;
    requests.get('power')?.abort();
    const controller = new AbortController();
    requests.set('power', controller);
    try {
      const bounds = currentBounds();
      const urls = [];
      if (regionVisible({ bounds: { minLon: -170, maxLon: -50, minLat: 15, maxLat: 73 } }, bounds)) urls.push('/power-outages');
      if (regionVisible({ bounds: { minLon: -11, maxLon: 8, minLat: 49, maxLat: 60 } }, bounds)) urls.push('/international-power');
      const results = await Promise.allSettled(urls.map(async url => {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`${url}: ${response.status}`);
        return response.json();
      }));
      if (controller.signal.aborted || !enabled.power) return;
      const feeds = results.filter(result => result.status === 'fulfilled').map(result => result.value);
      if (results.some(result => result.status === 'rejected')) console.warn('Some power feeds are unavailable:', results.filter(result => result.status === 'rejected').map(result => result.reason));
      if (urls.length && !feeds.length) throw new Error('All power feeds are unavailable');
      const records = new Map();
      const features = [];
      for (const item of feeds.flatMap(data => data.features || [])) {
        if (!item.geometry) continue;
        const p = item.properties || {};
        const ref = String(p.key || features.length);
        const impact = String(p.customers_affected || '').trim();
        const count = Number(impact);
        const impactLabel = /^<\s*\d+$/.test(impact) ? `${impact} affected`
          : Number.isFinite(count) && count > 0 ? `${count.toLocaleString()} affected` : 'Impact unreported';
        records.set(ref, {
          title: p.provider || 'Power outage',
          detail: [p.area_name, impactLabel, p.outages ? `${p.outages} outages` : '', p.status, p.reason, p.etr ? `ETR ${p.etr}` : '', p.source_updated ? `Source updated ${p.source_updated}` : ''].filter(Boolean).join(' · '),
          source: p.source_label || 'Utility outage feed', sourceUrl: p.source_url
        });
        let position = null;
        if (item.geometry.type === 'Point') position = item.geometry.coordinates;
        else if (item.geometry.type === 'MultiPoint') position = item.geometry.coordinates[0];
        const properties = position
          ? withGlobeVector({ ref, color: POINT.power.color }, Number(position[0]), Number(position[1]))
          : { ref, color: POINT.power.color };
        features.push({ type: 'Feature', geometry: item.geometry, properties });
      }
      refs.set('power', records);
      map.getSource('gm-power')?.setData({ type: 'FeatureCollection', features });
      setCount('power', features.length);
      fetchedAt.set('power', Date.now());
      updateToggle('power');
    } catch (error) {
      if (controller.signal.aborted) return;
      console.warn(error);
      showStatus('Power outage feed is unavailable.', true);
    }
  }

  function observedLabel(value) {
    if (!value) return '';
    const date = typeof value === 'number' ? new Date(value) : new Date(/(Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString(undefined, { timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'short' }) + ' UTC';
  }

  function hideCycloneGuidance() {
    cycloneGuidanceController?.abort();
    cycloneGuidanceController = null;
    selectedCycloneId = null;
    cycloneGuidanceData = EMPTY;
    map.getSource('gm-cyclone-guidance')?.setData(EMPTY);
    cycloneGuidancePanel.hidden = true;
    updateToggle('cyclones');
  }

  function cycloneGuidanceFeatures(tracks) {
    const features = [];
    for (const track of tracks) {
      let segment = [];
      for (const raw of track.points || []) {
        const lon = Number(raw?.[0]), lat = Number(raw?.[1]);
        if (!validCoordinate(lat, lon)) continue;
        if (segment.length && Math.abs(lon - segment.at(-1)[0]) > 180) {
          if (segment.length > 1) features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: segment },
            properties: { code: track.code, model: track.model, kind: track.kind, color: track.color } });
          segment = [];
        }
        segment.push([lon, lat]);
      }
      if (segment.length > 1) features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: segment },
        properties: { code: track.code, model: track.model, kind: track.kind, color: track.color } });
    }
    return { type: 'FeatureCollection', features };
  }

  function setCycloneGuidanceSource(url, available) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== 'https:' || !['ftp.nhc.noaa.gov', 'www.nhc.noaa.gov', 'www.metoc.navy.mil', 'hurricanes.ral.ucar.edu'].includes(parsed.hostname)) throw new Error('Unknown source');
      cycloneGuidanceSource.href = parsed.href;
      cycloneGuidanceSource.textContent = available
        ? parsed.hostname === 'hurricanes.ral.ucar.edu' ? 'View UCAR model guidance ↗' : 'View NHC model guidance ↗'
        : 'View agency storm report ↗';
      cycloneGuidanceSource.hidden = false;
    } catch { cycloneGuidanceSource.hidden = true; }
  }

  function frameCycloneGuidance(tracks, stormCoordinates) {
    const coreTracks = tracks.filter(track => track.kind !== 'ensemble');
    const positions = (coreTracks.length ? coreTracks : tracks).flatMap(track => track.points || []).concat([stormCoordinates]);
    const west = Math.min(...positions.map(point => point[0]));
    const east = Math.max(...positions.map(point => point[0]));
    const south = Math.min(...positions.map(point => point[1]));
    const north = Math.max(...positions.map(point => point[1]));
    const selectedId = selectedCycloneId;
    if (window.innerWidth <= 700) map.once('moveend', () => {
      if (selectedCycloneId !== selectedId || cycloneGuidancePanel.hidden) return;
      const point = map.project(stormCoordinates);
      const canvas = map.getCanvas();
      const panelTop = cycloneGuidancePanel.getBoundingClientRect().top - canvas.getBoundingClientRect().top;
      if (point.y > panelTop - 38) {
        map.easeTo({ center: stormCoordinates, offset: mobilePanelOffset(cycloneGuidancePanel),
          duration: 450, essential: true });
      }
    });
    if (east - west > 180) {
      map.easeTo({ center: stormCoordinates, zoom: 4, duration: 800, essential: true });
      return;
    }
    const wide = map.getCanvas().clientWidth > 700;
    const panelBottomPadding = Math.ceil(map.getCanvas().getBoundingClientRect().bottom -
      cycloneGuidancePanel.getBoundingClientRect().top + 24);
    map.fitBounds([[west, south], [east, north]], {
      padding: wide ? { top: 110, right: 365, bottom: 70, left: 320 }
        : { top: 75, right: 24, bottom: Math.max(250, panelBottomPadding), left: 24 },
      maxZoom: 5.8, duration: 800, essential: true,
    });
  }

  async function showCycloneGuidance(ref, meta, coordinates, popup) {
    hideCycloneGuidance();
    selectedCycloneId = ref;
    cycloneGuidancePanel.hidden = false;
    cycloneGuidanceTitle.textContent = meta.title || 'Tropical cyclone';
    cycloneGuidanceSummary.textContent = 'Loading current model guidance…';
    cycloneGuidanceLegend.replaceChildren();
    setCycloneGuidanceSource(meta.sourceUrl, false);
    updateToggle('cyclones');
    const controller = new AbortController();
    cycloneGuidanceController = controller;
    try {
      const response = await fetch(`/cyclone-guidance?id=${encodeURIComponent(ref)}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`Guidance: ${response.status}`);
      const guidance = await response.json();
      if (controller.signal.aborted || selectedCycloneId !== ref || !enabled.cyclones) return;
      if (guidance.status !== 'available' || !guidance.tracks?.length) {
        cycloneGuidanceSummary.textContent = guidance.message || 'Current model tracks are unavailable for this storm.';
        setCycloneGuidanceSource(guidance.sourceUrl || meta.sourceUrl, false);
        return;
      }
      cycloneGuidanceData = cycloneGuidanceFeatures(guidance.tracks);
      map.getSource('gm-cyclone-guidance')?.setData(cycloneGuidanceData);
      updateToggle('cyclones');
      const models = guidance.tracks.filter(track => track.kind === 'model').length;
      const ensembleMembers = guidance.tracks.filter(track => track.kind === 'ensemble').length;
      const issued = new Date(guidance.cycle);
      cycloneGuidanceSummary.textContent = `${models} model tracks${ensembleMembers ? ` · ${ensembleMembers} GEFS members` : ''} · initialized ${Number.isNaN(issued.getTime()) ? guidance.cycle : issued.toLocaleString([], { timeZone: 'UTC', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) + ' UTC'}`;
      for (const track of guidance.tracks) {
        if (track.kind === 'ensemble') continue;
        const row = document.createElement('div');
        row.className = 'cyclone-guidance-row';
        const line = document.createElement('i');
        line.style.borderColor = track.color;
        if (track.kind === 'consensus') line.classList.add('consensus');
        const label = document.createElement('span');
        label.textContent = track.model;
        row.append(line, label);
        cycloneGuidanceLegend.append(row);
      }
      if (ensembleMembers) {
        const row = document.createElement('div');
        row.className = 'cyclone-guidance-row';
        const line = document.createElement('i');
        line.className = 'ensemble';
        line.style.borderColor = '#8faec1';
        row.append(line, textElement('span', '', `GEFS ensemble · ${ensembleMembers} members`));
        cycloneGuidanceLegend.append(row);
      }
      setCycloneGuidanceSource(guidance.sourceUrl, true);
      if (popup.isOpen()) popup.remove();
      frameCycloneGuidance(guidance.tracks, coordinates);
    } catch (error) {
      if (controller.signal.aborted || selectedCycloneId !== ref) return;
      console.warn('Cyclone guidance:', error);
      cycloneGuidanceSummary.textContent = 'Model guidance could not be loaded. The observed storm track remains available.';
    }
  }
  document.getElementById('cyclone-guidance-close').addEventListener('click', hideCycloneGuidance);

  let irelandCountiesPromise;
  function loadIrelandCounties() {
    irelandCountiesPromise ||= fetch('/ireland-counties-2019.json').then(response => {
      if (!response.ok) throw new Error(`Ireland county boundaries: ${response.status}`);
      return response.json();
    }).then(data => data.counties);
    return irelandCountiesPromise;
  }

  async function loadHazards(type) {
    if (!styleReady || !enabled[type] || document.hidden) return;
    requests.get(type)?.abort();
    const controller = new AbortController();
    requests.set(type, controller);
    try {
      const response = await fetch(`/hazards?layer=${type}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`${type}: ${response.status}`);
      const data = await response.json();
      if (controller.signal.aborted || !enabled[type]) return;
      let irelandCounties = null;
      if (type === 'world_alerts' && data.items?.some(item => item.country === 'Ireland' && item.regions?.length)) {
        try { irelandCounties = await loadIrelandCounties(); }
        catch (error) { irelandCountiesPromise = null; console.warn(error); }
        if (controller.signal.aborted || !enabled[type]) return;
      }
      const features = [];
      const records = new Map();
      const tracks = [];
      const areas = [];
      for (const item of data.items || []) {
        if ((type === 'nws_alerts' || type === 'world_alerts') && item.ends && Date.parse(item.ends) <= Date.now()) continue;
        if (item.country === 'Ireland' && irelandCounties && Array.isArray(item.regions) &&
            item.regions.length && item.regions.every(code => irelandCounties[code]?.geometry)) {
          const polygons = item.regions.flatMap(code => {
            const shape = irelandCounties[code].geometry;
            return shape.type === 'Polygon' ? [shape.coordinates] :
              shape.type === 'MultiPolygon' ? shape.coordinates : [];
          });
          if (polygons.length) {
            item.geometry = { type: 'MultiPolygon', coordinates: polygons };
            item.locationKind = 'polygon';
          }
        }
        const lon = Number(item.lon), lat = Number(item.lat);
        if (!validCoordinate(lat, lon)) continue;
        const ref = String(item.id || features.length);
        let detail = '';
        if (type === 'cyclones') {
          detail = [Number.isFinite(Number(item.windKt)) && item.windKt != null ? `${Number(item.windKt).toFixed(0)} kt wind` : '',
            observedLabel(item.observed)].filter(Boolean).join(' · ');
          const segments = [[]];
          for (const position of item.track || []) {
            if (!validCoordinate(Number(position?.[1]), Number(position?.[0]))) continue;
            const point = [Number(position[0]), Number(position[1])];
            const segment = segments.at(-1);
            if (segment.length && Math.abs(segment.at(-1)[0] - point[0]) > 180) segments.push([]);
            segments.at(-1).push(point);
          }
          for (const segment of segments) if (segment.length > 1) tracks.push({
            type: 'Feature', geometry: { type: 'LineString', coordinates: segment }, properties: { ref }
          });
        } else if (type === 'earthquakes') {
          detail = [`Magnitude ${Number(item.magnitude).toFixed(1)}`,
            item.depthKm != null ? `${Number(item.depthKm).toFixed(1)} km deep` : '',
            observedLabel(item.observed)].filter(Boolean).join(' · ');
        } else if (type === 'nws_alerts' || type === 'world_alerts') {
          detail = [item.severity && item.severity !== 'Unknown' ? `${item.severity} severity` : '',
            item.country || '',
            item.area ? String(item.area).slice(0, 230) : item.zoneName || '',
            item.ends ? `Ends ${observedLabel(item.ends)}` : '',
            item.locationKind === 'polygon' ? 'Mapped alert boundary' : 'Representative zone point'].filter(Boolean).join(' · ');
          if (item.geometry?.type === 'Polygon' || item.geometry?.type === 'MultiPolygon') areas.push({
            type: 'Feature', geometry: item.geometry, properties: { ref, severity: item.severity || 'Unknown' }
          });
        } else if (type === 'fires') {
          detail = [item.country, item.alert ? `${item.alert} GDACS alert` : '',
            item.areaHa != null ? `${Math.round(Number(item.areaHa)).toLocaleString()} ha reported` : '',
            observedLabel(item.observed)].filter(Boolean).join(' · ');
        } else if (type === 'gdelt_events') {
          detail = [item.category || 'Media-coded conflict or protest event', item.geoPrecision || 'Approximate reported location',
            observedLabel(item.observed)].filter(Boolean).join(' · ');
        } else {
          detail = [item.country, item.alert ? `${item.alert} GDACS alert` : '',
            type === 'volcanoes' && !item.active ? 'Recent report' : '',
            observedLabel(item.observed)].filter(Boolean).join(' · ');
        }
      records.set(ref, { title: item.title || POINT[type].label, detail, source: item.source || data.source,
          sourceUrl: item.sourceUrl, advice: type === 'nws_alerts' || type === 'world_alerts' ? item.advice : '' });
        features.push(feature(type, ref, lon, lat, type === 'earthquakes' ? { magnitude: Number(item.magnitude) } : {}));
      }
      if (type === 'cyclones') {
        map.getSource('gm-cyclones-track')?.setData({ type: 'FeatureCollection', features: tracks });
        if (selectedCycloneId && !records.has(selectedCycloneId)) hideCycloneGuidance();
      }
      if (type === 'nws_alerts') map.getSource('gm-nws-alerts-areas')?.setData({ type: 'FeatureCollection', features: areas });
      if (type === 'world_alerts') map.getSource('gm-world-alerts-areas')?.setData({ type: 'FeatureCollection', features: areas });
      setPoints(type, features, records);
      if (type === 'world_alerts' && data.unavailable?.length) {
        showStatus(`Weather alerts are temporarily unavailable for ${data.unavailable.join(' and ')}.`, true);
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      console.warn(error);
      showStatus(`${POINT[type].label} feed is unavailable.`, true);
    }
  }

  async function loadInternationalEmergency() {
    if (!styleReady || !enabled.international || document.hidden) return;
    requests.get('international')?.abort();
    const controller = new AbortController();
    requests.set('international', controller);
    try {
      const response = await fetch('/international-emergency', { signal: controller.signal });
      if (!response.ok) throw new Error(`International emergency reports: ${response.status}`);
      const data = await response.json();
      if (controller.signal.aborted || !enabled.international) return;
      const features = [];
      const records = new Map();
      for (const item of data.items || []) {
        const lat = Number(item.lat), lon = Number(item.lon);
        if (!validCoordinate(lat, lon)) continue;
        const ref = String(item.id || features.length);
        const category = ['fire', 'medical', 'police', 'traffic', 'patrol', 'warning'].includes(item.category) ? item.category : 'warning';
        features.push(feature('international', ref, lon, lat, { category }));
        records.set(ref, {
          title: item.title || 'Emergency report',
          detail: [item.detail, observedLabel(item.observed)].filter(Boolean).join(' · '),
          source: item.source || 'Public emergency feed', sourceUrl: item.sourceUrl
        });
      }
      setPoints('international', features, records);
      if (data.sourceErrors?.length) {
        console.warn('Some international emergency feeds are unavailable:', data.sourceErrors);
        const names = { nsw_rfs: 'NSW', victoria: 'Victoria', queensland: 'Queensland', nz_alerts: 'New Zealand', england_floods: 'England floods', burgenland_fire: 'Burgenland', iceland_imo: 'Iceland', portugal_anepc: 'Portugal', sweden_vma: 'Sweden warnings', sweden_police: 'Sweden police reports', nl_p2000: 'Netherlands P2000' };
        const failed = data.sourceErrors.map(error => names[String(error).split(':')[0]]).filter(Boolean);
        showStatus(`${failed.join(', ') || 'Some regional feeds'} temporarily unavailable; other reports loaded.`, true, 6000);
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      console.warn(error);
      showStatus('International emergency reports are unavailable.', true);
    }
  }

  async function loadPorts() {
    if (!styleReady || !enabled.ports || zoomHint('ports') || document.hidden) return;
    try {
      if (!portCatalog) {
        const response = await fetch('/ports.json');
        if (!response.ok) throw new Error(`Ports: ${response.status}`);
        portCatalog = await response.json();
        portOverviewCount = portCatalog.filter(port => ['Large', 'Medium'].includes(port.size)).length;
      }
      if (!enabled.ports) return;
      const features = [];
      const records = new Map();
      for (const port of portCatalog) {
        const lat = Number(port.lat), lon = Number(port.lon);
        if (!validCoordinate(lat, lon)) continue;
        const ref = String(port.id);
        const details = [port.c, port.size ? `${port.size} harbor` : '', port.water,
          port.locode ? `UN/LOCODE ${port.locode}` : '',
          port.container === 'Yes' ? 'Container facilities' : ''].filter(Boolean).join(' · ');
        features.push(feature('ports', ref, lon, lat, { size: port.size || '' }));
        records.set(ref, { title: port.n, detail: details, source: 'NGA World Port Index · 2024 snapshot' });
      }
      setPoints('ports', features, records);
    } catch (error) {
      console.warn(error);
      showStatus('Port catalog is unavailable.', true);
    }
  }

  async function openSeaConditions(coordinates) {
    const [lon, lat] = coordinates.map(Number);
    if (!validCoordinate(lat, lon)) return;
    seaPopup?.remove();
    const root = document.createElement('div');
    root.append(textElement('span', 'popup-kicker', 'Sea conditions · model forecast'),
      textElement('strong', 'popup-title', `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? 'E' : 'W'}`));
    const detail = textElement('span', 'popup-detail', 'Loading latest forecast…');
    root.append(detail);
    seaPopup = new maplibregl.Popup({ closeButton: true, maxWidth: '315px', offset: 12 })
      .setLngLat([lon, lat]).setDOMContent(root).addTo(map);
    const popup = seaPopup;
    const params = new URLSearchParams({
      latitude: String(lat), longitude: String(lon),
      current: 'wave_height,wave_direction,wave_period,sea_surface_temperature,ocean_current_velocity,ocean_current_direction',
      cell_selection: 'sea', timezone: 'UTC'
    });
    try {
      const response = await fetch(`https://marine-api.open-meteo.com/v1/marine?${params}`);
      if (!response.ok) throw new Error(`Marine forecast: ${response.status}`);
      const data = await response.json();
      if (!popup.isOpen()) return;
      const gridKm = 111.2 * Math.hypot(Number(data.latitude) - lat,
        (Number(data.longitude) - lon) * Math.cos(lat * Math.PI / 180));
      if (!data.current || !Number.isFinite(gridKm) || gridKm > 50) {
        detail.textContent = 'No nearby sea forecast grid cell at this location.';
        return;
      }
      const c = data.current;
      detail.textContent = [
        c.wave_height != null ? `Wave height ${Number(c.wave_height).toFixed(1)} m` : '',
        c.wave_direction != null ? `Wave direction ${Math.round(c.wave_direction)}°` : '',
        c.wave_period != null ? `Period ${Number(c.wave_period).toFixed(1)} s` : '',
        c.sea_surface_temperature != null ? `Sea surface ${Number(c.sea_surface_temperature).toFixed(1)}°C` : '',
        c.ocean_current_velocity != null ? `Current ${Number(c.ocean_current_velocity).toFixed(1)} km/h${c.ocean_current_direction != null ? ` toward ${Math.round(c.ocean_current_direction)}°` : ''}` : '',
        observedLabel(c.time)
      ].filter(Boolean).join(' · ') || 'No forecast values available.';
      root.append(textElement('span', 'popup-detail', 'Model forecast at nearest sea grid cell. Not for navigation.'));
      appendLink(root, 'Open-Meteo Marine forecast ↗', 'https://open-meteo.com/en/docs/marine-weather-api');
    } catch (error) {
      console.warn(error);
      if (popup.isOpen()) detail.textContent = 'Sea forecast is unavailable. Try again later.';
    }
  }

  async function loadScans() {
    if (!styleReady || !enabled.scans || document.hidden) return;
    requests.get('scans')?.abort();
    const controller = new AbortController();
    requests.set('scans', controller);
    try {
      const response = await fetch('/cyber?feed=scans', { signal: controller.signal });
      if (!response.ok) throw new Error(`Scan feed: ${response.status}`);
      const data = await response.json();
      if (controller.signal.aborted || !enabled.scans) return;
      const records = new Map();
      const features = [];
      for (const item of data.items || []) {
        const lon = Number(item.lon), lat = Number(item.lat);
        if (!validCoordinate(lat, lon)) continue;
        const ref = String(item.id);
        records.set(ref, {
          title: `Reported source IP ${item.ip}`,
          detail: [`#${item.rank} on SANS top source-IP list`, item.hostname,
            Number(item.reports) > 0 ? `${Number(item.reports).toLocaleString()} SANS reports · ${Number(item.targets || 0).toLocaleString()} reported target IPs` : '',
            [item.city, item.country].filter(Boolean).join(', '),
            'Approximate IP geolocation · not a confirmed compromise or attack path'].filter(Boolean).join(' · '),
          source: 'SANS Internet Storm Center · RIPEstat location', sourceUrl: item.sourceUrl
        });
        features.push(feature('scans', ref, lon, lat, { rank: Number(item.rank) || 100 }));
      }
      setPoints('scans', features, records);
      cyberMapCount.textContent = `${features.length} mapped of ${Number(data.listed) || data.items?.length || features.length} listed IPs`;
      cyberMapUpdated.textContent = data.observed ? `Feed retrieved ${observedLabel(data.observed)}` : '';
    } catch (error) {
      if (controller.signal.aborted) return;
      console.warn(error);
      cyberMapCount.textContent = 'Source list unavailable';
      cyberMapUpdated.textContent = '';
      showStatus('Scan-source feed is unavailable.', true);
    }
  }

  let scanBriefingLoadedAt = 0;
  async function loadScanBriefing() {
    if (Date.now() - scanBriefingLoadedAt < 3600000) return;
    const summary = document.getElementById('scan-summary');
    const list = document.getElementById('scan-country-list');
    summary.textContent = 'Loading source list…';
    try {
      const response = await fetch('/cyber?feed=scans');
      if (!response.ok) throw new Error(`SANS feed: ${response.status}`);
      const data = await response.json();
      const items = (data.items || []).filter(item => item.country && Number.isFinite(Number(item.lat)) && Number.isFinite(Number(item.lon)));
      const countsByCountry = new Map();
      for (const item of items) countsByCountry.set(item.country, (countsByCountry.get(item.country) || 0) + 1);
      const ranked = [...countsByCountry].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 5);
      list.replaceChildren();
      const countryNames = typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames([navigator.language], { type: 'region' }) : null;
      for (const [country, count] of ranked) {
        const row = document.createElement('button'); row.type = 'button'; row.className = 'cyber-country';
        let name = country;
        try { if (/^[A-Z]{2}$/.test(country)) name = countryNames?.of(country) || country; } catch { /* Keep source code. */ }
        row.setAttribute('aria-label', `Show ${count} mapped scan-source IP${count === 1 ? '' : 's'} in ${name}`);
        const label = textElement('span', 'cyber-country-label', name);
        const value = textElement('span', 'cyber-country-count', `${count} IP${count === 1 ? '' : 's'}`);
        const track = document.createElement('div'); track.className = 'cyber-country-track';
        const fill = document.createElement('span'); fill.style.width = `${Math.max(7, count / Math.max(1, ranked[0][1]) * 100)}%`;
        track.append(fill); row.append(label, value, track);
        row.addEventListener('click', () => {
          stopRotation();
          if (!enabled.scans) document.querySelector('[data-layer="scans"]')?.click();
          const subset = items.filter(item => item.country === country);
          const lon = subset.reduce((total, item) => total + Number(item.lon), 0) / subset.length;
          const lat = subset.reduce((total, item) => total + Number(item.lat), 0) / subset.length;
          if (validCoordinate(lat, lon)) map.flyTo({ center: [lon, lat], zoom: 4.5, duration: 950, essential: true });
          if (window.innerWidth < 901) setPanelOpen(false);
        });
        list.append(row);
      }
      summary.textContent = `${items.length} mapped of ${Number(data.listed) || items.length} IPs in SANS's top source list · Feed retrieved ${observedLabel(data.observed)}. Counts are source IPs, not attack volume. Select a country to see them on the globe.`;
      if (!ranked.length) list.textContent = 'No locations available in the current source list.';
      scanBriefingLoadedAt = Date.now();
    } catch (error) {
      console.warn(error);
      summary.textContent = 'Scan-source list is unavailable right now.';
      list.replaceChildren();
    }
  }

  let outbreakLoadedAt = 0;
  async function loadOutbreaks() {
    if (Date.now() - outbreakLoadedAt < 3600000) return;
    const list = document.getElementById('outbreak-list');
    list.textContent = 'Loading alerts…';
    try {
      const response = await fetch('/cyber?feed=outbreaks');
      if (!response.ok) throw new Error(`FortiGuard feed: ${response.status}`);
      const data = await response.json();
      list.replaceChildren();
      for (const item of (data.items || []).slice(0, 5)) {
        let source;
        try { source = new URL(item.sourceUrl); } catch { continue; }
        if (source.protocol !== 'https:' || source.hostname !== 'fortiguard.fortinet.com') continue;
        const row = document.createElement('article'); row.className = 'kev-item';
        const link = document.createElement('a'); link.href = source.href;
        link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = item.title || 'Outbreak alert';
        row.append(link);
        if (item.published) row.append(textElement('small', '', observedLabel(item.published)));
        if (item.summary) row.append(textElement('p', '', item.summary));
        list.append(row);
      }
      if (!list.childElementCount) list.textContent = 'No recent outbreak alerts available.';
      outbreakLoadedAt = Date.now();
    } catch (error) {
      console.warn(error);
      list.textContent = 'FortiGuard alerts are unavailable right now.';
    }
  }

  let kevLoadedAt = 0;
  async function loadKev() {
    if (Date.now() - kevLoadedAt < 3600000) return;
    const list = document.getElementById('kev-list');
    list.textContent = 'Loading…';
    try {
      const response = await fetch('/cyber?feed=kev');
      if (!response.ok) throw new Error(`CISA feed: ${response.status}`);
      const data = await response.json();
      list.replaceChildren();
      for (const item of (data.items || []).slice(0, 6)) {
        if (!/^CVE-\d{4}-\d{4,}$/.test(item.cve || '')) continue;
        const row = document.createElement('article');
        row.className = 'kev-item';
        const link = document.createElement('a');
        link.href = `https://nvd.nist.gov/vuln/detail/${encodeURIComponent(item.cve)}`;
        link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.textContent = item.cve;
        row.append(link, textElement('small', '', [item.dateAdded, item.vendor, item.product].filter(Boolean).join(' · ')));
        if (item.name) row.append(textElement('p', '', item.name));
        list.append(row);
      }
      if (!list.childElementCount) list.textContent = 'No recent catalog entries available.';
      kevLoadedAt = Date.now();
    } catch (error) {
      console.warn(error);
      list.textContent = 'CISA catalog is unavailable. Try again later.';
    }
  }

  async function refreshRadar(force = false) {
    if (!styleReady || !enabled.radar || document.hidden) return;
    if (!force && Date.now() - radarTime < 300000) return;
    let source = { tiles: ['/radar-tile?x={x}&y={y}&z={z}&size=256'], tileSize: 256, minzoom: 4, maxzoom: 13, bounds: [-168, 14, -50, 73], attribution: 'Radar © NOAA NWS' };
    let provider = 'NOAA';
    try {
      const response = await fetch('https://api.rainviewer.com/public/weather-maps.json', { cache: 'no-store' });
      if (!response.ok) throw new Error(`RainViewer: ${response.status}`);
      const data = await response.json();
      const frame = data.radar?.past?.at(-1);
      if (!data.host || !frame?.path) throw new Error('No radar frame');
      radarTileTemplate = `${data.host}${frame.path}/512/{z}/{x}/{y}/2/1_0.png`;
      source = {
        tiles: [`gm-radar://${frame.time}/{z}/{x}/{y}`],
        tileSize: 512, minzoom: 0, maxzoom: 7,
        attribution: 'Radar © RainViewer'
      };
      provider = 'RainViewer';
    } catch (error) {
      console.warn('RainViewer unavailable; using NOAA:', error);
    }
    if (!enabled.radar) return;
    if (map.getLayer('gm-radar-layer')) map.removeLayer('gm-radar-layer');
    if (map.getSource('gm-radar')) map.removeSource('gm-radar');
    map.addSource('gm-radar', { type: 'raster', ...source });
    map.addLayer({ id: 'gm-radar-layer', type: 'raster', source: 'gm-radar', paint: { 'raster-opacity': 0.57, 'raster-fade-duration': 0 }, layout: { visibility: 'visible' } }, 'gm-govair-points');
    radarProvider = provider;
    radarTime = Date.now();
    setCount('radar', null);
    const node = document.querySelector('[data-count="radar"]');
    if (node) node.textContent = provider;
  }

  async function loadRadio() {
    if (radioPromise || (fetchedAt.get('radio') && Date.now() - fetchedAt.get('radio') < 3600000)) return radioPromise;
    radioPromise = (async () => {
      const response = await fetch('/radio/stations');
      if (!response.ok) throw new Error(`Radio directory: ${response.status}`);
      const payload = await response.json();
      if (!Array.isArray(payload.stations)) throw new Error('Invalid radio directory');
      radioStations = new Map(payload.stations.map(station => [station.id, station]));
      map.getSource('gm-radio')?.setData({
        type: 'FeatureCollection',
        features: payload.stations.map(station => ({
          type: 'Feature', properties: { id: station.id },
          geometry: { type: 'Point', coordinates: [station.lon, station.lat] },
        })),
      });
      fetchedAt.set('radio', Date.now());
      setCount('radio', payload.stations.length);
      map.once('idle', () => tuneRadioTarget(false));
      if (payload.stale) showStatus('Showing cached radio stations; the directory could not refresh.');
    })().catch(error => {
      console.warn('Radio stations:', error);
      showStatus('Radio station directory is unavailable. Try again later.', true);
    }).finally(() => { radioPromise = null; });
    return radioPromise;
  }

  function stationPlace(station) {
    return [station.state, station.country].filter(Boolean).join(', ') || station.countryCode || 'Location unknown';
  }

  function positionRadioTarget() {
    if (radioTarget.hidden) return;
    const point = map.project(map.getCenter());
    radioTarget.style.left = `${point.x}px`;
    radioTarget.style.top = `${point.y}px`;
  }

  function stationsAtTarget() {
    if (!enabled.radio || !styleReady || !map.getLayer('gm-radio-points')) return [];
    const center = map.project(map.getCenter());
    const radius = 27;
    const features = map.queryRenderedFeatures(
      [[center.x - radius, center.y - radius], [center.x + radius, center.y + radius]],
      { layers: ['gm-radio-points'] },
    );
    const candidates = new Map();
    const viewCenter = map.getCenter();
    const centerLat = viewCenter.lat * Math.PI / 180;
    for (const feature of features) {
      const station = radioStations.get(feature.properties.id);
      if (!station || candidates.has(station.id)) continue;
      if (globeProjection) {
        const stationLat = station.lat * Math.PI / 180;
        const longitudeGap = (station.lon - viewCenter.lng) * Math.PI / 180;
        const facing = Math.sin(centerLat) * Math.sin(stationLat) + Math.cos(centerLat) * Math.cos(stationLat) * Math.cos(longitudeGap);
        if (facing <= 0) continue;
      }
      const point = map.project([station.lon, station.lat]);
      const distance = Math.hypot(point.x - center.x, point.y - center.y);
      if (distance <= radius) candidates.set(station.id, { station, distance });
    }
    return [...candidates.values()].sort((a, b) => a.distance - b.distance || b.station.clickCount - a.station.clickCount);
  }

  function tuneRadioTarget(autoplay) {
    if (!enabled.radio || !radioTargetEnabled) return;
    positionRadioTarget();
    const candidates = stationsAtTarget();
    const nearest = candidates[0]?.station;
    radioTarget.classList.toggle('locked', !!nearest);
    radioTargetLabel.textContent = nearest ? nearest.name : 'Drag globe to a station dot';
    radioList.hidden = true;
    nearbyRadioStations = candidates.slice(0, 12).map(item => item.station);
    radioNearbyButton.hidden = candidates.length < 2;
    radioNearbyButton.textContent = `${candidates.length} stations nearby`;
    if (!nearest) return;
    if (autoplay && (currentRadioId !== nearest.id || radioAudio.paused)) startRadio(nearest);
  }

  function showRadioList(stations, title) {
    radioListTitle.textContent = title;
    radioListItems.replaceChildren();
    for (const station of stations) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'radio-station';
      const name = document.createElement('strong');
      name.textContent = station.name;
      const detail = document.createElement('small');
      detail.textContent = [stationPlace(station), station.codec && `${station.codec}${station.bitrate ? ` · ${station.bitrate} kbps` : ''}`].filter(Boolean).join(' · ');
      const play = document.createElement('span');
      play.textContent = 'Play';
      button.append(name, detail, play);
      button.addEventListener('click', () => startRadio(station));
      radioListItems.append(button);
    }
    radioList.hidden = false;
  }

  function startRadio(station) {
    radioAudio.pause();
    clearTimeout(radioConnectTimer);
    radioReportedId = station.id;
    currentRadioId = station.id;
    document.getElementById('radio-player-title').textContent = station.name;
    document.getElementById('radio-player-location').textContent = stationPlace(station);
    radioPlayerStatus.textContent = 'Connecting to broadcaster…';
    radioPlayer.hidden = false;
    document.body.classList.add('radio-playing');
    radioAudio.src = station.streamUrl;
    radioAudio.load();
    radioConnectTimer = setTimeout(() => {
      if (currentRadioId !== station.id || !radioAudio.paused && radioAudio.readyState >= 3) return;
      radioAudio.pause();
      radioReportedId = null;
      radioPlayerStatus.textContent = 'This stream timed out. Try another station.';
    }, 15000);
    const attempt = radioAudio.play();
    if (attempt) attempt.catch(() => {
      if (currentRadioId === station.id) {
        clearTimeout(radioConnectTimer);
        radioPlayerStatus.textContent = 'Could not start this stream. Try another station.';
      }
    });
  }

  radioAudio.addEventListener('playing', () => {
    clearTimeout(radioConnectTimer);
    radioPlayerStatus.textContent = 'Live stream · supplied by the broadcaster';
    if (radioReportedId) {
      const id = radioReportedId;
      radioReportedId = null;
      fetch(`/radio/click?id=${encodeURIComponent(id)}`)
        .then(response => { if (!response.ok) console.warn('Radio Browser click report failed:', response.status); })
        .catch(error => console.warn('Radio Browser click report:', error));
    }
  });
  radioAudio.addEventListener('error', () => {
    clearTimeout(radioConnectTimer);
    if (!radioPlayer.hidden) radioPlayerStatus.textContent = 'This stream is unavailable. Try another station.';
  });
  document.getElementById('radio-list-close').addEventListener('click', () => { radioList.hidden = true; });
  radioNearbyButton.addEventListener('click', () => {
    if (nearbyRadioStations.length) showRadioList(nearbyRadioStations,
      `${nearbyRadioStations.length} stations nearest target`);
  });
  radioTargetToggle.addEventListener('click', () => {
    radioTargetEnabled = !radioTargetEnabled;
    radioTargetToggle.setAttribute('aria-pressed', String(radioTargetEnabled));
    radioTarget.hidden = !radioTargetEnabled || !enabled.radio;
    if (!radioTargetEnabled) { radioList.hidden = true; nearbyRadioStations = []; }
    else tuneRadioTarget(false);
  });
  document.getElementById('radio-player-close').addEventListener('click', () => {
    clearTimeout(radioConnectTimer);
    radioAudio.pause();
    radioAudio.removeAttribute('src');
    radioAudio.load();
    radioReportedId = null;
    currentRadioId = null;
    radioPlayer.hidden = true;
    document.body.classList.remove('radio-playing');
  });

  async function loadArcgis(type) {
    if (ARCGIS[type].global) {
      const key = 'world';
      if (arcgisQueryKeys.get(type) === key) return;
      arcgisQueryKeys.set(type, key);
      requests.get(type)?.abort();
      const controller = new AbortController();
      requests.set(type, controller);
      try {
        const response = await fetch(`/arcgis-layer?layer=${type}`, { signal: controller.signal });
        if (!response.ok) throw new Error(`${ARCGIS[type].label}: ${response.status}`);
        const data = await response.json();
        if (!enabled[type] || requests.get(type) !== controller) return;
        if (ARCGIS[type].kind === 'point') addGlobeVectorsToPoints(data);
        map.getSource(`gm-arcgis-${type}`)?.setData(data);
        setCount(type, data.features.length);
      } catch (error) {
        if (error.name === 'AbortError') return;
        arcgisQueryKeys.delete(type);
        console.error(error);
        showStatus(`${ARCGIS[type].label} are unavailable from the source service.`, true);
      }
      return;
    }
    const bounds = map.getBounds();
    let west = bounds.getWest();
    let east = bounds.getEast();
    while (west < -180) { west += 360; east += 360; }
    while (west >= 180) { west -= 360; east -= 360; }
    const south = Math.max(-90, bounds.getSouth());
    const north = Math.min(90, bounds.getNorth());
    if (east - west > 80 || north - south > 65 || south >= north) {
      showStatus(`${ARCGIS[type].label}: zoom in to load this area.`, false, 4000);
      return;
    }
    const boxes = east > 180 ? [[west, south, 180, north], [-180, south, east - 360, north]] : [[west, south, east, north]];
    const keys = boxes.map(box => box.map(n => n.toFixed(3)).join(','));
    const key = keys.join('|');
    if (arcgisQueryKeys.get(type) === key) return;
    arcgisQueryKeys.set(type, key);
    requests.get(type)?.abort();
    const controller = new AbortController();
    requests.set(type, controller);
    try {
      const responses = await Promise.all(keys.map(bbox => fetch(`/arcgis-layer?layer=${type}&bbox=${bbox}`, { signal: controller.signal })));
      if (responses.some(response => !response.ok)) throw new Error(`${ARCGIS[type].label}: source error`);
      const results = await Promise.all(responses.map(response => response.json()));
      const data = { type: 'FeatureCollection', features: results.flatMap(result => result.features),
        count: results.reduce((sum, result) => sum + result.count, 0), too_many: results.some(result => result.too_many) };
      if (data.too_many) data.features = [];
      if (!enabled[type] || requests.get(type) !== controller) return;
      if (ARCGIS[type].kind === 'point') addGlobeVectorsToPoints(data);
      map.getSource(`gm-arcgis-${type}`)?.setData(data);
      setCount(type, data.too_many ? null : data.features.length);
      if (data.too_many) showStatus(`${ARCGIS[type].label}: ${data.count.toLocaleString()} features here. Zoom in to see them.`, false, 5000);
    } catch (error) {
      if (error.name === 'AbortError') return;
      arcgisQueryKeys.delete(type);
      console.error(error);
      showStatus(`${ARCGIS[type].label} are unavailable from the source service.`, true);
    }
  }

  async function loadBordeauxTraffic() {
    if (!enabled.traffic || document.hidden || !styleReady || map.getZoom() < 6) return;
    if (!regionVisible({ bounds: { minLon: -0.9, maxLon: -0.3, minLat: 44.6, maxLat: 45.1 } }, currentBounds())) return;
    if (Date.now() - (fetchedAt.get('traffic') || 0) < 5 * 60 * 1000) return;
    requests.get('traffic')?.abort();
    const controller = new AbortController();
    requests.set('traffic', controller);
    try {
      const response = await fetch('/international-traffic', { signal: controller.signal });
      if (!response.ok) throw new Error(`Bordeaux traffic: ${response.status}`);
      const data = await response.json();
      if (controller.signal.aborted || requests.get('traffic') !== controller || !enabled.traffic) return;
      map.getSource('gm-bordeaux-traffic')?.setData(data);
      fetchedAt.set('traffic', Date.now());
    } catch (error) {
      if (error.name !== 'AbortError') console.warn('Bordeaux traffic flow:', error);
    }
  }

  function loadLayer(type) {
    if (!styleReady || !enabled[type] || zoomHint(type)) return;
    if (ARCGIS[type]) loadArcgis(type);
    else if (ROAD[type]) loadRoad(type);
    else if (type === 'govair' || type === 'civair') loadAircraft();
    else if (type === 'vessels') loadVessels();
    else if (type === 'webcams') loadWebcams();
    else if (type === 'international') loadInternationalEmergency();
    else if (type === 'cyclones' || type === 'earthquakes' || type === 'nws_alerts' || type === 'world_alerts' || type === 'fires' || type === 'floods' || type === 'volcanoes' || type === 'gdelt_events') loadHazards(type);
    else if (type === 'ports') loadPorts();
    else if (type === 'scans') loadScans();
    else if (type === 'power') loadPower();
    else if (type === 'radar') refreshRadar();
    else if (type === 'traffic') loadBordeauxTraffic();
    else if (type === 'goes') refreshGoesFrames();
    else if (type === 'radio') loadRadio();
    else if (type !== 'traffic' && type !== 'imagery' && type !== 'marine' && type !== 'fire_hotspots' && type !== 'daynight') loadStatic(type);
  }

  function scheduleViewportLoad() {
    clearTimeout(viewportTimer);
    viewportTimer = setTimeout(() => {
      for (const type of Object.keys(enabled)) {
        updateToggle(type);
        if (enabled[type] && !zoomHint(type) && (ARCGIS[type] || ROAD[type] || type === 'power' || type === 'traffic' || type === 'lpr' || type === 'govair' || type === 'civair' || type === 'vessels' || (fetchedAt.get(type) == null))) loadLayer(type);
      }
    }, 320);
  }

  function updateLocation() {
    cyberMapKey.classList.toggle('detail', map.getZoom() >= 3.5);
  }

  function syncAircraftElevation() {
    for (const type of ['govair', 'civair']) {
      const groundLayer = `gm-${type}-points`;
      if (map.getLayer(groundLayer)) setPointLayerFilter(groundLayer, terrainEnabled
        ? ['<=', ['coalesce', ['get', 'altitude_m'], 0], 0] : null);
      if (styleReady) updateToggle(type);
    }
    if (trackedAircraft?.lastPosition) updateTrackedMarker();
  }


  function applyTerrain() {
    if (terrainEnabled) {
      if (!map.getSource('gm-terrain')) map.addSource('gm-terrain', {
        type: 'raster-dem', tiles: ['/terrain/{z}/{x}/{y}.webp'],
        tileSize: 512, encoding: 'terrarium', maxzoom: 12,
        attribution: '<a href="https://mapterhorn.com/attribution/">© Mapterhorn</a>'
      });
      if (!map.getSource('gm-hillshade-source')) map.addSource('gm-hillshade-source', {
        type: 'raster-dem', tiles: ['/terrain/{z}/{x}/{y}.webp'],
        tileSize: 512, encoding: 'terrarium', maxzoom: 12
      });
      if (!map.getLayer('gm-hillshade')) map.addLayer({
        id: 'gm-hillshade', type: 'hillshade', source: 'gm-hillshade-source',
        paint: {
          'hillshade-shadow-color': '#102126',
          'hillshade-highlight-color': '#668783',
          'hillshade-accent-color': '#263d40',
          'hillshade-exaggeration': 0.45
        }
      }, 'waterway');
      if (!map.getLayer('gm-3d-buildings')) {
        const firstLabel = map.getStyle().layers.find(layer => layer.type === 'symbol')?.id;
        map.addLayer({
          id: 'gm-3d-buildings', type: 'fill-extrusion', source: 'openmaptiles',
          'source-layer': 'building', minzoom: 14,
          paint: {
            'fill-extrusion-color': '#536968',
            'fill-extrusion-height': ['coalesce', ['to-number', ['get', 'render_height']], ['to-number', ['get', 'height']], 8],
            'fill-extrusion-base': ['coalesce', ['to-number', ['get', 'render_min_height']], 0],
            'fill-extrusion-opacity': 0.82
          }
        }, firstLabel);
      }
      map.setTerrain({ source: 'gm-terrain', exaggeration: 1 });
    } else {
      map.setTerrain(null);
      if (map.getLayer('gm-3d-buildings')) map.removeLayer('gm-3d-buildings');
      if (map.getLayer('gm-hillshade')) map.removeLayer('gm-hillshade');
      if (map.getSource('gm-terrain')) map.removeSource('gm-terrain');
      if (map.getSource('gm-hillshade-source')) map.removeSource('gm-hillshade-source');
    }
    syncAircraftElevation();
  }

  map.on('style.load', () => {
    try {
      setGlobeFrontVector();
      basemapGeometryLayers = map.getStyle().layers
        .filter(layer => layer.type !== 'symbol' && layer.type !== 'background')
        .map(layer => ({ id: layer.id, visibility: layer.layout?.visibility || 'visible' }));
      basemapImageryActive = false;
      map.setProjection({ type: globeProjection ? 'globe' : 'mercator' });
      map.setPaintProperty('background', 'background-color', '#29393e');
      map.setPaintProperty('water', 'fill-color', '#142f3a');
      for (const [id, color] of Object.entries({
        landcover_ice_shelf: '#52666a', landcover_glacier: '#506367',
        landuse_residential: '#304044', landcover_wood: '#324744',
        landuse_park: '#324744', building: '#344347',
        'aeroway-area': '#34464a', road_area_pier: '#29393e'
      })) if (map.getLayer(id)) map.setPaintProperty(id, 'fill-color', color);
      for (const layer of map.getStyle().layers) {
        if (layer.type !== 'symbol' || !layer.id.startsWith('place_')) continue;
        map.setPaintProperty(layer.id, 'text-color', layer.id.startsWith('place_country') ? '#e3eae8' : '#c1d0ce');
        map.setPaintProperty(layer.id, 'text-halo-color', '#26373b');
      }
      basemapLabelPaint = map.getStyle().layers.filter(layer => layer.type === 'symbol' && layer.layout?.['text-field'] != null).map(layer => ({
        id: layer.id,
        paint: Object.fromEntries(['text-color', 'text-halo-color', 'text-halo-width', 'text-halo-blur'].map(property => [property, map.getPaintProperty(layer.id, property) ?? null])),
        layout: { visibility: map.getLayoutProperty(layer.id, 'visibility') ?? 'visible' }
      }));
      map.addSource('gm-traffic', {
        type: 'raster', tiles: ['gm-flow://{z}/{x}/{y}'], tileSize: 256,
        minzoom: 6, maxzoom: 19, bounds: [-168, 14, -50, 73],
        attribution: 'Traffic data from supported 511 providers'
      });
      map.addLayer({ id: 'gm-traffic-layer', type: 'raster', source: 'gm-traffic', paint: { 'raster-opacity': 0.62, 'raster-fade-duration': 0 }, layout: { visibility: 'none' } });
      map.addSource('gm-bordeaux-traffic', { type: 'geojson', data: EMPTY,
        attribution: '<a href="https://www.data.gouv.fr/datasets/etat-du-trafic-en-temps-reel-3">Bordeaux Métropole · Licence Ouverte</a>' });
      map.addLayer({ id: 'gm-bordeaux-traffic-layer', type: 'line', source: 'gm-bordeaux-traffic',
        minzoom: 6, layout: { visibility: 'none', 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': ['match', ['get', 'state'], 'FLUIDE', '#54bd87', 'DENSE', '#e6ad54',
          'EMBOUTEILLE', '#e46d65', 'IMPOSSIBLE', '#a75b79', '#9aa7a8'],
          'line-width': ['interpolate', ['linear'], ['zoom'], 6, 2, 12, 5],
          'line-opacity': 0.85 } });
      const initialImagery = imageryTiles();
      imageryTileKey = initialImagery.key;
      map.addSource('gm-imagery', {
        type: 'raster',
        tiles: [initialImagery.url],
        tileSize: 256, minzoom: 0, maxzoom: 9,
        attribution: '<a href="https://gibs.earthdata.nasa.gov/">NASA GIBS · Suomi NPP VIIRS</a>'
      });
      const firstLabel = map.getStyle().layers.find(layer => layer.type === 'symbol')?.id;
      map.addLayer({ id: 'gm-imagery-layer', type: 'raster', source: 'gm-imagery',
        paint: { 'raster-opacity': 0.83, 'raster-fade-duration': 0 }, layout: { visibility: 'none' } }, firstLabel);
      map.addSource('gm-detail-imagery', {
        type: 'raster',
        tiles: ['https://tiles.versatiles.org/tiles/satellite/{z}/{x}/{y}'],
        tileSize: 512, minzoom: 0, maxzoom: 12,
        attribution: '<a href="https://versatiles.org/sources/">VersaTiles imagery sources</a>'
      });
      map.addLayer({ id: 'gm-detail-imagery-layer', type: 'raster', source: 'gm-detail-imagery',
        paint: { 'raster-opacity': 1, 'raster-fade-duration': 0 }, layout: { visibility: 'none' } }, firstLabel);
      map.addSource('gm-esri-imagery', {
        type: 'raster',
        tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
        tileSize: 256, minzoom: 0, maxzoom: 23,
        attribution: '<a href="https://www.esri.com/en-us/legal/terms/web-site-service" target="_blank" rel="noopener">Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community · Terms</a>'
      });
      map.addLayer({ id: 'gm-esri-imagery-layer', type: 'raster', source: 'gm-esri-imagery',
        paint: { 'raster-opacity': 1, 'raster-fade-duration': 0 }, layout: { visibility: 'none' } }, 'gm-detail-imagery-layer');
      // Above zoom 12, VersaTiles has orthophotos for selected European regions.
      // Missing high-zoom tiles leave the global zoom-12 source visible underneath.
      map.addSource('gm-europe-imagery', {
        type: 'raster', tiles: ['https://tiles.versatiles.org/tiles/satellite/{z}/{x}/{y}'],
        tileSize: 512, minzoom: 13, maxzoom: 19, bounds: [-12, 34, 36, 72],
        attribution: '<a href="https://versatiles.org/sources/">VersaTiles imagery sources</a>'
      });
      map.addLayer({ id: 'gm-europe-imagery-layer', type: 'raster', source: 'gm-europe-imagery',
        paint: { 'raster-opacity': 1, 'raster-fade-duration': 0 }, layout: { visibility: 'none' } }, firstLabel);
      map.addSource('gm-us-imagery', {
        type: 'raster',
        tiles: ['/naip-tile?z={z}&x={x}&y={y}'],
        tileSize: 256, minzoom: 14, maxzoom: 19, bounds: [-125, 24, -66, 50],
        attribution: '<a href="https://www.usgs.gov/the-national-map-data-delivery">USGS, USDA · The National Map orthoimagery</a>'
      });
      map.addLayer({ id: 'gm-us-imagery-layer', type: 'raster', source: 'gm-us-imagery',
        paint: { 'raster-opacity': 1, 'raster-fade-duration': 0 }, layout: { visibility: 'none' } }, firstLabel);
      rebuildGoesLayers();
      map.addSource('gm-fire-hotspots', { type: 'raster', tiles: [
        `https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&TIME=${firmsDate}&LAYERS=VIIRS_SNPP_Thermal_Anomalies_375m_All&FORMAT=image/png&STYLES=&HEIGHT=256&SRS=EPSG:3857&WIDTH=256&BBOX={bbox-epsg-3857}&TRANSPARENT=TRUE`
      ], tileSize: 256, minzoom: 0, maxzoom: 9, attribution: '<a href="https://firms.modaps.eosdis.nasa.gov/" target="_blank" rel="noopener">NASA FIRMS · VIIRS thermal anomalies</a>' });
      map.addLayer({ id: 'gm-fire-hotspots-layer', type: 'raster', source: 'gm-fire-hotspots', paint: { 'raster-opacity': 0.82, 'raster-fade-duration': 0 }, layout: { visibility: 'none' } });
      map.addSource('gm-daynight', { type: 'geojson', data: dayNightGeoJSON() });
      map.addLayer({ id: 'gm-daynight-fill', type: 'fill', source: 'gm-daynight', paint: { 'fill-color': '#071523', 'fill-opacity': 0.42 }, layout: { visibility: 'none' } }, firstLabel);
      map.addSource('gm-cyclones-track', { type: 'geojson', data: EMPTY });
      map.addLayer({ id: 'gm-cyclones-track-line', type: 'line', source: 'gm-cyclones-track',
        paint: { 'line-color': POINT.cyclones.color, 'line-width': 2, 'line-opacity': 0.65, 'line-dasharray': [2, 1.5] },
        layout: { visibility: 'none' } });
      map.addSource('gm-cyclone-guidance', { type: 'geojson', data: cycloneGuidanceData,
        attribution: 'Forecast guidance: <a href="https://ftp.nhc.noaa.gov/atcf/aid_public/">NOAA National Hurricane Center ATCF</a> / <a href="https://hurricanes.ral.ucar.edu/realtime/current/">UCAR Tropical Cyclone Guidance Project</a>' });
      map.addLayer({ id: 'gm-cyclone-ensemble-lines', type: 'line', source: 'gm-cyclone-guidance',
        filter: ['==', ['get', 'kind'], 'ensemble'], layout: { visibility: 'none' },
        paint: { 'line-color': '#8faec1', 'line-width': 1.35, 'line-opacity': 0.46 } });
      map.addLayer({ id: 'gm-cyclone-model-casing', type: 'line', source: 'gm-cyclone-guidance',
        filter: ['==', ['get', 'kind'], 'model'], layout: { visibility: 'none' },
        paint: { 'line-color': '#142027', 'line-width': 4.5, 'line-opacity': 0.78 } });
      map.addLayer({ id: 'gm-cyclone-model-lines', type: 'line', source: 'gm-cyclone-guidance',
        filter: ['==', ['get', 'kind'], 'model'], layout: { visibility: 'none' },
        paint: { 'line-color': ['get', 'color'], 'line-width': 2.2, 'line-opacity': 0.88 } });
      map.addLayer({ id: 'gm-cyclone-consensus-casing', type: 'line', source: 'gm-cyclone-guidance',
        filter: ['==', ['get', 'kind'], 'consensus'], layout: { visibility: 'none' },
        paint: { 'line-color': '#142027', 'line-width': 5.2, 'line-opacity': 0.82, 'line-dasharray': [2.5, 1.1] } });
      map.addLayer({ id: 'gm-cyclone-consensus-line', type: 'line', source: 'gm-cyclone-guidance',
        filter: ['==', ['get', 'kind'], 'consensus'], layout: { visibility: 'none' },
        paint: { 'line-color': '#f0ce78', 'line-width': 3.1, 'line-opacity': 0.96, 'line-dasharray': [2.5, 1.1] } });
      map.addLayer({ id: 'gm-cyclone-official-casing', type: 'line', source: 'gm-cyclone-guidance',
        filter: ['==', ['get', 'kind'], 'official'], layout: { visibility: 'none' },
        paint: { 'line-color': '#142027', 'line-width': 5.5, 'line-opacity': 0.85 } });
      map.addLayer({ id: 'gm-cyclone-official-line', type: 'line', source: 'gm-cyclone-guidance',
        filter: ['==', ['get', 'kind'], 'official'], layout: { visibility: 'none' },
        paint: { 'line-color': '#f3f8ee', 'line-width': 3.5, 'line-opacity': 0.98 } });
      map.addSource('gm-nws-alerts-areas', { type: 'geojson', data: EMPTY });
      const nwsColor = ['match', ['get', 'severity'], 'Extreme', '#ce5550', 'Severe', '#d9825e',
        'Moderate', '#d8ad68', 'Minor', '#b9ad7c', '#93a8aa'];
      map.addLayer({ id: 'gm-nws-alerts-area-fill', type: 'fill', source: 'gm-nws-alerts-areas',
        paint: { 'fill-color': nwsColor, 'fill-opacity': 0.18 }, layout: { visibility: 'none' } });
      map.addLayer({ id: 'gm-nws-alerts-area-line', type: 'line', source: 'gm-nws-alerts-areas',
        paint: { 'line-color': nwsColor, 'line-width': 1.5, 'line-opacity': 0.85 }, layout: { visibility: 'none' } });
      for (const id of ['gm-nws-alerts-area-fill', 'gm-nws-alerts-area-line']) {
        map.on('click', id, event => {
          const item = event.features?.[0];
          if (item) openPopup('nws_alerts', item.properties.ref, event.lngLat.toArray());
        });
        map.on('mouseenter', id, () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', id, () => { map.getCanvas().style.cursor = ''; });
      }
      map.addSource('gm-world-alerts-areas', { type: 'geojson', data: EMPTY,
        attribution: 'Weather alerts: <a href="https://api.weather.gc.ca/collections/weather-alerts">Environment and Climate Change Canada</a> (<a href="https://eccc-msc.github.io/open-data/licence/readme_en/">licence</a>) / <a href="https://alerts.metservice.com/cap/rss">© Meteorological Service of New Zealand Limited</a> (<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>)' });
      const worldAlertColor = ['match', ['downcase', ['to-string', ['get', 'severity']]],
        'extreme', '#d15b58', 'red', '#d15b58', 'severe', '#db865d', 'orange', '#db865d',
        'moderate', '#d8ad68', 'yellow', '#d8ad68', 'minor', '#96b9ad', '#93a8aa'];
      map.addLayer({ id: 'gm-world-alerts-area-fill', type: 'fill', source: 'gm-world-alerts-areas',
        paint: { 'fill-color': worldAlertColor, 'fill-opacity': 0.18 }, layout: { visibility: 'none' } });
      map.addLayer({ id: 'gm-world-alerts-area-line', type: 'line', source: 'gm-world-alerts-areas',
        paint: { 'line-color': worldAlertColor, 'line-width': 1.5, 'line-opacity': 0.85 }, layout: { visibility: 'none' } });
      for (const id of ['gm-world-alerts-area-fill', 'gm-world-alerts-area-line']) {
        map.on('click', id, event => {
          const item = event.features?.[0];
          if (item) openPopup('world_alerts', item.properties.ref, event.lngLat.toArray());
        });
        map.on('mouseenter', id, () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', id, () => { map.getCanvas().style.cursor = ''; });
      }
      map.addSource('gm-aircraft-trail', { type: 'geojson', data: EMPTY,
        attribution: 'Aircraft flown trail: <a href="https://opensky-network.org/">OpenSky Network</a>' });
      for (const type of Object.keys(POINT).filter(type => type !== 'power')) addPointLayer(type);
      addPowerGeometry();
      map.addSource('gm-aircraft-route', { type: 'geojson', data: EMPTY,
        attribution: 'Airport route: <a href="https://adsb.im/">ADSB.im</a> (plausible callsign match)' });
      map.addLayer({ id: 'gm-aircraft-route-origin', type: 'line', source: 'gm-aircraft-route',
        filter: ['all', ['==', ['geometry-type'], 'LineString'], ['==', ['get', 'role'], 'origin']],
        paint: { 'line-color': '#f5f7f4', 'line-width': 2.4, 'line-opacity': 0.9 } });
      map.addLayer({ id: 'gm-aircraft-route-destination', type: 'line', source: 'gm-aircraft-route',
        filter: ['all', ['==', ['geometry-type'], 'LineString'], ['==', ['get', 'role'], 'destination']],
        paint: { 'line-color': '#f4ca73', 'line-width': 2.4, 'line-opacity': 0.95,
          'line-dasharray': [2, 2] } });
      map.addLayer({ id: 'gm-aircraft-route-airports', type: 'circle', source: 'gm-aircraft-route',
        filter: ['==', ['geometry-type'], 'Point'],
        paint: { 'circle-radius': 5, 'circle-color': '#f4ca73', 'circle-stroke-color': '#16242b', 'circle-stroke-width': 2 } });
      setPointLayerFilter('gm-aircraft-route-airports', ['==', ['geometry-type'], 'Point']);
      map.addLayer({ id: 'gm-aircraft-route-labels', type: 'symbol', source: 'gm-aircraft-route',
        filter: ['==', ['geometry-type'], 'Point'],
        layout: { 'text-field': ['get', 'code'], 'text-size': 12, 'text-offset': [0, -1.4], 'text-allow-overlap': true },
        paint: { 'text-color': '#fff8db', 'text-halo-color': '#142229', 'text-halo-width': 2 } });
      setPointLayerFilter('gm-aircraft-route-labels', ['==', ['geometry-type'], 'Point']);
      map.addLayer({ id: 'gm-aircraft-trail-line', type: 'line', source: 'gm-aircraft-trail',
        paint: { 'line-color': '#79d8c8', 'line-width': ['interpolate', ['linear'], ['zoom'], 2, 2.5, 10, 4],
          'line-opacity': 0.94 } });
      map.addSource('gm-tracked-aircraft', { type: 'geojson', data: EMPTY });
      iconImage('gm-tracked-icon', '#f0c979', '✈');
      iconImage('gm-tracked-sat-icon', '#ffdf8b', '✈');
      map.addLayer({ id: 'gm-tracked-halo', type: 'circle', source: 'gm-tracked-aircraft',
        paint: { 'circle-radius': 20, 'circle-color': '#f0c979', 'circle-opacity': 0.12,
          'circle-stroke-color': '#f0c979', 'circle-stroke-width': 2,
          'circle-stroke-opacity': ['case', ['boolean', ['get', 'stale'], false], 0.45, 0.95] } });
      setPointLayerFilter('gm-tracked-halo');
      map.addLayer({ id: 'gm-tracked-plane', type: 'symbol', source: 'gm-tracked-aircraft',
        layout: { 'icon-image': 'gm-tracked-icon', 'icon-size': 1.28,
          'icon-allow-overlap': true, 'icon-ignore-placement': true,
          'icon-rotate': ['coalesce', ['get', 'heading'], 0] },
        paint: { 'icon-opacity': ['case', ['boolean', ['get', 'stale'], false], 0.55, 1] } });
      setPointLayerFilter('gm-tracked-plane');
      map.addSource('gm-radio', { type: 'geojson', data: { type: 'FeatureCollection', features: [...radioStations.values()].map(station => ({
        type: 'Feature', properties: withGlobeVector({ id: station.id }, station.lon, station.lat), geometry: { type: 'Point', coordinates: [station.lon, station.lat] },
      })) }, attribution: '<a href="https://www.radio-browser.info/">Radio Browser</a>' });
      map.addLayer({ id: 'gm-radio-points', type: 'circle', source: 'gm-radio', layout: { visibility: 'none' },
        paint: { 'circle-color': '#b2eaca', 'circle-radius': ['interpolate', ['linear'], ['zoom'], 0, 2, 5, 3, 11, 4],
          'circle-stroke-color': '#173632', 'circle-stroke-width': 1,
          'circle-opacity': 0.92 } });
      setPointLayerFilter('gm-radio-points');
      for (const [type, config] of Object.entries(ARCGIS)) {
        const source = `gm-arcgis-${type}`;
        map.addSource(source, { type: 'geojson', data: EMPTY, attribution: ARCGIS_ATTRIBUTION });
        const common = { id: `${source}-layer`, source, layout: { visibility: 'none' } };
        if (config.kind === 'shipping') map.addLayer({ ...common, type: 'line',
          paint: { 'line-color': ['match', ['get', 'Type'], 'Major', '#f2c77a', 'Middle', '#76cbd0', 'Minor', '#456c78', '#76cbd0'],
            'line-width': ['match', ['get', 'Type'], 'Major', 2.1, 'Middle', 1.25, 'Minor', 0.65, 1.1],
            'line-opacity': ['match', ['get', 'Type'], 'Major', 0.95, 'Middle', 0.58, 'Minor', 0.19, 0.5] } });
        else if (config.kind === 'line') map.addLayer({ ...common, type: 'line',
          paint: { 'line-color': config.color, 'line-width': ['interpolate', ['linear'], ['zoom'], 5, 1.8, 12, 3.5], 'line-opacity': 0.9 } });
        else if (config.kind === 'polygon') map.addLayer({ ...common, type: 'fill',
          paint: { 'fill-color': config.color, 'fill-opacity': 0.35, 'fill-outline-color': '#f5dfa1' } });
        else map.addLayer({ ...common, type: 'circle',
          paint: { 'circle-color': config.color, 'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 4, 12, 7],
            'circle-stroke-color': '#172a30', 'circle-stroke-width': 1.5, 'circle-opacity': 0.95 } });
        if (config.kind === 'point') setPointLayerFilter(`${source}-layer`);
      }
      if (trackedAircraft?.lastPosition) updateTrackedMarker();
      if (terrainEnabled) applyTerrain();
      styleReady = true;
      fetchedAt.delete('traffic');
      resumeRotation();
      arcgisQueryKeys.clear();
      for (const type of Object.keys(enabled)) updateToggle(type);
      if (enabled.traffic) loadBordeauxTraffic();
      if (Object.keys(ARCGIS).some(type => enabled[type])) scheduleViewportLoad();
      updateLocation();
      if (loadingMessage) loadingMessage.textContent = 'Drawing the globe';
      map.once('idle', finishLoading);
      setTimeout(finishLoading, 6000);
    } catch (error) {
      console.error('Map initialization:', error);
      showStatus('Map layers could not initialize.', true, 0);
      showLoadingError('The globe could not initialize. Please retry.');
    }
  });

  map.on('load', async () => {
    try {
      const response = await fetch('/road-regions.json');
      if (!response.ok) throw new Error(`Region catalog: ${response.status}`);
      roadRegions = await response.json();
      scheduleViewportLoad();
    } catch (error) { console.error(error); showStatus('Road coverage catalog is unavailable.', true); }
  });
  map.on('move', updateLocation);
  map.on('move', setGlobeFrontVector);
  map.on('move', positionRadioTarget);
  map.on('moveend', () => {
    if (!rotationEnabled) scheduleViewportLoad();
    tuneRadioTarget(radioTuneAfterMove);
    radioTuneAfterMove = false;
  });
  map.on('click', event => {
    const layers = Object.keys(ARCGIS).filter(type => enabled[type] && map.getLayer(`gm-arcgis-${type}-layer`))
      .map(type => `gm-arcgis-${type}-layer`);
    if (!layers.length) return;
    const feature = map.queryRenderedFeatures(event.point, { layers })[0];
    if (!feature) return;
    const type = feature.layer.id.replace('gm-arcgis-', '').replace('-layer', '');
    const properties = feature.properties || {};
    const title = type === 'rail_narn' ? (properties.SUBDIV || properties.DIVISION || 'Rail segment')
      : type === 'rail_world' ? 'Rail corridor'
      : properties.name || properties.p_name || properties.FEAT_NAME || properties.owner || properties.operator || ARCGIS[type].label;
    const details = {
      transmission: [['Voltage', properties.voltage && `${properties.voltage} kV`], ['Status', properties.status], ['Owner', properties.owner]],
      gas_pipelines: [['Type', properties.typepipe], ['Operator', properties.operator]],
      wind_turbines: [['Installed', properties.p_year], ['Turbine', [properties.t_manu, properties.t_model].filter(Boolean).join(' ')], ['Capacity', properties.t_cap && `${properties.t_cap} kW`]],
      solar_sites: [['Installed', properties.p_year], ['Capacity', properties.p_cap_ac && `${properties.p_cap_ac} MW AC`], ['Area', properties.p_area && `${properties.p_area} acres`]],
      power_plants: [['Country', properties.country_long], ['Fuel', properties.primary_fuel], ['Capacity', properties.capacity_mw && `${properties.capacity_mw} MW`], ['Data year', properties.year_of_capacity_data]],
      active_faults: [['Slip type', properties.slip_type], ['Catalog', properties.catalog_na]],
      impact_sites: [['Country', properties.COUNTRY], ['Region', properties.REGION], ['Estimated age (Ma)', properties.AGE_DESC], ['Diameter', properties.DIA_QUANT && `${properties.DIA_QUANT} km`], ['Impactor', properties.IMPACTOR]],
      rail_world: [['Continent', properties.continent], ['Class', properties.featurecla], ['Coverage', 'Generalized; local tracks may be omitted']],
      rail_narn: [['Country', { US: 'United States', CA: 'Canada', MX: 'Mexico' }[properties.COUNTRY] || properties.COUNTRY], ['Owner code', properties.RROWNER1], ['Division', properties.DIVISION], ['Subdivision', properties.SUBDIV], ['Tracks', properties.TRACKS > 0 ? properties.TRACKS : null]],
      shipping_routes: [['Route class', properties.Type], ['Coverage', 'Mapped customary routes; not live vessel tracks']]
    }[type];
    const content = document.createElement('div');
    content.className = 'arcgis-popup';
    const heading = document.createElement('strong');
    heading.textContent = String(title);
    content.append(heading);
    for (const [label, value] of details) {
      if (value == null || value === '' || value === 'NOT AVAILABLE') continue;
      const row = document.createElement('div');
      row.textContent = `${label}: ${value}`;
      content.append(row);
    }
    const source = document.createElement('small');
    source.textContent = ARCGIS[type].source + ' · inventory, not live status';
    content.append(source);
    new maplibregl.Popup({ maxWidth: '280px' }).setLngLat(event.lngLat).setDOMContent(content).addTo(map);
  });
  map.on('dragend', () => {
    if (!enabled.radio || !radioTargetEnabled) return;
    radioTuneAfterMove = true;
  });
  map.on('error', event => {
    console.error('MapLibre:', event.error || event);
    if (!styleReady) showStatus('Map style is unavailable.', true, 0);
  });
  map.on('click', 'gm-radio-points', event => {
    if (window.globalMapDrawing || !enabled.radio || !event.features?.length) return;
    const station = radioStations.get(event.features[0].properties.id);
    if (!station) return;
    map.easeTo({ center: [station.lon, station.lat], duration: 420 });
    startRadio(station);
  });
  map.on('mouseenter', 'gm-radio-points', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'gm-radio-points', () => { map.getCanvas().style.cursor = ''; });

  const rotationToggle = document.getElementById('rotation-toggle');
  rotationToggle.setAttribute('aria-pressed', String(rotationEnabled));
  function stopRotation() {
    if (!rotationEnabled) return;
    rotationEnabled = false;
    cancelAnimationFrame(rotationFrame);
    rotationFrame = 0;
    rotationTime = 0;
    rotationToggle.setAttribute('aria-pressed', 'false');
    scheduleViewportLoad();
  }
  window.globalMapStopRotation = stopRotation;
  function rotateFrame(timestamp) {
    rotationFrame = 0;
    if (!rotationEnabled || document.hidden || !globeProjection) return;
    if (rotationTime && timestamp - rotationTime >= 30) {
      const elapsed = Math.min(timestamp - rotationTime, 100);
      const center = map.getCenter();
      const longitude = ((center.lng + elapsed * 0.002 + 180) % 360 + 360) % 360 - 180;
      map.setCenter([longitude, center.lat]);
      rotationTime = timestamp;
    } else if (!rotationTime) rotationTime = timestamp;
    rotationFrame = requestAnimationFrame(rotateFrame);
  }
  function resumeRotation() {
    if (!rotationEnabled || document.hidden || rotationFrame || !styleReady) return;
    rotationTime = 0;
    rotationFrame = requestAnimationFrame(rotateFrame);
  }
  rotationToggle.addEventListener('click', () => {
    if (!styleReady || !globeProjection) return;
    if (rotationEnabled) { stopRotation(); return; }
    rotationEnabled = true;
    rotationToggle.setAttribute('aria-pressed', 'true');
    if (map.getZoom() > 2.2 || map.getPitch() > 0) {
      map.easeTo({ zoom: Math.min(map.getZoom(), 2.2), pitch: 0, duration: 650 });
      map.once('moveend', () => { scheduleViewportLoad(); resumeRotation(); });
    } else resumeRotation();
    showStatus('Globe rotation on · move the map to stop', false, 3000);
  });
  map.getCanvasContainer().addEventListener('pointerdown', stopRotation);
  map.getCanvasContainer().addEventListener('wheel', stopRotation, { passive: true });
  map.getCanvas().addEventListener('keydown', stopRotation);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      cancelAnimationFrame(rotationFrame);
      rotationFrame = 0;
      rotationTime = 0;
    } else resumeRotation();
  });
  document.getElementById('zoom-in').addEventListener('click', () => { stopRotation(); map.zoomTo(Math.min(map.getZoom() + 1, 19), { duration: 350 }); });
  document.getElementById('zoom-out').addEventListener('click', () => { stopRotation(); map.zoomTo(Math.max(map.getZoom() - 1, 0), { duration: 350 }); });
  document.getElementById('rotate-left').addEventListener('click', () => { stopRotation(); map.easeTo({ bearing: map.getBearing() - 45, duration: 350 }); });
  document.getElementById('rotate-right').addEventListener('click', () => { stopRotation(); map.easeTo({ bearing: map.getBearing() + 45, duration: 350 }); });
  document.getElementById('reset-view').addEventListener('click', () => {
    stopRotation();
    pauseTrackingForNavigation();
    history.replaceState(null, '', location.pathname);
    searchMarker?.remove();
    map.flyTo({ ...HOME, duration: 800, essential: true });
  });
  document.getElementById('projection-toggle').addEventListener('click', event => {
    const next = !globeProjection;
    if (!next) stopRotation();
    try { map.setProjection({ type: next ? 'globe' : 'mercator' }); }
    catch (error) { showStatus('Projection switch is unavailable.', true); return; }
    globeProjection = next;
    rotationToggle.disabled = !next;
    event.currentTarget.setAttribute('aria-pressed', String(next));
    setGlobeFrontVector();
    showStatus(next ? 'Spherical globe view' : 'Flat map view', false, 2400);
  });
  const terrainToggle = document.getElementById('terrain-toggle');
  const terrainDemo = document.getElementById('terrain-demo');
  terrainToggle.addEventListener('click', () => {
    if (!styleReady) return;
    stopRotation();
    terrainEnabled = !terrainEnabled;
    try { applyTerrain(); }
    catch (error) {
      terrainEnabled = false;
      console.error('3D terrain:', error);
      showStatus('3D terrain is unavailable right now.', true);
      return;
    }
    terrainToggle.setAttribute('aria-pressed', String(terrainEnabled));
    terrainDemo.hidden = !terrainEnabled;
    map.easeTo({ pitch: terrainEnabled ? 58 : 0, duration: 650, essential: true });
    showStatus(terrainEnabled ? '3D terrain on' : '3D off', false, 3800);
  });
  terrainDemo.addEventListener('click', () => {
    stopRotation();
    map.flyTo({ center: [7.66, 45.98], zoom: 11.2, pitch: 68, bearing: -25, duration: 1700, essential: true });
    if (window.innerWidth < 901) setPanelOpen(false);
  });
  const destinations = {
    'north-america': { center: [-95, 39], zoom: 5.1 },
    europe: { center: [13, 49], zoom: 4.3 },
    asia: { center: [100, 29], zoom: 3.9 },
    'south-america': { center: [-61, -15], zoom: 3.7 },
    africa: { center: [22, 0], zoom: 3.4 },
    oceania: { center: [150, -27], zoom: 3.5 }
  };
  document.querySelectorAll('[data-destination]').forEach(button => button.addEventListener('click', () => {
    stopRotation();
    pauseTrackingForNavigation();
    map.flyTo({ ...destinations[button.dataset.destination], pitch: terrainEnabled ? 58 : 0, duration: 1000, essential: true });
    if (window.innerWidth < 901) setPanelOpen(false);
  }));
  const placeForm = document.getElementById('place-search');
  const placeInput = document.getElementById('place-query');
  const placeResults = document.getElementById('place-results');
  let searchOptions = [];
  let activeMatch = -1;
  let searchMarker = null;
  const normalizeSearch = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
  function closeSearch() {
    placeResults.hidden = true;
    placeInput.setAttribute('aria-expanded', 'false');
    placeInput.removeAttribute('aria-activedescendant');
    activeMatch = -1;
  }
  function parseCoordinates(value) {
    const match = /^\s*([+-]?\d{1,2}(?:\.\d+)?)\s*[, ]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$/.exec(value);
    if (!match) return null;
    const lat = Number(match[1]), lon = Number(match[2]);
    return validCoordinate(lat, lon) ? { lat, lon } : null;
  }
  function selectPlace(row) {
    aircraftSearchController?.abort();
    stopRotation();
    pauseTrackingForNavigation();
    const [name, country, lat, lon, , kind] = row;
    placeInput.value = `${name}, ${country}`;
    closeSearch();
    placeInput.blur();
    searchMarker?.remove();
    const pin = document.createElement('div');
    pin.className = 'search-pin'; pin.title = name; pin.setAttribute('aria-label', `Selected place: ${name}`);
    searchMarker = new maplibregl.Marker({ element: pin, anchor: 'bottom' }).setLngLat([lon, lat]).addTo(map);
    map.flyTo({ center: [lon, lat], zoom: kind === 'port' ? 9 : 8, duration: 1100, essential: true });
    if (window.innerWidth < 901) setPanelOpen(false);
  }
  function selectAircraft(item) {
    searchMarker?.remove();
    searchMarker = null;
    placeInput.value = String(item.flight || item.r || item.hex).trim();
    closeSearch();
    placeInput.blur();
    startTracking(item);
  }
  function selectVessel(vessel) {
    stopRotation();
    pauseTrackingForNavigation();
    if (!enabled.vessels) setLayerEnabled('vessels', true, true);
    const name = String(vessel.name || `MMSI ${vessel.mmsi}`).trim();
    const age = Math.max(0, Math.floor((Date.now() / 1000 - Number(vessel.updated_at)) / 60));
    placeInput.value = name;
    closeSearch();
    placeInput.blur();
    searchMarker?.remove();
    const pin = document.createElement('div');
    pin.className = 'search-pin';
    pin.title = `${name} · MMSI ${vessel.mmsi}`;
    pin.setAttribute('aria-label', `Selected vessel: ${name}, MMSI ${vessel.mmsi}`);
    searchMarker = new maplibregl.Marker({ element: pin, anchor: 'bottom' })
      .setLngLat([Number(vessel.lon), Number(vessel.lat)]).addTo(map);
    map.flyTo({ center: [Number(vessel.lon), Number(vessel.lat)], zoom: Math.max(map.getZoom(), 6.5), duration: 1100, essential: true });
    showStatus(`${name} · MMSI ${vessel.mmsi} · last AIS report ${age} min ago`, false, 6500);
    if (window.innerWidth < 901) setPanelOpen(false);
  }
  async function lookupVessels(query) {
    aircraftSearchController?.abort();
    const controller = new AbortController();
    aircraftSearchController = controller;
    searchOptions = [];
    activeMatch = -1;
    const collectingHere = map.getZoom() >= 5;
    if (collectingHere && !enabled.vessels) setLayerEnabled('vessels', true, true);
    placeResults.replaceChildren(textElement('div', 'place-empty', 'Searching recent AIS vessel reports…'));
    placeResults.hidden = false;
    placeInput.setAttribute('aria-expanded', 'true');
    try {
      const response = await fetch(`/vessels/search?q=${encodeURIComponent(query)}`, { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error(`Vessel search: ${response.status}`);
      const data = await response.json();
      if (aircraftSearchController !== controller) return;
      if (data.status === 'needs_key') {
        placeResults.replaceChildren(textElement('div', 'place-empty', 'Vessel search needs AISSTREAM_API_KEY configured on the server.'));
      } else if (!(data.vessels || []).length) {
        const message = collectingHere
          ? 'No recent AIS name match yet. Live vessels is collecting this area now; try again in a few moments.'
          : 'No recent AIS name match. Search covers ships recently reported in areas the live vessel layer has monitored; zoom to 5+ to collect nearby traffic.';
        placeResults.replaceChildren(textElement('div', 'place-empty', message));
      } else {
        searchOptions = data.vessels.map(vessel => ({ kind: 'vessel-row', vessel }));
        placeResults.replaceChildren();
        searchOptions.forEach((option, index) => {
          const vessel = option.vessel;
          const age = Math.max(0, Math.floor((Date.now() / 1000 - Number(vessel.updated_at)) / 60));
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'place-result'; button.id = `place-option-${index}`;
          button.setAttribute('role', 'option'); button.setAttribute('aria-selected', 'false');
          button.append(textElement('span', '', String(vessel.name || `MMSI ${vessel.mmsi}`).trim()),
            textElement('small', '', `MMSI ${vessel.mmsi} · reported ${age} min ago`));
          button.addEventListener('click', () => selectVessel(vessel));
          placeResults.append(button);
        });
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      console.warn('Vessel search:', error);
      placeResults.replaceChildren(textElement('div', 'place-empty', 'Vessel search is unavailable. Try again shortly.'));
    } finally {
      if (aircraftSearchController === controller) aircraftSearchController = null;
    }
  }
  async function lookupAircraft(query) {
    aircraftSearchController?.abort();
    const liveMatches = aircraftSnapshot.filter(row =>
      [row.hex, row.r, row.flight].some(value => String(value || '').trim().toUpperCase() === query) &&
      /^[0-9a-f]{6}$/i.test(String(row.hex || '')) && validCoordinate(Number(row.lat), Number(row.lon)));
    if (liveMatches.length === 1 && Date.now() - aircraftFetchedAt < 120000) {
      selectAircraft(liveMatches[0]);
      return;
    }
    const controller = new AbortController();
    aircraftSearchController = controller;
    searchOptions = [];
    activeMatch = -1;
    placeResults.replaceChildren(textElement('div', 'place-empty', 'Finding current aircraft…'));
    placeResults.hidden = false;
    placeInput.setAttribute('aria-expanded', 'true');
    const scope = /^[0-9A-F]{6}$/.test(query) ? 'hex' : 'registration';
    try {
      const findMatches = async searchScope => {
        const response = await fetch(`/aircraft?scope=${searchScope}&id=${encodeURIComponent(query)}`, { signal: controller.signal, cache: 'no-store' });
        if (response.status === 429) throw new Error('rate_limited');
        if (!response.ok) throw new Error(`Aircraft search: ${response.status}`);
        const data = await response.json();
        return (data.aircraft || []).filter(row => /^[0-9a-f]{6}$/i.test(String(row.hex || '')) && validCoordinate(Number(row.lat), Number(row.lon)));
      };
      let matches = await findMatches(scope);
      if (aircraftSearchController !== controller) return;
      // Registrations (tail numbers) are checked first for every non-ICAO query.
      // If there is no registered aircraft match, treat the input as a callsign.
      if (!matches.length && scope === 'registration') matches = await findMatches('callsign');
      if (aircraftSearchController !== controller) return;
      if (!matches.length) {
        placeResults.replaceChildren(textElement('div', 'place-empty', 'No current aircraft position found for that identifier.'));
      } else if (matches.length === 1) {
        selectAircraft(matches[0]);
      } else {
        searchOptions = matches.slice(0, 8).map(item => ({ kind: 'aircraft-row', item }));
        activeMatch = -1;
        placeResults.replaceChildren();
        searchOptions.forEach((option, index) => {
          const item = option.item;
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'place-result'; button.id = `place-option-${index}`;
          button.setAttribute('role', 'option'); button.setAttribute('aria-selected', 'false');
          button.append(textElement('span', '', String(item.flight || item.r || item.hex).trim()),
            textElement('small', '', `ICAO ${String(item.hex).toUpperCase()}`));
          button.addEventListener('click', () => selectAircraft(item));
          placeResults.append(button);
        });
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      console.warn('Aircraft search:', error);
      placeResults.replaceChildren(textElement('div', 'place-empty', error.message === 'rate_limited'
        ? 'Aircraft provider is busy. Try again in a minute.' : 'Aircraft lookup is unavailable. Try again shortly.'));
    } finally {
      if (aircraftSearchController === controller) aircraftSearchController = null;
    }
  }
  function chooseSearchOption(option) {
    if (option.kind === 'place') selectPlace(option.row);
    else if (option.kind === 'aircraft-query') lookupAircraft(option.query);
    else if (option.kind === 'aircraft-row') selectAircraft(option.item);
    else if (option.kind === 'vessel-query') lookupVessels(option.query);
    else if (option.kind === 'vessel-row') selectVessel(option.vessel);
  }
  function renderSearch() {
    const q = normalizeSearch(placeInput.value);
    const coordinate = parseCoordinates(placeInput.value);
    placeResults.replaceChildren();
    searchOptions = [];
    if (!q) { closeSearch(); return; }
    if (coordinate) {
      const row = [placeInput.value.trim(), 'Coordinates', coordinate.lat, coordinate.lon, 0, 'coordinate'];
      searchOptions = [{ kind: 'place', row }];
    } else {
      let placeMatches = [];
      if (placesPromise?.rows) {
      const matches = [];
      for (const row of placesPromise.rows) {
        const name = normalizeSearch(row[0]);
        const country = normalizeSearch(row[1]);
        if (!name.includes(q) && !country.startsWith(q)) continue;
        const rank = (name === q ? 100000000 : name.startsWith(q) ? 50000000 : country === q ? 1000000 : 0) + Math.min(Number(row[4]) || 0, 10000000);
        matches.push({ row, rank });
      }
      matches.sort((a, b) => b.rank - a.rank || a.row[0].localeCompare(b.row[0]));
      placeMatches = matches.slice(0, 8).map(x => ({ kind: 'place', row: x.row }));
      }
      const raw = placeInput.value.trim().toUpperCase();
      const aircraft = /^[A-Z0-9-]{2,12}$/.test(raw) ? { kind: 'aircraft-query', query: raw } : null;
      const vessel = q.length >= 3 && q.length <= 80 ? { kind: 'vessel-query', query: placeInput.value.trim() } : null;
      searchOptions = aircraft && /[0-9-]/.test(raw)
        ? [aircraft, ...(vessel ? [vessel] : []), ...placeMatches]
        : [...placeMatches, ...(vessel ? [vessel] : []), ...(aircraft ? [aircraft] : [])];
    }
    if (!searchOptions.length) {
      const message = placesPromise?.rows ? 'No matching city or port. Try an aircraft callsign, tail number, ICAO hex, vessel name, or coordinates.' : placesPromise?.failed ? 'Place catalog unavailable; enter latitude, longitude.' : 'Loading place catalog…';
      placeResults.append(textElement('div', 'place-empty', message));
    } else {
      searchOptions.forEach((option, index) => {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'place-result'; button.id = `place-option-${index}`;
        button.setAttribute('role', 'option'); button.setAttribute('aria-selected', 'false');
        if (option.kind === 'aircraft-query') button.append(textElement('span', '', `Find aircraft ${option.query}`), textElement('small', '', 'Callsign / tail / ICAO'));
        else if (option.kind === 'vessel-query') button.append(textElement('span', '', `Search vessel names for “${option.query}”`), textElement('small', '', 'Recent live AIS reports'));
        else button.append(textElement('span', '', `${option.row[0]}, ${option.row[1]}`), textElement('small', '', option.row[5] === 'coordinate' ? 'Coordinate' : option.row[5] === 'port' ? 'Port' : 'City'));
        button.addEventListener('click', () => chooseSearchOption(option));
        placeResults.append(button);
      });
      if (searchOptions.some(option => option.kind === 'place' && option.row[5] !== 'coordinate')) {
        const credit = document.createElement('div');
        credit.className = 'place-credit';
        credit.append('Place data: ');
        const geonames = document.createElement('a');
        geonames.href = 'https://www.geonames.org/'; geonames.target = '_blank'; geonames.rel = 'noopener noreferrer';
        geonames.textContent = 'GeoNames';
        credit.append(geonames, ' / NGA World Port Index');
        placeResults.append(credit);
      }
    }
    activeMatch = -1;
    placeResults.hidden = false;
    placeInput.setAttribute('aria-expanded', 'true');
  }
  function ensurePlaces() {
    if (!placesPromise) placesPromise = fetch('/places.json').then(response => {
      if (!response.ok) throw new Error(`Place catalog: ${response.status}`);
      return response.json();
    }).then(rows => {
      placesPromise.rows = rows;
      if (document.activeElement === placeInput && !aircraftSearchController) renderSearch();
    }).catch(error => {
      console.warn(error);
      placesPromise.failed = true;
      showStatus('Place search is unavailable. Coordinate search still works.', true);
      if (document.activeElement === placeInput && !aircraftSearchController) renderSearch();
    });
  }
  placeInput.addEventListener('focus', () => { ensurePlaces(); if (placeInput.value) renderSearch(); });
  placeInput.addEventListener('input', () => { aircraftSearchController?.abort(); aircraftSearchController = null; renderSearch(); });
  placeInput.addEventListener('keydown', event => {
    if (event.key === 'Escape') { closeSearch(); placeInput.blur(); return; }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    if (!searchOptions.length) return;
    activeMatch = (activeMatch + (event.key === 'ArrowDown' ? 1 : -1) + searchOptions.length) % searchOptions.length;
    placeResults.querySelectorAll('.place-result').forEach((node, index) => {
      node.classList.toggle('active', index === activeMatch);
      node.setAttribute('aria-selected', String(index === activeMatch));
    });
    placeInput.setAttribute('aria-activedescendant', `place-option-${activeMatch}`);
  });
  placeForm.addEventListener('submit', event => {
    event.preventDefault();
    const coordinate = parseCoordinates(placeInput.value);
    if (coordinate) selectPlace([placeInput.value.trim(), 'Coordinates', coordinate.lat, coordinate.lon, 0, 'coordinate']);
    else if (searchOptions.length) chooseSearchOption(searchOptions[activeMatch >= 0 ? activeMatch : 0]);
    else showStatus('Search for a place, aircraft, or latitude, longitude.', true);
  });
  document.addEventListener('pointerdown', event => { if (!placeForm.contains(event.target)) closeSearch(); });
  const cyberFocusButton = document.getElementById('cyber-focus');
  function setCyberFocus(next) {
    cyberFocus = next && enabled.scans;
    if (cyberFocus) activePointPopup?.remove();
    aircraftTrackPanel.hidden = !trackedAircraft || cyberFocus;
    document.body.classList.toggle('tracking-aircraft', Boolean(trackedAircraft && !cyberFocus));
    updateTrackedMarker();
    cyberFocusButton.setAttribute('aria-pressed', String(cyberFocus));
    cyberFocusButton.textContent = cyberFocus ? 'Show all' : 'Focus';
    for (const type of Object.keys(enabled)) updateToggle(type);
  }
  cyberFocusButton.addEventListener('click', () => setCyberFocus(!cyberFocus));
  document.querySelectorAll('[data-imagery-mode]').forEach(button => button.addEventListener('click', () => {
    imageryMode = button.dataset.imageryMode;
    refreshImagery();
  }));
  goesPlayButton.addEventListener('click', () => {
    if (goesTimer) { stopGoesPlayback(); return; }
    if (!goesFrames.east.length && !goesFrames.west.length) return;
    goesPreload = true;
    if (goesFrameIndex === 5) { goesFrameIndex = 0; renderGoesFrame(); }
    else renderGoesFrame();
    goesPlayButton.textContent = 'Ⅱ Pause';
    goesPlayButton.setAttribute('aria-label', 'Pause GOES cloud frames');
    goesTimer = setInterval(() => {
      if (!enabled.goes || document.hidden) { stopGoesPlayback(); return; }
      goesFrameIndex = (goesFrameIndex + 1) % 6;
      renderGoesFrame();
    }, 2200);
  });
  goesFrameInput.addEventListener('input', () => {
    stopGoesPlayback();
    goesPreload = true;
    goesFrameIndex = Number(goesFrameInput.value);
    renderGoesFrame();
  });
  refreshImagery();
  setInterval(() => { if (enabled.imagery && !document.hidden) refreshImagery(); }, 5 * 60 * 1000);
  setInterval(() => { if (enabled.goes && !document.hidden) refreshGoesFrames(); }, 5 * 60 * 1000);
  setInterval(() => { if (enabled.daynight && !document.hidden) map.getSource('gm-daynight')?.setData(dayNightGeoJSON()); }, 60 * 1000);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopGoesPlayback();
    else {
      if (enabled.imagery) refreshImagery();
      if (enabled.goes) refreshGoesFrames();
    }
  });
  function setLayerEnabled(type, next, quiet = false) {
    if (enabled[type] === next) return;
    enabled[type] = next;
    if (!next && trackedAircraft?.layer === type) stopTracking();
    document.querySelector(`[data-layer="${type}"]`).setAttribute('aria-pressed', String(next));
    if (type === 'scans') {
      cyberMapKey.hidden = !enabled.scans;
      if (enabled.scans) { cyberMapCount.textContent = 'Loading mapped sources…'; cyberMapUpdated.textContent = ''; }
      else setCyberFocus(false);
    } else if (cyberFocus && enabled[type]) setCyberFocus(false);
    if (type === 'marine') document.body.classList.toggle('marine-mode', enabled.marine);
    if (type === 'shipping_routes') document.body.classList.toggle('shipping-routes-on', enabled.shipping_routes);
    updateToggle(type);
    if (type === 'imagery' && enabled.imagery) refreshImagery();
    if (type === 'goes' && !enabled.goes) { stopGoesPlayback(); goesPreload = false; }
    updateGroupControls();
    if (!enabled[type]) {
      requests.get(type)?.abort();
      if (ARCGIS[type]) {
        arcgisQueryKeys.delete(type);
        map.getSource(`gm-arcgis-${type}`)?.setData(EMPTY);
        setCount(type, null);
      }
      if (type === 'radio') { radioList.hidden = true; radioTarget.hidden = true; }
      if (type === 'marine') seaPopup?.remove();
      if (POINT[type]) {
        refs.get(type)?.clear();
        map.getSource(`gm-${type}`)?.setData(EMPTY);
        if (type === 'cyclones') {
          map.getSource('gm-cyclones-track')?.setData(EMPTY);
          hideCycloneGuidance();
        }
        if (type === 'nws_alerts') map.getSource('gm-nws-alerts-areas')?.setData(EMPTY);
        if (type === 'world_alerts') map.getSource('gm-world-alerts-areas')?.setData(EMPTY);
        setCount(type, null);
      }
      return;
    }
    const hint = zoomHint(type);
    if (hint && !quiet) showStatus(`Zoom to level ${POINT[type]?.minZoom || RASTER[type]?.minZoom || ARCGIS[type]?.minZoom} to show ${POINT[type]?.label?.toLowerCase() || ARCGIS[type]?.label?.toLowerCase() || type}.`);
    else if (type === 'marine' && !quiet) showStatus('Click an ocean location or a port to inspect sea conditions.', false, 5000);
    else loadLayer(type);
  }
  document.querySelectorAll('[data-layer]').forEach(button => button.addEventListener('click', () => {
    const type = button.dataset.layer;
    setLayerEnabled(type, !enabled[type]);
  }));
  document.querySelectorAll('[data-layer-group] .group-toggle').forEach(button => button.addEventListener('click', () => {
    const { header, types } = layerGroups.get(button.closest('[data-layer-group]').dataset.layerGroup);
    const turnOn = !types.every(type => enabled[type]);
    for (const type of types) setLayerEnabled(type, turnOn, true);
    if (turnOn && types.some(type => zoomHint(type))) {
      showStatus(`${header.querySelector('span').textContent} enabled. Zoom in to see layers with local coverage.`, false, 5000);
    }
  }));
  document.getElementById('cyber-toggle').addEventListener('click', event => {
    const open = event.currentTarget.getAttribute('aria-expanded') !== 'true';
    event.currentTarget.setAttribute('aria-expanded', String(open));
    const panel = document.getElementById('cyber-panel');
    panel.hidden = !open;
    if (open) {
      loadScanBriefing();
      loadOutbreaks();
      loadKev();
      panel.scrollIntoView({ block: 'nearest' });
    }
  });
  document.getElementById('panel-toggle').addEventListener('click', event => {
    setPanelOpen(event.currentTarget.getAttribute('aria-expanded') !== 'true');
  });
  map.on('click', event => {
    if (window.innerWidth < 901) setPanelOpen(false);
    if (window.globalMapTripPicking || window.globalMapDrawing) return;
    if (enabled.marine) {
      const pointLayers = Object.keys(POINT).map(type => `gm-${type}-points`).filter(id => map.getLayer(id));
      if (!map.queryRenderedFeatures(event.point, { layers: [...pointLayers, 'gm-radio-points'].filter(id => map.getLayer(id)) }).length) {
        openSeaConditions([event.lngLat.lng, event.lngLat.lat]);
      }
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    for (const type of Object.keys(enabled)) if (enabled[type] && !zoomHint(type)) loadLayer(type);
  });
  setInterval(() => {
    if (document.hidden) return;
    for (const type of Object.keys(POINT)) {
      if (!enabled[type] || zoomHint(type)) continue;
      // A slow road response must get a chance to finish before the next tick.
      // Viewport changes still start a fresh request through scheduleViewportLoad.
      const started = requestStartedAt.get(type) || 0;
      if (started > (fetchedAt.get(type) || 0) && Date.now() - started < 90000) continue;
      const refreshMs = (type === 'govair' || type === 'civair') && map.getZoom() < 6 ? 900000 : POINT[type].refreshMs;
      if (Date.now() - (fetchedAt.get(type) || 0) >= refreshMs) loadLayer(type);
    }
    if (enabled.radar) refreshRadar();
  }, 15000);
})();
