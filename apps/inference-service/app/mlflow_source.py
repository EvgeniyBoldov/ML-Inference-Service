"""MLflow adapter that materializes a deployable model's serving metadata."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .domain import ModelMetadata
from .errors import ServiceError
from .schema_adapter import mlflow_signature_to_json_schema


class MlflowModelSource:
    """Read registry metadata and artifacts exclusively through MLflow APIs."""

    def __init__(self, tracking_uri: str | None = None, artifact_cache_root: str | None = None) -> None:
        self._tracking_uri = tracking_uri
        self._artifact_cache_root = Path(artifact_cache_root) if artifact_cache_root else None

    async def resolve(self, model: str, uri: str) -> ModelMetadata:
        try:
            return await asyncio.to_thread(self._resolve_sync, model, uri)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("MLFLOW_UNAVAILABLE", "Unable to retrieve model metadata from MLflow", status_code=503, error_type="server_error") from exc

    def _resolve_sync(self, model: str, uri: str) -> ModelMetadata:
        name, version = _parse_model_uri(model, uri)
        try:
            import mlflow
            from mlflow.tracking import MlflowClient
        except ImportError as exc:
            raise ServiceError("MLFLOW_UNAVAILABLE", "MLflow support is not installed", status_code=503, error_type="server_error") from exc
        if self._tracking_uri:
            mlflow.set_tracking_uri(self._tracking_uri)
        client = MlflowClient(tracking_uri=self._tracking_uri)
        try:
            model_version = client.get_model_version(name, version)
            registered_model = client.get_registered_model(name)
            model_info = mlflow.models.get_model_info(uri)
            input_schema = mlflow_signature_to_json_schema(model_info.signature, "inputs")
            output_schema = mlflow_signature_to_json_schema(model_info.signature, "outputs")
            artifact_dir = _download_model(mlflow, uri, self._artifact_cache_root, name, version)
            input_example = _load_input_example(artifact_dir, model_info)
        except ValueError as exc:
            raise ServiceError("MODEL_SIGNATURE_REQUIRED", str(exc), status_code=422) from exc
        except FileNotFoundError as exc:
            raise ServiceError("INPUT_EXAMPLE_REQUIRED", "MLflow model input example is missing", status_code=422) from exc

        description = (model_version.description or registered_model.description or "").strip()
        if not description:
            raise ServiceError("MODEL_DESCRIPTION_REQUIRED", "MLflow model description is missing", status_code=422)
        owner = (model_version.tags.get("ml_inference.owner") or registered_model.tags.get("ml_inference.owner") or "").strip()
        if not owner:
            raise ServiceError("MODEL_LOAD_FAILED", "MLflow model owner tag ml_inference.owner is missing", status_code=422)
        return ModelMetadata(
            name=name, version=version, uri=uri, description=description, owner=owner,
            input_schema=input_schema, output_schema=output_schema, input_example=input_example,
            artifact_path=str(artifact_dir),
            created_at=int(model_version.creation_timestamp / 1000),
        )


def _parse_model_uri(model: str, uri: str) -> tuple[str, str]:
    prefix = f"models:/{model}/"
    if not uri.startswith(prefix):
        raise ServiceError("MODEL_LOAD_FAILED", "MLflow URI does not match requested model", status_code=422)
    version = uri.removeprefix(prefix)
    if not version or version.startswith("@") or "/" in version:
        raise ServiceError("MODEL_LOAD_FAILED", "MLflow URI must specify an immutable model version", status_code=422)
    return model, version


def _download_model(mlflow: Any, uri: str, cache_root: Path | None, name: str, version: str) -> Path:
    if cache_root is None:
        return Path(mlflow.artifacts.download_artifacts(artifact_uri=uri))
    destination = cache_root / name / version
    destination.mkdir(parents=True, exist_ok=True)
    return Path(mlflow.artifacts.download_artifacts(artifact_uri=uri, dst_path=str(destination)))


def _load_input_example(local_dir: Path, model_info: Any) -> Any:
    info = getattr(model_info, "saved_input_example_info", None) or {}
    artifact_path = info.get("artifact_path", "input_example.json")
    for candidate in (local_dir / artifact_path, local_dir / "input_example.json", local_dir / "serving_input_example.json"):
        if candidate.is_file():
            with candidate.open(encoding="utf-8") as file:
                return json.load(file)
    raise FileNotFoundError(artifact_path)
