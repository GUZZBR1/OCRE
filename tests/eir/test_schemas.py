import json
import hashlib
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas" / "eir"
EXAMPLES = SCHEMA_DIR / "examples" / "0.2.0"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class EirSchemaTests(unittest.TestCase):
    def check_document(self, schema_name, example_name):
        schema = load_json(SCHEMA_DIR / schema_name)
        document = load_json(EXAMPLES / example_name)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
        return schema, document

    def test_all_three_schemas_and_positive_examples(self):
        self.check_document("0.2.0.schema.json", "eir.json")
        self.check_document("input-bundle-0.2.0.schema.json", "input-bundle.json")
        self.check_document("mesh-group-manifest-0.2.0.schema.json", "mesh-group-manifest.json")

    def test_eir_rejects_wrong_version_and_missing_required_frame(self):
        schema = load_json(SCHEMA_DIR / "0.2.0.schema.json")
        document = load_json(EXAMPLES / "eir.json")
        invalid_version = dict(document, eir_schema_version="0.1.0")
        invalid_frame = json.loads(json.dumps(document))
        invalid_frame["frames"][0]["rotation_xyzw"] = [0, 0, 1]
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid_version)))
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid_frame)))

    def test_migration_draft_can_retain_pending_references(self):
        schema = load_json(SCHEMA_DIR / "0.2.0.schema.json")
        document = load_json(EXAMPLES / "eir.json")
        draft = json.loads(json.dumps(document))
        draft["document_state"] = "draft"
        draft["document_origin"] = "migration_v0.1"
        draft["migration"] = {"source_schema_version": "0.1.0", "source_sha256": "1" * 64, "tool_id": "eir_migrator", "tool_version": "0.1.0", "report_artifact_id": "migration_report"}
        draft["geometry"][0]["artifact_id"] = None
        draft["geometry"][0]["frame_id"] = None
        draft["frames"] = []
        case = draft["simulation_cases"][0]
        case["frame_id"] = None
        case["group_manifest_ref"] = None
        case["material_assignments"] = [{"geometry_id": None, "material_id": None}]
        case["constraints"][0]["frame_id"] = None
        Draft202012Validator(schema).validate(draft)
        draft["document_state"] = "complete"
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(draft)))

    def test_migrated_origin_requires_provenance(self):
        schema = load_json(SCHEMA_DIR / "0.2.0.schema.json")
        document = load_json(EXAMPLES / "eir.json")
        migrated = json.loads(json.dumps(document))
        migrated["document_origin"] = "migration_v0.1"
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(migrated)))

    def test_bundle_rejects_missing_artifact_metadata_and_bad_digest(self):
        schema = load_json(SCHEMA_DIR / "input-bundle-0.2.0.schema.json")
        document = load_json(EXAMPLES / "input-bundle.json")
        invalid_metadata = json.loads(json.dumps(document))
        del invalid_metadata["artifacts"][0]["uri"]
        invalid_digest = json.loads(json.dumps(document))
        invalid_digest["artifacts"][0]["sha256"] = "pending"
        duplicate = json.loads(json.dumps(document))
        duplicate["artifacts"].append(json.loads(json.dumps(duplicate["artifacts"][0])))
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid_metadata)))
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid_digest)))
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(duplicate)))

    def test_group_manifest_rejects_unresolved_artifact_metadata_fields(self):
        schema = load_json(SCHEMA_DIR / "mesh-group-manifest-0.2.0.schema.json")
        document = load_json(EXAMPLES / "mesh-group-manifest.json")
        invalid = json.loads(json.dumps(document))
        invalid["mesh_uri"] = "mesh/duplicate.inp"
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))

    def test_v01_schema_is_unchanged_and_still_validates_reference_example(self):
        schema = load_json(SCHEMA_DIR / "0.1.0.schema.json")
        document = load_json(SCHEMA_DIR / "examples" / "0.1.0" / "eir.json")
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)

    def test_v01_schema_and_example_digests_are_pinned(self):
        expected = {
            "0.1.0.schema.json": "30b6948d37f78def74080410fe900696275de83e5d42a10af3cae0d94d7d40d4",
            "mesh-group-manifest-0.1.0.schema.json": "2ebbce0b45119d51ab2668a7b301da36b6c1424e76a08a400c6bc28d10552338",
            "examples/0.1.0/eir.json": "ec588703903856e1ab7f31e9367b7b7c2cbb34ef3cb3332a67a8b910206a25ff"
        }
        for relative_path, digest in expected.items():
            actual = hashlib.sha256((SCHEMA_DIR / relative_path).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, relative_path)

    def test_v02_frozen_schema_digests_match_lock(self):
        lock = load_json(SCHEMA_DIR / "schema-lock-0.2.0.json")
        self.assertEqual(lock["lock_version"], "0.2.0")
        for entry in lock["schemas"]:
            actual = hashlib.sha256((SCHEMA_DIR / entry["path"]).read_bytes()).hexdigest()
            self.assertEqual(actual, entry["sha256"], entry["document_kind"])

    def test_duplicate_artifact_ids_are_left_to_semantic_preflight(self):
        schema = load_json(SCHEMA_DIR / "input-bundle-0.2.0.schema.json")
        document = load_json(EXAMPLES / "input-bundle.json")
        duplicate_id = json.loads(json.dumps(document["artifacts"][0]))
        duplicate_id["uri"] = "eir/other.json"
        document["artifacts"].append(duplicate_id)
        self.assertFalse(list(Draft202012Validator(schema).iter_errors(document)))


if __name__ == "__main__":
    unittest.main()
