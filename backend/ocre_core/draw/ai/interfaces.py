"""Contracts for the AI layer (scaffolding only — no model, no implementation).

These are ``typing.Protocol`` definitions on purpose: any object that implements the methods
counts as an adapter, and nothing here calls a network, a library or a
model — there is no model to call. The layer is fail-closed: without a
registered model, nothing AI runs (see ``registry.py``).

Rules these contracts encode (from ``docs/architecture/DRAW_AI_LAYER.md``
and the user's orders):

- Deterministic first: the AI only sees what deterministic software
  could not resolve (see ``router.py``).
- The AI proposes, never decides: every method below returns *proposal
  shapes* (``Classification`` / ``Incoherence`` / ``MaterialAdvice``).
  Turning one into anything in a document is a separate, explicit,
  human-confirmed step (see ``proposals.py``).
- The semantic domain is NOT closed here: ``Classification.label`` is a
  free string because the list of classes (parede, porta, janela, ...)
  is a pending user decision (D4). No label is validated against a
  list — a future knowledge base, when it exists, will own that list.
- No knowledge base is encoded here: there is no wall->brick or
  pillar->concrete mapping anywhere in this package.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .context import AIContext


@dataclass(frozen=True)
class Classification:
    """One semantic class *proposed* for a stroke. Proposed, never decided."""

    stroke_id: str
    label: str
    confidence: float
    rationale: str


@dataclass(frozen=True)
class Incoherence:
    """One incoherence found in a document/file. A finding, not a fix.

    ``kind`` is a free string (e.g. ``"wrong-format"`` for the canonical
    JPEG-vs-PDF example); the checker reports, it never corrects.
    """

    kind: str
    detail: str
    stroke_id: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class MaterialAdvice:
    """One material *proposed* for a stroke. Proposed, never decided."""

    stroke_id: str
    material: str
    rationale: str


@runtime_checkable
class SemanticClassifier(Protocol):
    """Classifies what each stable stroke/ shape is. Proposes only."""

    def classify(self, context: AIContext) -> list[Classification]:
        """Return proposed classes for stable strokes in ``context``.

        Must never propose a class for an erased stroke (tombstones are
        final) and must never write anything into the document.
        """
        ...


@runtime_checkable
class CoherenceChecker(Protocol):
    """Checks whether a document/file is still coherent. Reports only."""

    def check(self, context: AIContext) -> list[Incoherence]:
        """Return the incoherences found in ``context``.

        Must never correct anything: no file is changed, no document
        is mutated, no proposal is applied by this method.
        """
        ...


@runtime_checkable
class MaterialAdvisor(Protocol):
    """Proposes a material for a stroke from the project context."""

    def advise(self, context: AIContext) -> list[MaterialAdvice]:
        """Return proposed materials for stable strokes in ``context``.

        Proposes only — the material never enters a document or a job
        without human confirmation.
        """
        ...


@runtime_checkable
class ModelAdapter(Protocol):
    """The contract any AI model must fulfil to plug into OCRE.

    A model adapter is the union of the three roles: it classifies,
    checks coherence and advises material. A model that cannot do a
    role must return an empty list for it — never raise, never invent.
    """

    name: str

    def classify(self, context: AIContext) -> list[Classification]:
        ...

    def check_coherence(self, context: AIContext) -> list[Incoherence]:
        ...

    def advise_material(self, context: AIContext) -> list[MaterialAdvice]:
        ...
