import unittest

from ocre_core.draw.ai.interfaces import Classification
from ocre_core.draw.ai.proposals import confirm
from ocre_core.draw.ai.semantics import analyze, material_for_step
from ocre_core.draw.document import add_stroke, new_document, stabilize_stroke


class SemanticsTests(unittest.TestCase):
    def setUp(self):
        self.doc = new_document({"repo": "test", "commit": "abc", "origin": "upstream-vectorize"})
        stroke = add_stroke(self.doc, [[0, 0], [10, 0], [10, 10]], closed=True)
        self.sid = stroke["stroke_id"]
        stabilize_stroke(self.doc, self.sid)

    def test_known_stable_class_becomes_pending_proposal(self):
        sid = self.sid
        class Adapter:
            def classify(self, context):
                return [Classification(sid, "bar", .9, "shape")]
        proposals = analyze(self.doc, Adapter())
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0].content["label"], "bar")
        self.assertEqual(proposals[0].state, "pending")

    def test_unknown_class_abstains_and_explicit_material_wins(self):
        sid = self.sid
        class Adapter:
            def classify(self, context):
                return [Classification(sid, "beam", .99, "uncertain")]
        proposals = analyze(self.doc, Adapter(), explicit_material="steel")
        self.assertEqual(proposals, [])
        self.assertEqual(material_for_step(self.doc, None, explicit_material="aluminum"), "aluminum")

    def test_e2e_catalog_material_without_initial_choice_requires_approval(self):
        sid = self.sid
        class Adapter:
            def classify(self, context):
                return [Classification(sid, "plate", .95, "closed profile")]
            def advise_material(self, context):
                from ocre_core.draw.ai.interfaces import MaterialAdvice
                return [MaterialAdvice(sid, "steel", "adapter candidate")]
        proposals = analyze(self.doc, Adapter())
        material = next(p for p in proposals if p.kind == "material-advice")
        self.assertIsNone(material_for_step(self.doc, material))
        approved = confirm(material, by="reviewer")
        self.assertEqual(material_for_step(self.doc, approved), "steel")
        from ocre_core.draw.document import add_stroke
        add_stroke(self.doc, [[1, 1], [2, 2]])
        self.assertIsNone(material_for_step(self.doc, approved))

    def test_catalog_fallback_requires_unambiguous_classification(self):
        sid = self.sid
        class Adapter:
            def classify(self, context):
                return [Classification(sid, "plate", .95, "clear class")]
            def advise_material(self, context):
                return []
        proposals = analyze(self.doc, Adapter())
        material = next(item for item in proposals if item.kind == "material-advice")
        self.assertEqual(material.content["source"], "catalog")
        self.assertEqual(material.content["material"], "steel")
        self.assertEqual(material.content["stroke_ids"], [sid])

        class Ambiguous(Adapter):
            def classify(self, context):
                return [
                    Classification(sid, "plate", .95, "first"),
                    Classification(sid, "bar", .95, "conflict"),
                ]
        self.assertFalse(any(
            item.kind == "material-advice" for item in analyze(self.doc, Ambiguous())
        ))

    def test_malformed_confirmed_payload_returns_none(self):
        from dataclasses import replace
        sid = self.sid
        class Adapter:
            def classify(self, context):
                return [Classification(sid, "plate", .95, "closed profile")]
            def advise_material(self, context):
                from ocre_core.draw.ai.interfaces import MaterialAdvice
                return [MaterialAdvice(sid, "steel", "clear")]
        proposal = next(p for p in analyze(self.doc, Adapter()) if p.kind == "material-advice")
        approved = confirm(proposal, by="reviewer")
        malformed = replace(approved, content={**approved.content, "stroke_ids": [None]})
        self.assertIsNone(material_for_step(self.doc, malformed))


if __name__ == "__main__":
    unittest.main()
