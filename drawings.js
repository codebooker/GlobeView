/* Private map notes. No drawing data is sent to GlobalMap's server. */
(() => {
  'use strict';
  const map = window.globalMap;
  if (!map) return;
  const KEY = 'globalmap.drawings.v1';
  const MAX_ITEMS = 250;
  const MAX_VERTICES = 200;
  const MIN_RADIUS_METERS = 10;
  const MAX_RADIUS_METERS = 5_000_000;
  const EMPTY = { type: 'FeatureCollection', features: [] };
  const $ = id => document.getElementById(id);
  const panel = $('draw-panel');
  const toggle = $('draw-toggle');
  const message = $('draw-message');
  const progress = $('draw-progress');
  const list = $('draw-list');
  const selectedBox = $('draw-selected');
  const nameField = $('draw-name');
  const colorField = $('draw-color');
  const radiusField = $('draw-radius');
  const radiusUnit = $('draw-radius-unit');
  let items = [];
  let selectedId = null;
  let mode = null;
  let vertices = [];
  let hover = null;
  let markers = [];
  let storageAvailable = true;
  let restoreLayersOnClose = false;
  radiusUnit.value = /^en-US\b/i.test(navigator.language) ? 'mi' : 'km';
  let radiusDisplayUnit = radiusUnit.value;

  const TYPE_LABELS = { pin: 'Pin', label: 'Label', line: 'Line', polygon: 'Area', circle: 'Circle' };

  const validPoint = value => Array.isArray(value) && value.length === 2 &&
    Number.isFinite(value[0]) && Number.isFinite(value[1]) &&
    Math.abs(value[0]) <= 180 && Math.abs(value[1]) <= 90;
  const cleanPoint = value => [Number(value[0].toFixed(6)), Number(value[1].toFixed(6))];
  const mapPoint = lngLat => cleanPoint([((lngLat.lng + 180) % 360 + 360) % 360 - 180, lngLat.lat]);
  const radians = value => value * Math.PI / 180;
  function distanceMeters(a, b) {
    const deltaLat = radians(b[1] - a[1]);
    const deltaLon = radians(b[0] - a[0]);
    const sinLat = Math.sin(deltaLat / 2), sinLon = Math.sin(deltaLon / 2);
    const h = sinLat * sinLat + Math.cos(radians(a[1])) * Math.cos(radians(b[1])) * sinLon * sinLon;
    return 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function circleRing(center, radiusMeters) {
    const lat = radians(center[1]), lon = radians(center[0]);
    const angular = radiusMeters / 6371008.8;
    const ring = [];
    for (let i = 0; i <= 96; i++) {
      const bearing = 2 * Math.PI * i / 96;
      const pointLat = Math.asin(Math.max(-1, Math.min(1, Math.sin(lat) * Math.cos(angular) + Math.cos(lat) * Math.sin(angular) * Math.cos(bearing))));
      const pointLon = lon + Math.atan2(Math.sin(bearing) * Math.sin(angular) * Math.cos(lat),
        Math.cos(angular) - Math.sin(lat) * Math.sin(pointLat));
      let degreesLon = pointLon * 180 / Math.PI;
      while (degreesLon - center[0] > 180) degreesLon -= 360;
      while (degreesLon - center[0] < -180) degreesLon += 360;
      ring.push([degreesLon, pointLat * 180 / Math.PI]);
    }
    ring[ring.length - 1] = ring[0].slice();
    return ring;
  }
  const formatRadius = meters => radiusUnit.value === 'mi'
    ? `${(meters / 1609.344).toFixed(meters < 16093 ? 1 : 0)} mi`
    : `${(meters / 1000).toFixed(meters < 10000 ? 1 : 0)} km`;
  function cleanItem(value) {
    if (!value || typeof value !== 'object' || typeof value.id !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(value.id)) return null;
    if (!TYPE_LABELS[value.type]) return null;
    if (typeof value.name !== 'string' || typeof value.color !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(value.color)) return null;
    const path = value.type === 'line' || value.type === 'polygon';
    if (path && (!Array.isArray(value.coordinates) || value.coordinates.length < (value.type === 'line' ? 2 : 3) || value.coordinates.length > MAX_VERTICES || !value.coordinates.every(validPoint))) return null;
    if (!path && !validPoint(value.coordinates)) return null;
    const item = { id: value.id, type: value.type, name: value.name.slice(0, 80) || TYPE_LABELS[value.type],
      color: value.color, coordinates: path ? value.coordinates.map(cleanPoint) : cleanPoint(value.coordinates) };
    if (value.type === 'circle') {
      if (typeof value.radiusMeters !== 'number' || !Number.isFinite(value.radiusMeters) || value.radiusMeters < MIN_RADIUS_METERS || value.radiusMeters > MAX_RADIUS_METERS) return null;
      item.radiusMeters = Math.round(value.radiusMeters * 100) / 100;
    }
    return item;
  }
  function tell(value, error = false) {
    message.textContent = value;
    message.classList.toggle('error', error);
  }
  function readItems() {
    try {
      const parsed = JSON.parse(localStorage.getItem(KEY) || '[]');
      items = Array.isArray(parsed) ? parsed.slice(0, MAX_ITEMS).map(cleanItem).filter(Boolean) : [];
    } catch (error) {
      storageAvailable = false;
      tell('Browser storage is unavailable. Drawings in this session will disappear after reload.', true);
    }
  }
  function persist() {
    if (!storageAvailable) return false;
    try {
      localStorage.setItem(KEY, JSON.stringify(items));
      return true;
    } catch (error) {
      storageAvailable = false;
      tell('Could not save to this browser. Free storage or allow site data to keep drawings.', true);
      return false;
    }
  }
  function makeId() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
  function setPanelOpen(open) {
    if (open && window.innerWidth >= 901 && !document.body.classList.contains('panel-collapsed')) {
      $('panel-toggle')?.click();
      restoreLayersOnClose = true;
    }
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    toggle.classList.toggle('active', open);
    window.globalMapDrawing = open;
    if (open) {
      if (!$('trip-panel').hidden) $('trip-close').click();
    } else {
      cancelMode();
      if (restoreLayersOnClose && document.body.classList.contains('panel-collapsed')) $('panel-toggle')?.click();
      restoreLayersOnClose = false;
    }
  }
  function setMode(next) {
    if (mode === next) { cancelMode(); return; }
    mode = next;
    vertices = [];
    hover = null;
    for (const type of Object.keys(TYPE_LABELS)) $(`draw-${type}`).classList.toggle('active', mode === type);
    map.getCanvas().classList.toggle('draw-cursor', !!mode);
    progress.hidden = !['polygon', 'line', 'circle'].includes(mode);
    updateProgress();
    tell(({ pin: 'Click the map to drop a pin. Click again to add more.',
      label: 'Click the map to place a label. Edit its text below.',
      line: 'Click the map to add line points, then finish after at least two.',
      polygon: 'Click the map to add area corners, then finish after at least three.',
      circle: 'Click the map for the center, then click its edge to set the radius.' })[mode]);
  }
  function cancelMode() {
    mode = null;
    vertices = [];
    hover = null;
    for (const type of Object.keys(TYPE_LABELS)) $(`draw-${type}`).classList.remove('active');
    map.getCanvas().classList.remove('draw-cursor');
    progress.hidden = true;
    renderDraft();
  }
  function updateProgress() {
    $('draw-progress-text').textContent = mode === 'circle'
      ? vertices.length ? `Center set · ${hover ? `radius ${formatRadius(distanceMeters(vertices[0], hover))}` : 'click an edge point'}` : 'Click the center point'
      : `${vertices.length} ${vertices.length === 1 ? 'point' : 'points'} added`;
    $('draw-undo').disabled = !vertices.length;
    $('draw-finish').hidden = mode === 'circle';
    $('draw-finish').disabled = vertices.length < (mode === 'line' ? 2 : 3);
    $('draw-finish').textContent = mode === 'line' ? 'Finish line' : 'Finish area';
    renderDraft();
  }
  function newItem(type, coordinates, extra = {}) {
    if (items.length >= MAX_ITEMS) { tell(`This browser has reached the ${MAX_ITEMS}-drawing limit. Delete one to add another.`, true); return; }
    const name = `${TYPE_LABELS[type]} ${items.filter(item => item.type === type).length + 1}`;
    const item = { id: makeId(), type, name, color: ({ pin: '#e8b86d', label: '#f0d491', line: '#89b9eb', polygon: '#70c9bd', circle: '#8ab8df' })[type], coordinates, ...extra };
    items.push(item);
    selectedId = item.id;
    const saved = persist();
    render();
    if (saved) tell(`${name} saved in this browser. Rename it below if you like.`);
  }
  function finishPath() {
    const minimum = mode === 'line' ? 2 : 3;
    if (vertices.length < minimum) { tell(`Add at least ${minimum} points to finish this drawing.`, true); return; }
    const points = vertices.slice();
    const type = mode;
    cancelMode();
    newItem(type, points);
  }
  function feature(item) {
    const ring = item.type === 'circle' ? circleRing(item.coordinates, item.radiusMeters)
      : [...item.coordinates, item.coordinates[0]];
    return { type: 'Feature', properties: { id: item.id, color: item.color, selected: item.id === selectedId, type: item.type },
      geometry: { type: 'Polygon', coordinates: [ring] } };
  }
  function installLayers() {
    if (!map.getStyle() || map.getSource('gm-user-polygons')) return;
    map.addSource('gm-user-polygons', { type: 'geojson', data: EMPTY });
    map.addLayer({ id: 'gm-user-fill', type: 'fill', source: 'gm-user-polygons',
      paint: { 'fill-color': ['get', 'color'], 'fill-opacity': ['case', ['get', 'selected'], 0.30, 0.16] } });
    map.addLayer({ id: 'gm-user-line', type: 'line', source: 'gm-user-polygons',
      filter: ['==', ['get', 'type'], 'polygon'],
      paint: { 'line-color': ['get', 'color'], 'line-width': ['case', ['get', 'selected'], 4, 2.5], 'line-opacity': 0.98 } });
    map.addLayer({ id: 'gm-user-circle-line', type: 'line', source: 'gm-user-polygons',
      filter: ['==', ['get', 'type'], 'circle'],
      paint: { 'line-color': ['get', 'color'], 'line-width': ['case', ['get', 'selected'], 4, 2.5], 'line-opacity': 0.98, 'line-dasharray': [3, 1.5] } });
    map.addSource('gm-user-lines', { type: 'geojson', data: EMPTY });
    map.addLayer({ id: 'gm-user-paths', type: 'line', source: 'gm-user-lines',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': ['get', 'color'], 'line-width': ['case', ['get', 'selected'], 5, 3], 'line-opacity': 0.95 } });
    map.addSource('gm-user-draft', { type: 'geojson', data: EMPTY });
    map.addLayer({ id: 'gm-user-draft-fill', type: 'fill', source: 'gm-user-draft',
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'fill-color': '#f0d491', 'fill-opacity': 0.16 } });
    map.addLayer({ id: 'gm-user-draft-outline', type: 'line', source: 'gm-user-draft',
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'line-color': '#f0d491', 'line-width': 2.5, 'line-dasharray': [2, 1.5] } });
    map.addLayer({ id: 'gm-user-draft-line', type: 'line', source: 'gm-user-draft',
      filter: ['==', ['geometry-type'], 'LineString'],
      paint: { 'line-color': '#f0d491', 'line-width': 2.5, 'line-dasharray': [2, 1.5] } });
    map.addLayer({ id: 'gm-user-draft-points', type: 'circle', source: 'gm-user-draft',
      filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-radius': 5, 'circle-color': '#f0d491', 'circle-stroke-color': '#23383b', 'circle-stroke-width': 2 } });
    renderMap();
    renderDraft();
  }
  function renderDraft() {
    const source = map.getSource('gm-user-draft');
    if (!source) return;
    const features = vertices.map(coordinates => ({ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates } }));
    if (mode === 'circle' && vertices.length && hover) {
      const radius = distanceMeters(vertices[0], hover);
      if (radius >= MIN_RADIUS_METERS && radius <= MAX_RADIUS_METERS) features.unshift({ type: 'Feature', properties: {},
        geometry: { type: 'Polygon', coordinates: [circleRing(vertices[0], radius)] } });
    } else if (mode === 'line' || mode === 'polygon') {
      const line = hover && vertices.length ? [...vertices, hover] : vertices;
      if (line.length >= 2) features.unshift({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: line } });
    }
    source.setData({ type: 'FeatureCollection', features });
  }
  function renderMap() {
    map.getSource('gm-user-polygons')?.setData({ type: 'FeatureCollection', features: items.filter(item => item.type === 'polygon' || item.type === 'circle').map(feature) });
    map.getSource('gm-user-lines')?.setData({ type: 'FeatureCollection', features: items.filter(item => item.type === 'line').map(item => ({
      type: 'Feature', properties: { id: item.id, color: item.color, selected: item.id === selectedId },
      geometry: { type: 'LineString', coordinates: item.coordinates }
    })) });
    for (const marker of markers) marker.remove();
    markers = [];
    for (const item of items.filter(value => value.type === 'pin' || value.type === 'label')) {
      const element = document.createElement('button');
      element.type = 'button';
      element.className = (item.type === 'pin' ? 'user-pin' : 'user-label') + (item.id === selectedId ? ' selected' : '');
      element.style.setProperty(item.type === 'pin' ? '--pin-color' : '--label-color', item.color);
      if (item.type === 'label') element.textContent = item.name;
      element.title = item.name;
      element.setAttribute('aria-label', `Select ${item.type} ${item.name}`);
      element.addEventListener('click', event => { event.stopPropagation(); select(item.id); });
      markers.push(new maplibregl.Marker({ element, anchor: item.type === 'pin' ? 'bottom' : 'center' }).setLngLat(item.coordinates).addTo(map));
    }
  }
  function zoomTo(item) {
    window.globalMapStopRotation?.();
    if (item.type === 'pin' || item.type === 'label') map.flyTo({ center: item.coordinates, zoom: Math.max(map.getZoom(), 10), duration: 650, essential: true });
    else if (item.type === 'circle') {
      const zoom = Math.max(1.5, Math.min(14, Math.log2(19_500_000 * Math.max(0.15, Math.cos(radians(item.coordinates[1]))) / item.radiusMeters)));
      map.flyTo({ center: item.coordinates, zoom, duration: 650, essential: true });
    }
    else {
      const bounds = item.coordinates.reduce((box, point) => box.extend(point), new maplibregl.LngLatBounds(item.coordinates[0], item.coordinates[0]));
      map.fitBounds(bounds, { padding: 90, maxZoom: 13, duration: 650, essential: true });
    }
  }
  function select(id, fly = false) {
    selectedId = id;
    render();
    const item = items.find(value => value.id === id);
    if (item) {
      tell(`${TYPE_LABELS[item.type]} selected. Edit its name${item.type === 'circle' ? ', radius,' : ' or'} color below.`);
      if (fly) zoomTo(item);
    }
  }
  function renderList() {
    list.replaceChildren();
    $('draw-count').textContent = String(items.length);
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'draw-empty';
      empty.textContent = 'No drawings yet. Choose a tool above to start.';
      list.append(empty);
    }
    for (const item of [...items].reverse()) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'draw-item' + (item.id === selectedId ? ' selected' : '');
      const symbol = document.createElement('span'); symbol.className = 'draw-item-symbol';
      symbol.textContent = ({ pin: '●', label: 'T', line: '╱', polygon: '⬡', circle: '◯' })[item.type]; symbol.style.color = item.color;
      const label = document.createElement('span'); label.textContent = item.name;
      const kind = document.createElement('small'); kind.textContent = item.type === 'circle' ? `Circle · ${formatRadius(item.radiusMeters)}` : TYPE_LABELS[item.type];
      button.append(symbol, label, kind);
      button.addEventListener('click', () => select(item.id, true));
      list.append(button);
    }
    const selected = items.find(value => value.id === selectedId);
    selectedBox.hidden = !selected;
    if (selected) {
      nameField.value = selected.name; colorField.value = selected.color;
      $('draw-name-label').textContent = selected.type === 'label' ? 'Label text' : 'Name';
      const isCircle = selected.type === 'circle';
      $('draw-radius-label').hidden = !isCircle;
      $('draw-radius-control').hidden = !isCircle;
      if (isCircle) radiusField.value = Number((selected.radiusMeters / (radiusUnit.value === 'mi' ? 1609.344 : 1000)).toFixed(2));
    }
  }
  function render() { renderMap(); renderList(); }

  readItems();
  toggle.addEventListener('click', () => setPanelOpen(panel.hidden));
  $('draw-close').addEventListener('click', () => setPanelOpen(false));
  $('draw-pin').addEventListener('click', () => setMode('pin'));
  $('draw-label').addEventListener('click', () => setMode('label'));
  $('draw-line').addEventListener('click', () => setMode('line'));
  $('draw-polygon').addEventListener('click', () => setMode('polygon'));
  $('draw-circle').addEventListener('click', () => setMode('circle'));
  $('draw-cancel').addEventListener('click', () => { cancelMode(); tell('Drawing cancelled. Saved drawings remain on the map.'); });
  $('draw-undo').addEventListener('click', () => { vertices.pop(); updateProgress(); });
  $('draw-finish').addEventListener('click', finishPath);
  radiusUnit.addEventListener('change', () => {
    const priorUnit = radiusDisplayUnit;
    radiusDisplayUnit = radiusUnit.value;
    const pendingName = nameField.value, pendingColor = colorField.value;
    const pendingRadius = Number(radiusField.value) * (priorUnit === 'mi' ? 1609.344 : 1000);
    renderList();
    const selected = items.find(value => value.id === selectedId);
    if (selected) { nameField.value = pendingName; colorField.value = pendingColor; }
    if (selected?.type === 'circle' && Number.isFinite(pendingRadius))
      radiusField.value = Number((pendingRadius / (radiusUnit.value === 'mi' ? 1609.344 : 1000)).toFixed(2));
  });
  $('draw-save').addEventListener('click', () => {
    const item = items.find(value => value.id === selectedId);
    if (!item) return;
    if (item.type === 'circle') {
      const meters = Number(radiusField.value) * (radiusUnit.value === 'mi' ? 1609.344 : 1000);
      if (!Number.isFinite(meters) || meters < MIN_RADIUS_METERS || meters > MAX_RADIUS_METERS) {
        tell('Enter a radius between 10 meters and 5,000 km.', true); radiusField.focus(); return;
      }
      item.radiusMeters = Math.round(meters * 100) / 100;
    }
    item.name = nameField.value.trim().slice(0, 80) || TYPE_LABELS[item.type];
    item.color = colorField.value;
    const saved = persist();
    render();
    if (saved) tell('Changes saved in this browser.');
  });
  $('draw-delete').addEventListener('click', () => {
    const item = items.find(value => value.id === selectedId);
    if (!item || !window.confirm(`Delete “${item.name}”?`)) return;
    items = items.filter(value => value.id !== item.id);
    selectedId = null;
    const saved = persist();
    render();
    if (saved) tell('Drawing deleted.');
  });
  document.addEventListener('keydown', event => {
    if (!mode || !panel.hidden && event.target.closest('input, textarea')) return;
    if (event.key === 'Escape') { cancelMode(); tell('Drawing cancelled.'); }
    else if ((mode === 'polygon' || mode === 'line') && event.key === 'Enter') { event.preventDefault(); finishPath(); }
    else if (['polygon', 'line', 'circle'].includes(mode) && (event.key === 'Backspace' || event.key === 'Delete')) { event.preventDefault(); vertices.pop(); updateProgress(); }
  });
  $('trip-toggle').addEventListener('click', () => { if (!panel.hidden) setPanelOpen(false); });
  map.on('click', event => {
    if (panel.hidden) return;
    const point = mapPoint(event.lngLat);
    if (mode === 'pin' || mode === 'label') { newItem(mode, point); return; }
    if (mode === 'circle') {
      if (!vertices.length) { vertices.push(point); updateProgress(); tell('Move the cursor to preview the radius, then click its edge.'); return; }
      const radiusMeters = distanceMeters(vertices[0], point);
      if (radiusMeters < MIN_RADIUS_METERS || radiusMeters > MAX_RADIUS_METERS) { tell('Choose an edge between 10 meters and 5,000 km from the center.', true); return; }
      const center = vertices[0]; cancelMode(); newItem('circle', center, { radiusMeters: Math.round(radiusMeters * 100) / 100 }); return;
    }
    if (mode === 'polygon' || mode === 'line') {
      if (vertices.length >= MAX_VERTICES) { tell(`A drawing can have up to ${MAX_VERTICES} points. Finish this one first.`, true); return; }
      vertices.push(point); updateProgress(); return;
    }
    const layers = ['gm-user-line', 'gm-user-circle-line', 'gm-user-fill', 'gm-user-paths'].filter(id => map.getLayer(id));
    const hit = layers.length && map.queryRenderedFeatures(event.point, { layers })[0];
    if (hit?.properties?.id) select(hit.properties.id);
  });
  map.on('mousemove', event => {
    if (!['polygon', 'line', 'circle'].includes(mode) || !vertices.length) return;
    hover = mapPoint(event.lngLat);
    if (mode === 'circle') $('draw-progress-text').textContent = `Radius preview · ${formatRadius(distanceMeters(vertices[0], hover))}`;
    renderDraft();
  });
  map.on('style.load', installLayers);
  map.on('load', installLayers);
  if (map.isStyleLoaded()) installLayers();
  render();
})();
