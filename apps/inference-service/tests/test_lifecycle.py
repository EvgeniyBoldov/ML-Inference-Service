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
