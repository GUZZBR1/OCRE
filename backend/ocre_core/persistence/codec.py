"""Versioned, allowlisted JSON codec for SQLite persistence values."""
from __future__ import annotations

import base64
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

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
_HINTS = {t.__name__: get_type_hints(t) for t in _TYPES.values()}


class InvalidPersistedValue(ValueError):
    """Stored JSON is malformed or contains a type outside the allowlist."""


def _matches(value, hint):
    """Check a decoded/encoded field value against its declared dataclass type."""
    if hint is Any:
        return True
    if hint is int and isinstance(value, bool):
        return False
    origin = get_origin(hint)
    if origin is None:
        return isinstance(value, hint)
    if origin in (Union, UnionType):
        return any(_matches(value, arg) for arg in get_args(hint))
    if not isinstance(value, origin):
        return False
    args = get_args(hint)
    if args and args[-1] is Ellipsis:
        return all(_matches(item, args[0]) for item in value)
    if origin is tuple and args:
        return len(value) == len(args) and all(
            _matches(item, arg) for item, arg in zip(value, args)
        )
    return True


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
    if isinstance(value, dict):
        items = {}
        for k, v in value.items():
            if not isinstance(k, str): raise TypeError("persisted dict keys must be strings")
            items[k] = _encode(v)
        # A "$type" member would collide with the tagged-value envelope on
        # decode; escape it so the dict round-trips as data, not as a tag.
        if "$type" in items: return {"$type": "dict", "items": items}
        return items
    if is_dataclass(value) and type(value).__name__ in _TYPES:
        name = type(value).__name__
        hints = _HINTS[name]
        for f in fields(value):
            if not _matches(getattr(value, f.name), hints[f.name]):
                raise TypeError(f"{name}.{f.name} does not match its declared type")
        return {"$type": "record", "name": name, "fields": {f.name: _encode(getattr(value, f.name)) for f in fields(value)}}
    raise TypeError(f"unsupported persisted value {type(value).__name__}")


def _decode(value):
    if isinstance(value, list): return [_decode(v) for v in value]
    if not isinstance(value, dict): return value
    tag=value.get("$type")
    if tag is None: return {k:_decode(v) for k,v in value.items()}
    if tag == "dict":
        items=value.get("items")
        if not isinstance(items,dict): raise InvalidPersistedValue("dict items must be an object")
        return {k:_decode(v) for k,v in items.items()}
    if tag == "bytes": return base64.b64decode(value["value"], validate=True)
    if tag == "datetime": return datetime.fromisoformat(value["value"])
    if tag == "tuple":
        items=value.get("items")
        if not isinstance(items,list): raise InvalidPersistedValue("tuple items must be a list")
        return tuple(_decode(v) for v in items)
    if tag == "enum":
        cls=_ENUMS.get(value.get("name"))
        if cls is None: raise InvalidPersistedValue("unknown enum type")
        return cls(value["value"])
    if tag == "record":
        cls=_TYPES.get(value.get("name"))
        if cls is None: raise InvalidPersistedValue("unknown record type")
        hints=_HINTS[cls.__name__]
        supplied=value.get("fields")
        if not isinstance(supplied,dict) or set(supplied) != set(hints): raise InvalidPersistedValue("record fields do not match schema")
        decoded={k:_decode(v) for k,v in supplied.items()}
        if any(not _matches(v,hints[k]) for k,v in decoded.items()): raise InvalidPersistedValue("record field type does not match schema")
        return cls(**decoded)
    raise InvalidPersistedValue("unknown persisted value tag")


def dumps(value) -> bytes:
    return json.dumps({"version": 1, "value": _encode(value)}, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _reject_constant(value: str) -> None:
    raise InvalidPersistedValue(f"non-finite JSON number: {value}")


def loads(payload: bytes):
    if not isinstance(payload, (bytes, bytearray)):
        raise InvalidPersistedValue("persisted payload must be bytes")
    try:
        doc=json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(doc,dict) or set(doc) != {"version","value"} or type(doc["version"]) is not int or doc["version"] != 1:
            raise InvalidPersistedValue("unsupported persistence codec version")
        return _decode(doc["value"])
    except InvalidPersistedValue:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidPersistedValue("invalid persisted JSON value") from exc
