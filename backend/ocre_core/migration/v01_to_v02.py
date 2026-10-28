"""Pure local EIR 0.1 -> 0.2 migration; never writes or changes source bytes.

The converter copies only fields whose meaning is represented directly in both
versions. References and relationships absent from v0.1 remain null/empty and
are listed by JSON Pointer until a caller supplies an explicit resolution.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from ocre_core.validation.validator import StrictJSONError, parse_json_strict, validate


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = ROOT / "schemas" / "eir"
TOOL_ID = "ocre.eir-migrator"
TOOL_VERSION = "0.1.0"
REPORT_VERSION = "0.1.0"
_SHA = re.compile(r"^[a-f0-9]{64}$")


class MigrationInputError(ValueError):
    """Source bytes, resolution records, or pointer values are invalid."""


@dataclass(frozen=True)
class MigrationResult:
    """In-memory package draft. No filesystem or persistence writes occur."""

    source_bytes: bytes
    source_sha256: str
    draft_bytes: bytes
    draft_sha256: str
    report: Mapping[str, Any]

    @property
    def status(self) -> str:
        return str(self.report["status"])


def _schema(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    schema = parse_json_strict(raw)
    Draft202012Validator.check_schema(schema)
    return schema, hashlib.sha256(raw).hexdigest()


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _decode_pointer(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise MigrationInputError("resolution JSON Pointer must identify a draft field")
    parts = pointer[1:].split("/")
    decoded: list[str] = []
    for part in parts:
        if re.search(r"~(?![01])", part):
            raise MigrationInputError("resolution JSON Pointer has an invalid escape")
        decoded.append(part.replace("~1", "/").replace("~0", "~"))
    return decoded


def _set_pointer(document: Any, pointer: str, value: Any) -> None:
    parts = _decode_pointer(pointer)
    parent = document
    for part in parts[:-1]:
        if isinstance(parent, list):
            if not part.isdecimal() or int(part) >= len(parent):
                raise MigrationInputError(f"resolution pointer does not exist: {pointer}")
            parent = parent[int(part)]
        elif isinstance(parent, dict) and part in parent:
            parent = parent[part]
        else:
            raise MigrationInputError(f"resolution pointer does not exist: {pointer}")
    last = parts[-1]
    if isinstance(parent, list):
        if not last.isdecimal() or int(last) >= len(parent):
            raise MigrationInputError(f"resolution pointer does not exist: {pointer}")
        parent[int(last)] = deepcopy(value)
    elif isinstance(parent, dict) and last in parent:
        parent[last] = deepcopy(value)
    else:
        raise MigrationInputError(f"resolution pointer does not exist: {pointer}")


def _convert(source: dict[str, Any], report_artifact_id: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "eir_schema_version": "0.2.0",
        "document_state": "draft",
        "document_origin": "migration_v0.1",
        "project": deepcopy(source.get("project")),
        "revision": deepcopy(source.get("revision")),
        "geometry": [],
        "materials": deepcopy(source.get("materials", [])),
        "frames": [],
        "simulation_cases": [],
        "migration": {
            "source_schema_version": "0.1.0",
            "source_sha256": "",  # filled after the exact source bytes are hashed
            "tool_id": TOOL_ID,
            "tool_version": TOOL_VERSION,
            "report_artifact_id": deepcopy(report_artifact_id),
        },
    }
    if "extensions" in source:
        draft["extensions"] = deepcopy(source["extensions"])
    for geometry in source.get("geometry", []):
        item = {
            "geometry_id": deepcopy(geometry.get("geometry_id")),
            # v0.1 embeds a URI; v0.2 resolves artifact IDs only in the bundle.
            "artifact_id": None,
            "length_unit": deepcopy(geometry.get("length_unit")),
            # A frame ID without the corresponding frame definition is pending.
            "frame_id": None,
        }
        if "bounding_box" in geometry:
            item["bounding_box"] = deepcopy(geometry["bounding_box"])
        if "extensions" in geometry:
            item["extensions"] = deepcopy(geometry["extensions"])
        draft["geometry"].append(item)
    for case in source.get("simulation_cases", []):
        migrated = {
            "case_id": deepcopy(case.get("case_id")),
            "revision_id": deepcopy(case.get("revision_id")),
            "domain": deepcopy(case.get("domain")),
            "analysis": deepcopy(case.get("analysis")),
            "frame_id": None,
            "geometry_refs": [],
            "material_refs": [],
            "material_assignments": [],
            "group_manifest_ref": deepcopy(case.get("group_manifest_ref")),
            "loads": [],
            "constraints": [],
            "solver": deepcopy(case.get("solver")),
        }
        for kind in ("loads", "constraints"):
            for condition in case.get(kind, []):
                converted = deepcopy(condition)
                converted["frame_id"] = None
                migrated[kind].append(converted)
        if "extensions" in case:
            migrated["extensions"] = deepcopy(case["extensions"])
        draft["simulation_cases"].append(migrated)
    return draft


def _pending(draft: Mapping[str, Any], bundle: Any | None) -> list[dict[str, str]]:
    pending: list[dict[str, str]] = []

    def add(pointer: str, code: str, explanation: str, blocker: str) -> None:
        pending.append({"json_pointer": pointer, "code": code, "explanation": explanation, "blocked_by": blocker})

    artifact_ids: set[str] | None = None
    if bundle is not None:
        try:
            parsed_bundle = parse_json_strict(bundle)
            artifact_ids = {item.get("artifact_id") for item in parsed_bundle.get("artifacts", [])
                            if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)}
        except (StrictJSONError, AttributeError):
            artifact_ids = set()
    report_id = draft.get("migration", {}).get("report_artifact_id")
    if not isinstance(report_id, str) or artifact_ids is None or report_id not in artifact_ids:
        add("/migration/report_artifact_id", "report_artifact_id_required", "Choose the bundle artifact ID for this report.", "report reference is unresolved")
    frames = draft.get("frames", [])
    geometries = draft.get("geometry", [])
    cases = draft.get("simulation_cases", [])
    frame_ids = {item.get("frame_id") for item in frames if isinstance(item, dict) and isinstance(item.get("frame_id"), str)} if isinstance(frames, list) else set()
    for i, geometry in enumerate(geometries if isinstance(geometries, list) else []):
        if not isinstance(geometry, dict):
            continue
        aid, frame_id = geometry.get("artifact_id"), geometry.get("frame_id")
        if not isinstance(aid, str) or artifact_ids is None or aid not in artifact_ids:
            add(f"/geometry/{i}/artifact_id", "artifact_mapping_required", "Choose a bundle artifact ID; the embedded v0.1 URI is not copied.", "geometry artifact is unresolved")
        if not isinstance(frame_id, str) or frame_id not in frame_ids:
            add(f"/geometry/{i}/frame_id", "frame_definition_required", "Supply a valid frame ID and its frame definition; v0.1 has no frame tree.", "geometry frame is unresolved")
    for i, case in enumerate(cases if isinstance(cases, list) else []):
        if not isinstance(case, dict):
            continue
        for field, code, text in (
            ("frame_id", "frame_definition_required", "Supply a valid frame ID and its frame definition."),
            ("geometry_refs", "geometry_case_mapping_required", "Select the geometries used by this case."),
            ("material_refs", "material_case_mapping_required", "Select the materials used by this case."),
            ("material_assignments", "material_assignment_required", "Explicitly map each case geometry to a material."),
        ):
            value = case.get(field)
            if value is None or value == [] or (field == "frame_id" and (not isinstance(value, str) or value not in frame_ids)):
                add(f"/simulation_cases/{i}/{field}", code, text, "case relationship is unresolved")
        for kind in ("loads", "constraints"):
            conditions = case.get(kind, [])
            for j, condition in enumerate(conditions if isinstance(conditions, list) else []):
                if not isinstance(condition, dict):
                    continue
                frame_id = condition.get("frame_id")
                if not isinstance(frame_id, str) or frame_id not in frame_ids:
                    add(f"/simulation_cases/{i}/{kind}/{j}/frame_id", "frame_definition_required", "Supply the condition frame explicitly.", "condition frame is unresolved")
        group_id = case.get("group_manifest_ref")
        if not isinstance(group_id, str) or artifact_ids is None or group_id not in artifact_ids:
            add(f"/simulation_cases/{i}/group_manifest_ref", "group_manifest_required", "Supply the bundle artifact ID of the matching group manifest.", "group reference is unresolved")
    return pending


def validate_migration_draft(
    draft: Any,
    bundle: Any,
    group_manifest: Any | None = None,
    *,
    bundle_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run normal v0.2 structural and semantic authoring validation on a draft."""
    report = validate(draft, bundle, group_manifest, bundle_root=bundle_root, purpose="authoring")
    return report.as_dict()


def migrate_v01_to_v02(
    source_bytes: bytes,
    resolutions: Mapping[str, Mapping[str, Any]],
    *,
    actor: str,
    now: datetime | None = None,
    bundle: Any | None = None,
    group_manifest: Any | None = None,
    bundle_root: str | Path | None = None,
) -> MigrationResult:
    """Build a separate v0.2 draft and versioned report from original v0.1 bytes.

    ``resolutions`` is keyed by destination JSON Pointer. Each value must include
    ``value``, ``reason`` and ``actor``. Resolution metadata is recorded in the
    report; the caller's source bytes are retained byte-for-byte in the result.
    """
    if not isinstance(source_bytes, bytes):
        raise MigrationInputError("source must be original bytes")
    if not isinstance(actor, str) or not actor.strip():
        raise MigrationInputError("actor is required")
    if isinstance(resolutions, Mapping):
        if any(not isinstance(item, Mapping) for item in resolutions.values()):
            raise MigrationInputError("each resolution must be an object")
        resolution_entries = [{"json_pointer": pointer, **dict(item)} for pointer, item in resolutions.items()]
    elif isinstance(resolutions, (list, tuple)):
        resolution_entries = list(resolutions)
    else:
        raise MigrationInputError("resolutions must be a JSON Pointer mapping or ordered list")
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    source_schema, source_schema_sha = _schema(SCHEMA_DIR / "0.1.0.schema.json")
    destination_schema, destination_schema_sha = _schema(SCHEMA_DIR / "0.2.0.schema.json")
    try:
        source = parse_json_strict(source_bytes)
    except StrictJSONError as exc:
        raise MigrationInputError(f"source JSON is invalid: {exc}") from exc
    if not isinstance(source, dict):
        raise MigrationInputError("source EIR must be a JSON object")
    source_errors = list(Draft202012Validator(source_schema).iter_errors(source))
    if source_errors:
        raise MigrationInputError("source EIR does not conform to the frozen v0.1 schema")

    report_id = None
    for entry in resolution_entries:
        if isinstance(entry, Mapping) and entry.get("json_pointer") == "/migration/report_artifact_id":
            report_id = entry.get("value")
    draft = _convert(source, report_id)
    draft["migration"]["source_sha256"] = source_sha

    resolution_records: list[dict[str, Any]] = []
    timestamp = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    for resolution in resolution_entries:
        if not isinstance(resolution, Mapping):
            raise MigrationInputError("each resolution must be an object")
        pointer = resolution.get("json_pointer")
        if not isinstance(pointer, str):
            raise MigrationInputError("resolution json_pointer must be a string")
        if pointer in {"/eir_schema_version", "/document_state", "/document_origin", "/migration"} or (
            pointer.startswith("/migration/") and pointer != "/migration/report_artifact_id"
        ):
            raise MigrationInputError(f"resolution cannot alter migration provenance or draft state: {pointer}")
        if not isinstance(resolution, Mapping) or not {"value", "reason", "actor"} <= set(resolution):
            raise MigrationInputError(f"resolution {pointer!r} requires value, reason and actor")
        if not isinstance(resolution["reason"], str) or not resolution["reason"].strip():
            raise MigrationInputError(f"resolution {pointer!r} requires a non-empty reason")
        if not isinstance(resolution["actor"], str) or not resolution["actor"].strip():
            raise MigrationInputError(f"resolution {pointer!r} requires a non-empty actor")
        try:
            json.dumps(resolution["value"], ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise MigrationInputError(f"resolution {pointer!r} value is not finite JSON data") from exc
        _set_pointer(draft, pointer, resolution["value"])
        resolution_records.append({
            "json_pointer": pointer,
            "value": deepcopy(resolution["value"]),
            "reason": resolution["reason"],
            "actor": resolution["actor"],
            "timestamp": timestamp,
        })
    resolution_records.sort(key=lambda entry: entry["json_pointer"])
    pending = _pending(draft, bundle)
    structural_errors = list(Draft202012Validator(destination_schema).iter_errors(draft))
    structural_diagnostics = [{"json_pointer": "/" + "/".join(_escape(str(part)) for part in error.absolute_path),
                               "code": "destination_schema_validation", "explanation": error.message}
                              for error in structural_errors]
    validation_report = None
    if bundle is not None:
        validation_report = validate_migration_draft(draft, bundle, group_manifest, bundle_root=bundle_root)
    draft_bytes = json.dumps(draft, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    draft_sha = hashlib.sha256(draft_bytes).hexdigest()
    incomplete = bool(pending) or bool(structural_errors) or validation_report is None or validation_report["status"] != "valid"
    report = {
        "report_version": REPORT_VERSION,
        "status": "incomplete" if incomplete else "complete",
        "source": {"id": source.get("revision", {}).get("revision_id"), "eir_schema_version": "0.1.0", "sha256": source_sha},
        "output": {"id": source.get("revision", {}).get("revision_id"), "eir_schema_version": "0.2.0", "sha256": draft_sha, "document_state": "draft"},
        "schemas": {
            "source": {"document_kind": "eir", "schema_version": "0.1.0", "schema_sha256": source_schema_sha},
            "destination": {"document_kind": "eir", "schema_version": "0.2.0", "schema_sha256": destination_schema_sha},
        },
        "tool": {"id": TOOL_ID, "version": TOOL_VERSION, "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "resolutions": resolution_records,
        "pending": pending,
        "warnings": _migration_warnings(source),
        "diagnostics": {
            "source_schema_validation": "valid",
            "destination_schema_validation": "invalid" if structural_errors else "valid",
            "destination_schema_errors": structural_diagnostics,
            "v02_validation": validation_report,
            "ready": False,
        },
    }
    return MigrationResult(source_bytes, source_sha, draft_bytes, draft_sha, report)


def _migration_warnings(source: Mapping[str, Any]) -> list[dict[str, str]]:
    """Call out legacy fields that are intentionally kept only in source bytes."""
    warnings: list[dict[str, str]] = []
    for i, geometry in enumerate(source.get("geometry", [])):
        if isinstance(geometry, dict) and isinstance(geometry.get("artifact"), dict):
            warnings.append({
                "json_pointer": f"/geometry/{i}/artifact",
                "code": "legacy_embedded_artifact_metadata_preserved_in_source",
                "explanation": "The embedded URI/format/hash remain only in immutable v0.1 source; v0.2 resolves artifact IDs in the bundle.",
            })
    for i, case in enumerate(source.get("simulation_cases", [])):
        if isinstance(case, dict) and "groups" in case:
            warnings.append({
                "json_pointer": f"/simulation_cases/{i}/groups",
                "code": "legacy_case_group_list_preserved_in_source",
                "explanation": "The legacy list is not copied as a v0.2 relation; group membership must be resolved through the selected group manifest.",
            })
    return warnings
