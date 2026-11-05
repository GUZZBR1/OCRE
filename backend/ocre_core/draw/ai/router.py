"""Deterministic-first router (scaffolding only).

The routing rule is fixed and comes from the user: a conversion is
tried FIRST by deterministic software; only what no deterministic
converter resolves escalates to the AI. The router never calls a
model itself — it only decides where the item goes:

- ``deterministic`` — a deterministic converter resolved it. The AI
  is not consulted at all (and, when a model exists, it is not even
  looked up).
- ``escalated-to-ai`` — no converter resolved it, and a model is
  configured and registered. The caller then uses the registry to get
  the model and turns its answer into a ``Proposal`` (see
  ``proposals.py``); the router itself creates no proposal.
- ``unresolved`` — no converter resolved it and there is no usable
  model (not configured, or not registered). This is the normal state
  today: the item stays unresolved instead of the router inventing an
  answer. Fail-closed, with the reason in ``detail``.

A deterministic converter is a callable ``fn(item) -> result | None``:
a result means "resolved", ``None`` means "I cannot do this". The
``item`` is an opaque dict describing the conversion (e.g. the
canonical example: ``{"kind": "format-conversion", "from": "pdf",
"to": "jpeg"}``) — the router does not interpret it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .config import AIConfig, DEFAULT_CONFIG
from .registry import DEFAULT_REGISTRY, ModelRegistry

OUTCOMES = ("deterministic", "escalated-to-ai", "unresolved")


@dataclass(frozen=True)
class RouteResult:
    """Where an item went, and why."""

    outcome: str  # one of OUTCOMES
    detail: str
    deterministic_result: Any | None = None


def route(
    item: dict[str, Any],
    deterministic_converters: Sequence[Callable[[dict[str, Any]], Any | None]],
    *,
    config: AIConfig | None = None,
    registry: ModelRegistry | None = None,
) -> RouteResult:
    """Route ``item``: deterministic converters first, AI only as fallback."""
    if not isinstance(item, dict):
        raise ValueError("route item must be a dict")
    for converter in deterministic_converters:
        result = converter(item)
        if result is not None:
            return RouteResult(
                outcome="deterministic",
                detail=f"resolved by deterministic converter {getattr(converter, '__name__', converter)!r}",
                deterministic_result=result,
            )
    cfg = config if config is not None else DEFAULT_CONFIG
    reg = registry if registry is not None else DEFAULT_REGISTRY
    if not cfg.is_configured():
        return RouteResult(
            outcome="unresolved",
            detail="no deterministic converter resolved it and the AI layer "
            "is not configured (no model chosen)",
        )
    if not reg.has(cfg.model_name):  # type: ignore[arg-type]
        return RouteResult(
            outcome="unresolved",
            detail=f"no deterministic converter resolved it and the configured "
            f"model {cfg.model_name!r} is not registered",
        )
    return RouteResult(
        outcome="escalated-to-ai",
        detail=f"no deterministic converter resolved it; escalated to "
        f"model {cfg.model_name!r} (the caller invokes it via the registry)",
    )
