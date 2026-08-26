"""FastAPI composition root for the ML Inference Service."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from time import time
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import StaticTokenAuth
from .domain import Deployment
from .errors import ServiceError
from .mlflow_source import MlflowModelSource
from .model_source import ModelSource, UnavailableModelSource
from .repositories import DeploymentRepository, InMemoryDeploymentRepository
from .runtime import MlflowPyfuncRuntimeBackend, PredictorRuntimeBackend, RuntimeBackend
from .schemas import (
    DeploymentObject, DeploymentRequest, ErrorResponse, ModelList, ModelObject,
    PredictionOutput, ResponseObject, ResponseRequest,
)
from .services import DeploymentManager, PredictionService


def create_app(
    *,
    source: ModelSource | None = None,
    runtime: RuntimeBackend | None = None,
    auth: StaticTokenAuth | None = None,
    repository: DeploymentRepository | None = None,
) -> FastAPI:
    """Create an app with replaceable integration adapters for tests and deployment."""
    if repository is None:
        database_url = os.getenv("INFERENCE_DATABASE_URL")
        if database_url:
            from .postgres_repository import SqlAlchemyDeploymentRepository
            repository = SqlAlchemyDeploymentRepository(database_url)
        else:
            repository = InMemoryDeploymentRepository()
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
    source = source or (MlflowModelSource(tracking_uri) if tracking_uri else UnavailableModelSource())
    runtime = runtime or (MlflowPyfuncRuntimeBackend() if tracking_uri else PredictorRuntimeBackend())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        initialize = getattr(repository, "initialize", None)
        if initialize:
            await initialize()
        await manager.restore()
        yield
        dispose = getattr(repository, "dispose", None)
        if dispose:
            await dispose()

    app = FastAPI(title="ML Inference Service", version="0.1.0", lifespan=lifespan)
    manager = DeploymentManager(repository, source, runtime)
    prediction = PredictionService(manager, runtime)
    auth = auth or StaticTokenAuth()
    app.state.deployment_manager = manager

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": exc.message, "type": exc.error_type, "param": exc.param, "code": exc.code}},
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/v1/models", response_model=ModelList, responses={401: {"model": ErrorResponse}})
    async def list_models(_: None = Depends(auth.require("inference.read"))) -> ModelList:
        deployments = await manager.catalog()
        return ModelList(data=[_model_object(item) for item in deployments])

    @app.get("/v1/models/{model}", response_model=ModelObject, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
    async def get_model(model: str, _: None = Depends(auth.require("inference.read"))) -> ModelObject:
        deployment, _runtime = await manager.active(model)
        return _model_object(deployment, include_deployment=True)

    @app.post("/v1/responses", response_model=ResponseObject, responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
    async def response(body: ResponseRequest, _: None = Depends(auth.require("inference.predict"))) -> ResponseObject:
        deployment, output = await prediction.predict(body.model, body.input)
        return ResponseObject(
            id=f"resp_{uuid4().hex}", created_at=int(time()), model=body.model,
            model_version=deployment.version, output=[PredictionOutput(content=output)],
        )

    @app.post("/internal/v1/deployments", status_code=202, response_model=DeploymentObject)
    async def create_deployment(
        body: DeploymentRequest,
        request: Request,
        _: None = Depends(auth.require("deployment.write")),
    ) -> DeploymentObject:
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise ServiceError("SCHEMA_VALIDATION_ERROR", "Idempotency-Key header is required", param="Idempotency-Key")
        deployment = await manager.create(body.model, body.source.uri, key)
        return _deployment_object(deployment)

    @app.get("/internal/v1/deployments/{deployment_id}", response_model=DeploymentObject)
    async def get_deployment(deployment_id: str, _: None = Depends(auth.require("deployment.read"))) -> DeploymentObject:
        deployment = await manager.get(deployment_id)
        if deployment is None:
            raise ServiceError("DEPLOYMENT_NOT_FOUND", "Deployment was not found", status_code=404, param="deployment_id")
        return _deployment_object(deployment)

    @app.post("/internal/v1/deployments/{deployment_id}/rollback", response_model=DeploymentObject)
    async def rollback(deployment_id: str, _: None = Depends(auth.require("deployment.write"))) -> DeploymentObject:
        deployment = await manager.get(deployment_id)
        if deployment is None:
            raise ServiceError("DEPLOYMENT_NOT_FOUND", "Deployment was not found", status_code=404, param="deployment_id")
        active = await manager.rollback(deployment.model)
        return _deployment_object(active)

    return app


def _model_object(deployment: Deployment, *, include_deployment: bool = False) -> ModelObject:
    assert deployment.metadata is not None
    metadata = deployment.metadata
    return ModelObject(
        id=metadata.name, created=metadata.created_at, owned_by=metadata.owner,
        version=metadata.version, description=metadata.description,
        input_schema=metadata.input_schema, output_schema=metadata.output_schema,
        input_example=metadata.input_example,
        deployment={"version": deployment.version, "status": deployment.status.value} if include_deployment else None,
    )


def _deployment_object(deployment: Deployment) -> DeploymentObject:
    error: dict[str, str] | None = None
    if deployment.error_code:
        error = {"code": deployment.error_code, "message": deployment.error_message or deployment.error_code}
    return DeploymentObject(
        id=deployment.id, model=deployment.model, version=deployment.version,
        status=deployment.status.value, error=error,
    )


app = create_app()
