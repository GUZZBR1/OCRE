"""Deterministic platform-job and adapter-execution lifecycle rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class JobState(str, Enum):
    """Generic OCRE platform lifecycle from INTEGRATION_ARCHITECTURE."""

    CREATED = "created"
    VALIDATED = "validated"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AdapterExecutionState(str, Enum):
    """Specialized batch adapter vocabulary; starts when submit succeeds."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    EXECUTION_FAILED = "execution_failed"
    COLLECTION_FAILED = "collection_failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)
ADAPTER_TERMINAL_STATES = frozenset(
    {
        AdapterExecutionState.SUCCEEDED,
        AdapterExecutionState.EXECUTION_FAILED,
        AdapterExecutionState.COLLECTION_FAILED,
        AdapterExecutionState.CANCELLED,
    }
)

_NORMAL_TRANSITIONS = {
    JobState.CREATED: frozenset({JobState.VALIDATED}),
    JobState.VALIDATED: frozenset({JobState.QUEUED}),
    JobState.QUEUED: frozenset({JobState.PREPARING, JobState.CANCEL_REQUESTED}),
    JobState.PREPARING: frozenset({JobState.RUNNING}),
    JobState.RUNNING: frozenset(
        {JobState.CANCEL_REQUESTED, JobState.COLLECTING, JobState.FAILED}
    ),
    JobState.CANCEL_REQUESTED: frozenset(
        {JobState.CANCELLED, JobState.COLLECTING, JobState.FAILED}
    ),
    JobState.COLLECTING: frozenset({JobState.SUCCEEDED, JobState.FAILED}),
}

_ADAPTER_TRANSITIONS = {
    AdapterExecutionState.QUEUED: frozenset(
        {AdapterExecutionState.RUNNING, AdapterExecutionState.CANCEL_REQUESTED}
    ),
    AdapterExecutionState.RUNNING: frozenset(
        {
            AdapterExecutionState.CANCEL_REQUESTED,
            AdapterExecutionState.COLLECTING,
            AdapterExecutionState.EXECUTION_FAILED,
        }
    ),
    AdapterExecutionState.CANCEL_REQUESTED: frozenset(
        {
            AdapterExecutionState.CANCELLED,
            AdapterExecutionState.COLLECTING,
            AdapterExecutionState.EXECUTION_FAILED,
        }
    ),
    AdapterExecutionState.COLLECTING: frozenset(
        {AdapterExecutionState.SUCCEEDED, AdapterExecutionState.COLLECTION_FAILED}
    ),
}


class InvalidJobTransition(ValueError):
    """Raised when a job attempts a transition outside its lifecycle."""


class CancellationUnsupportedInPhase(InvalidJobTransition):
    """Cancellation is unsupported once result collection has started."""


class ConcurrentJobUpdate(RuntimeError):
    """Raised when a caller transitions a stale job version."""


class CancellationEvidenceKind(str, Enum):
    WORKER_START_PREVENTED = "worker_start_prevented"
    PROCESS_TERMINATED_BY_CANCELLATION = "process_terminated_by_cancellation"


@dataclass(frozen=True)
class CancellationEvidence:
    aggregate_id: str
    requested_from_state: JobState | AdapterExecutionState
    kind: CancellationEvidenceKind
    observed_at: datetime


@dataclass(frozen=True)
class JobTransition:
    job_id: str
    event_id: str
    sequence: int
    from_state: JobState
    to_state: JobState
    occurred_at: datetime
    cancellation_evidence: CancellationEvidence | None = None
    cancellation_origin: JobState | None = None


@dataclass(frozen=True)
class AdapterExecutionTransition:
    execution_id: str
    event_id: str
    sequence: int
    from_state: AdapterExecutionState
    to_state: AdapterExecutionState
    occurred_at: datetime
    cancellation_evidence: CancellationEvidence | None = None
    cancellation_origin: AdapterExecutionState | None = None


def _validate_transition(*, current_state: Enum, target_state: Enum, transitions: dict) -> None:
    if current_state == target_state:
        raise InvalidJobTransition(f"state is unchanged: {current_state}")
    if current_state in TERMINAL_STATES or current_state in ADAPTER_TERMINAL_STATES:
        raise InvalidJobTransition(f"job is already terminal: {current_state}")
    if current_state == JobState.COLLECTING and target_state in {
        JobState.CANCEL_REQUESTED,
        JobState.CANCELLED,
    }:
        raise CancellationUnsupportedInPhase("cancellation_unsupported_in_phase")
    if target_state not in transitions.get(current_state, frozenset()):
        raise InvalidJobTransition(f"cannot transition {current_state} -> {target_state}")


def transition_job(
    *,
    job_id: str,
    event_id: str,
    current_state: JobState,
    current_sequence: int,
    expected_sequence: int,
    target_state: JobState,
    occurred_at: datetime,
    cancellation_evidence: CancellationEvidence | None = None,
    cancellation_origin: JobState | None = None,
) -> JobTransition:
    """Return a platform event; caller atomically persists it with the snapshot.

    Preflight validate/prepare errors are reports, not execution states/events.
    A cancel request only records intent; `cancelled` requires authoritative
    worker/process observation. Completion reported while cancel is pending
    follows the normal collection/failure path and wins over cancellation.
    CAS sequence provides event linearization; adapters must report observed
    process completion before accepting a cancellation acknowledgement.
    """
    if not job_id:
        raise ValueError("job_id must not be empty")
    if not event_id:
        raise ValueError("event_id must not be empty")
    if current_sequence < 0 or expected_sequence < 0:
        raise ValueError("job sequences must be non-negative")
    if current_sequence != expected_sequence:
        raise ConcurrentJobUpdate(
            f"expected job sequence {expected_sequence}, found {current_sequence}"
        )
    _validate_transition(
        current_state=current_state,
        target_state=target_state,
        transitions=_NORMAL_TRANSITIONS,
    )
    cancellation_origin = _resolve_cancellation_origin(
        current_state, target_state, cancellation_origin, JobState
    )
    _validate_cancellation_evidence(
        job_id, target_state, cancellation_evidence, cancellation_origin
    )
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return JobTransition(
        job_id=job_id,
        event_id=event_id,
        sequence=current_sequence + 1,
        from_state=current_state,
        to_state=target_state,
        occurred_at=occurred_at.astimezone(timezone.utc),
        cancellation_evidence=cancellation_evidence,
        cancellation_origin=cancellation_origin,
    )


def transition_adapter_execution(
    *,
    execution_id: str,
    event_id: str,
    current_state: AdapterExecutionState,
    current_sequence: int,
    expected_sequence: int,
    target_state: AdapterExecutionState,
    occurred_at: datetime,
    cancellation_evidence: CancellationEvidence | None = None,
    cancellation_origin: AdapterExecutionState | None = None,
) -> AdapterExecutionTransition:
    """Validate a specialized batch adapter transition.

    `cancelled` is an observed outcome, never the cancel request itself.
    Collecting deliberately has no cancellation transition.
    """
    if not execution_id:
        raise ValueError("execution_id must not be empty")
    if not event_id:
        raise ValueError("event_id must not be empty")
    if current_sequence < 0 or expected_sequence < 0:
        raise ValueError("execution sequences must be non-negative")
    if current_sequence != expected_sequence:
        raise ConcurrentJobUpdate(
            f"expected execution sequence {expected_sequence}, found {current_sequence}"
        )
    if current_state == AdapterExecutionState.COLLECTING and target_state in {
        AdapterExecutionState.CANCEL_REQUESTED,
        AdapterExecutionState.CANCELLED,
    }:
        raise CancellationUnsupportedInPhase("cancellation_unsupported_in_phase")
    _validate_transition(
        current_state=current_state,
        target_state=target_state,
        transitions=_ADAPTER_TRANSITIONS,
    )
    cancellation_origin = _resolve_cancellation_origin(
        current_state, target_state, cancellation_origin, AdapterExecutionState
    )
    _validate_cancellation_evidence(
        execution_id, target_state, cancellation_evidence, cancellation_origin
    )
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return AdapterExecutionTransition(
        execution_id=execution_id,
        event_id=event_id,
        sequence=current_sequence + 1,
        from_state=current_state,
        to_state=target_state,
        occurred_at=occurred_at.astimezone(timezone.utc),
        cancellation_evidence=cancellation_evidence,
        cancellation_origin=cancellation_origin,
    )


def _validate_cancellation_evidence(
    aggregate_id: str,
    target_state: Enum,
    evidence: CancellationEvidence | None,
    cancellation_origin: JobState | AdapterExecutionState | None,
) -> None:
    is_cancelled = target_state in {JobState.CANCELLED, AdapterExecutionState.CANCELLED}
    if not is_cancelled:
        if evidence is not None:
            raise ValueError("cancellation evidence is only valid for cancelled outcome")
        return
    if evidence is None:
        raise InvalidJobTransition("cancelled requires authoritative cancellation evidence")
    if cancellation_origin is None:
        raise InvalidJobTransition("cancel request origin is missing from persisted state")
    if evidence.aggregate_id != aggregate_id:
        raise InvalidJobTransition("cancellation evidence belongs to a different aggregate")
    evidence_source = evidence.requested_from_state
    if evidence_source != cancellation_origin:
        raise InvalidJobTransition("cancellation evidence does not match persisted request origin")
    if cancellation_origin.value not in {"queued", "running"}:
        raise InvalidJobTransition("unsupported state for cancellation evidence")
    if (
        evidence_source.value == "queued"
        and evidence.kind is not CancellationEvidenceKind.WORKER_START_PREVENTED
    ) or (
        evidence_source.value == "running"
        and evidence.kind
        is not CancellationEvidenceKind.PROCESS_TERMINATED_BY_CANCELLATION
    ):
        raise InvalidJobTransition("cancellation evidence does not match request phase")
    valid_kinds = {
        CancellationEvidenceKind.WORKER_START_PREVENTED,
        CancellationEvidenceKind.PROCESS_TERMINATED_BY_CANCELLATION,
    }
    if evidence.kind not in valid_kinds:
        raise InvalidJobTransition("unsupported cancellation evidence")
    if evidence.observed_at.tzinfo is None or evidence.observed_at.utcoffset() is None:
        raise ValueError("cancellation evidence timestamp must be timezone-aware")


def _resolve_cancellation_origin(
    current_state: Enum,
    target_state: Enum,
    supplied_origin: JobState | AdapterExecutionState | None,
    state_type: type[JobState] | type[AdapterExecutionState],
) -> JobState | AdapterExecutionState | None:
    cancel_requested = state_type.CANCEL_REQUESTED
    if target_state == cancel_requested:
        if supplied_origin is not None:
            raise InvalidJobTransition("cancel request origin is derived from current state")
        if current_state not in {state_type.QUEUED, state_type.RUNNING}:
            raise InvalidJobTransition("cancellation may only originate in queued or running")
        return current_state
    if current_state == cancel_requested:
        if supplied_origin not in {state_type.QUEUED, state_type.RUNNING}:
            raise InvalidJobTransition("cancel request origin is missing or invalid")
        return supplied_origin
    if supplied_origin is not None:
        raise InvalidJobTransition("cancel request origin exists outside cancel_requested")
    return None


def platform_state_for_adapter(state: AdapterExecutionState) -> JobState:
    """Map adapter state to generic platform state without renaming either API."""
    return {
        AdapterExecutionState.QUEUED: JobState.QUEUED,
        AdapterExecutionState.RUNNING: JobState.RUNNING,
        AdapterExecutionState.CANCEL_REQUESTED: JobState.CANCEL_REQUESTED,
        AdapterExecutionState.COLLECTING: JobState.COLLECTING,
        AdapterExecutionState.SUCCEEDED: JobState.SUCCEEDED,
        AdapterExecutionState.EXECUTION_FAILED: JobState.FAILED,
        AdapterExecutionState.COLLECTION_FAILED: JobState.FAILED,
        AdapterExecutionState.CANCELLED: JobState.CANCELLED,
    }[state]
