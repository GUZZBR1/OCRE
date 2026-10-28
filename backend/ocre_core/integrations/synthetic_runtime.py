"""Tenant-scoped local composition of application outbox and fake worker.

This runtime is a test harness only. It starts no process and connects no real
broker, solver, cloud service, or approval path.
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Callable

from ocre_core.integrations.synthetic_worker import (
    SyntheticOutcome,
    SyntheticReport,
    SyntheticWorker,
)
from ocre_core.persistence.jobs import OutboxMessage
from ocre_core.persistence.sqlite import (
    FakeMessageConsumer,
    FakeOutboxDispatcher,
    SQLiteJobStore,
)


class _TenantOutboxView:
    """Expose only this tenant/job's messages to the existing fake dispatcher.

    This proof harness is deliberately bound to the local SQLite adapter. The
    shared OutboxMessage has no tenant field, so the view uses SQLite's stored
    tenant column instead of scanning globally and guessing from event IDs.
    This is not a production dispatcher port.
    """

    def __init__(self, store: SQLiteJobStore, tenant_id: str, job_id: str):
        self._store = store
        self._tenant_id = tenant_id
        self._job_id = job_id

    def _event_ids(self) -> set[str]:
        after_cursor = 0
        event_ids: set[str] = set()
        while True:
            page = self._store.read_events(
                tenant_id=self._tenant_id,
                job_id=self._job_id,
                after_cursor=after_cursor,
                limit=1000,
            )
            if not page:
                return event_ids
            event_ids.update(row.event.event_id for row in page)
            after_cursor = page[-1].cursor

    def pending_outbox(self, limit=100):
        with self._store._connect() as db:
            rows = db.execute(
                "SELECT message_id,aggregate_id,event_id,topic,body FROM outbox "
                "WHERE tenant=? AND aggregate_id=? AND delivered=0 "
                "ORDER BY rowid LIMIT ?",
                (self._tenant_id, self._job_id, limit),
            ).fetchall()
        return tuple(OutboxMessage(*row) for row in rows)

    def owns(self, message: OutboxMessage) -> bool:
        with self._store._connect() as db:
            row = db.execute(
                "SELECT 1 FROM outbox WHERE message_id=? AND tenant=? "
                "AND aggregate_id=? AND event_id=? AND topic=?",
                (
                    message.message_id,
                    self._tenant_id,
                    self._job_id,
                    message.event_id,
                    message.topic,
                ),
            ).fetchone()
        return row is not None

    def mark_delivered(self, message_id: str) -> bool:
        return self._store.mark_delivered(message_id)


class SyntheticRuntime:
    """Compose dispatch, durable fake-consumer dedupe, and a synthetic worker."""

    def __init__(
        self,
        store: SQLiteJobStore,
        *,
        tenant_id: str,
        job_id: str,
        consumer_database: str | Path,
        outcome: SyntheticOutcome,
        clock,
        before_transition: Callable[[object], None] | None = None,
        after_transition: Callable[[object], None] | None = None,
    ):
        self.store = store
        self.tenant_id = tenant_id
        self.job_id = job_id
        self.outcome = outcome
        self._scoped_store = _TenantOutboxView(store, tenant_id, job_id)
        self._worker = SyntheticWorker(
            store,
            clock,
            before_transition=before_transition,
            after_transition=after_transition,
        )
        self.reports: list[SyntheticReport] = []
        self._report_lock = Lock()
        self._consumer = FakeMessageConsumer(consumer_database, self._handle)

    def _handle(self, message: OutboxMessage) -> None:
        # A callback can only name this aggregate and a tenant-scoped persisted
        # event. Unknown or cross-tenant messages fail closed.
        valid_event_ids = self._scoped_store._event_ids()
        if (
            not self._scoped_store.owns(message)
            or message.aggregate_id != self.job_id
            or message.event_id not in valid_event_ids
            or message.topic not in {"job.submitted", "job.cancel_requested", "synthetic.job.events"}
        ):
            raise LookupError("synthetic callback is outside tenant/job scope")
        if message.topic == "synthetic.job.events":
            return
        report = self._worker.run(
            tenant_id=self.tenant_id,
            job_id=self.job_id,
            outcome=self.outcome,
        )
        with self._report_lock:
            if not self.reports:
                self.reports.append(report)

    def pending_messages(self, limit=100) -> tuple[OutboxMessage, ...]:
        return self._scoped_store.pending_outbox(limit)

    def deliver(self, message: OutboxMessage) -> bool:
        """Deliver one callback, useful for explicit duplicate/redelivery tests."""
        return self._consumer.consume(message)

    def dispatch(self, limit=100) -> int:
        """Publish pending scoped messages at least once through the fake consumer."""
        dispatcher = FakeOutboxDispatcher(self._scoped_store, self._consumer.consume)
        return dispatcher.dispatch(limit)

    def recover_abandoned(self) -> int:
        """Recover processing claims after the previous fake consumer stopped."""
        return self._consumer.recover_abandoned()
