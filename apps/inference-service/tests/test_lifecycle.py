"""Operator lifecycle changes preserve routing, isolation and durable power state."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.auth import StaticTokenAuth
from app.errors import ServiceError
from app.main import create_app
from app.metrics import ServiceMetrics
from app.repositories import InMemoryDeploymentRepository
from app.runtime import PredictorRuntimeBackend
from app.services import DeploymentManager, PredictionService
from test_endpoints import FakeModelSource


async def fleet():
    repository = InMemoryDeploymentRepository()
    runtime = PredictorRuntimeBackend({f"models:/{name}/1": lambda value: {"prediction": 1} for name in ("credit", "churn")})
    source = FakeModelSource()
    manager = DeploymentManager(repository, source, runtime, ServiceMetrics(), previous_ttl_seconds=3600)
    for name in ("credit", "churn"):
        item = await manager.create(name, f"models:/{name}/1", name)
        while (await manager.get(item.id)).status not in {"active", "failed"}:
            await asyncio.sleep(0)
        assert (await manager.get(item.id)).status == "active"
    return manager, repository, runtime, source


@pytest.mark.asyncio
async def test_model_deactivation_and_container_power_survive_restore():
    manager, repository, runtime, source = await fleet()
    try:
        group = (await manager.runtime_inventory())["data"][0]
        await manager.lifecycle("credit", "stop", model=True)
        with pytest.raises(ServiceError) as error:
            await manager.active("credit")
        assert error.value.code == "MODEL_NOT_FOUND"
        await manager.active("churn")
        assert [item.model for item in await manager.catalog()] == ["churn"]
        await manager.lifecycle(group["id"], "stop")
        assert not await manager.catalog()
        restored = DeploymentManager(repository, source, runtime, ServiceMetrics())
        await restored.restore()
        try:
            row = (await restored.runtime_inventory())["data"][0]
            assert row["status"] == "stopped"
            assert {item["id"]: item["enabled"] for item in row["models"]} == {"credit": False, "churn": True}
            with pytest.raises(ServiceError) as error:
                await restored.lifecycle("credit", "start", model=True)
            assert error.value.code == "RUNTIME_STOPPED"
            await restored.lifecycle(row["id"], "start")
            assert [item.model for item in await restored.catalog()] == ["churn"]
            await restored.lifecycle("credit", "start", model=True)
            prediction = PredictionService(restored, runtime)
            assert (await prediction.predict("credit", {"age": 20}))[1] == {"prediction": 1}
        finally:
            await restored.shutdown()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_redeploy_resolves_mlflow_and_failure_preserves_live_group():
    manager, repository, runtime, source = await fleet()
    try:
        source.resolve = AsyncMock(wraps=source.resolve)
        original = (await manager.runtime_inventory())["data"][0]
        await manager.lifecycle("credit", "stop", model=True)
        await manager.lifecycle("credit", "redeploy", model=True)
        source.resolve.assert_awaited_once_with("credit", "models:/credit/1")
        fresh = (await manager.runtime_inventory())["data"][0]
        assert fresh["id"] != original["id"]
        assert {item["id"] for item in fresh["models"]} == {"credit", "churn"}
        assert not next(item for item in fresh["models"] if item["id"] == "credit")["enabled"]
        before = await repository.list_managed()
        runtime.load = AsyncMock(side_effect=RuntimeError("broken candidate"))
        runtime.stop = AsyncMock(wraps=runtime.stop)
        with pytest.raises(RuntimeError, match="broken candidate"):
            await manager.lifecycle(fresh["id"], "redeploy")
        assert await repository.list_managed() == before
        runtime.stop.assert_awaited_once()
        assert (await manager.runtime_inventory())["data"][0]["id"] == fresh["id"]
        await manager.active("churn")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_predictions_and_blocks_new_requests():
    manager, repository, runtime, source = await fleet()
    try:
        _, handle = await manager.active("credit")
        runtime.stop = AsyncMock(wraps=runtime.stop)
        async with manager.prediction_runtime("credit"):
            task = asyncio.create_task(manager.lifecycle(handle.id, "stop"))
            while (await repository.active_for("credit")).runtime_enabled:
                await asyncio.sleep(0)
            assert not task.done()
            runtime.stop.assert_not_awaited()
            with pytest.raises(ServiceError):
                await manager.active("churn")
        await task
        runtime.stop.assert_awaited_once_with(handle)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_lifecycle_endpoints_require_write_permission_and_validate_action():
    app = create_app(source=FakeModelSource(), runtime=PredictorRuntimeBackend(), auth=StaticTokenAuth({"read": {"metrics.read"}, "write": {"deployment.write"}}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for kind in ("models", "runtimes"):
            path = f"/internal/v1/{kind}/missing/stop"
            assert (await client.post(path)).status_code == 401
            assert (await client.post(path, headers={"Authorization": "Bearer read"})).status_code == 403
            assert (await client.post(f"/internal/v1/{kind}/missing/delete", headers={"Authorization": "Bearer write"})).status_code == 400


@pytest.mark.asyncio
async def test_model_redeploy_splits_changed_image_and_keeps_neighbour_pinned():
    manager, repository, runtime, source = await fleet()
    try:
        original = source.resolve
        async def changed(model, uri):
            return replace(await original(model, uri), producer_image="new-image")
        source.resolve = AsyncMock(side_effect=changed)
        await manager.lifecycle("credit", "redeploy", model=True)
        groups = (await manager.runtime_inventory())["data"]
        assert len(groups) == 2
        assert {row["image"]: [item["id"] for item in row["models"]] for row in groups} == {
            "new-image": ["credit"], "test/producer:unit": ["churn"],
        }
        source.resolve.assert_awaited_once_with("credit", "models:/credit/1")
        for name in ("credit", "churn"):
            await manager.active(name)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_model_stop_start_only_switches_request_admission():
    manager, repository, runtime, source = await fleet()
    try:
        _, before = await manager.active("credit")
        runtime.deploy = AsyncMock(wraps=runtime.deploy)
        runtime.load = AsyncMock(wraps=runtime.load)
        runtime.stop = AsyncMock(wraps=runtime.stop)
        async with manager.prediction_runtime("credit"):
            await manager.lifecycle("credit", "stop", model=True)
            with pytest.raises(ServiceError):
                await PredictionService(manager, runtime).predict("credit", {"age": 20})
        await manager.lifecycle("credit", "start", model=True)
        assert (await manager.active("credit"))[1] is before
        assert (await PredictionService(manager, runtime).predict("credit", {"age": 20}))[1] == {"prediction": 1}
        runtime.deploy.assert_not_awaited()
        runtime.load.assert_not_awaited()
        runtime.stop.assert_not_awaited()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_delete_shared_model_removes_history_and_keys_and_survives_restore():
    manager, repository, runtime, source = await fleet()
    try:
        runtime._predictors["models:/credit/2"] = lambda value: {"prediction": 2}
        upgrade = await manager.create("credit", "models:/credit/2", "credit-v2")
        while (await manager.get(upgrade.id)).status not in {"active", "failed"}:
            await asyncio.sleep(0)
        assert (await manager.get(upgrade.id)).status == "active"
        credit_ids = [item.id for item in repository._deployments.values() if item.model == "credit"]
        await manager.lifecycle("churn", "stop", model=True)
        runtime.stop = AsyncMock(wraps=runtime.stop)
        await manager.delete_model("credit")
        assert all([await repository.get(identity) is None for identity in credit_ids])
        assert await repository.active_for("credit") is None
        assert "credit" not in repository._routes
        assert "credit" not in repository._idempotency
        groups = (await manager.runtime_inventory())["data"]
        assert len(groups) == 1
        assert [(item["id"], item["enabled"]) for item in groups[0]["models"]] == [("churn", False)]
        assert all("credit" not in handle.models for handle in manager._runtime_handles.values())
        assert manager._previous_snapshot is None
        assert runtime.stop.await_count >= 1
        with pytest.raises(ServiceError):
            await manager.rollback("churn")
        restored = DeploymentManager(repository, source, runtime, ServiceMetrics())
        await restored.restore()
        try:
            assert {item["id"] for group in (await restored.runtime_inventory())["data"] for item in group["models"]} == {"churn"}
            await restored.lifecycle("churn", "start", model=True)
            assert (await PredictionService(restored, runtime).predict("churn", {"age": 20}))[1] == {"prediction": 1}
        finally:
            await restored.shutdown()
        new = await manager.create("credit", "models:/credit/1", "credit")
        while (await manager.get(new.id)).status not in {"active", "failed"}:
            await asyncio.sleep(0)
        assert new.id not in credit_ids
        assert (await manager.get(new.id)).status == "active"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_delete_waits_for_old_requests_and_keeps_neighbour_available():
    manager, repository, runtime, source = await fleet()
    try:
        _, old = await manager.active("credit")
        runtime.stop = AsyncMock(wraps=runtime.stop)
        async with manager.prediction_runtime("credit"):
            deleting = asyncio.create_task(manager.delete_model("credit"))
            while await repository.active_for("credit") is not None:
                await asyncio.sleep(0)
            assert not deleting.done()
            assert old not in [call.args[0] for call in runtime.stop.await_args_list]
            with pytest.raises(ServiceError):
                await manager.active("credit")
            assert (await PredictionService(manager, runtime).predict("churn", {"age": 20}))[1] == {"prediction": 1}
        await deleting
        assert old in [call.args[0] for call in runtime.stop.await_args_list]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_delete_stopped_group_and_last_model_does_not_start_containers():
    manager, repository, runtime, source = await fleet()
    try:
        group = (await manager.runtime_inventory())["data"][0]
        await manager.lifecycle(group["id"], "stop")
        runtime.deploy = AsyncMock(wraps=runtime.deploy)
        await manager.delete_model("credit")
        remaining = (await manager.runtime_inventory())["data"][0]
        assert remaining["status"] == "stopped"
        assert [item["id"] for item in remaining["models"]] == ["churn"]
        await manager.delete_model("churn")
        assert not (await manager.runtime_inventory())["data"]
        assert not await repository.list_managed()
        assert await manager.ready()
        runtime.deploy.assert_not_awaited()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_delete_candidate_failure_preserves_model_and_neighbours():
    manager, repository, runtime, source = await fleet()
    try:
        before = await repository.list_managed()
        runtime.load = AsyncMock(side_effect=RuntimeError("broken candidate"))
        runtime.stop = AsyncMock(wraps=runtime.stop)
        with pytest.raises(RuntimeError, match="broken candidate"):
            await manager.delete_model("credit")
        assert await repository.list_managed() == before
        await manager.active("credit")
        await manager.active("churn")
        runtime.stop.assert_awaited_once()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_deleted_queued_deployment_cannot_recreate_model():
    manager, repository, runtime, source = await fleet()
    try:
        async with manager._rollout_lock:
            pending = await manager.create("credit", "models:/credit/2", "pending-credit")
            # Serialize deletion ahead of the queued deployment to exercise the
            # same existence check used for recovered deployment records.
            await repository.delete_model("credit", [])
        while manager._pending_deployments:
            await asyncio.sleep(0)
        assert await repository.get(pending.id) is None
        assert not [item for item in repository._deployments.values() if item.model == "credit"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_delete_endpoint_permissions_and_failed_deployment_cleanup():
    from app.domain import Deployment, DeploymentStatus
    repository = InMemoryDeploymentRepository()
    failed = Deployment("failed-credit", "credit", "1", "models:/credit/1", "green", status=DeploymentStatus.FAILED)
    await repository.create_or_get(failed, "failed-key", "fingerprint")
    app = create_app(repository=repository, source=FakeModelSource(), runtime=PredictorRuntimeBackend(), auth=StaticTokenAuth({"read": {"inference.read"}, "write": {"deployment.write"}}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/internal/v1/models/credit"
        assert (await client.delete(path)).status_code == 401
        assert (await client.delete(path, headers={"Authorization": "Bearer read"})).status_code == 403
        result = await client.delete(path, headers={"Authorization": "Bearer write"})
        assert result.status_code == 204
        assert result.content == b""
        assert await repository.get(failed.id) is None
        assert "failed-key" not in repository._idempotency
        assert (await client.delete(path, headers={"Authorization": "Bearer write"})).status_code == 404
