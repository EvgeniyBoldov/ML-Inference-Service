"""Status, Prometheus instrumentation and MinIO configuration regressions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from app.auth import StaticTokenAuth
from app.errors import ServiceError
from app.main import create_app
from app.mlflow_source import _download_model, validate_artifact_storage_config
from app.runtime import PredictorRuntimeBackend
from test_endpoints import FakeModelSource


@pytest.mark.asyncio
async def test_status_and_prometheus_cover_all_request_outcomes():
    app = create_app(source=FakeModelSource(), runtime=PredictorRuntimeBackend(), auth=StaticTokenAuth({"deploy": {"metrics.read", "deployment.read"}, "predict": {"inference.predict"}}))
    headers = {"Authorization": "Bearer deploy"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/v1/models")).status_code == 401
        assert (await client.post("/v1/responses", headers={"Authorization": "Bearer predict"}, json={})).status_code == 400
        for identity in ("arbitrary-id-1", "arbitrary-id-2"):
            assert (await client.get(f"/internal/v1/deployments/{identity}", headers=headers)).status_code == 404
        assert (await client.get("/arbitrary-path-1")).status_code == 404
        assert (await client.get("/arbitrary-path-2")).status_code == 404
        with patch.object(app.state.deployment_manager, "get", AsyncMock(side_effect=RuntimeError("private secret"))):
            assert (await client.get("/internal/v1/deployments/failed", headers=headers)).status_code == 500
        status = await client.get("/internal/v1/status", headers=headers)
        assert status.status_code == 200
        body = status.json()
        assert body["status"] == "ready"
        assert body["requests"]["total"] == 8
        assert body["requests"]["successful"] == 1
        assert body["requests"]["client_errors"] == 6
        assert body["requests"]["server_errors"] == 1
        assert body["requests"]["in_progress"] == 1  # This status request is running.
        assert body["active_models"] == 0
        assert body["pending_deployments"] == 0
        assert body["uptime_seconds"] >= 0
        assert "arbitrary-id" not in status.text
        assert "arbitrary-path" not in status.text
        metrics = await client.get("/metrics", headers=headers)
        assert metrics.status_code == 200
        assert metrics.headers["content-type"].startswith("text/plain")
        assert 'http_requests_total{method="GET",route="/internal/v1/deployments/{deployment_id}",status_code="404"} 2.0' in metrics.text
        assert 'http_requests_total{method="GET",route="unmatched",status_code="404"} 2.0' in metrics.text
        assert "http_request_duration_seconds_bucket" in metrics.text
        assert "http_requests_in_progress" in metrics.text
        assert "service_uptime_seconds" in metrics.text
        assert (await client.get("/internal/v1/status")).status_code == 401
        assert (await client.get("/internal/v1/status", headers={"Authorization": "Bearer predict"})).status_code == 403
        with patch.object(app.state.deployment_manager, "ready", AsyncMock(side_effect=RuntimeError("database unavailable"))):
            degraded = await client.get("/internal/v1/status", headers=headers)
            assert degraded.status_code == 200
            assert degraded.json()["status"] == "degraded"
            assert "database unavailable" not in degraded.text


@pytest.fixture
def clean_storage_env(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "MLFLOW_S3_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_minio_requires_complete_credentials(clean_storage_env):
    env = clean_storage_env
    validate_artifact_storage_config()  # Artifact proxy mode needs no client-side keys.
    env.setenv("MLFLOW_S3_ENDPOINT_URL", "http://minio:9000")
    with pytest.raises(RuntimeError, match="MinIO requires"):
        validate_artifact_storage_config()
    env.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    with pytest.raises(RuntimeError, match="configured together"):
        validate_artifact_storage_config()
    env.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    validate_artifact_storage_config()
    assert __import__("os").environ["AWS_SECRET_ACCESS_KEY"] == "test-secret-key"


@pytest.mark.parametrize("endpoint", ["s3://bucket", "http://user:secret@minio:9000", "http://minio:9000/bucket", "http://minio:9000?secret=value"])
def test_minio_endpoint_rejects_credentials_and_bucket_paths(clean_storage_env, endpoint):
    clean_storage_env.setenv("MLFLOW_S3_ENDPOINT_URL", endpoint)
    with pytest.raises(RuntimeError):
        validate_artifact_storage_config()


def test_explicit_credential_profile_is_supported(clean_storage_env):
    clean_storage_env.setenv("MLFLOW_S3_ENDPOINT_URL", "https://minio.internal")
    clean_storage_env.setenv("AWS_PROFILE", "minio")
    validate_artifact_storage_config()


@pytest.mark.parametrize("cause, code", [(NoCredentialsError(), "ARTIFACT_CREDENTIALS_MISSING"), (ClientError({"Error": {"Code": "AccessDenied", "Message": "secret upstream detail"}}, "GetObject"), "ARTIFACT_AUTH_FAILED")])
def test_minio_download_errors_have_safe_codes(tmp_path, cause, code):
    artifacts = SimpleNamespace(download_artifacts=lambda **_: None)
    mlflow = SimpleNamespace(artifacts=artifacts)
    with patch.object(artifacts, "download_artifacts", side_effect=cause):
        with pytest.raises(ServiceError) as error:
            _download_model(mlflow, "models:/credit/1", tmp_path, "credit", "1")
    assert error.value.code == code
    assert "secret upstream detail" not in error.value.message
