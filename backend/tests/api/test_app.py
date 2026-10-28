from fastapi.testclient import TestClient

from ocre_core.api.app import create_app
from ocre_core.application.services import OCREApplication, PreflightReport, Principal
from ocre_core.persistence.sqlite import SQLiteJobStore
from ocre_core.persistence.sync import SQLiteSyncStore


class ProjectAccess:
    def authorize(self, principal, project_id, action):
        return principal.tenant_id == "tenant-a" and project_id == "project-a"


class Blocked:
    def evaluate(self, _request):
        return PreflightReport("incomplete", "blocked", ({"code": "approval_fingerprint_unavailable"},))


def test_local_transport_injects_principal_and_exposes_only_six_operations(tmp_path):
    principal = Principal("tenant-a", "actor-a")
    service = OCREApplication(
        sync_store=SQLiteSyncStore(tmp_path / "sync.db"),
        job_store=SQLiteJobStore(tmp_path / "jobs.db"),
        authorizer=ProjectAccess(),
        preflight=Blocked(),
    )
    app = create_app(service, principal_dependency=lambda: principal)
    client = TestClient(app)
    operations = {
        (method, route.path)
        for route in app.routes
        if route.path.startswith("/v1/")
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST"}
    }
    assert operations == {
        ("POST", "/v1/projects/{project_id}/sync-batches"),
        ("GET", "/v1/projects/{project_id}/sync-batches/{batch_id}"),
        ("POST", "/v1/projects/{project_id}/jobs"),
        ("GET", "/v1/projects/{project_id}/jobs/{job_id}"),
        ("POST", "/v1/projects/{project_id}/jobs/{job_id}/cancel"),
        ("GET", "/v1/projects/{project_id}/jobs/{job_id}/events"),
    }
    response = client.post("/v1/projects/project-a/sync-batches", json={
        "batch_id": "b1", "tenant_id": "tenant-attacker", "actor_id": "attacker",
        "operations": [{"operation_id": "o1", "payload": "eA=="}],
    })
    assert response.status_code == 201
    stored = service.sync_store.lookup(tenant_id="tenant-a", project_id="project-a", batch_id="b1")
    assert stored.batch.actor_id == "actor-a"
    assert client.get("/v1/projects/project-a/sync-batches/b1").status_code == 200
    assert client.get("/v1/projects/project-a/sync-batches/b1", headers={"X-Tenant-ID": "tenant-attacker"}).status_code == 200
    assert service.sync_store.lookup(tenant_id="tenant-attacker", project_id="project-a", batch_id="b1") is None
    denied = client.get("/v1/projects/other/sync-batches/b1")
    assert denied.status_code == 404
    blocked = client.post("/v1/projects/project-a/jobs", headers={"Idempotency-Key": "k"}, json={
        "design_revision_id": "rev1", "case_ref": "case", "adapter_id": "a", "adapter_version": "1",
        "eir": {}, "bundle": {}, "adapter_capabilities": {"a": {"supported": True}},
    })
    assert blocked.status_code == 422
    assert blocked.json()["report"]["diagnostics"][0]["code"] == "approval_fingerprint_unavailable"


def test_http_sync_replay_returns_original_receipt_and_rejects_reused_identity(tmp_path):
    principal = Principal("tenant-a", "actor-a")
    service = OCREApplication(
        sync_store=SQLiteSyncStore(tmp_path / "sync.db"),
        job_store=SQLiteJobStore(tmp_path / "jobs.db"),
        authorizer=ProjectAccess(),
        preflight=Blocked(),
    )
    client = TestClient(create_app(service, principal_dependency=lambda: principal))
    path = "/v1/projects/project-a/sync-batches"
    batch = {
        "batch_id": "batch-replay",
        "operations": [{"operation_id": "op-1", "payload": "eA=="}],
    }

    first = client.post(path, json=batch)
    replay = client.post(path, json=batch)
    changed = client.post(path, json={**batch, "operations": [{"operation_id": "op-2", "payload": "eQ=="}]})

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["committed_revision_id"] == first.json()["committed_revision_id"]
    assert changed.status_code == 409
    assert changed.json()["code"] == "sync_batch_identity_conflict"


def test_cancel_accepts_empty_body_and_job_submit_rejects_non_strict_json_before_preflight(tmp_path):
    principal = Principal("tenant-a", "actor-a")

    class Ready:
        def __init__(self):
            self.calls = 0

        def evaluate(self, _request):
            self.calls += 1
            return PreflightReport("valid", "ready")

    preflight = Ready()
    service = OCREApplication(
        sync_store=SQLiteSyncStore(tmp_path / "sync.db"),
        job_store=SQLiteJobStore(tmp_path / "jobs.db"),
        authorizer=ProjectAccess(),
        preflight=preflight,
    )
    app = create_app(service, principal_dependency=lambda: principal)
    client = TestClient(app)
    job = client.post("/v1/projects/project-a/jobs", headers={"Idempotency-Key": "submit"}, json={
        "design_revision_id": "rev1", "case_ref": "case", "adapter_id": "a", "adapter_version": "1",
        "eir": {}, "bundle": {},
    })
    assert job.status_code == 202

    cancelled = client.post(
        f"/v1/projects/project-a/jobs/{job.json()['job_id']}/cancel",
        headers={"Idempotency-Key": "cancel"},
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancel_requested"

    raw_prefix = b'{"design_revision_id":"rev1","case_ref":"case","adapter_id":"a","adapter_version":"1","bundle":{},"eir":'
    for index, raw_eir in enumerate((b'{"x":1,"x":2}', b'{"x":1e999}', b'{"x":NaN}', b'{"x":1,}')):
        response = client.post(
            "/v1/projects/project-a/jobs",
            headers={"Idempotency-Key": f"invalid-{index}", "Content-Type": "application/json"},
            content=raw_prefix + raw_eir + b"}",
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_failed"
    assert preflight.calls == 1


def test_http_job_submit_cancel_and_event_replay_are_idempotent(tmp_path):
    principal = Principal("tenant-a", "actor-a")

    class Ready:
        def evaluate(self, _request):
            return PreflightReport("valid", "ready")

    service = OCREApplication(
        sync_store=SQLiteSyncStore(tmp_path / "sync.db"),
        job_store=SQLiteJobStore(tmp_path / "jobs.db"),
        authorizer=ProjectAccess(),
        preflight=Ready(),
    )
    client = TestClient(create_app(service, principal_dependency=lambda: principal))
    submit_path = "/v1/projects/project-a/jobs"
    submission = {
        "design_revision_id": "rev1",
        "case_ref": "case",
        "adapter_id": "adapter",
        "adapter_version": "1",
        "eir": {},
        "bundle": {},
    }
    submit_headers = {"Idempotency-Key": "submit-key"}

    first = client.post(submit_path, headers=submit_headers, json=submission)
    replay = client.post(submit_path, headers=submit_headers, json=submission)
    conflicting = client.post(
        submit_path,
        headers=submit_headers,
        json={**submission, "case_ref": "different-case"},
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert replay.json()["job_id"] == first.json()["job_id"]
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "idempotency_key_reused"

    job_id = first.json()["job_id"]
    cancel_path = f"/v1/projects/project-a/jobs/{job_id}/cancel"
    cancel_headers = {"Idempotency-Key": "cancel-key"}
    cancelled = client.post(cancel_path, headers=cancel_headers)
    cancel_replay = client.post(cancel_path, headers=cancel_headers)
    assert cancelled.status_code == cancel_replay.status_code == 202
    assert cancelled.json()["state"] == cancel_replay.json()["state"] == "cancel_requested"
    assert cancel_replay.json()["sequence"] == cancelled.json()["sequence"] == 2
    assert cancel_replay.json()["replayed"] is True

    events_path = f"/v1/projects/project-a/jobs/{job_id}/events"
    first_page = client.get(events_path, params={"limit": 1})
    second_page = client.get(events_path, params={"after_cursor": first_page.json()["next_cursor"]})
    assert first_page.status_code == second_page.status_code == 200
    assert first_page.json()["has_more"] is True
    assert first_page.json()["items"][0]["event"]["sequence"] == 1
    assert second_page.json()["items"][0]["sequence"] == 2
    assert second_page.json()["items"][0]["event"]["to_state"] == "cancel_requested"
