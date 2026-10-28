"""Minimal local FastAPI transport; identity and services are injected."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ocre_core.application.services import (
    ApplicationError,
    JobSubmission,
    OCREApplication,
    Principal,
)


class SyncOperationBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    operation_id: str = Field(min_length=1)
    payload: str


class SyncBatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    batch_id: str = Field(min_length=1)
    base_revision_id: str | None = None
    operations: list[SyncOperationBody]


class JobSubmitBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    design_revision_id: str = Field(min_length=1)
    case_ref: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    eir: Any
    bundle: Any
    group_manifest: Any | None = None


def create_app(application: OCREApplication, *, principal_dependency: Callable[..., Principal]) -> FastAPI:
    """Construct an app with an explicit identity provider.

    Development and tests may inject a fixed principal. Production callers must
    inject their verified identity dependency; this module has no default auth.
    """
    app = FastAPI(title="OCRE local API", version="1.0.0")

    @app.exception_handler(ApplicationError)
    async def application_error(_request, exc: ApplicationError):
        return JSONResponse(
            status_code=exc.status,
            media_type="application/problem+json",
            content={"code": exc.code, "title": exc.detail, "status": exc.status, **exc.extra},
        )

    @app.post("/v1/projects/{project_id}/sync-batches")
    def submit_sync(project_id: str, body: SyncBatchBody, principal: Principal = Depends(principal_dependency)):
        result = application.submit_sync(principal, project_id, body.model_dump())
        return JSONResponse(status_code=200 if result["replayed"] else 201, content=result)

    @app.get("/v1/projects/{project_id}/sync-batches/{batch_id}")
    def get_sync_receipt(project_id: str, batch_id: str, principal: Principal = Depends(principal_dependency)):
        return application.get_sync_receipt(principal, project_id, batch_id)

    @app.post("/v1/projects/{project_id}/jobs", status_code=202)
    async def submit_job(
        project_id: str,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        principal: Principal = Depends(principal_dependency),
    ):
        from ocre_core.validation.validator import StrictJSONError, parse_json_strict

        try:
            raw_body = parse_json_strict(await request.body())
            body = JobSubmitBody.model_validate(raw_body)
        except StrictJSONError as exc:
            raise ApplicationError("validation_failed", 422, "Request body is not strict JSON") from exc
        except ValidationError as exc:
            raise ApplicationError("validation_failed", 422, "Invalid job submission") from exc
        return application.submit_job(
            principal,
            project_id,
            JobSubmission(**body.model_dump()),
            idempotency_key,
        )

    @app.get("/v1/projects/{project_id}/jobs/{job_id}")
    def get_job(project_id: str, job_id: str, principal: Principal = Depends(principal_dependency)):
        return application.job_status(principal, project_id, job_id)

    @app.post("/v1/projects/{project_id}/jobs/{job_id}/cancel", status_code=202)
    def cancel_job(
        project_id: str,
        job_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        principal: Principal = Depends(principal_dependency),
    ):
        return application.cancel_job(principal, project_id, job_id, idempotency_key)

    @app.get("/v1/projects/{project_id}/jobs/{job_id}/events")
    def replay_events(
        project_id: str,
        job_id: str,
        after_cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
        principal: Principal = Depends(principal_dependency),
    ):
        return application.replay_events(principal, project_id, job_id, after_cursor, limit)

    return app
