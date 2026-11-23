"""Shared fixtures/helpers for backend tests (NOW, stubs, factories)."""
from __future__ import annotations

from datetime import datetime, timezone

from ocre_core.application.services import JobSubmission, OCREApplication, PreflightReport
from ocre_core.jobs import JobState
from ocre_core.persistence.jobs import JobSnapshot
from ocre_core.persistence.sqlite import SQLiteJobStore
from ocre_core.persistence.sync import SQLiteSyncStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXED_CLOCK = lambda: NOW
READY = PreflightReport("valid", "ready")
BLOCKED_APPROVAL = PreflightReport(
    "incomplete", "blocked", ({"code": "approval_fingerprint_unavailable"},)
)


class RuleAuthorizer:
    """Authorizes via a caller-supplied rule: ``rule(principal, project_id)``."""

    def __init__(self, rule):
        self._rule = rule

    def authorize(self, principal, project_id, action):
        return self._rule(principal, project_id)


class FixedPreflight:
    """Always returns the injected report; counts evaluations."""

    def __init__(self, report=READY):
        self.report = report
        self.calls = 0

    def evaluate(self, _request):
        self.calls += 1
        return self.report


def application(tmp_path, *, authorizer, preflight, clock=None):
    kwargs = {} if clock is None else {"clock": clock}
    return OCREApplication(
        sync_store=SQLiteSyncStore(tmp_path / "sync.db"),
        job_store=SQLiteJobStore(tmp_path / "jobs.db"),
        authorizer=authorizer,
        preflight=preflight,
        **kwargs,
    )


def submission(**overrides):
    fields = dict(
        design_revision_id="rev1",
        case_ref="case",
        adapter_id="adapter",
        adapter_version="0",
        eir={"eir": 1},
        bundle={"bundle": 1},
    )
    return JobSubmission(**(fields | overrides))


def insert_snapshot(store: SQLiteJobStore, snapshot) -> None:
    kind = "job" if isinstance(snapshot, JobSnapshot) else "execution"
    ident = snapshot.job_id if isinstance(snapshot, JobSnapshot) else snapshot.execution_id
    with store._connect() as db:
        db.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
            (
                snapshot.tenant_id,
                kind,
                ident,
                snapshot.job_id,
                snapshot.sequence,
                store._dump(snapshot),
            ),
        )


def queued_job(store: SQLiteJobStore, job_id="j1", *, tenant="default") -> JobSnapshot:
    snap = JobSnapshot(job_id, JobState.QUEUED, 1, NOW, tenant_id=tenant)
    insert_snapshot(store, snap)
    return snap
