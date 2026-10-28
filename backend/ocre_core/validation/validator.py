"""Structural and semantic validation for the frozen EIR 0.2 contract.

The validator never rewrites documents. Schema documents are loaded only from
this repository's schema directory, and external ``$ref`` values are rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = ROOT / "schemas" / "eir"
PROFILE_DIR = SCHEMA_DIR / "profiles"
SCHEMAS = {
    "eir": ("0.2.0", "0.2.0.schema.json"),
    "bundle": ("0.2.0", "input-bundle-0.2.0.schema.json"),
    "groups": ("0.2.0", "mesh-group-manifest-0.2.0.schema.json"),
}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class StrictJSONError(ValueError):
    """JSON was invalid or used an ambiguous/non-finite representation."""


class ArtifactTooLarge(ValueError):
    """Artifact exceeds its configured byte limit."""


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
        except (json.JSONDecodeError, StrictJSONError, RecursionError) as exc:
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
    if isinstance(node, dict):
        for key, child in node.items():
            if not isinstance(key, str):
                raise StrictJSONError("JSON object keys must be strings")
            _walk_finite(child)
    elif isinstance(node, (list, tuple)):
        for child in node:
            _walk_finite(child)


def _schema(kind: str) -> tuple[dict[str, Any], str, str]:
    try:
        version, filename = SCHEMAS[kind]
        path = SCHEMA_DIR / filename
        schema = json.loads(path.read_text(encoding="utf-8"))
        _assert_local_refs(schema)
        Draft202012Validator.check_schema(schema)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lock_path = SCHEMA_DIR / "schema-lock-0.2.0.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        locked = next((entry.get("sha256") for entry in lock.get("schemas", [])
                       if entry.get("path") == filename), None)
        if lock.get("lock_version") != "0.2.0" or locked != digest:
            raise ValueError("bundled schema digest does not match schema-lock-0.2.0.json")
        return schema, version, digest
    except RecursionError as exc:
        raise ValueError("bundled schema exceeds the supported nesting depth") from exc


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


@dataclass(frozen=True)
class ValidationReport:
    status: str
    purpose: str
    execution_readiness: str | None
    diagnostics: tuple[Diagnostic, ...]
    schema_digests: tuple[tuple[str, str, str], ...]
    profile_id: str = "structural.axial_bar.v1"
    profile_version: str = "1"

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_version": "0.2.0",
            "status": self.status,
            "purpose": self.purpose,
            "execution_readiness": self.execution_readiness,
            "profile": {"id": self.profile_id, "version": self.profile_version},
            "schemas": [{"document_kind": k, "schema_version": v, "schema_sha256": digest}
                        for k, v, digest in self.schema_digests],
            "diagnostics": [d.as_dict() for d in self.diagnostics],
        }


def _add(items: list[Diagnostic], stage: str, pointer: str, code: str, message: str,
         severity: str = "error") -> None:
    items.append(Diagnostic(stage, pointer, code, message, severity))


def _unique_index(items: Any, key: str, namespace: str, diagnostics: list[Diagnostic], pointer: str) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        return index
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get(key), str):
            continue
        value = item[key]
        if value in index:
            _add(diagnostics, "semantic", f"{pointer}/{i}/{key}", "duplicate_id", f"duplicate {namespace} id: {value}")
        else:
            index[value] = item
    return index


def _uri_path(uri: Any) -> PurePosixPath:
    if not isinstance(uri, str) or not uri or "\\" in uri or "\x00" in uri:
        raise ValueError("URI must be a non-empty relative POSIX path")
    if "%" in uri:
        raise ValueError("URI must not contain percent-encoded path characters")
    parts = urlsplit(uri)
    if parts.scheme or parts.netloc or parts.query or parts.fragment or uri.startswith("/"):
        raise ValueError("URI must not contain scheme, authority, query, fragment, or absolute path")
    decoded = unquote(uri)
    segments = decoded.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError("URI must use normalized path segments without dot traversal")
    return PurePosixPath(*segments)


def _resolve_file(root: Path, uri: Any) -> Path:
    rel = _uri_path(uri)
    canonical_root = root.resolve(strict=True)
    candidate = canonical_root.joinpath(*rel.parts)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(canonical_root):
        raise PermissionError("artifact path escapes bundle root")
    if not resolved.is_file():
        raise ValueError("artifact target is not a regular file")
    return resolved


def _read_confined(root: Path, uri: Any, max_bytes: int) -> bytes:
    """Read at most max_bytes+1 from one opened descriptor, then enforce size."""
    path = _resolve_file(root, uri)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("artifact target is not a regular file")
        if metadata.st_size > max_bytes:
            raise ArtifactTooLarge("artifact exceeds its configured byte limit")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ArtifactTooLarge("artifact exceeds its configured byte limit")
        return data
    finally:
        os.close(fd)


def _semantic(eir: dict[str, Any], bundle: dict[str, Any], groups: dict[str, Any] | None,
              root: Path | None, purpose: str, capabilities: Mapping[str, Any] | None,
              diagnostics: list[Diagnostic], max_artifact_bytes: int, max_document_bytes: int) -> None:
    project, revision = eir.get("project", {}), eir.get("revision", {})
    rev_id = revision.get("revision_id")
    if project.get("project_id") != revision.get("project_id"):
        _add(diagnostics, "semantic", "/revision/project_id", "project_revision_mismatch", "project and revision project_id must match")

    geometries = _unique_index(eir.get("geometry"), "geometry_id", "geometry", diagnostics, "/geometry")
    materials = _unique_index(eir.get("materials"), "material_id", "material", diagnostics, "/materials")
    frames = _unique_index(eir.get("frames"), "frame_id", "frame", diagnostics, "/frames")
    cases = _unique_index(eir.get("simulation_cases"), "case_id", "case", diagnostics, "/simulation_cases")
    artifacts = _unique_index(bundle.get("artifacts"), "artifact_id", "artifact", diagnostics, "/artifacts")
    if bundle.get("eir_artifact_id") not in artifacts:
        _add(diagnostics, "semantic", "/eir_artifact_id", "unresolved_artifact", "bundle EIR artifact id does not resolve")

    frame_items = eir.get("frames", [])
    roots = []
    for i, frame in enumerate(frame_items if isinstance(frame_items, list) else []):
        if not isinstance(frame, dict):
            continue
        fid, parent = frame.get("frame_id"), frame.get("parent_frame_id")
        if parent is None:
            roots.append((i, frame))
            if frame.get("translation", {}).get("value") != [0, 0, 0] or frame.get("rotation_xyzw") != [0, 0, 0, 1]:
                _add(diagnostics, "semantic", f"/frames/{i}", "invalid_root_transform", "root frame must have zero translation and identity quaternion")
        elif parent not in frames:
            _add(diagnostics, "semantic", f"/frames/{i}/parent_frame_id", "unresolved_frame", "parent frame does not exist")
        else:
            seen: set[str] = set()
            cursor = fid
            while cursor in frames:
                if cursor in seen:
                    _add(diagnostics, "semantic", f"/frames/{i}/parent_frame_id", "frame_cycle", "frame graph contains a cycle")
                    break
                seen.add(cursor)
                cursor = frames[cursor].get("parent_frame_id")
                if cursor is None:
                    break
    if frame_items and len(roots) != 1:
        _add(diagnostics, "semantic", "/frames", "frame_root_count", "frame graph must have exactly one root")
    for i, frame in enumerate(frame_items if isinstance(frame_items, list) else []):
        if isinstance(frame, dict):
            translation = frame.get("translation")
            if isinstance(translation, dict):
                _check_unit(translation.get("unit"), "length", f"/frames/{i}/translation/unit", diagnostics,
                            json.loads((PROFILE_DIR / "ucum.structural_axial.v1.json").read_text(encoding="utf-8")))

    for i, frame in enumerate(frame_items if isinstance(frame_items, list) else []):
        if not isinstance(frame, dict): continue
        q = frame.get("rotation_xyzw")
        if isinstance(q, list) and len(q) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in q):
            x, y, z, w = (float(v) for v in q)
            n2 = (((x*x + y*y) + z*z) + w*w)
            norm = math.sqrt(n2) if math.isfinite(n2) else math.inf
            if not math.isfinite(norm) or abs(norm - 1.0) > 1e-6:
                _add(diagnostics, "semantic", f"/frames/{i}/rotation_xyzw", "invalid_quaternion_norm", "quaternion norm must be within 1e-6 of one")

    profile = json.loads((PROFILE_DIR / "structural.axial_bar.v1.json").read_text(encoding="utf-8"))
    unit_profile = json.loads((PROFILE_DIR / "ucum.structural_axial.v1.json").read_text(encoding="utf-8"))
    for i, geometry in enumerate(eir.get("geometry", []) if isinstance(eir.get("geometry"), list) else []):
        if not isinstance(geometry, dict): continue
        if geometry.get("frame_id") is not None and geometry.get("frame_id") not in frames:
            _add(diagnostics, "semantic", f"/geometry/{i}/frame_id", "unresolved_frame", "geometry frame does not exist")
        if geometry.get("artifact_id") is not None and geometry.get("artifact_id") not in artifacts:
            _add(diagnostics, "semantic", f"/geometry/{i}/artifact_id", "unresolved_artifact", "geometry artifact does not exist")
        _check_unit(geometry.get("length_unit"), "length", f"/geometry/{i}/length_unit", diagnostics, unit_profile)
        box = geometry.get("bounding_box")
        if isinstance(box, dict): _check_unit(box.get("unit"), "length", f"/geometry/{i}/bounding_box/unit", diagnostics, unit_profile)

    for i, material in enumerate(eir.get("materials", []) if isinstance(eir.get("materials"), list) else []):
        if not isinstance(material, dict): continue
        for name, quantity in material.get("properties", {}).items():
            dim = "stress" if name == "young_modulus" else "dimensionless" if name == "poisson_ratio" else None
            if dim and isinstance(quantity, dict): _check_unit(quantity.get("unit"), dim, f"/materials/{i}/properties/{name}/unit", diagnostics, unit_profile)
            elif dim is None:
                _add(diagnostics, "semantic", f"/materials/{i}/properties/{name}", "unsupported_material_property", "material property has no dimension rule in the selected profile")

    group_ref_ids: set[str] = set()
    manifest_refs: set[str] = set()
    for i, case in enumerate(eir.get("simulation_cases", []) if isinstance(eir.get("simulation_cases"), list) else []):
        if not isinstance(case, dict): continue
        ptr = f"/simulation_cases/{i}"
        if case.get("revision_id") != rev_id:
            _add(diagnostics, "semantic", ptr + "/revision_id", "revision_mismatch", "case must belong to selected revision")
        if case.get("domain") != profile["domain"] or case.get("analysis") != profile["analysis"]:
            _add(diagnostics, "semantic", ptr, "unsupported_profile_case", "case domain/analysis is not supported by structural.axial_bar.v1")
        if case.get("frame_id") is not None and case.get("frame_id") not in frames:
            _add(diagnostics, "semantic", ptr + "/frame_id", "unresolved_frame", "case frame does not exist")
        geometry_refs = case.get("geometry_refs", [])
        material_refs = case.get("material_refs", [])
        assignments = case.get("material_assignments", [])
        for j, gid in enumerate(geometry_refs if isinstance(geometry_refs, list) else []):
            if gid not in geometries: _add(diagnostics, "semantic", f"{ptr}/geometry_refs/{j}", "unresolved_geometry", "geometry reference does not exist in selected revision")
        for j, mid in enumerate(material_refs if isinstance(material_refs, list) else []):
            if mid not in materials: _add(diagnostics, "semantic", f"{ptr}/material_refs/{j}", "unresolved_material", "material reference does not exist in selected revision")
        assigned_g = [a.get("geometry_id") for a in assignments if isinstance(a, dict)] if isinstance(assignments, list) else []
        assigned_m = [a.get("material_id") for a in assignments if isinstance(a, dict)] if isinstance(assignments, list) else []
        if len(assigned_g) != len(set(assigned_g)) or set(assigned_g) != set(geometry_refs):
            _add(diagnostics, "semantic", ptr + "/material_assignments", "material_coverage_mismatch", "assignments must cover every geometry_ref exactly once")
        if set(material_refs) != set(assigned_m) or len(material_refs) != len(set(material_refs)):
            _add(diagnostics, "semantic", ptr + "/material_refs", "material_refs_mismatch", "material_refs must equal the distinct assigned material ids")
        for j, assignment in enumerate(assignments if isinstance(assignments, list) else []):
            if not isinstance(assignment, dict): continue
            if assignment.get("geometry_id") not in geometries: _add(diagnostics, "semantic", f"{ptr}/material_assignments/{j}/geometry_id", "unresolved_geometry", "assignment geometry does not exist")
            if assignment.get("material_id") not in materials: _add(diagnostics, "semantic", f"{ptr}/material_assignments/{j}/material_id", "unresolved_material", "assignment material does not exist")
        group_id = case.get("group_manifest_ref")
        if group_id:
            group_ref_ids.add(group_id)
            manifest_refs.add(group_id)
            if group_id not in artifacts: _add(diagnostics, "semantic", ptr + "/group_manifest_ref", "unresolved_artifact", "group manifest artifact id does not exist")
        elif eir.get("document_state") == "complete":
            _add(diagnostics, "semantic", ptr + "/group_manifest_ref", "missing_group_manifest", "complete case requires a group manifest")
        for kind, allowed in (("loads", set(profile["allowed_load_kinds"])), ("constraints", set(profile["allowed_constraint_kinds"]))):
            for j, condition in enumerate(case.get(kind, []) if isinstance(case.get(kind), list) else []):
                if not isinstance(condition, dict): continue
                cp = f"{ptr}/{kind}/{j}"
                if condition.get("kind") not in allowed: _add(diagnostics, "semantic", cp + "/kind", "unsupported_condition_kind", "condition kind is not supported by the selected profile")
                if condition.get("frame_id") is not None and condition.get("frame_id") not in frames: _add(diagnostics, "semantic", cp + "/frame_id", "unresolved_frame", "condition frame does not exist")
                if condition.get("group"):
                    group_ref_ids.add(condition["group"])
                dimension = "force" if kind == "loads" else "length"
                for component, quantity in condition.get("components", {}).items():
                    if isinstance(quantity, dict): _check_unit(quantity.get("unit"), dimension, cp + f"/components/{component}/unit", diagnostics, unit_profile)

    for i, artifact in enumerate(bundle.get("artifacts", []) if isinstance(bundle.get("artifacts"), list) else []):
        if not isinstance(artifact, dict): continue
        try:
            _uri_path(artifact.get("uri"))
        except ValueError as exc:
            _add(diagnostics, "semantic", f"/artifacts/{i}/uri", "unsafe_uri", str(exc))
    if groups is not None:
        _validate_groups(eir, groups, artifacts, diagnostics, manifest_refs)
    elif group_ref_ids:
        _add(diagnostics, "semantic", "/simulation_cases", "group_manifest_unavailable", "group manifest document was not supplied or safely loaded", "warning" if purpose == "authoring" else "error")

    consumed = {bundle.get("eir_artifact_id")}
    consumed.update(case.get("group_manifest_ref") for case in eir.get("simulation_cases", [])
                    if isinstance(case, dict) and case.get("group_manifest_ref"))
    consumed.update(g.get("artifact_id") for g in eir.get("geometry", []) if isinstance(g, dict) and g.get("artifact_id"))
    if groups is not None:
        consumed.update((groups.get("source_artifact_id"), groups.get("mesh_artifact_id")))
    migration = eir.get("migration")
    if isinstance(migration, dict) and migration.get("report_artifact_id"):
        consumed.add(migration["report_artifact_id"])
    for artifact in bundle.get("artifacts", []):
        if not isinstance(artifact, dict): continue
        aid, usage = artifact.get("artifact_id"), artifact.get("usage")
        if purpose == "execution_preflight" and usage == "optional" and aid in consumed:
            _add(diagnostics, "semantic", f"/artifacts/{aid}/usage", "optional_artifact_consumed", "execution cannot consume an optional artifact")
        should_verify = purpose == "execution_preflight" and (usage == "required" or aid in consumed)
        if should_verify:
            if artifact.get("sha256") is None:
                _add(diagnostics, "semantic", f"/artifacts/{aid}/sha256", "missing_artifact_hash", "required or consumed artifact must declare sha256")
            if root is None:
                _add(diagnostics, "semantic", f"/artifacts/{aid}/uri", "bundle_root_unavailable", "execution pre-flight requires a bundle root for byte verification")
            else:
                try:
                    try:
                        artifact_bytes = _read_confined(root, artifact.get("uri"), max_artifact_bytes)
                    except ArtifactTooLarge:
                        _add(diagnostics, "semantic", f"/artifacts/{aid}/uri", "artifact_too_large", "artifact exceeds the configured byte limit")
                        continue
                    actual = hashlib.sha256(artifact_bytes).hexdigest()
                    if artifact.get("sha256") != actual:
                        _add(diagnostics, "semantic", f"/artifacts/{aid}/sha256", "artifact_hash_mismatch", "artifact bytes do not match declared sha256")
                    if aid == bundle.get("eir_artifact_id"):
                        if len(artifact_bytes) > max_document_bytes:
                            _add(diagnostics, "semantic", f"/artifacts/{aid}", "document_too_large", "EIR artifact exceeds the configured document byte limit")
                        elif parse_json_strict(artifact_bytes) != eir:
                            _add(diagnostics, "semantic", f"/artifacts/{aid}", "eir_artifact_content_mismatch", "bundle EIR artifact does not match the validated EIR document")
                    if groups is not None and aid in {case.get("group_manifest_ref") for case in eir.get("simulation_cases", []) if isinstance(case, dict)}:
                        if len(artifact_bytes) > max_document_bytes:
                            _add(diagnostics, "semantic", f"/artifacts/{aid}", "document_too_large", "group manifest artifact exceeds the configured document byte limit")
                        elif parse_json_strict(artifact_bytes) != groups:
                            _add(diagnostics, "semantic", f"/artifacts/{aid}", "group_manifest_content_mismatch", "hashed group artifact does not match the validated group manifest")
                except FileNotFoundError:
                    _add(diagnostics, "semantic", f"/artifacts/{aid}/uri", "artifact_missing", "artifact file does not exist")
                except PermissionError:
                    _add(diagnostics, "semantic", f"/artifacts/{aid}/uri", "artifact_unreadable", "artifact is outside the readable bundle scope")
                except (OSError, ValueError):
                    _add(diagnostics, "semantic", f"/artifacts/{aid}/uri", "artifact_unreadable", "artifact path could not be safely resolved or read")
    if eir.get("document_origin") == "migration_v0.1":
        migration = eir.get("migration", {})
        aid = migration.get("report_artifact_id") if isinstance(migration, dict) else None
        if aid not in artifacts: _add(diagnostics, "semantic", "/migration/report_artifact_id", "migration_report_unresolved", "migration report artifact does not resolve in bundle")
    if purpose == "execution_preflight":
        if capabilities is None:
            _add(diagnostics, "semantic", "/simulation_cases", "adapter_capabilities_unavailable", "execution readiness cannot be established without declared adapter capabilities")
        else:
            for i, case in enumerate(eir.get("simulation_cases", [])):
                if not isinstance(case, dict): continue
                adapter = case.get("solver", {}).get("adapter_id")
                supported = capabilities.get(adapter)
                if not isinstance(supported, Mapping) or supported.get("supported") is not True:
                    _add(diagnostics, "semantic", f"/simulation_cases/{i}/solver/adapter_id", "adapter_capability_mismatch", "adapter capabilities do not explicitly support this case")
                    continue
                requirements = {"domains": case.get("domain"), "analyses": case.get("analysis")}
                target_version = case.get("solver", {}).get("proposed_target_version")
                if target_version:
                    requirements["target_versions"] = target_version
                if groups is not None:
                    requirements["element_types"] = groups.get("mesh", {}).get("element_type")
                    requirements["element_orders"] = groups.get("mesh", {}).get("element_order")
                for capability_name, required_value in requirements.items():
                    if required_value not in supported.get(capability_name, []):
                        _add(diagnostics, "semantic", f"/simulation_cases/{i}/solver/adapter_id", "adapter_capability_mismatch", f"adapter does not declare support for {capability_name}")
        _add(diagnostics, "semantic", "/", "approval_fingerprint_unavailable", "OCRE-AF/1 approval fingerprint inputs are not part of the supplied validation context")
    if eir.get("document_state") != "complete":
        _add(diagnostics, "semantic", "/document_state", "document_incomplete", "draft EIR cannot pass validation as complete")
    if purpose == "authoring":
        for artifact in bundle.get("artifacts", []):
            if isinstance(artifact, dict) and artifact.get("usage") == "required" and artifact.get("sha256") is None:
                _add(diagnostics, "semantic", f"/artifacts/{artifact.get('artifact_id')}/sha256", "missing_artifact_hash", "required artifact hash is pending", "warning")


def _check_unit(unit: Any, dimension: str, pointer: str, diagnostics: list[Diagnostic], profile: dict[str, Any]) -> None:
    entry = profile.get("quantities", {}).get(dimension, {})
    if unit not in entry.get("accepted_ucum", []):
        _add(diagnostics, "semantic", pointer, "unsupported_unit", f"unit is not in the {dimension} allowlist")


def _null_complete_refs(eir: Any, diagnostics: list[Diagnostic]) -> None:
    """Report semantically required references even when schema validation also fails."""
    if not isinstance(eir, dict) or eir.get("document_state") != "complete":
        return
    for i, geometry in enumerate(eir.get("geometry", []) if isinstance(eir.get("geometry"), list) else []):
        if not isinstance(geometry, dict): continue
        for field in ("artifact_id", "frame_id"):
            if geometry.get(field) is None:
                _add(diagnostics, "semantic", f"/geometry/{i}/{field}", "null_complete_reference", "complete geometry requires this reference")
    for i, case in enumerate(eir.get("simulation_cases", []) if isinstance(eir.get("simulation_cases"), list) else []):
        if not isinstance(case, dict): continue
        if case.get("frame_id") is None:
            _add(diagnostics, "semantic", f"/simulation_cases/{i}/frame_id", "null_complete_reference", "complete case requires a frame reference")
        if case.get("group_manifest_ref") is None:
            _add(diagnostics, "semantic", f"/simulation_cases/{i}/group_manifest_ref", "null_complete_reference", "complete case requires a group manifest reference")
        for field in ("loads", "constraints"):
            for j, condition in enumerate(case.get(field, []) if isinstance(case.get(field), list) else []):
                if isinstance(condition, dict) and condition.get("frame_id") is None:
                    _add(diagnostics, "semantic", f"/simulation_cases/{i}/{field}/{j}/frame_id", "null_complete_reference", "complete condition requires a frame reference")
        for j, assignment in enumerate(case.get("material_assignments", []) if isinstance(case.get("material_assignments"), list) else []):
            if not isinstance(assignment, dict): continue
            for field in ("geometry_id", "material_id"):
                if assignment.get(field) is None:
                    _add(diagnostics, "semantic", f"/simulation_cases/{i}/material_assignments/{j}/{field}", "null_complete_reference", "complete material assignment requires this reference")


def _validate_groups(eir: dict[str, Any], groups: dict[str, Any], artifacts: dict[str, dict[str, Any]], diagnostics: list[Diagnostic], manifest_refs: set[str]) -> None:
    matching = [c for c in eir.get("simulation_cases", []) if isinstance(c, dict) and c.get("group_manifest_ref") in manifest_refs]
    if len(matching) != 1:
        _add(diagnostics, "semantic", "/manifest_id", "group_manifest_reference_mismatch", "one supplied group manifest must correspond to exactly one EIR case")
        return
    case = matching[0]
    manifest_ref = case.get("group_manifest_ref")
    artifact = artifacts[manifest_ref]
    if (artifact.get("media_type"), artifact.get("format"), artifact.get("format_version")) != ("application/json", "ocre.mesh-group-manifest", "0.2.0"):
        _add(diagnostics, "semantic", f"/artifacts/{manifest_ref}", "group_manifest_format_mismatch", "group manifest artifact must declare application/json, ocre.mesh-group-manifest, version 0.2.0")
    geometry_ids = case.get("geometry_refs", [])
    expected = {"revision_id": eir.get("revision", {}).get("revision_id"), "case_id": case.get("case_id")}
    for field, value in expected.items():
        if groups.get(field) != value: _add(diagnostics, "semantic", f"/{field}", "group_identity_mismatch", f"group manifest {field} does not match EIR")
    if groups.get("geometry_id") not in geometry_ids: _add(diagnostics, "semantic", "/geometry_id", "group_geometry_mismatch", "group manifest geometry is not used by the case")
    geometry = next((g for g in eir.get("geometry", []) if isinstance(g, dict) and g.get("geometry_id") == groups.get("geometry_id")), None)
    if geometry and geometry.get("artifact_id") != groups.get("source_artifact_id"):
        _add(diagnostics, "semantic", "/source_artifact_id", "group_source_mismatch", "group source artifact must match the selected geometry artifact")
    for field in ("source_artifact_id", "mesh_artifact_id"):
        if groups.get(field) not in artifacts: _add(diagnostics, "semantic", f"/{field}", "unresolved_artifact", "group manifest artifact reference does not resolve")
    group_ids = _unique_index(groups.get("groups"), "semantic_id", "semantic group", diagnostics, "/groups")
    for kind in ("loads", "constraints"):
        for i, condition in enumerate(case.get(kind, [])):
            if not isinstance(condition, dict): continue
            for reference_field in ("group", "selection_ref"):
                reference = condition.get(reference_field)
                if reference and reference not in group_ids:
                    _add(diagnostics, "semantic", f"/simulation_cases/{case['case_id']}/{kind}/{i}/{reference_field}", "unresolved_group", "condition group/selection does not exist in group manifest")
    for i, group in enumerate(groups.get("groups", [])):
        if isinstance(group, dict):
            counts = group.get("counts", {})
            if counts.get("expected") != counts.get("observed"):
                _add(diagnostics, "semantic", f"/groups/{i}/counts", "group_count_mismatch", "observed group count must equal expected count")


def validate(eir: Any, bundle: Any, group_manifest: Any | None = None, *,
             bundle_root: str | Path | None = None, purpose: str = "authoring",
             adapter_capabilities: Mapping[str, Any] | None = None,
             max_document_bytes: int = 16 * 1024 * 1024,
             max_artifact_bytes: int = 1024 * 1024 * 1024) -> ValidationReport:
    """Validate EIR and bundle; optionally verify group manifest/files for pre-flight.

    ``adapter_capabilities`` maps adapter ids to declarations including
    ``{"supported": true}``; absence always blocks execution readiness.
    """
    if purpose not in ("authoring", "execution_preflight"):
        raise ValueError("purpose must be 'authoring' or 'execution_preflight'")
    diagnostics: list[Diagnostic] = []
    parsed: dict[str, Any] = {}
    if max_document_bytes < 1 or max_artifact_bytes < 1:
        raise ValueError("document and artifact byte limits must be positive")
    for kind, raw in (("eir", eir), ("bundle", bundle), ("groups", group_manifest)):
        if raw is None and kind == "groups": continue
        try:
            if isinstance(raw, bytes): size = len(raw)
            elif isinstance(raw, bytearray): size = len(raw)
            elif isinstance(raw, str): size = len(raw.encode("utf-8"))
            else: size = len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if size > max_document_bytes:
                _add(diagnostics, "parsing", f"/{kind}", "document_too_large", "document exceeds the configured byte limit")
                continue
        except (TypeError, ValueError):
            pass
        try:
            parsed[kind] = parse_json_strict(raw)
        except StrictJSONError as exc:
            _add(diagnostics, "parsing", f"/{kind}", "invalid_json", str(exc))
    digests: list[tuple[str, str, str]] = []
    schema_valid = True
    for kind in ("eir", "bundle", "groups"):
        if kind not in parsed: continue
        try:
            schema, version, digest = _schema(kind)
            digests.append((kind, version, digest))
            errors = sorted(Draft202012Validator(schema, format_checker=_format_checker()).iter_errors(parsed[kind]),
                            key=lambda e: (_pointer(e.absolute_path), e.validator or "", str(e.message)))
            for error in errors:
                schema_valid = False
                _add(diagnostics, "structural", _pointer(error.absolute_path), "schema_validation", error.message)
        except (OSError, ValueError, RecursionError) as exc:
            schema_valid = False
            _add(diagnostics, "structural", f"/{kind}", "schema_unavailable", str(exc))
    if "eir" in parsed:
        _null_complete_refs(parsed["eir"], diagnostics)
    if schema_valid and "eir" in parsed and "bundle" in parsed:
        try:
            _semantic(parsed["eir"], parsed["bundle"], parsed.get("groups"),
                      Path(bundle_root) if bundle_root is not None else None,
                      purpose, adapter_capabilities, diagnostics, max_artifact_bytes, max_document_bytes)
        except (TypeError, KeyError, AttributeError) as exc:
            _add(diagnostics, "semantic", "", "invalid_document_shape", f"semantic evaluation could not proceed: {type(exc).__name__}")
    diagnostics.sort(key=lambda d: (d.stage, d.pointer, d.code, d.message, d.severity))
    blocking = any(d.severity == "error" for d in diagnostics)
    incomplete_codes = {"group_manifest_unavailable", "bundle_root_unavailable", "adapter_capabilities_unavailable",
                        "approval_fingerprint_unavailable", "document_incomplete", "missing_artifact_hash",
                        "adapter_capabilities_unavailable"}
    invalid = any(d.severity == "error" and d.code not in incomplete_codes for d in diagnostics)
    incomplete = any(d.code in incomplete_codes for d in diagnostics)
    status = "invalid" if invalid else "incomplete" if incomplete else "valid"
    readiness = None
    if purpose == "execution_preflight":
        readiness = "ready" if status == "valid" else "blocked"
    profile = json.loads((PROFILE_DIR / "structural.axial_bar.v1.json").read_text(encoding="utf-8"))
    return ValidationReport(status, purpose, readiness, tuple(diagnostics), tuple(sorted(digests)), profile["profile_id"], profile["version"])
