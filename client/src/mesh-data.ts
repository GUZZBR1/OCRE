import type { ElementRecord, LocalFixture, NodeRecord } from './fixture.js';

export type FaceRecord = { nodeIds: readonly [string, string, string]; elementId: string };

export function validateFixture(data: LocalFixture): string[] {
  const issues: string[] = [];
  if (data.metadata.kind !== 'synthetic-demo' || data.metadata.synthetic !== true) issues.push('Fixture must be explicitly synthetic.');
  if (data.metadata.source !== 'bundled-local-fixture') issues.push('Fixture source must be local.');
  if (data.metadata.validationStatus !== 'not-validated') issues.push('Demo data cannot claim validation.');
  if (!Number.isFinite(data.metadata.displacementScale)) issues.push('Displacement scale must be finite.');
  if (data.metadata.units.stress !== 'MPa' || data.metadata.units.displacement !== 'mm' || data.metadata.units.length !== 'mm') issues.push('Unsupported fixture units.');
  const nodes: readonly NodeRecord[] = Array.isArray(data.nodes) ? data.nodes : [];
  const elements: readonly ElementRecord[] = Array.isArray(data.elements) ? data.elements : [];
  if (!nodes.length || !elements.length) issues.push('Fixture must contain at least one node and one element.');
  const nodeIds = new Set(nodes.map((node) => node.id));
  if (nodeIds.size !== nodes.length) issues.push('Node IDs must be unique.');
  const elementIds = new Set(elements.map((element) => element.id));
  if (elementIds.size !== elements.length) issues.push('Element IDs must be unique.');
  for (const node of nodes) {
    if (!Number.isFinite(node.vonMisesMpa) || node.vonMisesMpa < 0) issues.push(`Invalid stress on ${node.id}.`);
    if (node.positionMm?.length !== 3 || node.displacementMm?.length !== 3 || ![...node.positionMm, ...node.displacementMm].every(Number.isFinite)) issues.push(`Invalid coordinate on ${node.id}.`);
  }
  for (const element of elements) {
    if (!Number.isFinite(element.vonMisesMpa) || element.vonMisesMpa < 0) issues.push(`Invalid stress on ${element.id}.`);
    if (element.nodeIds?.length !== 4 || new Set(element.nodeIds).size !== 4) issues.push(`Element ${element.id} must reference 4 distinct nodes.`);
    else if (element.nodeIds.some((nodeId) => !nodeIds.has(nodeId))) issues.push(`Unknown node reference in ${element.id}.`);
  }
  return issues;
}

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
}

export function deformedPosition(node: NodeRecord, enabled: boolean, scale: number): [number, number, number] {
  const factor = enabled ? scale : 0;
  return node.positionMm.map((value, index) => value + node.displacementMm[index]! * factor) as [number, number, number];
}

export function boundaryFaces(elements: readonly ElementRecord[]): FaceRecord[] {
  const faces = new Map<string, { nodeIds: [string, string, string]; elementId: string; count: number }>();
  for (const element of elements) {
    const [a, b, c, d] = element.nodeIds;
    const elementFaces: [string, string, string][] = [[a, b, c], [a, d, b], [a, c, d], [b, d, c]];
    for (const nodeIds of elementFaces) {
      const key = [...nodeIds].sort().join('|');
      const current = faces.get(key);
      if (current) current.count += 1;
      else faces.set(key, { nodeIds, elementId: element.id, count: 1 });
    }
  }
  return [...faces.values()].filter((face) => face.count === 1).map(({ nodeIds, elementId }) => ({ nodeIds, elementId }));
}

export function scalarRange(values: readonly number[]): [number, number] {
  return [Math.min(...values), Math.max(...values)];
}

export function colorForScalar(value: number, min: number, max: number): [number, number, number] {
  const stops: [number, [number, number, number]][] = [
    [0, [0.12, 0.35, 0.86]],
    [0.34, [0.08, 0.78, 0.79]],
    [0.67, [0.96, 0.76, 0.22]],
    [1, [0.91, 0.28, 0.22]],
  ];
  const t = max === min ? 0 : Math.max(0, Math.min(1, (value - min) / (max - min)));
  const upperIndex = stops.findIndex(([stop]) => stop >= t);
  if (upperIndex <= 0) return stops[0]![1];
  const [leftT, left] = stops[upperIndex - 1]!;
  const [rightT, right] = stops[upperIndex]!;
  const blend = (t - leftT) / (rightT - leftT);
  return left.map((channel, index) => channel + (right[index]! - channel) * blend) as [number, number, number];
}
