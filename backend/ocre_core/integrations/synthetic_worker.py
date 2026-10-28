"""Test harness for exercising job events without starting an executor.

This module is deliberately separate from submission and API services. It only
advances an already-created local job and returns a clearly marked fake report.
It never invokes a process host, solver, broker, or external service.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Callable

from ocre_core.jobs import (
    CancellationEvidence,
    CancellationEvidenceKind,
    ConcurrentJobUpdate,
    JobState,
    transition_job,
)
from ocre_core.persistence.jobs import JobPersistencePort, JobSnapshot, OutboxMessage
from ocre_core.persistence.sqlite import PersistenceConflict


SIMULATION_LABEL = "FAKE SIMULATION — NOT A REAL EXECUTION OR APPROVAL"


class SyntheticOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SyntheticReport:
    simulation: bool
    label: str
    job_id: str
    outcome: SyntheticOutcome
    result: str


class SyntheticWorker:
    """Deterministically exercise lifecycle persistence with static fake data."""

    def __init__(
        self,
        store: JobPersistencePort,
        clock,
        *,
        before_transition: Callable[[object], None] | None = None,
        after_transition: Callable[[object], None] | None = None,
    ):
        self._store = store
        self._clock = clock
        self._before_transition = before_transition
        self._after_transition = after_transition

    def run(self, *, tenant_id: str, job_id: str, outcome: SyntheticOutcome) -> SyntheticReport:
        if not isinstance(outcome, SyntheticOutcome):
            raise ValueError(f"unsupported synthetic outcome: {outcome}")
        conflicts = 0
        while True:
            snapshot = self._store.get_snapshot(job_id, tenant_id=tenant_id)
            if snapshot is None:
                raise LookupError("job not found in tenant scope")

            if snapshot.state is JobState.SUCCEEDED:
                return self._report(job_id, SyntheticOutcome.SUCCEEDED, "synthetic static result")
            if snapshot.state is JobState.FAILED:
                return self._report(job_id, SyntheticOutcome.FAILED, "synthetic execution failure")
            if snapshot.state is JobState.CANCELLED:
                return self._report(
                    job_id,
                    SyntheticOutcome.CANCELLED,
                    "synthetic cancellation observed before start",
                )
            if outcome is SyntheticOutcome.CANCELLED and snapshot.state in {
                JobState.PREPARING,
                JobState.RUNNING,
                JobState.COLLECTING,
            }:
                raise ValueError("synthetic cancellation requires a persisted cancel request")

            try:
                if snapshot.state is JobState.QUEUED:
                    target = (
                        JobState.CANCEL_REQUESTED
                        if outcome is SyntheticOutcome.CANCELLED
                        else JobState.PREPARING
                    )
                    self._advance(snapshot, tenant_id, target)
                elif snapshot.state is JobState.PREPARING:
                    self._advance(snapshot, tenant_id, JobState.RUNNING)
                elif snapshot.state is JobState.RUNNING:
                    target = (
                        JobState.FAILED
                        if outcome is SyntheticOutcome.FAILED
                        else JobState.COLLECTING
                    )
                    self._advance(snapshot, tenant_id, target)
                elif snapshot.state is JobState.CANCEL_REQUESTED:
                    origin = snapshot.cancellation_origin
                    if origin not in {JobState.QUEUED, JobState.RUNNING}:
                        raise ValueError("synthetic cancellation request has no valid origin")
                    if origin is JobState.QUEUED:
                        evidence = CancellationEvidence(
                            aggregate_id=job_id,
                            requested_from_state=origin,
                            kind=CancellationEvidenceKind.WORKER_START_PREVENTED,
                            observed_at=self._clock(),
                        )
                        self._advance(
                            snapshot,
                            tenant_id,
                            JobState.CANCELLED,
                            cancellation_evidence=evidence,
                            cancellation_origin=origin,
                        )
                    elif outcome is SyntheticOutcome.FAILED:
                        self._advance(
                            snapshot,
                            tenant_id,
                            JobState.FAILED,
                            cancellation_origin=origin,
                        )
                    elif outcome is SyntheticOutcome.CANCELLED:
                        evidence = CancellationEvidence(
                            aggregate_id=job_id,
                            requested_from_state=origin,
                            kind=CancellationEvidenceKind.PROCESS_TERMINATED_BY_CANCELLATION,
                            observed_at=self._clock(),
                        )
                        self._advance(
                            snapshot,
                            tenant_id,
                            JobState.CANCELLED,
                            cancellation_evidence=evidence,
                            cancellation_origin=origin,
                        )
                    else:
                        self._advance(
                            snapshot,
                            tenant_id,
                            JobState.COLLECTING,
                            cancellation_origin=origin,
                        )
                elif snapshot.state is JobState.COLLECTING:
                    self._advance(snapshot, tenant_id, JobState.SUCCEEDED)
                else:
                    raise ValueError(
                        f"synthetic worker cannot resume job in {snapshot.state.value}"
                    )
            except (ConcurrentJobUpdate, PersistenceConflict):
                conflicts += 1
                if conflicts > 8:
                    raise

    @staticmethod
    def _report(job_id: str, outcome: SyntheticOutcome, result: str) -> SyntheticReport:
        return SyntheticReport(True, SIMULATION_LABEL, job_id, outcome, result)

    def _advance(
        self,
        snapshot: JobSnapshot,
        tenant_id: str,
        target: JobState,
        *,
        cancellation_evidence: CancellationEvidence | None = None,
        cancellation_origin: JobState | None = None,
    ) -> JobSnapshot:
        now = self._clock()
        tenant_scope = sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
        event = transition_job(
            job_id=snapshot.job_id,
            event_id=f"synthetic-{tenant_scope}-{snapshot.job_id}-{snapshot.sequence + 1}",
            current_state=snapshot.state,
            current_sequence=snapshot.sequence,
            expected_sequence=snapshot.sequence,
            target_state=target,
            occurred_at=now,
            cancellation_evidence=cancellation_evidence,
            cancellation_origin=cancellation_origin,
        )
        next_snapshot = JobSnapshot(
            snapshot.job_id,
            target,
            event.sequence,
            now,
            snapshot.payload,
            tenant_id=tenant_id,
        )
        if self._before_transition is not None:
            self._before_transition(event)
        self._store.commit_transition(
            expected_sequence=snapshot.sequence,
            snapshot=next_snapshot,
            event=event,
            idempotency=None,
            outbox=OutboxMessage(
                f"synthetic-message-{tenant_scope}-{snapshot.job_id}-{event.sequence}",
                snapshot.job_id,
                event.event_id,
                "synthetic.job.events",
                b"",
            ),
        )
        if self._after_transition is not None:
            self._after_transition(event)
        return next_snapshot
