"""Provenance sidecar ``draw-paper-provenance/0.1`` for Draw paper documents.

A NEW sidecar, written from scratch for paper provenance. It deliberately
does not copy ``backend/ocre_core/provenance/sidecar.py``
(``cad-mesh-provenance/0.2``): that sidecar requires
``origin == "synthetic_geo"`` and FEA benchmark groups, which would label
real paper as synthetic. Only the builder/validator shape (explicit origins,
hash-pinned artifacts, a pure audit function) is borrowed as style.

Origins are explicit and closed -- a source artifact with a missing or
unknown origin is an error, never a silent default::

    real_paper | synthetic | upstream-vectorize | upstream-canvas-png
    | fork-session-v2

The sidecar carries the SHA-256 of the main ``draw.paper.v1`` document
INSIDE it (``document_sha256``), so an orphaned sidecar can be reassociated
with its document, plus the pinned adapter/version that produced each
derived artifact and the export parameters the derived format does not
carry (a DXF file, for example, cannot carry stroke order, order kind, or
the paper's effective size).

This module is pure: it operates on dicts only, touches no files, and
never imports the document builder (``backend/ocre_core/draw/document.py``
is owned by another implementer). The caller supplies the document facts;
hash verification against files on disk belongs to the caller/preflight.

Integration interface (for the document builder / DrawPreflight)::

    sidecar = build_draw_sidecar(
        doc_id=document["doc_id"],
        document_sha256=<sha256 hex of the canonical document bytes>,
        revision=document["revision"],
        source_artifacts=[
            {"artifact_id": "canvas-png-1", "uri": "captures/canvas_....png",
             "sha256": <hex>, "origin": "upstream-canvas-png",
             "artifact_kind": "canvas_png"},
        ],
        derived_artifacts=[
            {"artifact_id": "dxf-r2000-1", "uri": "exports/doc.dxf",
             "sha256": <hex>, "format": "dxf-r2000"},
        ],
        adapters=[
            {"stage_id": "vectorize", "adapter_id": "draw.vectorize",
             "version": <pinned Draw revision>,
             "binary_sha256": <hex>, "capabilities_sha256": <hex>},
        ],
        export_parameters=<export_case parameters the DXF does not carry>,
        fingerprint_digest=<DRAW-AF/1 hex digest, optional>,
        draw_source={"repo": "santleme/draw", "commit": <40-hex sha>},
    )
    issues = validate_draw_sidecar(sidecar)  # [] means valid
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

SIDECAR_VERSION = "draw-paper-provenance/0.1"
DRAW_FINGERPRINT_LABEL = "DRAW-AF/1"

DRAW_ORIGINS = frozenset({
    "real_paper",
    "synthetic",
    "upstream-vectorize",
    "upstream-canvas-png",
    "fork-session-v2",
})

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEX_SHA1 = re.compile(r"^[0-9a-f]{40}$")

# Adapter entries use the same five-field pin as DRAW-AF/1 field 8, so the
# fingerprint and the sidecar can never disagree about which adapter ran.
_ADAPTER_KEYS = {"stage_id", "adapter_id", "version", "binary_sha256",
                 "capabilities_sha256"}
_SOURCE_KEYS = {"artifact_id", "sha256", "origin", "artifact_kind"}
_SOURCE_OPTIONAL_KEYS = {"uri"}
_DERIVED_KEYS = {"artifact_id", "sha256", "format"}
_DERIVED_OPTIONAL_KEYS = {"uri", "derived_from"}


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None


def _is_non_empty_ascii(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value.isascii()


def _check_adapter(item: Any, label: str, issues: list[str]) -> None:
    if not isinstance(item, dict) or set(item) != _ADAPTER_KEYS:
        issues.append(f"{label} must contain exactly {sorted(_ADAPTER_KEYS)}")
        return
    for key in ("stage_id", "adapter_id", "version"):
        if not _is_non_empty_ascii(item[key]):
            issues.append(f"{label}.{key} must be a non-empty ASCII string")
    for key in ("binary_sha256", "capabilities_sha256"):
        if not _is_sha256(item[key]):
            issues.append(
                f"{label}.{key} must be 64 lowercase hexadecimal characters")


def _check_source(item: Any, label: str, issues: list[str]) -> None:
    if not isinstance(item, dict):
        issues.append(f"{label} must be an object")
        return
    if not _SOURCE_KEYS <= set(item) <= _SOURCE_KEYS | _SOURCE_OPTIONAL_KEYS:
        issues.append(
            f"{label} must contain {_SOURCE_KEYS} and only optionally "
            f"{_SOURCE_OPTIONAL_KEYS}")
        return
    if not _is_non_empty_ascii(item["artifact_id"]):
        issues.append(f"{label}.artifact_id must be a non-empty ASCII string")
    if not _is_sha256(item["sha256"]):
        issues.append(
            f"{label}.sha256 must be 64 lowercase hexadecimal characters")
    if item["origin"] not in DRAW_ORIGINS:
        issues.append(
            f"{label}.origin must be one of {sorted(DRAW_ORIGINS)}, "
            f"never a default; got {item['origin']!r}")
    if not isinstance(item["artifact_kind"], str) or not item["artifact_kind"]:
        issues.append(f"{label}.artifact_kind must be a non-empty string")
    if "uri" in item and (not isinstance(item["uri"], str)
                          or not item["uri"]):
        issues.append(f"{label}.uri must be a non-empty string")


def _check_derived(item: Any, label: str, issues: list[str]) -> None:
    if not isinstance(item, dict):
        issues.append(f"{label} must be an object")
        return
    if not _DERIVED_KEYS <= set(item) <= _DERIVED_KEYS | _DERIVED_OPTIONAL_KEYS:
        issues.append(
            f"{label} must contain {_DERIVED_KEYS} and only optionally "
            f"{_DERIVED_OPTIONAL_KEYS}")
        return
    if not _is_non_empty_ascii(item["artifact_id"]):
        issues.append(f"{label}.artifact_id must be a non-empty ASCII string")
    if not _is_sha256(item["sha256"]):
        issues.append(
            f"{label}.sha256 must be 64 lowercase hexadecimal characters")
    if not _is_non_empty_ascii(item["format"]):
        issues.append(f"{label}.format must be a non-empty ASCII string")
    for key in ("uri", "derived_from"):
        if key in item and (not isinstance(item[key], str)
                            or not item[key]):
            issues.append(f"{label}.{key} must be a non-empty string")


def validate_draw_sidecar(sidecar: Any) -> list[str]:
    """Audit a ``draw-paper-provenance/0.1`` sidecar; ``[]`` means valid.

    Checks structure, version, the embedded document hash, explicit
    origins, hash formats, pinned adapters, and the cross-field rule that
    a derived artifact requires at least one pinned adapter. This never
    means engineering readiness -- only that the provenance record itself
    is well-formed and self-consistent. It does not read files: hash
    verification against disk belongs to the caller.
    """
    issues: list[str] = []
    if not isinstance(sidecar, dict):
        return ["sidecar must be an object"]
    if sidecar.get("sidecar_version") != SIDECAR_VERSION:
        issues.append(f"sidecar_version must be {SIDECAR_VERSION}")
    if (not isinstance(sidecar.get("doc_id"), str)
            or not sidecar["doc_id"]):
        issues.append("doc_id must be a non-empty string")
    if not _is_sha256(sidecar.get("document_sha256")):
        issues.append(
            "document_sha256 must be 64 lowercase hexadecimal characters "
            "(the main document hash, carried inside the sidecar)")
    revision = sidecar.get("revision")
    if (not isinstance(revision, int) or isinstance(revision, bool)
            or revision < 0):
        issues.append("revision must be an integer >= 0")

    sources = sidecar.get("source_artifacts")
    if not isinstance(sources, list) or not sources:
        issues.append("source_artifacts must be a non-empty array")
        sources = []
    seen_sources: set[str] = set()
    for index, item in enumerate(sources):
        _check_source(item, f"source_artifacts[{index}]", issues)
        if isinstance(item, dict) and isinstance(item.get("artifact_id"), str):
            if item["artifact_id"] in seen_sources:
                issues.append(
                    f"duplicate source artifact_id: {item['artifact_id']}")
            seen_sources.add(item["artifact_id"])

    derived = sidecar.get("derived_artifacts")
    if not isinstance(derived, list):
        issues.append("derived_artifacts must be an array")
        derived = []
    seen_derived: set[str] = set()
    for index, item in enumerate(derived):
        _check_derived(item, f"derived_artifacts[{index}]", issues)
        if isinstance(item, dict) and isinstance(item.get("artifact_id"), str):
            if item["artifact_id"] in seen_derived:
                issues.append(
                    f"duplicate derived artifact_id: {item['artifact_id']}")
            seen_derived.add(item["artifact_id"])

    adapters = sidecar.get("adapters")
    if not isinstance(adapters, list):
        issues.append("adapters must be an array")
        adapters = []
    seen_adapters: set[tuple[str, str]] = set()
    for index, item in enumerate(adapters):
        _check_adapter(item, f"adapters[{index}]", issues)
        if isinstance(item, dict):
            key = (item.get("stage_id"), item.get("adapter_id"))
            if all(isinstance(part, str) for part in key):
                if key in seen_adapters:
                    issues.append(f"duplicate adapter key: {key}")
                seen_adapters.add(key)
    if derived and not adapters:
        issues.append(
            "derived_artifacts is non-empty but no adapter is pinned: "
            "a derived file without a pinned adapter/version is unauditable")

    params = sidecar.get("export_parameters")
    if not isinstance(params, dict):
        issues.append("export_parameters must be an object")
    else:
        try:
            from ocre_core.validation.approval_fingerprint import jcs
            jcs(params)
        except Exception as exc:  # noqa: BLE001 - any non-canonical value
            issues.append(
                "export_parameters must be canonically encodable (JCS): "
                f"{exc}")

    fingerprint = sidecar.get("fingerprint")
    if fingerprint is not None:
        if not isinstance(fingerprint, dict) \
                or set(fingerprint) != {"algorithm", "digest"}:
            issues.append(
                "fingerprint must be null or exactly "
                "{algorithm, digest}")
        else:
            if fingerprint["algorithm"] != DRAW_FINGERPRINT_LABEL:
                issues.append(
                    f"fingerprint.algorithm must be {DRAW_FINGERPRINT_LABEL}")
            if not _is_sha256(fingerprint["digest"]):
                issues.append(
                    "fingerprint.digest must be 64 lowercase hexadecimal "
                    "characters")

    draw_source = sidecar.get("draw_source")
    if draw_source is not None:
        if not isinstance(draw_source, dict) \
                or set(draw_source) != {"repo", "commit"}:
            issues.append("draw_source must be null or exactly "
                          "{repo, commit}")
        else:
            if (not isinstance(draw_source["repo"], str)
                    or not draw_source["repo"]):
                issues.append("draw_source.repo must be a non-empty string")
            if (not isinstance(draw_source["commit"], str)
                    or _HEX_SHA1.fullmatch(draw_source["commit"]) is None):
                issues.append(
                    "draw_source.commit must be a 40-character lowercase "
                    "hex git revision")

    extra = set(sidecar) - {
        "sidecar_version", "doc_id", "document_sha256", "revision",
        "source_artifacts", "derived_artifacts", "adapters",
        "export_parameters", "fingerprint", "draw_source",
    }
    if extra:
        issues.append(f"unexpected top-level fields: {sorted(extra)}")
    return issues


def build_draw_sidecar(
    *,
    doc_id: str,
    document_sha256: str,
    revision: int,
    source_artifacts: list[dict[str, Any]],
    derived_artifacts: list[dict[str, Any]],
    adapters: list[dict[str, Any]],
    export_parameters: dict[str, Any],
    fingerprint_digest: str | None = None,
    draw_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``draw-paper-provenance/0.1`` sidecar, fail-closed.

    All seven core arguments are required keyword-only and must be passed
    explicitly -- including empty ``derived_artifacts`` / ``adapters`` /
    ``export_parameters`` when nothing has been derived yet, so emptiness
    is a stated fact and never a silent default. Raises :class:`ValueError`
    with every issue found; nothing is built from invalid input.
    """
    sidecar: dict[str, Any] = {
        "sidecar_version": SIDECAR_VERSION,
        "doc_id": doc_id,
        "document_sha256": document_sha256,
        "revision": revision,
        "source_artifacts": deepcopy(source_artifacts),
        "derived_artifacts": deepcopy(derived_artifacts),
        "adapters": deepcopy(adapters),
        "export_parameters": deepcopy(export_parameters),
        "fingerprint": (
            {"algorithm": DRAW_FINGERPRINT_LABEL, "digest": fingerprint_digest}
            if fingerprint_digest is not None else None
        ),
        "draw_source": deepcopy(draw_source) if draw_source is not None else None,
    }
    issues = validate_draw_sidecar(sidecar)
    if issues:
        raise ValueError(
            "invalid draw-paper-provenance/0.1 sidecar:\n"
            + "\n".join(f"- {issue}" for issue in issues))
    return sidecar
