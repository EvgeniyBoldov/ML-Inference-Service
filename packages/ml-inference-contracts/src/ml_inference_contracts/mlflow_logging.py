"""Publish MLflow PyFunc models that satisfy the serving metadata contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


CONTRACT_VERSION = "1"
TAG_PREFIX = "ml_inference."


class ModelContractError(ValueError):
    """Raised when a model cannot satisfy the producer-side serving contract."""


@dataclass(frozen=True)
class ModelPublication:
    """Immutable identity of a registered MLflow model version."""

    registered_model_name: str
    model_version: str
    model_uri: str
    run_id: str
    artifact_path: str


def log_pyfunc_model(
    *,
    registered_model_name: str,
    description: str,
    owner: str,
    python_model: Any,
    input_example: Any,
    output_example: Any,
    artifact_path: str = "model",
    source_git_commit: str | None = None,
    tags: Mapping[str, str] | None = None,
    params_schema: Mapping[str, Any] | None = None,
    pip_requirements: list[str] | None = None,
    code_paths: list[str] | None = None,
    run_name: str | None = None,
) -> ModelPublication:
    """Log and register one deployable PyFunc model version.

    The caller supplies input and output examples so MLflow can produce a complete
    signature. The active MLflow run is reused; otherwise a run is created and
    closed by this function.
    """
    _require_text("registered_model_name", registered_model_name)
    _require_text("description", description)
    _require_text("owner", owner)
    _require_text("artifact_path", artifact_path)
    if input_example is None:
        raise ModelContractError("input_example is required")
    if output_example is None:
        raise ModelContractError("output_example is required")
    if python_model is None:
        raise ModelContractError("python_model is required")
    _ensure_json_mapping("params_schema", params_schema)
    _ensure_tag_values(tags)

    try:
        import mlflow
        from mlflow.models import infer_signature
        from mlflow.tracking import MlflowClient
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("Install ml-inference-contracts[mlflow] to publish models") from exc

    signature = infer_signature(input_example, output_example, params=params_schema)
    run_tags = {
        f"{TAG_PREFIX}contract_version": CONTRACT_VERSION,
        f"{TAG_PREFIX}owner": owner.strip(),
        f"{TAG_PREFIX}description": description.strip(),
    }
    if source_git_commit:
        run_tags[f"{TAG_PREFIX}source_git_commit"] = source_git_commit.strip()
    if params_schema:
        run_tags[f"{TAG_PREFIX}params_schema"] = json.dumps(params_schema, separators=(",", ":"))
    if tags:
        run_tags.update({f"{TAG_PREFIX}{key}": value for key, value in tags.items()})

    active_run = mlflow.active_run()
    if active_run is None:
        with mlflow.start_run(run_name=run_name, tags=run_tags) as run:
            return _log_and_register(
                mlflow, MlflowClient, run.info.run_id, registered_model_name,
                description, owner, artifact_path, python_model, input_example,
                signature, pip_requirements, code_paths, run_tags,
            )

    mlflow.set_tags(run_tags)
    return _log_and_register(
        mlflow, MlflowClient, active_run.info.run_id, registered_model_name,
        description, owner, artifact_path, python_model, input_example, signature,
        pip_requirements, code_paths, run_tags,
    )


def _log_and_register(
    mlflow: Any,
    client_type: Any,
    run_id: str,
    model_name: str,
    description: str,
    owner: str,
    artifact_path: str,
    python_model: Any,
    input_example: Any,
    signature: Any,
    pip_requirements: list[str] | None,
    code_paths: list[str] | None,
    contract_tags: Mapping[str, str],
) -> ModelPublication:
    model_info = mlflow.pyfunc.log_model(
        artifact_path=artifact_path,
        python_model=python_model,
        signature=signature,
        input_example=input_example,
        registered_model_name=model_name,
        pip_requirements=pip_requirements,
        code_path=code_paths,
    )
    client = client_type()
    version = getattr(model_info, "registered_model_version", None)
    if version is None:
        version = _find_registered_version(client, model_name, run_id)
    if version is None:
        raise ModelContractError("MLflow did not create a registered model version")

    version_text = str(version)
    client.update_registered_model(model_name, description=description.strip())
    client.update_model_version(model_name, version_text, description=description.strip())
    client.set_registered_model_tag(model_name, f"{TAG_PREFIX}owner", owner.strip())
    for key, value in contract_tags.items():
        client.set_model_version_tag(model_name, version_text, key, value)

    return ModelPublication(
        registered_model_name=model_name,
        model_version=version_text,
        model_uri=f"models:/{model_name}/{version_text}",
        run_id=run_id,
        artifact_path=artifact_path,
    )


def _find_registered_version(client: Any, model_name: str, run_id: str) -> str | None:
    escaped_name = model_name.replace("'", "\\'")
    matches = client.search_model_versions(f"name='{escaped_name}'")
    versions = [item for item in matches if getattr(item, "run_id", None) == run_id]
    if not versions:
        return None
    return max(versions, key=lambda item: int(item.version)).version


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelContractError(f"{name} must be a non-empty string")


def _ensure_json_mapping(name: str, value: Mapping[str, Any] | None) -> None:
    if value is None:
        return
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ModelContractError(f"{name} must be JSON serializable") from exc


def _ensure_tag_values(tags: Mapping[str, str] | None) -> None:
    if tags is None:
        return
    for key, value in tags.items():
        _require_text("tag key", key)
        _require_text(f"tag '{key}'", value)

