"""Ensure synchronous model code does not block runtime health requests."""

import asyncio
import importlib.util
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import httpx
import pytest


@pytest.fixture
def runner():
    # The fake MLflow loader keeps this test independent of a model environment.
    mlflow = ModuleType("mlflow")
    pyfunc = ModuleType("mlflow.pyfunc")
    mlflow.pyfunc = pyfunc
    spec = importlib.util.spec_from_file_location("inference_runtime_runner", Path(__file__).resolve().parents[3] / "projects/model-runtime-base/runner.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"mlflow": mlflow, "mlflow.pyfunc": pyfunc}):
        spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_health_responds_while_prediction_is_running(runner):
    started = Event()
    release = Event()

    def predict(_):
        started.set()
        assert release.wait(timeout=3)
        return {"prediction": 1}

    runner.MODELS["credit"] = SimpleNamespace(predict=predict)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app), base_url="http://runtime") as client:
        task = asyncio.create_task(client.post("/predict", json={"model": "credit", "input": {}}))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            health = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            assert health.json() == {"status": "ok", "models": ["credit"]}
            runner.PREDICTION_CAPACITY = asyncio.Semaphore(0)
            overloaded = await client.post("/predict", json={"model": "credit", "input": {}})
            assert overloaded.status_code == 503
        finally:
            release.set()
            result = await task
        assert result.json()["output"] == {"prediction": 1}


@pytest.mark.asyncio
async def test_invalid_output_is_a_safe_error(runner):
    runner.MODELS["credit"] = SimpleNamespace(predict=lambda _: {"prediction": float("nan")})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app), base_url="http://runtime") as client:
        response = await client.post("/predict", json={"model": "credit", "input": {}})
        assert response.status_code == 500
        assert response.json() == {"detail": "model prediction failed"}
