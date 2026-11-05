"""AI layer scaffolding: the surroundings of the future model.

This package is the *entorno* of the AI layer — the mechanics a model
will plug into when one is chosen. It contains NO model, NO knowledge
base, NO network call and NO training: the model choice and the
knowledge-base source are pending user decisions, and until they land
this package fails closed (nothing AI runs).

Modules:

- ``interfaces`` — the contracts a model must fulfil (``ModelAdapter``,
  ``SemanticClassifier``, ``CoherenceChecker``, ``MaterialAdvisor``)
  and the proposal shapes they return.
- ``context`` — ``AIContext``, the full project context a model
  receives (validated document + sidecar + registered decisions).
- ``registry`` — named model registry; empty by default; fail-closed.
- ``config`` — AI configuration; everything empty by default.
- ``router`` — deterministic-first routing: deterministic software
  first, AI only for what it cannot resolve.
- ``proposals`` — ``Proposal`` + the human-confirmation gate (G3.2):
  the AI proposes, never decides; applying is a separate explicit
  step, only for confirmed proposals.

Nothing in ``draw.paper.v1`` (Fase 0) imports this package.
"""
from __future__ import annotations

from .config import AIConfig, AINotConfiguredError, DEFAULT_CONFIG, require_configured
from .context import AIContext, build_context
from .interfaces import (
    Classification,
    CoherenceChecker,
    Incoherence,
    MaterialAdvice,
    MaterialAdvisor,
    ModelAdapter,
    SemanticClassifier,
)
from .proposals import (
    PROPOSAL_KINDS,
    PROPOSAL_STATES,
    ApplicationResult,
    Proposal,
    apply_proposal,
    confirm,
    new_proposal,
    reject,
)
from .registry import (
    DEFAULT_REGISTRY,
    ModelNotRegisteredError,
    ModelRegistry,
    get_model,
    register_model,
    require_model,
)
from .router import OUTCOMES, RouteResult, route

__all__ = [
    "AIConfig",
    "AIContext",
    "AINotConfiguredError",
    "ApplicationResult",
    "Classification",
    "CoherenceChecker",
    "DEFAULT_CONFIG",
    "DEFAULT_REGISTRY",
    "Incoherence",
    "MaterialAdvice",
    "MaterialAdvisor",
    "ModelAdapter",
    "ModelNotRegisteredError",
    "ModelRegistry",
    "OUTCOMES",
    "PROPOSAL_KINDS",
    "PROPOSAL_STATES",
    "Proposal",
    "RouteResult",
    "SemanticClassifier",
    "apply_proposal",
    "build_context",
    "confirm",
    "get_model",
    "new_proposal",
    "register_model",
    "reject",
    "require_configured",
    "require_model",
    "route",
]
