import asyncio

import httpx

from app.auth import StaticTokenAuth
from app.domain import ModelMetadata
from app.main import create_app
from app.runtime import PredictorRuntimeBackend


class FakeModelSource:
    async def resolve(self, model: str, uri: str) -> ModelMetadata:
        return ModelMetadata(
            name=model,
            version="18",
            uri=uri,
            description="Probability of customer default.",
            owner="risk-team",
            input_schema={
                "type": "object",
                "properties": {"age": {"type": "integer"}},
                "required": ["age"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "properties": {"prediction": {"type": "integer"}}, "required": ["prediction"]},
            input_example={"age": 38},
        )


def test_deploy_discover_predict_and_rollback_contract() -> None:
    uri = "models:/credit-scoring/18"
    app = create_app(
        source=FakeModelSource(),
        runtime=PredictorRuntimeBackend({uri: lambda payload: {"prediction": int(payload["age"] >= 18)}}),
        auth=StaticTokenAuth({"read": {"inference.read"}, "predict": {"inference.predict"}, "deploy": {"deployment.write", "deployment.read"}}),
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/internal/v1/deployments",
                headers={"Authorization": "Bearer deploy", "Idempotency-Key": "run-1"},
                json={"model": "credit-scoring", "source": {"type": "mlflow", "uri": uri}},
            )
            assert created.status_code == 202
            deployment_id = created.json()["id"]
            retry = await client.post(
                "/internal/v1/deployments",
                headers={"Authorization": "Bearer deploy", "Idempotency-Key": "run-1"},
                json={"model": "credit-scoring", "source": {"type": "mlflow", "uri": uri}},
            )
            assert retry.json()["id"] == deployment_id
            for _ in range(20):
                status = await client.get(f"/internal/v1/deployments/{deployment_id}", headers={"Authorization": "Bearer deploy"})
                if status.json()["status"] in {"active", "failed"}:
                    break
                await asyncio.sleep(0)
            assert status.json()["status"] == "active"
            models = await client.get("/v1/models", headers={"Authorization": "Bearer read"})
            assert models.json()["data"][0]["version"] == "18"
            invalid = await client.post("/v1/responses", headers={"Authorization": "Bearer predict"}, json={"model": "credit-scoring", "input": {}})
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "SCHEMA_VALIDATION_ERROR"
            response = await client.post("/v1/responses", headers={"Authorization": "Bearer predict"}, json={"model": "credit-scoring", "input": {"age": 38}})
            assert response.json()["model_version"] == "18"
            assert response.json()["output"][0]["content"] == {"prediction": 1}

    asyncio.run(scenario())
