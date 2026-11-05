"""Builder for the ``draw.paper.v1`` document (OCRE-side master).

The document is built, never rewritten by the validator: every mutation
goes through this module, bumps ``revision`` by exactly 1 and refreshes
``updated_at``. Erasing never removes a stroke — it writes a tombstone
(``status: "erased"`` plus ``erased_at_ms``/``erased_reason``); an erase
inferred from a diff between revisions is a *provisional* tombstone
(``erased_inferred: true``, no ``erased_reason``) until a real event
confirms it.

Canonical serialisation for hashing is plain ``json.dumps(sort_keys=True)``
of the document dict — an internal document hash only. The JCS/RFC 8785
fingerprint (DRAW-AF/1) lives in its own module, not here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from .paper import DEFAULT_SPEC, PaperSpec, resolve_paper
from .validator_draw import (
    SCHEMA_NAME,
    SCHEMA_VERSION,
    StrictJSONError,
    bundled_schema_sha256,
)

ERASE_REASONS = ("hand-erase", "page-turn", "manual")
ORDER_KINDS = ("temporal-stamps", "reading-centroid", "unknown")
STATUSES = ("pending", "stable", "erased")
SHAPES = ("line", "circle", "rectangle", "polygon", "polyline")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"not an RFC 3339 date-time: {value!r}") from exc


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise StrictJSONError(f"non-finite JSON number: {value}")


def compute_length_mm(points_mm: list[list[float]], closed: bool) -> float:
    """Polyline length in mm; the closing segment counts when closed."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points_mm, points_mm[1:]):
        total += math.hypot(x2 - x1, y2 - y1)
    if closed and len(points_mm) > 1:
        (x1, y1), (x2, y2) = points_mm[-1], points_mm[0]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def canonical_document_bytes(doc: dict[str, Any]) -> bytes:
    """Canonical serialisation of a document, for hashing only.

    Plain ``json.dumps(sort_keys=True)`` — deterministic for a given parsed
    document. This is NOT the JCS/RFC 8785 encoding used by fingerprints.
    """
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def document_sha256(doc: dict[str, Any]) -> str:
    """SHA-256 of the canonical document bytes."""
    return hashlib.sha256(canonical_document_bytes(doc)).hexdigest()


def dumps(doc: dict[str, Any]) -> str:
    """Serialise a document to canonical JSON text."""
    return canonical_document_bytes(doc).decode("utf-8")


def loads(text: str | bytes) -> dict[str, Any]:
    """Parse document JSON strictly (duplicate keys and NaN/Inf rejected)."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="strict")
    parsed = json.loads(
        text, object_pairs_hook=_pairs_no_duplicates, parse_constant=_reject_constant
    )
    if not isinstance(parsed, dict):
        raise StrictJSONError("draw.paper.v1 document must be a JSON object")
    return parsed


def new_document(
    source: dict[str, str],
    *,
    order_kind: str = "unknown",
    doc_id: str | None = None,
    created_at: str | None = None,
    paper: str | PaperSpec | None = None,
) -> dict[str, Any]:
    """Create a new draw.paper.v1 document at revision 1.

    ``source`` must carry ``repo``, ``commit`` and an explicit ``origin``
    (``fork-session-v2`` | ``upstream-vectorize`` | ``upstream-canvas-png``);
    there is no default origin. ``order_kind`` describes how stroke order
    is assigned — never ``temporal-stamps`` without stamps. ``paper`` is
    a format name from the paper registry, a ``PaperSpec``, or ``None``
    for the default (today A4 — the paper policy lives in ``paper.py``,
    decision D3, and is operator-configurable, not user-facing).
    """
    if order_kind not in ORDER_KINDS:
        raise ValueError(f"unknown order_kind: {order_kind!r}")
    for field in ("repo", "commit", "origin"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise ValueError(f"source.{field} must be a non-empty string")
    if source["origin"] not in ("fork-session-v2", "upstream-vectorize", "upstream-canvas-png"):
        raise ValueError(f"unknown source origin: {source['origin']!r}")
    now = created_at or _utc_now()
    _parse_rfc3339(now)
    spec = resolve_paper(paper)
    return {
        "schema": SCHEMA_NAME,
        "schema_sha256": bundled_schema_sha256(),
        "doc_id": doc_id or str(uuid.uuid4()),
        "revision": 1,
        "created_at": now,
        "updated_at": now,
        "source": {
            "repo": source["repo"],
            "commit": source["commit"],
            "origin": source["origin"],
        },
        "paper": spec.as_dict(),
        "coordinate_system": "sheet_top_left_y_down",
        "units": "mm",
        "order_kind": order_kind,
        "strokes": [],
    }


def _touch(doc: dict[str, Any]) -> None:
    """Bump revision by 1 and refresh updated_at after a mutation."""
    doc["revision"] = int(doc["revision"]) + 1
    doc["updated_at"] = _utc_now()


def _paper_bounds(doc: dict[str, Any]) -> tuple[str, float, float]:
    """(format, width_mm, effective_height_mm) of the document's sheet.

    Read from the document's own paper block (D3); a missing or
    malformed block falls back to the default spec — the validator
    flags such a document as invalid anyway.
    """
    paper = doc.get("paper") if isinstance(doc, dict) else None
    fmt = DEFAULT_SPEC.format
    width = DEFAULT_SPEC.width_mm
    eff_height = DEFAULT_SPEC.effective_height_mm
    if isinstance(paper, dict):
        if isinstance(paper.get("format"), str) and paper["format"]:
            fmt = paper["format"]
        if isinstance(paper.get("width_mm"), (int, float)) and not isinstance(paper.get("width_mm"), bool):
            width = float(paper["width_mm"])
        if isinstance(paper.get("effective_height_mm"), (int, float)) and not isinstance(paper.get("effective_height_mm"), bool):
            eff_height = float(paper["effective_height_mm"])
    return fmt, width, eff_height


def _check_points(points_mm: Any, closed: bool, doc: dict[str, Any]) -> list[list[float]]:
    fmt, width_mm, eff_height_mm = _paper_bounds(doc)
    if not isinstance(points_mm, list) or not points_mm:
        raise ValueError("points_mm must be a non-empty list of [x, y] points")
    clean: list[list[float]] = []
    for point in points_mm:
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in point)
            or any(not math.isfinite(float(v)) for v in point)
        ):
            raise ValueError(f"invalid point (finite [x, y] numbers required): {point!r}")
        x, y = float(point[0]), float(point[1])
        if not (0.0 <= x <= width_mm and 0.0 <= y <= eff_height_mm):
            raise ValueError(f"point ({x}, {y}) is outside the {fmt} sheet ({width_mm} x {eff_height_mm} mm)")
        clean.append([x, y])
    if closed and len(clean) < 3:
        raise ValueError("closed stroke requires at least 3 points")
    return clean


def _find_stroke(doc: dict[str, Any], stroke_id: str) -> dict[str, Any]:
    for stroke in doc["strokes"]:
        if stroke["stroke_id"] == stroke_id:
            return stroke
    raise KeyError(f"unknown stroke_id: {stroke_id}")


def next_stroke_id(doc: dict[str, Any]) -> str:
    """Next stable stroke id (s-0001, ...), from the highest in use."""
    highest = 0
    for stroke in doc["strokes"]:
        sid = stroke["stroke_id"]
        if isinstance(sid, str) and sid.startswith("s-") and sid[2:].isdigit():
            highest = max(highest, int(sid[2:]))
    return f"s-{highest + 1:04d}"


def add_stroke(
    doc: dict[str, Any],
    points_mm: list[list[float]],
    *,
    closed: bool = False,
    status: str = "pending",
    shape: str | None = None,
    t_start_ms: int | None = None,
    t_end_ms: int | None = None,
    provenance: dict[str, Any] | None = None,
    order_kind: str | None = None,
) -> dict[str, Any]:
    """Append a stroke; assigns a stable id and the next contiguous order.

    Timestamps (``t_start_ms``/``t_end_ms``) are only accepted when the
    document order is ``temporal-stamps``. Points are validated against
    the sheet before anything is written.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    if status == "erased":
        raise ValueError("a new stroke cannot be created erased; erase it after adding")
    if shape is not None and shape not in SHAPES:
        raise ValueError(f"unknown shape: {shape!r}")
    if (t_start_ms is not None or t_end_ms is not None) and doc.get("order_kind") != "temporal-stamps":
        raise ValueError("t_start_ms/t_end_ms require order_kind temporal-stamps")
    if (
        t_start_ms is not None and t_end_ms is not None
        and t_end_ms < t_start_ms
    ):
        raise ValueError("t_end_ms must not be earlier than t_start_ms")
    if order_kind is not None and order_kind != doc.get("order_kind"):
        raise ValueError("per-stroke order_kind must equal the document order_kind")
    points = _check_points(points_mm, closed, doc)
    stroke: dict[str, Any] = {
        "stroke_id": next_stroke_id(doc),
        "order": len(doc["strokes"]),
        "status": status,
        "points_mm": points,
        "closed": bool(closed),
        "length_mm": compute_length_mm(points, closed),
        "order_kind": doc["order_kind"],
    }
    if shape is not None:
        stroke["shape"] = shape
    if t_start_ms is not None:
        stroke["t_start_ms"] = t_start_ms
    if t_end_ms is not None:
        stroke["t_end_ms"] = t_end_ms
    if provenance is not None:
        stroke["provenance"] = copy.deepcopy(provenance)
    doc["strokes"].append(stroke)
    _touch(doc)
    return stroke


def stabilize_stroke(doc: dict[str, Any], stroke_id: str) -> dict[str, Any]:
    """Move a stroke pending -> stable. Points freeze from here on."""
    stroke = _find_stroke(doc, stroke_id)
    if stroke["status"] != "pending":
        raise ValueError(
            f"cannot stabilize stroke {stroke_id} in status {stroke['status']!r}"
        )
    stroke["status"] = "stable"
    _touch(doc)
    return stroke


def erase_stroke(
    doc: dict[str, Any],
    stroke_id: str,
    *,
    reason: str,
    at_ms: int,
    inferred: bool = False,
) -> dict[str, Any]:
    """Erase a stroke: write a tombstone, never remove it from the list.

    ``reason`` is one of ``hand-erase`` | ``page-turn`` | ``manual``.
    ``inferred=True`` marks a *provisional* tombstone (erase inferred from
    a diff between revisions, with no erase event); the reason is then
    omitted until a real event confirms it.
    """
    stroke = _find_stroke(doc, stroke_id)
    if stroke["status"] == "erased":
        raise ValueError(f"stroke {stroke_id} is already erased")
    if at_ms < 0 or isinstance(at_ms, bool):
        raise ValueError("erased_at_ms must be a non-negative integer")
    if inferred:
        stroke.pop("erased_reason", None)
        stroke["erased_inferred"] = True
    else:
        if reason not in ERASE_REASONS:
            raise ValueError(f"unknown erased_reason: {reason!r}")
        stroke["erased_reason"] = reason
        stroke.pop("erased_inferred", None)
    stroke["status"] = "erased"
    stroke["erased_at_ms"] = at_ms
    _touch(doc)
    return stroke


def confirm_erasure(
    doc: dict[str, Any], stroke_id: str, *, reason: str
) -> dict[str, Any]:
    """Confirm a provisional (inferred) tombstone with a real erase reason."""
    stroke = _find_stroke(doc, stroke_id)
    if stroke["status"] != "erased" or stroke.get("erased_inferred") is not True:
        raise ValueError(f"stroke {stroke_id} has no provisional tombstone to confirm")
    if reason not in ERASE_REASONS:
        raise ValueError(f"unknown erased_reason: {reason!r}")
    stroke["erased_reason"] = reason
    stroke.pop("erased_inferred", None)
    _touch(doc)
    return stroke


def live_strokes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Strokes that are not erased — what derived exports include by default."""
    return [s for s in doc["strokes"] if s.get("status") != "erased"]


def copy_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Deep copy of a document (e.g. to keep the previous revision)."""
    return copy.deepcopy(doc)
