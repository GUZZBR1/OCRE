import hashlib
import json
import struct
import unittest
from pathlib import Path


DOMAIN = b"OCRE:EIR-APPROVAL:FINGERPRINT\x00" + struct.pack(">H", 1)


def record(field_id, value_type, payload=b""):
    return struct.pack(">HBQ", field_id, value_type, len(payload)) + payload


def fingerprint(records):
    records = sorted(records, key=lambda item: item[0])
    if [item[0] for item in records] != list(range(1, 11)):
        raise ValueError("OCRE-AF/1 requires each field ID 1..10 exactly once")
    if any(not 0 <= value_type <= 255 for _, value_type, _ in records):
        raise ValueError("record type must fit in one byte")
    for field_id, value_type, payload in records:
        expected_type = 0x01 if field_id in (1, 2) else 0x03
        if field_id == 10 and value_type == 0x00:
            expected_type = 0x00
        if value_type != expected_type or (value_type == 0x00 and payload):
            raise ValueError(f"invalid type or payload for field {field_id}")
    encoded = DOMAIN + b"".join(record(field_id, value_type, payload) for field_id, value_type, payload in records)
    return hashlib.sha256(encoded).hexdigest(), encoded


def jcs(value):
    # These golden-vector inputs use only ASCII keys and numbers whose RFC 8785
    # encoding is identical to this compact, sorted JSON serialization.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ApprovalFingerprintFramingTests(unittest.TestCase):
    def setUp(self):
        schema_entries = [
            {"document_kind": "mesh_group_manifest", "schema_version": "0.2.0", "schema_sha256": "f" * 64},
            {"document_kind": "input_bundle", "schema_version": "0.2.0", "schema_sha256": "b" * 64},
            {"document_kind": "eir", "schema_version": "0.2.0", "schema_sha256": "a" * 64},
        ]
        schema_entries.sort(key=lambda item: (item["document_kind"], item["schema_version"]))
        schema_dir = Path(__file__).resolve().parents[2] / "schemas" / "eir"
        eir_example_path = schema_dir / "examples" / "0.2.0" / "eir.json"
        profile_sha = hashlib.sha256((schema_dir / "profiles" / "structural.axial_bar.v1.json").read_bytes()).hexdigest()
        unit_policy_sha = hashlib.sha256((schema_dir / "profiles" / "ucum.structural_axial.v1.json").read_bytes()).hexdigest()
        selected_case = json.loads(eir_example_path.read_text(encoding="utf-8"))["simulation_cases"][0]
        self.records = [
            (1, 0x01, bytes.fromhex("11" * 32)),
            (2, 0x01, bytes.fromhex("22" * 32)),
            (3, 0x03, jcs({"id": "structural.axial_bar.v1", "version": "1", "sha256": profile_sha})),
            (4, 0x03, jcs(schema_entries)),
            (5, 0x03, b'[{"artifact_id":"mesh","sha256":"' + b"c" * 64 + b'"}]'),
            (6, 0x03, jcs(selected_case)),
            (7, 0x03, jcs({"id": "ocre.validator", "version": "0.1.0", "implementation_sha256": "8" * 64, "policy_version": "1"})),
            (8, 0x03, b'[{"stage_id":"solve","adapter_id":"calculix.batch","version":"2.23","binary_sha256":"' + b"d" * 64 + b'","capabilities_sha256":"' + b"e" * 64 + b'"}]'),
            (9, 0x03, jcs({"id": "ucum.structural_axial.v1", "version": "1", "sha256": unit_policy_sha})),
            (10, 0x00, b""),
        ]

    def test_jcs_structured_values_have_expected_bytes(self):
        self.assertEqual(jcs({"z": -2, "a": 0.01, "b": 1.25}), b'{"a":0.01,"b":1.25,"z":-2}')
        self.assertEqual(json.loads(self.records[5][2]), json.loads((Path(__file__).resolve().parents[2] / "schemas" / "eir" / "examples" / "0.2.0" / "eir.json").read_text(encoding="utf-8"))["simulation_cases"][0])

    def test_golden_vector(self):
        digest, encoded = fingerprint(self.records)
        self.assertEqual(digest, "f88bc0eb105e4cbe432567b75c307964d31beff658426a86788b564798ace911")
        self.assertTrue(encoded.startswith(DOMAIN))

    def test_record_order_is_canonical_and_values_are_bound(self):
        expected, _ = fingerprint(self.records)
        reversed_records = list(reversed(self.records))
        self.assertEqual(fingerprint(reversed_records)[0], expected)
        for index, (field_id, value_type, payload) in enumerate(self.records):
            changed = list(self.records)
            if field_id == 10:
                changed[index] = (field_id, 0x03, b"{}")
            else:
                changed[index] = (field_id, value_type, payload + b"x")
            self.assertNotEqual(fingerprint(changed)[0], expected)

    def test_missing_or_duplicate_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            fingerprint(self.records[:-1])
        with self.assertRaises(ValueError):
            fingerprint(self.records + [self.records[0]])

    def test_null_empty_and_absent_are_distinct(self):
        encodings = {
            b"",  # absent record (invalid for a complete fingerprint)
            record(10, 0x00, b""),
            record(10, 0x01, b""),
            record(10, 0x03, b"{}"),
        }
        self.assertEqual(len(encodings), 4)


if __name__ == "__main__":
    unittest.main()
