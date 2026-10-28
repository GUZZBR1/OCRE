import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { fixture } from './fixture.js';
import { boundaryFaces, colorForScalar, deformedPosition, scalarRange, validateFixture } from './mesh-data.js';
import './style.css';

const issues = validateFixture(fixture);
if (issues.length) throw new Error(`Fixture local inválida: ${issues.join(' ')}`);

const viewport = document.querySelector<HTMLDivElement>('#viewport')!;
const rendererError = document.querySelector<HTMLDivElement>('#renderer-error')!;
const fieldSelect = document.querySelector<HTMLSelectElement>('#field-select')!;
const deformationToggle = document.querySelector<HTMLInputElement>('#deformation-toggle')!;
const selectNodeButton = document.querySelector<HTMLButtonElement>('#select-node')!;
const selectElementButton = document.querySelector<HTMLButtonElement>('#select-element')!;
const selectionEmpty = document.querySelector<HTMLDivElement>('#selection-empty')!;
const selectionDetail = document.querySelector<HTMLDivElement>('#selection-detail')!;
const [stressMin, stressMax] = scalarRange(fixture.nodes.map((node) => node.vonMisesMpa));
const surfaceFaces = boundaryFaces(fixture.elements);
const faceElementIds = surfaceFaces.map((face) => face.elementId);
let selectionMode: 'node' | 'element' = 'node';
let selectedNodeIndex: number | null = null;
let showStress = true;
let showMesh = true;
let showDeformation = false;
let disposed = false;

const scene = new THREE.Scene();
scene.background = new THREE.Color('#0d1722');
const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 2000);
camera.position.set(170, -230, 170);
let renderer: THREE.WebGLRenderer | null = null;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
} catch (error) {
  rendererError.hidden = false;
  rendererError.textContent = 'Este navegador ou dispositivo não disponibiliza WebGL2.';
  console.error(error);
}
if (renderer) {
const activeRenderer = renderer;
activeRenderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
activeRenderer.outputColorSpace = THREE.SRGBColorSpace;
activeRenderer.domElement.setAttribute('aria-label', 'Malha tridimensional. Use o mouse para orbitar e selecionar.');
viewport.prepend(activeRenderer.domElement);
const controls = new OrbitControls(camera, activeRenderer.domElement);
controls.target.set(50, 50, 20);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 90;
controls.maxDistance = 600;
scene.add(new THREE.HemisphereLight('#dce9ff', '#374456', 2.2));
const keyLight = new THREE.DirectionalLight('#ffffff', 2.6);
keyLight.position.set(-100, -150, 260);
scene.add(keyLight);
const grid = new THREE.GridHelper(180, 9, '#43536a', '#263546');
grid.position.set(50, 50, -5);
scene.add(grid);

const positions = new Float32Array(surfaceFaces.length * 9);
const colors = new Float32Array(surfaceFaces.length * 9);
const geometry = new THREE.BufferGeometry();
geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
const material = new THREE.MeshPhongMaterial({ vertexColors: true, side: THREE.DoubleSide, shininess: 30 });
const surface = new THREE.Mesh(geometry, material);
scene.add(surface);
const meshLines = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: '#c5d3e4', transparent: true, opacity: 0.52 }));
scene.add(meshLines);
const nodePositions = new Float32Array(fixture.nodes.length * 3);
const nodeGeometry = new THREE.BufferGeometry();
nodeGeometry.setAttribute('position', new THREE.BufferAttribute(nodePositions, 3));
const nodePoints = new THREE.Points(nodeGeometry, new THREE.PointsMaterial({ color: '#f2f6ff', size: 8, sizeAttenuation: false, transparent: true, opacity: 0.88 }));
nodePoints.visible = false;
scene.add(nodePoints);
const selectionMarker = new THREE.Mesh(new THREE.SphereGeometry(2.8, 16, 12), new THREE.MeshBasicMaterial({ color: '#ffffff', wireframe: true }));
selectionMarker.visible = false;
scene.add(selectionMarker);

function currentPosition(nodeId: string): [number, number, number] {
  const node = fixture.nodes.find((entry) => entry.id === nodeId)!;
  return deformedPosition(node, showDeformation, fixture.metadata.displacementScale);
}

function updateGeometry(): void {
  let cursor = 0;
  const linePositions: number[] = [];
  for (const face of surfaceFaces) {
    const faceNodes = face.nodeIds.map((id) => fixture.nodes.find((node) => node.id === id)!);
    for (const node of faceNodes) {
      const position = currentPosition(node.id);
      positions.set(position, cursor);
      const scalar = showStress ? node.vonMisesMpa : 0;
      const [red, green, blue] = showStress ? colorForScalar(scalar, stressMin, stressMax) : [0.39, 0.48, 0.59];
      colors.set([red, green, blue], cursor);
      cursor += 3;
    }
    for (let edge = 0; edge < 3; edge += 1) {
      const from = currentPosition(face.nodeIds[edge]!);
      const to = currentPosition(face.nodeIds[(edge + 1) % 3]!);
      linePositions.push(...from, ...to);
    }
  }
  geometry.attributes.position.needsUpdate = true;
  geometry.attributes.color.needsUpdate = true;
  geometry.computeVertexNormals();
  geometry.computeBoundingSphere();
  meshLines.geometry.dispose();
  meshLines.geometry = new THREE.BufferGeometry();
  meshLines.geometry.setAttribute('position', new THREE.Float32BufferAttribute(linePositions, 3));
  meshLines.geometry.computeBoundingSphere();
  meshLines.visible = showMesh;
  nodePositions.set(fixture.nodes.flatMap((node) => currentPosition(node.id)));
  nodeGeometry.attributes.position.needsUpdate = true;
  nodeGeometry.computeBoundingSphere();
  nodePoints.visible = selectionMode === 'node';
  updateSelectionMarker();
}

function updateSelectionMarker(): void {
  if (selectionMode === 'node' && selectedNodeIndex !== null) {
    selectionMarker.position.fromArray(currentPosition(fixture.nodes[selectedNodeIndex]!.id));
    selectionMarker.visible = true;
  } else selectionMarker.visible = false;
}

function formatValue(value: number): string {
  return new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 2 }).format(value);
}

function showSelection(kind: 'node' | 'element', id: string): void {
  selectionEmpty.hidden = true;
  selectionDetail.hidden = false;
  if (kind === 'node') {
    const node = fixture.nodes.find((entry) => entry.id === id)!;
    selectionDetail.innerHTML = `<div class="selected-id"><span>NÓ</span><strong>${node.id}</strong></div><dl><div><dt>Tensão equivalente</dt><dd>${formatValue(node.vonMisesMpa)} <span>${fixture.metadata.units.stress}</span></dd></div><div><dt>Deslocamento Z base</dt><dd>${formatValue(node.displacementMm[2])} <span>${fixture.metadata.units.displacement}</span></dd></div><div><dt>Posição Z base</dt><dd>${formatValue(node.positionMm[2])} <span>${fixture.metadata.units.length}</span></dd></div></dl>`;
  } else {
    const element = fixture.elements.find((entry) => entry.id === id)!;
    selectionDetail.innerHTML = `<div class="selected-id"><span>ELEMENTO TETRAÉDRICO</span><strong>${element.id}</strong></div><dl><div><dt>Tensão equivalente</dt><dd>${formatValue(element.vonMisesMpa)} <span>${fixture.metadata.units.stress}</span></dd></div><div><dt>Nós associados</dt><dd>${element.nodeIds.join(' · ')}</dd></div></dl>`;
  }
}

function setSelectionMode(mode: 'node' | 'element'): void {
  selectionMode = mode;
  selectNodeButton.setAttribute('aria-pressed', String(mode === 'node'));
  selectElementButton.setAttribute('aria-pressed', String(mode === 'element'));
  nodePoints.visible = mode === 'node';
  if (mode === 'element') selectedNodeIndex = null;
  selectionEmpty.hidden = false;
  selectionDetail.hidden = true;
  updateSelectionMarker();
}

const raycaster = new THREE.Raycaster();
raycaster.params.Points!.threshold = 6;
const pointer = new THREE.Vector2();
activeRenderer.domElement.addEventListener('pointerdown', (event) => {
  const bounds = activeRenderer.domElement.getBoundingClientRect();
  pointer.set(((event.clientX - bounds.left) / bounds.width) * 2 - 1, -((event.clientY - bounds.top) / bounds.height) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  if (selectionMode === 'node') {
    const hit = raycaster.intersectObject(nodePoints)[0];
    if (!hit || hit.index === undefined) return;
    const nodeIndex = hit.index;
    selectedNodeIndex = nodeIndex;
    showSelection('node', fixture.nodes[nodeIndex]!.id);
    updateSelectionMarker();
  } else {
    const hit = raycaster.intersectObject(surface)[0];
    if (!hit || hit.faceIndex == null) return;
    const elementId = faceElementIds[hit.faceIndex];
    if (!elementId) return;
    selectedNodeIndex = null;
    showSelection('element', elementId);
    updateSelectionMarker();
  }
});

selectNodeButton.addEventListener('click', () => setSelectionMode('node'));
selectElementButton.addEventListener('click', () => setSelectionMode('element'));
fieldSelect.addEventListener('change', () => { showStress = fieldSelect.value === 'stress'; updateGeometry(); });
deformationToggle.addEventListener('change', () => { showDeformation = deformationToggle.checked; updateGeometry(); });
document.querySelector<HTMLInputElement>('#mesh-toggle')!.addEventListener('change', (event) => { showMesh = (event.currentTarget as HTMLInputElement).checked; meshLines.visible = showMesh; });
document.querySelector<HTMLInputElement>('#result-toggle')!.addEventListener('change', (event) => { showStress = (event.currentTarget as HTMLInputElement).checked; updateGeometry(); });
document.querySelector<HTMLButtonElement>('#reset-view')!.addEventListener('click', () => {
  camera.position.set(170, -230, 170);
  controls.target.set(50, 50, 20);
  controls.update();
});

document.querySelector('#legend-min')!.textContent = formatValue(stressMin);
document.querySelector('#legend-max')!.textContent = formatValue(stressMax);
document.querySelector('#legend-unit')!.textContent = fixture.metadata.units.stress;
document.querySelector('#scale-label')!.textContent = `${formatValue(fixture.metadata.displacementScale)}×`;
document.querySelector('#mesh-count')!.textContent = `${fixture.nodes.length} nós · ${fixture.elements.length} elementos`;
document.querySelector('#fixture-id')!.textContent = `${fixture.metadata.source.toUpperCase()} · NÃO VALIDADA`;

function resize(): void {
  const width = viewport.clientWidth;
  const height = viewport.clientHeight;
  if (!width || !height || disposed) return;
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  activeRenderer.setSize(width, height, false);
}
const resizeObserver = new ResizeObserver(resize);
resizeObserver.observe(viewport);
window.addEventListener('beforeunload', () => {
  disposed = true;
  resizeObserver.disconnect();
  controls.dispose();
  geometry.dispose();
  meshLines.geometry.dispose();
  activeRenderer.dispose();
});
function render(): void {
  if (disposed) return;
  controls.update();
  activeRenderer.render(scene, camera);
  requestAnimationFrame(render);
}

try {
  updateGeometry();
  resize();
  render();
} catch (error) {
  rendererError.hidden = false;
  rendererError.textContent = 'Não foi possível iniciar a visualização WebGL2 neste dispositivo.';
  console.error(error);
}
}
