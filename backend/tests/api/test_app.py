import asyncio
import json

from fastapi.testclient import TestClient

from ocre_core.api.app import create_app
from ocre_core.application.services import Principal
from tests.helpers import BLOCKED_APPROVAL, FixedPreflight, RuleAuthorizer, application


AUTH = RuleAuthorizer(
    lambda p, proj: p.tenant_id == "tenant-a" and proj == "project-a"
)
PRINCIPAL = Principal("tenant-a", "actor-a")


def client_for(service):
    return TestClient(create_app(service, principal_dependency=lambda: PRINCIPAL))


def job_json(**overrides):
    return {
        "design_revision_id": "rev1",
        "case_ref": "case",
        "adapter_id": "a",
        "adapter_version": "1",
        "eir": {},
        "bundle": {},
        **overrides,
    }


def test_local_transport_injects_principal_and_exposes_only_six_operations(tmp_path):
    service = application(tmp_path, authorizer=AUTH, preflight=FixedPreflight(BLOCKED_APPROVAL))
    client = client_for(service)
    operations = {
        (method, route.path)
        for route in client.app.routes
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
    blocked = client.post("/v1/projects/project-a/jobs", headers={"Idempotency-Key": "k"}, json=job_json(
        adapter_capabilities={"a": {"supported": True}},
    ))
    assert blocked.status_code == 422
    assert blocked.json()["report"]["diagnostics"][0]["code"] == "approval_fingerprint_unavailable"


def test_sync_batches_require_strict_json_and_consistent_problem_details(tmp_path):
    service = application(tmp_path, authorizer=AUTH, preflight=FixedPreflight(BLOCKED_APPROVAL))
    client = client_for(service)
    path = "/v1/projects/project-a/sync-batches"
    for raw in (
        b'{"batch_id":"b1","batch_id":"b2","operations":[]}',
        b'{"batch_id":"b3","operations":[],"base_revision_id":NaN}',
        b'{"batch_id":"b4","operations":[{"operation_id":"o","payload":"eA=="}],}',
        b'{"batch_id":"b5","operations":[{"operation_id":"o","payload":"eA==","payload":"eQ=="}]}',
        b'{"batch_id":5,"operations":"x"}',
    ):
        response = client.post(path, headers={"Content-Type": "application/json"}, content=raw)
        assert response.status_code == 422
        assert response.json()["code"] == "validation_failed"
        assert response.headers["content-type"].startswith("application/problem+json")
    assert service.sync_store.lookup(tenant_id="tenant-a", project_id="project-a", batch_id="b2") is None


def test_body_limit_is_configurable_and_precedes_any_commit(tmp_path):
    preflight = FixedPreflight()
    service = application(tmp_path, authorizer=AUTH, preflight=preflight)
    client = TestClient(create_app(service, principal_dependency=lambda: PRINCIPAL, max_body_bytes=64))

    oversized_sync = client.post("/v1/projects/project-a/sync-batches", json={
        "batch_id": "b", "operations": [{"operation_id": "o", "payload": "eA=="}],
    })
    assert oversized_sync.status_code == 413
    assert oversized_sync.json()["code"] == "request_too_large"

    oversized_job = client.post("/v1/projects/project-a/jobs", headers={"Idempotency-Key": "k"}, json=job_json())
    assert oversized_job.status_code == 413

    accepted = client.post("/v1/projects/project-a/sync-batches", content=b'{"batch_id":"b","operations":[]}')
    assert accepted.status_code == 201
    assert service.sync_store.lookup(tenant_id="tenant-a", project_id="project-a", batch_id="b") is not None
    assert preflight.calls == 0


def test_http_sync_replay_returns_original_receipt_and_rejects_reused_identity(tmp_path):
    service = application(tmp_path, authorizer=AUTH, preflight=FixedPreflight(BLOCKED_APPROVAL))
    client = client_for(service)
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
    preflight = FixedPreflight()
    service = application(tmp_path, authorizer=AUTH, preflight=preflight)
    client = client_for(service)
    job = client.post("/v1/projects/project-a/jobs", headers={"Idempotency-Key": "submit"}, json=job_json())
    assert job.status_code == 202

    cancelled = client.post(
        f"/v1/projects/project-a/jobs/{job.json()['job_id']}/cancel",
        headers={"Idempotency-Key": "cancel"},
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancel_requested"

    raw_prefix = b'{"design_revision_id":"rev1","case_ref":"case","adapter_id":"a","adapter_version":"1","bundle":{},"eir":'
    for index, raw_eir in enumerate((b'{"x":1,"x":2}', b'{"x":1e999}', b'{"x":NaN}', b'{"x":1,}',
                                    b'{"x":' + b"9" * 5000 + b'}', b'{"x":"\\ud800"}')):
        response = client.post(
            "/v1/projects/project-a/jobs",
            headers={"Idempotency-Key": f"invalid-{index}", "Content-Type": "application/json"},
            content=raw_prefix + raw_eir + b"}",
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_failed"
    assert preflight.calls == 1


def test_http_job_submit_cancel_and_event_replay_are_idempotent(tmp_path):
    service = application(tmp_path, authorizer=AUTH, preflight=FixedPreflight())
    client = client_for(service)
    submit_path = "/v1/projects/project-a/jobs"
    submission = job_json(adapter_id="adapter")
    submit_headers = {"Idempotency-Key": "submit-key"}

    first = client.post(submit_path, headers=submit_headers, json=submission)
    replay = client.post(submit_path, headers=submit_headers, json=submission)
    conflicting = client.post(
        submit_path,
        headers=submit_headers,
        json=job_json(adapter_id="adapter", case_ref="different-case"),
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


def _resolve_ref(spec, ref):
    assert ref.startswith("#/"), f"non-local ref: {ref}"
    node = spec
    for segment in ref[2:].split("/"):
        node = node[segment]
    return node


def _collect_refs(node, refs):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                refs.append(value)
            else:
                _collect_refs(value, refs)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, refs)


def test_openapi_documents_sync_batch_request_body_with_resolvable_refs(tmp_path):
    service = application(tmp_path, authorizer=AUTH, preflight=FixedPreflight())
    client = client_for(service)
    spec = client.get("/openapi.json").json()

    post = spec["paths"]["/v1/projects/{project_id}/sync-batches"]["post"]
    assert post["requestBody"]["required"] is True
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert schema["type"] == "object"
    assert {"batch_id", "operations"} <= set(schema["required"])

    job_post = spec["paths"]["/v1/projects/{project_id}/jobs"]["post"]
    assert job_post["requestBody"]["required"] is True
    job_schema = job_post["requestBody"]["content"]["application/json"]["schema"]
    assert job_schema["type"] == "object"
    assert {"design_revision_id", "case_ref", "adapter_id", "adapter_version"} <= set(job_schema["required"])

    refs = []
    _collect_refs(spec, refs)
    assert schema["properties"]["operations"]["items"]["$ref"] in refs
    for ref in refs:
        assert not ref.startswith("#/$defs"), f"unresolvable OpenAPI ref: {ref}"
        _resolve_ref(spec, ref)

    nested = _resolve_ref(spec, schema["properties"]["operations"]["items"]["$ref"])
    assert {"operation_id", "payload"} <= set(nested["required"])


def test_mutating_routes_run_the_service_off_the_event_loop(tmp_path):
    service = application(tmp_path, authorizer=AUTH, preflight=FixedPreflight())
    client = client_for(service)
    loops = []

    def spy(method):
        def wrapped(*args, **kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                loops.append("off-loop")
            else:
                loops.append("on-loop")
            return method(*args, **kwargs)
        return wrapped

    service.submit_sync = spy(service.submit_sync)
    service.submit_job = spy(service.submit_job)

    sync = client.post("/v1/projects/project-a/sync-batches", json={"batch_id": "b", "operations": []})
    job = client.post("/v1/projects/project-a/jobs", headers={"Idempotency-Key": "k"}, json=job_json())

    assert sync.status_code == 201
    assert job.status_code == 202
    assert loops == ["off-loop", "off-loop"]


def test_chunked_body_without_content_length_is_bounded_and_valid_bodies_accepted(tmp_path):
    service = application(tmp_path, authorizer=AUTH, preflight=FixedPreflight())
    client = TestClient(create_app(service, principal_dependency=lambda: PRINCIPAL, max_body_bytes=64))
    path = "/v1/projects/project-a/sync-batches"

    def chunks():
        yield b'{"batch_id":"chunked","operations":['
        yield b" " * 256
        yield b"]}"

    response = client.post(path, content=chunks())
    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert service.sync_store.lookup(tenant_id="tenant-a", project_id="project-a", batch_id="chunked") is None

    accepted = client.post(path, content=iter([b'{"batch_id":"ok","operations":[]}']))
    assert accepted.status_code == 201


def test_oversized_stream_aborts_without_consuming_the_full_body(tmp_path):
    service = application(tmp_path, authorizer=AUTH, preflight=FixedPreflight())
    app = create_app(service, principal_dependency=lambda: PRINCIPAL, max_body_bytes=8)
    parts = [b'{"batch_id":', b'"too-big","ope', b'rations":[]}']
    consumed = 0
    sent = []

    async def receive():
        nonlocal consumed
        if consumed < len(parts):
            body = parts[consumed]
            consumed += 1
            return {"type": "http.request", "body": body, "more_body": consumed < len(parts)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/projects/project-a/sync-batches",
        "raw_path": b"/v1/projects/project-a/sync-batches",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("client", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))

    assert consumed == 1  # first 12-byte chunk alone exceeds the 8-byte cap
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413
    payload = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert json.loads(payload)["code"] == "request_too_large"
