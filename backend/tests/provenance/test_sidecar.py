from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from ocre_core.provenance.sidecar import (  # noqa: E402
    audit_sidecar,
    build_sidecar,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def evidence(tmp_path: Path) -> tuple[Path, dict]:
    (tmp_path / "source.geo").write_text("SetFactory(\"OpenCASCADE\");\nBox(1) = {0,0,0,1,1,1};\n")
    (tmp_path / "mesh.inp").write_text("*NODE\n1, 0, 0, 0\n")
    source_hash = digest(tmp_path / "source.geo")
    mesh_hash = digest(tmp_path / "mesh.inp")
    prepared = {"mesh_artifacts": [{
        "geometry": "source.geo", "geometry_sha256": source_hash,
        "mesh_input": "mesh.inp", "mesh_sha256": mesh_hash,
        "element_type": "C3D10", "node_count": 1, "element_count": 1,
        "volume_mm3": 1.0,
        "groups": {"VOLUME": 1, "X_FIXED": 1, "X_DISPLACED": 1},
        "shape_quality": {"metric": "corner mean ratio", "minimum": 0.8, "mean": 0.9,
                          "acceptance_threshold": None, "status": "measured_threshold_not_defined"},
    }]}
    (tmp_path / "prepared.json").write_text(json.dumps(prepared) + "\n")
    manifest_hash = digest(tmp_path / "prepared.json")
    groups = [
        {"semantic_id": "VOLUME", "mesh_artifact_id": "mesh-h1", "observed_count": 1},
        {"semantic_id": "X_FIXED", "mesh_artifact_id": "mesh-h1", "observed_count": 1},
        {"semantic_id": "X_DISPLACED", "mesh_artifact_id": "mesh-h1", "observed_count": 1},
    ]
    sidecar = build_sidecar(
        case_id="case_tension_bar_v1",
        revision_id="synthetic-box-r1",
        manifest_id=f"sha256:{manifest_hash}",
        source_artifact_id="geo-source-h1",
        source_uri="source.geo",
        source_sha256=source_hash,
        manifest_uri="prepared.json",
        manifest_sha256=manifest_hash,
        mesh_artifacts=[{
            "artifact_id": "mesh-h1",
            "uri": "mesh.inp",
            "sha256": mesh_hash,
            "source_artifact_id": "geo-source-h1",
            "source_sha256": source_hash,
            "semantic_ids": ["VOLUME", "X_FIXED", "X_DISPLACED"],
            "element_type": "C3D10", "node_count": 1, "element_count": 1,
            "geometry_volume": {"value": 1.0, "unit": "mm^3"},
            "quality_metrics": {"shape_quality": {
                "method": "corner mean ratio", "minimum": 0.8, "mean": 0.9,
                "unit": "1", "acceptance_threshold": None, "status": "measured_threshold_not_defined",
            }},
        }],
        groups=groups,
        tools={"gmsh": "4.15.2"},
        transformation={"name": "geo_to_inp", "version": "recorded-in-manifest"},
    )
    return tmp_path, sidecar


def test_valid_sidecar_links_hashes_and_marks_synthetic_source(evidence):
    root, sidecar = evidence

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "complete"
    assert len(report["checks"]) == 3
    assert sidecar["source_artifacts"][0]["origin"] == "synthetic_geo"
    assert report["quality_gate"] == "not_defined"
    assert report["engineering_readiness"] == "not_assessed"


@pytest.mark.parametrize("uri", ["../outside.geo", "/tmp/source.geo", "nested\\source.geo"])
def test_invalid_source_uri_is_rejected(evidence, uri):
    root, sidecar = evidence
    sidecar["source_artifacts"][0]["uri"] = uri

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "invalid"
    assert any(issue["code"] == "source_uri" for issue in report["issues"])


def test_invalid_hash_and_content_mismatch_are_rejected(evidence):
    root, sidecar = evidence
    sidecar["mesh_artifacts"][0]["sha256"] = "not-a-digest"

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "invalid"
    assert any(issue["code"] == "mesh_hash" for issue in report["issues"])

    sidecar["mesh_artifacts"][0]["sha256"] = "0" * 64
    report = audit_sidecar(root, sidecar)
    assert any(issue["code"] == "mesh_hash_mismatch" for issue in report["issues"])


def test_source_mesh_link_mismatch_is_rejected(evidence):
    root, sidecar = evidence
    sidecar["mesh_artifacts"][0]["source_sha256"] = "0" * 64

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "invalid"
    assert any(issue["code"] == "source_mesh_mismatch" for issue in report["issues"])


def test_manifest_hash_is_checked_and_missing_manifest_is_pending(evidence):
    root, sidecar = evidence
    sidecar["manifest_reference"]["sha256"] = "0" * 64

    report = audit_sidecar(root, sidecar)
    assert report["status"] == "invalid"
    assert any(issue["code"] == "manifest_hash_mismatch" for issue in report["issues"])

    sidecar["manifest_reference"]["sha256"] = digest(root / "prepared.json")
    sidecar["manifest_reference"]["uri"] = "not-produced.json"
    report = audit_sidecar(root, sidecar)
    assert report["status"] == "incomplete"
    assert any(issue["code"] == "manifest_uri" and issue["state"] == "pending"
               for issue in report["issues"])


def test_manifest_id_must_match_referenced_manifest_hash(evidence):
    root, sidecar = evidence
    sidecar["manifest_id"] = "sha256:" + "0" * 64

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "invalid"
    assert any(issue["code"] == "manifest_id_mismatch" for issue in report["issues"])


def test_group_count_and_quality_values_must_match_prepared_manifest(evidence):
    root, sidecar = evidence
    sidecar["groups"][0]["observed_count"] = 99
    sidecar["mesh_artifacts"][0]["quality_metrics"]["shape_quality"]["mean"] = 0.1

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "invalid"
    assert any(issue["code"] == "manifest_group_mismatch" for issue in report["issues"])
    assert any(issue["code"] == "manifest_report_mismatch" for issue in report["issues"])


def test_quality_evidence_is_required_and_has_no_threshold(evidence):
    root, sidecar = evidence
    sidecar["mesh_artifacts"][0]["quality_metrics"] = None

    report = audit_sidecar(root, sidecar)
    assert report["status"] == "incomplete"
    assert any(issue["code"] == "quality_metrics_pending" and issue["state"] == "pending"
               for issue in report["issues"])

    sidecar["mesh_artifacts"][0]["quality_metrics"] = {"shape_quality": {
        "method": "corner mean ratio", "minimum": 0.8, "mean": 0.9,
        "unit": "1", "acceptance_threshold": 0.5,
        "status": "measured_threshold_not_defined",
    }}
    report = audit_sidecar(root, sidecar)
    assert report["status"] == "invalid"
    assert any(issue["code"] == "quality_threshold" for issue in report["issues"])


def test_missing_groups_and_missing_artifacts_are_pending(evidence):
    root, sidecar = evidence
    sidecar["groups"] = sidecar["groups"][:-1]
    sidecar["mesh_artifacts"][0]["uri"] = "not-produced.inp"

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "incomplete"
    assert any(issue["code"] == "missing_group" and issue["state"] == "pending" for issue in report["issues"])
    assert any(issue["code"] == "mesh_uri" and issue["state"] == "pending" for issue in report["issues"])


def test_duplicate_semantic_groups_are_rejected(evidence):
    root, sidecar = evidence
    sidecar["groups"].append(dict(sidecar["groups"][0]))

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "invalid"
    assert any(issue["code"] == "duplicate_group" for issue in report["issues"])


def test_other_provenance_kind_cannot_be_mislabeled_as_this_benchmark(evidence):
    root, sidecar = evidence
    sidecar["source_artifacts"][0]["origin"] = "cad_import"

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "invalid"
    assert any(issue["code"] == "source_origin" for issue in report["issues"])


def test_each_mesh_links_to_its_matching_synthetic_geo_and_retains_quality_data(tmp_path):
    sources = []
    meshes = []
    groups = []
    for size in ("5mm", "2.5mm", "1.25mm"):
        source_uri = f"bar-{size}.geo"
        mesh_uri = f"bar-{size}.inp"
        (tmp_path / source_uri).write_text(f"Box for {size}\n")
        (tmp_path / mesh_uri).write_text(f"Mesh for {size}\n")
        source_id = f"geo-{size}"
        mesh_id = f"mesh-{size}"
        source_hash = digest(tmp_path / source_uri)
        sources.append({"artifact_id": source_id, "uri": source_uri, "sha256": source_hash,
                        "origin": "synthetic_geo", "artifact_kind": "synthetic_geometry_definition"})
        meshes.append({"artifact_id": mesh_id, "uri": mesh_uri, "sha256": digest(tmp_path / mesh_uri),
                       "source_artifact_id": source_id, "source_sha256": source_hash,
                       "quality_metrics": {"shape_quality": {
                           "method": "recorded mean ratio", "minimum": 0.91, "mean": 0.91,
                           "unit": "1", "acceptance_threshold": None, "status": "measured_threshold_not_defined",
                       }}})
        groups.extend({"semantic_id": semantic_id, "mesh_artifact_id": mesh_id, "observed_count": 1}
                      for semantic_id in ("VOLUME", "X_FIXED", "X_DISPLACED"))
    prepared = {"mesh_artifacts": []}
    for mesh, source in zip(meshes, sources):
        prepared["mesh_artifacts"].append({
            "geometry": source["uri"], "geometry_sha256": source["sha256"],
            "mesh_input": mesh["uri"], "mesh_sha256": mesh["sha256"],
            "groups": {"VOLUME": 1, "X_FIXED": 1, "X_DISPLACED": 1},
            "shape_quality": {"metric": "recorded mean ratio", "minimum": 0.91,
                              "mean": 0.91, "acceptance_threshold": None,
                              "status": "measured_threshold_not_defined"},
        })
    (tmp_path / "prepared.json").write_text(json.dumps(prepared) + "\n")
    manifest_hash = digest(tmp_path / "prepared.json")
    sidecar = build_sidecar(case_id="bar", revision_id="r1", manifest_id=f"sha256:{manifest_hash}",
                            source_artifacts=sources, mesh_artifacts=meshes, groups=groups,
                            manifest_uri="prepared.json", manifest_sha256=manifest_hash)

    report = audit_sidecar(tmp_path, sidecar)

    assert report["status"] == "complete"
    assert len(report["checks"]) == 7
    assert len(sidecar["source_artifacts"]) == 3
    assert meshes[0]["quality_metrics"]["shape_quality"]["mean"] == 0.91


def test_unknown_source_reference_is_pending(evidence):
    root, sidecar = evidence
    sidecar["mesh_artifacts"][0]["source_artifact_id"] = "not-yet-recorded"

    report = audit_sidecar(root, sidecar)

    assert report["status"] == "incomplete"
    assert any(issue["code"] == "source_mesh_reference" and issue["state"] == "pending"
               for issue in report["issues"])
