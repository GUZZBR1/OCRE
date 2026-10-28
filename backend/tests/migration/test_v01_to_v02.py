import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ocre_core.migration import MigrationInputError, migrate_v01_to_v02, validate_migration_draft


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "schemas/eir/examples/0.1.0/eir.json"
DEST_SCHEMA = ROOT / "schemas/eir/0.2.0.schema.json"
EXAMPLES = ROOT / "schemas/eir/examples/0.2.0"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def resolution(pointer, value, reason="explicit migration decision"):
    return {"json_pointer": pointer, "value": value, "reason": reason, "actor": "test-operator"}


def complete_shape_resolutions():
    frame = {
        "frame_id": "frame_tension_bar",
        "parent_frame_id": None,
        "translation": {"value": [0, 0, 0], "unit": "mm"},
        "rotation_xyzw": [0, 0, 0, 1],
    }
    choices = [
        resolution("/migration/report_artifact_id", "migration_report"),
        resolution("/geometry/0/artifact_id", "step_source"),
        resolution("/geometry/0/frame_id", "frame_tension_bar"),
        resolution("/frames", [frame]),
        resolution("/simulation_cases/0/frame_id", "frame_tension_bar"),
        resolution("/simulation_cases/0/geometry_refs", ["geom_tension_bar"]),
        resolution("/simulation_cases/0/material_refs", ["steel_linear_elastic"]),
        resolution("/simulation_cases/0/material_assignments", [
            {"geometry_id": "geom_tension_bar", "material_id": "steel_linear_elastic"}
        ]),
        resolution("/simulation_cases/0/group_manifest_ref", "artifact_group_manifest"),
    ]
    for kind in ("loads", "constraints"):
        case = json.loads(SOURCE.read_text(encoding="utf-8"))["simulation_cases"][0]
        for index, _ in enumerate(case[kind]):
            choices.append(resolution(f"/simulation_cases/0/{kind}/{index}/frame_id", "frame_tension_bar"))
    return choices


def test_migration_preserves_original_bytes_and_reports_unresolved_fields_without_writes():
    original = SOURCE.read_bytes() + b"\n"
    before = bytes(original)
    result = migrate_v01_to_v02(original, {}, actor="operator", now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    draft = json.loads(result.draft_bytes)

    assert result.source_bytes == before == original
    assert result.source_sha256 == hashlib.sha256(before).hexdigest()
    assert result.report["source"]["sha256"] == result.source_sha256
    assert result.report["source"]["id"] == result.report["output"]["id"] == "rev_tension_bar_001"
    assert result.report["output"]["sha256"] == hashlib.sha256(result.draft_bytes).hexdigest()
    assert {item["code"] for item in result.report["warnings"]} == {
        "legacy_embedded_artifact_metadata_preserved_in_source",
        "legacy_case_group_list_preserved_in_source",
    }
    assert draft["document_state"] == "draft"
    assert draft["document_origin"] == "migration_v0.1"
    assert result.status == "incomplete"
    assert {item["json_pointer"] for item in result.report["pending"]} >= {
        "/migration/report_artifact_id",
        "/geometry/0/artifact_id",
        "/geometry/0/frame_id",
        "/simulation_cases/0/material_assignments",
    }
    assert all(item["json_pointer"].startswith("/") for item in result.report["pending"])


def test_ambiguous_source_relationships_are_not_guessed_and_resolutions_are_recorded():
    result = migrate_v01_to_v02(SOURCE.read_bytes(), [], actor="operator", now=datetime(2026, 2, 3, tzinfo=timezone.utc))
    draft = json.loads(result.draft_bytes)
    assert draft["frames"] == []
    assert draft["geometry"][0]["artifact_id"] is None
    assert draft["geometry"][0]["frame_id"] is None
    assert draft["simulation_cases"][0]["geometry_refs"] == []
    assert draft["simulation_cases"][0]["material_assignments"] == []

    resolved = migrate_v01_to_v02(
        SOURCE.read_bytes(),
        [resolution("/migration/report_artifact_id", "migration_report"),
         resolution("/migration/report_artifact_id", "migration_report_v2", "corrected artifact ID")],
        actor="operator",
    )
    records = [item for item in resolved.report["resolutions"] if item["json_pointer"] == "/migration/report_artifact_id"]
    assert [item["value"] for item in records] == ["migration_report", "migration_report_v2"]
    assert json.loads(resolved.draft_bytes)["migration"]["report_artifact_id"] == "migration_report_v2"


def test_explicit_mapping_produces_a_structurally_valid_separate_draft():
    result = migrate_v01_to_v02(SOURCE.read_bytes(), complete_shape_resolutions(), actor="operator")
    draft = json.loads(result.draft_bytes)
    errors = list(Draft202012Validator(load(DEST_SCHEMA)).iter_errors(draft))
    assert errors == []
    assert draft["document_state"] == "draft"
    assert draft["migration"]["source_sha256"] == hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert result.report["schemas"]["source"]["schema_sha256"]
    assert result.report["schemas"]["destination"]["schema_sha256"]
    assert result.report["tool"]["implementation_sha256"]


def test_bundle_semantics_are_run_after_migration_and_readiness_stays_blocked():
    bundle = load(EXAMPLES / "input-bundle.json")
    bundle["artifacts"].extend([
        {"artifact_id": "artifact_group_manifest", "uri": "groups.json", "media_type": "application/json",
         "format": "ocre.mesh-group-manifest", "format_version": "0.2.0", "sha256": "1" * 64, "usage": "required"},
        {"artifact_id": "migration_report", "uri": "migration-report.json", "media_type": "application/json",
         "format": "ocre.migration-report", "format_version": "0.1.0", "sha256": "2" * 64, "usage": "required"},
    ])
    result = migrate_v01_to_v02(
        SOURCE.read_bytes(), complete_shape_resolutions(), actor="operator", bundle=bundle,
        group_manifest=load(EXAMPLES / "mesh-group-manifest.json"),
    )
    report = result.report["diagnostics"]["v02_validation"]
    codes = {item["code"] for item in report["diagnostics"]}
    assert report["purpose"] == "authoring"
    assert "document_incomplete" in codes
    assert result.report["diagnostics"]["ready"] is False


def test_invalid_source_resolution_and_hashes_are_rejected():
    with pytest.raises(MigrationInputError):
        migrate_v01_to_v02(b"{\"eir_schema_version\":\"0.1.0\",\"eir_schema_version\":\"0.1.0\"}", {}, actor="operator")
    with pytest.raises(MigrationInputError):
        migrate_v01_to_v02(b"{}", {}, actor="operator")
    with pytest.raises(MigrationInputError):
        migrate_v01_to_v02(SOURCE.read_bytes(), [resolution("/does/not/exist", "invented")], actor="operator")
    with pytest.raises(MigrationInputError):
        migrate_v01_to_v02(SOURCE.read_bytes(), [{"json_pointer": [], "value": "x", "reason": "bad pointer", "actor": "test"}], actor="operator")
    with pytest.raises(MigrationInputError):
        migrate_v01_to_v02(SOURCE.read_bytes(), [resolution("/geometry/0/artifact_id", "x", "")], actor="operator")
    with pytest.raises(MigrationInputError):
        migrate_v01_to_v02(SOURCE.read_bytes(), [resolution("/migration/source_sha256", "0" * 64)], actor="operator")
    for invalid_reference in ([], {"invented": "ref"}):
        result = migrate_v01_to_v02(
            SOURCE.read_bytes(), [resolution("/geometry/0/artifact_id", invalid_reference)], actor="operator"
        )
        assert result.status == "incomplete"
        assert result.report["diagnostics"]["destination_schema_validation"] == "invalid"
