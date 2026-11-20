from __future__ import annotations

import unittest
import io

import ezdxf

from ocre_core.draw.ai.context import build_context
from ocre_core.draw.ai.interfaces import Incoherence
from ocre_core.draw.ai.observer import observe_conversion
from ocre_core.draw.document import new_document


def doc():
    return new_document({"repo": "test", "commit": "abc", "origin": "upstream-vectorize"})


def valid_dxf():
    stream = io.StringIO()
    ezdxf.new("R2000").write(stream)
    return stream.getvalue().encode()


VALID_STEP = b"""ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('test'),'2;1');\nFILE_NAME('a.step','','',(''),(''),'','');\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"""


class ObserverTests(unittest.TestCase):
    def test_missing_adapter_is_unavailable_for_structurally_valid_dxf(self):
        result = observe_conversion(doc(), None, stage="dxf", artifacts={"drawing.dxf": valid_dxf()})
        self.assertEqual(result["status"], "unavailable")

    def test_independent_structure_error_is_confirmed_and_actionable(self):
        source = doc()
        before = build_context(source).document_sha256
        result = observe_conversion(source, None, stage="step", artifacts={"model.step": b"broken"})
        self.assertEqual(result["status"], "confirmed-error")
        self.assertTrue(result["alerts"])
        self.assertEqual(build_context(source).document_sha256, before)

    def test_confirmed_error_works_with_invalid_document_and_no_provider(self):
        result = observe_conversion({}, None, stage="step", artifacts={"bad.step": b"broken"})
        self.assertEqual(result["status"], "confirmed-error")

    def test_expected_artifact_missing_and_sidecar_ignored(self):
        result = observe_conversion(doc(), None, stage="dxf", artifacts={"report.json": b"SECTION EOF"})
        self.assertEqual(result["status"], "confirmed-error")
        self.assertEqual(result["findings"][0]["kind"], "expected-artifact-missing")

    def test_substring_litter_and_truncated_formats_are_rejected(self):
        bad_dxf = observe_conversion(doc(), None, stage="dxf", artifacts={"bad.dxf": b"junk SECTION EOF junk"})
        bad_step = observe_conversion(doc(), None, stage="step", artifacts={"bad.step": b"ISO-10303-21;END-ISO-10303-21;"})
        self.assertEqual(bad_dxf["status"], "confirmed-error")
        self.assertEqual(bad_step["status"], "confirmed-error")

    def test_model_finding_is_advisory_not_confirmed(self):
        class Adapter:
            def check_coherence(self, context):
                self.context = context
                return [Incoherence("geometry", "maybe wrong")]

        adapter = Adapter()
        result = observe_conversion(doc(), adapter, stage="dxf", artifacts={"a.dxf": valid_dxf()})
        self.assertEqual(result["status"], "observed")
        self.assertEqual(adapter.context.stage, "dxf")
        self.assertGreater(adapter.context.artifact_summaries["a.dxf"]["size"], 11)

    def test_model_response_is_bounded_json_with_evidence_fields(self):
        class Adapter:
            def check_conversion(self, context):
                return [Incoherence("x", "y" * 5000, stroke_id="s-1", evidence="proof")]

        result = observe_conversion(doc(), Adapter(), stage="step", artifacts={"a.step": VALID_STEP})
        finding = result["findings"][0]
        self.assertEqual(finding["stroke_id"], "s-1")
        self.assertEqual(finding["evidence"], "proof")
        self.assertLessEqual(len(finding["detail"]), 1000)

    def test_provider_failure_does_not_break_conversion(self):
        class Adapter:
            def check_conversion(self, context):
                raise RuntimeError("offline")

        result = observe_conversion(doc(), Adapter(), stage="step", artifacts={"a.step": VALID_STEP})
        self.assertEqual(result["status"], "unavailable")

    def test_unknown_stage_refused(self):
        with self.assertRaises(ValueError):
            observe_conversion(doc(), None, stage="semantic", artifacts={})


if __name__ == "__main__":
    unittest.main()
