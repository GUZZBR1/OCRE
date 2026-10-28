import hashlib
import json
import struct
import unittest
from pathlib import Path

from ocre_core.validation.approval_fingerprint import (
    FIELD_NAMES,
    FingerprintInputError,
    calculate_approval_fingerprint,
    jcs,
)


DOMAIN = b"OCRE:EIR-APPROVAL:FINGERPRINT\x00" + struct.pack(">H", 1)


def record(field_id, value_type, payload=b""):
    return struct.pack(">HBQ", field_id, value_type, len(payload)) + payload


def fingerprint(records):
    if len(records) != 10 or len({item[0] for item in records}) != len(records):
        raise ValueError("OCRE-AF/1 requires each field ID 1..10 exactly once")
    values = {}
    for field_id, value_type, payload in records:
        if not 1 <= field_id <= 10:
            raise ValueError("unknown OCRE-AF/1 field")
        expected_type = 0x01 if field_id in (1, 2) else 0x03
        if field_id == 10 and value_type == 0x00:
            expected_type = 0x00
        if value_type != expected_type or (value_type == 0x00 and payload):
            raise ValueError(f"invalid type or payload for field {field_id}")
        name = FIELD_NAMES[field_id - 1]
        values[name] = payload if field_id in (1, 2) else None if value_type == 0 else json.loads(payload)
    result = calculate_approval_fingerprint(values)
    return result.digest, result.encoded


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
        self.assertEqual(jcs([1e-7, 1e-6, 1e20, 1e21, -0.0]), b'[1e-7,0.000001,100000000000000000000,1e+21,0]')
        self.assertEqual(jcs([333333333.33333329, 1e30]), b'[333333333.3333333,1e+30]')
        self.assertEqual(jcs({"\ue000": 1, "\U00010000": 2}), b'{"\xf0\x90\x80\x80":2,"\xee\x80\x80":1}')
        self.assertEqual(json.loads(self.records[5][2]), json.loads((Path(__file__).resolve().parents[2] / "schemas" / "eir" / "examples" / "0.2.0" / "eir.json").read_text(encoding="utf-8"))["simulation_cases"][0])

    def test_rfc8785_appendix_b_number_vectors(self):
        vectors = {
            "0000000000000000": "0",
            "8000000000000000": "0",
            "0000000000000001": "5e-324",
            "8000000000000001": "-5e-324",
            "7fefffffffffffff": "1.7976931348623157e+308",
            "ffefffffffffffff": "-1.7976931348623157e+308",
            "4340000000000000": "9007199254740992",
            "c340000000000000": "-9007199254740992",
            "4430000000000000": "295147905179352830000",
            "44b52d02c7e14af5": "9.999999999999997e+22",
            "44b52d02c7e14af6": "1e+23",
            "44b52d02c7e14af7": "1.0000000000000001e+23",
            "444b1ae4d6e2ef4e": "999999999999999700000",
            "444b1ae4d6e2ef4f": "999999999999999900000",
            "444b1ae4d6e2ef50": "1e+21",
            "3eb0c6f7a0b5ed8c": "9.999999999999997e-7",
            "3eb0c6f7a0b5ed8d": "0.000001",
            "41b3de4355555553": "333333333.3333332",
            "41b3de4355555554": "333333333.33333325",
            "41b3de4355555555": "333333333.3333333",
            "41b3de4355555556": "333333333.3333334",
            "41b3de4355555557": "333333333.33333343",
            "becbf647612f3696": "-0.0000033333333333333333",
            "43143ff3c1cb0959": "1424953923781206.2",
        }
        for bits, expected in vectors.items():
            with self.subTest(bits=bits):
                value = struct.unpack(">d", bytes.fromhex(bits))[0]
                self.assertEqual(jcs(value), expected.encode("ascii"))
        self.assertEqual(jcs(9007199254740992), b"9007199254740992")
        with self.assertRaises(FingerprintInputError):
            jcs(9007199254740993)

    def test_rfc8785_unicode_property_sort_sample(self):
        value = {
            "\u20ac": "Euro Sign",
            "\r": "Carriage Return",
            "\ufb33": "Hebrew Letter Dalet With Dagesh",
            "1": "One",
            "\U0001f600": "Emoji: Grinning Face",
            "\u0080": "Control",
            "\u00f6": "Latin Small Letter O With Diaeresis",
        }
        self.assertEqual(jcs(value), b'{"\\r":"Carriage Return","1":"One","\xc2\x80":"Control","\xc3\xb6":"Latin Small Letter O With Diaeresis","\xe2\x82\xac":"Euro Sign","\xf0\x9f\x98\x80":"Emoji: Grinning Face","\xef\xac\xb3":"Hebrew Letter Dalet With Dagesh"}')

    def test_normative_golden_vector_pins_every_framed_byte(self):
        zeros = "0" * 64
        values = {
            "eir_document_sha256": bytes.fromhex("11" * 32),
            "bundle_manifest_sha256": bytes.fromhex("22" * 32),
            "profile": {"id": "p", "version": "1", "sha256": zeros},
            "schemas": [],
            "consumed_artifacts": [],
            "execution_parameters": {},
            "validator": {"id": "v", "version": "1", "implementation_sha256": zeros, "policy_version": "1"},
            "adapters": [],
            "unit_policy": {"id": "u", "version": "1", "sha256": zeros},
            "preparation_policy": None,
        }
        expected = DOMAIN + b"".join([
            record(1, 1, bytes.fromhex("11" * 32)),
            record(2, 1, bytes.fromhex("22" * 32)),
            record(3, 3, b'{"id":"p","sha256":"' + zeros.encode() + b'","version":"1"}'),
            record(4, 3, b"[]"),
            record(5, 3, b"[]"),
            record(6, 3, b"{}"),
            record(7, 3, b'{"id":"v","implementation_sha256":"' + zeros.encode() + b'","policy_version":"1","version":"1"}'),
            record(8, 3, b"[]"),
            record(9, 3, b'{"id":"u","sha256":"' + zeros.encode() + b'","version":"1"}'),
            record(10, 0, b""),
        ])
        result = calculate_approval_fingerprint(values)
        self.assertEqual(result.encoded, expected)
        self.assertEqual(result.digest, "64e510f555b49d888893015faa10b5fdf3941941631f3cc47553edb043388112")

    def test_golden_vector(self):
        digest, encoded = fingerprint(self.records)
        self.assertEqual(digest, "105aedd73f2fa576f9495ddc2f2ba7e6f99f356bfd2ab2b838f2097560fa4946")
        self.assertEqual(encoded[:len(DOMAIN)], DOMAIN)
        self.assertEqual(encoded[0:len(DOMAIN) + 11].hex(), "4f4352453a4549522d415050524f56414c3a46494e4745525052494e540000010001010000000000000020")

    def test_record_order_is_canonical_and_values_are_bound(self):
        expected, _ = fingerprint(self.records)
        reversed_records = list(reversed(self.records))
        self.assertEqual(fingerprint(reversed_records)[0], expected)
        for field_id, _, _ in self.records:
            changed = []
            for current_id, value_type, payload in self.records:
                if current_id != field_id:
                    changed.append((current_id, value_type, payload))
                elif current_id in (1, 2):
                    changed.append((current_id, value_type, payload[:-1] + bytes([payload[-1] ^ 1])))
                elif current_id == 10:
                    changed.append((current_id, 0x03, jcs({"id": "prep", "version": "1", "sha256": "a" * 64})))
                elif current_id in (4, 5, 8):
                    item = json.loads(payload)
                    if current_id == 5:
                        item[0]["sha256"] = "d" * 64
                    else:
                        item[0]["schema_version" if current_id == 4 else "version"] += "x"
                    changed.append((current_id, value_type, jcs(item)))
            else:
                if field_id in (3, 6, 7, 9):
                    item = json.loads(next(payload for i, _, payload in self.records if i == field_id))
                    if field_id == 6:
                        item["fingerprint_test_change"] = True
                    else:
                        item["version"] += "x"
                    changed.append((field_id, 0x03, jcs(item)))
            self.assertNotEqual(fingerprint(changed)[0], expected)

    def test_set_collections_are_sorted_and_duplicates_or_bad_hashes_rejected(self):
        values = {FIELD_NAMES[i - 1]: payload if i in (1, 2) else None if value_type == 0 else json.loads(payload)
                  for i, value_type, payload in self.records}
        baseline = calculate_approval_fingerprint(values).digest
        values["schemas"].reverse()
        self.assertEqual(calculate_approval_fingerprint(values).digest, baseline)
        values["schemas"].append(dict(values["schemas"][0]))
        with self.assertRaises(FingerprintInputError):
            calculate_approval_fingerprint(values)
        values["schemas"].pop()
        values["profile"]["sha256"] = "A" * 64
        with self.assertRaises(FingerprintInputError):
            calculate_approval_fingerprint(values)

    def test_rejects_non_jcs_values_and_missing_fields(self):
        with self.assertRaises(FingerprintInputError):
            jcs(float("nan"))
        values = {FIELD_NAMES[i - 1]: payload if i in (1, 2) else None if value_type == 0 else json.loads(payload)
                  for i, value_type, payload in self.records}
        del values["profile"]
        with self.assertRaises(FingerprintInputError):
            calculate_approval_fingerprint(values)

    def test_rejects_reserved_type_and_invalid_binary_hash_length(self):
        with self.assertRaises(ValueError):
            fingerprint([(field_id, 0x02 if field_id == 1 else value_type, payload)
                         for field_id, value_type, payload in self.records])
        values = {FIELD_NAMES[i - 1]: payload if i in (1, 2) else None if value_type == 0 else json.loads(payload)
                  for i, value_type, payload in self.records}
        values["eir_document_sha256"] = b"short"
        with self.assertRaises(FingerprintInputError):
            calculate_approval_fingerprint(values)

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
