"""Model registry (scaffolding only — no model is registered by default).

A model becomes usable by one explicit line — ``register_model(name,
adapter)`` — and by nothing else. There is no auto-discovery, no
default model, no environment sniffing: without that line, the
registry is empty and every AI step fails closed (see
``require_model``).

Registering validates that the adapter fulfils the ``ModelAdapter``
contract (a runtime-checkable protocol); anything else is refused.
"""
from __future__ import annotations

from .interfaces import ModelAdapter


class ModelNotRegisteredError(RuntimeError):
    """Raised when AI is asked for but no model is registered."""


class ModelRegistry:
    """A named registry of model adapters. Empty until explicitly filled."""

    def __init__(self) -> None:
        self._models: dict[str, ModelAdapter] = {}

    def register(self, name: str, adapter: ModelAdapter) -> None:
        """Register ``adapter`` under ``name``.

        ``name`` must be a non-empty string and ``adapter`` must fulfil
        the ``ModelAdapter`` protocol; re-registering a name replaces
        the previous adapter (the replacement is explicit, not silent
        — the caller chose to call register again).
        """
        if not isinstance(name, str) or not name:
            raise ValueError("model name must be a non-empty string")
        if not isinstance(adapter, ModelAdapter):
            raise TypeError(
                f"model adapter {adapter!r} does not fulfil the ModelAdapter contract"
            )
        self._models[name] = adapter

    def get(self, name: str) -> ModelAdapter:
        """Return the adapter registered as ``name``; fail closed if none."""
        try:
            return self._models[name]
        except KeyError as exc:
            raise ModelNotRegisteredError(
                f"nenhum modelo registrado com o nome {name!r}; "
                "registre um modelo com register_model() antes de usar a IA"
            ) from exc

    def names(self) -> list[str]:
        """Names currently registered (empty by default)."""
        return sorted(self._models)

    def has(self, name: str) -> bool:
        """True only if ``name`` is registered."""
        return name in self._models


# The single shared registry. Empty until someone registers a model.
DEFAULT_REGISTRY = ModelRegistry()


def register_model(name: str, adapter: ModelAdapter) -> None:
    """Register ``adapter`` under ``name`` in the shared registry."""
    DEFAULT_REGISTRY.register(name, adapter)


def require_model(name: str | None) -> ModelAdapter:
    """Return the model for ``name``; fail closed when no name is given.

    ``None`` means "no model was chosen" — the normal state today —
    and is refused with the same clear error as an unknown name.
    """
    if name is None:
        raise ModelNotRegisteredError(
            "nenhum modelo registrado; a IA não pode rodar sem um modelo "
            "escolhido e registrado com register_model()"
        )
    return DEFAULT_REGISTRY.get(name)
