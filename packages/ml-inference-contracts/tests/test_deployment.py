from ml_inference_contracts.deployment import DeploymentResult


def test_deployment_result_normalizes_status_and_error() -> None:
    result = DeploymentResult.from_payload(
        {
            "id": "deploy_1",
            "model": "credit-scoring",
            "version": "18",
            "status": "FAILED",
            "error": {"code": "MODEL_LOAD_FAILED", "message": "bad artifact"},
        }
    )

    assert result.status == "failed"
    assert result.error_code == "MODEL_LOAD_FAILED"

