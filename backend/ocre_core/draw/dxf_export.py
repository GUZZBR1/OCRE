"""Escritor DXF R2000 para documentos ``draw.paper.v1`` (Fase 0).

Escreve um documento ``draw.paper.v1`` (como ``dict`` puro)
como um arquivo DXF R2000, usando ezdxf.

Regras do plano (``PLANO-INTEGRACAO-OCRE-DRAW.md`` §4), implementadas aqui:

- Uma ``LWPOLYLINE`` por traço, na layer ``DRAW_STROKES``.
- ``$INSUNITS = 4`` (milímetros).
- Y invertido: o papel usa y-down (origem no canto superior esquerdo) e o
  CAD usa y-up, logo ``y_cad = altura_papel_mm - y_papel``. A altura
  usada é a EFETIVA da folha declarada no documento.
- Traço fechado: flag 70 = 1 (``closed=True`` na LWPOLYLINE).
- NUNCA CIRCLE: um círculo vetorizado continua sendo uma polilinha
  fechada; o campo ``shape`` do documento é ignorado na escrita.
- ``stroke_id`` em XDATA sob APPID próprio do Draw, ``DRAW`` (o APPID é
  criado antes de usar; NUNCA sob APPID do IngeCAD ou de outro alvo).
- Traços ``erased`` NUNCA vão para o DXF. Por padrão só ``stable`` vai;
  ``pending`` só vai se ``include_pending=True`` for pedido explicitamente.

Fail-closed: ``ValueError`` em documento sem ``strokes``, ``stroke_id``
ausente ou duplicado, status desconhecido, ponto não finito, ponto fora
da folha, ou traço fechado com menos de 3 pontos. Traços de 1 ponto não
têm representação em DXF: são pulados e registrados em ``skipped`` no
relatório (o documento continua sendo o mestre; nada é apagado dele).

Nada aqui depende da camada de IA (F3): este módulo só traduz geometria
já validada em DXF.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import ezdxf

from .paper import DEFAULT_SPEC, paper_size_mm

__all__ = [
    "APPID",
    "LAYER",
    "DEFAULT_PAPER_WIDTH_MM",
    "DEFAULT_PAPER_HEIGHT_MM",
    "DXF_VERSION",
    "DxfExportError",
    "build_dxf",
    "export_dxf",
]

APPID = "DRAW"
LAYER = "DRAW_STROKES"
DXF_VERSION = "R2000"
# Backward-compatible aliases for the default paper (A4) — the paper
# policy itself lives in ``paper.py`` (D3); _paper_size reads the
# document's own paper block, never these aliases.
DEFAULT_PAPER_WIDTH_MM = DEFAULT_SPEC.width_mm
DEFAULT_PAPER_HEIGHT_MM = DEFAULT_SPEC.effective_height_mm
# ezdxf.units.MM == 4; $INSUNITS = 4 significa milímetros.
INSUNITS_MM = 4

_STATUS_INCLUDED = {"stable"}
_STATUS_OPTIONAL = {"pending"}
_STATUS_NEVER = {"erased"}


class DxfExportError(ValueError):
    """Documento draw.paper.v1 inválido para exportação DXF (fail-closed)."""


def _paper_size(document: dict) -> tuple[float, float]:
    try:
        return paper_size_mm(document)
    except ValueError as exc:
        raise DxfExportError(str(exc)) from exc


def _validate_points(
    stroke_id: str, points: Any, closed: bool, width: float, height: float
) -> list[tuple[float, float]]:
    if not isinstance(points, (list, tuple)) or len(points) == 0:
        raise DxfExportError(f"traço {stroke_id}: points_mm vazio ou ausente")
    clean: list[tuple[float, float]] = []
    for point in points:
        # Mesma regra de document._check_points: bool e não-número
        # (str, None, ...) são recusados ANTES do float() — bool não é
        # número aqui e float() deixaria TypeError escapar cru.
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float))
                for v in point
            )
        ):
            raise DxfExportError(f"traço {stroke_id}: ponto inválido {point!r}")
        x, y = float(point[0]), float(point[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise DxfExportError(f"traço {stroke_id}: ponto não finito {point!r}")
        if not (0.0 <= x <= width and 0.0 <= y <= height):
            raise DxfExportError(
                f"traço {stroke_id}: ponto fora da folha ({x}, {y}) "
                f"para papel {width} x {height} mm"
            )
        clean.append((x, y))
    if closed and len(clean) < 3:
        raise DxfExportError(
            f"traço {stroke_id}: traço fechado com menos de 3 pontos"
        )
    return clean


def _paper_to_cad(points: list[tuple[float, float]], height_mm: float) -> list[tuple[float, float]]:
    """Papel y-down -> CAD y-up: y_cad = altura_papel_mm - y_papel."""
    return [(x, height_mm - y) for x, y in points]


def build_dxf(document: dict, *, include_pending: bool = False):
    """Constrói o documento ezdxf (R2000) a partir de ``document``.

    Devolve ``(doc, report)``; não escreve arquivo. ``report`` registra
    traços incluídos e pulados, para auditoria no sidecar.
    """
    schema = document.get("schema")
    if schema is not None and schema != "draw.paper.v1":
        raise DxfExportError(f"schema inesperado: {schema!r} (esperado 'draw.paper.v1')")
    strokes = document.get("strokes")
    if not isinstance(strokes, list):
        raise DxfExportError("documento sem lista 'strokes'")

    width_mm, height_mm = _paper_size(document)

    doc = ezdxf.new(DXF_VERSION, setup=True)
    doc.units = ezdxf.units.MM
    # Garantia explícita: $INSUNITS = 4 (mm), independente do default.
    doc.header["$INSUNITS"] = INSUNITS_MM
    if LAYER not in doc.layers:
        doc.layers.add(LAYER)
    if APPID not in doc.appids:
        doc.appids.add(APPID)
    msp = doc.modelspace()

    included: list[str] = []
    skipped: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for stroke in strokes:
        if not isinstance(stroke, dict):
            raise DxfExportError(f"traço inválido (não é objeto): {stroke!r}")
        stroke_id = stroke.get("stroke_id")
        if not isinstance(stroke_id, str) or not stroke_id:
            raise DxfExportError("traço sem stroke_id (string não vazia)")
        if stroke_id in seen_ids:
            raise DxfExportError(f"stroke_id duplicado: {stroke_id}")
        seen_ids.add(stroke_id)

        status = stroke.get("status")
        if status in _STATUS_NEVER:
            skipped.append({"stroke_id": stroke_id, "reason": "erased"})
            continue
        if status in _STATUS_OPTIONAL:
            if not include_pending:
                skipped.append({"stroke_id": stroke_id, "reason": "pending"})
                continue
        elif status not in _STATUS_INCLUDED:
            raise DxfExportError(f"traço {stroke_id}: status desconhecido {status!r}")

        points = _validate_points(
            stroke_id,
            stroke.get("points_mm"),
            bool(stroke.get("closed", False)),
            width_mm,
            height_mm,
        )
        if len(points) < 2:
            # 1 ponto não tem representação como LWPOLYLINE; o documento
            # continua sendo o mestre — registra-se o pulo, não se falha.
            skipped.append({"stroke_id": stroke_id, "reason": "single_point"})
            continue

        closed = bool(stroke.get("closed", False))
        cad_points = _paper_to_cad(points, height_mm)
        polyline = msp.add_lwpolyline(
            cad_points,
            close=closed,
            dxfattribs={"layer": LAYER},
        )
        # stroke_id em XDATA sob APPID próprio do Draw ("DRAW").
        # Grupo 1000 = string. Nunca CIRCLE: círculo vetorizado já chega
        # aqui como polilinha fechada; o campo "shape" é ignorado.
        polyline.set_xdata(APPID, [(1000, stroke_id)])
        included.append(stroke_id)

    report = {
        "dxf_version": DXF_VERSION,
        "insunits": INSUNITS_MM,
        "layer": LAYER,
        "appid": APPID,
        "paper_width_mm": width_mm,
        "paper_height_mm": height_mm,
        "entities": len(included),
        "included_stroke_ids": included,
        "skipped": skipped,
    }
    return doc, report


def export_dxf(
    document: dict,
    output_path: str | Path,
    *,
    include_pending: bool = False,
) -> dict:
    """Escreve o DXF em ``output_path`` e devolve o relatório da escrita.

    O relatório inclui ``output_path`` e o ``sha256`` do arquivo escrito,
    para o sidecar de proveniência.
    """
    doc, report = build_dxf(document, include_pending=include_pending)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(output_path)
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    report["output_path"] = str(output_path)
    report["output_sha256"] = digest
    return report
