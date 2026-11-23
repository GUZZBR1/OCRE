"""SQLite proof adapter for local atomicity and restart behavior only."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .jobs import (AdapterExecutionSnapshot, CommitReceipt,
                  IdempotencyConflict, JobSnapshot,
                  JobPersistencePort, OutboxMessage)
from . import codec
from ocre_core.jobs import ADAPTER_TERMINAL_STATES, TERMINAL_STATES


class PersistenceConflict(RuntimeError):
    """A sequence/head CAS lost or a persisted identity collided."""


@dataclass(frozen=True)
class PersistedEvent:
    tenant_id: str
    job_id: str
    sequence: int
    event: object
    cursor: int


class SQLiteJobStore(JobPersistencePort):
    """Local SQLite adapter; no PostgreSQL/cloud compatibility is implied."""

    def __init__(self, database: str | Path):
        self.database = str(database)
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots(tenant TEXT, kind TEXT, id TEXT, job_id TEXT, seq INTEGER, body BLOB, PRIMARY KEY(tenant,kind,id));
            CREATE TABLE IF NOT EXISTS events(cursor INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT, job_id TEXT, seq INTEGER, event_id TEXT, body BLOB, UNIQUE(tenant,event_id));
            CREATE TABLE IF NOT EXISTS receipts(tenant TEXT, operation TEXT, key TEXT, fingerprint TEXT, body BLOB, PRIMARY KEY(tenant,operation,key));
            CREATE TABLE IF NOT EXISTS outbox(message_id TEXT PRIMARY KEY, tenant TEXT, aggregate_id TEXT, event_id TEXT, topic TEXT, body BLOB, delivered INTEGER NOT NULL DEFAULT 0);
            """)

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @staticmethod
    def _dump(value): return sqlite3.Binary(codec.dumps(value))
    @staticmethod
    def _load(value): return codec.loads(value) if value is not None else None

    def _prior(self, db, idem):
        row = db.execute("SELECT fingerprint,body FROM receipts WHERE tenant=? AND operation=? AND key=?", (idem.tenant_id, idem.operation, idem.key)).fetchone()
        if row:
            if row[0] != idem.request_fingerprint: raise IdempotencyConflict(idem.key)
            receipt = self._load(row[1])
            return replace(receipt, replayed=True)
        return None

    def _commit(self, *, snapshot, event, expected_sequence, idempotency, outbox):
        if event.sequence != expected_sequence + 1 or snapshot.sequence != event.sequence:
            raise ValueError("snapshot/event sequence mismatch")
        agg_id = snapshot.job_id if isinstance(snapshot, JobSnapshot) else snapshot.execution_id
        kind = "job" if isinstance(snapshot, JobSnapshot) else "execution"
        tenant = snapshot.tenant_id
        if idempotency is not None and idempotency.tenant_id != tenant:
            raise ValueError("idempotency tenant differs from snapshot tenant")
        event_agg = getattr(event, "job_id", getattr(event, "execution_id", None))
        if event_agg != agg_id or outbox.aggregate_id != agg_id or outbox.event_id != event.event_id:
            raise ValueError("aggregate/event/outbox identity mismatch")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if idempotency:
                    prior = self._prior(db, idempotency)
                    if prior: db.rollback(); return prior
                row = db.execute("SELECT seq,body FROM snapshots WHERE tenant=? AND kind=? AND id=?", (tenant, kind, agg_id)).fetchone()
                if row is None or row[0] != expected_sequence: raise PersistenceConflict("stale or missing snapshot")
                old = self._load(row[1])
                if event.from_state != old.state: raise ValueError("event from_state differs from persisted snapshot")
                terminal_states = ADAPTER_TERMINAL_STATES if isinstance(old, AdapterExecutionSnapshot) else TERMINAL_STATES
                if old.state in terminal_states: raise ValueError("terminal snapshot is immutable")
                if snapshot.state != event.to_state: raise ValueError("snapshot state differs from event destination")
                origin = old.cancellation_origin
                if event.to_state.value == "cancel_requested":
                    if event.from_state.value not in {"queued", "running"} or event.cancellation_origin != old.state:
                        raise ValueError("cancel request origin does not match persisted state")
                    origin = old.state
                elif event.from_state.value == "cancel_requested":
                    if event.cancellation_origin != origin: raise ValueError("cancellation origin mismatch")
                    evidence=event.cancellation_evidence
                    if event.to_state.value == "cancelled":
                        expected_kind="worker_start_prevented" if origin.value == "queued" else "process_terminated_by_cancellation"
                        if evidence is None or evidence.aggregate_id != agg_id or evidence.requested_from_state != origin or evidence.kind.value != expected_kind:
                            raise ValueError("cancellation evidence does not match persisted request")
                    elif evidence is not None:
                        raise ValueError("cancellation evidence is only valid for cancelled outcome")
                elif event.cancellation_origin is not None:
                    raise ValueError("cancellation origin outside cancellation flow")
                elif event.cancellation_evidence is not None:
                    raise ValueError("cancellation evidence without persisted cancellation request")
                snapshot = replace(snapshot, cancellation_origin=origin)
                db.execute("UPDATE snapshots SET seq=?,body=? WHERE tenant=? AND kind=? AND id=?", (snapshot.sequence,self._dump(snapshot),tenant,kind,agg_id))
                db.execute("INSERT INTO events(tenant,job_id,seq,event_id,body) VALUES(?,?,?,?,?)", (tenant,snapshot.job_id,event.sequence,event.event_id,self._dump(event)))
                if idempotency: db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)",(tenant,idempotency.operation,idempotency.key,idempotency.request_fingerprint,self._dump(CommitReceipt(snapshot,event))))
                db.execute("INSERT INTO outbox(message_id,tenant,aggregate_id,event_id,topic,body) VALUES(?,?,?,?,?,?)",(outbox.message_id,tenant,outbox.aggregate_id,outbox.event_id,outbox.topic,outbox.payload))
                db.commit()
                return CommitReceipt(snapshot,event)
            except sqlite3.IntegrityError as exc:
                db.rollback(); raise PersistenceConflict("persisted identity collision") from exc
            except Exception:
                db.rollback(); raise

    def commit_transition(self, *, expected_sequence, snapshot, event, idempotency, outbox):
        return self._commit(snapshot=snapshot,event=event,expected_sequence=expected_sequence,idempotency=idempotency,outbox=outbox)

    def create_execution(self, *, job_snapshot, execution_snapshot, submitted_event, idempotency, outbox):
        if job_snapshot.state.value != "queued" or execution_snapshot.state.value != "queued" or job_snapshot.sequence != 1 or execution_snapshot.sequence != 1 or submitted_event.sequence != 1: raise ValueError("execution creation must start queued at sequence 1")
        if not job_snapshot.job_id or not execution_snapshot.execution_id or not submitted_event.event_id: raise ValueError("submission identity must not be empty")
        if submitted_event.occurred_at.tzinfo is None or submitted_event.occurred_at.utcoffset() is None: raise ValueError("submitted event must be timezone-aware")
        if job_snapshot.job_id != execution_snapshot.job_id or submitted_event.job_id != job_snapshot.job_id or submitted_event.execution_id != execution_snapshot.execution_id: raise ValueError("submission identity mismatch")
        tenant=job_snapshot.tenant_id
        if idempotency.tenant_id != tenant: raise ValueError("idempotency tenant differs from snapshot tenant")
        if execution_snapshot.tenant_id != tenant or outbox.aggregate_id != job_snapshot.job_id or outbox.event_id != submitted_event.event_id: raise ValueError("submission tenant/outbox identity mismatch")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                prior=self._prior(db,idempotency)
                if prior: db.rollback(); return prior
                for kind, ident, job, snap in (("job",job_snapshot.job_id,job_snapshot.job_id,job_snapshot),("execution",execution_snapshot.execution_id,execution_snapshot.job_id,execution_snapshot)):
                    db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",(tenant,kind,ident,job,1,self._dump(snap)))
                db.execute("INSERT INTO events(tenant,job_id,seq,event_id,body) VALUES(?,?,?,?,?)",(tenant,job_snapshot.job_id,1,submitted_event.event_id,self._dump(submitted_event)))
                receipt=CommitReceipt(job_snapshot,submitted_event)
                db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)",(tenant,idempotency.operation,idempotency.key,idempotency.request_fingerprint,self._dump(receipt)))
                db.execute("INSERT INTO outbox(message_id,tenant,aggregate_id,event_id,topic,body) VALUES(?,?,?,?,?,?)",(outbox.message_id,tenant,outbox.aggregate_id,outbox.event_id,outbox.topic,outbox.payload))
                db.commit(); return receipt
            except sqlite3.IntegrityError as exc: db.rollback(); raise PersistenceConflict("persisted identity collision") from exc
            except Exception: db.rollback(); raise

    def get_snapshot(self, job_id, *, tenant_id):
        with self._connect() as db: row=db.execute("SELECT body FROM snapshots WHERE tenant=? AND kind='job' AND id=?",(tenant_id,job_id)).fetchone()
        return self._load(row[0]) if row else None

    def get_execution_snapshot(self, execution_id, *, tenant_id):
        with self._connect() as db: row=db.execute("SELECT body FROM snapshots WHERE tenant=? AND kind='execution' AND id=?",(tenant_id,execution_id)).fetchone()
        return self._load(row[0]) if row else None

    def find_idempotency(self, *, tenant_id, operation, key):
        with self._connect() as db: row=db.execute("SELECT body FROM receipts WHERE tenant=? AND operation=? AND key=?",(tenant_id,operation,key)).fetchone()
        r=self._load(row[0]) if row else None
        return replace(r, replayed=True) if r else None

    def read_events(self, *, tenant_id, job_id, after_cursor=0, limit=100):
        if limit < 1: raise ValueError("limit must be positive")
        with self._connect() as db: rows=db.execute("SELECT cursor,seq,body FROM events WHERE tenant=? AND job_id=? AND cursor>? ORDER BY cursor LIMIT ?",(tenant_id,job_id,after_cursor,limit)).fetchall()
        return tuple(PersistedEvent(tenant_id,job_id,seq,self._load(body),cursor) for cursor,seq,body in rows)

    def pending_outbox(self, limit=100):
        with self._connect() as db: rows=db.execute("SELECT message_id,tenant,aggregate_id,event_id,topic,body FROM outbox WHERE delivered=0 ORDER BY rowid LIMIT ?",(limit,)).fetchall()
        return tuple(OutboxMessage(row[0], *row[2:]) for row in rows)

    def mark_delivered(self, message_id):
        with self._connect() as db: return db.execute("UPDATE outbox SET delivered=1 WHERE message_id=?",(message_id,)).rowcount == 1


class FakeOutboxDispatcher:
    """At-least-once fake publisher; injected failure leaves messages pending."""
    def __init__(self, store, publish: Callable[[OutboxMessage], None]): self.store,self.publish=store,publish
    def dispatch(self, limit=100):
        sent=0
        for message in self.store.pending_outbox(limit):
            if self.publish(message) is not False:
                self.store.mark_delivered(message.message_id)
                sent += 1
        return sent


class FakeMessageConsumer:
    """Dedupe fake consumer whose processed IDs survive process recreation."""
    def __init__(self, database, handle):
        self.database,self.handle=database,handle
        with sqlite3.connect(database) as db: db.execute("CREATE TABLE IF NOT EXISTS consumed(message_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('processing','processed')))")

    def recover_abandoned(self):
        """Make claims from an interrupted fake consumer eligible for redelivery.

        Call during controlled restart recovery, after the previous consumer has
        stopped. Concurrent live consumers must not invoke this operation.
        """
        with sqlite3.connect(self.database) as db:
            return db.execute("DELETE FROM consumed WHERE status='processing'").rowcount

    def consume(self, message):
        with sqlite3.connect(self.database) as db:
            try: db.execute("INSERT INTO consumed VALUES(?,'processing')",(message.message_id,))
            except sqlite3.IntegrityError: return False
        try:
            self.handle(message)
        except Exception:
            with sqlite3.connect(self.database) as db: db.execute("DELETE FROM consumed WHERE message_id=? AND status='processing'",(message.message_id,))
            raise
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE consumed SET status='processed' WHERE message_id=? AND status='processing'",(message.message_id,))
        return True
