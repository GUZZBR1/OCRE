from datetime import datetime, timezone

import pytest

from ocre_core.jobs import (
    AdapterExecutionState as E,
    CancellationEvidence,
    CancellationEvidenceKind,
    CancellationUnsupportedInPhase,
    ConcurrentJobUpdate,
    InvalidJobTransition,
    JobState as J,
    platform_state_for_adapter,
    transition_adapter_execution,
    transition_job,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def event(current, target, sequence=0, cancellation_evidence=None, cancellation_origin=None):
    return transition_job(
        job_id="j1", event_id=f"e{sequence + 1}", current_state=current,
        current_sequence=sequence, expected_sequence=sequence,
        target_state=target, occurred_at=NOW,
        cancellation_evidence=cancellation_evidence,
        cancellation_origin=cancellation_origin,
    )


def adapter_event(
    current, target, sequence=0, cancellation_evidence=None, cancellation_origin=None
):
    return transition_adapter_execution(
        execution_id="x1", event_id=f"ae{sequence + 1}", current_state=current,
        current_sequence=sequence, expected_sequence=sequence,
        target_state=target, occurred_at=NOW,
        cancellation_evidence=cancellation_evidence,
        cancellation_origin=cancellation_origin,
    )


def test_platform_lifecycle_keeps_generic_preparation_and_cancel_request():
    assert event(J.QUEUED, J.PREPARING).sequence == 1
    assert event(J.PREPARING, J.RUNNING).to_state is J.RUNNING
    requested = event(J.RUNNING, J.CANCEL_REQUESTED)
    assert requested.to_state is J.CANCEL_REQUESTED
    assert requested.cancellation_origin is J.RUNNING
    queued_request = event(J.QUEUED, J.CANCEL_REQUESTED)
    assert queued_request.cancellation_origin is J.QUEUED
    evidence = CancellationEvidence(
        "j1", J.QUEUED, CancellationEvidenceKind.WORKER_START_PREVENTED, NOW
    )
    assert event(
        J.CANCEL_REQUESTED,
        J.CANCELLED,
        cancellation_evidence=evidence,
        cancellation_origin=queued_request.cancellation_origin,
    ).to_state is J.CANCELLED


def test_platform_does_not_allow_direct_cancel_or_collecting_cancel():
    with pytest.raises(InvalidJobTransition):
        event(J.RUNNING, J.CANCELLED)
    with pytest.raises(CancellationUnsupportedInPhase, match="cancellation_unsupported_in_phase"):
        event(J.COLLECTING, J.CANCEL_REQUESTED)
    with pytest.raises(CancellationUnsupportedInPhase, match="cancellation_unsupported_in_phase"):
        event(J.COLLECTING, J.CANCELLED)


def test_preflight_is_not_an_execution_failure_or_cancellation():
    for state in (J.CREATED, J.VALIDATED, J.PREPARING):
        with pytest.raises(InvalidJobTransition):
            event(state, J.FAILED)
        with pytest.raises(InvalidJobTransition):
            event(state, J.CANCELLED)


def test_cancel_pending_completion_wins_and_terminal_cannot_be_rewritten():
    assert event(J.CANCEL_REQUESTED, J.COLLECTING, cancellation_origin=J.RUNNING).to_state is J.COLLECTING
    assert event(J.CANCEL_REQUESTED, J.FAILED, cancellation_origin=J.RUNNING).to_state is J.FAILED
    confirmed = event(
        J.CANCEL_REQUESTED,
        J.CANCELLED,
        cancellation_evidence=CancellationEvidence(
            "j1", J.RUNNING, CancellationEvidenceKind.PROCESS_TERMINATED_BY_CANCELLATION, NOW
        ),
        cancellation_origin=J.RUNNING,
    )
    assert confirmed.cancellation_evidence is not None
    with pytest.raises(InvalidJobTransition):
        event(J.SUCCEEDED, J.CANCELLED)
    with pytest.raises(InvalidJobTransition, match="authoritative cancellation evidence"):
        event(J.CANCEL_REQUESTED, J.CANCELLED, cancellation_origin=J.RUNNING)
    wrong_job_evidence = CancellationEvidence(
        "other-job", J.QUEUED, CancellationEvidenceKind.WORKER_START_PREVENTED, NOW
    )
    with pytest.raises(InvalidJobTransition, match="different aggregate"):
        event(
            J.CANCEL_REQUESTED,
            J.CANCELLED,
            cancellation_evidence=wrong_job_evidence,
            cancellation_origin=J.RUNNING,
        )
    wrong_phase_evidence = CancellationEvidence(
        "j1", J.RUNNING, CancellationEvidenceKind.WORKER_START_PREVENTED, NOW
    )
    with pytest.raises(InvalidJobTransition, match="does not match request phase"):
        event(
            J.CANCEL_REQUESTED,
            J.CANCELLED,
            cancellation_evidence=wrong_phase_evidence,
            cancellation_origin=J.RUNNING,
        )
    claimed_queued_origin = CancellationEvidence(
        "j1", J.QUEUED, CancellationEvidenceKind.WORKER_START_PREVENTED, NOW
    )
    with pytest.raises(InvalidJobTransition, match="persisted request origin"):
        event(
            J.CANCEL_REQUESTED,
            J.CANCELLED,
            cancellation_evidence=claimed_queued_origin,
            cancellation_origin=J.RUNNING,
        )


def test_compare_and_swap_rejects_stale_sequence():
    with pytest.raises(ConcurrentJobUpdate):
        transition_job(
            job_id="j1", event_id="e2", current_state=J.RUNNING,
            current_sequence=2, expected_sequence=1, target_state=J.CANCEL_REQUESTED,
            occurred_at=NOW,
        )


def test_adapter_vocabulary_and_platform_mapping_are_distinct():
    assert adapter_event(E.QUEUED, E.RUNNING).sequence == 1
    assert platform_state_for_adapter(E.EXECUTION_FAILED) is J.FAILED
    assert platform_state_for_adapter(E.COLLECTION_FAILED) is J.FAILED
    assert E.EXECUTION_FAILED.value != J.FAILED.value


def test_adapter_cancel_is_requested_then_observed_and_collecting_is_unsupported():
    requested = adapter_event(E.RUNNING, E.CANCEL_REQUESTED)
    assert requested.cancellation_origin is E.RUNNING
    queued_request = adapter_event(E.QUEUED, E.CANCEL_REQUESTED)
    assert queued_request.cancellation_origin is E.QUEUED
    evidence = CancellationEvidence(
        "x1", E.RUNNING, CancellationEvidenceKind.PROCESS_TERMINATED_BY_CANCELLATION, NOW
    )
    adapter_event(
        E.CANCEL_REQUESTED,
        E.CANCELLED,
        cancellation_evidence=evidence,
        cancellation_origin=E.RUNNING,
    )
    adapter_event(E.CANCEL_REQUESTED, E.COLLECTING, cancellation_origin=E.RUNNING)
    with pytest.raises(CancellationUnsupportedInPhase, match="cancellation_unsupported_in_phase"):
        adapter_event(E.COLLECTING, E.CANCEL_REQUESTED)
    with pytest.raises(CancellationUnsupportedInPhase, match="cancellation_unsupported_in_phase"):
        adapter_event(E.COLLECTING, E.CANCELLED)
    with pytest.raises(InvalidJobTransition):
        adapter_event(E.RUNNING, E.CANCELLED)
    with pytest.raises(InvalidJobTransition, match="authoritative cancellation evidence"):
        adapter_event(E.CANCEL_REQUESTED, E.CANCELLED, cancellation_origin=E.RUNNING)
    wrong_phase_evidence = CancellationEvidence(
        "x1", E.QUEUED, CancellationEvidenceKind.PROCESS_TERMINATED_BY_CANCELLATION, NOW
    )
    with pytest.raises(InvalidJobTransition, match="persisted request origin"):
        adapter_event(
            E.CANCEL_REQUESTED,
            E.CANCELLED,
            cancellation_evidence=wrong_phase_evidence,
            cancellation_origin=E.RUNNING,
        )
    claimed_queued_origin = CancellationEvidence(
        "x1", E.QUEUED, CancellationEvidenceKind.WORKER_START_PREVENTED, NOW
    )
    with pytest.raises(InvalidJobTransition, match="persisted request origin"):
        adapter_event(
            E.CANCEL_REQUESTED,
            E.CANCELLED,
            cancellation_evidence=claimed_queued_origin,
            cancellation_origin=E.RUNNING,
        )


def test_adapter_event_has_its_own_execution_identity_and_sequence_cas():
    item = adapter_event(E.QUEUED, E.RUNNING, sequence=7)
    assert (item.execution_id, item.sequence, item.event_id) == ("x1", 8, "ae8")
    with pytest.raises(ConcurrentJobUpdate):
        transition_adapter_execution(
            execution_id="x1", event_id="ae9", current_state=E.RUNNING,
            current_sequence=8, expected_sequence=7, target_state=E.COLLECTING,
            occurred_at=NOW,
        )
