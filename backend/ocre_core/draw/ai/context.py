"""The project context the AI receives (scaffolding only).

``AIContext`` is the carrier of the "full project context": everything
a future model needs to propose semantics or check coherence, packed
from data that already exists. It invents nothing — it only packages
the validated ``draw.paper.v1`` document, its sidecar/provenance and
the decisions already registered.

The document is deep-copied on entry: the AI must never be able to
mutate the master document through the context. Only stable strokes
are exposed for classification; erased strokes (tombstones) are final
and are never offered to the AI.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..document import document_sha256, live_strokes


@dataclass(frozen=True)
class AIContext:
    """Everything a model receives for one document revision."""

    document: dict[str, Any]
    document_sha256: str
    revision: int
    paper_format: str
    sidecar: dict[str, Any] | None = None
    decisions: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def stable_strokes(self) -> list[dict[str, Any]]:
        """Stable strokes only — the only ones a model may classify.

        ``pending`` strokes are not offered either: a model classifies
        what is confirmed, not what is still being drawn.
        """
        return [s for s in live_strokes(self.document) if s.get("status") == "stable"]

    def live_strokes(self) -> list[dict[str, Any]]:
        """All non-erased strokes (pending + stable)."""
        return live_strokes(self.document)


def build_context(
    document: dict[str, Any],
    *,
    sidecar: dict[str, Any] | None = None,
    decisions: tuple[dict[str, Any], ...] = (),
) -> AIContext:
    """Pack ``document`` (deep-copied) + sidecar + decisions into a context.

    ``revision`` and ``paper_format`` are read from the document itself.
    A document without a ``revision`` or without a paper block is
    refused — the AI only ever sees a validated document.
    """
    if not isinstance(document, dict):
        raise ValueError("AI context requires a draw.paper.v1 document (dict)")
    revision = document.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ValueError("document has no integer revision")
    paper = document.get("paper")
    if not isinstance(paper, dict) or not isinstance(paper.get("format"), str):
        raise ValueError("document has no paper block with a format")
    return AIContext(
        document=copy.deepcopy(document),
        document_sha256=document_sha256(document),
        revision=revision,
        paper_format=paper["format"],
        sidecar=copy.deepcopy(sidecar) if sidecar is not None else None,
        decisions=tuple(copy.deepcopy(d) for d in decisions),
    )
