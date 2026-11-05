"""AI proposals (scaffolding only — the human-confirmation gate).

Every AI output that could change anything is a ``Proposal``: a record
with an explicit state — ``pending`` | ``confirmed`` | ``rejected`` —
and the document revision it refers to. The gate (G3.2) is structural:

- A proposal is born ``pending`` and stays pending until a human acts.
- ``confirm`` / ``reject`` return a NEW proposal with the new state;
  they record who decided and when. There is no path from
  ``pending`` to ``confirmed`` without a recorded human act.
- ``apply_proposal`` is a separate, explicit step: it refuses anything
  that is not ``confirmed``, and refuses a proposal whose document
  revision no longer matches (the document moved on — the proposal
  refers to the past). It returns an ``ApplicationResult`` record and
  does NOT mutate the document: there is no agreed document field for
  semantic classes yet (decision D6 pending), so nothing is written
  into ``draw.paper.v1`` by this layer. When D6 lands, the application
  step is where the write happens — still only for confirmed proposals.

Automatic correction without human confirmation is PROHIBITED until
a correction protocol exists (gate G3.2); this module contains no
such path.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

PROPOSAL_KINDS = (
    "semantic-classification",
    "coherence-correction",
    "material-advice",
)
PROPOSAL_STATES = ("pending", "confirmed", "rejected")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Proposal:
    """One AI proposal. Immutable: state changes make a new record."""

    proposal_id: str
    origin: str  # "ai" — only the AI proposes through this module
    kind: str  # one of PROPOSAL_KINDS
    content: dict[str, Any]
    document_revision: int
    document_sha256: str
    state: str = "pending"  # pending | confirmed | rejected
    created_at: str = field(default_factory=_utc_now)
    decided_by: str | None = None
    decided_at: str | None = None

    def is_pending(self) -> bool:
        return self.state == "pending"

    def is_confirmed(self) -> bool:
        return self.state == "confirmed"

    def is_rejected(self) -> bool:
        return self.state == "rejected"


@dataclass(frozen=True)
class ApplicationResult:
    """Record of a proposal application. A record — not a document write."""

    proposal_id: str
    kind: str
    applied_to_revision: int
    applied_at: str = field(default_factory=_utc_now)
    note: str = (
        "recorded only: no document field exists yet for this kind "
        "(decision D6 pending), so nothing was written into draw.paper.v1"
    )


def new_proposal(
    *,
    kind: str,
    content: dict[str, Any],
    document_revision: int,
    document_sha256: str,
    proposal_id: str | None = None,
) -> Proposal:
    """Create a proposal in state ``pending``. Proposals are born pending."""
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"unknown proposal kind: {kind!r}")
    if not isinstance(document_revision, int) or isinstance(document_revision, bool):
        raise ValueError("document_revision must be an integer")
    if not isinstance(document_sha256, str) or not document_sha256:
        raise ValueError("document_sha256 must be a non-empty string")
    return Proposal(
        proposal_id=proposal_id or str(uuid.uuid4()),
        origin="ai",
        kind=kind,
        content=dict(content),
        document_revision=document_revision,
        document_sha256=document_sha256,
    )


def confirm(proposal: Proposal, *, by: str) -> Proposal:
    """Human confirmation: pending -> confirmed, recording who and when.

    Refuses anything that is not pending — a rejected proposal can
    never be confirmed afterwards.
    """
    if not isinstance(by, str) or not by:
        raise ValueError("confirmation requires a non-empty human identifier")
    if proposal.state != "pending":
        raise ValueError(
            f"cannot confirm proposal {proposal.proposal_id} in state {proposal.state!r}"
        )
    return replace(proposal, state="confirmed", decided_by=by, decided_at=_utc_now())


def reject(proposal: Proposal, *, by: str) -> Proposal:
    """Human rejection: pending -> rejected, recording who and when."""
    if not isinstance(by, str) or not by:
        raise ValueError("rejection requires a non-empty human identifier")
    if proposal.state != "pending":
        raise ValueError(
            f"cannot reject proposal {proposal.proposal_id} in state {proposal.state!r}"
        )
    return replace(proposal, state="rejected", decided_by=by, decided_at=_utc_now())


def apply_proposal(proposal: Proposal, document: dict[str, Any]) -> ApplicationResult:
    """Apply a CONFIRMED proposal — a separate, explicit step.

    Refuses (fail-closed) when the proposal is not confirmed, and when
    the document's revision no longer matches the proposal's revision.
    Returns an application record; it does NOT mutate the document
    (no agreed document field exists yet for proposal kinds — D6).
    """
    if proposal.state != "confirmed":
        raise ValueError(
            f"cannot apply proposal {proposal.proposal_id} in state "
            f"{proposal.state!r}: only confirmed proposals apply"
        )
    revision = document.get("revision") if isinstance(document, dict) else None
    if revision != proposal.document_revision:
        raise ValueError(
            f"proposal {proposal.proposal_id} refers to revision "
            f"{proposal.document_revision}, but the document is at "
            f"revision {revision!r}: re-propose against the current revision"
        )
    return ApplicationResult(
        proposal_id=proposal.proposal_id,
        kind=proposal.kind,
        applied_to_revision=proposal.document_revision,
    )
