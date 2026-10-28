from datetime import datetime, timezone
import inspect

from ocre_core.jobs import AdapterExecutionState, JobState, transition_job
from ocre_core.persistence.jobs import (
    AdapterExecutionSnapshot,
    ExecutionSubmitted,
    JobSnapshot,
    OutboxMessage,
    JobPersistencePort,
)


def test_snapshot_reads_require_explicit_tenant_scope():
    for method_name in ("get_snapshot", "get_execution_snapshot"):
        parameter=inspect.signature(getattr(JobPersistencePort,method_name)).parameters["tenant_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


def test_atomic_port_values_share_job_sequence_and_event_identity():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = transition_job(
        job_id="j1", event_id="ev-1", current_state=JobState.QUEUED,
        current_sequence=4, expected_sequence=4, target_state=JobState.PREPARING,
        occurred_at=now,
    )
    snapshot = JobSnapshot("j1", event.to_state, event.sequence, now)
    outbox = OutboxMessage("msg-ev-1", "j1", event.event_id, "job.events", b"{}")
    assert snapshot.sequence == event.sequence
    assert snapshot.state is event.to_state
    assert outbox.aggregate_id == event.job_id
    assert outbox.event_id == event.event_id


def test_submit_bootstrap_values_start_platform_and_adapter_at_queued():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = JobSnapshot("j1", JobState.QUEUED, 1, now)
    execution = AdapterExecutionSnapshot("x1", "j1", AdapterExecutionState.QUEUED, 1, now)
    submitted = ExecutionSubmitted("j1", "x1", "ev-submit", 1, now)
    assert job.state is JobState.QUEUED
    assert execution.state is AdapterExecutionState.QUEUED
    assert submitted.job_id == job.job_id == execution.job_id
    assert submitted.execution_id == execution.execution_id
