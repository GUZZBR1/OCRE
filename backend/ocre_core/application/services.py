"""Framework-independent local application services."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from ocre_core.jobs import (
    AdapterExecutionState,
    CancellationUnsupportedInPhase,
    ConcurrentJobUpdate,
    InvalidJobTransition,
    JobState,
    transition_job,
)
from ocre_core.persistence.jobs import (
    AdapterExecutionSnapshot,
    ExecutionSubmitted,
    IdempotencyConflict,
    IdempotencyRecord,
    JobPersistencePort,
    JobSnapshot,
    OutboxMessage,
)
from ocre_core.persistence.sync import SQLiteSyncStore
from ocre_core.persistence.sqlite import PersistenceConflict
from ocre_core.sync import (
    BatchReceipt,
    SyncBatch,
    SyncBatchIdentityConflict,
    SyncConflict,
    SyncOperation,
    SyncReplay,
)


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    principal_id: str


class ProjectAuthorizer(Protocol):
    def authorize(self, principal: Principal, project_id: str, action: str) -> bool: ...


class LocalTenantJobPersistencePort(JobPersistencePort, Protocol):
    """Local persistence boundary with tenant-qualified reads and replay."""

    def get_snapshot(self, job_id: str, *, tenant_id: str) -> JobSnapshot | None: ...

    def read_events(self, *, tenant_id: str, job_id: str, after_cursor: int = 0, limit: int = 100) -> tuple[Any, ...]: ...


@dataclass(frozen=True)
class PreflightReport:
    status: str
    execution_readiness: str
    diagnostics: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "execution_readiness": self.execution_readiness,
            "diagnostics": list(self.diagnostics),
        }


class PreflightPort(Protocol):
    def evaluate(self, request: "JobSubmission") -> PreflightReport: ...


@dataclass(frozen=True)
class JobSubmission:
    design_revision_id: str
    case_ref: str
    adapter_id: str
    adapter_version: str
    eir: Any
    bundle: Any
    group_manifest: Any | None = None


class ValidatorPreflight:
    """Uses the current validator; no client-supplied capabilities are trusted."""

    def evaluate(self, request: JobSubmission) -> PreflightReport:
        from ocre_core.validation.validator import validate

        report = validate(
            request.eir,
            request.bundle,
            request.group_manifest,
            purpose="execution_preflight",
        )
        diagnostics = tuple(
            {"code": item.code, "pointer": item.pointer, "message": item.message}
            for item in report.diagnostics
        )
        # OCRE-AF/1 approval fingerprints are not available in this slice.
        # Even if a validator configuration reports otherwise, this real port
        # cannot assert execution readiness. Tests may inject a fake port.
        if not any(item["code"] == "approval_fingerprint_unavailable" for item in diagnostics):
            diagnostics += ({"code": "approval_fingerprint_unavailable", "pointer": "", "message": "OCRE-AF/1 approval fingerprint is unavailable"},)
        return PreflightReport(
            report.status,
            "blocked",
            diagnostics,
        )


class ApplicationError(Exception):
    def __init__(self, code: str, status: int, detail: str, **extra: Any):
        self.code, self.status, self.detail, self.extra = code, status, detail, extra
        super().__init__(detail)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class OCREApplication:
    def __init__(
        self,
        *,
        sync_store: SQLiteSyncStore,
        job_store: LocalTenantJobPersistencePort,
        authorizer: ProjectAuthorizer,
        preflight: PreflightPort | None = None,
        clock=lambda: datetime.now(timezone.utc),
    ):
        self.sync_store = sync_store
        self.job_store = job_store
        self.authorizer = authorizer
        self.preflight = preflight or ValidatorPreflight()
        self.clock = clock

    def _authorize(self, principal: Principal, project_id: str, action: str) -> None:
        if not self.authorizer.authorize(principal, project_id, action):
            raise ApplicationError("resource_not_found", 404, "Resource not found")

    def submit_sync(self, principal: Principal, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._authorize(principal, project_id, "sync:write")
        try:
            operations = tuple(
                SyncOperation(item["operation_id"], base64.b64decode(item["payload"], validate=True))
                for item in body["operations"]
            )
            batch = SyncBatch(
                project_id=project_id,
                batch_id=body["batch_id"],
                actor_id=principal.principal_id,
                base_revision_id=body.get("base_revision_id"),
                operations=operations,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ApplicationError("validation_failed", 422, "Invalid sync batch") from exc
        decision = self.sync_store.evaluate(batch, tenant_id=principal.tenant_id)
        if isinstance(decision, SyncConflict):
            raise ApplicationError("sync_conflict", 409, "Batch base revision is stale", current_head_revision_id=decision.current_head_revision_id)
        if isinstance(decision, SyncBatchIdentityConflict):
            raise ApplicationError("sync_batch_identity_conflict", 409, "Batch identity was reused")
        if isinstance(decision, SyncReplay):
            return self._receipt(decision.receipt, replayed=True)
        receipt = self.sync_store.commit(
            batch,
            tenant_id=principal.tenant_id,
            committed_revision_id=_new_id("rev"),
        )
        if isinstance(receipt, SyncBatchIdentityConflict):
            raise ApplicationError("sync_batch_identity_conflict", 409, "Batch identity was reused")
        if isinstance(receipt, SyncConflict):
            raise ApplicationError("sync_conflict", 409, "Batch base revision is stale", current_head_revision_id=receipt.current_head_revision_id)
        replayed = isinstance(receipt, SyncReplay)
        return self._receipt(receipt.receipt if replayed else receipt, replayed=replayed)

    @staticmethod
    def _receipt(receipt: BatchReceipt, *, replayed: bool) -> dict[str, Any]:
        return {
            "batch_id": receipt.batch.batch_id,
            "project_id": receipt.batch.project_id,
            "committed_revision_id": receipt.committed_revision_id,
            "replayed": replayed,
        }

    def get_sync_receipt(self, principal: Principal, project_id: str, batch_id: str) -> dict[str, Any]:
        self._authorize(principal, project_id, "sync:read")
        receipt = self.sync_store.lookup(tenant_id=principal.tenant_id, project_id=project_id, batch_id=batch_id)
        if receipt is None:
            raise ApplicationError("resource_not_found", 404, "Resource not found")
        return self._receipt(receipt, replayed=False)

    def submit_job(self, principal: Principal, project_id: str, request: JobSubmission, idempotency_key: str) -> dict[str, Any]:
        self._authorize(principal, project_id, "jobs:submit")
        # Readiness is evaluated before IDs, snapshots, events, receipts, or outbox writes.
        report = self.preflight.evaluate(request)
        if report.execution_readiness != "ready":
            raise ApplicationError("preparation_failed", 422, "Execution preflight is blocked", report=report.as_dict())
        fingerprint = _fingerprint({
            "project_id": project_id,
            "design_revision_id": request.design_revision_id,
            "case_ref": request.case_ref,
            "adapter_id": request.adapter_id,
            "adapter_version": request.adapter_version,
            "eir": request.eir,
            "bundle": request.bundle,
            "group_manifest": request.group_manifest,
        })
        job_id, execution_id, event_id = _new_id("job"), _new_id("exec"), _new_id("evt")
        now = self.clock()
        payload = json.dumps({
            "project_id": project_id,
            "design_revision_id": request.design_revision_id,
            "case_ref": request.case_ref,
            "adapter_id": request.adapter_id,
            "adapter_version": request.adapter_version,
        }, sort_keys=True).encode()
        job = JobSnapshot(job_id, JobState.QUEUED, 1, now, payload, tenant_id=principal.tenant_id)
        execution = AdapterExecutionSnapshot(execution_id, job_id, AdapterExecutionState.QUEUED, 1, now, payload, tenant_id=principal.tenant_id)
        event = ExecutionSubmitted(job_id, execution_id, event_id, 1, now)
        idem = IdempotencyRecord(principal.tenant_id, "job-submit-v1", idempotency_key, fingerprint, job_id)
        outbox = OutboxMessage(_new_id("msg"), job_id, event_id, "job.submitted", b"")
        try:
            receipt = self.job_store.create_execution(
                job_snapshot=job,
                execution_snapshot=execution,
                submitted_event=event,
                idempotency=idem,
                outbox=outbox,
            )
        except IdempotencyConflict as exc:
            raise ApplicationError("idempotency_key_reused", 409, "Idempotency key was reused") from exc
        return {
            "job_id": receipt.snapshot.job_id,
            "project_id": project_id,
            "state": "queued",
            "sequence": receipt.snapshot.sequence,
            "submitted_event_id": receipt.event.event_id,
            "replayed": receipt.replayed,
        }

    def _job(self, principal: Principal, project_id: str, job_id: str, action: str) -> tuple[JobSnapshot, dict[str, Any]]:
        self._authorize(principal, project_id, action)
        snapshot = self.job_store.get_snapshot(job_id, tenant_id=principal.tenant_id)
        if snapshot is None:
            raise ApplicationError("resource_not_found", 404, "Resource not found")
        try:
            metadata = json.loads(snapshot.payload or b"{}")
        except (TypeError, ValueError):
            raise ApplicationError("resource_not_found", 404, "Resource not found")
        if metadata.get("project_id") != project_id:
            raise ApplicationError("resource_not_found", 404, "Resource not found")
        return snapshot, metadata

    def job_status(self, principal: Principal, project_id: str, job_id: str) -> dict[str, Any]:
        snapshot, metadata = self._job(principal, project_id, job_id, "jobs:read")
        return {
            "job_id": job_id,
            "project_id": project_id,
            "state": snapshot.state.value,
            "sequence": snapshot.sequence,
            "updated_at": snapshot.updated_at.isoformat(),
            "design_revision_id": metadata.get("design_revision_id"),
            "result_available": False,
        }

    def cancel_job(self, principal: Principal, project_id: str, job_id: str, idempotency_key: str) -> dict[str, Any]:
        snapshot, _ = self._job(principal, project_id, job_id, "jobs:cancel")
        prior = self.job_store.find_idempotency(tenant_id=principal.tenant_id, operation="job-cancel-v1", key=idempotency_key)
        if prior is not None:
            if prior.snapshot.job_id != job_id:
                raise ApplicationError("idempotency_key_reused", 409, "Idempotency key was reused")
            return {"job_id": job_id, "state": prior.snapshot.state.value, "sequence": prior.snapshot.sequence, "replayed": True}
        if snapshot.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
            return {"job_id": job_id, "state": snapshot.state.value, "sequence": snapshot.sequence, "replayed": False}
        now = self.clock()
        try:
            event = transition_job(
                job_id=job_id,
                event_id=_new_id("evt"),
                current_state=snapshot.state,
                current_sequence=snapshot.sequence,
                expected_sequence=snapshot.sequence,
                target_state=JobState.CANCEL_REQUESTED,
                occurred_at=now,
            )
        except CancellationUnsupportedInPhase as exc:
            raise ApplicationError("cancellation_unsupported_in_phase", 409, "Cancellation is unsupported in this job phase") from exc
        except InvalidJobTransition as exc:
            raise ApplicationError("cancellation_unsupported_in_phase", 409, "Job cannot be cancelled in its current phase") from exc
        updated = JobSnapshot(job_id, event.to_state, event.sequence, now, snapshot.payload, tenant_id=principal.tenant_id)
        idem = IdempotencyRecord(principal.tenant_id, "job-cancel-v1", idempotency_key, _fingerprint({"project_id": project_id, "job_id": job_id}), job_id)
        outbox = OutboxMessage(_new_id("msg"), job_id, event.event_id, "job.cancel_requested", b"")
        try:
            receipt = self.job_store.commit_transition(expected_sequence=snapshot.sequence, snapshot=updated, event=event, idempotency=idem, outbox=outbox)
        except IdempotencyConflict as exc:
            raise ApplicationError("idempotency_key_reused", 409, "Idempotency key was reused") from exc
        except (ConcurrentJobUpdate, PersistenceConflict) as exc:
            raise ApplicationError("sequence_conflict", 409, "Job sequence changed; reload the current status") from exc
        return {"job_id": job_id, "state": receipt.snapshot.state.value, "sequence": receipt.snapshot.sequence, "replayed": receipt.replayed}

    def replay_events(self, principal: Principal, project_id: str, job_id: str, after_cursor: int, limit: int) -> dict[str, Any]:
        self._job(principal, project_id, job_id, "events:read")
        if after_cursor < 0 or not 1 <= limit <= 200:
            raise ApplicationError("validation_failed", 422, "Invalid replay page")
        events = self.job_store.read_events(tenant_id=principal.tenant_id, job_id=job_id, after_cursor=after_cursor, limit=limit + 1)
        page = events[:limit]
        return {
            "items": [
                {"cursor": row.cursor, "sequence": row.sequence, "event": _event_dict(row.event)}
                for row in page
            ],
            "next_cursor": page[-1].cursor if page else after_cursor,
            "has_more": len(events) > limit,
        }


def _event_dict(event: Any) -> dict[str, Any]:
    def serialize(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        if is_dataclass(value) and not isinstance(value, type):
            return {field.name: serialize(getattr(value, field.name)) for field in fields(value)}
        if isinstance(value, dict):
            return {key: serialize(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [serialize(item) for item in value]
        return value

    if is_dataclass(event) and not isinstance(event, type):
        return serialize(event)
    return {"type": type(event).__name__}
