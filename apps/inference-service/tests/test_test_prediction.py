from hashlib import sha256

import httpx
import pytest

from app.auth import FileTokenAuth
from app.main import create_app
from app.model_source import UnavailableModelSource
from app.repositories import InMemoryDeploymentRepository
from app.runtime import PredictorRuntimeBackend


@pytest.fixture
def app(tmp_path):
    token_file = tmp_path / "tokens"
    token_file.write_text("\n".join(
        f"{role}_test {role} {sha256(token.encode()).hexdigest()}"
        for role, token in [("predict", "predict-token"), ("deploy", "deploy-token")]
    ))
    return create_app(
        source=UnavailableModelSource(), runtime=PredictorRuntimeBackend(),
        repository=InMemoryDeploymentRepository(), auth=FileTokenAuth(str(token_file)),
    )


@pytest.mark.parametrize("payload", [{"text": "hello"}, [1, "two"], "hello", 42, False, None])
async def test_prediction_without_deployed_models(app, payload):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/test/predict", json={"input": payload},
                headers={"Authorization": "Bearer predict-token"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["object"] == "response"
            assert body["status"] == "completed"
            assert body["model"] == "test-model"
            assert body["model_version"] == "1"
            assert body["output"] == [{"type": "prediction", "content": {"prediction": "test-response", "input": payload}}]
            assert body["id"].startswith("resp_")
            assert isinstance(body["created_at"], int)
            assert response.headers["X-Request-ID"].startswith("req_")
            catalog = await client.get("/v1/models", headers={"Authorization": "Bearer predict-token"})
            assert catalog.json()["data"] == []


@pytest.mark.parametrize("token,status", [(None, 401), ("invalid", 401), ("deploy-token", 403)])
async def test_prediction_requires_predict_token(app, token, status):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/test/predict", json={"input": {}},
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        assert response.status_code == status
        assert "error" in response.json()


@pytest.mark.parametrize("body", ["{}", "{", "[]"])
async def test_prediction_rejects_invalid_body(app, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/test/predict", content=body,
            headers={"Authorization": "Bearer predict-token", "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SCHEMA_VALIDATION_ERROR"
