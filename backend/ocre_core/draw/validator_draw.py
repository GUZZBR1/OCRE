"""Strict validation for the ``draw.paper.v1`` document (OCRE-side master).

Mirrors the EIR validator's mechanism (strict JSON, schema-lock SHA-256,
diagnostics instead of silent rewrites) without touching EIR behaviour:
this module is a separate profile for the ``draw-paper`` document family,
not an extension of EIR v0.2.

The validator never rewrites documents. It is fail-closed: any missing
required field, non-mm unit, scale inconsistent with the document's own
paper, point outside the sheet, duplicated/gapped ``order``, duplicated
``stroke_id``, closed stroke with fewer than 3 points, negative
``length_mm``, NaN/Inf, non-monotonic ``revision`` or unknown
``schema_sha256`` is an error.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .paper import DEFAULT_SPEC


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = ROOT / "schemas" / "draw-paper"
SCHEMA_FILE = "0.1.0.schema.json"
LOCK_FILE = "schema-lock-0.1.0.json"
SCHEMA_VERSION = "0.1.0"
SCHEMA_NAME = "draw.paper.v1"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Default paper (A4) — the paper policy lives in ``paper.py`` (D3); these
# names stay as backward-compatible aliases for the default spec only.
# The semantic checks below read the paper from the document itself, so
# a document on another sheet is judged against ITS sheet, not A4.
PAPER_WIDTH_MM = DEFAULT_SPEC.width_mm
PAPER_EFFECTIVE_HEIGHT_MM = DEFAULT_SPEC.effective_height_mm
# Tolerância de borda para círculos analíticos: o fit do vetorizador
# arredonda pontos a 3 casas, então um círculo tangente à folha pode
# exceder cx±r em ~1µm — não é fora da folha, é ruído numérico.
CIRCLE_SHEET_EPS_MM = 1e-3
# A scale is consistent with the paper when it maps the canvas onto the
# effective sheet within 0.01 mm on each axis.
SCALE_TOLERANCE_MM = 0.01
LENGTH_TOLERANCE_MM = 0.01

STATUS_PENDING = "pending"
STATUS_STABLE = "stable"
STATUS_ERASED = "erased"
ALLOWED_TRANSITIONS = {
    (STATUS_PENDING, STATUS_PENDING),
    (STATUS_PENDING, STATUS_STABLE),
    (STATUS_PENDING, STATUS_ERASED),
    (STATUS_STABLE, STATUS_STABLE),
    (STATUS_STABLE, STATUS_ERASED),
    (STATUS_ERASED, STATUS_ERASED),
}


class StrictJSONError(ValueError):
    """JSON was invalid or used an ambiguous/non-finite representation."""


class DrawValidationError(ValueError):
    """Raised by validate_draw_document_or_raise; carries the report."""

    def __init__(self, report: "DrawValidationReport") -> None:
        self.report = report
        errors = "; ".join(
            f"{d.pointer} [{d.code}] {d.message}" for d in report.errors
        )
        super().__init__(f"invalid draw.paper.v1 document: {errors}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise StrictJSONError(f"non-finite JSON number: {value}")


def _walk_finite(node: Any) -> None:
    if isinstance(node, float) and not math.isfinite(node):
        raise StrictJSONError("non-finite JSON number")
    if isinstance(node, str):
        try:
            node.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StrictJSONError("JSON strings must not contain lone surrogates") from exc
    if isinstance(node, dict):
        for key, child in node.items():
            if not isinstance(key, str):
                raise StrictJSONError("JSON object keys must be strings")
            _walk_finite(key)
            _walk_finite(child)
    elif isinstance(node, (list, tuple)):
        for child in node:
            _walk_finite(child)


def parse_json_strict(value: Any) -> Any:
    """Accept parsed JSON values or strict UTF-8 JSON text/bytes."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise StrictJSONError("input is not valid UTF-8") from exc
    if isinstance(value, str):
        try:
            parsed = json.loads(
                value,
                object_pairs_hook=_pairs_no_duplicates,
                parse_constant=_reject_constant,
            )
            _walk_finite(parsed)
            return parsed
        # ValueError covers JSONDecodeError, StrictJSONError, and CPython's
        # digit-limit rejection for oversized integer literals.
        except (ValueError, RecursionError) as exc:
            raise StrictJSONError(str(exc)) from exc
    try:
        _walk_finite(value)
    except RecursionError as exc:
        raise StrictJSONError("JSON value exceeds the supported nesting depth") from exc
    return value


def _assert_local_refs(node: Any) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref is not None and (not isinstance(ref, str) or not ref.startswith("#")):
            raise ValueError("schema contains a non-local $ref")
        for child in node.values():
            _assert_local_refs(child)
    elif isinstance(node, list):
        for child in node:
            _assert_local_refs(child)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def load_bundled_schema() -> tuple[dict[str, Any], str, str, tuple[str, ...]]:
    """Return (schema, version, sha256, legacy_digests) for the bundled
    draw-paper schema.

    The digest is the SHA-256 of the schema file's bytes and must match
    ``schema-lock-0.1.0.json`` — the same schema-lock mechanism the EIR
    validator uses. A mismatch means the bundled schema was edited without
    updating the lock, and validation refuses to run. ``legacy_digests``
    lists previously locked digests (``legacy_sha256`` in the lock):
    documents written against an older revision of the same schema keep
    validating, while a digest never locked still refuses.
    """
    try:
        path = SCHEMA_DIR / SCHEMA_FILE
        schema = json.loads(path.read_text(encoding="utf-8"))
        _assert_local_refs(schema)
        Draft202012Validator.check_schema(schema)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lock = json.loads((SCHEMA_DIR / LOCK_FILE).read_text(encoding="utf-8"))
        locked_entry = next(
            (
                entry
                for entry in lock.get("schemas", [])
                if entry.get("path") == SCHEMA_FILE
            ),
            None,
        )
        locked = locked_entry.get("sha256") if locked_entry else None
        legacy = locked_entry.get("legacy_sha256", ()) if locked_entry else ()
        if (
            not isinstance(legacy, list)
            or not all(isinstance(d, str) and SHA256_RE.fullmatch(d) for d in legacy)
        ):
            raise ValueError(
                "schema-lock-0.1.0.json has a malformed legacy_sha256 list"
            )
        if lock.get("lock_version") != SCHEMA_VERSION or locked != digest:
            raise ValueError(
                "bundled schema digest does not match schema-lock-0.1.0.json"
            )
        return schema, SCHEMA_VERSION, digest, tuple(legacy)
    except RecursionError as exc:
        raise ValueError("bundled schema exceeds the supported nesting depth") from exc


def bundled_schema_sha256() -> str:
    """SHA-256 of the bundled schema file's bytes (the schema-lock digest)."""
    return load_bundled_schema()[2]


def _format_checker() -> FormatChecker:
    """Assert RFC 3339 date-time even when optional jsonschema extras are absent."""
    checker = FormatChecker()

    @checker.checks("date-time")
    def valid_datetime(value: Any) -> bool:
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            value,
        ):
            return False
        try:
            datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
            return True
        except ValueError:
            return False

    return checker


def _pointer(parts: tuple[Any, ...] | list[Any]) -> str:
    def esc(part: Any) -> str:
        return str(part).replace("~", "~0").replace("/", "~1")

    return "" if not parts else "/" + "/".join(esc(p) for p in parts)


@dataclass(frozen=True)
class Diagnostic:
    stage: str
    pointer: str
    code: str
    message: str
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {
            "stage": self.stage,
            "pointer": self.pointer,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class DrawValidationReport:
    status: str
    diagnostics: tuple[Diagnostic, ...]
    schema_sha256: str | None
    schema_version: str = SCHEMA_VERSION

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == "error")

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_version": SCHEMA_VERSION,
            "status": self.status,
            "schema": SCHEMA_NAME,
            "schema_version": self.schema_version,
            "schema_sha256": self.schema_sha256,
            "diagnostics": [d.as_dict() for d in self.diagnostics],
        }


def _add(
    items: list[Diagnostic],
    stage: str,
    pointer: str,
    code: str,
    message: str,
    severity: str = "error",
) -> None:
    items.append(Diagnostic(stage, pointer, code, message, severity))


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def polyline_length_mm(points: list[list[float]], closed: bool) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        total += math.hypot(x2 - x1, y2 - y1)
    if closed and len(points) > 1:
        (x1, y1), (x2, y2) = points[-1], points[0]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def _parse_datetime(value: Any) -> datetime | None:
    """RFC 3339 com timezone, ou ``None`` — nunca um datetime naive.

    Um ``created_at``/``updated_at`` sem offset falha o format check do
    schema; devolvê-lo naive faria a comparação ``updated < created``
    explodir em TypeError (naive x aware), abortando o ``_semantic``
    inteiro e escondendo TODOS os diagnósticos seguintes atrás de um
    ``invalid_document_shape`` genérico.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _semantic(
    doc: dict[str, Any],
    diagnostics: list[Diagnostic],
    previous: dict[str, Any] | None,
) -> None:
    if doc.get("units") != "mm":
        _add(diagnostics, "semantic", "/units", "unsupported_unit",
             "units must be mm")
    if doc.get("coordinate_system") != "sheet_top_left_y_down":
        _add(diagnostics, "semantic", "/coordinate_system",
             "unsupported_coordinate_system",
             "coordinate_system must be sheet_top_left_y_down")

    paper = doc.get("paper")
    # The sheet the document declares (D3): scale and point bounds are
    # judged against the document's own paper, never against a hardcoded
    # A4. Missing fields fall back to the default spec — the schema
    # already flags a missing paper block as a structural error.
    paper_width = PAPER_WIDTH_MM
    paper_eff_height = PAPER_EFFECTIVE_HEIGHT_MM
    paper_format = DEFAULT_SPEC.format
    if isinstance(paper, dict):
        if isinstance(paper.get("format"), str) and paper["format"]:
            paper_format = paper["format"]
        if _is_number(paper.get("width_mm")):
            paper_width = float(paper["width_mm"])
        if _is_number(paper.get("effective_height_mm")):
            paper_eff_height = float(paper["effective_height_mm"])
        scale = paper.get("scale_mm_per_px")
        canvas_w = paper.get("canvas_width_px")
        canvas_h = paper.get("canvas_height_px")
        if _is_number(scale) and _is_number(canvas_w) and _is_number(canvas_h):
            if abs(float(scale) * float(canvas_w) - paper_width) > SCALE_TOLERANCE_MM:
                _add(diagnostics, "semantic", "/paper/scale_mm_per_px",
                     "scale_incompatible_with_paper",
                     f"scale_mm_per_px * canvas_width_px must equal the paper width ({paper_width} mm)")
            if abs(float(scale) * float(canvas_h) - paper_eff_height) > SCALE_TOLERANCE_MM:
                _add(diagnostics, "semantic", "/paper/scale_mm_per_px",
                     "scale_incompatible_with_paper",
                     f"scale_mm_per_px * canvas_height_px must equal the effective paper height ({paper_eff_height} mm)")

    created = _parse_datetime(doc.get("created_at"))
    updated = _parse_datetime(doc.get("updated_at"))
    if created is not None and updated is not None and updated < created:
        _add(diagnostics, "semantic", "/updated_at", "updated_before_created",
             "updated_at must not be earlier than created_at")

    strokes = doc.get("strokes")
    if not isinstance(strokes, list):
        return
    doc_order_kind = doc.get("order_kind")

    seen_ids: set[str] = set()
    orders: list[int] = []
    for i, stroke in enumerate(strokes):
        if not isinstance(stroke, dict):
            continue
        ptr = f"/strokes/{i}"
        sid = stroke.get("stroke_id")
        if isinstance(sid, str):
            if sid in seen_ids:
                _add(diagnostics, "semantic", f"{ptr}/stroke_id", "duplicate_id",
                     f"duplicate stroke_id: {sid}")
            else:
                seen_ids.add(sid)
        order = stroke.get("order")
        if isinstance(order, int) and not isinstance(order, bool):
            orders.append(order)

        status = stroke.get("status")
        if status == STATUS_ERASED:
            if stroke.get("erased_at_ms") is None:
                _add(diagnostics, "semantic", f"{ptr}/erased_at_ms",
                     "missing_erasure_event",
                     "erased stroke requires erased_at_ms")
            if stroke.get("erased_reason") is None and stroke.get("erased_inferred") is not True:
                _add(diagnostics, "semantic", f"{ptr}/erased_reason",
                     "missing_erasure_reason",
                     "erased stroke requires erased_reason unless erased_inferred is true")
        elif status in (STATUS_PENDING, STATUS_STABLE):
            for field in ("erased_at_ms", "erased_reason", "erased_inferred"):
                if field in stroke:
                    _add(diagnostics, "semantic", f"{ptr}/{field}",
                         "unexpected_erasure_field",
                         f"non-erased stroke must not carry {field}")

        effective_order_kind = stroke.get("order_kind", doc_order_kind)
        if stroke.get("order_kind") is not None and stroke.get("order_kind") != doc_order_kind:
            _add(diagnostics, "semantic", f"{ptr}/order_kind",
                 "order_kind_mismatch",
                 "per-stroke order_kind must equal the document order_kind")
        has_stamps = "t_start_ms" in stroke or "t_end_ms" in stroke
        if has_stamps and effective_order_kind != "temporal-stamps":
            _add(diagnostics, "semantic", ptr, "timestamps_without_stamps",
                 "t_start_ms/t_end_ms require order_kind temporal-stamps")
        t_start, t_end = stroke.get("t_start_ms"), stroke.get("t_end_ms")
        if (
            isinstance(t_start, int) and not isinstance(t_start, bool)
            and isinstance(t_end, int) and not isinstance(t_end, bool)
            and t_end < t_start
        ):
            _add(diagnostics, "semantic", f"{ptr}/t_end_ms", "invalid_time_range",
                 "t_end_ms must not be earlier than t_start_ms")

        circle = stroke.get("circle")
        if circle is not None:
            if stroke.get("shape") != "circle":
                _add(diagnostics, "semantic", f"{ptr}/circle",
                     "circle_without_circle_shape",
                     "circle parameters require shape 'circle'")
            if (
                not isinstance(circle, dict)
                or sorted(circle) != ["cx_mm", "cy_mm", "r_mm"]
                or not all(_is_number(circle.get(k)) for k in ("cx_mm", "cy_mm", "r_mm"))
                or float(circle["r_mm"]) <= 0
            ):
                _add(diagnostics, "semantic", f"{ptr}/circle",
                     "invalid_circle",
                     "circle requires finite cx_mm, cy_mm and r_mm > 0")
            elif not (
                -CIRCLE_SHEET_EPS_MM <= float(circle["cx_mm"]) - float(circle["r_mm"])
                and float(circle["cx_mm"]) + float(circle["r_mm"]) <= paper_width + CIRCLE_SHEET_EPS_MM
                and -CIRCLE_SHEET_EPS_MM <= float(circle["cy_mm"]) - float(circle["r_mm"])
                and float(circle["cy_mm"]) + float(circle["r_mm"]) <= paper_eff_height + CIRCLE_SHEET_EPS_MM
            ):
                _add(diagnostics, "semantic", f"{ptr}/circle",
                     "circle_outside_sheet",
                     f"circle is outside the {paper_format} sheet ({paper_width} x {paper_eff_height} mm)")

        points = stroke.get("points_mm")
        closed = stroke.get("closed") is True
        if isinstance(points, list):
            if closed and len(points) < 3:
                _add(diagnostics, "semantic", f"{ptr}/points_mm",
                     "closed_stroke_too_short",
                     "closed stroke requires at least 3 points")
            clean: list[list[float]] = []
            points_ok = True
            for j, point in enumerate(points):
                if (
                    not isinstance(point, list)
                    or len(point) != 2
                    or not all(_is_number(v) for v in point)
                ):
                    points_ok = False
                    continue
                x, y = float(point[0]), float(point[1])
                clean.append([x, y])
                if not (0.0 <= x <= paper_width
                        and 0.0 <= y <= paper_eff_height):
                    _add(diagnostics, "semantic", f"{ptr}/points_mm/{j}",
                         "point_outside_sheet",
                         f"point ({x}, {y}) is outside the {paper_format} sheet ({paper_width} x {paper_eff_height} mm)")
            length = stroke.get("length_mm")
            if points_ok and _is_number(length) and len(clean) >= 1:
                expected = polyline_length_mm(clean, closed)
                tolerance = max(LENGTH_TOLERANCE_MM, 1e-6 * expected)
                if abs(float(length) - expected) > tolerance:
                    _add(diagnostics, "semantic", f"{ptr}/length_mm",
                         "length_mismatch",
                         f"length_mm ({length}) does not match points_mm ({expected:.4f})")

    if orders:
        if len(set(orders)) != len(orders):
            _add(diagnostics, "semantic", "/strokes", "duplicate_order",
                 "stroke order values must be unique")
        if set(orders) != set(range(len(strokes))):
            _add(diagnostics, "semantic", "/strokes", "order_gap",
                 "stroke order must be 0-based, unique and contiguous")

    if previous is not None:
        _check_previous(doc, previous, strokes, seen_ids, diagnostics)


def _check_previous(
    doc: dict[str, Any],
    previous: dict[str, Any],
    strokes: Any,
    seen_ids: set[str],
    diagnostics: list[Diagnostic],
) -> None:
    if doc.get("doc_id") != previous.get("doc_id"):
        _add(diagnostics, "semantic", "/doc_id", "doc_id_changed",
             "doc_id is immutable across revisions")
    if doc.get("created_at") != previous.get("created_at"):
        _add(diagnostics, "semantic", "/created_at", "created_at_changed",
             "created_at is immutable across revisions")
    prev_revision = previous.get("revision")
    revision = doc.get("revision")
    if (
        isinstance(revision, int) and not isinstance(revision, bool)
        and isinstance(prev_revision, int) and not isinstance(prev_revision, bool)
    ):
        if revision != prev_revision + 1:
            _add(diagnostics, "semantic", "/revision", "revision_not_monotonic",
                 "revision must increase by exactly 1 per mutation")

    prev_strokes = previous.get("strokes")
    if not isinstance(prev_strokes, list):
        return
    prev_by_id: dict[str, dict[str, Any]] = {}
    for stroke in prev_strokes:
        if isinstance(stroke, dict) and isinstance(stroke.get("stroke_id"), str):
            prev_by_id[stroke["stroke_id"]] = stroke
    for sid in prev_by_id:
        if sid not in seen_ids:
            _add(diagnostics, "semantic", "/strokes", "stroke_removed",
                 f"stroke {sid} was removed; erasing keeps a tombstone instead")
    for i, stroke in enumerate(strokes):
        if not isinstance(stroke, dict):
            continue
        sid = stroke.get("stroke_id")
        prev = prev_by_id.get(sid) if isinstance(sid, str) else None
        if prev is None:
            continue
        ptr = f"/strokes/{i}"
        prev_status, status = prev.get("status"), stroke.get("status")
        if (
            prev_status in (STATUS_PENDING, STATUS_STABLE, STATUS_ERASED)
            and status in (STATUS_PENDING, STATUS_STABLE, STATUS_ERASED)
            and (prev_status, status) not in ALLOWED_TRANSITIONS
        ):
            _add(diagnostics, "semantic", f"{ptr}/status", "invalid_status_transition",
                 f"status transition {prev_status} -> {status} is not allowed")
        if prev_status in (STATUS_STABLE, STATUS_ERASED):
            immutable = [
                "points_mm", "closed", "length_mm", "shape", "circle",
                "order", "t_start_ms", "t_end_ms",
            ]
            if prev_status == STATUS_ERASED:
                immutable.append("erased_at_ms")
                if prev.get("erased_inferred") is not True:
                    # Tombstone provisional pode ser confirmada depois
                    # (confirm_erasure grava erased_reason e remove
                    # erased_inferred) — a tombstone confirmada é final.
                    immutable += ["erased_reason", "erased_inferred"]
            for field in immutable:
                if stroke.get(field) != prev.get(field):
                    _add(diagnostics, "semantic", f"{ptr}/{field}",
                         "stable_points_mutated",
                         f"{field} is immutable after a stroke becomes stable")


def validate_draw_document(
    doc: Any,
    *,
    schema: dict[str, Any] | None = None,
    previous: Any = None,
) -> DrawValidationReport:
    """Validate a draw.paper.v1 document; return a report (never rewrites).

    ``doc`` may be a parsed object or strict JSON text/bytes. ``schema``
    overrides the bundled schema (its expected digest is then the SHA-256
    of the schema's canonical JSON); by default the bundled schema under
    ``schemas/draw-paper/`` is used and its digest must match the lock.
    ``previous`` is the prior revision of the same document, when checking
    revision monotonicity, status transitions and stable-point immutability.
    """
    diagnostics: list[Diagnostic] = []
    parsed: dict[str, Any] | None = None
    try:
        parsed = parse_json_strict(doc)
    except StrictJSONError as exc:
        _add(diagnostics, "parsing", "", "invalid_json", str(exc))
    if parsed is not None and not isinstance(parsed, dict):
        _add(diagnostics, "parsing", "", "invalid_document_type",
             "draw.paper.v1 document must be a JSON object")
        parsed = None

    expected_digest: str | None = None
    legacy_digests: tuple[str, ...] = ()
    schema_version = SCHEMA_VERSION
    schema_doc: dict[str, Any] | None = None
    if schema is None:
        try:
            (schema_doc, schema_version, expected_digest,
             legacy_digests) = load_bundled_schema()
        except (OSError, ValueError, RecursionError) as exc:
            _add(diagnostics, "structural", "", "schema_unavailable", str(exc))
    else:
        try:
            schema_doc = parse_json_strict(schema)
            if not isinstance(schema_doc, dict):
                raise ValueError("supplied schema must be a JSON object")
            _assert_local_refs(schema_doc)
            Draft202012Validator.check_schema(schema_doc)
            expected_digest = hashlib.sha256(_canonical_bytes(schema_doc)).hexdigest()
        except (StrictJSONError, ValueError, RecursionError) as exc:
            _add(diagnostics, "structural", "", "schema_unavailable", str(exc))

    if schema_doc is not None and parsed is not None:
        errors = sorted(
            Draft202012Validator(
                schema_doc, format_checker=_format_checker()
            ).iter_errors(parsed),
            key=lambda e: (_pointer(e.absolute_path), e.validator or "", str(e.message)),
        )
        for error in errors:
            _add(diagnostics, "structural", _pointer(error.absolute_path),
                 "schema_validation", error.message)

    if parsed is not None and expected_digest is not None:
        declared = parsed.get("schema_sha256")
        if isinstance(declared, str) and SHA256_RE.fullmatch(declared):
            if declared != expected_digest and declared not in legacy_digests:
                _add(diagnostics, "schema-lock", "/schema_sha256",
                     "unknown_schema_digest",
                     "schema_sha256 does not match a locked draw.paper.v1 schema digest")

    prev_parsed: dict[str, Any] | None = None
    if previous is not None:
        try:
            prev_parsed = parse_json_strict(previous)
            if not isinstance(prev_parsed, dict):
                _add(diagnostics, "semantic", "", "invalid_previous_document",
                     "previous document must be a draw.paper.v1 object")
                prev_parsed = None
        except StrictJSONError as exc:
            _add(diagnostics, "semantic", "", "invalid_previous_document", str(exc))

    if parsed is not None:
        try:
            _semantic(parsed, diagnostics, prev_parsed)
        except (TypeError, KeyError, AttributeError) as exc:
            _add(diagnostics, "semantic", "", "invalid_document_shape",
                 f"semantic evaluation could not proceed: {type(exc).__name__}")

    diagnostics.sort(key=lambda d: (d.stage, d.pointer, d.code, d.message, d.severity))
    status = "invalid" if any(d.severity == "error" for d in diagnostics) else "valid"
    return DrawValidationReport(status, tuple(diagnostics), expected_digest, schema_version)


def validate_draw_document_or_raise(
    doc: Any,
    *,
    schema: dict[str, Any] | None = None,
    previous: Any = None,
) -> DrawValidationReport:
    """Validate and return the report, or raise DrawValidationError."""
    report = validate_draw_document(doc, schema=schema, previous=previous)
    if not report.valid:
        raise DrawValidationError(report)
    return report
