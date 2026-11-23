import sqlite3
from dataclasses import replace
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ocre_core.jobs import (AdapterExecutionState, CancellationEvidence, CancellationEvidenceKind, JobState, JobTransition, transition_adapter_execution, transition_job)
from ocre_core.persistence.jobs import AdapterExecutionSnapshot, ExecutionSubmitted, IdempotencyConflict, IdempotencyRecord, JobSnapshot, OutboxMessage
from ocre_core.persistence.sqlite import FakeMessageConsumer, FakeOutboxDispatcher, PersistenceConflict, SQLiteJobStore

from tests.helpers import NOW, insert_snapshot, queued_job

def setup_store(tmp_path):
    store=SQLiteJobStore(tmp_path/"jobs.db")
    snap=queued_job(store)
    return store,snap

def kick(store, snap, target, event_id="e1"):
    """Transition j1 one step from snap's state and commit it (shared setup)."""
    event=transition_job(job_id="j1",event_id=event_id,current_state=snap.state,current_sequence=1,expected_sequence=1,target_state=target,occurred_at=NOW)
    store.commit_transition(expected_sequence=1,snapshot=JobSnapshot("j1",event.to_state,2,NOW),event=event,idempotency=None,outbox=OutboxMessage("m1","j1",event.event_id,"t",b""))

def test_transition_atomic_idempotency_restart_and_pages(tmp_path):
    store,snap=setup_store(tmp_path)
    event=transition_job(job_id="j1",event_id="e2",current_state=snap.state,current_sequence=1,expected_sequence=1,target_state=JobState.PREPARING,occurred_at=NOW)
    new=JobSnapshot("j1",event.to_state,event.sequence,NOW)
    idem=IdempotencyRecord("default","transition","k","fp","j1")
    out=OutboxMessage("m2","j1","e2","jobs",b"x")
    r=store.commit_transition(expected_sequence=1,snapshot=new,event=event,idempotency=idem,outbox=out)
    assert r.snapshot.sequence == 2
    assert store.commit_transition(expected_sequence=1,snapshot=new,event=event,idempotency=idem,outbox=out).replayed
    with pytest.raises(IdempotencyConflict):
        store.commit_transition(expected_sequence=1,snapshot=new,event=event,idempotency=IdempotencyRecord("default","transition","k","different","j1"),outbox=out)
    restarted=SQLiteJobStore(tmp_path/"jobs.db")
    assert restarted.get_snapshot("j1", tenant_id="default").state is JobState.PREPARING
    first=restarted.read_events(tenant_id="default",job_id="j1",limit=1)
    assert len(first)==1 and first[0].event.event_id=="e2"
    assert restarted.read_events(tenant_id="other",job_id="j1")==()
    assert restarted.read_events(tenant_id="default",job_id="other-job")==()

def test_create_execution_is_atomic_and_replayable(tmp_path):
    store=SQLiteJobStore(tmp_path/"jobs.db")
    job=JobSnapshot("j",JobState.QUEUED,1,NOW)
    execution=AdapterExecutionSnapshot("x","j",AdapterExecutionState.QUEUED,1,NOW)
    event=ExecutionSubmitted("j","x","submit-1",1,NOW)
    idem=IdempotencyRecord("default","submit","key","fingerprint","j")
    outbox=OutboxMessage("submit-message","j","submit-1","jobs",b"")
    assert store.create_execution(job_snapshot=job,execution_snapshot=execution,submitted_event=event,idempotency=idem,outbox=outbox).event==event
    assert store.create_execution(job_snapshot=job,execution_snapshot=execution,submitted_event=event,idempotency=idem,outbox=outbox).replayed
    assert store.get_execution_snapshot("x", tenant_id="default").state.value=="queued"
    with pytest.raises(PersistenceConflict):
        store.create_execution(job_snapshot=JobSnapshot("j",JobState.QUEUED,1,NOW),execution_snapshot=AdapterExecutionSnapshot("x2","j",execution.state,1,NOW),submitted_event=ExecutionSubmitted("j","x2","submit-2",1,NOW),idempotency=IdempotencyRecord("default","submit","key2","fp","j"),outbox=OutboxMessage("m2","j","submit-2","jobs",b""))
    assert store.get_execution_snapshot("x2", tenant_id="default") is None

def test_committed_event_id_collision_surfaces_as_persistence_conflict(tmp_path):
    store,snap=setup_store(tmp_path)
    kick(store, snap, JobState.PREPARING, "e1")
    event=transition_job(job_id="j1",event_id="e1",current_state=JobState.PREPARING,current_sequence=2,expected_sequence=2,target_state=JobState.RUNNING,occurred_at=NOW)
    with pytest.raises(PersistenceConflict):
        store.commit_transition(expected_sequence=2,snapshot=JobSnapshot("j1",event.to_state,3,NOW),event=event,idempotency=None,outbox=OutboxMessage("m2","j1","e1","t",b""))
    assert store.get_snapshot("j1", tenant_id="default").state is JobState.PREPARING

def test_commit_rejects_cancellation_origin_outside_cancel_flow(tmp_path):
    store,snap=setup_store(tmp_path)
    event=transition_job(job_id="j1",event_id="e1",current_state=snap.state,current_sequence=1,expected_sequence=1,target_state=JobState.PREPARING,occurred_at=NOW)
    forged=replace(event,cancellation_origin=JobState.RUNNING)
    with pytest.raises(ValueError):
        store.commit_transition(expected_sequence=1,snapshot=JobSnapshot("j1",forged.to_state,2,NOW),event=forged,idempotency=None,outbox=OutboxMessage("m1","j1",forged.event_id,"t",b""))
    assert store.get_snapshot("j1", tenant_id="default").state is JobState.QUEUED
    assert store.read_events(tenant_id="default", job_id="j1") == ()

def test_create_execution_validates_submitted_event_shape(tmp_path):
    store=SQLiteJobStore(tmp_path/"jobs.db")
    naive=datetime(2026,1,1)
    for job_id, execution_id, event_id, sequence, occurred in (
        ("", "x", "e1", 1, NOW),
        ("j", "", "e1", 1, NOW),
        ("j", "x", "", 1, NOW),
        ("j", "x", "e1", 2, NOW),
        ("j", "x", "e1", 1, naive),
    ):
        with pytest.raises(ValueError):
            store.create_execution(
                job_snapshot=JobSnapshot(job_id,JobState.QUEUED,1,NOW),
                execution_snapshot=AdapterExecutionSnapshot(execution_id,job_id,AdapterExecutionState.QUEUED,1,NOW),
                submitted_event=ExecutionSubmitted(job_id,execution_id,event_id,sequence,occurred),
                idempotency=IdempotencyRecord("default","submit","key","fp",job_id),
                outbox=OutboxMessage("m",job_id,event_id,"t",b""))
    assert store.get_snapshot("j",tenant_id="default") is None

def test_stale_cas_and_event_collision_leave_no_partial_rows(tmp_path):
    store,snap=setup_store(tmp_path)
    with store._connect() as db: db.execute("UPDATE snapshots SET seq=2 WHERE tenant='default' AND kind='job' AND id='j1'")
    with pytest.raises(PersistenceConflict): kick(store, snap, JobState.PREPARING)
    assert store.get_snapshot("j1", tenant_id="default").sequence==1

def test_outbox_retry_after_failure_and_consumer_dedup(tmp_path):
    store,snap=setup_store(tmp_path)
    kick(store, snap, JobState.PREPARING)
    calls=[]
    def fail(_): raise RuntimeError("broker down")
    with pytest.raises(RuntimeError): FakeOutboxDispatcher(store,fail).dispatch()
    assert FakeOutboxDispatcher(store,calls.append).dispatch()==1
    assert FakeOutboxDispatcher(store,calls.append).dispatch()==0
    consumer=FakeMessageConsumer(tmp_path/"consumer.db",lambda m:calls.append(m))
    assert consumer.consume(calls[0]) is True
    assert FakeMessageConsumer(tmp_path/"consumer.db",lambda m:calls.append(m)).consume(calls[0]) is False


def test_publish_before_ack_crash_redelivers_duplicate(tmp_path):
    store, snap = setup_store(tmp_path)
    kick(store, snap, JobState.PREPARING)
    published = []
    original_ack = store.mark_delivered

    def crash_before_ack(_message_id):
        raise RuntimeError("dispatcher crashed after publish")

    store.mark_delivered = crash_before_ack
    with pytest.raises(RuntimeError, match="after publish"):
        FakeOutboxDispatcher(store, published.append).dispatch()
    store.mark_delivered = original_ack

    assert FakeOutboxDispatcher(store, published.append).dispatch() == 1
    assert [message.message_id for message in published] == ["m1", "m1"]
    assert FakeOutboxDispatcher(store, published.append).dispatch() == 0


def test_dispatch_marks_delivered_only_when_publisher_acks(tmp_path):
    store,snap=setup_store(tmp_path)
    kick(store, snap, JobState.PREPARING)
    assert FakeOutboxDispatcher(store, lambda message: False).dispatch()==0
    assert len(store.pending_outbox())==1
    delivered=[]
    assert FakeOutboxDispatcher(store, delivered.append).dispatch()==1
    assert delivered[0].message_id=="m1"
    assert store.pending_outbox()==()


def test_identical_job_ids_are_tenant_scoped_for_snapshots_and_event_replay(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    for tenant in ("tenant-a", "tenant-b"):
        insert_snapshot(store, JobSnapshot("shared-job", JobState.QUEUED, 1, NOW, tenant_id=tenant))
        event = transition_job(
            job_id="shared-job", event_id=f"{tenant}-event",
            current_state=JobState.QUEUED, current_sequence=1,
            expected_sequence=1, target_state=JobState.PREPARING,
            occurred_at=NOW,
        )
        store.commit_transition(
            expected_sequence=1,
            snapshot=JobSnapshot("shared-job", event.to_state, 2, NOW, tenant_id=tenant),
            event=event, idempotency=None,
            outbox=OutboxMessage(f"{tenant}-message", "shared-job", event.event_id, "jobs", b""),
        )

    assert store.get_snapshot("shared-job", tenant_id="tenant-a").state is JobState.PREPARING
    assert store.get_snapshot("shared-job", tenant_id="tenant-b").state is JobState.PREPARING
    tenant_a_events = store.read_events(tenant_id="tenant-a", job_id="shared-job")
    tenant_b_events = store.read_events(tenant_id="tenant-b", job_id="shared-job")
    assert [item.event.event_id for item in tenant_a_events] == ["tenant-a-event"]
    assert [item.event.event_id for item in tenant_b_events] == ["tenant-b-event"]

def test_consumer_failure_does_not_ack_and_can_resume(tmp_path):
    message=OutboxMessage("m-retry","j","e","t",b"")
    def fail(_): raise RuntimeError("consumer interrupted")
    with pytest.raises(RuntimeError): FakeMessageConsumer(tmp_path/"consumer.db",fail).consume(message)
    handled=[]
    assert FakeMessageConsumer(tmp_path/"consumer.db",handled.append).consume(message)
    assert handled == [message]

def test_consumer_recovers_abandoned_claim_after_restart(tmp_path):
    message=OutboxMessage("m-crash","j","e","t",b"")
    db=tmp_path/"consumer.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE consumed(message_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.execute("INSERT INTO consumed VALUES(?,'processing')",(message.message_id,))
    handled=[]
    consumer=FakeMessageConsumer(db,handled.append)
    assert consumer.recover_abandoned()==1
    assert consumer.consume(message)
    assert handled==[message]

def test_cancel_confirmation_must_match_persisted_origin_and_evidence(tmp_path):
    store,snap=setup_store(tmp_path)
    kick(store, snap, JobState.CANCEL_REQUESTED, "cancel-request")
    confirmed=transition_job(job_id="j1",event_id="cancelled",current_state=JobState.CANCEL_REQUESTED,current_sequence=2,expected_sequence=2,target_state=JobState.CANCELLED,occurred_at=NOW,cancellation_origin=JobState.QUEUED,cancellation_evidence=CancellationEvidence("j1",JobState.QUEUED,CancellationEvidenceKind.WORKER_START_PREVENTED,NOW))
    invalid=replace(confirmed,cancellation_evidence=CancellationEvidence("someone-else",JobState.QUEUED,CancellationEvidenceKind.WORKER_START_PREVENTED,NOW))
    with pytest.raises(ValueError):
        store.commit_transition(expected_sequence=2,snapshot=JobSnapshot("j1",JobState.CANCELLED,3,NOW),event=invalid,idempotency=None,outbox=OutboxMessage("m2","j1",invalid.event_id,"t",b""))
    assert store.get_snapshot("j1", tenant_id="default").state is JobState.CANCEL_REQUESTED


def test_cancel_confirmation_racing_completion_has_one_linearized_winner(tmp_path):
    store, snap = setup_store(tmp_path)
    kick(store, snap, JobState.CANCEL_REQUESTED, "cancel-request")
    barrier = Barrier(2)

    def candidate(target, event_id, evidence=None):
        event = transition_job(
            job_id="j1", event_id=event_id,
            current_state=JobState.CANCEL_REQUESTED, current_sequence=2,
            expected_sequence=2, target_state=target, occurred_at=NOW,
            cancellation_origin=JobState.QUEUED,
            cancellation_evidence=evidence,
        )
        barrier.wait()
        return store.commit_transition(
            expected_sequence=2,
            snapshot=JobSnapshot("j1", target, 3, NOW), event=event,
            idempotency=None,
            outbox=OutboxMessage(f"m-{event_id}", "j1", event_id, "jobs", b""),
        )

    cancelled = CancellationEvidence(
        "j1", JobState.QUEUED, CancellationEvidenceKind.WORKER_START_PREVENTED, NOW
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(candidate, JobState.CANCELLED, "cancelled", cancelled),
            pool.submit(candidate, JobState.FAILED, "completed", None),
        ]
    results = []
    for future in futures:
        try:
            results.append(future.result())
        except PersistenceConflict:
            results.append(None)

    assert sum(result is not None for result in results) == 1
    final = store.get_snapshot("j1", tenant_id="default")
    assert final.state in {JobState.CANCELLED, JobState.FAILED}
    events = store.read_events(tenant_id="default", job_id="j1", after_cursor=0, limit=10)
    assert [item.sequence for item in events] == [2, 3]
    assert len({item.event.event_id for item in events}) == 2


def test_concurrent_same_key_cancel_commits_once_and_replays(tmp_path):
    store, snap = setup_store(tmp_path)
    event = transition_job(
        job_id="j1", event_id="cancel-request", current_state=snap.state,
        current_sequence=1, expected_sequence=1,
        target_state=JobState.CANCEL_REQUESTED, occurred_at=NOW,
    )
    snapshot = JobSnapshot("j1", event.to_state, event.sequence, NOW)
    idempotency = IdempotencyRecord("default", "cancel", "cancel-key", "same-request", "j1")
    outbox = OutboxMessage("cancel-message", "j1", event.event_id, "jobs", b"")
    barrier = Barrier(2)

    def cancel():
        barrier.wait()
        return store.commit_transition(
            expected_sequence=1, snapshot=snapshot, event=event,
            idempotency=idempotency, outbox=outbox,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(cancel), pool.submit(cancel))]

    assert sorted(result.replayed for result in results) == [False, True]
    assert store.get_snapshot("j1", tenant_id="default").state is JobState.CANCEL_REQUESTED
    assert [item.event.event_id for item in store.read_events(tenant_id="default", job_id="j1")] == [
        "cancel-request"
    ]


def test_persistence_rejects_forged_transition_from_terminal_snapshot(tmp_path):
    store, _ = setup_store(tmp_path)
    terminal = JobSnapshot("j1", JobState.SUCCEEDED, 1, NOW)
    with store._connect() as db:
        db.execute(
            "UPDATE snapshots SET body=? WHERE tenant='default' AND kind='job' AND id='j1'",
            (store._dump(terminal),),
        )
    forged = transition_job(
        job_id="j1", event_id="forged-terminal-rewrite",
        current_state=JobState.RUNNING, current_sequence=1, expected_sequence=1,
        target_state=JobState.FAILED, occurred_at=NOW,
    )
    forged = replace(forged, from_state=JobState.SUCCEEDED)

    with pytest.raises(ValueError, match="terminal snapshot is immutable"):
        store.commit_transition(
            expected_sequence=1,
            snapshot=JobSnapshot("j1", JobState.FAILED, 2, NOW),
            event=forged, idempotency=None,
            outbox=OutboxMessage("forged-message", "j1", forged.event_id, "jobs", b""),
        )

    assert store.get_snapshot("j1", tenant_id="default").state is JobState.SUCCEEDED
    assert store.read_events(tenant_id="default", job_id="j1") == ()


def test_persistence_rejects_forged_transition_from_terminal_adapter_snapshot(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    terminal = AdapterExecutionSnapshot("x1", "j1", AdapterExecutionState.EXECUTION_FAILED, 1, NOW)
    insert_snapshot(store, terminal)
    forged = transition_adapter_execution(
        execution_id="x1", event_id="forged-adapter-terminal-rewrite",
        current_state=AdapterExecutionState.RUNNING, current_sequence=1,
        expected_sequence=1, target_state=AdapterExecutionState.EXECUTION_FAILED,
        occurred_at=NOW,
    )
    forged = replace(forged, from_state=AdapterExecutionState.EXECUTION_FAILED)

    with pytest.raises(ValueError, match="terminal snapshot is immutable"):
        store.commit_transition(
            expected_sequence=1,
            snapshot=AdapterExecutionSnapshot(
                "x1", "j1", AdapterExecutionState.COLLECTION_FAILED, 2, NOW
            ),
            event=forged, idempotency=None,
            outbox=OutboxMessage("forged-adapter-message", "x1", forged.event_id, "jobs", b""),
        )

    assert store.get_execution_snapshot("x1", tenant_id="default").state is AdapterExecutionState.EXECUTION_FAILED
    assert store.read_events(tenant_id="default", job_id="j1") == ()


def test_persistence_rejects_self_transition_events(tmp_path):
    store, snap = setup_store(tmp_path)
    noop = JobTransition("j1", "self-loop", 2, JobState.QUEUED, JobState.QUEUED, NOW)
    with pytest.raises(ValueError, match="must change state"):
        store.commit_transition(
            expected_sequence=1,
            snapshot=JobSnapshot("j1", JobState.QUEUED, 2, NOW),
            event=noop, idempotency=None,
            outbox=OutboxMessage("m-self", "j1", "self-loop", "t", b""),
        )
    assert store.get_snapshot("j1", tenant_id="default").sequence == 1
    assert store.read_events(tenant_id="default", job_id="j1") == ()


def test_commit_transition_rejects_wrong_port_record_types(tmp_path):
    store, snap = setup_store(tmp_path)
    event = transition_job(
        job_id="j1", event_id="e2", current_state=snap.state,
        current_sequence=1, expected_sequence=1,
        target_state=JobState.PREPARING, occurred_at=NOW,
    )
    base = dict(
        snapshot=JobSnapshot("j1", event.to_state, 2, NOW),
        event=event, idempotency=None,
        outbox=OutboxMessage("m2", "j1", "e2", "t", b""),
    )
    for key, bad in (
        ("snapshot", "j1"),
        ("event", "e2"),
        ("event", ExecutionSubmitted("j1", "x", "e2", 2, NOW)),
        ("outbox", "m2"),
        ("idempotency", "not-a-record"),
    ):
        with pytest.raises(ValueError, match="port types"):
            store.commit_transition(expected_sequence=1, **(base | {key: bad}))
    assert store.read_events(tenant_id="default", job_id="j1") == ()


def test_commit_transition_rejects_naive_event_timestamp_and_bad_outbox(tmp_path):
    store, snap = setup_store(tmp_path)
    event = transition_job(
        job_id="j1", event_id="e2", current_state=snap.state,
        current_sequence=1, expected_sequence=1,
        target_state=JobState.PREPARING, occurred_at=NOW,
    )
    base = dict(
        snapshot=JobSnapshot("j1", event.to_state, 2, NOW),
        event=event, idempotency=None,
        outbox=OutboxMessage("m2", "j1", "e2", "t", b""),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        store.commit_transition(expected_sequence=1, **(base | {"event": replace(event, occurred_at=datetime(2026, 1, 1))}))
    with pytest.raises(ValueError):
        store.commit_transition(expected_sequence=1, **(base | {"outbox": replace(base["outbox"], message_id="")}))
    assert store.read_events(tenant_id="default", job_id="j1") == ()


def test_create_execution_rejects_wrong_port_record_types(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.db")
    base = dict(
        job_snapshot=JobSnapshot("j", JobState.QUEUED, 1, NOW),
        execution_snapshot=AdapterExecutionSnapshot("x", "j", AdapterExecutionState.QUEUED, 1, NOW),
        submitted_event=ExecutionSubmitted("j", "x", "e", 1, NOW),
        idempotency=IdempotencyRecord("default", "submit", "k", "f", "j"),
        outbox=OutboxMessage("m", "j", "e", "t", b""),
    )
    for key, bad in (
        ("job_snapshot", "j"),
        ("execution_snapshot", base["job_snapshot"]),
        ("submitted_event", "e"),
        ("idempotency", None),
        ("outbox", "m"),
    ):
        with pytest.raises(ValueError):
            store.create_execution(**(base | {key: bad}))
    assert store.get_snapshot("j", tenant_id="default") is None


def test_event_pages_and_outbox_drain_require_positive_limits(tmp_path):
    store, _ = setup_store(tmp_path)
    with pytest.raises(ValueError):
        store.pending_outbox(0)
    with pytest.raises(ValueError):
        store.pending_outbox(-1)
    with pytest.raises(ValueError):
        store.read_events(tenant_id="default", job_id="j1", limit=0)
    with pytest.raises(ValueError):
        store.read_events(tenant_id="default", job_id="j1", after_cursor=-1)


def test_connect_closes_connection_on_exit_and_on_error(tmp_path):
    store=SQLiteJobStore(tmp_path/"jobs.db")
    with store._connect() as db: pass
    with pytest.raises(sqlite3.ProgrammingError): db.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with store._connect() as db: raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError): db.execute("SELECT 1")
