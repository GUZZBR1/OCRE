"""Test harness for exercising job events without starting an executor.

This module is deliberately separate from submission and API services. It only
advances an already-created local job and returns a clearly marked fake report.
It never invokes a process host, solver, broker, or external service.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256

from ocre_core.jobs import (
    CancellationEvidence,
    CancellationEvidenceKind,
    JobState,
    transition_job,
)
from ocre_core.persistence.jobs import JobPersistencePort, JobSnapshot, OutboxMessage


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

    def __init__(self, store: JobPersistencePort, clock):
        self._store = store
        self._clock = clock

    def run(self, *, tenant_id: str, job_id: str, outcome: SyntheticOutcome) -> SyntheticReport:
        if not isinstance(outcome, SyntheticOutcome):
            raise ValueError(f"unsupported synthetic outcome: {outcome}")
        snapshot = self._store.get_snapshot(job_id, tenant_id=tenant_id)
        if snapshot is None:
            raise LookupError("job not found in tenant scope")
        if snapshot.state is not JobState.QUEUED:
            raise ValueError("synthetic worker requires a queued job")

        if outcome is SyntheticOutcome.CANCELLED:
            snapshot = self._advance(snapshot, tenant_id, JobState.CANCEL_REQUESTED)
            evidence = CancellationEvidence(
                aggregate_id=job_id,
                requested_from_state=JobState.QUEUED,
                kind=CancellationEvidenceKind.WORKER_START_PREVENTED,
                observed_at=self._clock(),
            )
            self._advance(
                snapshot,
                tenant_id,
                JobState.CANCELLED,
                cancellation_evidence=evidence,
                cancellation_origin=JobState.QUEUED,
            )
            result = "synthetic cancellation observed before start"
        else:
            snapshot = self._advance(snapshot, tenant_id, JobState.PREPARING)
            snapshot = self._advance(snapshot, tenant_id, JobState.RUNNING)
            if outcome is SyntheticOutcome.FAILED:
                self._advance(snapshot, tenant_id, JobState.FAILED)
                result = "synthetic execution failure"
            elif outcome is SyntheticOutcome.SUCCEEDED:
                snapshot = self._advance(snapshot, tenant_id, JobState.COLLECTING)
                self._advance(snapshot, tenant_id, JobState.SUCCEEDED)
                result = "synthetic static result"
            else:
                raise ValueError(f"unsupported synthetic outcome: {outcome}")

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
        return next_snapshot
