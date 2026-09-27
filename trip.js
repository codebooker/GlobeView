(() => {
  'use strict';
  const map = window.globalMap;
  if (!map) return;
  const panel = document.getElementById('trip-panel');
  const toggle = document.getElementById('trip-toggle');
  const form = document.getElementById('trip-form');
  const fields = { from: document.getElementById('trip-from'), to: document.getElementById('trip-to') };
  const suggestionBoxes = { from: document.getElementById('trip-from-results'), to: document.getElementById('trip-to-results') };
  const message = document.getElementById('trip-message');
  const result = document.getElementById('trip-result');
  const stepsList = document.getElementById('trip-steps');
  const submit = document.getElementById('trip-submit');
  const mode = document.getElementById('trip-mode');
  const EMPTY = { type: 'FeatureCollection', features: [] };
  const points = { from: null, to: null };
  const markers = { from: null, to: null };
  let catalogPromise = null;
  let catalog = [];
  let route = null;
  let request = null;
  let picking = null;
  let restoreLayersOnClose = false;
  const miles = /^en-US\b/i.test(navigator.language);

  const normalize = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
  const coordinateLabel = point => `${point.lat.toFixed(5)}, ${point.lon.toFixed(5)}`;
  function parseCoordinates(value) {
    const match = /^\s*([+-]?\d{1,2}(?:\.\d+)?)\s*[, ]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$/.exec(value);
    if (!match) return null;
    const lat = Number(match[1]), lon = Number(match[2]);
    return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? { lat, lon } : null;
  }
  function formatDistance(kilometers) {
    const value = miles ? kilometers * 0.621371 : kilometers;
    if (value < 0.1) return `${Math.round(value * (miles ? 5280 : 1000))} ${miles ? 'ft' : 'm'}`;
    return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${miles ? 'mi' : 'km'}`;
  }
  function formatDuration(seconds) {
    const minutes = Math.max(1, Math.round(seconds / 60));
    const hours = Math.floor(minutes / 60);
    return hours ? `${hours} hr ${minutes % 60} min` : `${minutes} min`;
  }
  function status(text, error = false) {
    message.textContent = text;
    message.classList.toggle('error', error);
  }
  function setPanelOpen(open) {
    if (open && window.innerWidth >= 901 && !document.body.classList.contains('panel-collapsed')) {
      document.getElementById('panel-toggle')?.click();
      restoreLayersOnClose = true;
    }
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    toggle.classList.toggle('active', open);
    if (!open) {
      cancelPick();
      hideSuggestions('from');
      hideSuggestions('to');
      if (restoreLayersOnClose && document.body.classList.contains('panel-collapsed')) document.getElementById('panel-toggle')?.click();
      restoreLayersOnClose = false;
    } else {
      loadCatalog();
      fields.from.focus();
    }
  }
  function cancelPick() {
    picking = null;
    window.globalMapTripPicking = false;
    map.getCanvas().classList.remove('trip-picking');
    document.querySelectorAll('[data-trip-pick]').forEach(button => button.classList.remove('active'));
  }
  function startPick(kind) {
    document.getElementById('draw-close')?.click();
    if (picking === kind) { cancelPick(); return; }
    picking = kind;
    window.globalMapTripPicking = true;
    map.getCanvas().classList.add('trip-picking');
    document.querySelectorAll('[data-trip-pick]').forEach(button => button.classList.toggle('active', button.dataset.tripPick === kind));
    status(`Click the map to set ${kind === 'from' ? 'the start' : 'the destination'}.`);
    if (window.innerWidth < 901) panel.hidden = true;
  }
  function routeSource() { return map.getSource('gm-trip-route'); }
  function clearRoute() {
    request?.abort();
    request = null;
    submit.disabled = false;
    route = null;
    result.hidden = true;
    stepsList.replaceChildren();
    routeSource()?.setData(EMPTY);
  }
  function updateMarker(kind) {
    markers[kind]?.remove();
    markers[kind] = null;
    if (!points[kind]) return;
    const element = document.createElement('div');
    element.className = `trip-marker trip-marker-${kind}`;
    element.textContent = kind === 'from' ? 'A' : 'B';
    element.title = `${kind === 'from' ? 'Start' : 'Destination'}: ${points[kind].label}`;
    markers[kind] = new maplibregl.Marker({ element }).setLngLat([points[kind].lon, points[kind].lat]).addTo(map);
  }
  function setPoint(kind, point, label, pan = false) {
    points[kind] = { lat: point.lat, lon: point.lon, label: label || coordinateLabel(point) };
    fields[kind].value = points[kind].label;
    hideSuggestions(kind);
    clearRoute();
    updateMarker(kind);
    status(points.from && points.to ? 'Choose a travel mode, then show the route.' : `Choose ${kind === 'from' ? 'a destination' : 'a start point'}.`);
    if (pan) {
      window.globalMapStopRotation?.();
      map.flyTo({ center: [point.lon, point.lat], zoom: Math.max(map.getZoom(), 9), duration: 700, essential: true });
    }
  }
  function hideSuggestions(kind) {
    suggestionBoxes[kind].hidden = true;
    fields[kind].setAttribute('aria-expanded', 'false');
  }
  async function loadCatalog() {
    if (!catalogPromise) catalogPromise = fetch('/places.json').then(response => {
      if (!response.ok) throw new Error('Place catalog unavailable');
      return response.json();
    }).then(rows => {
      catalog = rows.map(row => ({ row, name: normalize(row[0]), country: normalize(row[1]), full: normalize(`${row[0]}, ${row[1]}`) }));
      for (const kind of ['from', 'to']) if (document.activeElement === fields[kind]) renderSuggestions(kind);
      return catalog;
    }).catch(() => {
      status('Place search is unavailable. You can still enter coordinates or pick on the map.', true);
      return [];
    });
    return catalogPromise;
  }
  function matchingPlaces(query) {
    const q = normalize(query);
    if (q.length < 2) return [];
    const matches = [];
    for (const item of catalog) {
      if (!item.name.includes(q) && !item.country.startsWith(q) && !item.full.startsWith(q)) continue;
      const rank = (item.full === q ? 150000000 : item.name === q ? 100000000 : item.name.startsWith(q) ? 50000000 : item.full.startsWith(q) ? 25000000 : item.country === q ? 1000000 : 0)
        + Math.min(Number(item.row[4]) || 0, 10000000);
      matches.push({ item, rank });
    }
    matches.sort((a, b) => b.rank - a.rank || a.item.row[0].localeCompare(b.item.row[0]));
    return matches.slice(0, 6).map(match => match.item.row);
  }
  function renderSuggestions(kind) {
    const box = suggestionBoxes[kind];
    box.replaceChildren();
    const value = fields[kind].value.trim();
    if (!value) { hideSuggestions(kind); return; }
    const coordinate = parseCoordinates(value);
    if (coordinate) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = `Use coordinates · ${coordinateLabel(coordinate)}`;
      button.addEventListener('click', () => setPoint(kind, coordinate, coordinateLabel(coordinate)));
      box.append(button);
    } else {
      for (const row of matchingPlaces(value)) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = `${row[0]}, ${row[1]}`;
        button.addEventListener('click', () => setPoint(kind, { lat: row[2], lon: row[3] }, button.textContent));
        box.append(button);
      }
      if (!box.childElementCount && catalog.length) {
        const note = document.createElement('span');
        note.textContent = 'No city or port found. Pick an exact point on the map.';
        box.append(note);
      }
    }
    box.hidden = !box.childElementCount;
    fields[kind].setAttribute('aria-expanded', String(!box.hidden));
  }
  async function resolveInput(kind) {
    if (points[kind]) return true;
    const value = fields[kind].value.trim();
    const coordinate = parseCoordinates(value);
    if (coordinate) { setPoint(kind, coordinate, coordinateLabel(coordinate)); return true; }
    await loadCatalog();
    const q = normalize(value);
    const row = matchingPlaces(value).find(candidate => normalize(candidate[0]) === q || normalize(`${candidate[0]}, ${candidate[1]}`) === q);
    if (!row) return false;
    setPoint(kind, { lat: row[2], lon: row[3] }, `${row[0]}, ${row[1]}`);
    return true;
  }
  function installRouteLayer() {
    if (routeSource()) { if (route) routeSource().setData({ type: 'Feature', properties: {}, geometry: route.geometry }); return; }
    map.addSource('gm-trip-route', { type: 'geojson', data: route
      ? { type: 'Feature', properties: {}, geometry: route.geometry } : EMPTY,
      attribution: 'Routing: <a href="https://valhalla.openstreetmap.de/">Valhalla</a> / <a href="https://project-osrm.org/">OSRM</a> / <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' });
    const before = map.getStyle().layers.find(layer => /^gm-.*-points$/.test(layer.id))?.id;
    map.addLayer({ id: 'gm-trip-route-casing', type: 'line', source: 'gm-trip-route',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#142c35', 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 5, 12, 10], 'line-opacity': 0.92 } }, before);
    map.addLayer({ id: 'gm-trip-route-line', type: 'line', source: 'gm-trip-route',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#78dfc8', 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 3, 12, 6], 'line-opacity': 0.98 } }, before);
  }
  function showRoute(data) {
    route = data;
    installRouteLayer();
    routeSource()?.setData({ type: 'Feature', properties: {}, geometry: data.geometry });
    document.getElementById('trip-duration').textContent = formatDuration(data.duration_seconds);
    document.getElementById('trip-distance').textContent = formatDistance(data.distance_km);
    stepsList.replaceChildren();
    for (const step of data.steps) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      const instruction = document.createElement('span'); instruction.textContent = step.instruction;
      const distance = document.createElement('small'); distance.textContent = formatDistance(step.distance_km);
      button.append(instruction, distance);
      button.addEventListener('click', () => {
        window.globalMapStopRotation?.();
        map.flyTo({ center: step.coordinate, zoom: Math.max(map.getZoom(), 13), duration: 700, essential: true });
      });
      item.append(button); stepsList.append(item);
    }
    result.hidden = false;
    status(`${mode.options[mode.selectedIndex].text} route ready · ${data.provider}.`);
    const coordinates = data.geometry.coordinates;
    const bounds = coordinates.reduce((value, point) => value.extend(point), new maplibregl.LngLatBounds(coordinates[0], coordinates[0]));
    window.globalMapStopRotation?.();
    try {
      map.fitBounds(bounds, { padding: window.innerWidth < 701
        ? { top: 75, right: 35, bottom: Math.min(window.innerHeight * 0.55, 390), left: 35 }
        : { top: 90, right: 380, bottom: 70, left: 55 },
        maxZoom: 14, duration: 900, essential: true });
    } catch (error) {
      console.warn('Route camera:', error);
      map.easeTo({ center: [(points.from.lon + points.to.lon) / 2, (points.from.lat + points.to.lat) / 2],
        zoom: Math.min(map.getZoom(), 4), duration: 700, essential: true });
    }
  }
  async function planRoute(event) {
    event.preventDefault();
    if (!await resolveInput('from')) { status('Choose a start from the city list, enter coordinates, or pick on the map.', true); fields.from.focus(); return; }
    if (!await resolveInput('to')) { status('Choose a destination from the city list, enter coordinates, or pick on the map.', true); fields.to.focus(); return; }
    clearRoute();
    const controller = new AbortController();
    request = controller;
    submit.disabled = true;
    status('Calculating route…');
    const origin = `${points.from.lat},${points.from.lon}`;
    const destination = `${points.to.lat},${points.to.lon}`;
    try {
      const response = await fetch(`/trip-route?from=${encodeURIComponent(origin)}&to=${encodeURIComponent(destination)}&mode=${mode.value}`, { signal: controller.signal, cache: 'no-store' });
      if (response.status === 404) throw new Error('No connected route was found. Try points on reachable roads or paths.');
      if (response.status === 422) throw new Error('This trip exceeds the public walking and cycling routing range. Try a shorter trip or switch to driving.');
      if (response.status === 429) throw new Error('The routing service is busy. Try again shortly.');
      if (!response.ok) throw new Error('Routing is unavailable right now. Try again shortly.');
      const data = await response.json();
      if (request !== controller) return;
      if (!Array.isArray(data.geometry?.coordinates) || data.geometry.coordinates.length < 2) throw new Error('The routing service returned no path.');
      showRoute(data);
    } catch (error) {
      if (!controller.signal.aborted) status(error.message || 'Routing is unavailable right now.', true);
    } finally {
      if (request === controller) { request = null; submit.disabled = false; }
    }
  }

  toggle.addEventListener('click', () => setPanelOpen(panel.hidden));
  document.getElementById('trip-close').addEventListener('click', () => setPanelOpen(false));
  document.getElementById('trip-clear').addEventListener('click', () => {
    cancelPick(); clearRoute();
    for (const kind of ['from', 'to']) { points[kind] = null; fields[kind].value = ''; hideSuggestions(kind); updateMarker(kind); }
    status('Choose two points or use ⌖ to pick them on the map.');
  });
  document.getElementById('trip-swap').addEventListener('click', () => {
    [points.from, points.to] = [points.to, points.from];
    [fields.from.value, fields.to.value] = [fields.to.value, fields.from.value];
    clearRoute(); updateMarker('from'); updateMarker('to');
    status(points.from && points.to ? 'Start and destination swapped. Show the route to recalculate.' : 'Start and destination swapped.');
  });
  mode.addEventListener('change', () => { clearRoute(); if (points.from && points.to) status('Show the route to recalculate for this travel mode.'); });
  document.querySelectorAll('[data-trip-pick]').forEach(button => button.addEventListener('click', () => startPick(button.dataset.tripPick)));
  for (const kind of ['from', 'to']) {
    fields[kind].addEventListener('focus', () => { loadCatalog(); renderSuggestions(kind); });
    fields[kind].addEventListener('input', () => {
      points[kind] = null; updateMarker(kind); clearRoute(); renderSuggestions(kind);
    });
    fields[kind].addEventListener('keydown', event => {
      if (event.key === 'Escape') { hideSuggestions(kind); cancelPick(); }
      if (event.key === 'Enter' && !event.shiftKey && suggestionBoxes[kind].querySelector('button')) {
        event.preventDefault(); suggestionBoxes[kind].querySelector('button').click();
        if (kind === 'from') fields.to.focus();
      }
    });
  }
  document.addEventListener('pointerdown', event => {
    for (const kind of ['from', 'to']) if (!fields[kind].closest('.trip-field').contains(event.target)) hideSuggestions(kind);
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && picking) cancelPick(); });
  map.on('click', event => {
    if (!picking) return;
    const kind = picking;
    const point = { lat: event.lngLat.lat, lon: event.lngLat.lng };
    cancelPick();
    panel.hidden = false;
    setPoint(kind, point, coordinateLabel(point));
  });
  map.on('style.load', installRouteLayer);
  if (map.isStyleLoaded()) installRouteLayer();
  form.addEventListener('submit', planRoute);
})();
