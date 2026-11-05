"""AI configuration (scaffolding only — everything empty by default).

``AIConfig`` holds what a future model needs to run: which model, where
it lives, which knowledge-base version its proposals come from. Today
all of it is empty: no model is chosen (that decision is pending with
the user), no endpoint is set, no knowledge base exists.

Rules:

- The key itself is NEVER stored here: ``api_key_env`` names only the
  environment variable that would hold it, and only for a future
  off-the-shelf route. No credential is read, stored or logged.
- ``enabled`` is False by default and ``is_configured`` is False until
  a model name is set. ``require_configured`` refuses to run AI steps
  otherwise — fail-closed, with a clear error.
"""
from __future__ import annotations

from dataclasses import dataclass


class AINotConfiguredError(RuntimeError):
    """Raised when an AI step is asked for but the AI is not configured."""


@dataclass(frozen=True)
class AIConfig:
    """Configuration of the AI layer. All empty until a model is chosen."""

    model_name: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    knowledge_base_version: str | None = None
    enabled: bool = False

    def is_configured(self) -> bool:
        """True only when a model is named and the layer is enabled."""
        return self.enabled and self.model_name is not None


DEFAULT_CONFIG = AIConfig()


def require_configured(config: AIConfig | None = None) -> AIConfig:
    """Return ``config`` (or the default) if configured; fail closed otherwise."""
    cfg = config if config is not None else DEFAULT_CONFIG
    if not cfg.is_configured():
        raise AINotConfiguredError(
            "a camada de IA não está configurada: nenhum modelo escolhido "
            "(model_name) e/ou a camada não está habilitada (enabled). "
            "Sem modelo, a IA não roda — o sistema segue só com o "
            "software determinístico."
        )
    return cfg
