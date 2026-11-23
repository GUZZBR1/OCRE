"""Exportação STEP de documentos ``draw.paper.v1`` (beta v0.1, F1/F4).

Host OCRE (só stdlib): valida o documento, traduz traços fechados
``stable`` em perfis 2D e delega a geometria ao worker build123d em
subprocesso (``step_worker.py`` no venv próprio ``.venv-beta``).
NUNCA importa build123d/OCP neste processo — o worker 3D pinado pelo
Senado é o build123d e o CadQuery é plano B: os dois NUNCA podem ser
instalados no mesmo venv (conflito ``OCP/``: ``cadquery-ocp`` e
``cadquery-ocp-novtk`` instalam o mesmo pacote de topo; o último
instalado vence e quebra o outro).

Regras (fichas ``cad.md`` / ``formatos.md`` + decisão do Senado):

- Traço fechado ``stable`` -> perfil 2D (polilinha fechada, em mm,
  Y já no sentido CAD: ``y_cad = altura_efetiva_mm - y_papel`` — ou
  circunferência analítica quando ``shape="circle"`` traz o bloco
  ``circle`` ou os pontos ajustam um círculo) -> ``extrude(thickness)``
  -> sólido STEP. Contornos aninhados viram furos/ilhas (par-ímpar).
- Espessura e material são DECLARADOS por humano (argumento) — nunca
  inventados. Espessura ausente/``None``/<=0 ou material vazio =
  recusa fail-closed.
- Traço aberto ``stable``: ``open_strokes_policy="fail"`` (padrão)
  recusa fail-closed — nunca vira sólido. A política opt-in
  ``"skip-open"`` registra o traço em ``skipped`` e segue sem ele;
  documento sem nenhum traço fechado ``stable`` recusa nas duas
  políticas.
- ``erased`` nunca entra; ``pending`` é pulado e registrado no
  relatório (mesma regra do escritor DXF F0).
- O documento ``draw.paper.v1`` continua sendo o mestre: o STEP é só
  saída (round-trip dá no máximo projeções de arestas, sem id/order/
  status). O fingerprint do documento é ``DRAW:PAPER-APPROVAL:FINGERPRINT``
  (errata do Senado — nunca o rótulo antigo ``DRAW-AF/1``).
- "Aprovado" aqui = selecionado para o beta provisório, NÃO provado
  contra conjunto selado — os gaps ficam registrados no relatório.

Oráculo FreeCAD: ``freecadcmd`` NÃO existe via apt nesta máquina
(sem candidato). O oráculo procura, nesta ordem: ``freecadcmd`` no
PATH, AppImage oficial em ``.external/FreeCAD.AppImage`` (worktree)
ou no caminho dado por ``FREECAD_APPIMAGE``. Se não houver nenhum,
``oracle_step`` devolve ``status == "unavailable"`` — skip honesto,
nunca passo fingido. O oráculo, quando existe, roda via subprocesso
e lê um JSON (nunca confia no exit code, como manda a ficha).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .paper import paper_size_mm
from .document import document_sha256

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "OPEN_STROKES_POLICIES",
    "ORACLE_UNAVAILABLE",
    "StepExportError",
    "document_to_step",
    "export_dxf_ac1015",
    "find_step_worker_python",
    "load_step_material_manifest",
    "oracle_step",
    "validate_step",
    "worker_versions",
]

DEFAULT_TIMEOUT_S = 300
# Volume esperado (área líquida do perfil x espessura) vs volume do
# sólido: tolerância relativa do teste de sanidade do host (OCCT
# arredonda em ~1e-7 relativo; 0,1% é folga ampla sem esconder erro
# de geometria).
VOLUME_TOLERANCE_REL = 1e-3
# Políticas para traços ``stable`` abertos no pipeline STEP:
# ``"fail"`` recusa a exportação inteira (fail-closed, padrão);
# ``"skip-open"`` registra o traço em ``skipped`` com ``reason="open"``
# e segue sem ele — um documento sem nenhum traço fechado ``stable``
# continua recusando nas duas políticas.
OPEN_STROKES_POLICIES = ("fail", "skip-open")
# Epsilon geométrico do host (mm): separa "encostado" de "claramente
# separado/contido" nos testes de cruzamento e contenção.
_GEOM_EPS = 1e-9
# Traço ``shape="circle"`` SEM bloco ``circle``: o ajuste Kåsa só
# promove a polilinha a circunferência analítica quando o desvio radial
# máximo cabe em ``max(0.05 mm, 0.1% do raio)`` — senão segue a
# polilinha discretizada (fallback seguro, nunca finge círculo).
_CIRCLE_FIT_TOL_MM = 0.05
_CIRCLE_FIT_TOL_REL = 1e-3
_FREECAD_ORACLE_SCRIPT = "freecad_oracle.py"
ORACLE_UNAVAILABLE = "unavailable"
MATERIAL_MANIFEST_SCHEMA = "ocre.step-material-manifest.v1"


class StepExportError(ValueError):
    """Documento/parâmetros inválidos para exportação STEP (fail-closed)."""


def load_step_material_manifest(step_path: str | Path, *, document: dict | None = None) -> dict:
    """Read material from the adjacent sidecar after verifying STEP and source identity."""
    step_path = Path(step_path)
    manifest_path = step_path.with_suffix(step_path.suffix + ".manifest.json")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != MATERIAL_MANIFEST_SCHEMA:
            raise StepExportError("schema de manifesto STEP desconhecido")
        step, source, geometry, material = data["step"], data["document"], data["geometry"], data["material"]
        if step["path"] != step_path.name or step["sha256"] != hashlib.sha256(step_path.read_bytes()).hexdigest():
            raise StepExportError("SHA/caminho do STEP não corresponde ao manifesto")
        ids = geometry["stroke_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids) or len(ids) != len(set(ids)):
            raise StepExportError("IDs de geometria inválidos no manifesto")
        if not isinstance(material.get("value"), str) or not material["value"].strip():
            raise StepExportError("material ausente no manifesto")
        if document is not None:
            if source.get("document_sha256") != document_sha256(document):
                raise StepExportError("document_sha256 não corresponde ao documento fornecido")
            expected_ids = [s.get("stroke_id") for s in document.get("strokes", []) if isinstance(s, dict) and s.get("status") == "stable" and s.get("closed")]
            if ids != expected_ids:
                raise StepExportError("IDs de geometria não correspondem ao documento fornecido")
        return {"material": material["value"], "geometry": geometry, "document": source, "step": step, "source": "manifest"}
    except StepExportError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StepExportError(f"manifesto STEP inválido ou ilegível: {manifest_path}") from exc


class StepWorkerError(RuntimeError):
    """O worker build123d falhou ou não está disponível."""


def _worktree_root() -> Path:
    # backend/ocre_core/draw/step_export.py -> 3 níveis acima = worktree.
    return Path(__file__).resolve().parents[3]


_sys_executable_has_build123d: bool | None = None


def _sys_executable_imports_build123d() -> bool:
    """True se o interpretador corrente importa build123d (probe em
    subprocesso, com cache — nunca importa build123d neste processo)."""
    global _sys_executable_has_build123d
    if _sys_executable_has_build123d is None:
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import build123d"],
                capture_output=True, text=True, timeout=120,
            )
            _sys_executable_has_build123d = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _sys_executable_has_build123d = False
    return _sys_executable_has_build123d


def find_step_worker_python(explicit: str | Path | None = None) -> Path | None:
    """Python do venv próprio do beta (``.venv-beta``), ou ``None``.

    Ordem: argumento explícito > env ``OCRE_STEP_WORKER_PYTHON`` >
    ``<worktree>/.venv-beta/bin/python`` > ``sys.executable`` (só se
    importar build123d). O último fallback existe para clone fresco
    sem ``.venv-beta``: a suíte é rodada com o venv do beta, e nesse
    caso ``sys.executable`` É o python do venv. Nunca importa
    build123d neste processo — a checagem é por subprocesso.
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    env = os.environ.get("OCRE_STEP_WORKER_PYTHON")
    if env:
        candidates.append(Path(env))
    candidates.append(_worktree_root() / ".venv-beta" / "bin" / "python")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    if _sys_executable_imports_build123d():
        return Path(sys.executable)
    return None


def _worker_script() -> Path:
    return Path(__file__).resolve().parent / "step_worker.py"


def _run_worker(job: dict, *, timeout_s: float, worker_python: str | Path | None = None) -> dict:
    """Roda um job no worker build123d (subprocesso, sem shell)."""
    python = find_step_worker_python(worker_python)
    if python is None:
        raise StepWorkerError(
            "venv do worker STEP não encontrado: crie .venv-beta no worktree "
            "e instale build123d nele (ver docs/step-worker.md) — o "
            "interpretador corrente também não importa build123d; "
            "CadQuery NUNCA deve ser instalado nesse venv"
        )
    with tempfile.TemporaryDirectory(prefix="ocre-step-worker-") as tmp:
        job_path = Path(tmp) / "job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        try:
            completed = subprocess.run(
                [str(python), str(_worker_script()), str(job_path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                shell=False,
                check=False,
                cwd=str(_worktree_root()),
                env=dict(os.environ),
            )
        except subprocess.TimeoutExpired as exc:
            raise StepWorkerError(
                f"worker STEP excedeu o timeout de {timeout_s}s"
            ) from exc
    if completed.returncode != 0:
        raise StepWorkerError(
            f"worker STEP saiu com código {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '')[-2000:]}"
        )
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise StepWorkerError(
            f"worker STEP devolveu saída ilegível: {(completed.stdout or '')[-2000:]}"
        ) from exc
    if not isinstance(result, dict):
        raise StepWorkerError(f"worker STEP devolveu JSON não-objeto: {result!r}")
    return result


def worker_versions(*, worker_python: str | Path | None = None) -> dict:
    """Versões pinadas do worker (build123d / OCP / OCCT)."""
    result = _run_worker({"kind": "versions"}, timeout_s=120, worker_python=worker_python)
    if not result.get("ok"):
        raise StepWorkerError(f"worker versions falhou: {result.get('error')}")
    return result["versions"]


def _paper_size(document: dict) -> tuple[float, float]:
    try:
        return paper_size_mm(document)
    except ValueError as exc:
        raise StepExportError(str(exc)) from exc


def _fit_circle_mm(
    points: list[list[float]], width_mm: float, height_mm: float
) -> dict[str, Any] | None:
    """Círculo ajustado (Kåsa) sobre pontos CAD, ou None se não couber.

    Ajuste algébrico centrado no baricentro (sistema 2x2 bem
    condicionado). Exige raio finito e coerente com a dispersão dos
    pontos — sem essa trava um arco fino ajustaria um círculo enorme —
    e desvio radial máximo dentro de ``max(_CIRCLE_FIT_TOL_MM,
    _CIRCLE_FIT_TOL_REL * r)``. Três gates antes de promover o
    descritor: cobertura angular quase completa (arco parcial viraria
    disco inteiro), anel fonte sem auto-interseção (``circle`` pula a
    checagem intra-anel de ``_check_no_crossings``) e a circunferência
    ajustada dentro da folha (os vértices estar dentro não basta —
    borda entre amostras pode sair). Devolve o mesmo descritor do
    bloco ``circle`` declarado, mais ``"fitted": True``.
    """
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    suu = svv = suv = suz = svz = 0.0
    spread_sq = 0.0
    for x, y in points:
        u, v = x - mean_x, y - mean_y
        z = u * u + v * v
        suu += u * u
        svv += v * v
        suv += u * v
        suz += u * z
        svz += v * z
        spread_sq = max(spread_sq, z)
    det = suu * svv - suv * suv
    if not (det > 0.0) or spread_sq <= 0.0:
        return None  # pontos colineares ou coincidentes: sem círculo
    # u²+v² + a·u + b·v + c = 0; com Σu = Σv = 0 sobra um 2x2 para a, b.
    a = (suv * svz - svv * suz) / det
    b = (suv * suz - suu * svz) / det
    cx, cy = mean_x - a / 2.0, mean_y - b / 2.0
    radius = math.sqrt((a * a + b * b) / 4.0 + (suu + svv) / n)
    if not (math.isfinite(radius) and 0.0 < radius <= 2.0 * math.sqrt(spread_sq)):
        return None
    tol = max(_CIRCLE_FIT_TOL_MM, _CIRCLE_FIT_TOL_REL * radius)
    max_dev = max(abs(math.hypot(x - cx, y - cy) - radius) for x, y in points)
    if max_dev > tol:
        return None
    # Cobertura angular: sem gap > 60° entre amostras consecutivas.
    # Sem isso um arco parcial (semicírculo etc.) ajusta com desvio
    # zero e vira disco inteiro em vez do segmento da polilinha.
    angles = sorted(math.atan2(y - cy, x - cx) for x, y in points)
    gaps = [b - a for a, b in zip(angles, angles[1:])]
    gaps.append(angles[0] + 2.0 * math.pi - angles[-1])
    if max(gaps) > math.pi / 3.0:
        return None
    # Anel auto-intersectante (bow tie et al.) também ajusta bem —
    # ``circle`` depois escaparia da checagem intra-anel.
    ring = list(points)
    segs = list(zip(ring, ring[1:] + ring[:1]))
    n = len(segs)
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # arestas adjacentes dividem o vértice do anel
            if _segments_cross(*segs[i], *segs[j]):
                return None
    # Folha: a circunferência inteira (não só os vértices) deve caber.
    if not (
        0.0 <= cx - radius and cx + radius <= width_mm
        and 0.0 <= cy - radius and cy + radius <= height_mm
    ):
        return None
    return {"center_mm": [cx, cy], "radius_mm": radius, "fitted": True}


def _stroke_circle(
    stroke: dict,
    stroke_id: str,
    *,
    points_cad: list[list[float]],
    width_mm: float,
    height_mm: float,
) -> dict[str, Any] | None:
    """Descritor analítico CAD de um traço ``shape == "circle"``, ou None.

    O bloco opcional ``circle`` (``cx_mm``/``cy_mm``/``r_mm``, paper
    coords y-down) tem prioridade e devolve ``{"center_mm": [cx,
    cy_cad], "radius_mm": r}`` — já com o flip do CAD (``y_cad =
    height_mm - cy_mm``). Parâmetro malformado, não finito, ``r_mm <=
    0`` ou círculo fora da folha recusa fail-closed: um ajuste de
    círculo corrompido nunca vira polilinha por engano. Sem o bloco,
    tenta o ajuste Kåsa sobre ``points_cad`` (polilinha discretizada);
    ajuste ruim segue a polilinha (compatível, fallback seguro).
    """
    if stroke.get("shape") != "circle":
        return None
    circle = stroke.get("circle")
    if circle is None:
        return _fit_circle_mm(points_cad, width_mm, height_mm)
    if (
        not isinstance(circle, dict)
        or not all(
            isinstance(circle.get(k), (int, float)) and not isinstance(circle.get(k), bool)
            for k in ("cx_mm", "cy_mm", "r_mm")
        )
    ):
        raise StepExportError(
            f"traço {stroke_id}: 'circle' exige cx_mm/cy_mm/r_mm numéricos"
        )
    cx, cy, r = float(circle["cx_mm"]), float(circle["cy_mm"]), float(circle["r_mm"])
    if not (math.isfinite(cx) and math.isfinite(cy) and math.isfinite(r)) or r <= 0:
        raise StepExportError(
            f"traço {stroke_id}: 'circle' exige valores finitos e r_mm > 0"
        )
    if not (
        0.0 <= cx - r and cx + r <= width_mm
        and 0.0 <= cy - r and cy + r <= height_mm
    ):
        raise StepExportError(
            f"traço {stroke_id}: círculo (cx={cx}, cy={cy}, r={r}) fora da "
            f"folha {width_mm} x {height_mm} mm"
        )
    return {"center_mm": [cx, height_mm - cy], "radius_mm": r}


def _closed_stable_profiles(
    document: dict,
    *,
    open_strokes_policy: str = "fail",
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    """Contornos CAD (y-up) dos traços ``stable`` fechados + relatório.

    Devolve ``(contours, stroke_ids, skipped)``; cada contorno é
    ``{"stroke_id": str, "points": [[x, y_cad]...], "circle": desc|None}``.
    Fail-closed: ponto fora da folha, não finito ou fechado com <3 pontos
    recusa, sempre. Traço ``stable`` ABERTO recusa por padrão
    (``open_strokes_policy="fail"``); com ``"skip-open"`` é pulado e
    registrado (``reason="open"``) — e ainda assim falha se nenhum
    traço fechado ``stable`` restar.
    """
    if open_strokes_policy not in OPEN_STROKES_POLICIES:
        raise StepExportError(
            f"open_strokes_policy desconhecida: {open_strokes_policy!r} "
            f"(esperado uma de {OPEN_STROKES_POLICIES})"
        )
    schema = document.get("schema")
    if schema is not None and schema != "draw.paper.v1":
        raise StepExportError(f"schema inesperado: {schema!r} (esperado 'draw.paper.v1')")
    strokes = document.get("strokes")
    if not isinstance(strokes, list):
        raise StepExportError("documento sem lista 'strokes'")
    width_mm, height_mm = _paper_size(document)

    contours: list[dict[str, Any]] = []
    stroke_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for stroke in strokes:
        if not isinstance(stroke, dict):
            raise StepExportError(f"traço inválido (não é objeto): {stroke!r}")
        stroke_id = stroke.get("stroke_id")
        if not isinstance(stroke_id, str) or not stroke_id:
            raise StepExportError("traço sem stroke_id (string não vazia)")
        if stroke_id in seen_ids:
            raise StepExportError(f"stroke_id duplicado: {stroke_id}")
        seen_ids.add(stroke_id)
        status = stroke.get("status")
        if status == "erased":
            skipped.append({"stroke_id": stroke_id, "reason": "erased"})
            continue
        if status == "pending":
            skipped.append({"stroke_id": stroke_id, "reason": "pending"})
            continue
        if status != "stable":
            raise StepExportError(f"traço {stroke_id}: status desconhecido {status!r}")
        if not stroke.get("closed", False):
            if open_strokes_policy == "fail":
                # Traço aberto NUNCA vira sólido — recusa fail-closed,
                # mesmo que haja outros traços fechados no documento.
                raise StepExportError(
                    f"traço {stroke_id}: traço aberto não vira sólido "
                    "(só traços fechados 'stable' vão para o STEP)"
                )
            # "skip-open": o traço aberto não vira sólido, mas não
            # derruba a exportação — fica registrado, rastreável.
            skipped.append({"stroke_id": stroke_id, "reason": "open"})
            continue
        points = stroke.get("points_mm")
        if not isinstance(points, list) or len(points) < 3:
            raise StepExportError(
                f"traço {stroke_id}: traço fechado com menos de 3 pontos"
            )
        clean: list[list[float]] = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise StepExportError(f"traço {stroke_id}: ponto inválido {point!r}")
            x, y = float(point[0]), float(point[1])
            if not (math.isfinite(x) and math.isfinite(y)):
                raise StepExportError(f"traço {stroke_id}: ponto não finito {point!r}")
            if not (0.0 <= x <= width_mm and 0.0 <= y <= height_mm):
                raise StepExportError(
                    f"traço {stroke_id}: ponto fora da folha ({x}, {y}) "
                    f"para papel {width_mm} x {height_mm} mm"
                )
            # Papel y-down -> CAD y-up (mesma regra do DXF F0).
            clean.append([x, height_mm - y])
        contours.append({
            "stroke_id": stroke_id,
            "points": clean,
            "circle": _stroke_circle(
                stroke, stroke_id, points_cad=clean,
                width_mm=width_mm, height_mm=height_mm,
            ),
        })
        stroke_ids.append(stroke_id)
    if not contours:
        raise StepExportError(
            "documento sem traço fechado 'stable': nada a extrudar para STEP"
        )
    return contours, stroke_ids, skipped


def _polygon_area_mm2(ring: list[list[float]]) -> float:
    """Área do anel poligonal (shoelace) — sanidade do host, sem OCP."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _ring(points: list[list[float]]) -> list[list[float]]:
    """Anel fechado sem o último ponto repetido (aresta degenerada)."""
    if len(points) > 1 and points[0] == points[-1]:
        return points[:-1]
    return points


def _orient(a: list[float], b: list[float], c: list[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_cross(
    p1: list[float], p2: list[float], p3: list[float], p4: list[float]
) -> bool:
    """True se os segmentos se cruzam ou se tocam (qualquer ponto comum).

    Toque conta: um furo encostado na parede externa ou dois contornos
    dividindo vértice/aresta são geometria degenerada para o STEP —
    recusar fail-closed é mais honesto que deixar o kernel decidir.
    """
    if (
        max(p1[0], p2[0]) < min(p3[0], p4[0])
        or max(p3[0], p4[0]) < min(p1[0], p2[0])
        or max(p1[1], p2[1]) < min(p3[1], p4[1])
        or max(p3[1], p4[1]) < min(p1[1], p2[1])
    ):
        return False

    def on(a: list[float], b: list[float], p: list[float]) -> bool:
        return (
            abs(_orient(a, b, p)) <= 1e-9
            and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
        )

    o1, o2 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    o3, o4 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    return on(p1, p2, p3) or on(p1, p2, p4) or on(p3, p4, p1) or on(p3, p4, p2)


def _seg_crosses_circle(
    a: list[float], b: list[float], center: list[float], r: float
) -> bool:
    """True se o segmento cruza ou toca a circunferência (coords CAD)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    fx, fy = a[0] - center[0], a[1] - center[1]
    if dx == 0.0 and dy == 0.0:  # segmento degenerado: ponto — toque se sobre o círculo
        return abs(math.hypot(fx, fy) - r) <= _GEOM_EPS * max(1.0, r)
    qa = dx * dx + dy * dy
    qb = 2.0 * (fx * dx + fy * dy)
    qc = fx * fx + fy * fy - r * r
    disc = qb * qb - 4.0 * qa * qc
    if disc < 0.0:
        # |p(t)-c|^2 = r^2 sem raiz real; tolera tangência dentro do eps.
        if disc < -_GEOM_EPS * max(qb * qb, 4.0 * qa * abs(qc), 1.0):
            return False
        disc = 0.0
    sq = math.sqrt(disc)
    t1, t2 = (-qb - sq) / (2.0 * qa), (-qb + sq) / (2.0 * qa)
    # Alguma raiz em [0, 1] (inclusive): cruzamento; nas pontas, vértice
    # sobre o círculo (toque). Raízes fora das duas pontas = segmento
    # inteiro dentro do disco, sem cruzar a circunferência.
    t_lo, t_hi = -_GEOM_EPS, 1.0 + _GEOM_EPS
    return (t_lo <= t1 <= t_hi) or (t_lo <= t2 <= t_hi)


def _circles_cross(ca: dict[str, Any], cb: dict[str, Any]) -> bool:
    """True se as circunferências se cruzam ou se tocam."""
    r1, r2 = ca["radius_mm"], cb["radius_mm"]
    d = math.hypot(
        ca["center_mm"][0] - cb["center_mm"][0],
        ca["center_mm"][1] - cb["center_mm"][1],
    )
    tol = _GEOM_EPS * max(1.0, r1 + r2)
    # OK: disjuntas (d > r1+r2) ou uma contida na outra (d < |r1-r2|).
    return not (d > r1 + r2 + tol or d < abs(r1 - r2) - tol)


def _contours_cross(ca: dict[str, Any], cb: dict[str, Any]) -> bool:
    """Cruzamento/toque entre contornos na geometria enviada ao worker:
    anel poligonal ou circunferência analítica — nunca a polilinha
    discretizada de um traço ``circle``."""
    a_circ, b_circ = ca["circle"], cb["circle"]
    if a_circ is not None and b_circ is not None:
        return _circles_cross(a_circ, b_circ)
    if a_circ is not None:
        ring = _ring(cb["points"])
        return any(
            _seg_crosses_circle(p, q, a_circ["center_mm"], a_circ["radius_mm"])
            for p, q in zip(ring, ring[1:] + ring[:1])
        )
    if b_circ is not None:
        ring = _ring(ca["points"])
        return any(
            _seg_crosses_circle(p, q, b_circ["center_mm"], b_circ["radius_mm"])
            for p, q in zip(ring, ring[1:] + ring[:1])
        )
    ring_a, ring_b = _ring(ca["points"]), _ring(cb["points"])
    return any(
        _segments_cross(*s1, *s2)
        for s1 in zip(ring_a, ring_a[1:] + ring_a[:1])
        for s2 in zip(ring_b, ring_b[1:] + ring_b[:1])
    )


def _check_no_crossings(contours: list[dict[str, Any]]) -> None:
    """Fail-closed: recusa auto-interseção e cruzamento/toque entre
    contornos — na geometria do wire (círculo analítico ou anel)."""
    for contour in contours:
        if contour["circle"] is not None:
            continue  # circunferência analítica não auto-intersecta
        ring = _ring(contour["points"])
        segs = list(zip(ring, ring[1:] + ring[:1]))
        n = len(segs)
        for i in range(n):
            for j in range(i + 2, n):
                if i == 0 and j == n - 1:
                    continue  # arestas adjacentes dividem o vértice do anel
                if _segments_cross(*segs[i], *segs[j]):
                    raise StepExportError(
                        f"traço {contour['stroke_id']}: contorno "
                        "auto-intersecta ou se toca"
                    )
    for a in range(len(contours)):
        for b in range(a + 1, len(contours)):
            if _contours_cross(contours[a], contours[b]):
                raise StepExportError(
                    f"contornos {contours[a]['stroke_id']} e "
                    f"{contours[b]['stroke_id']} se cruzam ou se tocam"
                )


def _point_in_ring(point: list[float], ring: list[list[float]]) -> bool:
    """Ray-casting par-ímpar (+x); o ponto nunca cai sobre a borda —
    toque já teria sido recusado por ``_check_no_crossings``."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xin > x:
                inside = not inside
    return inside


def _rep_point(contour: dict[str, Any]) -> list[float]:
    """Ponto representativo SOBRE a fronteira do contorno, para o teste
    de contenção: primeiro vértice do anel ou um ponto da circunferência.
    Usar o centro do círculo (interior) falharia quando um contorno
    menor contivesse o centro — diria "círculo dentro do quadrado"
    quando na verdade o quadrado está dentro do disco."""
    circle = contour["circle"]
    if circle is not None:
        cx, cy = circle["center_mm"]
        return [cx + circle["radius_mm"], cy]
    return _ring(contour["points"])[0]


def _point_in_contour(point: list[float], contour: dict[str, Any]) -> bool:
    """Contenção estrita na fronteira do contorno — anel poligonal ou
    circunferência analítica (a geometria que o worker extruda)."""
    circle = contour["circle"]
    if circle is not None:
        r = circle["radius_mm"]
        dx = point[0] - circle["center_mm"][0]
        dy = point[1] - circle["center_mm"][1]
        return math.hypot(dx, dy) < r - _GEOM_EPS * max(1.0, r)
    return _point_in_ring(point, _ring(contour["points"]))


def _contour_area_mm2(contour: dict[str, Any]) -> float:
    """Área esperada do contorno: ``pi * r^2`` para ``circle`` analítico,
    shoelace do anel caso contrário — a mesma geometria que o worker gera."""
    if contour["circle"] is not None:
        return math.pi * contour["circle"]["radius_mm"] ** 2
    return _polygon_area_mm2(_ring(contour["points"]))


def _wire_desc(contour: dict[str, Any]) -> dict[str, Any]:
    """Descritor de wire do contorno para o job do worker."""
    if contour["circle"] is not None:
        circle = contour["circle"]
        return {
            "kind": "circle",
            "center_mm": circle["center_mm"],
            "radius_mm": circle["radius_mm"],
        }
    return {"kind": "polygon", "points_mm": _ring(contour["points"])}


def _regions(contours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agrupa contornos fechados em ``{outer, holes}`` pela regra par-ímpar.

    Profundidade par (0, 2, ...) = contorno externo (sólido/ilha);
    profundidade ímpar = furo pertencente ao contorno externo de maior
    profundidade que o contém. Cruzamento e contenção usam a geometria
    do wire enviado ao worker (circunferência analítica quando o traço
    tem bloco ``circle``, nunca só a polilinha discretizada), então um
    único ponto representativo por contorno basta. Toda região sai com
    área líquida > 0 — furo que zera a chapa recusa fail-closed.
    """
    for contour in contours:
        if contour["circle"] is not None:
            continue  # a fronteira é a circunferência, não a polilinha
        if len(_ring(contour["points"])) < 3:
            raise StepExportError(
                f"traço {contour['stroke_id']}: contorno fechado degenerado "
                "(menos de 3 pontos distintos)"
            )
    _check_no_crossings(contours)
    containers = [
        [j for j, other in enumerate(contours)
         if j != i and _point_in_contour(_rep_point(contour), other)]
        for i, contour in enumerate(contours)
    ]
    depth = [len(c) for c in containers]
    regions: list[dict[str, Any]] = []
    region_of: dict[int, int] = {}
    for i, contour in enumerate(contours):
        if depth[i] % 2 == 0:
            region_of[i] = len(regions)
            regions.append({"outer": contour, "holes": []})
    for i, contour in enumerate(contours):
        if depth[i] % 2 == 1:
            parent = max(containers[i], key=lambda j: depth[j])
            regions[region_of[parent]]["holes"].append(contour)
    for region in regions:
        net = _contour_area_mm2(region["outer"]) - sum(
            _contour_area_mm2(hole) for hole in region["holes"]
        )
        if net <= 0:
            raise StepExportError(
                f"contorno {region['outer']['stroke_id']}: área líquida "
                "nula ou negativa (furos cobrem o contorno externo)"
            )
    return regions


def document_to_step(
    document: dict,
    thickness_mm: float | None,
    material: str | None,
    out_path: str | Path,
    *,
    timestamp: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    worker_python: str | Path | None = None,
    open_strokes_policy: str = "fail",
) -> dict:
    """Documento ``draw.paper.v1`` -> sólido STEP (via worker build123d).

    ``thickness_mm`` e ``material`` são DECLARADOS por humano — nunca
    inventados: ``thickness_mm`` ausente/``None``/<=0 ou ``material``
    vazio recusa fail-closed. ``open_strokes_policy`` decide o destino
    de traços ``stable`` ABERTOS (ver ``OPEN_STROKES_POLICIES``); o
    padrão ``"fail"`` mantém a recusa fail-closed. Contornos fechados
    aninhados viram furos/ilhas (regra par-ímpar) e traços
    ``shape="circle"`` com bloco ``circle`` válido — ou cujos pontos
    ajustam um círculo (Kåsa) — viram fio analítico no worker. Devolve
    o relatório da exportação (caminho, sha256, volume, bbox,
    FILE_SCHEMA real, versões pinadas).
    """
    if thickness_mm is None:
        raise StepExportError(
            "thickness_mm é obrigatório e declarado por humano "
            "(espessura nunca é inventada)"
        )
    if isinstance(thickness_mm, bool) or not isinstance(thickness_mm, (int, float)):
        raise StepExportError(f"thickness_mm inválido: {thickness_mm!r}")
    if not math.isfinite(float(thickness_mm)) or float(thickness_mm) <= 0:
        raise StepExportError(
            f"thickness_mm deve ser positivo e finito (recebido {thickness_mm!r})"
        )
    if not isinstance(material, str) or not material.strip():
        raise StepExportError(
            "material é obrigatório e declarado por humano "
            "(material nunca é inventado)"
        )
    material = material.strip()
    contours, stroke_ids, skipped = _closed_stable_profiles(
        document, open_strokes_policy=open_strokes_policy
    )
    regions = _regions(contours)
    expected_area = sum(
        _contour_area_mm2(region["outer"])
        - sum(_contour_area_mm2(hole) for hole in region["holes"])
        for region in regions
    )
    expected_volume = expected_area * float(thickness_mm)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    job: dict[str, Any] = {
        "kind": "export",
        "regions": [
            {
                "outer": _wire_desc(region["outer"]),
                "holes": [_wire_desc(hole) for hole in region["holes"]],
            }
            for region in regions
        ],
        "thickness_mm": float(thickness_mm),
        "material": material,
        "out_path": str(out_path),
    }
    if timestamp is not None:
        job["timestamp"] = timestamp
    result = _run_worker(job, timeout_s=timeout_s, worker_python=worker_python)
    if not result.get("ok"):
        raise StepWorkerError(f"exportação STEP falhou no worker: {result.get('error')}")

    # Sanidade do host, sem OCP: volume do sólido vs área x espessura.
    volume = float(result["volume_mm3"])
    if expected_volume <= 0 or abs(volume - expected_volume) / expected_volume > VOLUME_TOLERANCE_REL:
        raise StepWorkerError(
            f"volume do sólido ({volume} mm³) diverge do esperado "
            f"(área {expected_area} mm² x espessura {thickness_mm} mm = "
            f"{expected_volume} mm³) além de {VOLUME_TOLERANCE_REL:.0%}"
        )
    report = {
        "adapter": "draw.paper.v1 -> STEP (build123d worker, subprocesso)",
        "fingerprint_domain": "DRAW:PAPER-APPROVAL:FINGERPRINT",
        "doc_id": document.get("doc_id"),
        "doc_revision": document.get("revision"),
        "output_path": str(out_path),
        "output_sha256": result["sha256"],
        "output_bytes": result["bytes"],
        "file_schema": result.get("file_schema"),
        "thickness_mm": float(thickness_mm),
        "material_declared": material,
        "open_strokes_policy": open_strokes_policy,
        "stroke_ids": stroke_ids,
        "skipped": skipped,
        "profiles": len(regions),
        "contours": len(contours),
        "expected_volume_mm3": expected_volume,
        "volume_mm3": volume,
        "bbox_mm": result["bbox_mm"],
        "worker_versions": result.get("worker_versions", {}),
        "timestamp_param_used": result.get("timestamp_param_used"),
        "notes": [
            f"traço {contour['stroke_id']}: círculo ajustado dos pontos "
            "(traço 'circle' sem bloco 'circle' declarado)"
            for contour in contours
            if contour["circle"] is not None and contour["circle"].get("fitted")
        ] + result.get("notes", []),
        "material_in_step_native": bool(result.get("material_in_step_native", False)),
    }
    # AP214 emitted by the current build123d/OCP writer does not encode
    # material semantics. Ship an explicit, hash-bound handoff next to the
    # STEP so downstream CAD tooling can consume the declaration safely.
    manifest_path = out_path.with_suffix(out_path.suffix + ".manifest.json")
    manifest = {
        "schema": "ocre.step-material-manifest.v1",
        "step": {"path": out_path.name, "sha256": result["sha256"]},
        "document": {
            "doc_id": document.get("doc_id"),
            "revision": document.get("revision"),
            "document_sha256": document_sha256(document),
        },
        "geometry": {
            "stroke_ids": stroke_ids,
            "profiles": len(regions),
            "thickness_mm": float(thickness_mm),
            "volume_mm3": volume,
            "bbox_mm": result["bbox_mm"],
        },
        "material": {"value": material, "source": "human-confirmed-or-declared"},
        "material_in_step_native": bool(result.get("material_in_step_native", False)),
    }
    # Publish sidecar atomically so a failed write cannot truncate a prior manifest.
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=manifest_path.name + ".", suffix=".tmp", dir=manifest_path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, manifest_path)
    finally:
        temp_path.unlink(missing_ok=True)
    report["material_manifest_path"] = str(manifest_path)
    report["material_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return report


def validate_step(
    step_path: str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    worker_python: str | Path | None = None,
) -> dict:
    """Valida um STEP com BRepCheck via OCP dentro do worker.

    Não é o oráculo FreeCAD (que é ``oracle_step``) — é a checagem
    interna do próprio kernel que gerou o arquivo.
    """
    step_path = Path(step_path)
    if not step_path.is_file():
        raise StepWorkerError(f"STEP não encontrado: {step_path}")
    result = _run_worker(
        {"kind": "validate", "step_path": str(step_path)},
        timeout_s=timeout_s,
        worker_python=worker_python,
    )
    if not result.get("ok"):
        raise StepWorkerError(f"validação STEP falhou no worker: {result.get('error')}")
    return result


def export_dxf_ac1015(
    document: dict,
    dxf_path: str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    worker_python: str | Path | None = None,
    open_strokes_policy: str = "fail",
) -> dict:
    """Spike (a) do Senado: DXF ``AC1015`` (R2000) do perfil 2D via build123d.

    Usa os mesmos contornos fechados ``stable`` do STEP (espessura 1 mm
    só para o worker rodar; o DXF é 2D e não carrega espessura) e a
    mesma ``open_strokes_policy`` para traços abertos ``stable``.
    Devolve o relatório do worker; a abertura no LibreCAD é o gate
    ``librecad_gate.check_dxf`` (F0), chamado pelo teste/caller.
    """
    contours, stroke_ids, skipped = _closed_stable_profiles(
        document, open_strokes_policy=open_strokes_policy
    )
    regions = _regions(contours)
    dxf_path = Path(dxf_path)
    dxf_path.parent.mkdir(parents=True, exist_ok=True)
    # O worker precisa de um STEP de saída no job de export; usa-se um
    # temporário descartável — o artefato deste spike é o DXF.
    with tempfile.TemporaryDirectory(prefix="ocre-step-dxf-") as tmp:
        job: dict[str, Any] = {
            "kind": "export",
            "regions": [
                {
                    "outer": _wire_desc(region["outer"]),
                    "holes": [_wire_desc(hole) for hole in region["holes"]],
                }
                for region in regions
            ],
            "thickness_mm": 1.0,
            "material": "spike-dxf-only",
            "out_path": str(Path(tmp) / "spike.step"),
            "dxf_path": str(dxf_path),
        }
        result = _run_worker(job, timeout_s=timeout_s, worker_python=worker_python)
    if not result.get("ok"):
        raise StepWorkerError(f"spike DXF falhou no worker: {result.get('error')}")
    if "dxf_path" not in result:
        raise StepWorkerError(
            f"spike DXF: worker não produziu DXF: {result.get('dxf_error', 'sem detalhe')}"
        )
    return {
        "dxf_version": "AC1015",
        "dxf_path": result["dxf_path"],
        "dxf_sha256": result["dxf_sha256"],
        "dxf_bytes": result["dxf_bytes"],
        "stroke_ids": stroke_ids,
        "skipped": skipped,
        "worker_versions": result.get("worker_versions", {}),
    }


def _freecad_command() -> list[str] | None:
    """Comando do oráculo FreeCAD, ou ``None`` se indisponível.

    Ordem: ``freecadcmd`` no PATH > ``FREECAD_CMD`` (env, para o
    ``freecadcmd`` extraído do AppImage) > ``freecadcmd`` extraído em
    ``<worktree>/.external/squashfs-root/usr/bin/freecadcmd`` >
    AppImage em ``FREECAD_APPIMAGE`` ou ``.external/FreeCAD.AppImage``
    (precisa de FUSE; sem FUSE, a extração é a rota).
    ``freecadcmd`` NÃO existe via apt nesta máquina (sem candidato);
    o AppImage oficial 1.0.2 é a rota tentada no beta.
    """
    binary = shutil.which("freecadcmd")
    if binary:
        return [binary]
    env_cmd = os.environ.get("FREECAD_CMD")
    if env_cmd and Path(env_cmd).is_file() and os.access(env_cmd, os.X_OK):
        return [env_cmd]
    extracted = _worktree_root() / ".external" / "squashfs-root" / "usr" / "bin" / "freecadcmd"
    if extracted.is_file() and os.access(extracted, os.X_OK):
        return [str(extracted)]
    appimage = os.environ.get("FREECAD_APPIMAGE") or str(
        _worktree_root() / ".external" / "FreeCAD.AppImage"
    )
    if Path(appimage).is_file() and os.access(appimage, os.X_OK):
        return [appimage, "--console"]
    return None


def oracle_step(
    step_path: str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    freecad_command: list[str] | None = None,
) -> dict:
    """Oráculo FreeCAD: lê o STEP e reporta isValid/volume/bbox.

    Devolve ``status`` ``pass``/``fail``/``unavailable``. ``unavailable``
    = FreeCAD não existe nesta máquina (gap registrado, nunca fingido).
    Nunca confia no exit code: o veredito vem do JSON escrito pelo
    script do oráculo.
    """
    step_path = Path(step_path)
    if not step_path.is_file():
        raise StepWorkerError(f"STEP não encontrado: {step_path}")
    command = freecad_command if freecad_command is not None else _freecad_command()
    if not command:
        return {
            "status": ORACLE_UNAVAILABLE,
            "available": False,
            "step_path": str(step_path),
            "reason": "oráculo FreeCAD indisponível nesta máquina: "
            "'freecadcmd' não existe via apt (sem candidato) e nenhum "
            "AppImage oficial foi obtido (ver docs/step-worker.md); "
            "o oráculo fica como passo opcional que falha honestamente",
        }
    script = (
        "import json, sys\n"
        "inp, outp = sys.argv[-2], sys.argv[-1]\n"
        "import Part\n"
        "shape = Part.Shape()\n"
        "shape.read(inp)\n"
        "bb = shape.BoundBox\n"
        "report = {\n"
        "    'is_valid': bool(shape.isValid()),\n"
        "    'is_closed': bool(shape.isClosed()),\n"
        "    'volume_mm3': float(shape.Volume),\n"
        "    'solids': len(shape.Solids),\n"
        "    'bbox_mm': {\n"
        "        'min': [float(bb.XMin), float(bb.YMin), float(bb.ZMin)],\n"
        "        'max': [float(bb.XMax), float(bb.YMax), float(bb.ZMax)],\n"
        "        'size': [float(bb.XLength), float(bb.YLength), float(bb.ZLength)],\n"
        "    },\n"
        "}\n"
        "open(outp, 'w', encoding='utf-8').write(json.dumps(report))\n"
    )
    with tempfile.TemporaryDirectory(prefix="ocre-freecad-oracle-") as tmp:
        tmp_path = Path(tmp)
        script_path = tmp_path / _FREECAD_ORACLE_SCRIPT
        script_path.write_text(script, encoding="utf-8")
        report_path = tmp_path / "oracle.json"
        # freecadcmd: args do script vão depois de "--".
        argv = [*command, str(script_path), "--", str(step_path), str(report_path)]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                shell=False,
                check=False,
                cwd=tmp,
                env=dict(os.environ),
            )
        except subprocess.TimeoutExpired as exc:
            raise StepWorkerError(
                f"oráculo FreeCAD excedeu o timeout de {timeout_s}s"
            ) from exc
        if not report_path.is_file():
            stderr = completed.stderr or ""
            # AppImage sem FUSE nesta máquina: o oráculo não existe
            # aqui — indisponível, não falha do STEP.
            if "fusermount" in stderr or "Cannot mount AppImage" in stderr:
                return {
                    "status": ORACLE_UNAVAILABLE,
                    "available": False,
                    "step_path": str(step_path),
                    "command": command,
                    "reason": "AppImage do FreeCAD não monta nesta máquina "
                    "(sem FUSE); extraia com --appimage-extract e aponte "
                    "FREECAD_CMD para squashfs-root/usr/bin/freecadcmd",
                }
            return {
                "status": "fail",
                "available": True,
                "step_path": str(step_path),
                "command": command,
                "returncode": completed.returncode,
                "stdout": (completed.stdout or "")[-2000:],
                "stderr": stderr[-2000:],
                "reason": "freecadcmd rodou mas não escreveu o relatório JSON",
            }
        report = json.loads(report_path.read_text(encoding="utf-8"))
    report["status"] = "pass" if report.get("is_valid") else "fail"
    report["available"] = True
    report["step_path"] = str(step_path)
    report["command"] = command
    return report
