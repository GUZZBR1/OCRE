import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ocre_core.validation.validator as validator
from ocre_core.validation.validator import ArtifactTooLarge, parse_json_strict, validate

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "schemas/eir/examples/0.2.0"


def load(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class StrictJsonTests(unittest.TestCase):
    def test_rejects_duplicate_keys_and_non_finite_numbers(self):
        for source in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                parse_json_strict(source)

    def test_rejects_non_finite_python_values(self):
        with self.assertRaises(ValueError):
            parse_json_strict({"x": float("inf")})

    def test_deep_json_inputs_become_diagnostics_for_all_document_kinds(self):
        deep = b"[" * 2500 + b"0" + b"]" * 2500
        examples = load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json")
        for index, kind in enumerate(("eir", "bundle", "groups")):
            documents = list(examples)
            documents[index] = deep
            with self.subTest(kind=kind):
                report = validate(documents[0], documents[1], documents[2])
                self.assertTrue(any(d.pointer == f"/{kind}" and d.code == "invalid_json" for d in report.diagnostics))

    def test_schema_recursion_becomes_a_validation_diagnostic(self):
        with mock.patch.object(validator, "_schema", side_effect=RecursionError("nested schema")):
            report = validate(load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json"))
        self.assertEqual(3, sum(d.code == "schema_unavailable" for d in report.diagnostics))

    def test_bounded_read_detects_growth_using_the_same_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "growing.bin"
            target.write_bytes(b"")
            original_fstat = validator.os.fstat
            original_read = validator.os.read
            descriptors = []
            requested_sizes = []
            grown = False

            def grow_after_fstat(fd):
                nonlocal grown
                descriptors.append(fd)
                result = original_fstat(fd)
                if not grown:
                    grown = True
                    with target.open("ab") as output:
                        output.write(b"0123456789")
                return result

            def track_read(fd, size):
                descriptors.append(fd)
                requested_sizes.append(size)
                return original_read(fd, size)

            with mock.patch.object(validator.os, "fstat", side_effect=grow_after_fstat), \
                    mock.patch.object(validator.os, "read", side_effect=track_read):
                with self.assertRaises(ArtifactTooLarge):
                    validator._read_confined(root, "growing.bin", 5)
            self.assertTrue(requested_sizes)
            self.assertLessEqual(max(requested_sizes), 6)
            self.assertEqual(1, len(set(descriptors)))


class ValidatorTests(unittest.TestCase):
    def test_validates_examples_with_explicit_format_checker(self):
        report = validate(load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json"))
        self.assertEqual("valid", report.status, report.as_dict())
        invalid_time = load("eir.json")
        invalid_time["revision"]["created_at"] = "2025-99-99T88:00:00Z"
        report = validate(invalid_time, load("input-bundle.json"))
        self.assertIn("schema_validation", {d.code for d in report.diagnostics})

    def test_reports_identity_and_material_relation_errors(self):
        eir = load("eir.json")
        eir["revision"]["project_id"] = "another_project"
        eir["simulation_cases"][0]["material_refs"] = ["different"]
        report = validate(eir, load("input-bundle.json"), load("mesh-group-manifest.json"))
        codes = {d.code for d in report.diagnostics}
        self.assertIn("project_revision_mismatch", codes)
        self.assertIn("material_refs_mismatch", codes)

    def test_complete_document_null_references_get_semantic_diagnostics(self):
        eir = load("eir.json")
        eir["geometry"][0]["artifact_id"] = None
        eir["geometry"][0]["frame_id"] = None
        case = eir["simulation_cases"][0]
        case["frame_id"] = None
        case["group_manifest_ref"] = None
        case["constraints"][0]["frame_id"] = None
        case["material_assignments"][0]["material_id"] = None
        report = validate(eir, load("input-bundle.json"), load("mesh-group-manifest.json"))
        nulls = [d for d in report.diagnostics if d.code == "null_complete_reference"]
        self.assertEqual(6, len(nulls))

    def test_draft_is_incomplete_and_optional_consumed_artifact_blocks_preflight(self):
        eir, bundle, groups = load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json")
        eir["document_state"] = "draft"
        bundle["artifacts"][1]["usage"] = "optional"  # geometry consumes step_source
        report = validate(eir, bundle, groups, purpose="execution_preflight")
        codes = {d.code for d in report.diagnostics}
        self.assertIn("document_incomplete", codes)
        self.assertIn("optional_artifact_consumed", codes)
        self.assertEqual("blocked", report.execution_readiness)

    def test_rejects_incompatible_group_artifact_format(self):
        bundle = load("input-bundle.json")
        bundle["artifacts"][3]["format"] = "application/octet-stream"
        report = validate(load("eir.json"), bundle, load("mesh-group-manifest.json"))
        self.assertIn("group_manifest_format_mismatch", {d.code for d in report.diagnostics})

    def test_selection_reference_resolves_against_semantic_groups(self):
        eir = load("eir.json")
        condition = eir["simulation_cases"][0]["constraints"][0]
        del condition["group"]
        condition["selection_ref"] = "DOES_NOT_EXIST"
        report = validate(eir, load("input-bundle.json"), load("mesh-group-manifest.json"))
        self.assertIn("unresolved_group", {d.code for d in report.diagnostics})

    def test_rejects_bad_frame_and_unit_without_mutating_document(self):
        eir = load("eir.json")
        original = copy.deepcopy(eir)
        eir["frames"][0]["rotation_xyzw"] = [0, 0, 0, 2]
        eir["geometry"][0]["length_unit"] = "furlong"
        eir["frames"][0]["translation"]["unit"] = "furlong"
        report = validate(eir, load("input-bundle.json"), load("mesh-group-manifest.json"))
        codes = {d.code for d in report.diagnostics}
        self.assertIn("invalid_root_transform", codes)
        self.assertIn("invalid_quaternion_norm", codes)
        self.assertIn("unsupported_unit", codes)
        self.assertTrue(any(d.pointer == "/frames/0/translation/unit" and d.code == "unsupported_unit" for d in report.diagnostics))
        self.assertEqual(original["materials"], eir["materials"])

    def test_rejects_duplicate_artifact_ids_and_unsafe_uris(self):
        bundle = load("input-bundle.json")
        duplicate = copy.deepcopy(bundle["artifacts"][0])
        duplicate["uri"] = "safe/other.json"
        bundle["artifacts"].append(duplicate)
        bundle["artifacts"][1]["uri"] = "../outside.step"
        report = validate(load("eir.json"), bundle, load("mesh-group-manifest.json"))
        codes = {d.code for d in report.diagnostics}
        self.assertIn("duplicate_id", codes)
        self.assertIn("unsafe_uri", codes)
        bundle["artifacts"][1]["uri"] = "%2e%2e/outside.step"
        report = validate(load("eir.json"), bundle, load("mesh-group-manifest.json"))
        self.assertIn("unsafe_uri", {d.code for d in report.diagnostics})

    def test_preflight_never_ready_without_capabilities_and_verified_files(self):
        report = validate(load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json"), purpose="execution_preflight")
        self.assertEqual("blocked", report.execution_readiness)
        self.assertIn("adapter_capabilities_unavailable", {d.code for d in report.diagnostics})

    def test_document_size_limit_is_enforced(self):
        report = validate(load("eir.json"), load("input-bundle.json"), max_document_bytes=8)
        self.assertIn("document_too_large", {d.code for d in report.diagnostics})

    def test_preflight_hashes_only_confined_bytes_and_checks_groups(self):
        eir, bundle, groups = load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for item in bundle["artifacts"]:
                target = root / item["uri"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if item["artifact_id"] == bundle["eir_artifact_id"]:
                    contents = (EXAMPLES / "eir.json").read_bytes()
                elif item["artifact_id"] == "groups_axial_bar":
                    contents = (EXAMPLES / "mesh-group-manifest.json").read_bytes()
                else:
                    contents = item["artifact_id"].encode()
                target.write_bytes(contents)
                item["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            caps = {"calculix.batch": {"supported": True, "domains": ["structural"], "analyses": ["static_linear"], "target_versions": ["2.23"], "element_types": ["C3D10"], "element_orders": [2]}}
            report = validate(eir, bundle, groups, bundle_root=root, purpose="execution_preflight", adapter_capabilities=caps)
            self.assertEqual("incomplete", report.status, report.as_dict())
            self.assertEqual("blocked", report.execution_readiness)
            self.assertIn("approval_fingerprint_unavailable", {d.code for d in report.diagnostics})
            mismatched_groups = copy.deepcopy(groups)
            mismatched_groups["mesh"]["node_count"] += 1
            report = validate(eir, bundle, mismatched_groups, bundle_root=root, purpose="execution_preflight", adapter_capabilities=caps)
            self.assertIn("group_manifest_content_mismatch", {d.code for d in report.diagnostics})
            bundle["artifacts"][1]["sha256"] = "0" * 64
            report = validate(eir, bundle, groups, bundle_root=root, purpose="execution_preflight", adapter_capabilities=caps)
            self.assertEqual("blocked", report.execution_readiness)
            self.assertIn("artifact_hash_mismatch", {d.code for d in report.diagnostics})

    def test_report_and_diagnostics_are_deterministically_sorted(self):
        eir = load("eir.json")
        eir["revision"]["project_id"] = "wrong"
        one = validate(eir, load("input-bundle.json"), load("mesh-group-manifest.json")).as_dict()
        two = validate(eir, load("input-bundle.json"), load("mesh-group-manifest.json")).as_dict()
        self.assertEqual(one, two)
        keys = [(d["stage"], d["pointer"], d["code"], d["message"]) for d in one["diagnostics"]]
        self.assertEqual(sorted(keys), keys)


if __name__ == "__main__":
    unittest.main()
