"""Tests for the AI layer scaffolding (entorno only — no model).

The package ``ocre_core/draw/ai/`` is mechanics, not a model: interfaces,
registry, context, deterministic-first router, proposal gate and config.
No model is registered by default, no knowledge base is encoded, no
network is used. These tests prove the scaffolding fails closed without
a model and that the human-confirmation gate (G3.2) is structural:
proposals are born pending, applying without confirmation is refused,
and the coherence checker reports without correcting anything.
"""
from __future__ import annotations

import unittest

from ocre_core.draw.ai import (
    AIConfig,
    AINotConfiguredError,
    ApplicationResult,
    Classification,
    Incoherence,
    ModelNotRegisteredError,
    ModelRegistry,
    apply_proposal,
    build_context,
    confirm,
    new_proposal,
    reject,
    require_configured,
    require_model,
    route,
)
from ocre_core.draw.ai.interfaces import (
    CoherenceChecker,
    MaterialAdvice,
    ModelAdapter,
    SemanticClassifier,
)
from ocre_core.draw.document import (
    add_stroke,
    document_sha256,
    erase_stroke,
    new_document,
    stabilize_stroke,
)

SOURCE = {"repo": "santleme/draw", "commit": "8a17115", "origin": "upstream-vectorize"}


def make_document():
    """A document at revision 4: one stable stroke, one pending, one erased."""
    doc = new_document(SOURCE)
    s1 = add_stroke(doc, [[10.0, 10.0], [100.0, 10.0]])
    stabilize_stroke(doc, s1["stroke_id"])
    add_stroke(doc, [[10.0, 20.0], [100.0, 20.0]])
    s3 = add_stroke(doc, [[10.0, 30.0], [100.0, 30.0]])
    erase_stroke(doc, s3["stroke_id"], reason="hand-erase", at_ms=100)
    return doc


class FakeModel:
    """A test-only adapter fulfilling the ModelAdapter contract.

    Counts calls so tests can prove the router never invokes it when
    deterministic software resolves the item. Proposes nothing real:
    the labels here are test fixtures, not a knowledge base.
    """

    name = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, context):
        self.calls += 1
        return [
            Classification(
                stroke_id=s["stroke_id"],
                label="parede",
                confidence=0.9,
                rationale="test fixture",
            )
            for s in context.stable_strokes()
        ]

    def check_coherence(self, context):
        self.calls += 1
        return [Incoherence(kind="wrong-format", detail="test fixture")]

    def advise_material(self, context):
        self.calls += 1
        return [
            MaterialAdvice(
                stroke_id=s["stroke_id"],
                material="concreto",
                rationale="test fixture",
            )
            for s in context.stable_strokes()
        ]


class RegistryTests(unittest.TestCase):
    def test_empty_registry_fails_closed(self):
        registry = ModelRegistry()
        self.assertEqual(registry.names(), [])
        self.assertFalse(registry.has("qualquer"))
        with self.assertRaises(ModelNotRegisteredError):
            registry.get("qualquer")
        with self.assertRaises(ModelNotRegisteredError):
            require_model(None)

    def test_register_and_get(self):
        registry = ModelRegistry()
        model = FakeModel()
        registry.register("fake", model)
        self.assertEqual(registry.names(), ["fake"])
        self.assertTrue(registry.has("fake"))
        self.assertIs(registry.get("fake"), model)

    def test_registering_a_non_adapter_is_refused(self):
        registry = ModelRegistry()
        with self.assertRaises(TypeError):
            registry.register("nao-modelo", object())

    def test_registering_an_empty_name_is_refused(self):
        registry = ModelRegistry()
        with self.assertRaises(ValueError):
            registry.register("", FakeModel())


class ConfigTests(unittest.TestCase):
    def test_default_config_is_empty_and_not_configured(self):
        cfg = AIConfig()
        self.assertIsNone(cfg.model_name)
        self.assertIsNone(cfg.endpoint)
        self.assertIsNone(cfg.api_key_env)
        self.assertIsNone(cfg.knowledge_base_version)
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.is_configured())
        with self.assertRaises(AINotConfiguredError):
            require_configured(cfg)
        with self.assertRaises(AINotConfiguredError):
            require_configured()

    def test_configured_only_with_model_and_enabled(self):
        self.assertFalse(AIConfig(model_name="x").is_configured())
        self.assertFalse(AIConfig(enabled=True).is_configured())
        self.assertTrue(AIConfig(model_name="x", enabled=True).is_configured())
        self.assertIs(
            require_configured(AIConfig(model_name="x", enabled=True)).model_name, "x"
        )


class ContextTests(unittest.TestCase):
    def test_build_context_packs_the_document(self):
        doc = make_document()
        ctx = build_context(doc, decisions=({"decision": "D3"},))
        self.assertEqual(ctx.revision, doc["revision"])
        self.assertEqual(ctx.paper_format, "A4")
        self.assertEqual(ctx.document_sha256, document_sha256(doc))
        self.assertEqual(ctx.decisions, ({"decision": "D3"},))

    def test_context_deep_copies_the_document(self):
        doc = make_document()
        ctx = build_context(doc)
        ctx.document["strokes"][0]["points_mm"][0][0] = 999.0
        self.assertEqual(doc["strokes"][0]["points_mm"][0][0], 10.0)

    def test_only_stable_strokes_are_offered_for_classification(self):
        doc = make_document()
        ctx = build_context(doc)
        stable = ctx.stable_strokes()
        self.assertEqual(len(stable), 1)
        self.assertEqual(stable[0]["status"], "stable")
        live = ctx.live_strokes()
        self.assertEqual(len(live), 2)  # stable + pending; erased excluded

    def test_document_without_revision_or_paper_is_refused(self):
        with self.assertRaises(ValueError):
            build_context({"strokes": []})
        with self.assertRaises(ValueError):
            build_context({"revision": 1, "strokes": []})


class RouterTests(unittest.TestCase):
    ITEM = {"kind": "format-conversion", "from": "pdf", "to": "jpeg"}

    def test_deterministic_converter_wins_and_model_is_never_called(self):
        model = FakeModel()
        registry = ModelRegistry()
        registry.register("fake", model)
        config = AIConfig(model_name="fake", enabled=True)

        def pdf_to_jpeg(item):
            if item.get("from") == "pdf" and item.get("to") == "jpeg":
                return "converted-by-software"
            return None

        result = route(self.ITEM, [pdf_to_jpeg], config=config, registry=registry)
        self.assertEqual(result.outcome, "deterministic")
        self.assertEqual(result.deterministic_result, "converted-by-software")
        self.assertEqual(model.calls, 0)

    def test_no_converter_and_no_model_is_unresolved(self):
        result = route(self.ITEM, [lambda item: None])
        self.assertEqual(result.outcome, "unresolved")
        self.assertIn("not configured", result.detail)

    def test_no_converter_with_model_escalates_to_ai(self):
        model = FakeModel()
        registry = ModelRegistry()
        registry.register("fake", model)
        config = AIConfig(model_name="fake", enabled=True)
        result = route(self.ITEM, [lambda item: None], config=config, registry=registry)
        self.assertEqual(result.outcome, "escalated-to-ai")
        self.assertIn("fake", result.detail)
        # The router decides only: it never invokes the model itself.
        self.assertEqual(model.calls, 0)

    def test_configured_but_unregistered_model_is_unresolved(self):
        registry = ModelRegistry()
        config = AIConfig(model_name="fantasma", enabled=True)
        result = route(self.ITEM, [lambda item: None], config=config, registry=registry)
        self.assertEqual(result.outcome, "unresolved")

    def test_first_converter_that_can_do_it_wins(self):
        calls = []

        def first(item):
            calls.append("first")
            return None

        def second(item):
            calls.append("second")
            return "done"

        result = route(self.ITEM, [first, second])
        self.assertEqual(result.outcome, "deterministic")
        self.assertEqual(calls, ["first", "second"])


class ProposalTests(unittest.TestCase):
    def test_proposals_are_born_pending(self):
        doc = make_document()
        proposal = new_proposal(
            kind="semantic-classification",
            content={"stroke_id": "s-0001", "label": "parede"},
            document_revision=doc["revision"],
            document_sha256="abc",
        )
        self.assertEqual(proposal.state, "pending")
        self.assertTrue(proposal.is_pending())
        self.assertEqual(proposal.origin, "ai")

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            new_proposal(
                kind="decidir-sozinho",
                content={},
                document_revision=1,
                document_sha256="abc",
            )

    def test_confirm_then_apply_is_a_separate_explicit_step(self):
        doc = make_document()
        proposal = new_proposal(
            kind="semantic-classification",
            content={"stroke_id": "s-0001", "label": "parede"},
            document_revision=doc["revision"],
            document_sha256=document_sha256(doc),
        )
        confirmed = confirm(proposal, by="humano-1")
        self.assertEqual(confirmed.state, "confirmed")
        self.assertEqual(confirmed.decided_by, "humano-1")
        self.assertIsNotNone(confirmed.decided_at)
        # The original record is untouched: immutability, no silent rewrite.
        self.assertEqual(proposal.state, "pending")
        result = apply_proposal(confirmed, doc)
        self.assertIsInstance(result, ApplicationResult)
        self.assertEqual(result.proposal_id, confirmed.proposal_id)
        self.assertEqual(result.applied_to_revision, doc["revision"])

    def test_records_own_their_content_caller_and_decided_edits_do_not_leak(self):
        doc = make_document()
        content = {"stroke_id": "s-0001", "labels": ["parede"]}
        proposal = new_proposal(
            kind="semantic-classification",
            content=content,
            document_revision=doc["revision"],
            document_sha256="abc",
        )
        content["labels"].append("muro")
        content["extra"] = 1
        self.assertEqual(proposal.content, {"stroke_id": "s-0001", "labels": ["parede"]})
        for decided in (confirm(proposal, by="humano-1"), reject(proposal, by="humano-2")):
            decided.content["injected"] = True
            decided.content["labels"].append("muro")
        self.assertEqual(proposal.content, {"stroke_id": "s-0001", "labels": ["parede"]})

    def test_apply_without_confirmation_is_refused(self):
        doc = make_document()
        proposal = new_proposal(
            kind="material-advice",
            content={"stroke_id": "s-0001", "material": "concreto"},
            document_revision=doc["revision"],
            document_sha256="abc",
        )
        with self.assertRaises(ValueError):
            apply_proposal(proposal, doc)

    def test_reject_then_apply_is_refused_and_reject_is_final(self):
        doc = make_document()
        proposal = new_proposal(
            kind="coherence-correction",
            content={"kind": "wrong-format"},
            document_revision=doc["revision"],
            document_sha256="abc",
        )
        rejected = reject(proposal, by="humano-1")
        self.assertEqual(rejected.state, "rejected")
        with self.assertRaises(ValueError):
            apply_proposal(rejected, doc)
        with self.assertRaises(ValueError):
            confirm(rejected, by="humano-1")

    def test_apply_against_a_stale_revision_is_refused(self):
        doc = make_document()
        proposal = new_proposal(
            kind="semantic-classification",
            content={"stroke_id": "s-0001", "label": "parede"},
            document_revision=doc["revision"],
            document_sha256="abc",
        )
        confirmed = confirm(proposal, by="humano-1")
        add_stroke(doc, [[10.0, 40.0], [100.0, 40.0]])  # revision +1
        with self.assertRaises(ValueError):
            apply_proposal(confirmed, doc)

    def test_apply_against_another_document_same_revision_is_refused(self):
        # A proposta se liga ao documento por sha256 — a mesma revision
        # em OUTRO documento não aplica (a revision sozinha não basta).
        doc_a = make_document()
        doc_b = make_document()
        self.assertEqual(doc_a["revision"], doc_b["revision"])
        self.assertNotEqual(document_sha256(doc_a), document_sha256(doc_b))
        proposal = new_proposal(
            kind="semantic-classification",
            content={"stroke_id": "s-0001", "label": "parede"},
            document_revision=doc_a["revision"],
            document_sha256=document_sha256(doc_a),
        )
        confirmed = confirm(proposal, by="humano-1")
        apply_proposal(confirmed, doc_a)  # documento certo aplica
        with self.assertRaises(ValueError):
            apply_proposal(confirmed, doc_b)

    def test_apply_does_not_mutate_the_document(self):
        doc = make_document()
        before = dict(doc)
        proposal = new_proposal(
            kind="semantic-classification",
            content={"stroke_id": "s-0001", "label": "parede"},
            document_revision=doc["revision"],
            document_sha256=document_sha256(doc),
        )
        apply_proposal(confirm(proposal, by="humano-1"), doc)
        self.assertEqual(doc, before)


class CoherenceCheckerTests(unittest.TestCase):
    def test_checker_reports_without_correcting_anything(self):
        doc = make_document()
        before = build_context(doc).document

        class ReportingChecker:
            def check(self, context):
                return [
                    Incoherence(
                        kind="wrong-format",
                        detail="o arquivo devia ser JPEG e veio PDF",
                        evidence="test fixture",
                    )
                ]

        checker = ReportingChecker()
        self.assertIsInstance(checker, CoherenceChecker)
        findings = checker.check(build_context(doc))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "wrong-format")
        # Nothing was corrected: the document is byte-identical.
        self.assertEqual(build_context(doc).document, before)

    def test_fake_model_fulfils_the_contracts(self):
        model = FakeModel()
        self.assertIsInstance(model, ModelAdapter)
        self.assertIsInstance(model, SemanticClassifier)
        ctx = build_context(make_document())
        self.assertEqual(len(model.classify(ctx)), 1)
        self.assertEqual(len(model.advise_material(ctx)), 1)
        self.assertEqual(len(model.check_coherence(ctx)), 1)


if __name__ == "__main__":
    unittest.main()
