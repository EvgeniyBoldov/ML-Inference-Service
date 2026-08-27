"""Reference Airflow tasks for publishing and deploying one immutable model."""

from __future__ import annotations

from airflow.decorators import task
from airflow.models import DagRun

from ml_inference_contracts import DeploymentClient, log_pyfunc_model


@task
def train_and_publish() -> dict[str, str]:
    """Train and publish one registered MLflow version."""
    model = train_model()  # Replace with the team's mlflow.pyfunc.PythonModel.
    publication = log_pyfunc_model(
        registered_model_name="credit-scoring",
        description="Probability of customer default for loan underwriting.",
        owner="risk-team",
        python_model=model,
        input_example={"age": 38, "income": 150000.0, "loan_amount": 2_000_000.0},
        output_example={"probability": 0.82, "prediction": 1},
        source_git_commit=get_training_git_commit(),
        tags={"domain": "risk"},
    )
    # Return primitives so the result is safe for ordinary Airflow XCom backends.
    return {
        "registered_model_name": publication.registered_model_name,
        "model_version": publication.model_version,
        "model_uri": publication.model_uri,
    }


@task
def deploy_published_model(publication: dict[str, str], dag_run: DagRun | None = None) -> dict[str, str]:
    """Fail unless the exact just-published MLflow version becomes active."""
    client = DeploymentClient.from_environment()
    deployment = client.deploy_and_wait(
        model=publication["registered_model_name"],
        model_uri=publication["model_uri"],
        idempotency_key=(
            f"{dag_run.run_id if dag_run else 'manual'}:"
            f"{publication['registered_model_name']}:"
            f"{publication['model_version']}"
        ),
    )
    return {"deployment_id": deployment.id, "status": deployment.status}


def train_model():  # pragma: no cover - example placeholder
    raise NotImplementedError("Implement training and return an MLflow PyFunc model")


def get_training_git_commit() -> str:  # pragma: no cover - example placeholder
    raise NotImplementedError("Return the training source Git SHA")
