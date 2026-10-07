"""FastAPI composition root for the ML Inference Service."""

from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager
from time import perf_counter, time
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from .auth import FileTokenAuth, StaticTokenAuth
from .domain import Deployment
from .errors import ServiceError
from .mlflow_source import MlflowModelSource
from .model_source import ModelSource, UnavailableModelSource
from .metrics import ServiceMetrics
from .logging_config import configure_structured_logging
from .repositories import DeploymentRepository, InMemoryDeploymentRepository
from .runtime import DockerFleetRuntimeBackend, PredictorRuntimeBackend, RuntimeBackend
from .schemas import (
    DeploymentObject, DeploymentRequest, ErrorResponse, ModelList, ModelObject,
    PredictionOutput, ResponseObject, ResponseRequest,
)
from .services import DeploymentManager, PredictionService


def create_app(
    *,
    source: ModelSource | None = None,
    runtime: RuntimeBackend | None = None,
    auth: StaticTokenAuth | FileTokenAuth | None = None,
    repository: DeploymentRepository | None = None,
) -> FastAPI:
    """Create an app with replaceable integration adapters for tests and deployment."""
    configure_structured_logging()
    logger = logging.getLogger("ml_inference")
    if repository is None:
        database_url = os.getenv("INFERENCE_DATABASE_URL")
        if database_url:
            from .postgres_repository import SqlAlchemyDeploymentRepository
            repository = SqlAlchemyDeploymentRepository(database_url)
        else:
            repository = InMemoryDeploymentRepository()
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
    artifact_cache_root = os.getenv("MODEL_ARTIFACT_CACHE_ROOT")
    configured_runtime_values = (tracking_uri, artifact_cache_root)
    if any(configured_runtime_values) and not all(configured_runtime_values):
        raise RuntimeError(
            "MLFLOW_TRACKING_URI and MODEL_ARTIFACT_CACHE_ROOT must be configured together "
            "for the production fleet runtime"
        )
    source = source or (MlflowModelSource(tracking_uri, artifact_cache_root) if tracking_uri else UnavailableModelSource())
    runtime = runtime or (
        DockerFleetRuntimeBackend(
            artifact_cache_root=os.environ["MODEL_ARTIFACT_CACHE_ROOT"],
            memory_limit=os.getenv("MODEL_FLEET_MEMORY_LIMIT"),
            cpu_limit=os.getenv("MODEL_FLEET_CPU_LIMIT"),
            startup_timeout_seconds=float(os.getenv("FLEET_RUNTIME_STARTUP_TIMEOUT_SECONDS", "180")),
            command_timeout_seconds=float(os.getenv("DOCKER_COMMAND_TIMEOUT_SECONDS", "30")),
            prediction_timeout_seconds=float(os.getenv("PREDICTION_TIMEOUT_SECONDS", "30")),
        ) if tracking_uri else PredictorRuntimeBackend()
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        initialize = getattr(repository, "initialize", None)
        if initialize:
            await initialize()
        try:
            await manager.restore()
            yield
        finally:
            try:
                await manager.shutdown()
            finally:
                dispose = getattr(repository, "dispose", None)
                if dispose:
                    await dispose()

    app = FastAPI(title="ML Inference Service", version="0.1.0", lifespan=lifespan)
    metrics = ServiceMetrics()
    manager = DeploymentManager(repository, source, runtime, metrics, previous_ttl_seconds=int(os.getenv("PREVIOUS_RUNTIME_TTL_SECONDS", "3600")), max_pending_deployments=int(os.getenv("MAX_PENDING_DEPLOYMENTS", "32")))
    prediction = PredictionService(manager, runtime, timeout_seconds=float(os.getenv("PREDICTION_TIMEOUT_SECONDS", "30")), max_concurrent=int(os.getenv("MAX_CONCURRENT_PREDICTIONS", "64")))
    auth = auth or (FileTokenAuth(os.environ["INFERENCE_TOKEN_FILE"]) if os.getenv("INFERENCE_TOKEN_FILE") else StaticTokenAuth())
    app.state.deployment_manager = manager
    app.state.metrics = metrics

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        request.state.request_id = f"req_{uuid4().hex}"
        started = perf_counter()
        status_code = 500
        metrics.http_in_progress.inc()
        try:
            result = await call_next(request)
            status_code = result.status_code
            result.headers["X-Request-ID"] = request.state.request_id
            return result
        finally:
            elapsed = perf_counter() - started
            route = getattr(request.scope.get("route"), "path", "unmatched")
            method = request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"} else "OTHER"
            metrics.http_requests.labels(method, route, str(status_code)).inc()
            metrics.http_latency.labels(method, route).observe(elapsed)
            metrics.http_in_progress.dec()
            logger.info("http_request_completed", extra={"request_id": request.state.request_id, "method": method, "route": route, "status_code": status_code, "latency_ms": round(elapsed * 1000, 3)})

    def error_response(request: Request, exc: ServiceError) -> JSONResponse:
        headers = {"X-Request-ID": getattr(request.state, "request_id", f"req_{uuid4().hex}")}
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(status_code=exc.status_code, headers=headers, content={
            "error": {"message": exc.message, "type": exc.error_type, "param": exc.param, "code": exc.code},
        })

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        error = exc.errors()[0]
        location = error.get("loc", ())
        param = ".".join(str(part) for part in location if part != "body") or "body"
        return error_response(request, ServiceError("SCHEMA_VALIDATION_ERROR", error["msg"], param=param))

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "REQUEST_TOO_LARGE"}
        response = error_response(request, ServiceError(codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail), status_code=exc.status_code))
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("database_request_failed", exc_info=exc, extra={"request_id": request.state.request_id})
        return error_response(request, ServiceError("STORAGE_UNAVAILABLE", "Deployment storage is unavailable", status_code=503))

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("request_failed", exc_info=exc, extra={"request_id": getattr(request.state, "request_id", None)})
        return error_response(request, ServiceError("INTERNAL_ERROR", "An internal service error occurred", status_code=500))

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        if not await manager.ready():
            raise ServiceError("RUNTIME_UNAVAILABLE", "Persisted model fleet is not ready", status_code=503)
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(_: None = Depends(auth.require("metrics.read"))) -> Response:
        return Response(metrics.exposition(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/internal/v1/status")
    async def service_status(_: None = Depends(auth.require("metrics.read"))) -> dict[str, Any]:
        return {**await manager.operational_status(), **metrics.request_statistics()}

    @app.get("/v1/models", response_model=ModelList, responses={401: {"model": ErrorResponse}})
    async def list_models(_: None = Depends(auth.require("inference.read"))) -> ModelList:
        deployments = await manager.catalog()
        return ModelList(data=[_model_object(item) for item in deployments])

    @app.get("/v1/models/{model}", response_model=ModelObject, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
    async def get_model(model: str, _: None = Depends(auth.require("inference.read"))) -> ModelObject:
        deployment, _runtime = await manager.active(model)
        return _model_object(deployment, include_deployment=True)

    @app.post("/v1/responses", response_model=ResponseObject, responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
    async def response(body: ResponseRequest, request: Request, _: None = Depends(auth.require("inference.predict"))) -> ResponseObject:
        request_id = request.state.request_id
        started_at = perf_counter()
        try:
            deployment, output, model_latency = await prediction.predict(body.model, body.input)
        except ServiceError as exc:
            metric_model = manager.metric_model(body.model)
            metrics.prediction_errors.labels(metric_model, "unknown", exc.code).inc()
            metrics.prediction_requests.labels(metric_model, "unknown", "error").inc()
            logger.info("prediction_failed", extra={"request_id": request_id, "model": body.model, "status": "error", "code": exc.code})
            raise
        total_latency = perf_counter() - started_at
        metrics.prediction_requests.labels(deployment.model, deployment.version, "completed").inc()
        metrics.prediction_latency.labels(deployment.model, deployment.version, "total").observe(total_latency)
        metrics.prediction_latency.labels(deployment.model, deployment.version, "model").observe(model_latency)
        metrics.prediction_latency.labels(deployment.model, deployment.version, "gateway").observe(total_latency - model_latency)
        logger.info("prediction_completed", extra={"request_id": request_id, "model": deployment.model, "version": deployment.version, "deployment_id": deployment.id, "runtime_id": deployment.runtime_id, "status": "completed", "latency_ms": round(total_latency * 1000, 3)})
        return ResponseObject(id=f"resp_{uuid4().hex}", created_at=int(time()), model=body.model, model_version=deployment.version, output=[PredictionOutput(content=output)])

    @app.post("/internal/v1/deployments", status_code=202, response_model=DeploymentObject)
    async def create_deployment(
        body: DeploymentRequest,
        request: Request,
        _: None = Depends(auth.require("deployment.write")),
    ) -> DeploymentObject:
        key = request.headers.get("Idempotency-Key")
        if not key or not key.strip() or len(key) > 255:
            raise ServiceError("SCHEMA_VALIDATION_ERROR", "Idempotency-Key must contain 1 to 255 characters", param="Idempotency-Key")
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
        active = await manager.rollback(deployment.model, expected_deployment_id=deployment_id)
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
