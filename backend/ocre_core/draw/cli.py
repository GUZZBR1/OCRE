"""Local CLI for the F0 Draw integration (``ocre-draw-export``).

Runnable as::

    PYTHONPATH=backend python -m ocre_core.draw.cli export --png ... --out-dir ...
    PYTHONPATH=backend python -m ocre_core.draw.cli validate --document ...

Subcommands:

- ``export``: PNG -> vectorize (black-box subprocess, SHA-pinned;
  script-file execution or ``python -m`` via ``--vectorize-module``) ->
  ``draw.paper.v1`` document -> DXF R2000 + sidecar + DRAW-AF/1
  fingerprint. The document is validated BEFORE any derivative is
  written (fail-closed). The LibreCAD gate (G0.2) is opt-in via
  ``--librecad-check``; without the binary it reports ``unavailable``
  and does not fail the export.
- ``validate``: validate a ``draw.paper.v1`` document file and print
  the validation report as JSON (exit 0 valid, 1 invalid).
- ``fea-axial``: run the FEA axial reference case (gate G4.1) on a
  STEP file -- Gmsh mesh + CalculiX solve + validated summary +
  sidecar + fingerprint (exit 0 gate passed, 1 failed, 2 blocked).
- ``beta-run``: the whole beta v0.1 chain in one command (PNG ->
  draw.paper.v1 -> DXF -> live CAD (OCS) -> STEP 2D/3D -> axial FEA
  gate G4.1 -> physics load profile -> viz + PDF -> FMPy gate).
  Writes ``beta-report.json`` + ``beta-report.txt`` in ``--out-dir``.
  Exit 0 ok, 1 failed, 2 blocked (fail-closed: a missing declared
  datum -- thickness/material, a STEP without a closed stroke, an
  incomplete case -- blocks the job, never estimated). Optional steps
  report ``skipped-unavailable``/``not-triggered`` honestly and do
  not fail the chain. The fingerprint domain cited by the report is
  ``DRAW:PAPER-APPROVAL:FINGERPRINT`` (Senate errata, never the old
  ``DRAW-AF/1`` label).

Fingerprint inputs (DRAW-AF/1, 10 fields) — no silent defaults:

- Fields 1/2 (document digest, bundle-manifest digest) are computed
  from the artifacts of this run.
- Field 3 (profile) comes from the bundled schema lock; field 4 lists
  the draw.paper schema with the same locked digest.
- Field 5 (consumed artifacts) is the input PNG, hashed here.
- Field 6 (export_parameters) is the complete export_case object built
  from the CLI flags of this run; it is written out verbatim in
  ``fingerprint.json`` so an auditor can see exactly what was hashed.
- Field 7 (validator) pins ``validator_draw.py`` by its file SHA-256.
- Field 8 (adapters) pins the vectorize script from ``santleme/draw-core``
  (``src/draw/exporters/vectorize.py``; SHA given on the
  command line, verified before running) and the DXF writer
  (``dxf_export.py`` by file SHA-256).
- Field 9 (unit_policy) is the pinned ``mm`` policy (version 0.1.0).
- Field 10 (preparation_policy) is the normative null — recorded
  explicitly as ``null``, never a hidden default.

The same adapter list and the same export_parameters object go into
the sidecar, so fingerprint and sidecar can never disagree about what
ran. ``--draw-commit`` must be a full 40-hex git revision: the sidecar
requires it, and the document source does not accept a default origin
either — ``--origin`` is required and must be one of the three
``draw.paper.v1`` origins.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from .document import (
    SHAPES,
    add_stroke,
    document_sha256,
    dumps as dumps_document,
    loads as loads_document,
    new_document,
    stabilize_stroke,
)
from .fingerprint import compute_fingerprint
from .paper import DEFAULT_SPEC, load_draw_config, resolve_paper
from .sidecar import build_draw_sidecar
from .validator_draw import (
    SCHEMA_NAME,
    SCHEMA_VERSION,
    _canonical_bytes,
    bundled_schema_sha256,
    validate_draw_document,
    validate_draw_document_or_raise,
)
from .vectorize_runner import sha256_bytes as _sha256_bytes, sha256_file as _sha256_file

__all__ = ["main"]

VALIDATOR_ID = "draw-paper-validator"
VALIDATOR_VERSION = "0.1.0"
POLICY_VERSION = "0.1.0"
UNIT_POLICY_VERSION = "0.1.0"
DXF_ADAPTER_VERSION = "0.1.0"
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_ORIGINS = ("fork-session-v2", "upstream-vectorize", "upstream-canvas-png")


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Métricas de fidelidade do ``summary`` do vectorize que vão para o
# sidecar/fingerprint — whitelist explícita porque o summary também
# carrega chaves voláteis (``svg``/``json``: caminhos temporários que
# tornariam o fingerprint não-reprodutível entre runs).
_FIDELITY_SUMMARY_KEYS = (
    "pixels_tinta",
    "polilinhas",
    "polilinhas_fechadas",
    "polilinhas_de_um_ponto",
    "saltos_de_caneta",
    "distancias",
    "polaridade_tinta",
    "epsilon_px",
    "thinning",
    "primitivas_contagem",
)


def _png_dimensions_px(path: Path) -> tuple[int, int]:
    """(largura, altura) em px do PNG, lidos do chunk IHDR (só stdlib).

    Fail-closed: assinatura errada, arquivo curto ou IHDR fora do lugar
    são recusados — nunca se adivinha a geometria do canvas.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(33)
    except OSError as exc:
        raise ValueError(f"cannot read PNG: {path}: {exc}") from exc
    if len(header) < 33 or not header.startswith(_PNG_SIGNATURE):
        raise ValueError(f"not a PNG file (bad signature/short): {path}")
    ihdr_len = int.from_bytes(header[8:12], "big")
    if header[12:16] != b"IHDR" or ihdr_len != 13:
        raise ValueError(f"PNG sem chunk IHDR esperado: {path}")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        raise ValueError(f"PNG com dimensões inválidas {width}x{height}: {path}")
    return width, height


def _fidelity_summary(summary: Any) -> dict[str, Any]:
    """Recorte estável do ``summary`` do vectorize para o sidecar."""
    if not isinstance(summary, dict):
        return {}
    return {
        key: summary[key] for key in _FIDELITY_SUMMARY_KEYS if key in summary
    }


def _fidelity_p95_px(summary: Any) -> float | None:
    """Maior ``p95_px`` entre as direções de ``summary.distancias``."""
    if not isinstance(summary, dict):
        return None
    distancias = summary.get("distancias")
    if not isinstance(distancias, dict):
        return None
    values = [
        direction.get("p95_px")
        for direction in distancias.values()
        if isinstance(direction, dict)
        and isinstance(direction.get("p95_px"), (int, float))
        and not isinstance(direction.get("p95_px"), bool)
    ]
    return max((float(v) for v in values), default=None)


def _json_finite(value: Any) -> bool:
    """True quando a árvore JSON-like só contém números finitos.

    ``json.loads`` aceita ``NaN``/``Infinity`` literais; se entrarem no
    summary do vectorize eles chegariam ao fingerprint — que os recusa
    — DEPOIS do DXF já promovido. Validar cedo mantém fail-closed.
    """
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_json_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_json_finite(item) for item in value)
    return True


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def _fail(message: str, code: int) -> int:
    """Print ``error: <message>`` to stderr and return the exit code."""
    print(f"error: {message}", file=sys.stderr)
    return code


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _material_declared(material: Any) -> bool:
    return isinstance(material, str) and bool(material.strip())


def _module_file_sha256(module_name: str) -> str:
    """SHA-256 of a sibling module file (validator / DXF writer pin)."""
    import importlib

    module = importlib.import_module(f".{module_name}", __package__)
    path = getattr(module, "__file__", None)
    if not path:
        raise ValueError(f"cannot pin module {module_name}: no __file__")
    return _sha256_file(path)


def _resolve_ai_adapter(factory_spec: str):
    """Resolve the optional provider seam without coupling it to ledger state."""
    import importlib
    from .ai.registry import register_model, require_model
    module_name, factory_name = factory_spec.split(":", 1)
    adapter = getattr(importlib.import_module(module_name), factory_name)()
    register_model("cli-injected", adapter)
    return require_model("cli-injected")


def _build_parser() -> argparse.ArgumentParser:
    from .step_export import OPEN_STROKES_POLICIES

    parser = argparse.ArgumentParser(
        prog="ocre-draw-export",
        description="F0 Draw export: PNG -> draw.paper.v1 -> DXF R2000 + sidecar + fingerprint.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="vectorize a PNG and export DXF + sidecar + fingerprint")
    exp.add_argument("--png", required=True, help="input canvas PNG")
    exp.add_argument("--vectorize-script", required=True,
                     help="path to santleme/draw-core src/draw/exporters/vectorize.py (never copied into OCRE)")
    exp.add_argument("--vectorize-sha256", required=True,
                     help="pinned SHA-256 of the vectorize script (verified before running)")
    exp.add_argument("--vectorize-python", default=None,
                     help="python interpreter for the vectorize subprocess "
                          "(default: OCRE_VECTORIZE_PYTHON env, else the "
                          "interpreter running this CLI); the resolved path is "
                          "recorded in export_parameters")
    exp.add_argument("--vectorize-module", default=None,
                     help="invoke the vectorizer as 'python -m MODULE' instead of "
                          "executing the script file (required at draw-core HEAD, "
                          "where vectorize.py uses package-relative imports); the "
                          "pinned --vectorize-script file is still sha256-verified "
                          "before running")
    exp.add_argument("--vectorize-pythonpath", default=None,
                     help="directories prepended to PYTHONPATH for "
                          "--vectorize-module (e.g. <draw-core clone>/src); "
                          "refused without --vectorize-module")
    exp.add_argument("--out-dir", required=True, help="output directory for the derivatives")
    exp.add_argument("--origin", required=True, choices=list(_ORIGINS),
                     help="document source origin (explicit, never a default)")
    exp.add_argument("--draw-repo", required=True, help="Draw repo the artifacts came from")
    exp.add_argument("--draw-commit", required=True,
                     help="pinned Draw revision, full 40-hex git SHA (sidecar requires it)")
    exp.add_argument("--order-kind", default=None,
                     choices=("temporal-stamps", "reading-centroid", "unknown"),
                     help="how stroke order is assigned (default: "
                          "temporal-stamps when stamped primitives come "
                          "back, else unknown)")
    exp.add_argument("--stamps", default=None, metavar="STAMPS.npz",
                     help="carimbos temporais do Draw (.npz), repassados "
                          "ao vectorize; primitivas com t_start_ms/t_end_ms "
                          "implicam order_kind temporal-stamps")
    exp.add_argument("--paper", default=None,
                     help="paper format from the paper registry (operator flag; "
                          "default: the current Draw paper, today A4)")
    exp.add_argument("--ai-adapter-factory", default=None,
                     help="optional module:callable returning a ModelAdapter")
    exp.add_argument("--draw-config", default=None,
                     help="path to Draw's config.py; the paper is derived from "
                          "its PAPER_W/PAPER_H (read as text/AST, never "
                          "imported). Overrides --paper when given.")
    exp.add_argument("--doc-id", default=None, help="document UUID v4 (generated if omitted)")
    exp.add_argument("--timeout-s", type=float, default=300.0,
                     help="vectorize subprocess timeout in seconds")
    exp.add_argument("--clean", action="store_true",
                     help="request clean vectorization")
    exp.add_argument("--epsilon", type=float, default=None)
    exp.add_argument("--thinning", default=None)
    exp.add_argument("--max-p95-px", type=float, default=None,
                     help="fidelity gate: refuse the export when the "
                          "vectorizer's p95 distance (px, either "
                          "direction) exceeds this value")
    exp.add_argument("--dxf-circles", action="store_true",
                     help="opt-in: emit DXF CIRCLE entities for circle "
                          "strokes carrying the analytic block; the "
                          "result is for external CAD consumers — the "
                          "live-CAD (OCS) sync path expects LWPOLYLINE")
    exp.add_argument("--librecad-check", action="store_true",
                     help="also run the LibreCAD gate G0.2 on the DXF (unavailable without the binary)")

    val = sub.add_parser("validate", help="validate a draw.paper.v1 document file")
    val.add_argument("--document", required=True, help="path to the document JSON file")

    fea = sub.add_parser(
        "fea-axial",
        help="run the FEA axial reference case (gate G4.1) on a STEP file")
    fea.add_argument("--step", required=True,
                     help="input STEP file (reference bar for the gate; "
                          "drawing STEP later, same path)")
    fea.add_argument("--out-dir", required=True,
                     help="isolated case workdir (mesh, deck, raw solver "
                          "artifacts, summary)")
    fea.add_argument("--doc-id", default=None,
                     help="draw.paper.v1 doc id when the case belongs to a "
                          "document (default: the axial case id)")
    fea.add_argument("--document-sha256", default=None,
                     help="sha256 of the draw.paper.v1 document when there "
                          "is one (default: the STEP sha256, recorded as such)")
    fea.add_argument("--mesh-size", type=float, default=None,
                     help="Gmsh characteristic length in mm")
    fea.add_argument("--timeout-s", type=float, default=300.0,
                     help="per-subprocess timeout in seconds")

    beta = sub.add_parser(
        "beta-run",
        help="run the whole beta v0.1 chain end-to-end (PNG -> CAD -> "
             "STEP -> FEA -> physics -> viz/PDF)")
    beta.add_argument("--png", required=True, help="input canvas PNG")
    beta.add_argument("--vectorize-script", required=True,
                      help="path to santleme/draw-core src/draw/exporters/vectorize.py (never copied "
                           "into OCRE)")
    beta.add_argument("--vectorize-sha256", required=True,
                      help="pinned SHA-256 of the vectorize script "
                           "(verified before running)")
    beta.add_argument("--vectorize-python", default=None,
                      help="python interpreter for the vectorize subprocess "
                           "(default: OCRE_VECTORIZE_PYTHON env, else the "
                           "interpreter running this CLI)")
    beta.add_argument("--vectorize-module", default=None,
                      help="invoke the vectorizer as 'python -m MODULE' instead "
                           "of executing the script file (required at draw-core "
                           "HEAD, where vectorize.py uses package-relative "
                           "imports); the pinned --vectorize-script file is "
                           "still sha256-verified before running")
    beta.add_argument("--vectorize-pythonpath", default=None,
                      help="directories prepended to PYTHONPATH for "
                           "--vectorize-module (e.g. <draw-core clone>/src); "
                           "refused without --vectorize-module")
    beta.add_argument("--out-dir", required=True,
                      help="output directory (report + step subdirs)")
    beta.add_argument("--origin", required=True, choices=list(_ORIGINS),
                      help="document source origin (explicit, never a default)")
    beta.add_argument("--draw-repo", required=True,
                      help="Draw repo the artifacts came from")
    beta.add_argument("--draw-commit", required=True,
                      help="pinned Draw revision, full 40-hex git SHA")
    beta.add_argument("--clean", action="store_true",
                      help="request clean vectorization")
    beta.add_argument("--stamps", default=None, metavar="STAMPS.npz",
                      help="carimbos temporais do Draw (.npz) para o "
                           "vectorize (passo export)")
    beta.add_argument("--thickness-mm", type=float, default=None,
                      help="DECLARED extrusion thickness in mm (required; "
                           "missing blocks the job, never estimated)")
    beta.add_argument("--material", default=None,
                      help="DECLARED material name (required; missing "
                           "blocks the job, never estimated)")
    beta.add_argument("--open-strokes-policy", default="fail",
                      choices=list(OPEN_STROKES_POLICIES),
                      help="policy for open 'stable' strokes in the STEP "
                           "step: 'fail' refuses the export (default, "
                           "fail-closed); 'skip-open' records them in the "
                           "STEP report's skipped list — a document with "
                           "no closed 'stable' stroke is still refused")
    beta.add_argument("--ai-adapter-factory", default=None,
                      help="optional module:callable returning a ModelAdapter; injectable provider seam")
    beta.add_argument("--proposal-file", default=None,
                      help="persistent JSON proposal ledger (defaults to OUT_DIR/proposals.json)")
    beta.add_argument("--confirm-proposal", action="append", default=[],
                      help="proposal id explicitly confirmed by this human; repeatable")
    beta.add_argument("--reject-proposal", action="append", default=[],
                      help="proposal id explicitly rejected by this human; repeatable")
    beta.add_argument("--confirmed-by", default=None,
                      help="human identity required with --confirm-proposal")
    beta.add_argument("--cad-only", action="store_true",
                      help="stop after DXF/OCS/STEP; skip simulation, physics and visualization")
    beta.add_argument("--ocs-binary", default=None,
                      help="OpenCADStudio binary for the live-CAD step "
                           "(default: OCS_BINARY env or PATH; without it "
                           "the step is skipped-unavailable, honestly)")
    beta.add_argument("--draw-config", default=None,
                      help="path to Draw's config.py (paper source; "
                           "overrides the default paper)")
    beta.add_argument("--document", default=None,
                      help="optional draw.paper.v1 document to use as the "
                           "working document for steps 2+ (e.g. the "
                           "beta-exemplo fixture, which carries an erased "
                           "stroke to prove the eraser); without it, the "
                           "document from step 1 is the working document")
    beta.add_argument("--fmu", default=None,
                      help="optional .fmu file for the FMPy gate; without "
                           "it the gate reports not-triggered")
    beta.add_argument("--timeout-s", type=float, default=300.0,
                      help="per-subprocess timeout in seconds")
    return parser


def _cmd_export(args: argparse.Namespace) -> int:
    # Lazy imports: `validate` must work without ezdxf installed.
    from .dxf_export import export_dxf
    from .librecad_gate import check_dxf
    from .vectorize_runner import (
        VectorizeError,
        _resolve_pythonpath,
        run_vectorize,
    )
    if not _HEX40_RE.fullmatch(args.draw_commit):
        return _fail(
            "--draw-commit must be a full 40-hex git revision "
            f"(got {args.draw_commit!r}); the sidecar requires it", 2)
    if not (math.isfinite(args.timeout_s) and args.timeout_s > 0):
        return _fail(
            f"--timeout-s must be positive and finite "
            f"(got {args.timeout_s!r})", 2)
    # --vectorize-pythonpath sem --vectorize-module não faria efeito
    # nenhum (o PYTHONPATH só é injetado na invocação "python -m"):
    # combinação sem sentido é recusada antes de qualquer escrita.
    if args.vectorize_pythonpath and not args.vectorize_module:
        return _fail(
            "--vectorize-pythonpath requires --vectorize-module", 2)
    png_path = Path(args.png)
    if not png_path.is_file():
        return _fail(f"PNG not found: {png_path}", 2)
    # --max-p95-px is parsed as float, which accepts nan/inf: a nan
    # limit never blocks the gate and would land in export_parameters
    # (the fingerprint refuses non-finite numbers) — after the DXF was
    # already promoted. Refuse before anything is written.
    max_p95_px = getattr(args, "max_p95_px", None)
    if max_p95_px is not None and not math.isfinite(max_p95_px):
        print(
            f"error: --max-p95-px must be a finite number, got "
            f"{max_p95_px!r}",
            file=sys.stderr,
        )
        return 2
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Paper policy (D3): single source in paper.py. Resolve BEFORE the
    # vectorize subprocess so an unknown format / unreadable config
    # fails fast, with nothing written. --draw-config overrides --paper.
    try:
        spec = load_draw_config(args.draw_config) if args.draw_config else resolve_paper(args.paper)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return _fail(f"cannot resolve paper: {exc}", 2)

    # The PNG canvas must match the resolved sheet exactly: a landscape
    # or foreign-size canvas vectorized against the wrong grid would
    # produce silently wrong mm. Fail closed before the subprocess.
    try:
        png_w_px, png_h_px = _png_dimensions_px(png_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if (png_w_px, png_h_px) != (spec.canvas_width_px, spec.canvas_height_px):
        print(
            f"error: PNG {png_w_px}x{png_h_px} px does not match the "
            f"{spec.format} canvas {spec.canvas_width_px}x"
            f"{spec.canvas_height_px} px; refusing — the vectorizer mm "
            "scale would be wrong",
            file=sys.stderr,
        )
        return 2

    stamps_path = getattr(args, "stamps", None)
    stamps_sha256 = None
    if stamps_path is not None:
        stamps_path = Path(stamps_path)
        if not stamps_path.is_file():
            print(f"error: stamps file not found: {stamps_path}", file=sys.stderr)
            return 2
        stamps_sha256 = _sha256_file(stamps_path)

    png_sha256 = _sha256_file(png_path)

    # The pinned vectorize hardcodes A4 retrato; other sheets need the
    # --paper-w-mm/--paper-h-mm flags. Sent only for non-default specs
    # so the A4 path keeps working against today's pinned script (the
    # flags make older scripts fail loudly, never silently).
    paper_mm = (
        (spec.width_mm, spec.effective_height_mm)
        if spec is not DEFAULT_SPEC
        else None
    )

    try:
        vectorized = run_vectorize(
            png_path,
            args.vectorize_script,
            args.vectorize_sha256,
            output_dir=None,  # temp dir, cleaned up; only polylines are kept
            timeout_s=args.timeout_s,
            clean=args.clean,
            epsilon=args.epsilon,
            thinning=args.thinning,
            stamps_path=stamps_path,
            paper_mm=paper_mm,
            vectorize_python=args.vectorize_python,
            module=args.vectorize_module,
            pythonpath=args.vectorize_pythonpath,
        )
    except VectorizeError as exc:
        return _fail(f"vectorize failed: {exc}", 1)

    # O recorte do summary vai para export_parameters (fingerprint):
    # números não-finitos seriam recusados lá, depois do DXF promovido.
    fidelity_summary = _fidelity_summary(vectorized.get("summary"))
    if not _json_finite(fidelity_summary):
        return _fail(
            "vectorizer summary carries non-finite numbers; refusing "
            "to build a package that cannot be fingerprinted", 1,
        )

    if max_p95_px is not None:
        p95_px = _fidelity_p95_px(vectorized.get("summary"))
        if p95_px is None:
            print(
                "error: --max-p95-px given but the vectorizer summary "
                "carries no distancias.p95_px; the fidelity gate cannot "
                "be checked, refusing",
                file=sys.stderr,
            )
            return 1
        if p95_px > max_p95_px:
            print(
                f"error: vectorization fidelity gate failed: p95 "
                f"{p95_px} px exceeds --max-p95-px {max_p95_px}",
                file=sys.stderr,
            )
            return 1

    polylines = vectorized["polylines"]
    # Stamped primitives require the temporal-stamps order (the
    # validator refuses timestamps without it); without stamps the
    # order stays as asked (default unknown).
    has_stamps = any(
        "t_start_ms" in polyline or "t_end_ms" in polyline
        for polyline in polylines
    )
    if has_stamps and args.order_kind not in (None, "temporal-stamps"):
        print(
            f"error: vectorizer returned stamped primitives but "
            f"--order-kind is {args.order_kind!r}; stamped output "
            "requires temporal-stamps",
            file=sys.stderr,
        )
        return 2
    order_kind = args.order_kind or (
        "temporal-stamps" if has_stamps else "unknown"
    )
    # Build the document (master) from the vectorizer polylines.
    doc = new_document(
        {"repo": args.draw_repo, "commit": args.draw_commit, "origin": args.origin},
        order_kind=order_kind,
        doc_id=args.doc_id,
        paper=spec,
    )
    try:
        for polyline in polylines:
            shape = polyline.get("type")
            # O fit analítico do círculo (cx_mm/cy_mm/r_mm) sobe ao
            # documento mestre — a Fase 2 o usa no STEP analítico.
            # add_stroke valida na folha e implica shape="circle".
            circle_mm = polyline.get("circle_mm")
            circle = (
                {key: circle_mm[key] for key in ("cx_mm", "cy_mm", "r_mm")}
                if circle_mm is not None
                else None
            )
            stroke = add_stroke(
                doc,
                polyline["points_mm"],
                closed=bool(polyline.get("closed", False)),
                shape=shape if shape in SHAPES else None,
                circle=circle,
                t_start_ms=polyline.get("t_start_ms"),
                t_end_ms=polyline.get("t_end_ms"),
            )
            # Vectorizer output is the confirmed layer: stable, so the
            # DXF writer includes it with its default (stable-only) rule.
            stabilize_stroke(doc, stroke["stroke_id"])
    except (ValueError, KeyError) as exc:
        return _fail(f"cannot build document from vectorizer output: {exc}", 1)

    # Fail-closed: validate BEFORE writing any derivative.
    try:
        validate_draw_document_or_raise(doc)
    except ValueError as exc:
        return _fail(f"document failed validation, nothing written: {exc}", 1)

    doc_sha_hex = document_sha256(doc)
    schema_sha = bundled_schema_sha256()

    vectorize_parameters: dict[str, Any] = {
        "script_sha256": vectorized["script_sha256"],
        "python": vectorized["python_path"],
        "primitives_total": vectorized["primitives_total"],
        "polylines_total": vectorized["polylines_total"],
        "primitives_without_points": vectorized["primitives_without_points"],
        "skipped_primitive_types": vectorized["skipped_primitive_types"],
        "summary": fidelity_summary,
    }
    if stamps_sha256 is not None:
        vectorize_parameters["stamps_sha256"] = stamps_sha256
    if args.vectorize_module is not None:
        # O modo de invocação faz parte do registro do que rodou; sem a
        # flag o modo arquivo não deixa rastro (status quo).
        vectorize_parameters["module"] = args.vectorize_module
        if vectorized.get("module_origin") is not None:
            vectorize_parameters["module_origin"] = vectorized["module_origin"]
        if args.vectorize_pythonpath is not None:
            vectorize_parameters["pythonpath"] = _resolve_pythonpath(
                args.vectorize_pythonpath)
    if paper_mm is not None:
        vectorize_parameters["paper_mm"] = list(paper_mm)
    if max_p95_px is not None:
        vectorize_parameters["max_p95_px"] = max_p95_px

    export_parameters: dict[str, Any] = {
        "target": "dxf-r2000",
        "origin": args.origin,
        "order_kind": order_kind,
        "include_pending": False,
        "emit_circles": bool(args.dxf_circles),
        "strokes_stabilized": True,
        "paper": {"format": spec.format, "width_mm": spec.width_mm,
                  "height_mm": spec.height_mm,
                  "effective_width_mm": spec.effective_width_mm,
                  "effective_height_mm": spec.effective_height_mm},
        "vectorize": vectorize_parameters,
    }

    consumed_artifacts: list[dict[str, Any]] = [
        {"artifact_id": "canvas-png-1", "sha256": png_sha256}
    ]
    source_artifacts: list[dict[str, Any]] = [
        {
            "artifact_id": "canvas-png-1",
            "uri": png_path.name,
            "sha256": png_sha256,
            "origin": args.origin,
            "artifact_kind": "canvas_png",
        }
    ]
    if stamps_path is not None:
        consumed_artifacts.append(
            {"artifact_id": "canvas-stamps-1", "sha256": stamps_sha256}
        )
        source_artifacts.append(
            {
                "artifact_id": "canvas-stamps-1",
                "uri": stamps_path.name,
                "sha256": stamps_sha256,
                "origin": args.origin,
                "artifact_kind": "stamps_npz",
            }
        )

    adapters: list[dict[str, Any]] = [
        {
            "stage_id": "vectorize",
            "adapter_id": "draw.vectorize",
            "version": args.draw_commit,
            "binary_sha256": vectorized["script_sha256"],
            "capabilities_sha256": _sha256_bytes(_canonical_bytes(
                {"argv": ["--json", "--out"], "input": "png", "output": "json+svg"})),
        },
        {
            "stage_id": "export",
            "adapter_id": "draw.export.dxf",
            "version": DXF_ADAPTER_VERSION,
            "binary_sha256": _module_file_sha256("dxf_export"),
            "capabilities_sha256": _sha256_bytes(_canonical_bytes(
                {"appid": "DRAW", "dxf_version": "R2000", "insunits": 4,
                 "layer": "DRAW_STROKES"})),
        },
    ]

    # DXF first (to a temp file — promoted only after every metadata
    # file is ready), so its hash can go into the bundle manifest and
    # the sidecar. Promoting earlier would leave a new DXF over an old
    # package when a later stage (fingerprint, sidecar) fails.
    dxf_tmp = out_dir / ".document.dxf.tmp"
    dxf_path = out_dir / "document.dxf"
    try:
        dxf_report = export_dxf(
            doc, dxf_tmp, include_pending=False,
            emit_circles=args.dxf_circles)
    except ValueError as exc:
        dxf_tmp.unlink(missing_ok=True)
        return _fail(f"DXF export failed: {exc}", 1)
    adapter_error: str | None = None
    adapter = getattr(args, "ai_adapter", None)
    if adapter is None and getattr(args, "ai_adapter_factory", None):
        try:
            adapter = _resolve_ai_adapter(args.ai_adapter_factory)
        except Exception as exc:
            # Explicit factory that fails to resolve is recorded, never
            # swallowed silently (beta-run records it the same way).
            adapter_error = f"{type(exc).__name__}: {exc}"
    if adapter is not None or getattr(args, "observe_conversions", False) or getattr(args, "ai_adapter_factory", None):
        from .ai.observer import observe_conversion
        observation = observe_conversion(doc, adapter, stage="dxf",
                                         artifacts={dxf_path.name: dxf_tmp.read_bytes()})
        if observation.get("status") == "confirmed-error":
            dxf_tmp.unlink(missing_ok=True)
            print(json.dumps({"status": "failed", "observation": observation}))
            return _fail("DXF observer confirmed a conversion error", 1)
        dxf_report["observation"] = observation
    dxf_sha_hex = dxf_report["output_sha256"]

    manifest = {
        "document_sha256": doc_sha_hex,
        "dxf_sha256": dxf_sha_hex,
        "png_sha256": png_sha256,
        "export_parameters": export_parameters,
    }
    manifest_sha = _sha256_bytes(_canonical_bytes(manifest))

    fingerprint_inputs: dict[str, Any] = {
        "draw_document_sha256": bytes.fromhex(doc_sha_hex),
        "bundle_manifest_sha256": bytes.fromhex(manifest_sha),
        "profile": {"id": SCHEMA_NAME, "version": SCHEMA_VERSION, "sha256": schema_sha},
        "schemas": [{"document_kind": "draw.paper", "schema_version": SCHEMA_VERSION,
                     "schema_sha256": schema_sha}],
        "consumed_artifacts": consumed_artifacts,
        "export_parameters": export_parameters,
        "validator": {"id": VALIDATOR_ID, "version": VALIDATOR_VERSION,
                      "implementation_sha256": _module_file_sha256("validator_draw"),
                      "policy_version": POLICY_VERSION},
        "adapters": adapters,
        "unit_policy": {"id": "mm", "version": UNIT_POLICY_VERSION,
                        "sha256": _sha256_bytes(_canonical_bytes(
                            {"id": "mm", "version": UNIT_POLICY_VERSION}))},
        "preparation_policy": None,
    }
    try:
        fingerprint = compute_fingerprint(fingerprint_inputs)
    except ValueError as exc:
        dxf_tmp.unlink(missing_ok=True)
        return _fail(f"DRAW-AF/1 fingerprint failed: {exc}", 1)

    try:
        sidecar = build_draw_sidecar(
            doc_id=doc["doc_id"],
            document_sha256=doc_sha_hex,
            revision=doc["revision"],
            source_artifacts=source_artifacts,
            derived_artifacts=[{
                "artifact_id": "dxf-r2000-1",
                "uri": "document.dxf",
                "sha256": dxf_sha_hex,
                "format": "dxf-r2000",
            }],
            adapters=adapters,
            export_parameters=export_parameters,
            fingerprint_digest=fingerprint.digest,
            draw_source={"repo": args.draw_repo, "commit": args.draw_commit},
        )
    except ValueError as exc:
        dxf_tmp.unlink(missing_ok=True)
        return _fail(f"sidecar failed: {exc}", 1)

    # Every metadata input is ready: promote the DXF and write the
    # remaining derivatives atomically.
    os.replace(dxf_tmp, dxf_path)
    dxf_report["output_path"] = str(dxf_path)
    _atomic_write_bytes(out_dir / "document.json", dumps_document(doc).encode("utf-8"))
    _atomic_write_json(out_dir / "sidecar.json", sidecar)
    _atomic_write_json(out_dir / "fingerprint.json", {
        "algorithm": "DRAW-AF/1",
        "digest": fingerprint.digest,
        "encoded_hex": fingerprint.encoded.hex(),
        "inputs": {
            "draw_document_sha256": doc_sha_hex,
            "bundle_manifest_sha256": manifest_sha,
            "profile": fingerprint_inputs["profile"],
            "schemas": fingerprint_inputs["schemas"],
            "consumed_artifacts": fingerprint_inputs["consumed_artifacts"],
            "export_parameters": export_parameters,
            "validator": fingerprint_inputs["validator"],
            "adapters": adapters,
            "unit_policy": fingerprint_inputs["unit_policy"],
            "preparation_policy": None,
        },
    })

    librecad_status = "not-run"
    librecad: dict[str, Any] | None = None
    if args.librecad_check:
        try:
            librecad = check_dxf(dxf_path)
        except Exception as exc:  # noqa: BLE001 - gate timeout/IO is a failure
            return _fail(f"LibreCAD gate failed: {exc}", 1)
        librecad_status = str(librecad.get("status"))

    summary: dict[str, Any] = {
        "status": "ok",
        "doc_id": doc["doc_id"],
        "revision": doc["revision"],
        "document_sha256": doc_sha_hex,
        "dxf_sha256": dxf_sha_hex,
        "fingerprint_digest": fingerprint.digest,
        "dxf_report": dxf_report,
        "librecad_gate": librecad_status,
        "librecad": librecad,
        "ai_adapter_error": adapter_error,
        "out_dir": str(out_dir),
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    if librecad_status == "fail":
        return _fail("LibreCAD gate G0.2 failed for the exported DXF", 1)
    return 0


def _cmd_fea_axial(args: argparse.Namespace) -> int:
    from .fea import AxialCaseSpec, run_axial_case
    out_dir = Path(args.out_dir).resolve()
    spec = AxialCaseSpec(
        step_path=args.step,
        timeout_gmsh_s=args.timeout_s, timeout_ccx_s=args.timeout_s)
    result = run_axial_case(
        args.step, out_dir, spec=spec, doc_id=args.doc_id,
        document_sha256=args.document_sha256,
        mesh_size_mm=args.mesh_size)
    result_json = json.dumps(result, sort_keys=True, indent=2)
    out_path = out_dir / "fea-axial-result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result_json, encoding="utf-8")
    print(result_json)
    return {"passed": 0, "blocked": 2}.get(result["status"], 1)


def _cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.document)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _fail(f"cannot read document: {exc}", 2)
    report = validate_draw_document(raw)
    print(json.dumps(report.as_dict(), sort_keys=True, indent=2))
    return 0 if report.valid else 1


# ---------------------------------------------------------------------------
# beta-run: the whole beta v0.1 chain in one command.
#
# Step order (each step records what ran, what failed, timings and the
# document revision it saw; the final report is beta-report.json + .txt):
#   1. export      PNG -> draw.paper.v1 (F0 export) -> DXF R2000
#   2. ocs         live CAD: sync_strokes + scan + sync_document (eraser)
#   3. step        2D -> 3D: closed stable stroke -> STEP (build123d)
#   4. fea         Gmsh mesh -> CalculiX axial case, gate G4.1 (<= 1%)
#   5. physics     report step: PyBullet envelope + MuJoCo cross-check
#   6. viz         PyVista .png (visualization only) + .glb + PDF
#   7. fmu         FMPy gate (not-triggered without an FMU)
#
# The working document for steps 2+ is --document when given (the
# beta-exemplo fixture carries an erased stroke, so the OCS step can
# prove the eraser), else the document step 1 built. Both documents
# are recorded in the report with their SHAs. The CAD loop is the
# quality bar: a line on paper -> the line in CAD, 2D and 3D, with
# the eraser working. Simulations are a consequence: the axial FEA
# case only validates the whole path (G4.1), it is not the centre.
# The physics load profile is an artifact only -- the FEA gate is
# NOT rewired to use it (that wiring is future work).
# ---------------------------------------------------------------------------

BETA_FINGERPRINT_DOMAIN = "DRAW:PAPER-APPROVAL:FINGERPRINT"
_BETA_STEP_IDS = ("export", "ocs", "step", "fea", "physics", "viz", "fmu")
# Step ids --cad-only never requests (everything after the CAD loop).
_BETA_SIM_STEP_IDS = frozenset(_BETA_STEP_IDS[3:])

# run_axial_case status -> step status.
_FEA_STEP_STATUS = {"passed": "ok", "failed": "failed", "blocked": "blocked"}
# validate_fmu status -> step status.
_FMU_STEP_STATUS = {
    "pass": "ok",
    "fail": "failed",
    "blocked": "blocked",
    "not-triggered": "not-triggered",
    "unavailable": "skipped-unavailable",
}


def _beta_step_report(
    step_id: str,
    status: str,
    duration_s: float,
    detail: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "id": step_id,
        "status": status,
        "duration_s": round(float(duration_s), 3),
    }
    if detail is not None:
        report["detail"] = detail
    if error is not None:
        report["error"] = error
    return report


def _beta_step_done(
    step_id: str,
    status: str,
    started: float,
    detail: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """``_beta_step_report`` with the duration measured from ``started``."""
    return _beta_step_report(
        step_id, status, time.perf_counter() - started, detail, error=error)


def _doc_entry(doc: dict[str, Any], sha256: str) -> dict[str, Any]:
    """The {doc_id, revision, sha256} record under report["documents"]."""
    return {
        "doc_id": doc["doc_id"],
        "revision": doc["revision"],
        "sha256": sha256,
    }


def _beta_artifact_entry(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if target.is_file():
        return {
            "path": str(target),
            "sha256": _sha256_file(target),
            "bytes": target.stat().st_size,
        }
    return {"path": str(target), "sha256": None,
            "note": "file not present"}


def _beta_step_export(ctx: dict[str, Any]) -> dict[str, Any]:
    """Step 1: PNG -> draw.paper.v1 (F0 export) -> DXF R2000."""
    args = ctx["args"]
    started = time.perf_counter()
    export_dir = ctx["out_dir"] / "export"
    if args.document and Path(args.document).resolve() == (export_dir / "document.json").resolve():
        export_dir = ctx["out_dir"] / "export-rerun"
    export_dir.mkdir(parents=True, exist_ok=True)
    namespace = argparse.Namespace(
        png=args.png,
        vectorize_script=args.vectorize_script,
        vectorize_sha256=args.vectorize_sha256,
        vectorize_python=args.vectorize_python,
        vectorize_module=args.vectorize_module,
        vectorize_pythonpath=args.vectorize_pythonpath,
        out_dir=str(export_dir),
        origin=args.origin,
        draw_repo=args.draw_repo,
        draw_commit=args.draw_commit,
        order_kind=None,
        paper=None,
        draw_config=args.draw_config,
        doc_id=None,
        timeout_s=args.timeout_s,
        clean=args.clean,
        epsilon=None,
        thinning=None,
        max_p95_px=None,
        stamps=getattr(args, "stamps", None),
        # DXFs do beta servem ao fluxo live-CAD (sync por vértices):
        # CIRCLE opt-in não se aplica aqui — sempre LWPOLYLINE.
        dxf_circles=False,
        librecad_check=True,
        ai_adapter=ctx.get("ai_adapter"),
        observe_conversions=ctx["observe_conversions"],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = _cmd_export(namespace)
    try:
        summary = json.loads(buf.getvalue())
    except ValueError:
        summary = {}
    if code != 0:
        return _beta_step_done(
            "export", "failed", started,
            {"returncode": code, "summary": summary},
            error=f"export subcommand exited with code {code}",
        )
    doc_path = export_dir / "document.json"
    try:
        doc = loads_document(doc_path.read_bytes())
    except (OSError, ValueError) as exc:
        return _beta_step_done(
            "export", "failed", started,
            {"returncode": code, "summary": summary},
            error=f"export succeeded but document.json could not be "
            f"read back: {exc}",
        )
    ctx["export_doc"] = doc
    ctx["export_doc_sha256"] = document_sha256(doc)
    ctx["export_dir"] = export_dir
    detail = {
        "doc_id": doc["doc_id"],
        "revision": doc["revision"],
        "document_sha256": ctx["export_doc_sha256"],
        "dxf_sha256": summary.get("dxf_sha256"),
        "fingerprint_digest": summary.get("fingerprint_digest"),
        "librecad_gate": summary.get("librecad_gate"),
        "strokes": len(doc.get("strokes", [])),
    }
    observation = summary.get("dxf_report", {}).get("observation")
    if observation is not None:
        detail["observation"] = observation
    return _beta_step_done("export", "ok", started, detail)


def _beta_step_ocs(ctx: dict[str, Any]) -> dict[str, Any]:
    """Step 2: live CAD (OCS headless) + eraser proof.

    Without a binary the step is ``skipped-unavailable`` and the
    chain goes on by file -- never faked. With a binary: the working
    document's DXF is opened, ``sync_strokes`` preserves the ids
    (proven by ``scan_stroke_ids``) and ``sync_document`` proves the
    erased strokes absent (``erased`` when the CAD still had them,
    ``already_absent`` when the F0 DXF never carried them -- erased
    strokes never go to DXF by the F0 rule) and runs ``save_verified``.
    """
    from .dxf_export import export_dxf
    from .ocs import OcsError, find_ocs_binary, launch_headless, stop_server

    args = ctx["args"]
    started = time.perf_counter()
    binary = find_ocs_binary(args.ocs_binary)
    if binary is None:
        return _beta_step_done(
            "ocs", "skipped-unavailable", started,
            {"reason": "no OpenCADStudio binary: --ocs-binary not given "
             "and neither OCS_BINARY nor PATH provides one; the live-CAD "
             "step was not run (honest skip, not an approval); the chain "
             "continues by file"},
        )
    doc = ctx["doc"]
    detail: dict[str, Any] = {"binary": binary}
    ocs_dir = ctx["out_dir"] / "ocs"
    ocs_dir.mkdir(parents=True, exist_ok=True)
    dxf_path = ocs_dir / "document.dxf"
    try:
        dxf_report = export_dxf(doc, dxf_path)
    except ValueError as exc:
        return _beta_step_done(
            "ocs", "failed", started, detail,
            error=f"could not export the working document to DXF for "
            f"the OCS step: {exc}",
        )
    ctx["ocs_dxf"] = dxf_path
    # The CAD live set is exactly what the DXF carried: pending strokes
    # and 1-point strokes stay in the document master but never reach
    # the CAD — they are live_not_in_cad, not divergences.
    live_ids = dxf_report["included_stroke_ids"]
    erased_ids = [s["stroke_id"] for s in doc.get("strokes", [])
                  if s.get("status") == "erased"]
    try:
        process, client = launch_headless(binary)
    except OcsError as exc:
        detail["code"] = exc.code
        return _beta_step_done(
            "ocs", "failed", started, detail,
            error=f"could not launch the headless OpenCADStudio server: "
            f"{exc}",
        )
    try:
        try:
            sync_report = client.sync_strokes(
                dxf_path, live_ids,
                out_path=ocs_dir / "ocs-verified.dxf")
            scan_after_sync = client.scan_stroke_ids()
            sync_doc_report = client.sync_document(
                doc, out_path=ocs_dir / "ocs-after-erase.dxf",
                expected_live=live_ids)
            scan_after_document = client.scan_stroke_ids()
        except OcsError as exc:
            detail.update(code=exc.code, dxf_report=dxf_report)
            return _beta_step_done(
                "ocs", "failed", started, detail,
                error=f"live-CAD sync failed: {exc}",
            )
    finally:
        stop_server(process)
    if set(scan_after_document) != set(live_ids):
        detail.update(
            sync=sync_report,
            scan_after_sync=scan_after_sync,
            scan_after_sync_document=scan_after_document,
            live_ids=live_ids,
        )
        return _beta_step_done(
            "ocs", "failed", started, detail,
            error="scan after sync_document does not match the live "
            f"stroke ids: CAD has {sorted(scan_after_document)}, "
            f"document has {sorted(live_ids)}",
        )
    detail.update(
        dxf_report=dxf_report,
        live_ids=live_ids,
        erased_ids_in_document=erased_ids,
        sync=sync_report,
        scan_after_sync=scan_after_sync,
        sync_document=sync_doc_report,
        scan_after_sync_document=scan_after_document,
        eraser_proof=(
            "sync_document proved the erased strokes absent "
            f"(erased now: {sync_doc_report.get('erased')}, already "
            f"absent: {sync_doc_report.get('already_absent')})"
            if erased_ids else
            "no erased stroke in the working document: the eraser "
            "proof is not-triggered for this run (sync_document still "
            "ran and saved the verified file)"
        ),
    )
    return _beta_step_done("ocs", "ok", started, detail)


def _beta_step_step(ctx: dict[str, Any]) -> dict[str, Any]:
    """Step 3: 2D -> 3D: closed stable stroke -> STEP (build123d).

    Thickness and material are DECLARED (checked in preflight); a
    document without a closed stable stroke blocks the job (the
    exporter refuses fail-closed) instead of the chain estimating a
    solid. The FreeCAD oracle runs when FREECAD_CMD exists, else the
    step reports it ``unavailable`` and relies on the worker BRepCheck.
    """
    from .step_export import (
        StepExportError,
        StepWorkerError,
        document_to_step,
        oracle_step,
        validate_step,
    )

    args = ctx["args"]
    started = time.perf_counter()
    step_dir = ctx["out_dir"] / "step"
    step_dir.mkdir(parents=True, exist_ok=True)
    step_path = step_dir / "drawing.step"
    final_step_path = step_path
    staging = ctx["observe_conversions"]
    temp_context = None
    if staging:
        temp_context = tempfile.TemporaryDirectory(dir=step_dir)
        step_path = Path(temp_context.name) / "drawing.step"
    try:
        report = document_to_step(
            ctx["doc"], args.thickness_mm, args.material, step_path,
            timeout_s=args.timeout_s,
            open_strokes_policy=args.open_strokes_policy)
    except StepExportError as exc:
        if temp_context is not None:
            temp_context.cleanup()
        return _beta_step_done(
            "step", "blocked", started,
            {"thickness_mm": args.thickness_mm,
             "material": args.material,
             "open_strokes_policy": args.open_strokes_policy},
            error=f"STEP export refused (fail-closed): {exc}",
        )
    except StepWorkerError as exc:
        if temp_context is not None:
            temp_context.cleanup()
        return _beta_step_done(
            "step", "failed", started,
            {"thickness_mm": args.thickness_mm,
             "material": args.material,
             "open_strokes_policy": args.open_strokes_policy},
            error=f"STEP worker failed: {exc}",
        )
    if staging:
        from .ai.observer import observe_conversion
        observation = observe_conversion(ctx["doc"], ctx["ai_adapter"], stage="step",
                                        artifacts={step_path.name: step_path.read_bytes()})
        ctx["step_observation"] = observation
        if observation.get("status") == "confirmed-error":
            temp_context.cleanup()
            return _beta_step_done("step", "failed", started,
                                     {"step": report, "observation": observation},
                                     error="STEP observer confirmed a conversion error")
    try:
        brepcheck = validate_step(step_path, timeout_s=args.timeout_s)
    except StepWorkerError as exc:
        if temp_context is not None:
            temp_context.cleanup()
        return _beta_step_done(
            "step", "failed", started,
            {"step": report},
            error=f"BRepCheck failed: {exc}",
        )
    if brepcheck.get("brepcheck_valid") is not True:
        # O passo depende do BRepCheck do worker (docstring): um STEP
        # que o próprio kernel recusa não passa — com ou sem oráculo.
        if temp_context is not None:
            temp_context.cleanup()
        return _beta_step_done(
            "step", "failed", started,
            {"step": report, "brepcheck": brepcheck},
            error="worker BRepCheck says the STEP is not valid",
        )
    try:
        oracle = oracle_step(step_path, timeout_s=args.timeout_s)
    except StepWorkerError as exc:
        if temp_context is not None:
            temp_context.cleanup()
        return _beta_step_done(
            "step", "failed", started,
            {"step": report, "brepcheck": brepcheck},
            error=f"FreeCAD oracle failed: {exc}",
        )
    if oracle.get("status") == "fail":
        if temp_context is not None:
            temp_context.cleanup()
        return _beta_step_done(
            "step", "failed", started,
            {"step": report, "brepcheck": brepcheck, "oracle": oracle},
            error="FreeCAD oracle says the STEP is not valid",
        )
    if staging:
        manifest = Path(report["material_manifest_path"])
        final_manifest = final_step_path.with_suffix(".step.manifest.json")
        os.replace(step_path, final_step_path)
        os.replace(manifest, final_manifest)
        temp_context.cleanup()
        step_path = final_step_path
        report["output_path"] = str(step_path)
        report["material_manifest_path"] = str(final_manifest)
    detail = {"step": report, "brepcheck": brepcheck, "oracle": oracle}
    if ctx.get("step_observation") is not None:
        detail["observation"] = ctx["step_observation"]
    return _beta_step_done("step", "ok", started, detail)


def _beta_step_fea(ctx: dict[str, Any]) -> dict[str, Any]:
    """Step 4: Gmsh mesh -> CalculiX axial case, gate G4.1 (<= 1%).

    The case runs on the REFERENCE bar STEP (built by Gmsh), not on
    the drawing STEP: the gate's references (Rx 2100 N, S11 21 MPa)
    belong to the 100x10x10 mm bar. The drawing STEP is validated by
    step 3 (BRepCheck + FreeCAD oracle). A gate outside 1% marks the
    run ``failed`` and the command exits non-zero.
    """
    from .fea import build_bar_step, run_axial_case

    args = ctx["args"]
    started = time.perf_counter()
    fea_dir = ctx["out_dir"] / "fea"
    built = build_bar_step(fea_dir / "bar", timeout_s=args.timeout_s)
    if not built["ok"]:
        return _beta_step_done(
            "fea", "failed", started,
            {"build": built},
            error=f"could not build the reference bar STEP: "
            f"{built.get('failure')}",
        )
    case_dir = fea_dir / "case"
    case_dir.mkdir(parents=True, exist_ok=True)
    result = run_axial_case(
        built["step_path"], case_dir,
        doc_id=ctx["doc"]["doc_id"],
        document_sha256=ctx["doc_sha256"])
    (case_dir / "fea-axial-result.json").write_text(
        json.dumps(result, sort_keys=True, indent=2), encoding="utf-8")
    ctx["fea_result"] = result
    ctx["fea_dir"] = case_dir
    ctx["fea_input_step"] = case_dir / "input.step"
    status = _FEA_STEP_STATUS.get(result["status"], "failed")
    detail = {
        "case": result["case"],
        "gate": result["gate"],
        "measured": result["measured"],
        "errors_pct": result["errors_pct"],
        "fingerprint": result["fingerprint"],
        "artifacts": result["artifacts"],
    }
    error = None
    if status != "ok":
        error = "; ".join(result.get("failures") or
                          [f"axial case status: {result['status']}"])
    return _beta_step_done("fea", status, started, detail, error=error)


def _run_physics_subprocess(report_path: Path, timeout_s: float) -> dict[str, Any]:
    """Roda o caso de contato como SUBPROCESSO e devolve o relatório lido.

    Fronteira: ``python -m ocre_core.draw.physics --out <json>`` com
    timeout obrigatório — a regra do próprio módulo physics é "nunca
    em processo" (pybullet/mujoco/pinocchio são C++; um segfault de
    engine mata o subprocesso, nunca o beta-run). Devolve o JSON que
    o subprocesso escreveu; qualquer falha vira RuntimeError.
    """
    backend_dir = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = backend_dir + os.pathsep + env.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "ocre_core.draw.physics",
             "--out", str(report_path)],
            capture_output=True, text=True, timeout=timeout_s,
            shell=False, check=False, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"physics subprocess excedeu o timeout de {timeout_s}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"could not start the physics subprocess: {exc}"
        ) from exc
    if completed.returncode != 0 or not report_path.is_file():
        raise RuntimeError(
            f"physics subprocess failed (returncode "
            f"{completed.returncode}): "
            f"{(completed.stderr or completed.stdout or '')[-2000:]}"
        )
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"physics report unreadable: {exc}") from exc


def _beta_step_physics(ctx: dict[str, Any]) -> dict[str, Any]:
    """Step 5 (report step): PyBullet envelope + MuJoCo cross-check.

    The engines run in a subprocess (``_run_physics_subprocess``), per
    the physics module's own rule — never in-process. The load profile
    is returned as an artifact; the FEA gate is NOT rewired to use it
    (that wiring is future work -- the gate stays the declared axial
    case).
    """
    started = time.perf_counter()
    phys_dir = ctx["out_dir"] / "physics"
    phys_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = _run_physics_subprocess(
            phys_dir / "physics.json", ctx["args"].timeout_s)
    except Exception as exc:  # noqa: BLE001 - engine/model failure is a step failure
        return _beta_step_done(
            "physics", "failed", started,
            {}, error=f"physics contact case failed: "
            f"{type(exc).__name__}: {exc}",
        )
    profiles: list[str] = []
    for engine in ("pybullet", "mujoco"):
        profile = report.get(f"load_profile_{engine}")
        if profile is not None:
            (phys_dir / f"load-profile-{engine}.json").write_text(
                json.dumps(profile, sort_keys=True, indent=2),
                encoding="utf-8")
            profiles.append(engine)
    engines = {
        name: {"status": value.get("status"),
               "envelope": (value.get("envelope") or {})}
        for name, value in (report.get("engines") or {}).items()
    }
    pin_check = report.get("pinocchio_check") or {}
    detail = {
        "case": report.get("case"),
        "model_sha256": report.get("model_sha256"),
        "engines": engines,
        "comparison": report.get("comparison"),
        "pinocchio_check_passed": pin_check.get("passed"),
        "pinocchio_check": pin_check,
        "load_profiles": profiles,
        "note": "Load profile is an artifact only; the FEA gate stays "
        "the declared axial case (wiring the profile into FEA is "
        "future work, not this run).",
    }
    if not profiles:
        return _beta_step_done(
            "physics", "failed", started, detail,
            error="no engine produced a load profile",
        )
    return _beta_step_done("physics", "ok", started, detail)


def _beta_gmsh_surface_stl(
    step_path: str | Path,
    out_stl: str | Path,
    *,
    mesh_size_mm: float = 2.5,
    timeout_s: float = 300.0,
    gmsh_bin: str | None = None,
) -> dict[str, Any]:
    """Full closed surface triangulation of a STEP (Gmsh, subprocess).

    The FEA mesh (``bar.msh``) only carries the two physical faces
    (``fixed``/``loaded``), so its surface is not watertight; this
    re-meshes the surface with ``Mesh.SaveAll = 1`` (``Mesh 2``) for
    the viewer ``.glb``. Same subprocess rules as the FEA module:
    argv list, isolated cwd, timeout, ``shell=False``.
    """
    from .fea import DEFAULT_GMSH_BIN, DEFAULT_MESH_SIZE_MM, resolve_fea_tool

    gmsh_bin = resolve_fea_tool(DEFAULT_GMSH_BIN, gmsh_bin)
    mesh_size_mm = mesh_size_mm or DEFAULT_MESH_SIZE_MM
    out_stl = Path(out_stl)
    out_stl.parent.mkdir(parents=True, exist_ok=True)
    geo = (
        "SetFactory(\"OpenCASCADE\");\n"
        "Merge \"input.step\";\n"
        "Mesh.SaveAll = 1;\n"
        f"Mesh.CharacteristicLengthMax = {mesh_size_mm:g};\n"
        "Mesh 2;\n"
        "Save \"surface.stl\";\n"
    )
    with tempfile.TemporaryDirectory(prefix="ocre-beta-stl-") as tmp:
        tmp_path = Path(tmp)
        shutil.copyfile(step_path, tmp_path / "input.step")
        (tmp_path / "surf.geo").write_text(geo, encoding="utf-8")
        try:
            completed = subprocess.run(
                [gmsh_bin, "-3", str(tmp_path / "surf.geo"),
                 "-format", "stl", "-o", str(tmp_path / "surface.stl")],
                cwd=str(tmp_path), timeout=timeout_s, shell=False,
                capture_output=True, text=True, errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"gmsh surface mesh timed out after {timeout_s}s"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"could not start gmsh ({gmsh_bin}): {exc}"
            ) from exc
        produced = tmp_path / "surface.stl"
        if completed.returncode != 0 or not produced.is_file():
            raise RuntimeError(
                f"gmsh surface mesh failed (returncode "
                f"{completed.returncode}): "
                f"{(completed.stderr or completed.stdout or '')[-2000:]}"
            )
        shutil.copyfile(produced, out_stl)
    return {"stl_path": str(out_stl), "stl_bytes": out_stl.stat().st_size,
            "mesh_size_mm": mesh_size_mm, "gmsh_bin": gmsh_bin}


def _beta_step_viz(ctx: dict[str, Any]) -> dict[str, Any]:
    """Step 6: PyVista .png (visualization, never analysis) + .glb + PDF.

    The render reads the FEA ``.frd`` (solver field, SXX by default);
    the ``.glb`` is the axial-bar surface mesh scaled mm -> m; the
    PDF plots the working document's DXF via LibreCAD ``dxf2pdf``.
    """
    from .viz import VizError, plot_pdf, render_field, to_glb

    args = ctx["args"]
    started = time.perf_counter()
    viz_dir = ctx["out_dir"] / "viz"
    viz_dir.mkdir(parents=True, exist_ok=True)
    fea_result = ctx.get("fea_result")
    if not isinstance(fea_result, dict) or fea_result.get("status") != "passed":
        return _beta_step_done(
            "viz", "failed", started,
            {"fea_status": (fea_result or {}).get("status")},
            error="no passed FEA result to visualize (render needs the "
            "solver .frd)",
        )
    frd_path = ctx["fea_dir"] / "job.frd"
    try:
        render = render_field(frd_path, viz_dir / "field.png",
                              timeout_s=args.timeout_s)
    except ImportError as exc:
        # viz deps (meshio, imported lazily in viz.py) are missing:
        # record the step as failed instead of killing beta-run --
        # the report must still be written with the other steps.
        return _beta_step_done(
            "viz", "failed", started, {},
            error=f"viz dependency missing: {exc}",
        )
    except VizError as exc:
        return _beta_step_done(
            "viz", "failed", started, {},
            error=f"PyVista render failed: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - a step failure is recorded, never a lost report
        return _beta_step_done(
            "viz", "failed", started, {},
            error=f"render failed: {type(exc).__name__}: {exc}",
        )
    try:
        stl = _beta_gmsh_surface_stl(
            ctx["fea_input_step"], viz_dir / "bar-surface.stl",
            timeout_s=args.timeout_s)
        glb = to_glb(viz_dir / "bar-surface.stl", viz_dir / "model.glb",
                     units="mm")
    except Exception as exc:  # noqa: BLE001 - a step failure is recorded, never a lost report
        return _beta_step_done(
            "viz", "failed", started,
            {"render": render},
            error=f".glb export failed: {exc}",
        )
    dxf_path = ctx.get("ocs_dxf")
    if dxf_path is None:
        from .dxf_export import export_dxf

        dxf_path = viz_dir / "document.dxf"
        try:
            export_dxf(ctx["doc"], dxf_path)
        except ValueError as exc:
            return _beta_step_done(
                "viz", "failed", started,
                {"render": render, "glb": glb},
                error=f"could not export the working document to DXF "
                f"for the PDF: {exc}",
            )
    try:
        pdf = plot_pdf(dxf_path, viz_dir / "drawing.pdf",
                       timeout_s=args.timeout_s)
    except Exception as exc:  # noqa: BLE001 - a step failure is recorded, never a lost report
        return _beta_step_done(
            "viz", "failed", started,
            {"render": render, "glb": glb, "stl": stl},
            error=f"PDF plot failed: {exc}",
        )
    detail = {"render": render, "stl": stl, "glb": glb, "pdf": pdf}
    return _beta_step_done("viz", "ok", started, detail)


def _beta_step_fmu(ctx: dict[str, Any]) -> dict[str, Any]:
    """Step 7: FMPy gate -- ``not-triggered`` without an FMU."""
    from .fmu_gate import validate_fmu

    started = time.perf_counter()
    result = validate_fmu(ctx["args"].fmu)
    status = _FMU_STEP_STATUS.get(str(result.get("status")), "failed")
    error = None
    if status in ("failed", "blocked"):
        error = str(result.get("reason") or "FMU gate failed")
    return _beta_step_done("fmu", status, started, result, error=error)


def _beta_text_report(report: dict[str, Any]) -> str:
    lines = [
        "beta-run OCRE<->Draw v0.1",
        f"status: {report['status']} (exit {report['exit_code']})",
        f"fingerprint domain: {report['fingerprint_domain']}",
        f"duration: {report['duration_s']:.1f}s",
        "",
    ]
    docs = report.get("documents", {})
    for key in ("export", "working"):
        entry = docs.get(key)
        if entry:
            lines.append(
                f"document {key}: {entry.get('doc_id')} "
                f"rev {entry.get('revision')} "
                f"sha256 {str(entry.get('sha256'))[:16]}...")
    if docs.get("working_source"):
        lines.append(f"working document source: {docs['working_source']}")
    lines.append("")
    for step in report["steps"]:
        line = f"[{step['status']}] {step['id']} ({step['duration_s']}s)"
        if step.get("error"):
            line += f" -- {step['error']}"
        lines.append(line)
        detail = step.get("detail") or {}
        if step["id"] == "fea" and detail.get("errors_pct"):
            lines.append(
                f"    gate G4.1 errors: Rx "
                f"{detail['errors_pct'].get('rx'):.4f}% / S11 "
                f"{detail['errors_pct'].get('s11'):.4f}% (<= 1%)")
        if step["id"] == "step" and isinstance(detail.get("step"), dict):
            lines.append(
                f"    STEP volume: "
                f"{detail['step'].get('volume_mm3')} mm3, "
                f"thickness {detail['step'].get('thickness_mm')} mm, "
                f"material {detail['step'].get('material_declared')}")
        if step["id"] == "physics" and isinstance(detail.get("engines"), dict):
            for name, eng in detail["engines"].items():
                env = eng.get("envelope") or {}
                lines.append(
                    f"    {name}: {eng.get('status')}, "
                    f"f_max {env.get('f_max_N')} N")
        if step["id"] == "fmu":
            lines.append(f"    {detail.get('reason', '')}")
    lines.append("")
    lines.append("artifacts:")
    for artifact in report.get("artifacts", []):
        sha = artifact.get("sha256") or "?"
        lines.append(f"  {artifact['path']} (sha256 {str(sha)[:16]}...)")
    for note in report.get("notes", []):
        lines.append(f"note: {note}")
    return "\n".join(lines) + "\n"


def _cmd_beta_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).resolve()
    proposal_file = Path(args.proposal_file).resolve() if args.proposal_file else out_dir / "proposals.json"
    started = time.perf_counter()
    report: dict[str, Any] = {
        "command": "beta-run",
        "status": "blocked",
        "exit_code": 2,
        "fingerprint_domain": BETA_FINGERPRINT_DOMAIN,
        "started_at": _utc_now(),
        "params": {
            "png": args.png,
            "vectorize_script": args.vectorize_script,
            "vectorize_sha256": args.vectorize_sha256,
            "vectorize_python": args.vectorize_python,
            "vectorize_module": args.vectorize_module,
            "vectorize_pythonpath": args.vectorize_pythonpath,
            "origin": args.origin,
            "draw_repo": args.draw_repo,
            "draw_commit": args.draw_commit,
            "thickness_mm": args.thickness_mm,
            "material": args.material,
            "open_strokes_policy": args.open_strokes_policy,
            "ocs_binary": args.ocs_binary,
            "stamps": args.stamps,
            "draw_config": args.draw_config,
            "document": args.document,
            "fmu": args.fmu,
            "timeout_s": args.timeout_s,
            "cad_only": args.cad_only,
            "proposal_file": str(proposal_file),
        },
        "documents": {},
        "steps": [],
        "artifacts": [],
        "notes": [
            "The CAD loop is the quality bar: paper line -> CAD line, "
            "2D and 3D, with the eraser working. Simulations are a "
            "consequence: the axial FEA case only validates the whole "
            "path (gate G4.1).",
            "The physics load profile is an artifact only; the FEA gate "
            "stays the declared axial case (wiring the profile into "
            "FEA is future work).",
            "The FMU gate reports not-triggered without an FMU; it never "
            "runs end-to-end without one.",
        ],
    }

    def _finish() -> int:
        report["duration_s"] = round(time.perf_counter() - started, 3)
        report["finished_at"] = _utc_now()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(out_dir / "beta-report.json", report)
            (out_dir / "beta-report.txt").write_text(
                _beta_text_report(report), encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write the beta report: {exc}",
                  file=sys.stderr)
        print(json.dumps(report, sort_keys=True, indent=2))
        return int(report["exit_code"])

    # --- Preflight (fail-closed, before any heavy work). ---
    preflight_errors: list[str] = []
    if not _HEX40_RE.fullmatch(args.draw_commit or ""):
        preflight_errors.append(
            "--draw-commit must be a full 40-hex git revision "
            f"(got {args.draw_commit!r})")
    png_path = Path(args.png)
    if not png_path.is_file():
        preflight_errors.append(f"PNG not found: {png_path}")
    if not Path(args.vectorize_script).is_file():
        preflight_errors.append(
            f"vectorize script not found: {args.vectorize_script}")
    if args.vectorize_pythonpath and not args.vectorize_module:
        preflight_errors.append(
            "--vectorize-pythonpath requires --vectorize-module")
    if args.thickness_mm is None:
        preflight_errors.append(
            "thickness-mm is required and declared by a human "
            "(thickness is never invented)")
    elif not (math.isfinite(args.thickness_mm) and args.thickness_mm > 0):
        preflight_errors.append(
            f"thickness-mm must be positive and finite "
            f"(got {args.thickness_mm!r})")
    if not _material_declared(args.material) and not (args.ai_adapter_factory or args.proposal_file or args.confirm_proposal or args.reject_proposal):
        preflight_errors.append(
            "material is required and declared by a human "
            "(material is never invented)")
    if not (math.isfinite(args.timeout_s) and args.timeout_s > 0):
        preflight_errors.append(
            f"--timeout-s must be positive and finite "
            f"(got {args.timeout_s!r})")
    working_doc: dict[str, Any] | None = None
    if args.document:
        doc_path = Path(args.document)
        if not doc_path.is_file():
            preflight_errors.append(
                f"--document not found: {doc_path}")
        else:
            try:
                working_doc = loads_document(doc_path.read_bytes())
                validate_draw_document_or_raise(working_doc)
            except ValueError as exc:
                preflight_errors.append(
                    f"--document failed validation: {exc}")
    if preflight_errors:
        report["status"] = "blocked"
        report["exit_code"] = 2
        report["steps"] = [
            _beta_step_done(
                "preflight", "blocked", started,
                {"errors": preflight_errors},
                error="; ".join(preflight_errors),
            )
        ]
        return _finish()

    ctx: dict[str, Any] = {
        "args": args,
        "out_dir": out_dir,
        "doc": working_doc,
        "doc_sha256": (document_sha256(working_doc)
                       if working_doc is not None else None),
    }
    ctx["observe_conversions"] = bool(args.ai_adapter_factory or args.cad_only or args.proposal_file)
    ctx["ai_adapter"] = None
    if args.ai_adapter_factory:
        try:
            ctx["ai_adapter"] = _resolve_ai_adapter(args.ai_adapter_factory)
        except Exception as exc:
            report["ai_adapter"] = {"status": "unavailable", "error": type(exc).__name__}
    if working_doc is not None:
        report["documents"]["working"] = _doc_entry(
            working_doc, ctx["doc_sha256"])
        report["documents"]["working_source"] = "example-document (--document)"

    def process_proposals(ctx: dict[str, Any], proposal_file: Path) -> bool:
        """Load/update proposals bound to the current document."""
        ai_adapter = ctx["ai_adapter"]
        doc = ctx.get("doc")
        if doc is None:
            return True
        try:
            from dataclasses import asdict
            from .ai.proposals import PROPOSAL_STATES, Proposal, confirm, reject
            from .ai.semantics import analyze, material_for_step
            digest = document_sha256(doc)
            ledger = (
                json.loads(proposal_file.read_text(encoding="utf-8"))
                if proposal_file.is_file() else None
            )
            if ledger is not None:
                if not isinstance(ledger, dict):
                    raise ValueError("proposal ledger must be a JSON object")
                if (ledger.get("document_sha256") != digest
                        or ledger.get("revision") != doc.get("revision")):
                    raise ValueError("proposal ledger document revision/hash mismatch")
                if not isinstance(ledger.get("proposals"), list):
                    raise ValueError("proposal ledger proposals must be a list")
            if ledger is None and ai_adapter is not None:
                proposals = analyze(doc, ai_adapter, explicit_material=args.material)
                ledger = {
                    "document_sha256": digest,
                    "revision": doc["revision"],
                    "proposals": [asdict(proposal) for proposal in proposals],
                }
                proposal_file.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_json(proposal_file, ledger)
            if ledger is not None:
                confirmed, rejected = set(args.confirm_proposal), set(args.reject_proposal)
                if confirmed & rejected:
                    raise ValueError("proposal ids cannot be both confirmed and rejected")
                if args.confirmed_by and not (confirmed or rejected):
                    raise ValueError("--confirmed-by requires a proposal action")
                if (confirmed or rejected) and not args.confirmed_by:
                    raise ValueError("--confirmed-by is required with proposal actions")
                proposals_by_id: dict[str, Proposal] = {}
                for raw_proposal in ledger["proposals"]:
                    if not isinstance(raw_proposal, dict):
                        raise ValueError("each ledger proposal must be an object")
                    proposal = Proposal(**raw_proposal)
                    if proposal.proposal_id in proposals_by_id:
                        raise ValueError(f"duplicate proposal id: {proposal.proposal_id}")
                    if (proposal.document_revision != doc.get("revision")
                            or proposal.document_sha256 != digest):
                        raise ValueError(
                            f"proposal {proposal.proposal_id} document revision/hash mismatch"
                        )
                    if proposal.state not in PROPOSAL_STATES:
                        raise ValueError(f"invalid state for proposal {proposal.proposal_id}")
                    proposals_by_id[proposal.proposal_id] = proposal
                known = set(proposals_by_id)
                requested = confirmed | rejected
                if requested - known:
                    raise ValueError(f"unknown proposal ids: {sorted(requested - known)}")
                for proposal_id in confirmed:
                    proposal = proposals_by_id[proposal_id]
                    if proposal.state == "rejected":
                        raise ValueError(f"cannot confirm rejected proposal {proposal_id}")
                    if proposal.state == "pending":
                        proposals_by_id[proposal_id] = confirm(proposal, by=args.confirmed_by)
                for proposal_id in rejected:
                    proposal = proposals_by_id[proposal_id]
                    if proposal.state == "confirmed":
                        raise ValueError(f"cannot reject confirmed proposal {proposal_id}")
                    if proposal.state == "pending":
                        proposals_by_id[proposal_id] = reject(proposal, by=args.confirmed_by)
                ledger["proposals"] = [asdict(p) for p in proposals_by_id.values()]
                if requested:
                    _atomic_write_json(proposal_file, ledger)
                report["proposals"] = {
                    "path": str(proposal_file),
                    "count": len(ledger["proposals"]),
                    "document_sha256": digest,
                }
                materials = {
                    material
                    for proposal in proposals_by_id.values()
                    if (material := material_for_step(doc, proposal)) is not None
                }
                if len(materials) > 1:
                    raise ValueError("multiple distinct confirmed materials require resolution")
                if not _material_declared(args.material) and materials:
                    args.material = next(iter(materials))
        except Exception as exc:
            report["proposal_error"] = str(exc)
        if not _material_declared(args.material):
            steps.append(_beta_step_report(
                "human-confirmation", "blocked", 0,
                {"proposal_file": str(proposal_file)},
                error="valid declared or confirmed material required",
            ))
            return False
        return True

    # --- The chain, in order. Step 1 is unrolled: only after it
    # succeeds does the working document exist for the report and the
    # proposal gate, and only then do the remaining steps run. ---
    steps: list[dict[str, Any]] = [_beta_step_export(ctx)]
    if steps[0]["status"] == "ok":
        # The working document is --document when given, else the
        # document step 1 just built.
        if ctx["doc"] is None:
            ctx["doc"] = ctx["export_doc"]
            ctx["doc_sha256"] = ctx["export_doc_sha256"]
            report["documents"]["working_source"] = "export (step 1)"
        report["documents"]["export"] = _doc_entry(
            ctx["export_doc"], ctx["export_doc_sha256"])
        report["documents"]["working"] = _doc_entry(
            ctx["doc"], ctx["doc_sha256"])
        if process_proposals(ctx, proposal_file):
            runners = [_beta_step_ocs, _beta_step_step]
            if not args.cad_only:
                runners += [_beta_step_fea, _beta_step_physics,
                            _beta_step_viz, _beta_step_fmu]
            for runner in runners:
                started = time.perf_counter()
                try:
                    steps.append(runner(ctx))
                except Exception as exc:  # noqa: BLE001 - a step failure is recorded, never a lost report
                    steps.append(_beta_step_done(
                        runner.__name__.removeprefix("_beta_step_"),
                        "failed", started, error=str(exc)))
    ran = {step["id"] for step in steps}
    for missing in (step_id for step_id in _BETA_STEP_IDS if step_id not in ran):
        if args.cad_only and missing in _BETA_SIM_STEP_IDS:
            reason = ("not requested: --cad-only stops the chain after "
                      "the CAD steps (export, ocs, step)")
        else:
            reason = ("a previous step failed; the chain stopped "
                      "before this step (no document to work on)")
        steps.append(_beta_step_report(
            missing, "not-run", 0.0, {"reason": reason}))
    report["steps"] = steps

    statuses = [s["status"] for s in steps]
    if "failed" in statuses:
        report["status"] = "failed"
        report["exit_code"] = 1
    elif "blocked" in statuses:
        report["status"] = "blocked"
        report["exit_code"] = 2
    else:
        report["status"] = "ok"
        report["exit_code"] = 0

    export_dir = ctx.get("export_dir", out_dir / "export")
    artifact_paths: list[Path] = [
        export_dir / "document.json",
        export_dir / "document.dxf",
        export_dir / "sidecar.json",
        export_dir / "fingerprint.json",
        out_dir / "ocs" / "document.dxf",
        out_dir / "ocs" / "ocs-verified.dxf",
        out_dir / "ocs" / "ocs-after-erase.dxf",
        out_dir / "step" / "drawing.step",
        out_dir / "step" / "drawing.step.manifest.json",
        out_dir / "fea" / "case" / "fea-axial-result.json",
        out_dir / "fea" / "case" / "job.frd",
        out_dir / "fea" / "case" / "job.dat",
        out_dir / "physics" / "physics.json",
        out_dir / "physics" / "load-profile-pybullet.json",
        out_dir / "physics" / "load-profile-mujoco.json",
        out_dir / "viz" / "field.png",
        out_dir / "viz" / "model.glb",
        out_dir / "viz" / "drawing.pdf",
    ]
    published = {
        (export_dir.name if step["id"] == "export" else step["id"])
        for step in steps if step["status"] == "ok"
    }
    report["artifacts"] = [
        _beta_artifact_entry(path) for path in artifact_paths
        if path.relative_to(out_dir).parts[0] in published and path.is_file()
    ]
    return _finish()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits on --help / bad usage; as a library entry point
        # return the code instead of raising.
        return int(exc.code or 0)
    return {
        "export": _cmd_export,
        "validate": _cmd_validate,
        "fea-axial": _cmd_fea_axial,
        "beta-run": _cmd_beta_run,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
