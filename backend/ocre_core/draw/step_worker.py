"""Worker STEP (build123d) — RODA SÓ NO SUBPROCESSO ``.venv-beta``.

Este arquivo NUNCA é importado pelo servidor OCRE nem por nenhum módulo
de ``ocre_core``: ele é executado como script pelo Python do venv
próprio do beta (``.venv-beta/bin/python``), via ``step_export.py``,
que o chama com ``subprocess.run`` (argv lista, timeout obrigatório,
sem shell). Motivo: build123d/OCP é Apache-2.0 mas pesado e com
conflito de pacote de topo ``OCP/`` com o CadQuery — o Senado pinou
build123d como worker 3D único e proibiu CadQuery no mesmo venv.

Protocolo: o host escreve um JSON de job e passa o caminho como
``argv[1]``; o worker escreve o resultado como JSON em stdout (uma
linha) e sai com código 0 mesmo quando o job falha — falha de job é
``{"ok": false, "error": ...}``; código != 0 é só para erro do próprio
worker (ex.: build123d não importável).

Job ``export`` (campos):
- ``profiles``: lista de perfis; cada perfil é uma lista de [x, y] em
  mm, já no sentido CAD (y-up), polilinha FECHADA (o último ponto NÃO
  precisa repetir o primeiro; ponto final igual ao inicial é removido).
- ``thickness_mm``: espessura declarada (float > 0).
- ``material``: string declarada (registrada no relatório; o STEP
  AP214 emitido pelo OCCT NÃO carrega material — ver ``notes``).
- ``out_path``: caminho do STEP a escrever.
- ``timestamp``: string opcional passada a ``export_step`` quando o
  build123d instalado aceita o parâmetro (saída determinística).
- ``dxf_path`` (opcional): se dado, exporta também o perfil 2D em DXF
  ``AC1015`` (R2000) — spike (a) do Senado.

Job ``validate`` (campos):
- ``step_path``: STEP a ler e checar com BRepCheck + volume + bbox.

O worker NÃO decide nada sobre o documento: validação de status
(``stable``/``erased``/``pending``), de espessura/material declarados
e de traço fechado é do host (``step_export.py``). Aqui só geometria.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import sys
from pathlib import Path


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def _file_schema(step_path: Path) -> str | None:
    """Lê a linha FILE_SCHEMA do STEP (diz qual AP foi de fato emitido)."""
    try:
        with step_path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(60):
                line = fh.readline()
                if not line:
                    break
                if "FILE_SCHEMA" in line:
                    return line.strip()
    except OSError:
        return None
    return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _job_export(job: dict) -> dict:
    from build123d import Polyline, export_step, extrude, make_face

    profiles = job.get("profiles")
    thickness = job.get("thickness_mm")
    material = job.get("material")
    out_path = Path(job["out_path"])
    if not isinstance(profiles, list) or not profiles:
        return _fail("job.export: 'profiles' vazio ou ausente")
    if not isinstance(thickness, (int, float)) or isinstance(thickness, bool):
        return _fail("job.export: 'thickness_mm' inválido")
    if thickness <= 0 or not math.isfinite(float(thickness)):
        return _fail("job.export: 'thickness_mm' deve ser positivo e finito")
    if not isinstance(material, str) or not material.strip():
        return _fail("job.export: 'material' declarado ausente")

    solids = []
    areas = []
    for index, raw_points in enumerate(profiles):
        if not isinstance(raw_points, list) or len(raw_points) < 3:
            return _fail(f"job.export: perfil {index} com menos de 3 pontos")
        points = []
        for point in raw_points:
            if (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in point)
                or any(not math.isfinite(float(v)) for v in point)
            ):
                return _fail(f"job.export: perfil {index} tem ponto inválido {point!r}")
            points.append((float(point[0]), float(point[1])))
        # Ponto final igual ao inicial: polilinha fechada não precisa
        # dele e ele criaria aresta degenerada no make_face.
        if points[0] == points[-1]:
            points = points[:-1]
        if len(points) < 3:
            return _fail(f"job.export: perfil {index} ficou com menos de 3 pontos")
        try:
            wire = Polyline(*points, close=True)
            face = make_face(wire)
            solid = extrude(face, amount=float(thickness))
        except Exception as exc:  # noqa: BLE001 - geometria inválida é erro de job
            return _fail(f"job.export: perfil {index} não virou sólido: {exc}")
        if solid is None or solid.volume <= 0:
            return _fail(f"job.export: perfil {index} gerou sólido sem volume")
        solids.append(solid)
        areas.append(face.area)

    # Um sólido por perfil; vários perfis viram um Compound no STEP.
    if len(solids) == 1:
        to_export = solids[0]
    else:
        from build123d import Compound

        to_export = Compound(children=solids)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = job.get("timestamp")
    export_kwargs: dict = {}
    # build123d >= 0.10 aceita timestamp= em export_step (saída
    # determinística); versões sem o parâmetro escrevem o relógio no
    # FILE_NAME — o spike (c) mede isso e reporta, sem fingir.
    try:
        if timestamp is not None and "timestamp" in inspect.signature(export_step).parameters:
            export_kwargs["timestamp"] = timestamp
    except (TypeError, ValueError):
        pass
    try:
        export_step(to_export, str(out_path), **export_kwargs)
    except Exception as exc:  # noqa: BLE001 - falha de escrita é erro de job
        return _fail(f"job.export: export_step falhou: {exc}")
    if not out_path.is_file() or out_path.stat().st_size == 0:
        return _fail("job.export: STEP não foi escrito ou está vazio")

    bbox = to_export.bounding_box()
    result: dict = {
        "ok": True,
        "out_path": str(out_path),
        "sha256": _sha256(out_path),
        "bytes": out_path.stat().st_size,
        "file_schema": _file_schema(out_path),
        "volume_mm3": float(to_export.volume),
        "area_mm2": float(sum(areas)),
        "thickness_mm": float(thickness),
        "material_declared": material,
        "solids": len(solids),
        "bbox_mm": {
            "min": [float(v) for v in bbox.min],
            "max": [float(v) for v in bbox.max],
            "size": [float(v) for v in bbox.size],
        },
        "timestamp_param_used": bool(export_kwargs),
        "notes": [
            "material é declarado no relatório/sidecar; o STEP AP214 "
            "emitido pelo OCCT não carrega o material no arquivo."
        ],
    }

    dxf_path = job.get("dxf_path")
    if dxf_path:
        # Spike (a): DXF AC1015 do perfil 2D, via build123d.
        try:
            from build123d import ExportDXF, Unit

            dxf_path = Path(dxf_path)
            dxf_path.parent.mkdir(parents=True, exist_ok=True)
            face_or_faces = []
            for prof in profiles:
                pts = [(float(p[0]), float(p[1])) for p in prof]
                if len(pts) > 1 and pts[0] == pts[-1]:
                    pts = pts[:-1]
                face_or_faces.append(make_face(Polyline(*pts, close=True)))
            exporter = ExportDXF(version="AC1015", unit=Unit.MM)
            if len(face_or_faces) == 1:
                exporter.add_shape(face_or_faces[0])
            else:
                for face in face_or_faces:
                    exporter.add_shape(face)
            exporter.write(str(dxf_path))
            result["dxf_path"] = str(dxf_path)
            result["dxf_sha256"] = _sha256(dxf_path)
            result["dxf_bytes"] = dxf_path.stat().st_size
        except Exception as exc:  # noqa: BLE001 - spike registra, não finge
            result["dxf_error"] = f"ExportDXF falhou: {exc}"

    return result


def _job_validate(job: dict) -> dict:
    from build123d import import_step
    from OCP.BRepCheck import BRepCheck_Analyzer

    step_path = Path(job["step_path"])
    if not step_path.is_file():
        return _fail(f"job.validate: STEP não encontrado: {step_path}")
    try:
        shape = import_step(str(step_path))
    except Exception as exc:  # noqa: BLE001 - STEP ilegível é erro de job
        return _fail(f"job.validate: import_step falhou: {exc}")
    analyzer = BRepCheck_Analyzer(shape.wrapped)
    bbox = shape.bounding_box()
    solids = shape.solids() if hasattr(shape, "solids") else []
    return {
        "ok": True,
        "step_path": str(step_path),
        "sha256": _sha256(step_path),
        "file_schema": _file_schema(step_path),
        "brepcheck_valid": bool(analyzer.IsValid()),
        "is_closed": bool(shape.is_closed()) if hasattr(shape, "is_closed") else None,
        "volume_mm3": float(shape.volume),
        "solids": len(solids),
        "bbox_mm": {
            "min": [float(v) for v in bbox.min],
            "max": [float(v) for v in bbox.max],
            "size": [float(v) for v in bbox.size],
        },
    }


def _versions() -> dict:
    import build123d

    versions = {"build123d": getattr(build123d, "__version__", "unknown")}
    try:
        import OCP

        # OCP.__version__ é a versão do OCCT empacotada (ex.: 8.0.1.1);
        # o wheel chama-se cadquery-ocp-novtk mas o pacote de topo é OCP/.
        versions["OCP"] = getattr(OCP, "__version__", "unknown")
        versions["OCCT"] = versions["OCP"]
    except ImportError:
        versions["OCP"] = "not-importable"
    return versions


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps(_fail("uso: step_worker.py <job.json>")))
        return 2
    try:
        job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - job ilegível
        print(json.dumps(_fail(f"job JSON ilegível: {exc}")))
        return 2
    kind = job.get("kind")
    try:
        if kind == "export":
            result = _job_export(job)
        elif kind == "validate":
            result = _job_validate(job)
        elif kind == "versions":
            result = {"ok": True, "versions": _versions()}
        else:
            result = _fail(f"kind desconhecido: {kind!r}")
    except Exception as exc:  # noqa: BLE001 - qualquer erro vira erro de job
        result = _fail(f"worker: {type(exc).__name__}: {exc}")
    result.setdefault("worker_versions", _versions())
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
