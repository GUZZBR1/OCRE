import test from 'node:test';
import assert from 'node:assert/strict';
import { fixture } from '../.test-dist/fixture.js';
import { boundaryFaces, colorForScalar, deformedPosition, scalarRange, validateFixture } from '../.test-dist/mesh-data.js';

test('fixture is explicitly local, synthetic, and not validated', () => {
  assert.deepEqual(validateFixture(fixture), []);
  assert.equal(fixture.metadata.synthetic, true);
  assert.equal(fixture.metadata.source, 'bundled-local-fixture');
  assert.equal(fixture.metadata.validationStatus, 'not-validated');
});

test('element references and displayed units agree with the fixture', () => {
  const knownNodes = new Set(fixture.nodes.map(({ id }) => id));
  assert.ok(fixture.elements.every(({ nodeIds }) => nodeIds.every((id) => knownNodes.has(id))));
  assert.equal(fixture.metadata.units.stress, 'MPa');
  assert.equal(fixture.metadata.units.displacement, 'mm');
  assert.equal(fixture.nodes.find(({ id }) => id === 'N07')?.vonMisesMpa, 82);
  assert.equal(fixture.elements.find(({ id }) => id === 'E03')?.vonMisesMpa, 37.75);
  for (const element of fixture.elements) {
    const nodalAverage = element.nodeIds.reduce((sum, id) => sum + fixture.nodes.find((node) => node.id === id).vonMisesMpa, 0) / element.nodeIds.length;
    assert.equal(element.vonMisesMpa, nodalAverage, `${element.id} agrees with its nodal field average`);
  }
});

test('fixture validation rejects an unknown node and an unmarked source', () => {
  const invalid = structuredClone(fixture);
  invalid.metadata.source = 'remote';
  invalid.elements[0].nodeIds[0] = 'missing';
  assert.ok(validateFixture(invalid).some((issue) => issue.includes('source')));
  assert.ok(validateFixture(invalid).some((issue) => issue.includes('Unknown node')));
});

test('displacement switch changes coordinates only when enabled', () => {
  const node = fixture.nodes.find(({ id }) => id === 'N07');
  assert.deepEqual(deformedPosition(node, false, 1), [100, 100, 40]);
  assert.equal(fixture.metadata.displacementScale, 30);
  assert.deepEqual(deformedPosition(node, true, fixture.metadata.displacementScale), [100, 100, 31.6]);
});

test('tetra mesh boundary has 12 exterior triangles and scalar range matches legend', () => {
  assert.equal(boundaryFaces(fixture.elements).length, 12);
  assert.deepEqual(scalarRange(fixture.nodes.map(({ vonMisesMpa }) => vonMisesMpa)), [16, 82]);
  assert.deepEqual(colorForScalar(16, 16, 82), [0.12, 0.35, 0.86]);
  assert.deepEqual(colorForScalar(82, 16, 82), [0.91, 0.28, 0.22]);
});
