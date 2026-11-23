"""Bounded, read-only structural observation for emitted DXF R2000 / STEP.

Only expected-format files (``.dxf`` for DXF; ``.step``/``.stp`` for STEP)
are inspected. Model findings remain advisory; confirmed errors come only
from these independent parsers/structural checks. The adapter receives the
deep-copied document plus artifact size/hash/64-byte preview, never CAD bytes.
"""
from __future__ import annotations

import hashlib
import io
import json
from typing import Any

MAX_ARTIFACT_BYTES = 2_000_000
MAX_DOCUMENT_BYTES = 256_000
MAX_FINDINGS = 32
MAX_TEXT = 1000
MAX_ARTIFACTS = 16
MAX_TOTAL_ARTIFACT_BYTES = 4_000_000
_EXTENSIONS = {"dxf": (".dxf",), "step": (".step", ".stp")}


def _check_dxf(name: str, data: bytes) -> list[dict[str, str]]:
    try:
        import ezdxf
        drawing = ezdxf.read(io.StringIO(data.decode("cp1252", errors="surrogateescape")))
        if drawing.dxfversion != "AC1015":
            raise ValueError(f"expected AC1015/R2000, got {drawing.dxfversion}")
        # Force entity decoding and section table access on the parsed copy.
        list(drawing.modelspace())
        return []
    except Exception as exc:
        return [{"kind": "invalid-dxf-r2000", "artifact": name,
                 "detail": f"DXF R2000 parser rejected artifact ({type(exc).__name__})"}]


def _check_step(name: str, data: bytes) -> list[dict[str, str]]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        text = ""
    lines = [line.strip().upper() for line in text.splitlines() if line.strip()]
    # ISO 10303-21 physical-file structure: exact delimiters and ordered
    # HEADER/DATA/ENDSEC sections. This does not assert geometric validity.
    valid = (lines[:2] == ["ISO-10303-21;", "HEADER;"]
             and lines[-2:] == ["ENDSEC;", "END-ISO-10303-21;"]
             and "DATA;" in lines and lines.count("ENDSEC;") == 2)
    if valid:
        data_start = lines.index("DATA;")
        valid = (data_start > 2 and lines[data_start - 1] == "ENDSEC;"
                 and any(line.startswith("FILE_SCHEMA(") for line in lines[2:data_start - 1]))
    return [] if valid else [{"kind": "invalid-step-structure", "artifact": name,
                              "detail": "STEP lacks ordered ISO-10303-21 HEADER/FILE_SCHEMA/DATA/ENDSEC structure"}]


def _json_findings(raw: Any) -> list[dict[str, Any]]:
    """Normalize untrusted adapter output into bounded JSON-safe findings."""
    if not isinstance(raw, (list, tuple)):
        return [{"kind": "invalid-model-response", "detail": "expected a list of findings"}]
    normalized = []
    for entry in raw[:MAX_FINDINGS]:
        if isinstance(entry, dict):
            source = entry
        else:
            source = {key: getattr(entry, key) for key in ("kind", "detail", "stroke_id", "evidence")
                      if isinstance(getattr(entry, key, None), (str, int, float, bool, type(None)))}
        item = {}
        for key in ("kind", "detail", "stroke_id", "evidence"):
            value = source.get(key)
            if isinstance(value, (str, int, float, bool)):
                item[key] = str(value)[:MAX_TEXT]
        item.setdefault("kind", "advisory")
        item.setdefault("detail", "Model reported a finding")
        normalized.append(item)
    if len(raw) > MAX_FINDINGS:
        normalized.append({"kind": "truncated-model-response", "detail": "additional findings omitted"})
    # Enforce serializability (e.g. reject NaN and exotic values defensively).
    return json.loads(json.dumps(normalized, ensure_ascii=True, allow_nan=False))


def _unavailable(detail: str) -> dict[str, Any]:
    """The honest result whenever observation cannot run — never a guess."""
    return {"status": "unavailable", "findings": [], "alerts": [], "detail": detail}


def observe_conversion(document: dict[str, Any], adapter: Any, *, stage: str,
                       artifacts: dict[str, bytes]) -> dict[str, Any]:
    """Observe ``stage`` (``dxf``/``step``) over filename-to-bytes artifacts.

    Only matching extensions count; a missing expected artifact is confirmed.
    Artifact inspection is capped at 2 MB and document context at 256 KB.
    Oversized input is ``unavailable`` (never a guessed error), and provider
    output is restricted to 32 JSON findings with four bounded string fields.
    """
    if not isinstance(stage, str) or stage.lower() not in _EXTENSIONS:
        raise ValueError("stage must be 'dxf' or 'step'")
    stage = stage.lower()
    if not isinstance(artifacts, dict) or any(not isinstance(n, str) for n in artifacts):
        raise ValueError("artifacts must be a dict[str, bytes]")
    if len(artifacts) > MAX_ARTIFACTS or any(len(n) > 256 for n in artifacts):
        return _unavailable("artifact count/name exceeds observer limits")
    if any(not isinstance(data, bytes) for data in artifacts.values()):
        return _unavailable("artifact payload must be bytes")
    selected = {name: bytes(data) for name, data in artifacts.items()
                if name.lower().endswith(_EXTENSIONS[stage]) and isinstance(data, bytes)}
    findings = []
    if not selected:
        findings.append({"kind": "expected-artifact-missing", "artifact": stage,
                         "detail": f"no expected {stage.upper()} artifact supplied"})
    else:
        if sum(map(len, selected.values())) > MAX_TOTAL_ARTIFACT_BYTES:
            return _unavailable("artifact set exceeds 4 MB independent-check limit")
        for name, data in selected.items():
            if len(data) > MAX_ARTIFACT_BYTES:
                return _unavailable("artifact exceeds 2 MB independent-check limit")
            findings += _check_dxf(name, data) if stage == "dxf" else _check_step(name, data)
    if findings:
        return {"status": "confirmed-error", "findings": findings[:MAX_FINDINGS],
                "alerts": ["Do not release artifact; independent structural check failed."],
                "detail": "independent structural verification failed"}
    try:
        doc_size = len(json.dumps(document, ensure_ascii=True, allow_nan=False).encode("utf-8"))
        if doc_size > MAX_DOCUMENT_BYTES:
            raise ValueError("document exceeds 256 KB context limit")
        from .context import build_context
        summaries = {n: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                         "preview_hex": data[:64].hex()} for n, data in selected.items()}
        context = build_context(document, stage=stage, artifact_summaries=summaries)
    except Exception as exc:
        return _unavailable(f"observation context unavailable: {type(exc).__name__}")
    try:
        checker = ((getattr(adapter, "check_conversion", None) or getattr(adapter, "check_coherence", None))
                   if adapter is not None else None)
    except Exception:
        checker = None
    if not callable(checker):
        return _unavailable("observer adapter unavailable")
    try:
        advisories = _json_findings(checker(context))
    except Exception as exc:
        return _unavailable(f"observer provider failed: {type(exc).__name__}")
    if advisories:
        return {"status": "observed", "findings": advisories, "alerts": [],
                "detail": "advisory findings require human review"}
    return {"status": "clear", "findings": [], "alerts": [], "detail": "no issues detected"}
