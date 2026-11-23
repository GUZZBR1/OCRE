"""Shared strict-JSON, schema and diagnostic machinery for the validators.

Both validator profiles — EIR 0.2 (``validation/validator.py``) and
draw.paper.v1 (``draw/validator_draw.py``) — run on the same fail-closed
mechanism: strict JSON parsing (no duplicate keys, non-finite numbers or
lone surrogates), local-only ``$ref`` schemas, an explicit RFC 3339
``date-time`` format check, RFC 6901 pointer escaping and the
``Diagnostic`` record. The profiles' semantic rules stay in each
validator module; only the shared mechanism lives here so a fix applies
to both.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from datetime import datetime
from typing import Any

from jsonschema import FormatChecker


class StrictJSONError(ValueError):
    """JSON was invalid or used an ambiguous/non-finite representation."""


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise StrictJSONError(f"non-finite JSON number: {value}")


def parse_json_strict(value: Any) -> Any:
    """Accept parsed JSON values or strict UTF-8 JSON text/bytes."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise StrictJSONError("input is not valid UTF-8") from exc
    if isinstance(value, str):
        try:
            parsed = json.loads(value, object_pairs_hook=_pairs_no_duplicates, parse_constant=_reject_constant)
            _walk_finite(parsed)
            return parsed
        # ValueError covers JSONDecodeError, StrictJSONError, and CPython's
        # digit-limit rejection for oversized integer literals.
        except (ValueError, RecursionError) as exc:
            raise StrictJSONError(str(exc)) from exc
    # Reject non-finite values in caller-provided Python objects as well.
    try:
        _walk_finite(value)
    except RecursionError as exc:
        raise StrictJSONError("JSON value exceeds the supported nesting depth") from exc
    return value


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
            # RFC 3339 uses ':' in offsets; fromisoformat validates calendar and clock ranges.
            datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
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
        return {"stage": self.stage, "pointer": self.pointer, "code": self.code,
                "message": self.message, "severity": self.severity}


def _add(items: list[Diagnostic], stage: str, pointer: str, code: str, message: str,
         severity: str = "error") -> None:
    items.append(Diagnostic(stage, pointer, code, message, severity))
