"""Tests for the DRAW-AF/1 approval fingerprint (ocre_core/draw/fingerprint)."""
from __future__ import annotations

import copy
import hashlib
import unittest

from ocre_core.draw.fingerprint import (
    DOMAIN,
    FIELD_NAMES,
    DrawApprovalFingerprint,
    DrawFingerprintInputError,
    compute_fingerprint,
    jcs,
    parse_encoded,
)
from ocre_core.validation.approval_fingerprint import (  # noqa: E402
    calculate_approval_fingerprint,
)


def _sha_bytes(seed: str) -> bytes:
    return hashlib.sha256(seed.encode()).digest()


def _sha_hex(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def valid_inputs() -> dict:
    return {
        "draw_document_sha256": _sha_bytes("draw-document"),
        "bundle_manifest_sha256": _sha_bytes("bundle-manifest"),
        "profile": {"id": "draw.paper.v1", "version": "0.1.0",
                    "sha256": _sha_hex("draw-paper-schema")},
        "schemas": [
            {"document_kind": "draw.paper", "schema_version": "0.1.0",
             "schema_sha256": _sha_hex("schema-draw-paper")},
            {"document_kind": "draw.export-case", "schema_version": "0.1.0",
             "schema_sha256": _sha_hex("schema-export-case")},
        ],
        "consumed_artifacts": [
            {"artifact_id": "canvas-png-1", "sha256": _sha_hex("canvas")},
            {"artifact_id": "session-report-1",
             "sha256": _sha_hex("session-report")},
        ],
        "export_parameters": {
            "target": "dxf-r2000",
            "paper": "A4",
            "scale_mm_per_px": 0.247,
            "include_erased": False,
        },
        "validator": {"id": "draw-paper-validator", "version": "0.1.0",
                      "implementation_sha256": _sha_hex("validator-impl"),
                      "policy_version": "0.1.0"},
        "adapters": [
            {"stage_id": "vectorize", "adapter_id": "draw.vectorize",
             "version": "8a17115",
             "binary_sha256": _sha_hex("vectorize-bin"),
             "capabilities_sha256": _sha_hex("vectorize-caps")},
            {"stage_id": "export", "adapter_id": "draw.export.dxf",
             "version": "0.1.0",
             "binary_sha256": _sha_hex("dxf-writer-bin"),
             "capabilities_sha256": _sha_hex("dxf-writer-caps")},
        ],
        "unit_policy": {"id": "mm", "version": "0.1.0",
                        "sha256": _sha_hex("unit-policy-mm")},
        "preparation_policy": {"id": "draw-prep", "version": "0.1.0",
                               "sha256": _sha_hex("prep-policy")},
    }


class JcsVectorsTests(unittest.TestCase):
    """The reused canonizer must be real RFC 8785 JCS, not sort_keys JSON.

    These vectors pin the behaviours where ``json.dumps(sort_keys=True)``
    differs from JCS: UTF-16 key ordering (astral-plane keys sort before
    U+E000 in JCS, after it in code-point order) and ECMAScript number
    rendering (1.0 -> "1", -0.0 -> "0", 1e21 -> "1e+21", ...).
    """

    def test_key_ordering_is_utf16_not_code_point(self):
        # U+1F600 (surrogate pair D83D DE00) sorts BEFORE U+E000 under
        # UTF-16 ordering; json.dumps(sort_keys=True) puts it after.
        self.assertEqual(
            jcs({"\ue000": 1, "\U0001F600": 2}),
            b'{"\xf0\x9f\x98\x80":2,"\xee\x80\x80":1}')
        self.assertEqual(jcs({"b": 1, "a": 2}), b'{"a":2,"b":1}')
        self.assertEqual(jcs({"2": 1, "10": 2}), b'{"10":2,"2":1}')

    def test_number_rendering_follows_ecmascript(self):
        self.assertEqual(jcs({"a": 1.0}), b'{"a":1}')
        self.assertEqual(jcs({"a": -0.0}), b'{"a":0}')
        self.assertEqual(jcs({"a": 1e21}), b'{"a":1e+21}')
        self.assertEqual(jcs({"a": 1e-7}), b'{"a":1e-7}')
        self.assertEqual(jcs({"a": 0.000001}), b'{"a":0.000001}')
        self.assertEqual(jcs({"a": 1.5}), b'{"a":1.5}')
        self.assertEqual(jcs({"a": 333333333.33333329}),
                         b'{"a":333333333.3333333}')
        self.assertEqual(jcs({"a": 1e30}), b'{"a":1e+30}')
        self.assertEqual(jcs({"a": 2 ** 53}), b'{"a":9007199254740992}')

    def test_string_escaping_keeps_unicode_unescaped(self):
        self.assertEqual(jcs({"a": "\u20ac"}),
                         '{"a":"\u20ac"}'.encode("utf-8"))
        self.assertEqual(jcs({"a": "x\ny"}), b'{"a":"x\\ny"}')
        self.assertEqual(jcs({"a": "\u0001"}), b'{"a":"\\u0001"}')

    def test_non_finite_and_inexact_integers_are_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf"), 2 ** 53 + 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                jcs({"a": bad})


class ComputeFingerprintTests(unittest.TestCase):
    def test_valid_inputs_produce_digest_and_draw_domain(self):
        result = compute_fingerprint(valid_inputs())
        self.assertIsInstance(result, DrawApprovalFingerprint)
        self.assertEqual(len(result.digest), 64)
        self.assertEqual(result.digest,
                         hashlib.sha256(result.encoded).hexdigest())
        self.assertTrue(result.encoded.startswith(DOMAIN))
        self.assertIn(b"DRAW:PAPER-APPROVAL:FINGERPRINT", result.encoded)
        self.assertNotIn(b"OCRE", result.encoded)

    def test_deterministic_and_order_insensitive(self):
        first = compute_fingerprint(valid_inputs())
        shuffled = valid_inputs()
        shuffled["schemas"] = list(reversed(shuffled["schemas"]))
        shuffled["consumed_artifacts"] = list(
            reversed(shuffled["consumed_artifacts"]))
        shuffled["adapters"] = list(reversed(shuffled["adapters"]))
        shuffled["export_parameters"] = dict(
            reversed(list(shuffled["export_parameters"].items())))
        second = compute_fingerprint(shuffled)
        self.assertEqual(first, second)

    def test_framing_parses_back_to_ten_records(self):
        result = compute_fingerprint(valid_inputs())
        records = parse_encoded(result.encoded)
        self.assertEqual(len(records), 10)
        self.assertEqual([r[0] for r in records], list(range(1, 11)))
        kinds = [r[1] for r in records]
        self.assertEqual(kinds, [0x01, 0x01] + [0x03] * 8)
        # Fields 1 and 2 carry the raw digests.
        self.assertEqual(records[0][2], _sha_bytes("draw-document"))
        self.assertEqual(records[1][2], _sha_bytes("bundle-manifest"))
        # Field 3 is JCS with the draw.paper.v1 profile id.
        self.assertEqual(records[2][2],
                         b'{"id":"draw.paper.v1","sha256":"'
                         + _sha_hex("draw-paper-schema").encode()
                         + b'","version":"0.1.0"}')

    def test_null_preparation_policy_encodes_as_kind_zero(self):
        inputs = valid_inputs()
        inputs["preparation_policy"] = None
        result = compute_fingerprint(inputs)
        records = parse_encoded(result.encoded)
        self.assertEqual(records[9], (10, 0x00, b""))

    def test_inputs_are_not_mutated(self):
        inputs = valid_inputs()
        before = copy.deepcopy(inputs)
        compute_fingerprint(inputs)
        self.assertEqual(inputs, before)

    def test_missing_or_extra_field_is_an_error(self):
        for missing in FIELD_NAMES:
            inputs = valid_inputs()
            del inputs[missing]
            with self.subTest(missing=missing), \
                    self.assertRaises(DrawFingerprintInputError):
                compute_fingerprint(inputs)
        inputs = valid_inputs()
        inputs["extra"] = 1
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint(inputs)
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint({"only": "one"})

    def test_null_field_other_than_preparation_policy_is_an_error(self):
        for name in FIELD_NAMES:
            if name == "preparation_policy":
                continue
            inputs = valid_inputs()
            inputs[name] = None
            with self.subTest(field=name), \
                    self.assertRaises(DrawFingerprintInputError):
                compute_fingerprint(inputs)

    def test_digest_fields_must_be_32_byte_digests(self):
        for name in ("draw_document_sha256", "bundle_manifest_sha256"):
            for bad in (_sha_hex("x"), b"short", "a" * 64, 1234):
                inputs = valid_inputs()
                inputs[name] = bad
                with self.subTest(field=name, bad=repr(bad)), \
                        self.assertRaises(DrawFingerprintInputError):
                    compute_fingerprint(inputs)

    def test_profile_must_be_draw_paper_v1(self):
        inputs = valid_inputs()
        inputs["profile"]["id"] = "eir.v0.2"
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint(inputs)
        inputs = valid_inputs()
        inputs["profile"]["sha256"] = "NOTHEX"
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint(inputs)

    def test_unit_policy_is_mm_only(self):
        inputs = valid_inputs()
        inputs["unit_policy"]["id"] = "cm"
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint(inputs)
        inputs = valid_inputs()
        inputs["unit_policy"]["id"] = "si-mm"
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint(inputs)

    def test_export_parameters_must_be_the_complete_object(self):
        for bad in ({}, [], None, "dxf", 42):
            inputs = valid_inputs()
            inputs["export_parameters"] = bad
            with self.subTest(bad=repr(bad)), \
                    self.assertRaises(DrawFingerprintInputError):
                compute_fingerprint(inputs)

    def test_duplicate_schemas_artifacts_adapters_are_errors(self):
        inputs = valid_inputs()
        inputs["schemas"].append(dict(inputs["schemas"][0]))
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint(inputs)
        inputs = valid_inputs()
        inputs["consumed_artifacts"].append(
            dict(inputs["consumed_artifacts"][0]))
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint(inputs)
        inputs = valid_inputs()
        inputs["adapters"].append(dict(inputs["adapters"][0]))
        with self.assertRaises(DrawFingerprintInputError):
            compute_fingerprint(inputs)

    def test_empty_list_fields_are_errors(self):
        for name in ("schemas", "consumed_artifacts", "adapters"):
            inputs = valid_inputs()
            inputs[name] = []
            with self.subTest(field=name), \
                    self.assertRaises(DrawFingerprintInputError):
                compute_fingerprint(inputs)

    def test_domain_separation_from_ocre_af1(self):
        draw = compute_fingerprint(valid_inputs())
        ocre_inputs = {
            "eir_document_sha256": _sha_bytes("draw-document"),
            "bundle_manifest_sha256": _sha_bytes("bundle-manifest"),
            "profile": {"id": "draw.paper.v1", "version": "0.1.0",
                        "sha256": _sha_hex("draw-paper-schema")},
            "schemas": valid_inputs()["schemas"],
            "consumed_artifacts": valid_inputs()["consumed_artifacts"],
            "execution_parameters": valid_inputs()["export_parameters"],
            "validator": valid_inputs()["validator"],
            "adapters": valid_inputs()["adapters"],
            "unit_policy": valid_inputs()["unit_policy"],
            "preparation_policy": valid_inputs()["preparation_policy"],
        }
        ocre = calculate_approval_fingerprint(ocre_inputs)
        self.assertNotEqual(draw.digest, ocre.digest)
        self.assertNotEqual(draw.encoded, ocre.encoded)
        self.assertTrue(ocre.encoded.startswith(
            b"OCRE:EIR-APPROVAL:FINGERPRINT\x00"))

    def test_parse_encoded_rejects_tampering(self):
        result = compute_fingerprint(valid_inputs())
        with self.assertRaises(DrawFingerprintInputError):
            parse_encoded(result.encoded + b"x")
        with self.assertRaises(DrawFingerprintInputError):
            parse_encoded(result.encoded[: len(DOMAIN) + 5])
        with self.assertRaises(DrawFingerprintInputError):
            parse_encoded(b"OCRE" + result.encoded[len(DOMAIN):])

    def test_parse_encoded_rejects_record_kinds_the_encoder_never_emits(self):
        """A auditoria recusa (field_id, kind, tamanho) impossíveis:
        digests só valem kind 0x01 com 32 bytes, só o field 10 pode ser
        0x00 (e vazio), e todo o resto é 0x03 (JCS)."""
        import struct

        result = compute_fingerprint(valid_inputs())
        records = parse_encoded(result.encoded)

        def forge(overrides):
            blob = DOMAIN
            for f_id, kind, payload in records:
                kind, payload = overrides.get(f_id, (kind, payload))
                blob += struct.pack(">HBQ", f_id, kind, len(payload)) + payload
            return blob

        for bad in (
            {1: (0x03, b'"ab"')},           # digest como JSON
            {1: (0x01, b"\x00" * 10)},      # digest com 10 bytes
            {2: (0x00, b"")},               # field 2 nunca é null
            {5: (0x00, b"")},               # null fora do field 10
            {5: (0x01, _sha_bytes("x"))},   # JCS field como digest
            {10: (0x00, b"abc")},           # null com payload
            {10: (0x01, _sha_bytes("x"))},  # field 10 como digest
            {3: (0x03, b"not json")},       # payload 0x03 não-JSON
            {3: (0x03, b'{"b":1, "a":2}')},  # JSON não-canônico (JCS difere)
            {3: (0x03, b"NaN")},            # extensão Python que JCS não emite
        ):
            with self.subTest(bad=bad), \
                    self.assertRaises(DrawFingerprintInputError):
                parse_encoded(forge(bad))


if __name__ == "__main__":
    unittest.main()
