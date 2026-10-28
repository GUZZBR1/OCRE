from datetime import datetime, timezone

import pytest

from ocre_core.integrations.synthetic_worker import (
    SIMULATION_LABEL,
    SyntheticOutcome,
    SyntheticWorker,
)
from ocre_core.jobs import JobState
from ocre_core.persistence.jobs import JobSnapshot
from ocre_core.persistence.sqlite import SQLiteJobStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def queued_job(store, job_id, tenant_id="tenant-a"):
    snapshot = JobSnapshot(job_id, JobState.QUEUED, 1, NOW, tenant_id=tenant_id)
    with store._connect() as db:
        db.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
            (tenant_id, "job", job_id, job_id, 1, store._dump(snapshot)),
        )


@pytest.mark.parametrize(
    ("outcome", "terminal", "expected_states"),
    [
        (
            SyntheticOutcome.SUCCEEDED,
            JobState.SUCCEEDED,
            ["preparing", "running", "collecting", "succeeded"],
        ),
        (SyntheticOutcome.FAILED, JobState.FAILED, ["preparing", "running", "failed"]),
        (
            SyntheticOutcome.CANCELLED,
            JobState.CANCELLED,
            ["cancel_requested", "cancelled"],
        ),
    ],
)
def test_synthetic_worker_persists_labeled_fake_lifecycle(
    tmp_path, outcome, terminal, expected_states
):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    queued_job(store, "job-1")

    report = SyntheticWorker(store, lambda: NOW).run(
        tenant_id="tenant-a", job_id="job-1", outcome=outcome
    )

    assert report.simulation is True
    assert report.label == SIMULATION_LABEL
    assert "FAKE" in report.label and "NOT A REAL" in report.label
    assert report.outcome is outcome
    assert store.get_snapshot("job-1", tenant_id="tenant-a").state is terminal
    events = store.read_events(tenant_id="tenant-a", job_id="job-1", limit=20)
    assert [item.event.to_state.value for item in events] == expected_states
    assert [item.event.sequence for item in events] == list(
        range(2, 2 + len(expected_states))
    )
    assert all(item.event.event_id.startswith("synthetic-") for item in events)
    assert all(item.event.to_state is not JobState.SUCCEEDED for item in events[:-1])


def test_synthetic_worker_is_tenant_scoped_and_requires_queued_job(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    queued_job(store, "job-1")
    worker = SyntheticWorker(store, lambda: NOW)

    with pytest.raises(LookupError):
        worker.run(tenant_id="tenant-b", job_id="job-1", outcome=SyntheticOutcome.SUCCEEDED)

    first = worker.run(tenant_id="tenant-a", job_id="job-1", outcome=SyntheticOutcome.FAILED)
    replay = worker.run(tenant_id="tenant-a", job_id="job-1", outcome=SyntheticOutcome.SUCCEEDED)
    assert first == replay


def test_same_job_id_in_two_tenants_has_distinct_fake_event_and_outbox_ids(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    queued_job(store, "shared-job", "tenant-a")
    queued_job(store, "shared-job", "tenant-b")
    worker = SyntheticWorker(store, lambda: NOW)

    worker.run(tenant_id="tenant-a", job_id="shared-job", outcome=SyntheticOutcome.FAILED)
    worker.run(tenant_id="tenant-b", job_id="shared-job", outcome=SyntheticOutcome.FAILED)

    a_events = store.read_events(tenant_id="tenant-a", job_id="shared-job", limit=20)
    b_events = store.read_events(tenant_id="tenant-b", job_id="shared-job", limit=20)
    assert {item.event.event_id for item in a_events}.isdisjoint(
        {item.event.event_id for item in b_events}
    )
    with store._connect() as db:
        message_ids = db.execute(
            "SELECT message_id FROM outbox WHERE aggregate_id='shared-job'"
        ).fetchall()
    assert len(message_ids) == len({row[0] for row in message_ids})


def test_unsupported_outcome_is_rejected_without_persisting_partial_lifecycle(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    queued_job(store, "job-1")

    with pytest.raises(ValueError, match="unsupported synthetic outcome"):
        SyntheticWorker(store, lambda: NOW).run(
            tenant_id="tenant-a", job_id="job-1", outcome="bogus"
        )

    assert store.get_snapshot("job-1", tenant_id="tenant-a").state is JobState.QUEUED
    assert store.read_events(tenant_id="tenant-a", job_id="job-1") == ()
    assert store.pending_outbox() == ()


def test_cancelled_outcome_cannot_turn_running_job_into_success(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    queued_job(store, "job-1")
    running = JobSnapshot("job-1", JobState.RUNNING, 3, NOW, tenant_id="tenant-a")
    with store._connect() as db:
        db.execute(
            "UPDATE snapshots SET seq=?, body=? WHERE tenant=? AND kind='job' AND id=?",
            (running.sequence, store._dump(running), "tenant-a", "job-1"),
        )

    with pytest.raises(ValueError, match="requires a persisted cancel request"):
        SyntheticWorker(store, lambda: NOW).run(
            tenant_id="tenant-a", job_id="job-1", outcome=SyntheticOutcome.CANCELLED
        )

    assert store.get_snapshot("job-1", tenant_id="tenant-a") == running
