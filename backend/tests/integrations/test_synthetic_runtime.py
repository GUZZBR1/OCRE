from datetime import datetime, timezone

import pytest

from ocre_core.application.services import (
    ApplicationError,
    JobSubmission,
    OCREApplication,
    PreflightReport,
    Principal,
)
from ocre_core.integrations.synthetic_runtime import SyntheticRuntime
from ocre_core.integrations.synthetic_worker import SyntheticOutcome
from ocre_core.jobs import JobState
from ocre_core.persistence.jobs import ExecutionSubmitted, JobSnapshot, OutboxMessage
from ocre_core.persistence.sqlite import SQLiteJobStore
from ocre_core.persistence.sync import SQLiteSyncStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Projects:
    def authorize(self, principal, project_id, action):
        return project_id == "project" and principal.tenant_id.startswith("tenant-")


class ReadyPreflight:
    def evaluate(self, _request):
        return PreflightReport("valid", "ready")


def setup_runtime(
    tmp_path,
    *,
    tenants=("tenant-a",),
    outcome=SyntheticOutcome.SUCCEEDED,
    before_transition=None,
    after_transition=None,
):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    app = OCREApplication(
        sync_store=SQLiteSyncStore(tmp_path / "sync.db"),
        job_store=store,
        authorizer=Projects(),
        preflight=ReadyPreflight(),
        clock=lambda: NOW,
    )
    submitted = {}
    for tenant in tenants:
        principal = Principal(tenant, f"user-{tenant}")
        submitted[tenant] = app.submit_job(
            principal,
            "project",
            JobSubmission("revision", "case", "fake-adapter", "0", {"eir": 1}, {"bundle": 1}),
            f"submit-{tenant}",
        )
    runtimes = {
        tenant: SyntheticRuntime(
            store,
            tenant_id=tenant,
            job_id=result["job_id"],
            consumer_database=tmp_path / f"consumer-{tenant}.db",
            outcome=outcome,
            clock=lambda: NOW,
            before_transition=before_transition,
            after_transition=after_transition,
        )
        for tenant, result in submitted.items()
    }
    return app, store, submitted, runtimes


def assert_monotonic_single_terminal(store, tenant, job_id, terminal):
    events = store.read_events(tenant_id=tenant, job_id=job_id, limit=50)
    assert [row.sequence for row in events] == list(range(1, len(events) + 1))
    assert [row.cursor for row in events] == sorted(row.cursor for row in events)
    assert sum(getattr(row.event, "to_state", None) in {
        JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED
    } for row in events) == 1
    assert store.get_snapshot(job_id, tenant_id=tenant).state is terminal
    return events


def drain(runtime):
    while runtime.dispatch():
        pass


def test_application_dispatch_consumer_worker_replay_is_ordered_and_tenant_scoped(tmp_path):
    app, store, submitted, runtimes = setup_runtime(tmp_path, tenants=("tenant-a", "tenant-b"))
    a_job = submitted["tenant-a"]["job_id"]
    b_job = submitted["tenant-b"]["job_id"]

    assert runtimes["tenant-a"].dispatch() == 1
    assert store.get_snapshot(a_job, tenant_id="tenant-a").state is JobState.SUCCEEDED
    assert store.get_snapshot(b_job, tenant_id="tenant-b").state is JobState.QUEUED
    drain(runtimes["tenant-a"])
    drain(runtimes["tenant-b"])

    events = assert_monotonic_single_terminal(store, "tenant-a", a_job, JobState.SUCCEEDED)
    assert [row.event.to_state.value for row in events[1:]] == [
        "preparing", "running", "collecting", "succeeded"
    ]
    assert store.get_snapshot(b_job, tenant_id="tenant-b").state is JobState.SUCCEEDED
    assert app.job_status(Principal("tenant-a", "user-a"), "project", a_job)["result_available"] is False
    assert all("FAKE" in report.label and "NOT A REAL" in report.label for report in runtimes["tenant-a"].reports)
    assert len(runtimes["tenant-a"].reports) == len(runtimes["tenant-b"].reports) == 1


def test_outbox_routing_is_tenant_scoped_even_with_colliding_job_and_event_ids(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    job_id = "shared-job"
    for tenant in ("tenant-a", "tenant-b"):
        snapshot = JobSnapshot(job_id, JobState.QUEUED, 1, NOW, tenant_id=tenant)
        event = ExecutionSubmitted(job_id, f"execution-{tenant}", "same-event", 1, NOW)
        with store._connect() as db:
            db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                (tenant, "job", job_id, job_id, 1, store._dump(snapshot)),
            )
            db.execute(
                "INSERT INTO events(tenant,job_id,seq,event_id,body) VALUES(?,?,?,?,?)",
                (tenant, job_id, 1, event.event_id, store._dump(event)),
            )
            db.execute(
                "INSERT INTO outbox(message_id,tenant,aggregate_id,event_id,topic,body) VALUES(?,?,?,?,?,?)",
                (f"message-{tenant}", tenant, job_id, event.event_id, "job.submitted", b""),
            )

    runtimes = {
        tenant: SyntheticRuntime(
            store,
            tenant_id=tenant,
            job_id=job_id,
            consumer_database=tmp_path / f"consumer-{tenant}.db",
            outcome=SyntheticOutcome.SUCCEEDED,
            clock=lambda: NOW,
        )
        for tenant in ("tenant-a", "tenant-b")
    }
    a_message = runtimes["tenant-a"].pending_messages()[0]
    b_message = runtimes["tenant-b"].pending_messages()[0]
    assert a_message.event_id == b_message.event_id == "same-event"
    assert a_message.message_id != b_message.message_id
    with pytest.raises(LookupError, match="outside tenant/job scope"):
        runtimes["tenant-a"].deliver(b_message)
    assert store.get_snapshot(job_id, tenant_id="tenant-a").state is JobState.QUEUED
    assert store.get_snapshot(job_id, tenant_id="tenant-b").state is JobState.QUEUED

    drain(runtimes["tenant-a"])
    assert store.get_snapshot(job_id, tenant_id="tenant-a").state is JobState.SUCCEEDED
    assert store.get_snapshot(job_id, tenant_id="tenant-b").state is JobState.QUEUED
    drain(runtimes["tenant-b"])
    assert_monotonic_single_terminal(store, "tenant-a", job_id, JobState.SUCCEEDED)
    assert_monotonic_single_terminal(store, "tenant-b", job_id, JobState.SUCCEEDED)


@pytest.mark.parametrize(
    ("outcome", "crash_state", "terminal"),
    [
        (SyntheticOutcome.SUCCEEDED, JobState.PREPARING, JobState.SUCCEEDED),
        (SyntheticOutcome.SUCCEEDED, JobState.RUNNING, JobState.SUCCEEDED),
        (SyntheticOutcome.SUCCEEDED, JobState.COLLECTING, JobState.SUCCEEDED),
        (SyntheticOutcome.SUCCEEDED, JobState.SUCCEEDED, JobState.SUCCEEDED),
        (SyntheticOutcome.FAILED, JobState.FAILED, JobState.FAILED),
        (SyntheticOutcome.CANCELLED, JobState.CANCEL_REQUESTED, JobState.CANCELLED),
        (SyntheticOutcome.CANCELLED, JobState.CANCELLED, JobState.CANCELLED),
    ],
)
def test_restart_after_worker_commit_resumes_from_persisted_state(
    tmp_path, outcome, crash_state, terminal
):
    crashed = False

    def crash_once(event):
        nonlocal crashed
        if event.to_state is crash_state and not crashed:
            crashed = True
            raise RuntimeError("synthetic worker crashed after transition commit")

    _app, store, submitted, runtimes = setup_runtime(
        tmp_path, outcome=outcome, after_transition=crash_once
    )
    tenant = "tenant-a"
    job_id = submitted[tenant]["job_id"]
    with pytest.raises(RuntimeError, match="after transition commit"):
        runtimes[tenant].dispatch()
    assert store.get_snapshot(job_id, tenant_id=tenant).state is crash_state

    restarted = SyntheticRuntime(
        store,
        tenant_id=tenant,
        job_id=job_id,
        consumer_database=tmp_path / "consumer-tenant-a.db",
        outcome=outcome,
        clock=lambda: NOW,
    )
    assert restarted.recover_abandoned() == 0
    drain(restarted)
    assert_monotonic_single_terminal(store, tenant, job_id, terminal)
    assert restarted.reports


def test_duplicate_callback_and_publish_before_ack_do_not_duplicate_transitions(tmp_path):
    _app, store, submitted, runtimes = setup_runtime(tmp_path)
    runtime = runtimes["tenant-a"]
    job_id = submitted["tenant-a"]["job_id"]
    original = runtime.pending_messages()[0]

    assert runtime.deliver(original) is True
    event_count = len(store.read_events(tenant_id="tenant-a", job_id=job_id, limit=50))
    restarted = SyntheticRuntime(
        store,
        tenant_id="tenant-a",
        job_id=job_id,
        consumer_database=tmp_path / "consumer-tenant-a.db",
        outcome=SyntheticOutcome.SUCCEEDED,
        clock=lambda: NOW,
    )
    assert restarted.deliver(original) is False
    drain(restarted)
    assert len(store.read_events(tenant_id="tenant-a", job_id=job_id, limit=50)) == event_count
    assert_monotonic_single_terminal(store, "tenant-a", job_id, JobState.SUCCEEDED)
    assert len(runtime.reports) == 1


def test_process_death_after_terminal_commit_recovers_consumer_claim_and_redelivers(tmp_path):
    def terminate_process(event):
        if event.to_state is JobState.SUCCEEDED:
            raise SystemExit("simulated process death after commit")

    _app, store, submitted, runtimes = setup_runtime(
        tmp_path, after_transition=terminate_process
    )
    tenant = "tenant-a"
    job_id = submitted[tenant]["job_id"]
    with pytest.raises(SystemExit, match="simulated process death"):
        runtimes[tenant].dispatch()
    assert store.get_snapshot(job_id, tenant_id=tenant).state is JobState.SUCCEEDED

    restarted = SyntheticRuntime(
        store,
        tenant_id=tenant,
        job_id=job_id,
        consumer_database=tmp_path / "consumer-tenant-a.db",
        outcome=SyntheticOutcome.SUCCEEDED,
        clock=lambda: NOW,
    )
    assert restarted.recover_abandoned() == 1
    drain(restarted)
    assert_monotonic_single_terminal(store, tenant, job_id, JobState.SUCCEEDED)
    assert len(restarted.reports) == 1


def test_cancel_racing_worker_start_has_one_valid_terminal_outcome(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered = Event()
    release = Event()
    paused = False

    def pause_before_start(event):
        nonlocal paused
        if event.to_state is JobState.PREPARING and not paused:
            paused = True
            entered.set()
            assert release.wait(timeout=5)

    app, store, submitted, runtimes = setup_runtime(
        tmp_path, before_transition=pause_before_start
    )
    tenant = "tenant-a"
    job_id = submitted[tenant]["job_id"]
    principal = Principal(tenant, "user-tenant-a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(runtimes[tenant].dispatch)
        assert entered.wait(timeout=5)
        try:
            cancel = app.cancel_job(principal, "project", job_id, "cancel-key")
        except ApplicationError as error:
            assert error.code == "cancellation_unsupported_in_phase"
            cancel = None
        finally:
            release.set()
        worker.result(timeout=10)

    drain(runtimes[tenant])
    final = store.get_snapshot(job_id, tenant_id=tenant)
    assert final.state is JobState.CANCELLED
    events = assert_monotonic_single_terminal(store, tenant, job_id, final.state)
    assert cancel["state"] == "cancel_requested"
    assert any(
        getattr(row.event, "to_state", None) is JobState.CANCEL_REQUESTED
        for row in events
    )


def test_cancel_racing_completion_commits_one_terminal_completion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered = Event()
    release = Event()
    paused = False

    def pause_before_collection(event):
        nonlocal paused
        if event.to_state is JobState.COLLECTING and not paused:
            paused = True
            entered.set()
            assert release.wait(timeout=5)

    app, store, submitted, runtimes = setup_runtime(
        tmp_path, before_transition=pause_before_collection
    )
    tenant = "tenant-a"
    job_id = submitted[tenant]["job_id"]
    principal = Principal(tenant, "user-tenant-a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(runtimes[tenant].dispatch)
        assert entered.wait(timeout=5)
        try:
            cancel = app.cancel_job(principal, "project", job_id, "cancel-key")
        finally:
            release.set()
        worker.result(timeout=10)

    drain(runtimes[tenant])
    assert cancel["state"] == "cancel_requested"
    events = assert_monotonic_single_terminal(store, tenant, job_id, JobState.SUCCEEDED)
    assert any(
        getattr(row.event, "to_state", None) is JobState.CANCEL_REQUESTED
        for row in events
    )


def test_failures_never_expose_approval_or_scientific_result(tmp_path):
    app, store, submitted, runtimes = setup_runtime(
        tmp_path, outcome=SyntheticOutcome.FAILED
    )
    tenant = "tenant-a"
    job_id = submitted[tenant]["job_id"]
    drain(runtimes[tenant])

    assert_monotonic_single_terminal(store, tenant, job_id, JobState.FAILED)
    status = app.job_status(Principal(tenant, "user-tenant-a"), "project", job_id)
    assert status["result_available"] is False
    assert all(report.outcome is SyntheticOutcome.FAILED for report in runtimes[tenant].reports)
    assert all("static result" not in report.result for report in runtimes[tenant].reports)
