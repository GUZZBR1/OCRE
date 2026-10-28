"""Pure domain contracts for immutable project-revision synchronization.

This module deliberately knows nothing about CAD operation semantics or storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, TypeAlias


@dataclass(frozen=True)
class SyncConflict:
    project_id: str
    batch_id: str
    expected_base_revision_id: Optional[str]
    current_head_revision_id: Optional[str]


@dataclass(frozen=True)
class SyncOperation:
    """One ordered, uninterpreted client operation."""

    operation_id: str
    payload: bytes

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("operation_id must not be empty")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload must be opaque bytes")


@dataclass(frozen=True)
class SyncBatch:
    """Immutable batch envelope; operation order is significant."""

    project_id: str
    batch_id: str
    actor_id: str
    base_revision_id: Optional[str]
    operations: tuple[SyncOperation, ...]

    def __post_init__(self) -> None:
        for name in ("project_id", "batch_id", "actor_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.operations, tuple):
            raise TypeError("operations must be a tuple")
        if any(not isinstance(op, SyncOperation) for op in self.operations):
            raise TypeError("operations must contain SyncOperation values")


@dataclass(frozen=True)
class BatchReceipt:
    """Previously committed result stored by the persistence adapter."""

    batch: SyncBatch
    committed_revision_id: str

    def __post_init__(self) -> None:
        if not self.committed_revision_id:
            raise ValueError("committed_revision_id must not be empty")


@dataclass(frozen=True)
class SyncReady:
    """Batch may be submitted for an atomic append; it is not yet committed."""

    batch: SyncBatch
    expected_head_revision_id: Optional[str]


@dataclass(frozen=True)
class SyncReplay:
    """Exact repeated batch; return its stored receipt without appending again."""

    receipt: BatchReceipt


@dataclass(frozen=True)
class SyncBatchIdentityConflict:
    """A batch identity was reused for a different envelope."""

    project_id: str
    batch_id: str


SyncDecision: TypeAlias = SyncReady | SyncReplay | SyncConflict | SyncBatchIdentityConflict


def check_sync_base(
    *,
    project_id: str,
    batch_id: str,
    base_revision_id: Optional[str],
    current_head_revision_id: Optional[str],
) -> SyncConflict | None:
    """Return a conflict unless the client's base is still the current head.

    ``None`` means the batch may proceed to a transactional append. This check
    does not merge operations, persist them, or guarantee acceptance by itself;
    the storage layer must repeat the comparison atomically when committing.
    """
    if not project_id:
        raise ValueError("project_id must not be empty")
    if not batch_id:
        raise ValueError("batch_id must not be empty")

    if base_revision_id == current_head_revision_id:
        return None

    return SyncConflict(
        project_id=project_id,
        batch_id=batch_id,
        expected_base_revision_id=base_revision_id,
        current_head_revision_id=current_head_revision_id,
    )


def evaluate_sync_batch(
    batch: SyncBatch,
    *,
    current_head_revision_id: Optional[str],
    prior_receipt: Optional[BatchReceipt] = None,
) -> SyncDecision:
    """Classify a batch as replay, identity collision, conflict, or commit-ready.

    The adapter must look up ``prior_receipt`` by (project_id, batch_id), then
    atomically repeat the identity check and compare-and-swap against the head
    while appending the immutable revision and receipt. This function alone is
    only a read-time decision and cannot prevent a race.
    """
    if prior_receipt is not None:
        prior = prior_receipt.batch
        if (prior.project_id, prior.batch_id) != (batch.project_id, batch.batch_id):
            raise ValueError("prior_receipt identity does not match batch")
        if prior != batch:
            return SyncBatchIdentityConflict(project_id=batch.project_id, batch_id=batch.batch_id)
        return SyncReplay(receipt=prior_receipt)

    conflict = check_sync_base(
        project_id=batch.project_id,
        batch_id=batch.batch_id,
        base_revision_id=batch.base_revision_id,
        current_head_revision_id=current_head_revision_id,
    )
    if conflict is not None:
        return conflict
    return SyncReady(batch=batch, expected_head_revision_id=current_head_revision_id)
