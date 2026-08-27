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
            version=uri.rsplit("/", 1)[1],
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
    upgraded_uri = "models:/credit-scoring/19"
    churn_uri = "models:/churn/7"
    app = create_app(
        source=FakeModelSource(),
        runtime=PredictorRuntimeBackend({
            uri: lambda payload: {"prediction": int(payload["age"] >= 18)},
            upgraded_uri: lambda payload: {"prediction": int(payload["age"] >= 21)},
            churn_uri: lambda payload: {"prediction": int(payload["age"] >= 30)},
        }),
        auth=StaticTokenAuth({"read": {"inference.read"}, "predict": {"inference.predict"}, "deploy": {"deployment.write", "deployment.read", "metrics.read"}}),
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
            fleet_update = await client.post(
                "/internal/v1/deployments",
                headers={"Authorization": "Bearer deploy", "Idempotency-Key": "run-2"},
                json={"model": "churn", "source": {"type": "mlflow", "uri": churn_uri}},
            )
            churn_deployment_id = fleet_update.json()["id"]
            for _ in range(20):
                churn_status = await client.get(f"/internal/v1/deployments/{churn_deployment_id}", headers={"Authorization": "Bearer deploy"})
                if churn_status.json()["status"] in {"active", "failed"}:
                    break
                await asyncio.sleep(0)
            assert churn_status.json()["status"] == "active"
            old_model_after_fleet_switch = await client.post("/v1/responses", headers={"Authorization": "Bearer predict"}, json={"model": "credit-scoring", "input": {"age": 38}})
            assert old_model_after_fleet_switch.json()["output"][0]["content"] == {"prediction": 1}
            upgraded = await client.post(
                "/internal/v1/deployments",
                headers={"Authorization": "Bearer deploy", "Idempotency-Key": "run-3"},
                json={"model": "credit-scoring", "source": {"type": "mlflow", "uri": upgraded_uri}},
            )
            upgraded_id = upgraded.json()["id"]
            for _ in range(20):
                upgraded_status = await client.get(f"/internal/v1/deployments/{upgraded_id}", headers={"Authorization": "Bearer deploy"})
                if upgraded_status.json()["status"] in {"active", "failed"}:
                    break
                await asyncio.sleep(0)
            assert upgraded_status.json()["status"] == "active"
            rollback = await client.post(f"/internal/v1/deployments/{upgraded_id}/rollback", headers={"Authorization": "Bearer deploy"})
            assert rollback.status_code == 200
            assert rollback.json()["version"] == "18"
            metrics = await client.get("/metrics", headers={"Authorization": "Bearer deploy"})
            assert metrics.status_code == 200
            assert "prediction_requests_total" in metrics.text

    asyncio.run(scenario())
