import asyncio
import importlib.util
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.domain import Deployment, DeploymentStatus, ModelMetadata
from app.errors import ServiceError
from app.metrics import ServiceMetrics
from app.repositories import InMemoryDeploymentRepository
from app.runtime import PredictorRuntimeBackend
from app.services import DeploymentManager, FleetSnapshot


def load_runner():
    mlflow = ModuleType("mlflow")
    pyfunc = ModuleType("mlflow.pyfunc")
    mlflow.pyfunc = pyfunc
    path = Path(__import__("app.runtime", fromlist=["__file__"]).__file__).with_name("runtime_runner.py")
    spec = importlib.util.spec_from_file_location("runner_monitoring_tests", path)
    runner = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"mlflow": mlflow, "mlflow.pyfunc": pyfunc, spec.name: runner}):
        spec.loader.exec_module(runner)
    return runner


class RunnerStatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_real_model_invocations_are_counted_per_model(self):
        runner = load_runner()
        runner.MODELS.update({
            "credit": SimpleNamespace(predict=lambda value: {"prediction": 1}),
            "broken": SimpleNamespace(predict=lambda value: {"prediction": float("nan")}),
        })
        for _ in range(3):
            await runner.health()
            await runner.statistics()
        await runner.predict(runner.PredictRequest(model="credit", input={}, purpose="warmup"))
        await runner.predict(runner.PredictRequest(model="credit", input={}))
        with self.assertRaises(HTTPException) as invalid_output:
            await runner.predict(runner.PredictRequest(model="broken", input={}))
        self.assertEqual(invalid_output.exception.status_code, 500)
        with self.assertRaises(HTTPException) as missing:
            await runner.predict(runner.PredictRequest(model="missing", input={}))
        self.assertEqual(missing.exception.status_code, 404)
        runner.PREDICTION_CAPACITY = asyncio.Semaphore(0)
        with self.assertRaises(HTTPException) as overloaded:
            await runner.predict(runner.PredictRequest(model="credit", input={}))
        self.assertEqual(overloaded.exception.status_code, 503)
        result = (await runner.statistics())["statistics"]
        self.assertEqual(result["credit"]["total"], 1)
        self.assertEqual(result["credit"]["completed"], 1)
        self.assertEqual(result["credit"]["failed"], 0)
        self.assertEqual(result["broken"]["total"], 1)
        self.assertEqual(result["broken"]["failed"], 1)
        self.assertEqual(result["broken"]["in_progress"], 0)
        self.assertNotIn("missing", result)

    async def test_running_request_is_visible_without_blocking_health(self):
        runner = load_runner()
        started, release = Event(), Event()

        def predict(_):
            started.set()
            if not release.wait(3):
                raise RuntimeError("test model timed out")
            return {"prediction": 1}

        runner.MODELS["credit"] = SimpleNamespace(predict=predict)
        task = asyncio.create_task(runner.predict(runner.PredictRequest(model="credit", input={})))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            self.assertEqual((await asyncio.wait_for(runner.health(), .5))["status"], "ok")
            row = (await runner.statistics())["statistics"]["credit"]
            self.assertEqual((row["total"], row["in_progress"], row["completed"]), (1, 1, 0))
        finally:
            release.set()
            await task
        self.assertEqual((await runner.statistics())["statistics"]["credit"]["in_progress"], 0)


class RuntimeInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = PredictorRuntimeBackend({
            "models:/credit/1": lambda value: 1,
            "models:/churn/2": lambda value: 2,
            "models:/embedding/3": lambda value: [1],
        })
        self.backend.kind = "docker"
        self.manager = DeploymentManager(InMemoryDeploymentRepository(), None, self.backend, ServiceMetrics())
        metadata = [ModelMetadata(name=name, version=version, uri=f"models:/{name}/{version}", description=name, owner="test", input_schema={}, output_schema={}, input_example={}, producer_image=image) for name, version, image in [("credit", "1", "image-a"), ("churn", "2", "image-a"), ("embedding", "3", "image-b")]]
        first = replace(await self.backend.deploy(metadata[:2], image="image-a", runtime_id="runtime_a"), container_name="runtime-a")
        second = replace(await self.backend.deploy(metadata[2:], image="image-b", runtime_id="runtime_b"), container_name="runtime-b")
        await self.backend.load(first)
        await self.backend.load(second)
        deployments = {item.name: Deployment(id=f"deploy_{item.name}", model=item.name, version=item.version, uri=item.uri, slot="blue", status=DeploymentStatus.ACTIVE, metadata=item, runtime_id=first.id if index < 2 else second.id, runtime_image=item.producer_image) for index, item in enumerate(metadata)}
        self.manager._active_snapshot = FleetSnapshot({first.id: first, second.id: second}, deployments)
        await self.backend.warmup(first, "credit", {})
        await self.backend.predict(first, "credit", {})
        await self.backend.predict(first, "churn", {})
        await self.backend.predict(first, "churn", {})
        await self.backend.predict(second, "embedding", {})

    async def test_groups_share_container_and_keep_separate_model_counts(self):
        inventory = await self.manager.runtime_inventory()
        self.assertEqual(inventory["metrics_source"], "runtime")
        self.assertEqual(len(inventory["data"]), 2)
        first, second = inventory["data"]
        self.assertEqual(first["container_name"], "runtime-a")
        self.assertEqual({item["id"]: item["requests"]["total"] for item in first["models"]}, {"churn": 2, "credit": 1})
        self.assertEqual(second["models"][0]["requests"]["total"], 1)
        await self.manager.runtime_inventory("runtime_a", include_statistics=False)
        fresh = await self.manager.runtime_inventory()
        self.assertEqual(fresh["data"][0]["models"], first["models"])

    async def test_missing_statistics_never_fall_back_to_api_counts(self):
        self.manager._metrics.prediction_requests.labels("credit", "1", "completed").inc(99)
        with patch.object(self.backend, "statistics", AsyncMock(side_effect=RuntimeError("old runner"))):
            groups = (await self.manager.runtime_inventory())["data"]
        self.assertTrue(all(group["status"] == "healthy" for group in groups))
        self.assertTrue(all(group["statistics_status"] == "unavailable" for group in groups))
        self.assertTrue(all(model["requests"] is None for group in groups for model in group["models"]))
        with self.assertRaises(ServiceError) as error:
            await self.manager.runtime_inventory("missing")
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
