// Animated, geolocated FortiGuard detections. Canvas stays above MapLibre and
// never intercepts map gestures; only the currently visible globe is painted.
export function createCyberTrails(map, canvas, onCount, onError) {
  const context = canvas.getContext('2d');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const seen = new Map();
  const waiting = [];
  const active = [];
  let enabled = false;
  let timer = 0;
  let frame = 0;
  let controller = null;
  let lastSpawn = 0;

  function vector([lon, lat]) {
    const latitude = lat * Math.PI / 180;
    const longitude = lon * Math.PI / 180;
    return [Math.cos(latitude) * Math.cos(longitude), Math.cos(latitude) * Math.sin(longitude), Math.sin(latitude)];
  }

  function pathBetween(src, dest) {
    const a = vector(src), b = vector(dest);
    const cosine = Math.max(-1, Math.min(1, a.reduce((sum, value, index) => sum + value * b[index], 0)));
    const angle = Math.acos(cosine);
    const points = [];
    for (let index = 0; index <= 32; index++) {
      const fraction = index / 32;
      const mix = Math.sin(angle) > .0001
        ? [Math.sin((1 - fraction) * angle) / Math.sin(angle), Math.sin(fraction * angle) / Math.sin(angle)]
        : [1 - fraction, fraction];
      const xyz = a.map((value, axis) => value * mix[0] + b[axis] * mix[1]);
      const length = Math.hypot(...xyz) || 1;
      points.push({ coordinate: [Math.atan2(xyz[1], xyz[0]) * 180 / Math.PI,
        Math.asin(xyz[2] / length) * 180 / Math.PI], vector: xyz.map(value => value / length) });
    }
    return points;
  }

  function sizeCanvas() {
    const ratio = Math.min(devicePixelRatio || 1, 2);
    const width = map.getCanvas().clientWidth;
    const height = map.getCanvas().clientHeight;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return [width, height];
  }

  function paint(now) {
    frame = 0;
    const [width, height] = sizeCanvas();
    context.clearRect(0, 0, width, height);
    if (!enabled || document.hidden) return;
    if (!reducedMotion && now - lastSpawn > 380 && waiting.length && active.length < 18) {
      const item = waiting.shift();
      active.push({ points: pathBetween(item.src, item.dest), started: now,
        color: item.severity === 'Critical' ? [255, 123, 115] : [255, 178, 109] });
      lastSpawn = now;
    }
    const globe = map.getProjection()?.type === 'globe';
    const center = globe ? vector([map.getCenter().lng, map.getCenter().lat]) : null;
    for (let index = active.length - 1; index >= 0; index--) {
      const trail = active[index];
      const progress = reducedMotion ? 1 : (now - trail.started) / 3400;
      if (progress > 1.3) { active.splice(index, 1); continue; }
      const head = Math.min(1, progress);
      const tail = reducedMotion ? 0 : Math.max(0, head - .27);
      const visible = point => !globe || center.reduce((sum, value, axis) => sum + value * point.vector[axis], 0) > .015;
      const project = point => map.project(point.coordinate);
      for (let segment = Math.max(1, Math.floor(tail * 32)); segment <= Math.ceil(head * 32); segment++) {
        const first = trail.points[segment - 1], second = trail.points[segment];
        if (!first || !second || !visible(first) || !visible(second)) continue;
        const a = project(first), b = project(second);
        if (Math.abs(a.x - b.x) > width / 2 || Math.abs(a.y - b.y) > height / 2) continue;
        if (Math.max(a.x, b.x) < -20 || Math.min(a.x, b.x) > width + 20 ||
            Math.max(a.y, b.y) < -20 || Math.min(a.y, b.y) > height + 20) continue;
        const strength = reducedMotion ? .3 : .12 + .75 * ((segment / 32 - tail) / Math.max(.01, head - tail));
        context.strokeStyle = `rgba(${trail.color.join(',')},${Math.min(.85, strength)})`;
        context.lineWidth = reducedMotion ? 1 : 1 + 2.3 * strength;
        context.shadowColor = `rgb(${trail.color.join(',')})`;
        context.shadowBlur = reducedMotion ? 0 : 10;
        context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y); context.stroke();
      }
      const tip = trail.points[Math.min(32, Math.floor(head * 32))];
      if (!reducedMotion && tip && visible(tip) && progress <= 1) {
        const point = project(tip);
        context.shadowColor = '#fff3d7'; context.shadowBlur = 16;
        context.fillStyle = '#fff5d7';
        context.beginPath(); context.arc(point.x, point.y, 2.8, 0, Math.PI * 2); context.fill();
      }
    }
    context.shadowBlur = 0;
    if (!reducedMotion && (waiting.length || active.length)) frame = requestAnimationFrame(paint);
  }

  function requestPaint() { if (!frame && enabled && !document.hidden) frame = requestAnimationFrame(paint); }

  async function refresh() {
    if (!enabled || document.hidden || controller) return;
    controller = new AbortController();
    try {
      const response = await fetch('/cyber?feed=attacks', { signal: controller.signal });
      if (!response.ok) throw new Error(`FortiGuard: ${response.status}`);
      const data = await response.json();
      if (!enabled) return;
      const now = Date.now();
      for (const [id, observed] of seen) if (now - observed > 180000) seen.delete(id);
      const items = Array.isArray(data.items)
        ? data.items.filter(item => Number.isFinite(Number(item.observed)) &&
          now - Number(item.observed) >= -10000 && now - Number(item.observed) <= 150000)
        : [];
      onCount(items.length);
      for (const item of items.slice().reverse()) {
        if (!item?.id || seen.has(item.id) || !Array.isArray(item.src) || !Array.isArray(item.dest)) continue;
        seen.set(item.id, now);
        waiting.push(item);
      }
      if (waiting.length > 90) waiting.splice(0, waiting.length - 90);
      if (reducedMotion) {
        active.splice(0, active.length, ...items.slice(0, 12).map(item => ({
          points: pathBetween(item.src, item.dest), started: now, color: [255, 178, 109] })));
      }
      requestPaint();
    } catch (error) {
      if (error.name !== 'AbortError') onError(error);
    } finally { controller = null; }
  }

  map.on('move', requestPaint);
  window.addEventListener('resize', requestPaint);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); requestPaint(); } });
  return { setEnabled(value) {
    if (value === enabled) return;
    enabled = value;
    canvas.hidden = !value;
    if (value) { refresh(); timer = setInterval(refresh, 60000); requestPaint(); }
    else {
      clearInterval(timer); controller?.abort(); waiting.length = 0; active.length = 0;
      cancelAnimationFrame(frame); frame = 0; context.clearRect(0, 0, canvas.width, canvas.height);
      onCount(null);
    }
  } };
}
