import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ocre_core.validation.validator as validator
from ocre_core.validation.approval_fingerprint import jcs
from ocre_core.validation.validator import ArtifactTooLarge, StrictJSONError, parse_json_strict, validate
from tests.helpers import (
    EIR_EXAMPLE_CAPS,
    eir_approval_context,
    stage_eir_example_bundle,
)

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

    def test_rejects_oversized_integer_literal_as_strict_json_error(self):
        with self.assertRaises(StrictJSONError):
            parse_json_strict("[" + "9" * 5000 + "]")

    def test_rejects_lone_surrogate_strings(self):
        for source in ('"\\ud800"', '{"\\ud800": 1}', '{"a": "\\udfff"}', '["\\udc00"]'):
            with self.subTest(source=source), self.assertRaises(StrictJSONError):
                parse_json_strict(source)
        for value in ({"a": "\ud800"}, {"\ud800": 1}, ["\udfff"]):
            with self.subTest(value=value), self.assertRaises(StrictJSONError):
                parse_json_strict(value)

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

    def test_deep_python_object_becomes_an_invalid_json_diagnostic(self):
        deep: list = []
        cursor = deep
        for _ in range(12000):
            child: list = []
            cursor.append(child)
            cursor = child
        report = validate(deep, load("input-bundle.json"))
        self.assertTrue(any(d.pointer == "/eir" and d.code == "invalid_json" for d in report.diagnostics))

    def test_malformed_bundled_schema_or_lock_becomes_schema_unavailable(self):
        variants = []
        with tempfile.TemporaryDirectory() as temp:
            schema_dir = Path(temp)
            schema_file = schema_dir / "0.2.0.schema.json"
            schema_file.write_text(json.dumps({"type": "not_a_json_schema_type"}), encoding="utf-8")
            digest = hashlib.sha256(schema_file.read_bytes()).hexdigest()
            variants.append({"lock_version": "0.2.0",
                             "schemas": [{"path": "0.2.0.schema.json", "sha256": digest}]})
            schema_file.write_text(json.dumps({"type": "object"}), encoding="utf-8")
            variants.append({"lock_version": "0.2.0", "schemas": {"0.2.0.schema.json": digest}})
            for lock in variants:
                (schema_dir / "schema-lock-0.2.0.json").write_text(json.dumps(lock), encoding="utf-8")
                with self.subTest(lock=lock), mock.patch.object(validator, "SCHEMA_DIR", schema_dir):
                    report = validate(load("eir.json"), load("input-bundle.json"))
                self.assertTrue(all(d.code == "schema_unavailable" for d in report.diagnostics))
                self.assertEqual("invalid", report.status)

    def test_unavailable_profile_is_a_fail_closed_diagnostic_not_a_crash(self):
        real_read_text = Path.read_text
        for missing in ("structural.axial_bar.v1.json", "ucum.structural_axial.v1.json"):
            def fail_profile(self, *args, **kwargs):
                if self.name == missing:
                    raise OSError("profile unavailable")
                return real_read_text(self, *args, **kwargs)
            with self.subTest(missing=missing), mock.patch.object(Path, "read_text", fail_profile):
                report = validate(load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json"))
            self.assertEqual("invalid", report.status)
            self.assertIn("schema_unavailable", {d.code for d in report.diagnostics})
            self.assertFalse(any(d.stage == "semantic" for d in report.diagnostics))
            self.assertEqual("structural.axial_bar.v1", report.profile_id)

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

    def test_non_list_capability_values_cannot_substring_match(self):
        caps = {"calculix.batch": {"supported": True, "domains": "structural",
                                 "analyses": ["static_linear"], "target_versions": ["2.23"],
                                 "element_types": {"C3D10": True}, "element_orders": [2]}}
        report = validate(load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json"),
                          purpose="execution_preflight", adapter_capabilities=caps)
        messages = {d.message for d in report.diagnostics if d.code == "adapter_capability_mismatch"}
        self.assertIn("adapter does not declare support for domains", messages)
        self.assertIn("adapter does not declare support for element_types", messages)
        self.assertNotIn("adapter does not declare support for analyses", messages)

    def test_preflight_never_ready_without_capabilities_and_verified_files(self):
        report = validate(load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json"), purpose="execution_preflight")
        self.assertEqual("blocked", report.execution_readiness)
        self.assertIn("adapter_capabilities_unavailable", {d.code for d in report.diagnostics})

    def test_document_size_limit_is_enforced(self):
        report = validate(load("eir.json"), load("input-bundle.json"), max_document_bytes=8)
        self.assertIn("document_too_large", {d.code for d in report.diagnostics})

    def test_unresolved_group_manifest_reference_preserves_remaining_checks(self):
        eir = load("eir.json")
        eir["simulation_cases"][0]["group_manifest_ref"] = "missing_artifact_id"
        eir["document_state"] = "draft"
        report = validate(eir, load("input-bundle.json"), load("mesh-group-manifest.json"))
        codes = {d.code for d in report.diagnostics}
        self.assertIn("unresolved_artifact", codes)
        self.assertNotIn("invalid_document_shape", codes)
        self.assertIn("document_incomplete", codes)  # semantic checks after the group block still ran

    def test_artifact_content_match_does_not_conflate_true_and_one(self):
        eir, bundle, groups = load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for item in bundle["artifacts"]:
                target = root / item["uri"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if item["artifact_id"] == bundle["eir_artifact_id"]:
                    contents = (EXAMPLES / "eir.json").read_bytes()
                elif item["artifact_id"] == "groups_axial_bar":
                    # counts.expected/observed are 1 in the manifest; encode them as booleans.
                    crafted = copy.deepcopy(groups)
                    crafted["groups"][0]["counts"] = {"expected": True, "observed": True}
                    contents = json.dumps(crafted, ensure_ascii=False).encode("utf-8")
                else:
                    contents = item["artifact_id"].encode()
                target.write_bytes(contents)
                item["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            caps = {"calculix.batch": {"supported": True, "domains": ["structural"], "analyses": ["static_linear"],
                                       "target_versions": ["2.23"], "element_types": ["C3D10"], "element_orders": [2]}}
            report = validate(eir, bundle, groups, bundle_root=root, purpose="execution_preflight", adapter_capabilities=caps)
            self.assertIn("group_manifest_content_mismatch", {d.code for d in report.diagnostics})

    def test_missing_ids_do_not_produce_spurious_manifest_mismatch(self):
        eir, bundle, groups = load("eir.json"), load("input-bundle.json"), load("mesh-group-manifest.json")
        del eir["simulation_cases"][0]["group_manifest_ref"]
        artifact = bundle["artifacts"][0]
        del artifact["artifact_id"]
        artifact["usage"] = "required"
        diagnostics = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for item in bundle["artifacts"]:
                target = root / item["uri"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if item.get("artifact_id") == "groups_axial_bar":
                    target.write_bytes((EXAMPLES / "mesh-group-manifest.json").read_bytes())
                else:
                    target.write_bytes(b"{}")
                item["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            profile = json.loads((validator.PROFILE_DIR / "structural.axial_bar.v1.json").read_text(encoding="utf-8"))
            unit_profile = json.loads((validator.PROFILE_DIR / "ucum.structural_axial.v1.json").read_text(encoding="utf-8"))
            validator._semantic(eir, bundle, groups, root, "execution_preflight", None,
                                diagnostics, 1024 * 1024, 1024 * 1024, profile, unit_profile)
            self.assertNotIn("group_manifest_content_mismatch", {d.code for d in diagnostics})

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


class ApprovalFingerprintTests(unittest.TestCase):
    def _validate(self, eir, bundle, groups, root, caps=EIR_EXAMPLE_CAPS,
                  context=None, case_id="axial_bar"):
        return validate(eir, bundle, groups, bundle_root=root,
                        purpose="execution_preflight", adapter_capabilities=caps,
                        case_id=case_id,
                        approval_context=eir_approval_context(caps) if context is None else context)

    def test_verified_context_computes_fingerprint_and_reports_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            root, eir, bundle, groups = stage_eir_example_bundle(temp)
            report = self._validate(eir, bundle, groups, root)
        self.assertEqual("valid", report.status, report.as_dict())
        self.assertEqual("ready", report.execution_readiness)
        self.assertRegex(report.approval_fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(report.approval_fingerprint,
                         report.as_dict()["approval_fingerprint"])

    def test_fingerprint_is_deterministic_and_binds_staged_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root, eir, bundle, groups = stage_eir_example_bundle(temp)
            first = self._validate(eir, bundle, groups, root)
            second = self._validate(eir, bundle, groups, root)
            self.assertEqual(first.approval_fingerprint, second.approval_fingerprint)
            step = next(a for a in bundle["artifacts"] if a["artifact_id"] == "step_source")
            (root / step["uri"]).write_bytes(b"different step bytes")
            step["sha256"] = hashlib.sha256(b"different step bytes").hexdigest()
            changed = self._validate(eir, bundle, groups, root)
            self.assertEqual("ready", changed.execution_readiness)
            self.assertNotEqual(first.approval_fingerprint, changed.approval_fingerprint)

    def test_single_case_selects_implicitly_and_unknown_case_id_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root, eir, bundle, groups = stage_eir_example_bundle(temp)
            implicit = self._validate(eir, bundle, groups, root, case_id=None)
            self.assertEqual("ready", implicit.execution_readiness)
            explicit = self._validate(eir, bundle, groups, root)
            self.assertEqual(implicit.approval_fingerprint, explicit.approval_fingerprint)
            unknown = self._validate(eir, bundle, groups, root, case_id="no_such_case")
        self.assertEqual("blocked", unknown.execution_readiness)
        self.assertIsNone(unknown.approval_fingerprint)
        self.assertIn("approval_case_unresolved", {d.code for d in unknown.diagnostics})

    def test_missing_context_inputs_block_as_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root, eir, bundle, groups = stage_eir_example_bundle(temp)
            for context in ({}, {"validator": eir_approval_context()["validator"]}):
                report = self._validate(eir, bundle, groups, root, context=context)
                self.assertEqual("incomplete", report.status)
                self.assertEqual("blocked", report.execution_readiness)
                self.assertIn("approval_fingerprint_inputs_missing",
                              {d.code for d in report.diagnostics})

    def test_adapter_mismatches_block_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root, eir, bundle, groups = stage_eir_example_bundle(temp)
            wrong_id = [{"stage_id": "solve", "adapter_id": "gmsh", "version": "1",
                         "binary_sha256": "d" * 64, "capabilities_sha256": "e" * 64}]
            wrong_version = [{"stage_id": "solve", "adapter_id": "calculix.batch",
                              "version": "2.22", "binary_sha256": "d" * 64,
                              "capabilities_sha256": hashlib.sha256(
                                  jcs(EIR_EXAMPLE_CAPS["calculix.batch"])).hexdigest()}]
            wrong_caps = [{"stage_id": "solve", "adapter_id": "calculix.batch",
                           "version": "2.23", "binary_sha256": "d" * 64,
                           "capabilities_sha256": "0" * 64}]
            for adapters in (wrong_id, wrong_version, wrong_caps):
                report = self._validate(eir, bundle, groups, root,
                                        context=eir_approval_context(adapters=adapters))
                self.assertEqual("invalid", report.status)
                self.assertEqual("blocked", report.execution_readiness)
                self.assertIsNone(report.approval_fingerprint)
                self.assertIn("approval_adapter_mismatch",
                              {d.code for d in report.diagnostics})

    def test_malformed_context_values_block_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root, eir, bundle, groups = stage_eir_example_bundle(temp)
            report = self._validate(eir, bundle, groups, root,
                                    context=eir_approval_context(validator={"id": "v"}))
        self.assertEqual("invalid", report.status)
        self.assertEqual("blocked", report.execution_readiness)
        self.assertIn("approval_fingerprint_invalid", {d.code for d in report.diagnostics})

    def test_semantic_errors_suppress_fingerprint_even_with_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root, eir, bundle, groups = stage_eir_example_bundle(temp)
            eir["simulation_cases"][0]["material_refs"] = ["unresolved"]
            report = self._validate(eir, bundle, groups, root)
        self.assertEqual("invalid", report.status)
        self.assertEqual("blocked", report.execution_readiness)
        self.assertIsNone(report.approval_fingerprint)
        self.assertNotIn("approval_fingerprint_unavailable",
                         {d.code for d in report.diagnostics})

    def test_bundle_manifest_hash_binds_the_supplied_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root, eir, bundle, groups = stage_eir_example_bundle(temp)
            context = eir_approval_context()
            as_dict = self._validate(eir, bundle, groups, root, context=context)
            canonical = self._validate(eir, jcs(bundle), groups, root, context=context)
            pretty = self._validate(eir, json.dumps(bundle, indent=2).encode("utf-8"),
                                    groups, root, context=context)
        self.assertEqual("ready", as_dict.execution_readiness)
        self.assertEqual(as_dict.approval_fingerprint, canonical.approval_fingerprint)
        self.assertNotEqual(as_dict.approval_fingerprint, pretty.approval_fingerprint)


if __name__ == "__main__":
    unittest.main()
