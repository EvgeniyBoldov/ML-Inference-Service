"""Control-plane deployment and data-plane prediction use cases."""

from __future__ import annotations

import asyncio
from time import time
from typing import Any
from uuid import uuid4

from jsonschema import ValidationError, validate

from .domain import Deployment, DeploymentStatus, ModelMetadata
from .errors import ServiceError
from .model_source import ModelSource
from .repositories import DeploymentRepository
from .runtime import RuntimeBackend, RuntimeHandle


class DeploymentManager:
    def __init__(self, repository: DeploymentRepository, source: ModelSource, runtime: RuntimeBackend) -> None:
        self._repository = repository
        self._source = source
        self._runtime = runtime
        self._runtimes: dict[str, RuntimeHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def create(self, model: str, uri: str, idempotency_key: str) -> Deployment:
        version = _version_from_uri(model, uri)
        candidate = Deployment(
            id=f"deploy_{uuid4().hex}", model=model, version=version, uri=uri,
            slot="pending",
        )
        try:
            deployment, created = await self._repository.create_or_get(
                candidate, idempotency_key, request_fingerprint=f"{model}:{uri}",
            )
        except ValueError as exc:
            raise ServiceError("SCHEMA_VALIDATION_ERROR", str(exc), status_code=409, param="Idempotency-Key") from exc
        if created:
            task = asyncio.create_task(self._run(deployment), name=deployment.id)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return deployment

    async def get(self, deployment_id: str) -> Deployment | None:
        return await self._repository.get(deployment_id)

    async def rollback(self, model: str) -> Deployment:
        try:
            active, _former_active = await self._repository.rollback(model)
        except LookupError as exc:
            raise ServiceError("DEPLOYMENT_FAILED", str(exc), status_code=409) from exc
        return active

    async def _run(self, deployment: Deployment) -> None:
        try:
            deployment.status = DeploymentStatus.DOWNLOADING
            await self._repository.save(deployment)
            metadata = await self._source.resolve(deployment.model, deployment.uri)
            _validate_metadata(metadata, deployment)
            deployment.metadata = metadata

            deployment.status = DeploymentStatus.LOADING
            await self._repository.save(deployment)
            runtime = await self._runtime.deploy(metadata)
            deployment.runtime_id = runtime.id
            await self._runtime.load(runtime)
            self._runtimes[deployment.id] = runtime
            if not await self._runtime.health(runtime):
                raise ServiceError("MODEL_HEALTHCHECK_FAILED", "Runtime healthcheck failed", status_code=502)

            deployment.status = DeploymentStatus.WARMING_UP
            await self._repository.save(deployment)
            output = await self._runtime.predict(runtime, metadata.input_example)
            _validate_output(metadata.output_schema, output)
            deployment.status = DeploymentStatus.READY
            await self._repository.save(deployment)
            previous = await self._repository.activate(deployment)
            deployment.activated_at = int(time())
            await self._repository.save(deployment)
            if previous and previous.id in self._runtimes:
                await self._runtime.drain(self._runtimes[previous.id])
        except ServiceError as exc:
            deployment.status = DeploymentStatus.FAILED
            deployment.failed_at = int(time())
            deployment.error_code = exc.code
            deployment.error_message = exc.message
            await self._repository.save(deployment)
        except Exception as exc:  # external runtime exceptions must not escape a background task
            deployment.status = DeploymentStatus.FAILED
            deployment.failed_at = int(time())
            deployment.error_code = "MODEL_LOAD_FAILED"
            deployment.error_message = str(exc)
            await self._repository.save(deployment)

    async def active(self, model: str) -> tuple[Deployment, RuntimeHandle]:
        deployment = await self._repository.active_for(model)
        if not deployment:
            raise ServiceError("MODEL_NOT_FOUND", f"Model '{model}' is not active", status_code=404, param="model")
        runtime = self._runtimes.get(deployment.id)
        if not runtime:
            raise ServiceError("RUNTIME_UNAVAILABLE", "Active model runtime is unavailable", status_code=503)
        return deployment, runtime

    async def catalog(self) -> list[Deployment]:
        return await self._repository.list_active()

    async def restore(self) -> None:
        """Recreate persisted active/standby handles after a service restart.

        Metadata was saved at deployment time, so this path does not contact the
        MLflow control plane. Any handle that cannot be recreated remains absent
        and requests fail safely with ``RUNTIME_UNAVAILABLE``.
        """
        for deployment in await self._repository.list_recoverable():
            if deployment.metadata is None:
                continue
            try:
                runtime = await self._runtime.deploy(deployment.metadata)
                await self._runtime.load(runtime)
                if await self._runtime.health(runtime):
                    deployment.runtime_id = runtime.id
                    self._runtimes[deployment.id] = runtime
                    await self._repository.save(deployment)
            except Exception:
                # Preserve durable routing state for operator diagnosis; do not
                # silently route traffic to an unverified replacement runtime.
                continue


class PredictionService:
    def __init__(self, deployments: DeploymentManager, runtime: RuntimeBackend) -> None:
        self._deployments = deployments
        self._runtime = runtime

    async def predict(self, model: str, payload: Any) -> tuple[Deployment, Any]:
        deployment, runtime = await self._deployments.active(model)
        assert deployment.metadata is not None
        _validate_input(deployment.metadata.input_schema, payload)
        try:
            output = await self._runtime.predict(runtime, payload)
            _validate_output(deployment.metadata.output_schema, output)
            return deployment, output
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("MODEL_PREDICTION_FAILED", "Model prediction failed", status_code=502, error_type="server_error") from exc


def _version_from_uri(model: str, uri: str) -> str:
    prefix = f"models:/{model}/"
    if not uri.startswith(prefix) or not uri[len(prefix):] or uri[len(prefix):].startswith("@"):
        raise ServiceError("SCHEMA_VALIDATION_ERROR", "source.uri must be models:/<model>/<immutable-version>", param="source.uri")
    return uri[len(prefix):]


def _validate_metadata(metadata: ModelMetadata, deployment: Deployment) -> None:
    if metadata.name != deployment.model or metadata.version != deployment.version:
        raise ServiceError("MODEL_LOAD_FAILED", "MLflow metadata does not match deployment request", status_code=422)
    if not metadata.description.strip():
        raise ServiceError("MODEL_DESCRIPTION_REQUIRED", "Model description is required", status_code=422)
    if metadata.input_example is None:
        raise ServiceError("INPUT_EXAMPLE_REQUIRED", "Model input example is required", status_code=422)
    if not metadata.input_schema or not metadata.output_schema:
        raise ServiceError("MODEL_SIGNATURE_REQUIRED", "Model input and output signatures are required", status_code=422)


def _validate_input(schema: dict[str, Any], payload: Any) -> None:
    try:
        validate(instance=payload, schema=schema)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.path)
        param = f"input.{path}" if path else "input"
        raise ServiceError("SCHEMA_VALIDATION_ERROR", exc.message, param=param) from exc


def _validate_output(schema: dict[str, Any], output: Any) -> None:
    try:
        validate(instance=output, schema=schema)
    except ValidationError as exc:
        raise ServiceError("MODEL_OUTPUT_VALIDATION_FAILED", exc.message, status_code=502, error_type="server_error") from exc
