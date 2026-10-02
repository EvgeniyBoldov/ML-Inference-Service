"""Failure-path regressions for HTTP handling and fleet lifecycle."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from urllib.error import URLError

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from app.auth import FileTokenAuth, StaticTokenAuth
from app.domain import DeploymentStatus
from app.errors import ServiceError
from app.main import create_app
from app.metrics import ServiceMetrics
from app.repositories import InMemoryDeploymentRepository
from app.runtime import DockerFleetRuntimeBackend, PredictorRuntimeBackend, RuntimeHandle
from app.services import DeploymentManager, PredictionService
from test_endpoints import FakeModelSource


URI = "models:/credit/1"
AUTH = StaticTokenAuth({"token": {"deployment.write", "deployment.read", "inference.read", "inference.predict"}})
HEADERS = {"Authorization": "Bearer token"}


def manager_with(runtime, *, ttl=3600, source=None):
    repository = InMemoryDeploymentRepository()
    manager = DeploymentManager(repository, source or FakeModelSource(), runtime, ServiceMetrics(), previous_ttl_seconds=ttl)
    return manager, repository


async def deploy(manager, model="credit", version="1"):
    deployment = await manager.create(model, f"models:/{model}/{version}", f"{model}-{version}")
    tasks = [task for task in manager._tasks if task.get_name() == deployment.id]
    await asyncio.gather(*tasks)
    return await manager.get(deployment.id)


@pytest.mark.asyncio
async def test_http_error_envelopes_and_request_ids():
    app = create_app(source=FakeModelSource(), runtime=PredictorRuntimeBackend(), auth=AUTH)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        cases = [
            ("POST", "/v1/responses", {"json": {}}, 400, "SCHEMA_VALIDATION_ERROR"),
            ("POST", "/v1/responses", {"content": "{"}, 400, "SCHEMA_VALIDATION_ERROR"),
            ("GET", "/missing", {}, 404, "NOT_FOUND"),
            ("POST", "/health/live", {}, 405, "METHOD_NOT_ALLOWED"),
            ("GET", "/v1/models/absent", {}, 404, "MODEL_NOT_FOUND"),
            ("POST", "/internal/v1/deployments", {"json": {"model": "credit", "source": {"type": "mlflow", "uri": "models:/credit/alias"}}, "headers": {**HEADERS, "Idempotency-Key": "key"}}, 400, "SCHEMA_VALIDATION_ERROR"),
        ]
        for method, path, kwargs, status, code in cases:
            kwargs.setdefault("headers", HEADERS)
            response = await client.request(method, path, **kwargs)
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
            assert response.headers["X-Request-ID"].startswith("req_")
        bad_auth = await client.get("/v1/models", headers={"Authorization": "Bearer invalid"})
        assert bad_auth.status_code == 401
        assert bad_auth.headers["WWW-Authenticate"] == "Bearer"
        with patch.object(app.state.deployment_manager, "catalog", AsyncMock(side_effect=RuntimeError("private credential"))):
            error = await client.get("/v1/models", headers=HEADERS)
            assert error.status_code == 500
            assert error.json()["error"]["type"] == "server_error"
            assert "private credential" not in error.text
            assert "X-Request-ID" in error.headers
        with patch.object(app.state.deployment_manager, "catalog", AsyncMock(side_effect=OperationalError("statement", {}, Exception("database down")))):
            error = await client.get("/v1/models", headers=HEADERS)
            assert error.status_code == 503
            assert error.json()["error"]["code"] == "STORAGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_hide_deployment_failure():
    runtime = PredictorRuntimeBackend()
    runtime.stop = AsyncMock(side_effect=RuntimeError("cleanup unavailable"))
    manager, repository = manager_with(runtime)
    try:
        deployment = await deploy(manager)
        assert deployment.status == DeploymentStatus.FAILED
        assert deployment.error_code == "MODEL_LOAD_FAILED"
        assert "No runtime loader" not in deployment.error_message
        assert await repository.list_active() == []
        runtime.stop.assert_awaited_once()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_maintenance_failure_preserves_promoted_fleet():
    runtime = PredictorRuntimeBackend({URI: lambda _: {"prediction": 1}, "models:/credit/2": lambda _: {"prediction": 2}})
    manager, repository = manager_with(runtime)
    try:
        await deploy(manager)
        runtime.drain = AsyncMock(side_effect=RuntimeError("drain failed"))
        upgraded = await deploy(manager, version="2")
        assert upgraded.status == DeploymentStatus.ACTIVE
        assert (await repository.active_for("credit")).id == upgraded.id
        assert (await PredictionService(manager, runtime).predict("credit", {"age": 38}))[1] == {"prediction": 2}
        assert any(task.get_name().startswith("expire_") for task in manager._tasks)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_rollback_restores_all_persisted_runtime_ids_and_rejects_stale_id():
    runtime = PredictorRuntimeBackend({URI: lambda _: {"prediction": 1}, "models:/credit/2": lambda _: {"prediction": 2}, "models:/churn/1": lambda _: {"prediction": 3}})
    manager, repository = manager_with(runtime)
    try:
        first = await deploy(manager)
        await deploy(manager, model="churn")
        old_runtime_id = (await repository.active_for("credit")).runtime_id
        upgrade = await deploy(manager, version="2")
        assert len({item.runtime_id for item in await repository.list_active()}) == 1
        with pytest.raises(ServiceError) as error:
            await manager.rollback("credit", expected_deployment_id=first.id)
        assert error.value.status_code == 409
        assert (await repository.active_for("credit")).id == upgrade.id
        await manager.rollback("credit", expected_deployment_id=upgrade.id)
        assert {item.runtime_id for item in await repository.list_active()} == {old_runtime_id}
        assert (await repository.get(upgrade.id)).status == DeploymentStatus.STANDBY
        # A returned record must not mutate durable state or a fleet snapshot.
        record = await repository.active_for("credit")
        record.status = DeploymentStatus.FAILED
        assert (await repository.active_for("credit")).status == DeploymentStatus.ACTIVE
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_unavailable_previous_runtime_does_not_change_routes():
    runtime = PredictorRuntimeBackend({URI: lambda _: {"prediction": 1}, "models:/credit/2": lambda _: {"prediction": 2}})
    manager, repository = manager_with(runtime)
    try:
        await deploy(manager)
        upgrade = await deploy(manager, version="2")
        runtime.health = AsyncMock(return_value=False)
        with pytest.raises(ServiceError) as error:
            await manager.rollback("credit", expected_deployment_id=upgrade.id)
        assert error.value.status_code == 503
        assert (await repository.active_for("credit")).id == upgrade.id
        runtime.health = AsyncMock(return_value=True)
        manager._previous_fleet = None
        with pytest.raises(ServiceError):
            await manager.rollback("credit", expected_deployment_id=upgrade.id)
        assert (await repository.active_for("credit")).id == upgrade.id
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_expiry_waits_for_predictions_and_ignores_invalidated_timer():
    runtime = PredictorRuntimeBackend({URI: lambda _: {"prediction": 1}, "models:/credit/2": lambda _: {"prediction": 2}})
    runtime.stop = AsyncMock()
    manager, _ = manager_with(runtime, ttl=0)
    try:
        await deploy(manager)
        async with manager.prediction_runtime("credit") as (_, pinned):
            await deploy(manager, version="2")
            await asyncio.sleep(0)
            runtime.stop.assert_not_awaited()
        expiry_tasks = [task for task in manager._tasks if task.get_name().startswith("expire_")]
        await asyncio.gather(*expiry_tasks)
        runtime.stop.assert_awaited_once_with(pinned)
        active = manager._active_fleet
        manager._expiry_generation[pinned.id] = 2
        await manager._expire_previous_fleet(pinned, None, 1)
        assert runtime.stop.await_count == 1
        assert manager._active_fleet == active
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_prediction_timeout_overload_and_nonfinite_output():
    runtime = PredictorRuntimeBackend({URI: lambda _: {"prediction": 1}})
    manager, _ = manager_with(runtime)
    try:
        await deploy(manager)
        prediction = PredictionService(manager, runtime, timeout_seconds=0.02, max_concurrent=1)
        entered = asyncio.Event()

        async def hang(*args):
            entered.set()
            await asyncio.Event().wait()

        runtime.predict = hang
        first = asyncio.create_task(prediction.predict("credit", {"age": 38}))
        await entered.wait()
        with pytest.raises(ServiceError) as error:
            await prediction.predict("credit", {"age": 38})
        assert error.value.code == "SERVICE_OVERLOADED"
        with pytest.raises(ServiceError) as error:
            await first
        assert error.value.code == "MODEL_PREDICTION_TIMEOUT"
        assert error.value.status_code == 504
        runtime.predict = AsyncMock(return_value={"prediction": 1, "extra": float("nan")})
        with pytest.raises(ServiceError) as error:
            await prediction.predict("credit", {"age": 38})
        assert error.value.code == "MODEL_PREDICTION_FAILED"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cleans_up_candidate_and_waits_for_tasks():
    runtime = PredictorRuntimeBackend({URI: lambda _: {"prediction": 1}})
    entered = asyncio.Event()

    async def load(_):
        entered.set()
        await asyncio.Event().wait()

    runtime.load = load
    runtime.stop = AsyncMock()
    manager, repository = manager_with(runtime)
    deployment = await manager.create("credit", URI, "one")
    await entered.wait()
    await manager.shutdown()
    runtime.stop.assert_awaited_once()
    assert not manager._tasks
    assert (await repository.get(deployment.id)).status == DeploymentStatus.LOADING


@pytest.mark.asyncio
async def test_invalid_model_schema_fails_before_provisioning():
    source = FakeModelSource()
    metadata = await source.resolve("credit", URI)
    source.resolve = AsyncMock(return_value=replace(metadata, input_schema={"type": "unknown"}))
    runtime = PredictorRuntimeBackend()
    runtime.deploy = AsyncMock()
    manager, _ = manager_with(runtime, source=source)
    try:
        deployment = await deploy(manager)
        assert deployment.error_code == "MODEL_SIGNATURE_INVALID"
        runtime.deploy.assert_not_awaited()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_auth_storage_failure_is_service_unavailable(tmp_path):
    auth = FileTokenAuth(str(tmp_path / "missing"))
    with pytest.raises(ServiceError) as error:
        await auth.require("inference.predict")(authorization="Bearer secret")
    assert error.value.code == "AUTH_UNAVAILABLE"
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_docker_command_timeout_reaps_process(tmp_path):
    runtime = DockerFleetRuntimeBackend(image_manifest=str(tmp_path / "manifest"), artifact_cache_root=str(tmp_path), command_timeout_seconds=0.01)
    process = AsyncMock()
    process.returncode = None
    process.kill = lambda: setattr(process, "returncode", -9)
    calls = 0

    async def communicate():
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()
        return b"", b""

    process.communicate = communicate
    with patch("app.runtime.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
        with pytest.raises(TimeoutError):
            await runtime._docker("inspect", "runtime")
    assert calls == 2
    assert process.returncode == -9


def test_runtime_transport_errors_are_classified(tmp_path):
    runtime = DockerFleetRuntimeBackend(image_manifest=str(tmp_path / "manifest"), artifact_cache_root=str(tmp_path))
    handle = RuntimeHandle("fleet", {})
    runtime._containers[handle.id] = "fleet"
    for cause, code, status in [(URLError("connection refused"), "RUNTIME_UNAVAILABLE", 503), (TimeoutError(), "MODEL_PREDICTION_TIMEOUT", 504)]:
        with patch("app.runtime.urlopen", side_effect=cause):
            with pytest.raises(ServiceError) as error:
                runtime._http_json(handle, "/predict", {"model": "credit", "input": {}})
            assert (error.value.code, error.value.status_code) == (code, status)


@pytest.mark.asyncio
async def test_full_deployment_queue_still_accepts_idempotent_retry():
    runtime = PredictorRuntimeBackend({URI: lambda _: {"prediction": 1}})
    entered = asyncio.Event()

    async def load(_):
        entered.set()
        await asyncio.Event().wait()

    runtime.load = load
    runtime.stop = AsyncMock()
    manager, repository = manager_with(runtime)
    manager._max_pending_deployments = 1
    try:
        first = await manager.create("credit", URI, "key")
        await entered.wait()
        retry = await manager.create("credit", URI, "key")
        assert retry.id == first.id
        with pytest.raises(ServiceError) as error:
            await manager.create("credit", "models:/credit/2", "another")
        assert error.value.code == "DEPLOYMENT_QUEUE_FULL"
        assert len(await repository.list_incomplete()) == 1
    finally:
        await manager.shutdown()
    assert manager._pending_deployments == 0


@pytest.mark.asyncio
async def test_ambiguous_promotion_does_not_delete_persisted_active_runtime():
    runtime = PredictorRuntimeBackend({URI: lambda _: {"prediction": 1}})
    runtime.stop = AsyncMock()
    manager, repository = manager_with(runtime)
    original = repository.activate

    async def uncertain(candidate, fleet):
        await original(candidate, fleet)
        candidate.status = DeploymentStatus.READY  # Client lost the commit acknowledgement.
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    repository.activate = uncertain
    try:
        deployment = await deploy(manager)
        assert deployment.status == DeploymentStatus.ACTIVE
        runtime.stop.assert_not_awaited()
        assert not await manager.ready()
        with pytest.raises(ServiceError) as error:
            await PredictionService(manager, runtime).predict("credit", {"age": 38})
        assert error.value.status_code == 503
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_restore_does_not_remove_unhealthy_persisted_container(tmp_path):
    runtime = DockerFleetRuntimeBackend(image_manifest=str(tmp_path / "manifest"), artifact_cache_root=str(tmp_path))
    runtime._docker = AsyncMock(return_value=(0, "exists"))
    runtime.health = AsyncMock(return_value=False)
    handle = RuntimeHandle("fleet", {}, "image@sha256:123", attach_existing=True)
    with pytest.raises(ServiceError) as error:
        await runtime.load(handle)
    assert error.value.code == "RUNTIME_UNAVAILABLE"
    assert runtime._docker.await_args_list[0].args == ("inspect", "fleet")
    assert runtime._docker.await_count == 1
