import test from 'node:test';
import assert from 'node:assert/strict';
import { fixture } from '../.test-dist/fixture.js';
import { boundaryFaces, colorForScalar, deformedPosition, escapeHtml, scalarRange, validateFixture } from '../.test-dist/mesh-data.js';

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

test('fixture validation rejects malformed vectors, elements, and metadata', () => {
  const shortVector = structuredClone(fixture);
  shortVector.nodes[0].positionMm = [0, 0];
  assert.ok(validateFixture(shortVector).some((issue) => issue.includes('coordinate')));

  const nonFiniteScale = structuredClone(fixture);
  nonFiniteScale.metadata.displacementScale = Number.POSITIVE_INFINITY;
  assert.ok(validateFixture(nonFiniteScale).some((issue) => issue.includes('Displacement scale')));

  const badLengthUnit = structuredClone(fixture);
  badLengthUnit.metadata.units.length = 'in';
  assert.ok(validateFixture(badLengthUnit).some((issue) => issue.includes('units')));

  const emptyMesh = structuredClone(fixture);
  emptyMesh.nodes = [];
  assert.ok(validateFixture(emptyMesh).some((issue) => issue.includes('at least one')));

  for (const nodeIds of [['N01', 'N02', 'ZZZ'], ['N01', 'N01', 'N03', 'N07']]) {
    const badElement = structuredClone(fixture);
    badElement.elements[0].nodeIds = nodeIds;
    const issues = validateFixture(badElement);
    assert.ok(issues.some((issue) => issue.includes('4 distinct')), `${nodeIds} must be rejected`);
    assert.ok(!issues.some((issue) => issue.includes('Unknown node')), 'arity check should not double-report');
  }
});

test('escapeHtml neutralizes markup injected through fixture strings', () => {
  assert.equal(escapeHtml('<img src=x onerror=alert(1)>'), '&#60;img src=x onerror=alert(1)&#62;');
  assert.equal(escapeHtml('"><script>'), '&#34;&#62;&#60;script&#62;');
  assert.equal(escapeHtml("a&b'c"), 'a&#38;b&#39;c');
  assert.equal(escapeHtml('N01'), 'N01');
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
