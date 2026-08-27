"""Control-plane deployment and data-plane prediction use cases."""

from __future__ import annotations

import asyncio
from time import perf_counter, time
from typing import Any
from uuid import uuid4

from jsonschema import ValidationError, validate

from .domain import Deployment, DeploymentStatus, ModelMetadata
from .errors import ServiceError
from .model_source import ModelSource
from .metrics import ServiceMetrics
from .repositories import DeploymentRepository
from .runtime import RuntimeBackend, RuntimeHandle


class DeploymentManager:
    def __init__(self, repository: DeploymentRepository, source: ModelSource, runtime: RuntimeBackend, metrics: ServiceMetrics, previous_ttl_seconds: int = 3600) -> None:
        self._repository = repository
        self._source = source
        self._runtime = runtime
        self._metrics = metrics
        self._previous_ttl_seconds = previous_ttl_seconds
        # A fleet is a single consistency unit: concurrent Airflow deployments
        # must not build two candidates from different model sets.
        self._rollout_lock = asyncio.Lock()
        self._active_fleet: RuntimeHandle | None = None
        self._previous_fleet: RuntimeHandle | None = None
        self._active_fleet_deployments: dict[str, Deployment] = {}
        self._previous_fleet_deployments: dict[str, Deployment] = {}
        self._fleet_lock = asyncio.Lock()
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
        async with self._rollout_lock:
            return await self._rollback_serialized(model)

    async def _rollback_serialized(self, model: str) -> Deployment:
        # A fleet is switched as one revision. Only its immediately preceding
        # revision is safe to restore; older per-model routes may describe a
        # model set that no longer exists in the retained runtime.
        try:
            current = await self._repository.active_for(model)
            previous = self._previous_fleet_deployments.get(model)
            if current is None or previous is None or self._active_fleet_deployments.get(model, current).id != current.id:
                raise LookupError("Rollback is available only for the latest fleet transition")
            active, _former_active = await self._repository.rollback(model)
            if active.id != previous.id:
                raise LookupError("Rollback target is not part of the previous fleet")
        except LookupError as exc:
            raise ServiceError("DEPLOYMENT_FAILED", str(exc), status_code=409) from exc
        async with self._fleet_lock:
            if self._previous_fleet is None:
                raise ServiceError("RUNTIME_UNAVAILABLE", "Previous fleet runtime is unavailable", status_code=409)
            self._active_fleet, self._previous_fleet = self._previous_fleet, self._active_fleet
            self._active_fleet_deployments, self._previous_fleet_deployments = (
                self._previous_fleet_deployments,
                self._active_fleet_deployments,
            )
            standby_fleet = self._previous_fleet
        if standby_fleet:
            self._schedule(self._expire_previous_fleet(standby_fleet, _former_active), f"expire_{_former_active.id}")
        return active

    async def _run(self, deployment: Deployment) -> None:
        async with self._rollout_lock:
            await self._run_serialized(deployment)

    async def _run_serialized(self, deployment: Deployment) -> None:
        started_at = perf_counter()
        runtime: RuntimeHandle | None = None
        try:
            deployment.status = DeploymentStatus.DOWNLOADING
            await self._repository.save(deployment)
            metadata = await self._source.resolve(deployment.model, deployment.uri)
            _validate_metadata(metadata, deployment)
            deployment.metadata = metadata

            deployment.status = DeploymentStatus.LOADING
            await self._repository.save(deployment)
            current = await self._repository.list_active()
            fleet_models = [item.metadata for item in current if item.metadata and item.model != deployment.model]
            fleet_models.append(metadata)
            runtime = await self._runtime.deploy(fleet_models)
            deployment.runtime_id = runtime.id
            await self._runtime.load(runtime)
            if not await self._runtime.health(runtime):
                raise ServiceError("MODEL_HEALTHCHECK_FAILED", "Runtime healthcheck failed", status_code=502)

            deployment.status = DeploymentStatus.WARMING_UP
            await self._repository.save(deployment)
            for fleet_model in fleet_models:
                output = await self._runtime.predict(runtime, fleet_model.name, fleet_model.input_example)
                _validate_output(fleet_model.output_schema, output)
            deployment.status = DeploymentStatus.READY
            await self._repository.save(deployment)
            previous = await self._repository.activate(deployment)
            deployment.activated_at = int(time())
            await self._repository.save(deployment)
            for existing in current:
                existing.runtime_id = runtime.id
                await self._repository.save(existing)
            async with self._fleet_lock:
                previous_fleet = self._active_fleet
                self._previous_fleet_deployments = {item.model: item for item in current}
                self._active_fleet_deployments = {
                    item.model: item for item in current if item.model != deployment.model
                }
                self._active_fleet_deployments[deployment.model] = deployment
                self._active_fleet = runtime
                self._previous_fleet = previous_fleet
            self._metrics.deployments.labels(deployment.model, deployment.version, "active").inc()
            self._metrics.model_load_duration.labels(deployment.model, deployment.version).observe(perf_counter() - started_at)
            self._metrics.runtime_status.labels(deployment.model, deployment.version, "active").set(1)
            self._metrics.active_version.labels(deployment.model, deployment.version).set(1)
            if previous_fleet:
                await self._runtime.drain(previous_fleet)
                if previous:
                    self._metrics.runtime_status.labels(previous.model, previous.version, "active").set(0)
                    self._metrics.runtime_status.labels(previous.model, previous.version, "standby").set(1)
                    self._metrics.active_version.labels(previous.model, previous.version).set(0)
                self._schedule(
                    self._expire_previous_fleet(previous_fleet, previous),
                    f"expire_{previous.id if previous else previous_fleet.id}",
                )
        except ServiceError as exc:
            await self._stop_failed_candidate(runtime)
            deployment.status = DeploymentStatus.FAILED
            deployment.failed_at = int(time())
            deployment.error_code = exc.code
            deployment.error_message = exc.message
            await self._repository.save(deployment)
            self._metrics.deployments.labels(deployment.model, deployment.version, "failed").inc()
            self._metrics.deployment_failures.labels(deployment.model, deployment.version, exc.code).inc()
            self._metrics.runtime_status.labels(deployment.model, deployment.version, "failed").set(1)
        except Exception as exc:  # external runtime exceptions must not escape a background task
            await self._stop_failed_candidate(runtime)
            deployment.status = DeploymentStatus.FAILED
            deployment.failed_at = int(time())
            deployment.error_code = "MODEL_LOAD_FAILED"
            deployment.error_message = str(exc)
            await self._repository.save(deployment)
            self._metrics.deployments.labels(deployment.model, deployment.version, "failed").inc()
            self._metrics.deployment_failures.labels(deployment.model, deployment.version, "MODEL_LOAD_FAILED").inc()
            self._metrics.runtime_status.labels(deployment.model, deployment.version, "failed").set(1)

    async def _stop_failed_candidate(self, runtime: RuntimeHandle | None) -> None:
        """Remove an unpromoted GREEN fleet after any validation/load failure."""
        if runtime is None:
            return
        async with self._fleet_lock:
            is_active = self._active_fleet is not None and self._active_fleet.id == runtime.id
        if not is_active:
            await self._runtime.stop(runtime)

    async def active(self, model: str) -> tuple[Deployment, RuntimeHandle]:
        deployment = await self._repository.active_for(model)
        if not deployment:
            raise ServiceError("MODEL_NOT_FOUND", f"Model '{model}' is not active", status_code=404, param="model")
        async with self._fleet_lock:
            runtime = self._active_fleet
        if not runtime:
            raise ServiceError("RUNTIME_UNAVAILABLE", "Active model runtime is unavailable", status_code=503)
        return deployment, runtime

    async def catalog(self) -> list[Deployment]:
        return await self._repository.list_active()

    async def ready(self) -> bool:
        """A replacement service is ready only after its persisted fleet is usable."""
        active = await self._repository.list_active()
        if not active:
            return True
        async with self._fleet_lock:
            runtime = self._active_fleet
        return runtime is not None and await self._runtime.health(runtime)

    async def restore(self) -> None:
        """Recreate persisted active/standby handles after a service restart.

        Metadata was saved at deployment time, so this path does not contact the
        MLflow control plane. Any handle that cannot be recreated remains absent
        and requests fail safely with ``RUNTIME_UNAVAILABLE``.
        """
        active = await self._repository.list_active()
        if active and all(item.metadata for item in active):
            try:
                runtime = await self._runtime.deploy([item.metadata for item in active if item.metadata])
                await self._runtime.load(runtime)
                if await self._runtime.health(runtime):
                    self._active_fleet = runtime
                    self._active_fleet_deployments = {item.model: item for item in active}
                    for deployment in active:
                        deployment.runtime_id = runtime.id
                        await self._repository.save(deployment)
            except Exception:
                pass
        for deployment in await self._repository.list_incomplete():
            self._schedule(self._run(deployment), f"recover_{deployment.id}")

    async def shutdown(self) -> None:
        """Stop runtime containers owned by this control-plane instance."""
        for task in tuple(self._tasks):
            task.cancel()
        async with self._fleet_lock:
            runtimes = {runtime.id: runtime for runtime in (self._active_fleet, self._previous_fleet) if runtime}
            self._active_fleet = None
            self._previous_fleet = None
        for runtime in runtimes.values():
            await self._runtime.stop(runtime)

    def _schedule(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _expire_previous_fleet(self, runtime: RuntimeHandle, deployment: Deployment | None) -> None:
        await asyncio.sleep(self._previous_ttl_seconds)
        async with self._fleet_lock:
            if self._active_fleet and self._active_fleet.id == runtime.id:
                return
            if self._previous_fleet and self._previous_fleet.id == runtime.id:
                self._previous_fleet = None
        await self._runtime.stop(runtime)
        if deployment:
            deployment.status = DeploymentStatus.REMOVED
            await self._repository.save(deployment)
            self._metrics.runtime_status.labels(deployment.model, deployment.version, "standby").set(0)


class PredictionService:
    def __init__(self, deployments: DeploymentManager, runtime: RuntimeBackend) -> None:
        self._deployments = deployments
        self._runtime = runtime

    async def predict(self, model: str, payload: Any) -> tuple[Deployment, Any, float]:
        deployment, runtime = await self._deployments.active(model)
        assert deployment.metadata is not None
        _validate_input(deployment.metadata.input_schema, payload)
        try:
            started_at = perf_counter()
            output = await self._runtime.predict(runtime, model, payload)
            model_latency = perf_counter() - started_at
            _validate_output(deployment.metadata.output_schema, output)
            return deployment, output, model_latency
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
