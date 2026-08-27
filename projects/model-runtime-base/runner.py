"""Generic fleet runtime loaded from a read-only MLflow artifact manifest."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import mlflow.pyfunc
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


MODELS: dict[str, Any] = {}


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
    return {"output": _to_jsonable(model.predict(request.input))}


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict()
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
