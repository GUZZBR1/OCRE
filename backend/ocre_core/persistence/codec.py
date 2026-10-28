"""Versioned, allowlisted JSON codec for SQLite persistence values."""
from __future__ import annotations

import base64
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum

from ocre_core.jobs import (
    AdapterExecutionState, AdapterExecutionTransition, CancellationEvidence,
    CancellationEvidenceKind, JobState, JobTransition,
)
from ocre_core.persistence.jobs import (
    AdapterExecutionSnapshot, CommitReceipt, ExecutionSubmitted,
    IdempotencyRecord, JobSnapshot, OutboxMessage,
)
from ocre_core.sync import BatchReceipt, SyncBatch, SyncOperation

_TYPES = {t.__name__: t for t in (
    AdapterExecutionSnapshot, CommitReceipt, ExecutionSubmitted,
    IdempotencyRecord, JobSnapshot, OutboxMessage, CancellationEvidence,
    JobTransition, AdapterExecutionTransition, SyncBatch, SyncOperation,
    BatchReceipt,
)}
_ENUMS = {t.__name__: t for t in (
    AdapterExecutionState, CancellationEvidenceKind, JobState,
)}


class InvalidPersistedValue(ValueError):
    """Stored JSON is malformed or contains a type outside the allowlist."""


def _encode(value):
    if isinstance(value, Enum):
        if type(value).__name__ not in _ENUMS: raise TypeError(f"unsupported enum {type(value).__name__}")
        return {"$type": "enum", "name": type(value).__name__, "value": value.value}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"$type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, tuple): return {"$type": "tuple", "items": [_encode(v) for v in value]}
    if isinstance(value, list): return [_encode(v) for v in value]
    if isinstance(value, dict): return {str(k): _encode(v) for k,v in value.items()}
    if is_dataclass(value) and type(value).__name__ in _TYPES:
        return {"$type": "record", "name": type(value).__name__, "fields": {f.name: _encode(getattr(value,f.name)) for f in fields(value)}}
    raise TypeError(f"unsupported persisted value {type(value).__name__}")


def _decode(value):
    if isinstance(value, list): return [_decode(v) for v in value]
    if not isinstance(value, dict): return value
    tag=value.get("$type")
    if tag is None: return {k:_decode(v) for k,v in value.items()}
    if tag == "bytes": return base64.b64decode(value["value"], validate=True)
    if tag == "datetime": return datetime.fromisoformat(value["value"])
    if tag == "tuple": return tuple(_decode(v) for v in value["items"])
    if tag == "enum":
        cls=_ENUMS.get(value.get("name"))
        if cls is None: raise InvalidPersistedValue("unknown enum type")
        return cls(value["value"])
    if tag == "record":
        cls=_TYPES.get(value.get("name"))
        if cls is None: raise InvalidPersistedValue("unknown record type")
        expected={f.name for f in fields(cls)}
        supplied=value.get("fields")
        if not isinstance(supplied,dict) or set(supplied) != expected: raise InvalidPersistedValue("record fields do not match schema")
        return cls(**{k:_decode(v) for k,v in supplied.items()})
    raise InvalidPersistedValue("unknown persisted value tag")


def dumps(value) -> bytes:
    return json.dumps({"version": 1, "value": _encode(value)}, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def loads(payload: bytes):
    try:
        doc=json.loads(payload.decode("utf-8"))
        if doc.get("version") != 1 or set(doc) != {"version", "value"}: raise InvalidPersistedValue("unsupported persistence codec version")
        return _decode(doc["value"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, InvalidPersistedValue): raise
        raise InvalidPersistedValue("invalid persisted JSON value") from exc
