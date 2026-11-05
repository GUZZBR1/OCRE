"""Tests for the draw-paper-provenance/0.1 sidecar (ocre_core/draw/sidecar)."""
from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from ocre_core.draw.sidecar import (  # noqa: E402
    DRAW_ORIGINS,
    SIDECAR_VERSION,
    build_draw_sidecar,
    validate_draw_sidecar,
)


def _sha_hex(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def valid_kwargs() -> dict:
    return {
        "doc_id": "doc-0001",
        "document_sha256": _sha_hex("draw-paper-document"),
        "revision": 3,
        "source_artifacts": [
            {"artifact_id": "canvas-png-1",
             "uri": "captures/canvas_1234567890.png",
             "sha256": _sha_hex("canvas-png"),
             "origin": "upstream-canvas-png",
             "artifact_kind": "canvas_png"},
            {"artifact_id": "session-report-1",
             "uri": "session-reports/session_1.json",
             "sha256": _sha_hex("session-report"),
             "origin": "upstream-canvas-png",
             "artifact_kind": "session_report"},
        ],
        "derived_artifacts": [
            {"artifact_id": "dxf-r2000-1", "uri": "exports/doc-0001.dxf",
             "sha256": _sha_hex("dxf-output"), "format": "dxf-r2000"},
        ],
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
        "export_parameters": {
            "target": "dxf-r2000",
            "order_kind": "reading-centroid",
            "paper_effective_mm": [210.0, 296.47],
            "include_erased": False,
        },
        "fingerprint_digest": _sha_hex("draw-af1-digest"),
        "draw_source": {"repo": "santleme/draw",
                        "commit": "8a17115" + "0" * 33},
    }


class BuildSidecarTests(unittest.TestCase):
    def test_valid_sidecar_carries_document_hash_and_version(self):
        sidecar = build_draw_sidecar(**valid_kwargs())
        self.assertEqual(sidecar["sidecar_version"], SIDECAR_VERSION)
        self.assertEqual(sidecar["sidecar_version"],
                         "draw-paper-provenance/0.1")
        self.assertNotEqual(sidecar["sidecar_version"],
                            "cad-mesh-provenance/0.2")
        # The main document hash lives inside the sidecar for reassociation.
        self.assertEqual(sidecar["document_sha256"],
                         _sha_hex("draw-paper-document"))
        self.assertEqual(sidecar["doc_id"], "doc-0001")
        self.assertEqual(sidecar["revision"], 3)
        self.assertEqual(sidecar["fingerprint"],
                         {"algorithm": "DRAW-AF/1",
                          "digest": _sha_hex("draw-af1-digest")})
        self.assertEqual(sidecar["draw_source"],
                         {"repo": "santleme/draw",
                          "commit": "8a17115" + "0" * 33})
        self.assertEqual(validate_draw_sidecar(sidecar), [])

    def test_all_five_origins_are_accepted(self):
        for origin in sorted(DRAW_ORIGINS):
            kwargs = valid_kwargs()
            kwargs["source_artifacts"] = [
                {"artifact_id": "src-1", "sha256": _sha_hex("src"),
                 "origin": origin, "artifact_kind": "paper_scan"},
            ]
            sidecar = build_draw_sidecar(**kwargs)
            self.assertEqual(sidecar["source_artifacts"][0]["origin"], origin)
        self.assertEqual(DRAW_ORIGINS, {
            "real_paper", "synthetic", "upstream-vectorize",
            "upstream-canvas-png", "fork-session-v2",
        })

    def test_unknown_or_missing_origin_is_an_error_never_a_default(self):
        for bad in ("synthetic_geo", "real-paper", "REAL_PAPER", "", None, 42):
            kwargs = valid_kwargs()
            kwargs["source_artifacts"] = [
                {"artifact_id": "src-1", "sha256": _sha_hex("src"),
                 "origin": bad, "artifact_kind": "paper_scan"},
            ]
            with self.subTest(origin=repr(bad)), self.assertRaises(ValueError):
                build_draw_sidecar(**kwargs)
        # Omitting the key entirely must also fail, not default.
        kwargs = valid_kwargs()
        del kwargs["source_artifacts"][0]["origin"]
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)

    def test_no_derivation_yet_is_allowed_but_derived_needs_adapter(self):
        kwargs = valid_kwargs()
        kwargs["derived_artifacts"] = []
        kwargs["adapters"] = []
        kwargs["fingerprint_digest"] = None
        kwargs["draw_source"] = None
        sidecar = build_draw_sidecar(**kwargs)
        self.assertEqual(sidecar["derived_artifacts"], [])
        self.assertEqual(sidecar["adapters"], [])
        self.assertIsNone(sidecar["fingerprint"])
        self.assertIsNone(sidecar["draw_source"])
        kwargs = valid_kwargs()
        kwargs["adapters"] = []
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)

    def test_bad_document_hash_doc_id_and_revision_are_errors(self):
        for kwargs_patch in (
            {"document_sha256": "NOTHEX"},
            {"document_sha256": _sha_hex("x").upper()},
            {"document_sha256": _sha_hex("x")[:63]},
            {"doc_id": ""},
            {"doc_id": None},
            {"revision": -1},
            {"revision": True},
            {"revision": "3"},
        ):
            kwargs = valid_kwargs()
            kwargs.update(kwargs_patch)
            with self.subTest(patch=kwargs_patch), \
                    self.assertRaises(ValueError):
                build_draw_sidecar(**kwargs)

    def test_duplicate_artifact_ids_and_bad_adapter_pins_are_errors(self):
        kwargs = valid_kwargs()
        kwargs["source_artifacts"].append(dict(kwargs["source_artifacts"][0]))
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)
        kwargs = valid_kwargs()
        kwargs["derived_artifacts"].append(
            dict(kwargs["derived_artifacts"][0]))
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)
        kwargs = valid_kwargs()
        kwargs["adapters"][0]["binary_sha256"] = "zzz"
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)
        kwargs = valid_kwargs()
        del kwargs["adapters"][0]["capabilities_sha256"]
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)

    def test_bad_fingerprint_and_draw_source_are_errors(self):
        kwargs = valid_kwargs()
        kwargs["fingerprint_digest"] = "not-a-hash"
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)
        kwargs = valid_kwargs()
        kwargs["draw_source"] = {"repo": "santleme/draw",
                                 "commit": "short"}
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)

    def test_inputs_are_not_mutated_and_sidecar_is_a_copy(self):
        kwargs = valid_kwargs()
        before = copy.deepcopy(kwargs)
        sidecar = build_draw_sidecar(**kwargs)
        self.assertEqual(kwargs, before)
        sidecar["source_artifacts"][0]["sha256"] = "tampered"
        self.assertEqual(kwargs["source_artifacts"][0]["sha256"],
                         _sha_hex("canvas-png"))

    def test_empty_source_list_is_an_error(self):
        kwargs = valid_kwargs()
        kwargs["source_artifacts"] = []
        with self.assertRaises(ValueError):
            build_draw_sidecar(**kwargs)


class ValidateSidecarTests(unittest.TestCase):
    def test_validate_accepts_valid_and_rejects_tampering(self):
        sidecar = build_draw_sidecar(**valid_kwargs())
        self.assertEqual(validate_draw_sidecar(sidecar), [])

        tampered = copy.deepcopy(sidecar)
        tampered["sidecar_version"] = "cad-mesh-provenance/0.2"
        self.assertTrue(validate_draw_sidecar(tampered))

        tampered = copy.deepcopy(sidecar)
        tampered["source_artifacts"][0]["origin"] = "synthetic_geo"
        issues = validate_draw_sidecar(tampered)
        self.assertTrue(any("origin" in issue for issue in issues))

        tampered = copy.deepcopy(sidecar)
        tampered["document_sha256"] = _sha_hex("another-document")
        # A different (but well-formed) hash still validates structurally:
        # reassociation is the caller's job, the hash format is ours.
        self.assertEqual(validate_draw_sidecar(tampered), [])

        tampered = copy.deepcopy(sidecar)
        tampered["extra_field"] = True
        self.assertTrue(validate_draw_sidecar(tampered))

    def test_validate_rejects_non_dict(self):
        self.assertTrue(validate_draw_sidecar(None))
        self.assertTrue(validate_draw_sidecar([]))
        self.assertTrue(validate_draw_sidecar("sidecar"))


if __name__ == "__main__":
    unittest.main()
