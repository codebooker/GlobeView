// Preserve continuity when a line crosses the 180° meridian. MapLibre needs
// separate segments on either side instead of a 360° jump across the map.
export function splitLineAtAntimeridian(points) {
  const segments = [];
  let segment = [];
  for (const point of points) {
    const lon = Number(point?.[0]);
    const lat = Number(point?.[1]);
    if (!Number.isFinite(lon) || !Number.isFinite(lat) || Math.abs(lon) > 180 || Math.abs(lat) > 90) continue;
    const previous = segment.at(-1);
    if (previous && Math.abs(lon - previous[0]) > 180) {
      const boundary = lon < previous[0] ? 180 : -180;
      const unwrappedLon = lon + (boundary === 180 ? 360 : -360);
      const fraction = (boundary - previous[0]) / (unwrappedLon - previous[0]);
      const crossingLat = previous[1] + (lat - previous[1]) * fraction;
      segment.push([boundary, crossingLat]);
      if (segment.length > 1) segments.push(segment);
      segment = [[-boundary, crossingLat]];
    }
    segment.push([lon, lat]);
  }
  if (segment.length > 1) segments.push(segment);
  return segments;
}

// Keep a storm's forecast bounds together when tracks cross the date line.
export function boundsAroundLongitude(points, anchor) {
  const lons = points.map(point => anchor + ((((point[0] - anchor) + 540) % 360) - 180));
  const lats = points.map(point => point[1]);
  return [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]];
}
