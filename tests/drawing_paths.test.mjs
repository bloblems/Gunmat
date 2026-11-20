import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../web/js/drawing-editor.js', import.meta.url), 'utf8');
const { splitPathData } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('relative subpaths keep their absolute positions after separation', () => {
  assert.deepEqual(splitPathData('M10 20l5 6m2 3l4 5'), ['M 10 20 l 5 6', 'M 17 29 l 4 5']);
});

test('closed contours reset the relative starting point', () => {
  assert.deepEqual(splitPathData('M10 20h5v6z m2 3l4 5'), ['M 10 20 h 5 v 6 z', 'M 12 23 l 4 5']);
});

test('implicit lines, curves, arcs, and exponent notation keep their endpoints', () => {
  assert.deepEqual(
    splitPathData('m1e1 20 5 6c0 0 1 1 2 3s1 1 2 3q1 1 2 3t2 3a1 1 0 0 1 2 3m1 1h2'),
    ['M 10 20 l 5 6 c 0 0 1 1 2 3 s 1 1 2 3 q 1 1 2 3 t 2 3 a 1 1 0 0 1 2 3', 'M 26 42 h 2'],
  );
});

test('absolute contours remain separate and invalid data is rejected', () => {
  assert.deepEqual(splitPathData('M0,0L10,10M20,20L30,30'), ['M 0 0 L 10 10', 'M 20 20 L 30 30']);
  assert.throws(() => splitPathData('M0 0L10'));
  assert.throws(() => splitPathData('M0 0R10 10'));
});
