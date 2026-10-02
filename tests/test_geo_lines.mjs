import assert from 'node:assert/strict';
import test from 'node:test';
import { boundsAroundLongitude, splitLineAtAntimeridian } from '../geo-lines.mjs';

test('cyclone guidance bounds stay together across the date line', () => {
  const bounds = boundsAroundLongitude([[144.6, 17.7], [179.8, 55.4], [-174.7, 48.2]], 144.6);
  assert.deepEqual([bounds[0][0], bounds[0][1], bounds[1][1]], [144.6, 17.7, 55.4]);
  assert.ok(Math.abs(bounds[1][0] - 185.3) < 1e-9);
});

test('westward storm tracks connect at the date line', () => {
  const segments = splitLineAtAntimeridian([[-178.6, 24.3], [178.3, 25.4], [175.4, 26.4]]);
  assert.equal(segments.length, 2);
  assert.equal(segments[0].at(-1)[0], -180);
  assert.equal(segments[1][0][0], 180);
  assert.equal(segments[0].at(-1)[1], segments[1][0][1]);
  assert.equal(segments[1].at(-1)[0], 175.4);
});

test('eastward crossings and ordinary tracks retain their endpoints', () => {
  const crossing = splitLineAtAntimeridian([[179, 10], [-179, 12]]);
  assert.equal(crossing.length, 2);
  assert.deepEqual(crossing[0].at(-1), [180, 11]);
  assert.deepEqual(crossing[1][0], [-180, 11]);
  assert.deepEqual(splitLineAtAntimeridian([[-80, 20], [-79, 21]]), [[[-80, 20], [-79, 21]]]);
});
