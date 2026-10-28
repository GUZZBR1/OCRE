"""Storage-independent atomic persistence ports for Core Cloud jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ocre_core.jobs import (
    AdapterExecutionState,
    AdapterExecutionTransition,
    JobState,
    JobTransition,
)


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    state: JobState
    sequence: int
    updated_at: datetime
    payload: bytes = b""
    cancellation_origin: JobState | None = None
    tenant_id: str = "default"


@dataclass(frozen=True)
class AdapterExecutionSnapshot:
    execution_id: str
    job_id: str
    state: AdapterExecutionState
    sequence: int
    updated_at: datetime
    payload: bytes = b""
    cancellation_origin: AdapterExecutionState | None = None
    tenant_id: str = "default"


@dataclass(frozen=True)
class IdempotencyRecord:
    tenant_id: str
    operation: str
    key: str
    request_fingerprint: str
    result_reference: str


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    aggregate_id: str
    event_id: str
    topic: str
    payload: bytes


@dataclass(frozen=True)
class ExecutionSubmitted:
    job_id: str
    execution_id: str
    event_id: str
    sequence: int
    occurred_at: datetime


@dataclass(frozen=True)
class CommitReceipt:
    snapshot: JobSnapshot | AdapterExecutionSnapshot
    event: JobTransition | AdapterExecutionTransition | ExecutionSubmitted
    replayed: bool = False


class IdempotencyConflict(ValueError):
    """A key was reused for a request with a different fingerprint."""


class JobPersistencePort(Protocol):
    """Atomic unit: CAS snapshot + append event + idempotency + outbox.

    Implementations must commit all supplied records or none. A transition out
    of cancel_requested must copy `cancellation_origin` from the persisted
    snapshot into the event; a cancellation confirmation must match that origin.
    No broker/worker
    call belongs inside this boundary. `commit_transition` first resolves an
    existing idempotency key: equal fingerprints replay its original receipt;
    differing fingerprints raise `IdempotencyConflict`.
    """

    def commit_transition(
        self,
        *,
        expected_sequence: int,
        snapshot: JobSnapshot | AdapterExecutionSnapshot,
        event: JobTransition | AdapterExecutionTransition,
        idempotency: IdempotencyRecord | None,
        outbox: OutboxMessage,
    ) -> CommitReceipt: ...

    def create_execution(
        self,
        *,
        job_snapshot: JobSnapshot,
        execution_snapshot: AdapterExecutionSnapshot,
        submitted_event: ExecutionSubmitted,
        idempotency: IdempotencyRecord,
        outbox: OutboxMessage,
    ) -> CommitReceipt: ...

    def get_snapshot(self, job_id: str, *, tenant_id: str) -> JobSnapshot | None: ...

    def get_execution_snapshot(
        self, execution_id: str, *, tenant_id: str
    ) -> AdapterExecutionSnapshot | None: ...

    def find_idempotency(
        self, *, tenant_id: str, operation: str, key: str
    ) -> CommitReceipt | None: ...
