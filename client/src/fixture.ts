export type NodeRecord = {
  id: string;
  positionMm: readonly [number, number, number];
  displacementMm: readonly [number, number, number];
  vonMisesMpa: number;
};

export type ElementRecord = {
  id: string;
  nodeIds: readonly [string, string, string, string];
  vonMisesMpa: number;
};

export type LocalFixture = {
  metadata: {
    kind: 'synthetic-demo';
    synthetic: true;
    source: 'bundled-local-fixture';
    validationStatus: 'not-validated';
    displayName: string;
    displacementScale: number;
    units: { length: 'mm'; displacement: 'mm'; stress: 'MPa' };
  };
  nodes: readonly NodeRecord[];
  elements: readonly ElementRecord[];
};

// Invented values for UI demonstration only. This fixture is not solver output.
export const fixture: LocalFixture = {
  metadata: {
    kind: 'synthetic-demo',
    synthetic: true,
    source: 'bundled-local-fixture',
    validationStatus: 'not-validated',
    displayName: 'Viga demonstrativa',
    displacementScale: 30,
    units: { length: 'mm', displacement: 'mm', stress: 'MPa' },
  },
  nodes: [
    { id: 'N01', positionMm: [0, 0, 0], displacementMm: [0, 0, 0], vonMisesMpa: 18 },
    { id: 'N02', positionMm: [100, 0, 0], displacementMm: [0, 0, -0.18], vonMisesMpa: 32 },
    { id: 'N03', positionMm: [100, 100, 0], displacementMm: [0, 0, -0.34], vonMisesMpa: 51 },
    { id: 'N04', positionMm: [0, 100, 0], displacementMm: [0, 0, -0.08], vonMisesMpa: 24 },
    { id: 'N05', positionMm: [0, 0, 40], displacementMm: [0, 0, 0], vonMisesMpa: 16 },
    { id: 'N06', positionMm: [100, 0, 40], displacementMm: [0, 0, -0.12], vonMisesMpa: 45 },
    { id: 'N07', positionMm: [100, 100, 40], displacementMm: [0, 0, -0.28], vonMisesMpa: 82 },
    { id: 'N08', positionMm: [0, 100, 40], displacementMm: [0, 0, -0.05], vonMisesMpa: 27 },
  ],
  elements: [
    { id: 'E01', nodeIds: ['N01', 'N02', 'N03', 'N07'], vonMisesMpa: 45.75 },
    { id: 'E02', nodeIds: ['N01', 'N03', 'N04', 'N07'], vonMisesMpa: 43.75 },
    { id: 'E03', nodeIds: ['N01', 'N04', 'N08', 'N07'], vonMisesMpa: 37.75 },
    { id: 'E04', nodeIds: ['N01', 'N08', 'N05', 'N07'], vonMisesMpa: 35.75 },
    { id: 'E05', nodeIds: ['N01', 'N05', 'N06', 'N07'], vonMisesMpa: 40.25 },
    { id: 'E06', nodeIds: ['N01', 'N06', 'N02', 'N07'], vonMisesMpa: 44.25 },
  ],
};
