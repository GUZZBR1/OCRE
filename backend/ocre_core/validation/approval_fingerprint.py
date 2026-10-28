"""Pure OCRE-AF/1 approval fingerprint encoder.

This module has no access to files, registries, adapters, or application state.
Callers are responsible for obtaining and verifying the inputs before encoding.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping


DOMAIN = b"OCRE:EIR-APPROVAL:FINGERPRINT\x00" + struct.pack(">H", 1)
FIELD_NAMES = (
    "eir_document_sha256", "bundle_manifest_sha256", "profile", "schemas",
    "consumed_artifacts", "execution_parameters", "validator", "adapters",
    "unit_policy", "preparation_policy",
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FingerprintInputError(ValueError):
    """An OCRE-AF/1 input is absent, malformed, or not canonically encodable."""


@dataclass(frozen=True)
class ApprovalFingerprint:
    digest: str
    encoded: bytes


def _escape_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        raise FingerprintInputError("boolean is not a JSON number")
    if isinstance(value, int):
        if abs(value) > 9007199254740991:
            try:
                as_binary64 = float(value)
            except OverflowError as exc:
                raise FingerprintInputError("integer is outside the IEEE-754 number range") from exc
            if not math.isfinite(as_binary64) or int(as_binary64) != value:
                raise FingerprintInputError("integer cannot be represented exactly as an IEEE-754 number")
            return _number(as_binary64)
        return str(value)
    if not math.isfinite(value):
        raise FingerprintInputError("NaN and Infinity are not valid JCS numbers")
    if value == 0:
        return "0"  # JCS serializes negative zero as zero.
    # Python and ECMAScript both use shortest-roundtrip binary64 digits. Convert
    # Python's exponent/decimal choice to ECMAScript's JCS notation thresholds.
    raw = repr(value).lower()
    negative = raw.startswith("-")
    if negative:
        raw = raw[1:]
    if "e" in raw:
        mantissa, exponent_text = raw.split("e")
        exponent = int(exponent_text)
    else:
        mantissa, exponent = raw, 0
    whole, dot, fraction = mantissa.partition(".")
    untrimmed = whole + fraction
    leading_zeroes = len(untrimmed) - len(untrimmed.lstrip("0"))
    digits = untrimmed.lstrip("0") or "0"
    decimal_pos = len(whole) + exponent - leading_zeroes
    # Remove insignificant zeros without changing the decimal point location.
    while len(digits) > 1 and digits.endswith("0"):
        digits = digits[:-1]
    if 0 < decimal_pos <= 21:
        rendered = digits + "0" * (decimal_pos - len(digits)) if decimal_pos >= len(digits) else digits[:decimal_pos] + "." + digits[decimal_pos:]
    elif -6 < decimal_pos <= 0:
        rendered = "0." + "0" * (-decimal_pos) + digits
    else:
        power = decimal_pos - 1
        rendered = digits[0] + (("." + digits[1:]) if len(digits) > 1 else "") + "e" + ("+" if power >= 0 else "") + str(power)
    return ("-" if negative else "") + rendered


def jcs(value: Any) -> bytes:
    """Serialize a JSON value as UTF-8 using RFC 8785 canonical rules."""
    def encode(node: Any) -> str:
        if node is None:
            return "null"
        if node is True:
            return "true"
        if node is False:
            return "false"
        if isinstance(node, str):
            try:
                node.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise FingerprintInputError("JCS strings must contain valid Unicode scalar values") from exc
            return _escape_string(node)
        if isinstance(node, (int, float)):
            return _number(node)
        if isinstance(node, list):
            return "[" + ",".join(encode(item) for item in node) + "]"
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise FingerprintInputError("JCS object keys must be strings")
            try:
                for key in node:
                    key.encode("utf-8", errors="strict")
                keys = sorted(node, key=lambda key: key.encode("utf-16-be", errors="strict"))
            except UnicodeEncodeError as exc:
                raise FingerprintInputError("JCS object keys must contain valid Unicode scalar values") from exc
            return "{" + ",".join(_escape_string(key) + ":" + encode(node[key]) for key in keys) + "}"
        raise FingerprintInputError(f"unsupported JCS value type: {type(node).__name__}")

    try:
        return encode(value).encode("utf-8", errors="strict")
    except RecursionError as exc:
        raise FingerprintInputError("JCS nesting exceeds supported depth") from exc


def _sha_hex(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _HEX_SHA256.fullmatch(value):
        raise FingerprintInputError(f"{label} must be exactly 64 lowercase hexadecimal characters")


def _object(value: Any, keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise FingerprintInputError(f"{label} must contain exactly {sorted(keys)}")


def _ascii_fields(value: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    if any(not isinstance(value[field], str) or not value[field].isascii() for field in fields):
        raise FingerprintInputError(f"{label} identifiers and versions must be ASCII strings")


def _json_payload(field_id: int, value: Any) -> bytes:
    # Canonical sorting must never mutate a caller-owned request/fixture.
    value = deepcopy(value)
    if field_id == 3:
        _object(value, {"id", "version", "sha256"}, "profile")
        _ascii_fields(value, ("id", "version"), "profile")
        _sha_hex(value["sha256"], "profile.sha256")
    elif field_id == 4:
        if not isinstance(value, list):
            raise FingerprintInputError("schemas must be a list")
        seen: set[tuple[str, str]] = set()
        for item in value:
            _object(item, {"document_kind", "schema_version", "schema_sha256"}, "schema entry")
            if not all(isinstance(item[key], str) and item[key].isascii() for key in ("document_kind", "schema_version")):
                raise FingerprintInputError("schema keys must be ASCII strings")
            _sha_hex(item["schema_sha256"], "schema_sha256")
            key = (item["document_kind"], item["schema_version"])
            if key in seen:
                raise FingerprintInputError("duplicate schema key")
            seen.add(key)
        value.sort(key=lambda item: (item["document_kind"].encode("ascii"), item["schema_version"].encode("ascii")))
    elif field_id == 5:
        if not isinstance(value, list):
            raise FingerprintInputError("consumed_artifacts must be a list")
        seen_ids: set[str] = set()
        for item in value:
            _object(item, {"artifact_id", "sha256"}, "consumed artifact")
            artifact_id = item["artifact_id"]
            if not isinstance(artifact_id, str) or not artifact_id.isascii() or artifact_id in seen_ids:
                raise FingerprintInputError("artifact_id must be unique ASCII")
            seen_ids.add(artifact_id)
            _sha_hex(item["sha256"], "artifact sha256")
        value.sort(key=lambda item: item["artifact_id"].encode("ascii"))
    elif field_id == 6:
        if not isinstance(value, dict):
            raise FingerprintInputError("execution_parameters must be the complete simulation_case object")
    elif field_id == 7:
        _object(value, {"id", "version", "implementation_sha256", "policy_version"}, "validator")
        _ascii_fields(value, ("id", "version", "policy_version"), "validator")
        _sha_hex(value["implementation_sha256"], "validator.implementation_sha256")
    elif field_id == 8:
        if not isinstance(value, list):
            raise FingerprintInputError("adapters must be a list")
        seen_adapter: set[tuple[str, str]] = set()
        for item in value:
            _object(item, {"stage_id", "adapter_id", "version", "binary_sha256", "capabilities_sha256"}, "adapter")
            key = (item["stage_id"], item["adapter_id"])
            if not all(isinstance(part, str) and part.isascii() for part in key):
                raise FingerprintInputError("adapter ordering keys must be ASCII strings")
            _ascii_fields(item, ("version",), "adapter")
            if key in seen_adapter:
                raise FingerprintInputError("duplicate adapter key")
            seen_adapter.add(key)
            _sha_hex(item["binary_sha256"], "adapter.binary_sha256")
            _sha_hex(item["capabilities_sha256"], "adapter.capabilities_sha256")
        value.sort(key=lambda item: (item["stage_id"].encode("ascii"), item["adapter_id"].encode("ascii")))
    elif field_id == 9:
        _object(value, {"id", "version", "sha256"}, "unit_policy")
        _ascii_fields(value, ("id", "version"), "unit policy")
        _sha_hex(value["sha256"], "unit_policy.sha256")
    elif field_id == 10:
        _object(value, {"id", "version", "sha256"}, "preparation_policy")
        _ascii_fields(value, ("id", "version"), "preparation policy")
        _sha_hex(value["sha256"], "preparation_policy.sha256")
    return jcs(value)


def calculate_approval_fingerprint(values: Mapping[str, Any]) -> ApprovalFingerprint:
    """Validate, canonically frame, and hash the ten normative field values.

    Fields 1 and 2 must be 32-byte SHA-256 digests. The other fields follow
    the exact JSON shapes in EIR_V02_CONTRACT.md. Preparation policy may be
    ``None`` to encode the normative null field.
    """
    if not isinstance(values, Mapping) or set(values) != set(FIELD_NAMES):
        raise FingerprintInputError("all OCRE-AF/1 fields 1..10 must be present exactly once")
    records: list[bytes] = []
    for field_id, name in enumerate(FIELD_NAMES, 1):
        value = values[name]
        if field_id in (1, 2):
            if not isinstance(value, bytes) or len(value) != 32:
                raise FingerprintInputError(f"{name} must be a 32-byte SHA-256 digest")
            kind, payload = 0x01, value
        elif field_id == 10 and value is None:
            kind, payload = 0x00, b""
        else:
            if value is None:
                raise FingerprintInputError(f"{name} may not be null")
            kind, payload = 0x03, _json_payload(field_id, value)
        records.append(struct.pack(">HBQ", field_id, kind, len(payload)) + payload)
    encoded = DOMAIN + b"".join(records)
    return ApprovalFingerprint(hashlib.sha256(encoded).hexdigest(), encoded)
