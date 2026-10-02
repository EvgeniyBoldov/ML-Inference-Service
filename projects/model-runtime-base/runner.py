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
from starlette.concurrency import run_in_threadpool

import mlflow.pyfunc
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
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    for item in manifest["models"]:
        MODELS[item["name"]] = mlflow.pyfunc.load_model(item["path"])
    yield
    MODELS.clear()


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
