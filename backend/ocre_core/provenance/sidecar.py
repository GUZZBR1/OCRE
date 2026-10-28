"""Validate local experimental CAD-to-mesh provenance sidecars.

The sidecar is an evidence index, not an EIR contract or a readiness gate.
Only relative POSIX URIs rooted at the supplied evidence directory are read.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

SIDECAR_VERSION = "cad-mesh-provenance/0.2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_GROUPS = {"VOLUME", "X_FIXED", "X_DISPLACED"}


def sha256_file(path: Path) -> str:
    """Return a lowercase SHA-256 digest for a file, streaming its contents."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_sidecar(*, case_id: str, revision_id: str, manifest_id: str,
                  mesh_artifacts: list[dict[str, Any]],
                  source_artifacts: list[dict[str, Any]] | None = None,
                  source_artifact_id: str | None = None,
                  source_uri: str | None = None,
                  source_sha256: str | None = None,
                  manifest_uri: str | None = None,
                  manifest_sha256: str | None = None,
                  groups: list[dict[str, Any]], tools: dict[str, Any] | None = None,
                  transformation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a versioned sidecar explicitly identifying this source as synthetic.

    The caller supplies stable semantic IDs; local tags and entity topology are
    intentionally not promoted to persistent identity here.
    """
    if source_artifacts is None:
        if source_artifact_id is None or source_uri is None or source_sha256 is None:
            raise ValueError("provide source_artifacts or all single-source fields")
        source_artifacts = [{
            "artifact_id": source_artifact_id,
            "uri": source_uri,
            "sha256": source_sha256,
            "origin": "synthetic_geo",
            "artifact_kind": "synthetic_geometry_definition",
        }]
    normalized_sources = []
    for item in source_artifacts:
        source = dict(item)
        source.setdefault("origin", "synthetic_geo")
        source.setdefault("artifact_kind", "synthetic_geometry_definition")
        normalized_sources.append(source)
    return {
        "sidecar_version": SIDECAR_VERSION,
        "case_id": case_id,
        "revision_id": revision_id,
        "manifest_id": manifest_id,
        "manifest_reference": ({"uri": manifest_uri, "sha256": manifest_sha256}
                               if manifest_uri is not None and manifest_sha256 is not None else None),
        "source_artifacts": normalized_sources,
        "transformation": transformation,
        "tools": tools or {},
        "groups": groups,
        "mesh_artifacts": mesh_artifacts,
        "identity_note": (
            "semantic IDs name intended engineering meaning; mesh tags and "
            "topology are revision-local and are not assumed stable"
        ),
    }


def _issue(code: str, message: str, *, pending: bool = False) -> dict[str, str]:
    return {"code": code, "message": message,
            "state": "pending" if pending else "invalid"}


def _safe_local_file(root: Path, uri: Any) -> tuple[Path | None, str | None]:
    if not isinstance(uri, str) or not uri:
        return None, "URI must be a non-empty relative POSIX path"
    if "\\" in uri or "\x00" in uri:
        return None, "URI must use normalized POSIX path syntax"
    raw = PurePosixPath(uri)
    if raw.is_absolute() or any(part in ("", ".", "..") for part in uri.split("/")):
        return None, "URI must be normalized, relative, and confined to the evidence root"
    candidate = root.joinpath(*raw.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "referenced artifact is missing or unresolvable"
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, "URI resolves outside the evidence root"
    if not resolved.is_file():
        return None, "referenced artifact is not a regular file"
    return resolved, None


def audit_sidecar(evidence_root: Path, sidecar: dict[str, Any]) -> dict[str, Any]:
    """Audit structure, local artifact confinement, hashes, and lineage.

    Status is ``complete`` when all evidence links resolve, ``incomplete`` for
    missing evidence/references, and ``invalid`` for malformed or contradictory
    evidence. This status never means engineering readiness or mesh acceptance.
    """
    root = evidence_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("evidence root must be a directory")
    issues: list[dict[str, str]] = []
    checks: list[dict[str, str]] = []
    if not isinstance(sidecar, dict):
        return {"status": "invalid", "checks": [],
                "issues": [_issue("sidecar_type", "sidecar must be an object")]}
    if sidecar.get("sidecar_version") != SIDECAR_VERSION:
        issues.append(_issue("version", f"sidecar_version must be {SIDECAR_VERSION}"))
    for field in ("case_id", "revision_id", "manifest_id"):
        if not isinstance(sidecar.get(field), str) or not sidecar[field].strip():
            issues.append(_issue("identity", f"{field} must be a non-empty string"))

    manifest_reference = sidecar.get("manifest_reference")
    prepared_manifest: dict[str, Any] | None = None
    if not isinstance(manifest_reference, dict):
        issues.append(_issue("manifest_reference", "prepared manifest URI and hash are required", pending=True))
    else:
        manifest_digest = manifest_reference.get("sha256")
        if not isinstance(manifest_digest, str) or not SHA256_RE.fullmatch(manifest_digest):
            issues.append(_issue("manifest_hash", "manifest_reference.sha256 must be 64 lowercase hexadecimal characters"))
        path, error = _safe_local_file(root, manifest_reference.get("uri"))
        if error:
            missing = error.startswith("referenced artifact is missing")
            issues.append(_issue("manifest_uri", error, pending=missing))
        elif path is not None and isinstance(manifest_digest, str) and SHA256_RE.fullmatch(manifest_digest):
            actual = sha256_file(path)
            if actual != manifest_digest:
                issues.append(_issue("manifest_hash_mismatch", "prepared manifest SHA-256 does not match"))
            else:
                if sidecar.get("manifest_id") != f"sha256:{actual}":
                    issues.append(_issue("manifest_id_mismatch", "manifest_id must equal sha256:<referenced manifest digest>"))
                try:
                    loaded_manifest = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    issues.append(_issue("manifest_content", f"prepared manifest is not readable JSON: {exc}"))
                else:
                    if isinstance(loaded_manifest, dict):
                        prepared_manifest = loaded_manifest
                    else:
                        issues.append(_issue("manifest_content", "prepared manifest root must be an object"))
                checks.append({"artifact_id": str(sidecar.get("manifest_id", "")),
                               "uri": str(manifest_reference.get("uri")), "sha256": actual})

    source_artifacts = sidecar.get("source_artifacts")
    sources_by_id: dict[str, str] = {}
    source_records_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(source_artifacts, list) or not source_artifacts:
        issues.append(_issue("source_artifacts", "source_artifacts must be a non-empty array", pending=True))
        source_artifacts = []
    for index, source in enumerate(source_artifacts):
        if not isinstance(source, dict):
            issues.append(_issue("source_shape", f"source_artifacts[{index}] must be an object"))
            continue
        source_id = source.get("artifact_id") if isinstance(source.get("artifact_id"), str) else None
        source_digest = source.get("sha256") if isinstance(source.get("sha256"), str) else None
        if not source_id:
            issues.append(_issue("source_id", f"source_artifacts[{index}].artifact_id must be a non-empty stable ID"))
        elif source_id in sources_by_id:
            issues.append(_issue("duplicate_source", f"duplicate source artifact ID: {source_id}"))
        if source.get("origin") != "synthetic_geo":
            issues.append(_issue("source_origin", f"source_artifacts[{index}] must be explicitly synthetic_geo"))
        if source.get("artifact_kind") != "synthetic_geometry_definition":
            issues.append(_issue("source_kind", f"source_artifacts[{index}] must be a synthetic geometry definition"))
        if not isinstance(source_digest, str) or not SHA256_RE.fullmatch(source_digest):
            issues.append(_issue("source_hash", f"source_artifacts[{index}].sha256 must be 64 lowercase hexadecimal characters"))
        elif source_id:
            sources_by_id[source_id] = source_digest
            source_records_by_id[source_id] = source
        path, error = _safe_local_file(root, source.get("uri"))
        if error:
            missing = error.startswith("referenced artifact is missing")
            issues.append(_issue("source_uri", f"source_artifacts[{index}]: {error}", pending=missing))
        elif path is not None and source_digest and SHA256_RE.fullmatch(source_digest):
            actual = sha256_file(path)
            if actual != source_digest:
                issues.append(_issue("source_hash_mismatch", f"source artifact {source_id or index} SHA-256 does not match"))
            else:
                checks.append({"artifact_id": source_id or "", "uri": str(source.get("uri")), "sha256": actual})

    groups = sidecar.get("groups")
    seen_groups: set[tuple[str, str]] = set()
    if not isinstance(groups, list):
        issues.append(_issue("groups", "groups must be an array"))
    else:
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                issues.append(_issue("group_shape", f"groups[{index}] must be an object"))
                continue
            semantic_id = group.get("semantic_id")
            if not isinstance(semantic_id, str) or not semantic_id.strip():
                issues.append(_issue("group_id", f"groups[{index}].semantic_id must be non-empty"))
                continue
            mesh_ref = group.get("mesh_artifact_id")
            group_key = (semantic_id, mesh_ref if isinstance(mesh_ref, str) else "")
            if group_key in seen_groups:
                issues.append(_issue("duplicate_group", f"duplicate semantic group ID {semantic_id} for mesh {group_key[1]}"))
            seen_groups.add(group_key)
            if not isinstance(group.get("mesh_artifact_id"), str) or not group["mesh_artifact_id"]:
                issues.append(_issue("group_mesh_link", f"group {semantic_id} requires mesh_artifact_id"))

    meshes = sidecar.get("mesh_artifacts")
    mesh_ids: set[str] = set()
    manifest_meshes = prepared_manifest.get("mesh_artifacts") if prepared_manifest else None
    if prepared_manifest is not None and not isinstance(manifest_meshes, list):
        issues.append(_issue("manifest_meshes", "prepared manifest must contain a mesh_artifacts array"))
        manifest_meshes = []
    if not isinstance(meshes, list) or not meshes:
        issues.append(_issue("mesh_artifacts", "mesh_artifacts must be a non-empty array", pending=True))
        meshes = []
    else:
        for index, mesh in enumerate(meshes):
            if not isinstance(mesh, dict):
                issues.append(_issue("mesh_shape", f"mesh_artifacts[{index}] must be an object"))
                continue
            mesh_id = mesh.get("artifact_id")
            if not isinstance(mesh_id, str) or not mesh_id:
                issues.append(_issue("mesh_id", f"mesh_artifacts[{index}].artifact_id must be non-empty"))
                continue
            if mesh_id in mesh_ids:
                issues.append(_issue("duplicate_mesh", f"duplicate mesh artifact ID: {mesh_id}"))
            mesh_ids.add(mesh_id)
            linked_source_id = mesh.get("source_artifact_id")
            linked_source_hash = mesh.get("source_sha256")
            digest = mesh.get("sha256")
            if not isinstance(linked_source_id, str) or not linked_source_id:
                issues.append(_issue("source_mesh_reference", f"mesh {mesh_id} has no source_artifact_id", pending=True))
            elif linked_source_id not in sources_by_id:
                issues.append(_issue("source_mesh_reference", f"mesh {mesh_id} references unknown source {linked_source_id}", pending=True))
            elif linked_source_hash != sources_by_id[linked_source_id]:
                issues.append(_issue("source_mesh_mismatch", f"mesh {mesh_id} source hash does not match source {linked_source_id}"))
            if isinstance(manifest_meshes, list):
                matching_records = [item for item in manifest_meshes
                                    if isinstance(item, dict) and item.get("mesh_input") == mesh.get("uri")]
                if len(matching_records) != 1:
                    _, uri_error = _safe_local_file(root, mesh.get("uri"))
                    missing = uri_error is not None and uri_error.startswith("referenced artifact is missing")
                    issues.append(_issue("manifest_mesh_reference", f"mesh {mesh_id} must match exactly one prepared manifest mesh entry", pending=missing))
                else:
                    recorded = matching_records[0]
                    source_record = source_records_by_id.get(linked_source_id) if isinstance(linked_source_id, str) else None
                    if recorded.get("mesh_sha256") != digest:
                        issues.append(_issue("manifest_mesh_hash_mismatch", f"mesh {mesh_id} hash differs from the prepared manifest"))
                    if source_record is not None and (source_record.get("uri") != recorded.get("geometry")
                                                       or source_record.get("sha256") != recorded.get("geometry_sha256")):
                        issues.append(_issue("manifest_source_mesh_mismatch", f"mesh {mesh_id} source does not match the prepared manifest geometry"))
                    for field in ("element_type", "node_count", "element_count"):
                        if field in mesh and mesh[field] != recorded.get(field):
                            issues.append(_issue("manifest_report_mismatch", f"mesh {mesh_id} {field} differs from the prepared manifest"))
                    volume = mesh.get("geometry_volume")
                    if isinstance(volume, dict) and volume.get("value") != recorded.get("volume_mm3"):
                        issues.append(_issue("manifest_report_mismatch", f"mesh {mesh_id} geometry volume differs from the prepared manifest"))
                    quality = mesh.get("quality_metrics", {}).get("shape_quality") if isinstance(mesh.get("quality_metrics"), dict) else None
                    recorded_quality = recorded.get("shape_quality")
                    if quality is not None:
                        if not isinstance(recorded_quality, dict):
                            issues.append(_issue("manifest_report_mismatch", f"mesh {mesh_id} shape quality is absent from the prepared manifest"))
                        else:
                            for field in ("minimum", "mean", "acceptance_threshold", "status"):
                                if field in quality and quality[field] != recorded_quality.get(field):
                                    issues.append(_issue("manifest_report_mismatch", f"mesh {mesh_id} shape-quality {field} differs from the prepared manifest"))
                            if "method" in quality and quality["method"] != recorded_quality.get("metric"):
                                issues.append(_issue("manifest_report_mismatch", f"mesh {mesh_id} shape-quality method differs from the prepared manifest"))
            quality_metrics = mesh.get("quality_metrics")
            if quality_metrics is not None and not isinstance(quality_metrics, (dict, list)):
                issues.append(_issue("quality_metrics_shape", f"mesh {mesh_id} quality_metrics must be an object or array"))
            if not isinstance(quality_metrics, dict) or not isinstance(quality_metrics.get("shape_quality"), dict):
                issues.append(_issue("quality_metrics_pending", f"mesh {mesh_id} requires recorded shape_quality evidence", pending=True))
            else:
                shape_quality = quality_metrics["shape_quality"]
                required_quality_fields = ("method", "minimum", "mean", "unit", "acceptance_threshold", "status")
                missing_quality_fields = [field for field in required_quality_fields if field not in shape_quality]
                if missing_quality_fields:
                    issues.append(_issue("quality_metrics_pending", f"mesh {mesh_id} shape_quality is missing {', '.join(missing_quality_fields)}", pending=True))
                if shape_quality.get("unit") != "1":
                    issues.append(_issue("quality_metrics_unit", f"mesh {mesh_id} mean-ratio unit must be dimensionless ('1')"))
                if shape_quality.get("acceptance_threshold") is not None:
                    issues.append(_issue("quality_threshold", f"mesh {mesh_id} must not define a universal quality threshold"))
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                issues.append(_issue("mesh_hash", f"mesh {mesh_id} SHA-256 must be 64 lowercase hexadecimal characters"))
            uri = mesh.get("uri")
            path, error = _safe_local_file(root, uri)
            if error:
                missing = error.startswith("referenced artifact is missing")
                issues.append(_issue("mesh_uri", f"mesh {mesh_id}: {error}", pending=missing))
            elif path is not None and isinstance(digest, str) and SHA256_RE.fullmatch(digest):
                actual = sha256_file(path)
                if actual != digest:
                    issues.append(_issue("mesh_hash_mismatch", f"mesh {mesh_id} SHA-256 does not match"))
                else:
                    checks.append({"artifact_id": mesh_id, "uri": str(uri), "sha256": actual})

    for group in groups if isinstance(groups, list) else []:
        if isinstance(group, dict) and isinstance(group.get("mesh_artifact_id"), str) and group["mesh_artifact_id"] not in mesh_ids:
            issues.append(_issue("broken_group_reference", f"group {group.get('semantic_id', '<unknown>')} references missing mesh {group['mesh_artifact_id']}", pending=True))
    for mesh_id in sorted(mesh_ids):
        present = {semantic_id for semantic_id, group_mesh_id in seen_groups if group_mesh_id == mesh_id}
        for missing_group in sorted(REQUIRED_GROUPS - present):
            issues.append(_issue("missing_group", f"mesh {mesh_id} is missing required benchmark group {missing_group}", pending=True))
    if isinstance(manifest_meshes, list):
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            mesh = next((item for item in meshes if isinstance(item, dict)
                         and item.get("artifact_id") == group.get("mesh_artifact_id")), None)
            if mesh is None:
                continue
            records = [item for item in manifest_meshes if isinstance(item, dict)
                       and item.get("mesh_input") == mesh.get("uri")]
            if len(records) != 1:
                continue
            observed = group.get("observed_count")
            reported_groups = records[0].get("groups")
            expected = reported_groups.get(group.get("semantic_id")) if isinstance(reported_groups, dict) else None
            if isinstance(expected, dict):
                expected = 1
            if expected is not None and observed != expected:
                issues.append(_issue("manifest_group_mismatch", f"group {group.get('semantic_id')} count differs from the prepared manifest"))

    if any(item["state"] == "invalid" for item in issues):
        status = "invalid"
    elif issues:
        status = "incomplete"
    else:
        status = "complete"
    return {"status": status, "checks": checks, "issues": issues,
            "quality_gate": "not_defined", "engineering_readiness": "not_assessed"}
