import pytest

from ml_inference_contracts.mlflow_logging import ModelContractError, log_pyfunc_model


def test_publisher_rejects_missing_input_example_before_importing_mlflow() -> None:
    with pytest.raises(ModelContractError, match="input_example"):
        log_pyfunc_model(
            registered_model_name="credit-scoring",
            description="Score a customer.",
            owner="risk-team",
            python_model=object(),
            input_example=None,
            output_example={"prediction": 1},
        )

