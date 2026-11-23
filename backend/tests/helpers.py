"""Shared fixtures/helpers for backend tests (NOW, stubs, factories)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ocre_core.application.services import JobSubmission, OCREApplication, PreflightReport
from ocre_core.jobs import JobState
from ocre_core.persistence.jobs import JobSnapshot
from ocre_core.persistence.sqlite import SQLiteJobStore
from ocre_core.persistence.sync import SQLiteSyncStore
from ocre_core.validation.approval_fingerprint import jcs

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

    def evaluate(self, _request, **_ignored):
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


EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "schemas" / "eir" / "examples" / "0.2.0"
EIR_EXAMPLE_CAPS = {
    "calculix.batch": {
        "supported": True,
        "domains": ["structural"],
        "analyses": ["static_linear"],
        "target_versions": ["2.23"],
        "element_types": ["C3D10"],
        "element_orders": [2],
    }
}


def load_eir_example(name):
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


BUNDLE_MANIFEST_NAME = "input-bundle.json"


def stage_eir_example_bundle(root, eir=None, bundle=None, groups=None):
    """Materialize the 0.2.0 example bundle under ``root``.

    Each declared ``sha256`` is pinned to the bytes actually written, so
    the bundle passes pre-flight byte verification, and the manifest
    itself is staged at ``input-bundle.json`` so its exact bytes can be
    bound into the approval fingerprint. Caller-supplied ``eir``/
    ``bundle``/``groups`` (possibly mutated) are what the bytes encode.
    """
    root = Path(root)
    eir = eir if eir is not None else load_eir_example("eir.json")
    bundle = bundle if bundle is not None else load_eir_example("input-bundle.json")
    groups = groups if groups is not None else load_eir_example("mesh-group-manifest.json")
    root.mkdir(parents=True, exist_ok=True)
    for item in bundle["artifacts"]:
        target = root / item["uri"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if item["artifact_id"] == bundle["eir_artifact_id"]:
            contents = jcs(eir)
        elif item["artifact_id"] == "groups_axial_bar":
            contents = jcs(groups)
        else:
            contents = item["artifact_id"].encode()
        target.write_bytes(contents)
        item["sha256"] = hashlib.sha256(contents).hexdigest()
    # Written last so the staged manifest encodes the final artifact hashes.
    (root / BUNDLE_MANIFEST_NAME).write_bytes(jcs(bundle))
    return root, eir, bundle, groups


def eir_approval_context(caps=EIR_EXAMPLE_CAPS, adapters=None, **overrides):
    """A complete OCRE-AF/1 context for the 0.2.0 example case.

    ``capabilities_sha256`` is the sha256 of the JCS form of the
    capabilities document — the same bytes the validator binds.
    """
    context = {
        "validator": {
            "id": "ocre.validator",
            "version": "0.2.0",
            "implementation_sha256": "f" * 64,
            "policy_version": "1",
        },
        "adapters": [
            {
                "stage_id": "solve",
                "adapter_id": "calculix.batch",
                "version": "2.23",
                "binary_sha256": "d" * 64,
                "capabilities_sha256": hashlib.sha256(
                    jcs(caps["calculix.batch"])).hexdigest(),
            }
        ] if adapters is None else adapters,
        "preparation_policy": None,
    }
    return context | overrides
