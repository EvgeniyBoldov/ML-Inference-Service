"""Runtime boundary. Production backends can use processes, containers, or pods."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from .domain import ModelMetadata


@dataclass(frozen=True)
class RuntimeHandle:
    id: str
    metadata: ModelMetadata


class RuntimeBackend(Protocol):
    async def deploy(self, metadata: ModelMetadata) -> RuntimeHandle: ...
    async def load(self, runtime: RuntimeHandle) -> None: ...
    async def predict(self, runtime: RuntimeHandle, payload: Any) -> Any: ...
    async def health(self, runtime: RuntimeHandle) -> bool: ...
    async def drain(self, runtime: RuntimeHandle) -> None: ...
    async def stop(self, runtime: RuntimeHandle) -> None: ...


class PredictorRuntimeBackend:
    """In-process backend for development and contract tests only.

    It intentionally accepts a resolver function. The production MLflow runtime
    adapter will replace this with an isolated process/container implementation.
    """

    def __init__(self, predictors: dict[str, Any] | None = None) -> None:
        self._predictors = predictors or {}

    async def deploy(self, metadata: ModelMetadata) -> RuntimeHandle:
        return RuntimeHandle(id=f"runtime_{uuid4().hex}", metadata=metadata)

    async def load(self, runtime: RuntimeHandle) -> None:
        if runtime.metadata.uri not in self._predictors:
            raise RuntimeError(f"No runtime loader registered for {runtime.metadata.uri}")

    async def predict(self, runtime: RuntimeHandle, payload: Any) -> Any:
        predictor = self._predictors[runtime.metadata.uri]
        result = await asyncio.to_thread(predictor, payload)
        return result

    async def health(self, runtime: RuntimeHandle) -> bool:
        return runtime.metadata.uri in self._predictors

    async def drain(self, runtime: RuntimeHandle) -> None:
        return None

    async def stop(self, runtime: RuntimeHandle) -> None:
        return None


class MlflowPyfuncRuntimeBackend:
    """MLflow PyFunc adapter for a compatible local environment.

    Use it for development. A process/container runtime backend remains required
    in production when deployed model dependencies can differ.
    """

    def __init__(self) -> None:
        self._models: dict[str, Any] = {}

    async def deploy(self, metadata: ModelMetadata) -> RuntimeHandle:
        return RuntimeHandle(id=f"runtime_{uuid4().hex}", metadata=metadata)

    async def load(self, runtime: RuntimeHandle) -> None:
        try:
            import mlflow
        except ImportError as exc:
            raise RuntimeError("MLflow support is not installed") from exc
        self._models[runtime.id] = await asyncio.to_thread(mlflow.pyfunc.load_model, runtime.metadata.uri)

    async def predict(self, runtime: RuntimeHandle, payload: Any) -> Any:
        return _to_jsonable(await asyncio.to_thread(self._models[runtime.id].predict, payload))

    async def health(self, runtime: RuntimeHandle) -> bool:
        return runtime.id in self._models

    async def drain(self, runtime: RuntimeHandle) -> None:
        return None

    async def stop(self, runtime: RuntimeHandle) -> None:
        self._models.pop(runtime.id, None)


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
