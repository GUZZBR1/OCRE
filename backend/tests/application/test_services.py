from datetime import datetime, timezone

import pytest

from ocre_core.application.services import (
    ApplicationError,
    JobSubmission,
    OCREApplication,
    PreflightReport,
    Principal,
)
from ocre_core.persistence.sqlite import SQLiteJobStore
from ocre_core.persistence.jobs import OutboxMessage
from ocre_core.persistence.sync import SQLiteSyncStore
from ocre_core.jobs import (
    CancellationEvidence,
    CancellationEvidenceKind,
    JobState,
    transition_job,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Projects:
    def authorize(self, principal, project_id, action):
        return project_id in {"p1"} and principal.tenant_id == "t1"


class FixedPreflight:
    def __init__(self, report):
        self.report = report
        self.calls = 0

    def evaluate(self, _request):
        self.calls += 1
        return self.report


def application(tmp_path, preflight):
    return OCREApplication(
        sync_store=SQLiteSyncStore(tmp_path / "sync.db"),
        job_store=SQLiteJobStore(tmp_path / "jobs.db"),
        authorizer=Projects(),
        preflight=preflight,
        clock=lambda: NOW,
    )


def submission():
    return JobSubmission("rev1", "case", "adapter", "0", {"eir": 1}, {"bundle": 1})


def test_blocked_approval_fingerprint_preflight_writes_no_job_event_or_outbox(tmp_path):
    preflight = FixedPreflight(PreflightReport("incomplete", "blocked", ({"code": "approval_fingerprint_unavailable"},)))
    app = application(tmp_path, preflight)
    with pytest.raises(ApplicationError) as error:
        app.submit_job(Principal("t1", "u1"), "p1", submission(), "key")
    assert error.value.code == "preparation_failed"
    assert error.value.extra["report"]["diagnostics"][0]["code"] == "approval_fingerprint_unavailable"
    store = app.job_store
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_test_only_ready_preflight_proves_atomic_submit_cancel_and_replay(tmp_path):
    app = application(tmp_path, FixedPreflight(PreflightReport("valid", "ready")))
    principal = Principal("t1", "u1")
    submitted = app.submit_job(principal, "p1", submission(), "submit-key")
    assert submitted["state"] == "queued"
    job_id = submitted["job_id"]
    assert app.job_status(principal, "p1", job_id)["state"] == "queued"
    cancelled = app.cancel_job(principal, "p1", job_id, "cancel-key")
    assert cancelled["state"] == "cancel_requested"
    assert app.cancel_job(principal, "p1", job_id, "cancel-key")["replayed"]
    with pytest.raises(ApplicationError) as repeated_with_new_key:
        app.cancel_job(principal, "p1", job_id, "another-cancel-key")
    assert (repeated_with_new_key.value.status, repeated_with_new_key.value.code) == (409, "cancellation_unsupported_in_phase")
    page = app.replay_events(principal, "p1", job_id, 0, 1)
    assert len(page["items"]) == 1
    assert page["has_more"] is True
    next_page = app.replay_events(principal, "p1", job_id, page["next_cursor"], 1)
    assert next_page["items"][0]["event"]["to_state"] == "cancel_requested"
    assert next_page["has_more"] is False


def test_sync_identity_comes_from_principal_and_cross_tenant_is_not_found(tmp_path):
    app = application(tmp_path, FixedPreflight(PreflightReport("incomplete", "blocked")))
    principal = Principal("t1", "real-actor")
    receipt = app.submit_sync(principal, "p1", {
        "batch_id": "b1",
        "actor_id": "forged-actor",
        "tenant_id": "forged-tenant",
        "operations": [{"operation_id": "o1", "payload": "eA=="}],
    })
    stored = app.sync_store.lookup(tenant_id="t1", project_id="p1", batch_id="b1")
    assert stored.batch.actor_id == "real-actor"
    assert app.get_sync_receipt(principal, "p1", "b1")["committed_revision_id"] == receipt["committed_revision_id"]
    with pytest.raises(ApplicationError) as error:
        app.get_sync_receipt(Principal("t2", "other"), "p1", "b1")
    assert error.value.status == 404


def test_job_project_mismatch_and_cross_tenant_are_indistinguishable_not_found(tmp_path):
    app = application(tmp_path, FixedPreflight(PreflightReport("valid", "ready")))
    owner = Principal("t1", "u1")
    job_id = app.submit_job(owner, "p1", submission(), "k")["job_id"]
    for principal, project in ((Principal("t2", "u2"), "p1"), (owner, "other")):
        with pytest.raises(ApplicationError) as error:
            app.job_status(principal, project, job_id)
        assert (error.value.status, error.value.code) == (404, "resource_not_found")


def test_cancel_collecting_job_returns_conflict_and_cancelled_event_replays_nested_evidence(tmp_path):
    app = application(tmp_path, FixedPreflight(PreflightReport("valid", "ready")))
    principal = Principal("t1", "u1")
    job_id = app.submit_job(principal, "p1", submission(), "submit-key")["job_id"]
    assert app.cancel_job(principal, "p1", job_id, "cancel-key")["state"] == "cancel_requested"

    snapshot = app.job_store.get_snapshot(job_id, tenant_id="t1")
    evidence = CancellationEvidence(
        aggregate_id=job_id,
        requested_from_state=JobState.QUEUED,
        kind=CancellationEvidenceKind.WORKER_START_PREVENTED,
        observed_at=NOW,
    )
    event = transition_job(
        job_id=job_id,
        event_id="cancelled-event",
        current_state=snapshot.state,
        current_sequence=snapshot.sequence,
        expected_sequence=snapshot.sequence,
        target_state=JobState.CANCELLED,
        occurred_at=NOW,
        cancellation_origin=JobState.QUEUED,
        cancellation_evidence=evidence,
    )
    app.job_store.commit_transition(
        expected_sequence=snapshot.sequence,
        snapshot=type(snapshot)(job_id, event.to_state, event.sequence, NOW, snapshot.payload, tenant_id="t1"),
        event=event,
        idempotency=None,
        outbox=OutboxMessage("cancelled-message", job_id, event.event_id, "job.cancelled", b""),
    )
    replay = app.replay_events(principal, "p1", job_id, 0, 10)
    cancelled = next(item["event"] for item in replay["items"] if item["event"].get("event_id") == "cancelled-event")
    assert cancelled["cancellation_evidence"] == {
        "aggregate_id": job_id,
        "requested_from_state": "queued",
        "kind": "worker_start_prevented",
        "observed_at": NOW.isoformat(),
    }

    collecting_job = app.submit_job(principal, "p1", submission(), "second-submit-key")["job_id"]
    transitions = (
        (JobState.QUEUED, JobState.PREPARING),
        (JobState.PREPARING, JobState.RUNNING),
        (JobState.RUNNING, JobState.COLLECTING),
    )
    for index, (current, target) in enumerate(transitions, start=2):
        current_snapshot = app.job_store.get_snapshot(collecting_job, tenant_id="t1")
        transition = transition_job(
            job_id=collecting_job,
            event_id=f"phase-{index}",
            current_state=current_snapshot.state,
            current_sequence=current_snapshot.sequence,
            expected_sequence=current_snapshot.sequence,
            target_state=target,
            occurred_at=NOW,
        )
        app.job_store.commit_transition(
            expected_sequence=current_snapshot.sequence,
            snapshot=type(current_snapshot)(
                collecting_job, target, transition.sequence, NOW,
                current_snapshot.payload, tenant_id="t1",
            ),
            event=transition,
            idempotency=None,
            outbox=OutboxMessage(f"phase-message-{index}", collecting_job, transition.event_id, "job.phase", b""),
        )

    with pytest.raises(ApplicationError) as error:
        app.cancel_job(principal, "p1", collecting_job, "cancel-collecting")
    assert (error.value.status, error.value.code) == (409, "cancellation_unsupported_in_phase")
