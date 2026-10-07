"""Generic fleet runtime loaded from a read-only MLflow artifact manifest."""

from __future__ import annotations

import json
import logging
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from functools import partial
from time import monotonic
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("uvicorn.error")
logger.info("runtime_module_import_started")
import mlflow.pyfunc
logger.info("runtime_mlflow_import_completed")
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


MODELS: dict[str, Any] = {}
MAX_CONCURRENT_PREDICTIONS = int(os.getenv("MAX_CONCURRENT_PREDICTIONS", "64"))
if MAX_CONCURRENT_PREDICTIONS < 1:
    raise ValueError("MAX_CONCURRENT_PREDICTIONS must be positive")
PREDICTION_CAPACITY = asyncio.Semaphore(MAX_CONCURRENT_PREDICTIONS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    manifest_path = os.environ["MODEL_MANIFEST"]
    logger.info("runtime_startup_manifest_read_started path=%s", manifest_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    models = manifest["models"]
    logger.info("runtime_startup_manifest_read_completed model_count=%d", len(models))
    for index, item in enumerate(models, start=1):
        name = item["name"]
        started = monotonic()
        logger.info("runtime_model_load_started model=%s index=%d total=%d", name, index, len(models))
        try:
            MODELS[name] = mlflow.pyfunc.load_model(item["path"])
        except BaseException:
            logger.exception("runtime_model_load_failed model=%s index=%d total=%d", name, index, len(models))
            raise
        logger.info("runtime_model_load_completed model=%s elapsed_seconds=%.3f", name, monotonic() - started)
    logger.info("runtime_startup_completed model_count=%d", len(MODELS))
    yield
    logger.info("runtime_shutdown_started")
    MODELS.clear()
    logger.info("runtime_shutdown_completed")


app = FastAPI(lifespan=lifespan)


class PredictRequest(BaseModel):
    model: str
    input: Any


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "models": sorted(MODELS)}


@app.post("/predict")
async def predict(request: PredictRequest) -> dict[str, Any]:
    model = MODELS.get(request.model)
    if model is None:
        raise HTTPException(status_code=404, detail="model is not loaded")
    if PREDICTION_CAPACITY.locked():
        raise HTTPException(status_code=503, detail="prediction capacity is exhausted")
    async with PREDICTION_CAPACITY:
        try:
            output = await run_in_threadpool(partial(model.predict, request.input))
            output = _to_jsonable(output)
            json.dumps(output, allow_nan=False)
            return {"output": output}
        except Exception as exc:
            logging.getLogger("ml_inference.runtime").exception("model_prediction_failed", extra={"model": request.model})
            raise HTTPException(status_code=500, detail="model prediction failed") from exc


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        try:
            return _to_jsonable(value.to_dict(orient="records"))
        except TypeError:
            return _to_jsonable(value.to_dict())
    if hasattr(value, "tolist"):
        return _to_jsonable(value.tolist())
    if hasattr(value, "item"):
        return _to_jsonable(value.item())
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
