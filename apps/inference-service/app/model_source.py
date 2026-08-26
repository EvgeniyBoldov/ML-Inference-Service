"""Control-plane model metadata boundary; MLflow implementation belongs here later."""

from __future__ import annotations

from typing import Protocol

from .domain import ModelMetadata
from .errors import ServiceError


class ModelSource(Protocol):
    async def resolve(self, model: str, uri: str) -> ModelMetadata: ...


class UnavailableModelSource:
    async def resolve(self, model: str, uri: str) -> ModelMetadata:
        raise ServiceError(
            "MLFLOW_UNAVAILABLE",
            "MLflow adapter is not configured",
            status_code=503,
            error_type="server_error",
        )

