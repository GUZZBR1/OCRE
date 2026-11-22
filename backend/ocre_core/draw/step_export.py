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
  Y já no sentido CAD: ``y_cad = altura_efetiva_mm - y_papel``) ->
  ``extrude(thickness)`` -> sólido STEP.
- Espessura e material são DECLARADOS por humano (argumento) — nunca
  inventados. Espessura ausente/``None``/<=0 ou material vazio =
  recusa fail-closed.
- Traço aberto = recusa fail-closed (nunca vira sólido). Documento
  sem traço fechado ``stable`` = recusa.
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

from .paper import DEFAULT_SPEC
from .document import document_sha256

__all__ = [
    "DEFAULT_TIMEOUT_S",
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
# Volume esperado (área do perfil x espessura) vs volume do sólido:
# tolerância relativa do teste de sanidade do host (OCCT arredonda em
# ~1e-7 relativo; 0,1% é folga ampla sem esconder erro de geometria).
VOLUME_TOLERANCE_REL = 1e-3
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
    paper = document.get("paper") or {}
    width = float(paper.get("width_mm", DEFAULT_SPEC.width_mm))
    height = float(
        paper.get(
            "effective_height_mm",
            paper.get("height_mm", DEFAULT_SPEC.effective_height_mm),
        )
    )
    if not (math.isfinite(width) and math.isfinite(height)):
        raise StepExportError("paper.width_mm/height_mm devem ser finitos")
    if width <= 0 or height <= 0:
        raise StepExportError("paper.width_mm/height_mm devem ser positivos")
    return width, height


def _closed_stable_profiles(document: dict) -> tuple[list[list[list[float]]], list[str], list[dict[str, str]]]:
    """Perfis CAD (y-up) dos traços ``stable`` fechados + relatório.

    Devolve ``(profiles, stroke_ids, skipped)``. Fail-closed: traço
    aberto ``stable`` recusa tudo (nunca vira sólido); ponto fora da
    folha, não finito ou fechado com <3 pontos também recusa.
    """
    schema = document.get("schema")
    if schema is not None and schema != "draw.paper.v1":
        raise StepExportError(f"schema inesperado: {schema!r} (esperado 'draw.paper.v1')")
    strokes = document.get("strokes")
    if not isinstance(strokes, list):
        raise StepExportError("documento sem lista 'strokes'")
    width_mm, height_mm = _paper_size(document)

    profiles: list[list[list[float]]] = []
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
            # Traço aberto NUNCA vira sólido — recusa fail-closed,
            # mesmo que haja outros traços fechados no documento.
            raise StepExportError(
                f"traço {stroke_id}: traço aberto não vira sólido "
                "(só traços fechados 'stable' vão para o STEP)"
            )
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
        profiles.append(clean)
        stroke_ids.append(stroke_id)
    if not profiles:
        raise StepExportError(
            "documento sem traço fechado 'stable': nada a extrudar para STEP"
        )
    return profiles, stroke_ids, skipped


def _polygon_area_mm2(profile: list[list[float]]) -> float:
    """Área do polígono (shoelace) — sanidade do host, sem OCP."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(profile, profile[1:] + profile[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def document_to_step(
    document: dict,
    thickness_mm: float | None,
    material: str | None,
    out_path: str | Path,
    *,
    timestamp: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    worker_python: str | Path | None = None,
) -> dict:
    """Documento ``draw.paper.v1`` -> sólido STEP (via worker build123d).

    ``thickness_mm`` e ``material`` são DECLARADOS por humano — nunca
    inventados: ``thickness_mm`` ausente/``None``/<=0 ou ``material``
    vazio recusa fail-closed. Devolve o relatório da exportação
    (caminho, sha256, volume, bbox, FILE_SCHEMA real, versões pinadas).
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
    profiles, stroke_ids, skipped = _closed_stable_profiles(document)
    expected_area = sum(_polygon_area_mm2(p) for p in profiles)
    expected_volume = expected_area * float(thickness_mm)

    out_path = Path(out_path)
    if out_path.parent != Path(".") and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    job: dict[str, Any] = {
        "kind": "export",
        "profiles": profiles,
        "thickness_mm": float(thickness_mm),
        "material": material.strip(),
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
        "material_declared": material.strip(),
        "stroke_ids": stroke_ids,
        "skipped": skipped,
        "profiles": len(profiles),
        "expected_volume_mm3": expected_volume,
        "volume_mm3": volume,
        "bbox_mm": result["bbox_mm"],
        "worker_versions": result.get("worker_versions", {}),
        "timestamp_param_used": result.get("timestamp_param_used"),
        "notes": result.get("notes", []),
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
            "profiles": len(profiles),
            "thickness_mm": float(thickness_mm),
            "volume_mm3": volume,
            "bbox_mm": result["bbox_mm"],
        },
        "material": {"value": material.strip(), "source": "human-confirmed-or-declared"},
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
) -> dict:
    """Spike (a) do Senado: DXF ``AC1015`` (R2000) do perfil 2D via build123d.

    Usa o mesmo perfil fechado ``stable`` do STEP (espessura 1 mm só
    para o worker rodar; o DXF é 2D e não carrega espessura). Devolve
    o relatório do worker; a abertura no LibreCAD é o gate
    ``librecad_gate.check_dxf`` (F0), chamado pelo teste/caller.
    """
    profiles, stroke_ids, skipped = _closed_stable_profiles(document)
    dxf_path = Path(dxf_path)
    if dxf_path.parent != Path(".") and not dxf_path.parent.exists():
        dxf_path.parent.mkdir(parents=True, exist_ok=True)
    # O worker precisa de um STEP de saída no job de export; usa-se um
    # temporário descartável — o artefato deste spike é o DXF.
    with tempfile.TemporaryDirectory(prefix="ocre-step-dxf-") as tmp:
        job: dict[str, Any] = {
            "kind": "export",
            "profiles": profiles,
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
