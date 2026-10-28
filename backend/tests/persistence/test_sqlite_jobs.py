from datetime import datetime, timezone
from dataclasses import replace

import pytest

from ocre_core.jobs import (AdapterExecutionState, CancellationEvidence, CancellationEvidenceKind, JobState, transition_job)
from ocre_core.persistence.jobs import AdapterExecutionSnapshot, ExecutionSubmitted, IdempotencyConflict, IdempotencyRecord, JobSnapshot, OutboxMessage
from ocre_core.persistence.sqlite import FakeMessageConsumer, FakeOutboxDispatcher, PersistenceConflict, SQLiteJobStore

NOW=datetime(2026,1,1,tzinfo=timezone.utc)

def setup_store(tmp_path):
    store=SQLiteJobStore(tmp_path/"jobs.db")
    snap=JobSnapshot("j1",JobState.QUEUED,1,NOW)
    with store._connect() as db: db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",("default","job","j1","j1",1,store._dump(snap)))
    return store,snap

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
    with pytest.raises(Exception):
        store.create_execution(job_snapshot=JobSnapshot("j",JobState.QUEUED,1,NOW),execution_snapshot=AdapterExecutionSnapshot("x2","j",execution.state,1,NOW),submitted_event=ExecutionSubmitted("j","x2","submit-2",1,NOW),idempotency=IdempotencyRecord("default","submit","key2","fp","j"),outbox=OutboxMessage("m2","j","submit-2","jobs",b""))
    assert store.get_execution_snapshot("x2", tenant_id="default") is None

def test_stale_cas_and_event_collision_leave_no_partial_rows(tmp_path):
    store,snap=setup_store(tmp_path)
    ev=transition_job(job_id="j1",event_id="e1",current_state=snap.state,current_sequence=1,expected_sequence=1,target_state=JobState.PREPARING,occurred_at=NOW)
    with store._connect() as db: db.execute("UPDATE snapshots SET seq=2 WHERE tenant='default' AND kind='job' AND id='j1'")
    with pytest.raises(PersistenceConflict): store.commit_transition(expected_sequence=1,snapshot=JobSnapshot("j1",ev.to_state,2,NOW),event=ev,idempotency=None,outbox=OutboxMessage("m","j1","e1","t",b""))
    assert store.get_snapshot("j1", tenant_id="default").sequence==1

def test_outbox_retry_after_failure_and_consumer_dedup(tmp_path):
    store,snap=setup_store(tmp_path)
    ev=transition_job(job_id="j1",event_id="e1",current_state=snap.state,current_sequence=1,expected_sequence=1,target_state=JobState.PREPARING,occurred_at=NOW)
    store.commit_transition(expected_sequence=1,snapshot=JobSnapshot("j1",ev.to_state,2,NOW),event=ev,idempotency=None,outbox=OutboxMessage("m","j1","e1","t",b""))
    calls=[]
    def fail(_): raise RuntimeError("broker down")
    with pytest.raises(RuntimeError): FakeOutboxDispatcher(store,fail).dispatch()
    assert FakeOutboxDispatcher(store,calls.append).dispatch()==1
    assert FakeOutboxDispatcher(store,calls.append).dispatch()==0
    consumer=FakeMessageConsumer(tmp_path/"consumer.db",lambda m:calls.append(m))
    assert consumer.consume(calls[0]) is True
    assert FakeMessageConsumer(tmp_path/"consumer.db",lambda m:calls.append(m)).consume(calls[0]) is False

def test_consumer_failure_does_not_ack_and_can_resume(tmp_path):
    from ocre_core.persistence.jobs import OutboxMessage
    message=OutboxMessage("m-retry","j","e","t",b"")
    def fail(_): raise RuntimeError("consumer interrupted")
    with pytest.raises(RuntimeError): FakeMessageConsumer(tmp_path/"consumer.db",fail).consume(message)
    handled=[]
    assert FakeMessageConsumer(tmp_path/"consumer.db",handled.append).consume(message)
    assert handled == [message]

def test_consumer_recovers_abandoned_claim_after_restart(tmp_path):
    from ocre_core.persistence.jobs import OutboxMessage
    message=OutboxMessage("m-crash","j","e","t",b"")
    db=tmp_path/"consumer.db"
    with __import__("sqlite3").connect(db) as conn:
        conn.execute("CREATE TABLE consumed(message_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.execute("INSERT INTO consumed VALUES(?,'processing')",(message.message_id,))
    handled=[]
    consumer=FakeMessageConsumer(db,handled.append)
    assert consumer.recover_abandoned()==1
    assert consumer.consume(message)
    assert handled==[message]

def test_cancel_confirmation_must_match_persisted_origin_and_evidence(tmp_path):
    store,snap=setup_store(tmp_path)
    request=transition_job(job_id="j1",event_id="cancel-request",current_state=snap.state,current_sequence=1,expected_sequence=1,target_state=JobState.CANCEL_REQUESTED,occurred_at=NOW)
    store.commit_transition(expected_sequence=1,snapshot=JobSnapshot("j1",request.to_state,2,NOW),event=request,idempotency=None,outbox=OutboxMessage("m1","j1",request.event_id,"t",b""))
    confirmed=transition_job(job_id="j1",event_id="cancelled",current_state=JobState.CANCEL_REQUESTED,current_sequence=2,expected_sequence=2,target_state=JobState.CANCELLED,occurred_at=NOW,cancellation_origin=JobState.QUEUED,cancellation_evidence=CancellationEvidence("j1",JobState.QUEUED,CancellationEvidenceKind.WORKER_START_PREVENTED,NOW))
    invalid=replace(confirmed,cancellation_evidence=CancellationEvidence("someone-else",JobState.QUEUED,CancellationEvidenceKind.WORKER_START_PREVENTED,NOW))
    with pytest.raises(ValueError):
        store.commit_transition(expected_sequence=2,snapshot=JobSnapshot("j1",JobState.CANCELLED,3,NOW),event=invalid,idempotency=None,outbox=OutboxMessage("m2","j1",invalid.event_id,"t",b""))
    assert store.get_snapshot("j1", tenant_id="default").state is JobState.CANCEL_REQUESTED
