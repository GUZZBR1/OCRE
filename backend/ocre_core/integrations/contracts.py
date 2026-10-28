"""Adapter ports. Persistence and job-state mapping are supplied by callers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .models import CapabilityManifest


class AdapterExecutionState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    EXECUTION_FAILED = "execution_failed"
    COLLECTION_FAILED = "collection_failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversionReport:
    source_formats: tuple[str, ...] = ()
    target_formats: tuple[str, ...] = ()
    transformations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    discarded_data: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedBundle:
    bundle_id: str
    root: Path
    manifest_sha256: str
    conversion_report: ConversionReport


@dataclass(frozen=True)
class ExecutionRef:
    execution_id: str
    adapter_id: str


@dataclass(frozen=True)
class ExecutionEvent:
    execution_id: str
    sequence: int
    state: AdapterExecutionState
    occurred_at: datetime
    code: str | None = None


@dataclass(frozen=True)
class ExecutionStatus:
    execution: ExecutionRef
    state: AdapterExecutionState
    progress: float | None = None
    events: tuple[ExecutionEvent, ...] = ()
    failure_code: str | None = None


@dataclass(frozen=True)
class CancellationReport:
    accepted: bool
    state: AdapterExecutionState
    code: str | None = None


@dataclass(frozen=True)
class ResultArtifact:
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str | None = None


@dataclass(frozen=True)
class ResultManifest:
    execution: ExecutionRef
    input_manifest_sha256: str
    artifacts: tuple[ResultArtifact, ...]
    logs: tuple[str, ...] = ()
    diagnostic_only: bool = False


@dataclass(frozen=True)
class CleanupReport:
    removed: bool
    retained_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterError:
    code: str
    message: str
    retryable: bool = False
    details: tuple[tuple[str, str], ...] = ()


class ExecutionPort(Protocol):
    """Persistence/event port using the specialized adapter execution vocabulary.

    Implementations own idempotency, atomic state/event writes, restart recovery,
    and mapping to any broader platform job lifecycle.
    """

    def create_or_get(self, *, prepared: PreparedBundle, idempotency_key: str) -> ExecutionRef: ...

    def get_status(self, execution: ExecutionRef) -> ExecutionStatus: ...

    def request_cancel(self, execution: ExecutionRef) -> CancellationReport: ...


class Adapter(Protocol):
    def describe_capabilities(self) -> CapabilityManifest: ...

    def validate(self, request: Any, immutable_input_manifest: Any) -> ValidationReport: ...

    def prepare(self, request: Any, immutable_revision: Any) -> tuple[PreparedBundle, ConversionReport]: ...

    def submit(self, bundle: PreparedBundle, idempotency_key: str) -> ExecutionRef: ...

    def get_status(self, execution: ExecutionRef) -> ExecutionStatus: ...

    def cancel(self, execution: ExecutionRef) -> CancellationReport: ...

    def collect(self, execution: ExecutionRef) -> ResultManifest: ...

    def cleanup(self, execution: ExecutionRef) -> CleanupReport: ...
