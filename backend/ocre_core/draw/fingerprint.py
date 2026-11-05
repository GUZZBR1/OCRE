"""DRAW-AF/1 approval fingerprint for ``draw.paper.v1`` documents.

Same mechanism as OCRE-AF/1, new domain. The framing skeleton is identical
(JCS RFC 8785 canonical JSON + ``>HBQ`` records, 10 fields), but the domain
separator is ``DRAW:PAPER-APPROVAL:FINGERPRINT`` -- the ``OCRE-AF/1`` label
is never reused, so a Draw fingerprint can never collide with an EIR one.

Canonization is NOT reimplemented here: the JCS encoder is reused from
:mod:`ocre_core.validation.approval_fingerprint` (``jcs``), which was
verified against RFC 8785 vectors (see ``backend/tests/draw/``). Plain
``json.dumps(sort_keys=True)`` is not JCS: it sorts object keys by Unicode
code point instead of UTF-16 code units and it does not apply the RFC 8785
number rules, so it must never be substituted.

This module is pure: it operates on dicts/bytes only, touches no files,
registries, adapters, or application state, and never imports the document
builder (``backend/ocre_core/draw/document.py`` is owned by another
implementer). Callers are responsible for collecting and verifying the ten
inputs before encoding -- that is the lesson from OCRE, where nobody
collected them and the pre-flight stayed blocked. A missing or malformed
input is an error here, never a silent default.

Integration interface (for the document builder / DrawPreflight)::

    inputs = {
        "draw_document_sha256": <32-byte sha256 digest of the canonical
                                 draw.paper.v1 document bytes>,
        "bundle_manifest_sha256": <32-byte sha256 digest of the bundle
                                   manifest bytes>,
        "profile": {"id": "draw.paper.v1", "version": "0.1.0",
                    "sha256": <schema-lock sha256>},
        "schemas": [{"document_kind": ..., "schema_version": ...,
                     "schema_sha256": ...}, ...],
        "consumed_artifacts": [{"artifact_id": ..., "sha256": ...}, ...],
        "export_parameters": <complete export_case object (dict)>,
        "validator": {"id": ..., "version": ...,
                      "implementation_sha256": ..., "policy_version": ...},
        "adapters": [{"stage_id": ..., "adapter_id": ..., "version": ...,
                      "binary_sha256": ..., "capabilities_sha256": ...}, ...],
        "unit_policy": {"id": "mm", "version": ...,
                        "sha256": <policy sha256>},
        "preparation_policy": {"id": ..., "version": ...,
                               "sha256": ...} | None,
    }
    result = compute_fingerprint(inputs)  # .digest (hex str), .encoded (bytes)

Field 10 (``preparation_policy``) may be ``None`` to encode the normative
null field, exactly as OCRE-AF/1 does; every other field must be present
and non-null. ``export_parameters`` must be the complete ``export_case``
object -- completeness is the caller's responsibility; the fingerprint
guarantees only that exactly the object passed is what gets hashed.
"""
from __future__ import annotations

import hashlib
import re
import struct
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from ocre_core.validation.approval_fingerprint import (
    FingerprintInputError as _OcreFingerprintInputError,
)
from ocre_core.validation.approval_fingerprint import jcs

# Re-exported so callers and tests have one obvious place for the canonical
# encoder. This is the reused OCRE JCS implementation, not a second copy.
__all__ = [
    "DOMAIN",
    "DRAW_FINGERPRINT_LABEL",
    "FIELD_NAMES",
    "PROFILE_ID",
    "UNIT_POLICY_ID",
    "DrawApprovalFingerprint",
    "DrawFingerprintInputError",
    "compute_fingerprint",
    "jcs",
    "parse_encoded",
]

DRAW_FINGERPRINT_LABEL = "DRAW-AF/1"
DOMAIN = b"DRAW:PAPER-APPROVAL:FINGERPRINT\x00" + struct.pack(">H", 1)
FIELD_NAMES = (
    "draw_document_sha256", "bundle_manifest_sha256", "profile", "schemas",
    "consumed_artifacts", "export_parameters", "validator", "adapters",
    "unit_policy", "preparation_policy",
)
PROFILE_ID = "draw.paper.v1"
UNIT_POLICY_ID = "mm"
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DrawFingerprintInputError(ValueError):
    """A DRAW-AF/1 input is absent, malformed, or not canonically encodable."""


@dataclass(frozen=True)
class DrawApprovalFingerprint:
    digest: str
    encoded: bytes


def _sha_hex(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _HEX_SHA256.fullmatch(value):
        raise DrawFingerprintInputError(
            f"{label} must be exactly 64 lowercase hexadecimal characters")


def _object(value: Any, keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise DrawFingerprintInputError(
            f"{label} must contain exactly {sorted(keys)}")


def _ascii_fields(value: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    if any(not isinstance(value[field], str) or not value[field]
           or not value[field].isascii() for field in fields):
        raise DrawFingerprintInputError(
            f"{label} identifiers and versions must be non-empty ASCII strings")


def _json_payload(field_id: int, value: Any) -> bytes:
    # Canonical sorting must never mutate a caller-owned request/fixture.
    value = deepcopy(value)
    if field_id == 3:
        _object(value, {"id", "version", "sha256"}, "profile")
        if value["id"] != PROFILE_ID:
            raise DrawFingerprintInputError(
                f"profile.id must be {PROFILE_ID!r}")
        _ascii_fields(value, ("version",), "profile")
        _sha_hex(value["sha256"], "profile.sha256")
    elif field_id == 4:
        if not isinstance(value, list) or not value:
            raise DrawFingerprintInputError(
                "schemas must be a non-empty list")
        seen: set[tuple[str, str]] = set()
        for item in value:
            _object(item, {"document_kind", "schema_version", "schema_sha256"},
                    "schema entry")
            if not all(isinstance(item[key], str) and item[key]
                       and item[key].isascii()
                       for key in ("document_kind", "schema_version")):
                raise DrawFingerprintInputError(
                    "schema keys must be non-empty ASCII strings")
            _sha_hex(item["schema_sha256"], "schema_sha256")
            key = (item["document_kind"], item["schema_version"])
            if key in seen:
                raise DrawFingerprintInputError("duplicate schema key")
            seen.add(key)
        value.sort(key=lambda item: (item["document_kind"].encode("ascii"),
                                     item["schema_version"].encode("ascii")))
    elif field_id == 5:
        if not isinstance(value, list) or not value:
            raise DrawFingerprintInputError(
                "consumed_artifacts must be a non-empty list")
        seen_ids: set[str] = set()
        for item in value:
            _object(item, {"artifact_id", "sha256"}, "consumed artifact")
            artifact_id = item["artifact_id"]
            if (not isinstance(artifact_id, str) or not artifact_id
                    or not artifact_id.isascii() or artifact_id in seen_ids):
                raise DrawFingerprintInputError(
                    "artifact_id must be unique non-empty ASCII")
            seen_ids.add(artifact_id)
            _sha_hex(item["sha256"], "artifact sha256")
        value.sort(key=lambda item: item["artifact_id"].encode("ascii"))
    elif field_id == 6:
        if not isinstance(value, dict) or not value:
            raise DrawFingerprintInputError(
                "export_parameters must be the complete export_case object "
                "(a non-empty dict, never a partial subset)")
    elif field_id == 7:
        _object(value, {"id", "version", "implementation_sha256",
                        "policy_version"}, "validator")
        _ascii_fields(value, ("id", "version", "policy_version"), "validator")
        _sha_hex(value["implementation_sha256"],
                 "validator.implementation_sha256")
    elif field_id == 8:
        if not isinstance(value, list) or not value:
            raise DrawFingerprintInputError(
                "adapters must be a non-empty list (at least the pinned "
                "vectorize adapter and the DXF writer)")
        seen_adapter: set[tuple[str, str]] = set()
        for item in value:
            _object(item, {"stage_id", "adapter_id", "version",
                           "binary_sha256", "capabilities_sha256"}, "adapter")
            key = (item["stage_id"], item["adapter_id"])
            if not all(isinstance(part, str) and part
                       and part.isascii() for part in key):
                raise DrawFingerprintInputError(
                    "adapter ordering keys must be non-empty ASCII strings")
            _ascii_fields(item, ("version",), "adapter")
            if key in seen_adapter:
                raise DrawFingerprintInputError("duplicate adapter key")
            seen_adapter.add(key)
            _sha_hex(item["binary_sha256"], "adapter.binary_sha256")
            _sha_hex(item["capabilities_sha256"], "adapter.capabilities_sha256")
        value.sort(key=lambda item: (item["stage_id"].encode("ascii"),
                                     item["adapter_id"].encode("ascii")))
    elif field_id == 9:
        _object(value, {"id", "version", "sha256"}, "unit_policy")
        if value["id"] != UNIT_POLICY_ID:
            raise DrawFingerprintInputError(
                "unit_policy.id must be 'mm': DRAW-AF/1 pins millimetres "
                "only, no other unit may be fingerprinted")
        _ascii_fields(value, ("version",), "unit_policy")
        _sha_hex(value["sha256"], "unit_policy.sha256")
    elif field_id == 10:
        _object(value, {"id", "version", "sha256"}, "preparation_policy")
        _ascii_fields(value, ("id", "version"), "preparation_policy")
        _sha_hex(value["sha256"], "preparation_policy.sha256")
    try:
        return jcs(value)
    except _OcreFingerprintInputError as exc:
        raise DrawFingerprintInputError(str(exc)) from exc


def compute_fingerprint(inputs: Mapping[str, Any]) -> DrawApprovalFingerprint:
    """Validate, canonically frame, and hash the ten normative field values.

    Fields 1 and 2 must be 32-byte SHA-256 digests. The other fields follow
    the exact JSON shapes documented in this module. Preparation policy may
    be ``None`` to encode the normative null field; every other field must
    be present exactly once and non-null -- a missing input is an error,
    never a silent default.
    """
    if not isinstance(inputs, Mapping) or set(inputs) != set(FIELD_NAMES):
        raise DrawFingerprintInputError(
            "all DRAW-AF/1 fields 1..10 must be present exactly once")
    records: list[bytes] = []
    for field_id, name in enumerate(FIELD_NAMES, 1):
        value = inputs[name]
        if field_id in (1, 2):
            if not isinstance(value, bytes) or len(value) != 32:
                raise DrawFingerprintInputError(
                    f"{name} must be a 32-byte SHA-256 digest")
            kind, payload = 0x01, value
        elif field_id == 10 and value is None:
            kind, payload = 0x00, b""
        else:
            if value is None:
                raise DrawFingerprintInputError(f"{name} may not be null")
            kind, payload = 0x03, _json_payload(field_id, value)
        records.append(struct.pack(">HBQ", field_id, kind, len(payload)) + payload)
    encoded = DOMAIN + b"".join(records)
    return DrawApprovalFingerprint(hashlib.sha256(encoded).hexdigest(), encoded)


def parse_encoded(encoded: bytes) -> list[tuple[int, int, bytes]]:
    """Audit helper: parse a DRAW-AF/1 framing back into its ten records.

    Returns ``[(field_id, kind, payload), ...]`` in order. Raises
    :class:`DrawFingerprintInputError` on a wrong domain, a truncated or
    mis-framed record, a field out of order, or trailing bytes. This never
    approves anything -- it only lets an auditor check what was hashed.
    """
    if not isinstance(encoded, bytes) or not encoded.startswith(DOMAIN):
        raise DrawFingerprintInputError(
            "encoded fingerprint must start with the DRAW-AF/1 domain")
    offset = len(DOMAIN)
    records: list[tuple[int, int, bytes]] = []
    while offset < len(encoded):
        if offset + 11 > len(encoded):
            raise DrawFingerprintInputError("truncated fingerprint record header")
        field_id, kind, length = struct.unpack_from(">HBQ", encoded, offset)
        offset += 11
        if offset + length > len(encoded):
            raise DrawFingerprintInputError("truncated fingerprint record payload")
        records.append((field_id, kind, encoded[offset:offset + length]))
        offset += length
    if [record[0] for record in records] != list(range(1, 11)):
        raise DrawFingerprintInputError(
            "fingerprint must contain exactly fields 1..10 in order")
    if offset != len(encoded):
        raise DrawFingerprintInputError("trailing bytes after fingerprint records")
    return records
