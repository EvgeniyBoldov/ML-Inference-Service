"""Control-plane deployment and data-plane prediction use cases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import re
from time import perf_counter, time
from typing import Any
from uuid import uuid4

from jsonschema import SchemaError, ValidationError, validate
from jsonschema.validators import validator_for

from .domain import Deployment, DeploymentStatus, ModelMetadata
from .errors import ServiceError
from .model_source import ModelSource
from .metrics import ServiceMetrics
from .repositories import DeploymentRepository
from .runtime import RuntimeBackend, RuntimeHandle


@dataclass(frozen=True)
class FleetSnapshot:
    """A prediction-safe pairing of one runtime and its exact model routes."""

    runtime: RuntimeHandle
    deployments: dict[str, Deployment]


class DeploymentManager:
    def __init__(self, repository: DeploymentRepository, source: ModelSource, runtime: RuntimeBackend, metrics: ServiceMetrics, previous_ttl_seconds: int = 3600, max_pending_deployments: int = 32) -> None:
        if previous_ttl_seconds < 0 or max_pending_deployments < 1:
            raise ValueError("Fleet TTL must be nonnegative and deployment capacity must be positive")
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
        self._active_snapshot: FleetSnapshot | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._logger = logging.getLogger("ml_inference")
        self._inflight: dict[str, int] = {}
        self._idle = asyncio.Condition()
        self._expiry_generation: dict[str, int] = {}
        self._closing = False
        self._reconciliation_required = False
        self._admission_lock = asyncio.Lock()
        self._pending_deployments = 0
        self._recovering_deployments = 0
        self._max_pending_deployments = max_pending_deployments

    async def create(self, model: str, uri: str, idempotency_key: str) -> Deployment:
        if self._closing:
            raise ServiceError("SERVICE_SHUTTING_DOWN", "Service is shutting down", status_code=503)
        if self._reconciliation_required:
            raise ServiceError("RUNTIME_UNAVAILABLE", "Fleet state requires recovery", status_code=503)
        version = _version_from_uri(model, uri)
        candidate = Deployment(
            id=f"deploy_{uuid4().hex}", model=model, version=version, uri=uri,
            slot="pending",
        )
        try:
            async with self._admission_lock:
                deployment, created = await self._repository.create_or_get(
                    candidate, idempotency_key, request_fingerprint=sha256(json.dumps([model, uri]).encode()).hexdigest(),
                    allow_create=self._pending_deployments < self._max_pending_deployments,
                )
                if created:
                    self._schedule_deployment(deployment)
        except ValueError as exc:
            raise ServiceError("SCHEMA_VALIDATION_ERROR", str(exc), status_code=409, param="Idempotency-Key") from exc
        return deployment

    async def get(self, deployment_id: str) -> Deployment | None:
        return await self._repository.get(deployment_id)

    async def rollback(self, model: str, *, expected_deployment_id: str | None = None) -> Deployment:
        async with self._rollout_lock:
            if self._reconciliation_required:
                raise ServiceError("RUNTIME_UNAVAILABLE", "Fleet state requires recovery", status_code=503)
            return await self._rollback_serialized(model, expected_deployment_id)

    async def _rollback_serialized(self, model: str, expected_deployment_id: str | None) -> Deployment:
        current = await self._repository.active_for(model)
        previous = self._previous_fleet_deployments.get(model)
        async with self._fleet_lock:
            previous_runtime = self._previous_fleet
        if current is None or previous is None or previous_runtime is None:
            raise ServiceError("DEPLOYMENT_FAILED", "No previous fleet is available", status_code=409)
        if (expected_deployment_id and current.id != expected_deployment_id) or previous.id == current.id:
            raise ServiceError("DEPLOYMENT_FAILED", "Rollback is available only for the latest fleet transition", status_code=409)
        if self._active_fleet_deployments.get(model) is None or self._active_fleet_deployments[model].id != current.id:
            raise ServiceError("DEPLOYMENT_FAILED", "Active fleet does not match persisted routes", status_code=409)
        try:
            healthy = await asyncio.wait_for(self._runtime.health(previous_runtime), timeout=2)
        except Exception as exc:
            raise ServiceError("RUNTIME_UNAVAILABLE", "Previous fleet runtime is unavailable", status_code=503) from exc
        if not healthy:
            raise ServiceError("RUNTIME_UNAVAILABLE", "Previous fleet runtime is unhealthy", status_code=503)
        restored = {
            name: replace(item, runtime_id=previous_runtime.id, runtime_image=previous_runtime.image, status=DeploymentStatus.ACTIVE)
            for name, item in self._previous_fleet_deployments.items()
        }
        try:
            active, former_active = await self._repository.rollback(model, list(restored.values()))
        except LookupError as exc:
            raise ServiceError("DEPLOYMENT_FAILED", str(exc), status_code=409) from exc
        except (Exception, asyncio.CancelledError):
            self._reconciliation_required = True
            raise
        async with self._fleet_lock:
            self._active_fleet, self._previous_fleet = previous_runtime, self._active_fleet
            self._previous_fleet_deployments = dict(self._active_fleet_deployments)
            self._active_fleet_deployments = restored
            self._active_snapshot = FleetSnapshot(previous_runtime, dict(restored))
            standby_fleet = self._previous_fleet
            # Invalidate any earlier expiry timer for the runtime becoming active.
            self._expiry_generation[previous_runtime.id] = self._expiry_generation.get(previous_runtime.id, 0) + 1
        if standby_fleet:
            self._schedule_expiry(standby_fleet, former_active)
        return active

    async def _run(self, deployment: Deployment) -> None:
        async with self._rollout_lock:
            if self._reconciliation_required:
                return
            await self._run_serialized(deployment)

    async def _run_serialized(self, deployment: Deployment) -> None:
        started_at = perf_counter()
        runtime: RuntimeHandle | None = None
        promotion_started = False
        try:
            deployment.status = DeploymentStatus.DOWNLOADING
            await self._repository.save(deployment)
            metadata = await asyncio.wait_for(self._source.resolve(deployment.model, deployment.uri), timeout=120)
            _validate_metadata(metadata, deployment)
            deployment.metadata = metadata

            deployment.status = DeploymentStatus.LOADING
            await self._repository.save(deployment)
            current = await self._repository.list_active()
            if any(item.metadata is None for item in current):
                raise ServiceError("RUNTIME_UNAVAILABLE", "Active fleet metadata is incomplete", status_code=503)
            fleet_models = [item.metadata for item in current if item.metadata and item.model != deployment.model]
            fleet_models.append(metadata)
            runtime = await self._runtime.deploy(fleet_models)
            deployment.runtime_id = runtime.id
            deployment.runtime_image = runtime.image
            await asyncio.wait_for(self._runtime.load(runtime), timeout=240)
            if not await asyncio.wait_for(self._runtime.health(runtime), timeout=2):
                raise ServiceError("MODEL_HEALTHCHECK_FAILED", "Runtime healthcheck failed", status_code=502)

            deployment.status = DeploymentStatus.WARMING_UP
            await self._repository.save(deployment)
            for fleet_model in fleet_models:
                output = await asyncio.wait_for(self._runtime.predict(runtime, fleet_model.name, fleet_model.input_example), timeout=30)
                _validate_output(fleet_model.output_schema, output)
                json.dumps(output, allow_nan=False)
            deployment.status = DeploymentStatus.READY
            await self._repository.save(deployment)
            deployment.activated_at = int(time())
            promotion_started = True
            previous = await self._repository.activate(deployment, current)
            # Preserve the old snapshot for rollback; do not mutate its records.
            updated = [replace(item, runtime_id=runtime.id, runtime_image=runtime.image) for item in current]
            async with self._fleet_lock:
                previous_fleet = self._active_fleet
                self._previous_fleet_deployments = {item.model: item for item in current}
                self._active_fleet_deployments = {
                    item.model: item for item in updated if item.model != deployment.model
                }
                self._active_fleet_deployments[deployment.model] = deployment
                self._active_fleet = runtime
                self._active_snapshot = FleetSnapshot(runtime, dict(self._active_fleet_deployments))
                self._previous_fleet = previous_fleet
            self._metrics.deployments.labels(deployment.model, deployment.version, "active").inc()
            self._metrics.model_load_duration.labels(deployment.model, deployment.version).observe(perf_counter() - started_at)
            self._metrics.runtime_status.labels(deployment.model, deployment.version, "active").set(1)
            self._metrics.active_version.labels(deployment.model, deployment.version).set(1)
            if previous_fleet:
                self._schedule_expiry(previous_fleet, previous)
                await asyncio.wait_for(self._runtime.drain(previous_fleet), timeout=30)
                if previous:
                    self._metrics.runtime_status.labels(previous.model, previous.version, "active").set(0)
                    self._metrics.runtime_status.labels(previous.model, previous.version, "standby").set(1)
                    self._metrics.active_version.labels(previous.model, previous.version).set(0)
        except asyncio.CancelledError:
            if promotion_started:
                self._reconciliation_required = True
            await self._stop_failed_candidate(runtime, check_persisted=promotion_started)
            raise
        except Exception as exc:
            # Once promoted, maintenance failure must never mark the active fleet FAILED.
            if deployment.status == DeploymentStatus.ACTIVE:
                self._logger.exception("deployment_maintenance_failed", extra={"deployment_id": deployment.id})
                return
            if promotion_started:
                # A lost connection during COMMIT has an uncertain outcome.
                # Preserve persisted routes and runtime until restart reconciles them.
                self._logger.exception("fleet_promotion_outcome_unknown", extra={"deployment_id": deployment.id, "runtime_id": runtime.id if runtime else None})
                self._reconciliation_required = True
                return
            code = exc.code if isinstance(exc, ServiceError) else ("DEPLOYMENT_TIMEOUT" if isinstance(exc, TimeoutError) else "MODEL_LOAD_FAILED")
            message = exc.message if isinstance(exc, ServiceError) else "Model deployment failed"
            self._logger.exception("deployment_failed", extra={"deployment_id": deployment.id, "model": deployment.model, "code": code})
            await self._stop_failed_candidate(runtime)
            deployment.status = DeploymentStatus.FAILED
            deployment.failed_at = int(time())
            deployment.error_code = code
            deployment.error_message = message
            try:
                await self._repository.save(deployment)
            except Exception:
                self._logger.exception("deployment_failure_persistence_failed", extra={"deployment_id": deployment.id})
            self._metrics.deployments.labels(deployment.model, deployment.version, "failed").inc()
            self._metrics.deployment_failures.labels(deployment.model, deployment.version, code).inc()
            self._metrics.runtime_status.labels(deployment.model, deployment.version, "failed").set(1)

    async def _stop_failed_candidate(self, runtime: RuntimeHandle | None, *, check_persisted: bool = False) -> None:
        """Remove an unpromoted GREEN fleet after any validation/load failure."""
        if runtime is None:
            return
        async with self._fleet_lock:
            is_active = self._active_fleet is not None and self._active_fleet.id == runtime.id
        if not is_active:
            if check_persisted:
                try:
                    if any(item.runtime_id == runtime.id for item in await self._repository.list_active()):
                        return
                except Exception:
                    self._logger.exception("candidate_cleanup_deferred", extra={"runtime_id": runtime.id})
                    return
            try:
                await asyncio.wait_for(self._runtime.stop(runtime), timeout=30)
            except Exception:
                self._logger.exception("candidate_cleanup_failed", extra={"runtime_id": runtime.id})

    async def active(self, model: str) -> tuple[Deployment, RuntimeHandle]:
        if self._reconciliation_required:
            raise ServiceError("RUNTIME_UNAVAILABLE", "Fleet state requires recovery", status_code=503)
        async with self._fleet_lock:
            snapshot = self._active_snapshot
        if snapshot is None:
            if await self._repository.active_for(model) is None:
                raise ServiceError("MODEL_NOT_FOUND", f"Model '{model}' is not active", status_code=404, param="model")
            raise ServiceError("RUNTIME_UNAVAILABLE", "Active model runtime is unavailable", status_code=503)
        deployment = snapshot.deployments.get(model)
        if deployment is None:
            raise ServiceError("MODEL_NOT_FOUND", f"Model '{model}' is not active", status_code=404, param="model")
        return deployment, snapshot.runtime

    @asynccontextmanager
    async def prediction_runtime(self, model: str):
        # Pin the runtime before expiry can stop it. No suspension occurs between
        # selecting an existing snapshot and incrementing its reference count.
        deployment, runtime = await self.active(model)
        self._inflight[runtime.id] = self._inflight.get(runtime.id, 0) + 1
        try:
            yield deployment, runtime
        finally:
            async with self._idle:
                self._inflight[runtime.id] -= 1
                self._idle.notify_all()

    async def catalog(self) -> list[Deployment]:
        return await self._repository.list_active()

    def metric_model(self, model: str) -> str:
        return model if self._active_snapshot and model in self._active_snapshot.deployments else "unknown"

    async def operational_status(self) -> dict[str, Any]:
        try:
            ready = await self.ready()
        except Exception:
            self._logger.exception("service_status_check_failed")
            ready = False
        return {
            "status": "ready" if ready and not self._closing else "degraded",
            "active_models": len(self._active_snapshot.deployments) if self._active_snapshot else 0,
            "pending_deployments": self._pending_deployments + self._recovering_deployments,
            "recovery_required": self._reconciliation_required,
        }

    async def ready(self) -> bool:
        """A replacement service is ready only after its persisted fleet is usable."""
        if self._reconciliation_required:
            return False
        try:
            async with asyncio.timeout(2):
                active = await self._repository.list_active()
                if not active:
                    return True
                async with self._fleet_lock:
                    snapshot = self._active_snapshot
                return snapshot is not None and await self._runtime.health(snapshot.runtime)
        except Exception:
            self._logger.exception("readiness_check_failed")
            return False

    async def restore(self) -> None:
        """Recreate persisted active/standby handles after a service restart.

        Metadata was saved at deployment time, so this path does not contact the
        MLflow control plane. Any handle that cannot be recreated remains absent
        and requests fail safely with ``RUNTIME_UNAVAILABLE``.
        """
        active = await self._repository.list_active()
        if active and all(item.metadata for item in active):
            try:
                images = {item.runtime_image for item in active}
                runtime_ids = {item.runtime_id for item in active}
                if None in images or len(images) != 1:
                    raise RuntimeError("Persisted active fleet has no single pinned runtime image")
                if None in runtime_ids or len(runtime_ids) != 1:
                    raise RuntimeError("Persisted active fleet has no single runtime ID")
                runtime = await self._runtime.deploy(
                    [item.metadata for item in active if item.metadata],
                    image=images.pop(), runtime_id=runtime_ids.pop(),
                )
                await asyncio.wait_for(self._runtime.load(runtime), timeout=240)
                if await asyncio.wait_for(self._runtime.health(runtime), timeout=2):
                    for item in active:
                        assert item.metadata is not None
                        output = await asyncio.wait_for(self._runtime.predict(runtime, item.model, item.metadata.input_example), timeout=30)
                        _validate_output(item.metadata.output_schema, output)
                    self._active_fleet = runtime
                    self._active_fleet_deployments = {item.model: item for item in active}
                    self._active_snapshot = FleetSnapshot(runtime, dict(self._active_fleet_deployments))
                else:
                    raise RuntimeError("Restored fleet healthcheck failed")
            except Exception:
                self._logger.exception("fleet_restore_failed", extra={"models": [item.model for item in active]})
        incomplete = await self._repository.list_incomplete()
        if incomplete:
            self._recovering_deployments = len(incomplete)
            self._schedule(self._recover(incomplete), "recover_deployments")

    async def _recover(self, deployments: list[Deployment]) -> None:
        # Recovery uses one task even when the durable backlog is large.
        for deployment in deployments:
            try:
                await self._run(deployment)
            finally:
                self._recovering_deployments -= 1

    async def shutdown(self) -> None:
        """Release local handles without killing the persisted active fleet.

        A replacement FastAPI container may already have attached to that fleet.
        Failed candidates are cleaned up before local tasks finish.
        """
        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._fleet_lock:
            self._active_fleet = None
            self._previous_fleet = None
            self._active_snapshot = None
        # Retained runtimes may still serve requests from another service slot.
        # Their expiry is owned by the running manager, not shutdown.

    def _schedule(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _schedule_deployment(self, deployment: Deployment) -> None:
        self._pending_deployments += 1
        task = asyncio.create_task(self._run(deployment), name=deployment.id)
        self._tasks.add(task)
        task.add_done_callback(self._deployment_done)

    def _deployment_done(self, task: asyncio.Task[None]) -> None:
        self._pending_deployments -= 1
        self._task_done(task)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._logger.error("background_task_failed", exc_info=task.exception(), extra={"deployment_id": task.get_name()})

    def _schedule_expiry(self, runtime: RuntimeHandle, deployment: Deployment | None) -> None:
        generation = self._expiry_generation.get(runtime.id, 0) + 1
        self._expiry_generation[runtime.id] = generation
        self._schedule(self._expire_previous_fleet(runtime, deployment, generation), f"expire_{runtime.id}")

    async def _expire_previous_fleet(self, runtime: RuntimeHandle, deployment: Deployment | None, generation: int) -> None:
        await asyncio.sleep(self._previous_ttl_seconds)
        async with self._rollout_lock:
            async with self._fleet_lock:
                if self._expiry_generation.get(runtime.id) != generation:
                    return
                if self._active_fleet and self._active_fleet.id == runtime.id:
                    return
                if self._previous_fleet and self._previous_fleet.id == runtime.id:
                    self._previous_fleet = None
                    self._previous_fleet_deployments = {}
            async with self._idle:
                await self._idle.wait_for(lambda: self._inflight.get(runtime.id, 0) == 0)
            await self._runtime.stop(runtime)
            self._inflight.pop(runtime.id, None)
            self._expiry_generation.pop(runtime.id, None)
            if deployment:
                persisted = await self._repository.get(deployment.id)
                if persisted and persisted.status == DeploymentStatus.STANDBY:
                    persisted.status = DeploymentStatus.REMOVED
                    await self._repository.save(persisted)
                    self._metrics.runtime_status.labels(deployment.model, deployment.version, "standby").set(0)


class PredictionService:
    def __init__(self, deployments: DeploymentManager, runtime: RuntimeBackend, *, timeout_seconds: float = 30, max_concurrent: int = 64) -> None:
        if timeout_seconds <= 0 or max_concurrent < 1:
            raise ValueError("Prediction timeout and concurrency must be positive")
        self._deployments = deployments
        self._runtime = runtime
        self._timeout = timeout_seconds
        self._capacity = asyncio.Semaphore(max_concurrent)

    async def predict(self, model: str, payload: Any) -> tuple[Deployment, Any, float]:
        if self._capacity.locked():
            raise ServiceError("SERVICE_OVERLOADED", "Prediction capacity is exhausted", status_code=503)
        async with self._capacity:
            async with self._deployments.prediction_runtime(model) as (deployment, runtime):
                if deployment.metadata is None:
                    raise ServiceError("RUNTIME_UNAVAILABLE", "Active model metadata is missing", status_code=503)
                _validate_input(deployment.metadata.input_schema, payload)
                try:
                    started_at = perf_counter()
                    output = await asyncio.wait_for(self._runtime.predict(runtime, model, payload), timeout=self._timeout)
                    model_latency = perf_counter() - started_at
                    _validate_output(deployment.metadata.output_schema, output)
                    # Reject NaN, infinity and non-serializable values before sending HTTP headers.
                    json.dumps(output, allow_nan=False)
                    return deployment, output, model_latency
                except TimeoutError as exc:
                    raise ServiceError("MODEL_PREDICTION_TIMEOUT", "Model prediction timed out", status_code=504) from exc
                except ServiceError:
                    raise
                except Exception as exc:
                    logging.getLogger("ml_inference").exception("runtime_prediction_failed", extra={"model": model, "runtime_id": runtime.id})
                    raise ServiceError("MODEL_PREDICTION_FAILED", "Model prediction failed", status_code=502) from exc


def _version_from_uri(model: str, uri: str) -> str:
    prefix = f"models:/{model}/"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", model) or not uri.startswith(prefix) or not re.fullmatch(r"[0-9]+", uri[len(prefix):]) or len(uri[len(prefix):]) > 128:
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
    try:
        for schema in (metadata.input_schema, metadata.output_schema):
            validator_for(schema).check_schema(schema)
        _validate_input(metadata.input_schema, metadata.input_example)
    except (SchemaError, ServiceError) as exc:
        raise ServiceError("MODEL_SIGNATURE_INVALID", "Model schema or input example is invalid", status_code=422) from exc


def _validate_input(schema: dict[str, Any], payload: Any) -> None:
    try:
        json.dumps(payload, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ServiceError("SCHEMA_VALIDATION_ERROR", "Input must contain finite JSON values", param="input") from exc
    try:
        validate(instance=payload, schema=schema)
    except SchemaError as exc:
        raise ServiceError("MODEL_SIGNATURE_INVALID", "Stored model input schema is invalid", status_code=502) from exc
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.path)
        param = f"input.{path}" if path else "input"
        raise ServiceError("SCHEMA_VALIDATION_ERROR", exc.message, param=param) from exc


def _validate_output(schema: dict[str, Any], output: Any) -> None:
    try:
        validate(instance=output, schema=schema)
    except SchemaError as exc:
        raise ServiceError("MODEL_SIGNATURE_INVALID", "Stored model output schema is invalid", status_code=502) from exc
    except ValidationError as exc:
        raise ServiceError("MODEL_OUTPUT_VALIDATION_FAILED", "Model output does not match its schema", status_code=502) from exc
