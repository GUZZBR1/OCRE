"""Local CLI for the F0 Draw integration (``ocre-draw-export``).

Runnable as::

    PYTHONPATH=backend python -m ocre_core.draw.cli export --png ... --out-dir ...
    PYTHONPATH=backend python -m ocre_core.draw.cli validate --document ...

Subcommands:

- ``export``: PNG -> vectorize (black-box subprocess, SHA-pinned) ->
  ``draw.paper.v1`` document -> DXF R2000 + sidecar + DRAW-AF/1
  fingerprint. The document is validated BEFORE any derivative is
  written (fail-closed). The LibreCAD gate (G0.2) is opt-in via
  ``--librecad-check``; without the binary it reports ``unavailable``
  and does not fail the export.
- ``validate``: validate a ``draw.paper.v1`` document file and print
  the validation report as JSON (exit 0 valid, 1 invalid).

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
- Field 8 (adapters) pins the vectorize script (SHA given on the
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
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .document import (
    SHAPES,
    add_stroke,
    document_sha256,
    dumps as dumps_document,
    new_document,
    stabilize_stroke,
)
from .fingerprint import compute_fingerprint
from .paper import load_draw_config, resolve_paper
from .sidecar import build_draw_sidecar
from .validator_draw import (
    SCHEMA_NAME,
    SCHEMA_VERSION,
    bundled_schema_sha256,
    validate_draw_document,
    validate_draw_document_or_raise,
)

__all__ = ["main"]

VALIDATOR_ID = "draw-paper-validator"
VALIDATOR_VERSION = "0.1.0"
POLICY_VERSION = "0.1.0"
UNIT_POLICY_VERSION = "0.1.0"
DXF_ADAPTER_VERSION = "0.1.0"
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_ORIGINS = ("fork-session-v2", "upstream-vectorize", "upstream-canvas-png")


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


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


def _module_file_sha256(module_name: str) -> str:
    """SHA-256 of a sibling module file (validator / DXF writer pin)."""
    import importlib

    module = importlib.import_module(f".{module_name}", __package__)
    path = getattr(module, "__file__", None)
    if not path:
        raise ValueError(f"cannot pin module {module_name}: no __file__")
    return _sha256_file(path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocre-draw-export",
        description="F0 Draw export: PNG -> draw.paper.v1 -> DXF R2000 + sidecar + fingerprint.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="vectorize a PNG and export DXF + sidecar + fingerprint")
    exp.add_argument("--png", required=True, help="input canvas PNG")
    exp.add_argument("--vectorize-script", required=True,
                     help="path to Draw tools/vectorize.py (never copied into OCRE)")
    exp.add_argument("--vectorize-sha256", required=True,
                     help="pinned SHA-256 of the vectorize script (verified before running)")
    exp.add_argument("--out-dir", required=True, help="output directory for the derivatives")
    exp.add_argument("--origin", required=True, choices=list(_ORIGINS),
                     help="document source origin (explicit, never a default)")
    exp.add_argument("--draw-repo", required=True, help="Draw repo the artifacts came from")
    exp.add_argument("--draw-commit", required=True,
                     help="pinned Draw revision, full 40-hex git SHA (sidecar requires it)")
    exp.add_argument("--order-kind", default="unknown",
                     choices=("temporal-stamps", "reading-centroid", "unknown"),
                     help="how stroke order is assigned (default: unknown)")
    exp.add_argument("--paper", default=None,
                     help="paper format from the paper registry (operator flag; "
                          "default: the current Draw paper, today A4)")
    exp.add_argument("--draw-config", default=None,
                     help="path to Draw's config.py; the paper is derived from "
                          "its PAPER_W/PAPER_H (read as text/AST, never "
                          "imported). Overrides --paper when given.")
    exp.add_argument("--doc-id", default=None, help="document UUID v4 (generated if omitted)")
    exp.add_argument("--timeout-s", type=float, default=300.0,
                     help="vectorize subprocess timeout in seconds")
    exp.add_argument("--epsilon", type=float, default=None)
    exp.add_argument("--thinning", default=None)
    exp.add_argument("--librecad-check", action="store_true",
                     help="also run the LibreCAD gate G0.2 on the DXF (unavailable without the binary)")

    val = sub.add_parser("validate", help="validate a draw.paper.v1 document file")
    val.add_argument("--document", required=True, help="path to the document JSON file")
    return parser


def _cmd_export(args: argparse.Namespace) -> int:
    # Lazy imports: `validate` must work without ezdxf installed.
    from .dxf_export import export_dxf
    from .librecad_gate import check_dxf
    from .vectorize_runner import VectorizeError, run_vectorize

    if not _HEX40_RE.fullmatch(args.draw_commit):
        print(
            "error: --draw-commit must be a full 40-hex git revision "
            f"(got {args.draw_commit!r}); the sidecar requires it",
            file=sys.stderr,
        )
        return 2
    png_path = Path(args.png)
    if not png_path.is_file():
        print(f"error: PNG not found: {png_path}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Paper policy (D3): single source in paper.py. Resolve BEFORE the
    # vectorize subprocess so an unknown format / unreadable config
    # fails fast, with nothing written. --draw-config overrides --paper.
    try:
        spec = load_draw_config(args.draw_config) if args.draw_config else resolve_paper(args.paper)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"error: cannot resolve paper: {exc}", file=sys.stderr)
        return 2

    png_sha256 = _sha256_file(png_path)

    try:
        vectorized = run_vectorize(
            png_path,
            args.vectorize_script,
            args.vectorize_sha256,
            output_dir=None,  # temp dir, cleaned up; only polylines are kept
            timeout_s=args.timeout_s,
            epsilon=args.epsilon,
            thinning=args.thinning,
        )
    except VectorizeError as exc:
        print(f"error: vectorize failed: {exc}", file=sys.stderr)
        return 1

    polylines = vectorized["polylines"]
    skipped_primitives = [
        {"type": str(p.get("type", "unknown"))}
        for p in vectorized["primitives"]
        if not (p.get("points_mm") or p.get("corners_mm"))
    ]

    # Build the document (master) from the vectorizer polylines.
    doc = new_document(
        {"repo": args.draw_repo, "commit": args.draw_commit, "origin": args.origin},
        order_kind=args.order_kind,
        doc_id=args.doc_id,
        paper=spec,
    )
    try:
        for polyline in polylines:
            shape = polyline.get("type")
            stroke = add_stroke(
                doc,
                polyline["points_mm"],
                closed=bool(polyline.get("closed", False)),
                shape=shape if shape in SHAPES else None,
            )
            # Vectorizer output is the confirmed layer: stable, so the
            # DXF writer includes it with its default (stable-only) rule.
            stabilize_stroke(doc, stroke["stroke_id"])
    except (ValueError, KeyError) as exc:
        print(f"error: cannot build document from vectorizer output: {exc}", file=sys.stderr)
        return 1

    # Fail-closed: validate BEFORE writing any derivative.
    try:
        validate_draw_document_or_raise(doc)
    except ValueError as exc:
        print(f"error: document failed validation, nothing written: {exc}", file=sys.stderr)
        return 1

    doc_sha_hex = document_sha256(doc)
    schema_sha = bundled_schema_sha256()

    export_parameters: dict[str, Any] = {
        "target": "dxf-r2000",
        "origin": args.origin,
        "order_kind": args.order_kind,
        "include_pending": False,
        "strokes_stabilized": True,
        "paper": {"format": spec.format, "width_mm": spec.width_mm,
                  "height_mm": spec.height_mm,
                  "effective_width_mm": spec.effective_width_mm,
                  "effective_height_mm": spec.effective_height_mm},
        "vectorize": {
            "script_sha256": vectorized["script_sha256"],
            "primitives_total": vectorized["primitives_total"],
            "polylines_total": vectorized["polylines_total"],
            "primitives_without_points": vectorized["primitives_without_points"],
            "skipped_primitive_types": [s["type"] for s in skipped_primitives],
        },
    }

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

    # DXF first (to a temp file, then atomic move), so its hash can go
    # into the bundle manifest and the sidecar.
    dxf_tmp = out_dir / ".document.dxf.tmp"
    dxf_path = out_dir / "document.dxf"
    try:
        dxf_report = export_dxf(doc, dxf_tmp, include_pending=False)
    except ValueError as exc:
        print(f"error: DXF export failed: {exc}", file=sys.stderr)
        return 1
    dxf_sha_hex = dxf_report["output_sha256"]
    os.replace(dxf_tmp, dxf_path)
    dxf_report["output_path"] = str(dxf_path)

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
        "consumed_artifacts": [{"artifact_id": "canvas-png-1", "sha256": png_sha256}],
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
        print(f"error: DRAW-AF/1 fingerprint failed: {exc}", file=sys.stderr)
        return 1

    try:
        sidecar = build_draw_sidecar(
            doc_id=doc["doc_id"],
            document_sha256=doc_sha_hex,
            revision=doc["revision"],
            source_artifacts=[{
                "artifact_id": "canvas-png-1",
                "uri": png_path.name,
                "sha256": png_sha256,
                "origin": args.origin,
                "artifact_kind": "canvas_png",
            }],
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
        print(f"error: sidecar failed: {exc}", file=sys.stderr)
        return 1

    # Write the remaining derivatives atomically.
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
            print(f"error: LibreCAD gate failed: {exc}", file=sys.stderr)
            return 1
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
        "out_dir": str(out_dir),
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    if librecad_status == "fail":
        print("error: LibreCAD gate G0.2 failed for the exported DXF",
              file=sys.stderr)
        return 1
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.document)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"error: cannot read document: {exc}", file=sys.stderr)
        return 2
    report = validate_draw_document(raw)
    print(json.dumps(report.as_dict(), sort_keys=True, indent=2))
    return 0 if report.valid else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits on --help / bad usage; as a library entry point
        # return the code instead of raising.
        return int(exc.code or 0)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "validate":
        return _cmd_validate(args)
    parser.error(f"unknown command: {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
