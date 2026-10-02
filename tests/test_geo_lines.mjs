import assert from 'node:assert/strict';
import test from 'node:test';
import { splitLineAtAntimeridian } from '../geo-lines.mjs';

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
