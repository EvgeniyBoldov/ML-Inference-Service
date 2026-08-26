# MLflow model contract

This document defines the producer contract between training code (Jupyter or
Airflow) and ML Inference Service. A deployment is accepted only when the model
has been published through this contract or an equivalent process that produces
the same MLflow data.

## Ownership boundaries

| Producer: training code | Consumer: Inference Service |
| --- | --- |
| Chooses and trains a model | Never retrains or chooses a model version |
| Logs a PyFunc-compatible model and contract metadata to MLflow | Reads exactly one supplied MLflow model URI during deployment |
| Supplies representative input and output examples | Validates metadata, loads, smoke-tests, warms, and routes the runtime |
| Starts deployment through the Airflow task after successful publication | Owns blue/green state, traffic switch, retention, and rollback |

The artifact store is an MLflow implementation detail. Producers and consumers
use MLflow URIs only; neither forms MinIO/S3 artifact paths itself.

## Required MLflow data

The registered model version is deployable only if all of the following are
present:

| MLflow object | Required value | Inference Service use |
| --- | --- | --- |
| Registered model | Stable logical name | Public `model` ID and routing key |
| Model version | Explicit immutable version | `model_version` in responses and deployment identity |
| Model artifact | MLflow PyFunc-loadable artifact | Isolated runtime loading |
| Model signature | Inputs and outputs | JSON Schema discovery and validation |
| Input example | Valid representative input | Smoke test and API documentation |
| Registered-model or model-version description | Non-empty human-readable purpose | Agent/MCP discovery description |
| `ml_inference.owner` tag | Owning team/service | Public `owned_by` field |

The publisher writes these optional tags when supplied: `ml_inference.contract_version`,
`ml_inference.source_git_commit`, `ml_inference.params_schema`, and
`ml_inference.extra_metadata`. Tags are JSON strings where applicable. They are
read-only metadata to the serving system and must not contain secrets or PII.

`input_example` must match the signature input. The supplied `output_example`
must be the result shape expected from that input and is used to infer or verify
the output signature. For tabular PyFunc models use the same input representation
that `predict` accepts (normally a pandas DataFrame); for object-shaped models use
a JSON-compatible dictionary/list if supported by the flavor.

## Publisher library

The distributable package lives in `packages/ml-inference-contracts`. It does
not require Airflow, so it can be used in Jupyter as well as a DAG task.

```python
from ml_inference_contracts import log_pyfunc_model

publication = log_pyfunc_model(
    registered_model_name="credit-scoring",
    description="Probability of customer default for loan underwriting.",
    owner="risk-team",
    python_model=model,
    input_example=input_example,
    output_example=output_example,
    source_git_commit="a1b2c3d4",
    tags={"domain": "risk"},
)

print(publication.model_uri)  # models:/credit-scoring/18
```

The function fails before publishing if required local fields are missing and
fails after logging if MLflow does not create a registered model version. It sets
the description on both registered model and version so an individual version
remains self-describing.

For a non-PyFunc flavor, the training owner must provide a runtime adapter before
deployment support is added. The current shared publisher deliberately logs only
PyFunc models because the first runtime contract is `mlflow.pyfunc.load_model`.

## Airflow deployment contract

The DAG must deploy the exact successful publication, rather than a stage alias
such as `@champion`. Pass `publication.model_uri` to `DeploymentClient` and save
the returned deployment ID to task logs/XCom. The service's `ACTIVE` status is the
only success condition.

```python
from ml_inference_contracts import DeploymentClient

client = DeploymentClient.from_environment()
deployment = client.deploy_and_wait(
    model=publication.registered_model_name,
    model_uri=publication.model_uri,
    idempotency_key=f"{dag_run_id}:credit-scoring:{publication.model_version}",
)
```

Required environment variables are `INFERENCE_SERVICE_URL` and
`INFERENCE_DEPLOYMENT_TOKEN`. The deployment client sends `Authorization: Bearer`
and `Idempotency-Key`, accepts only `202` for creation, and raises on terminal
`failed` state or polling timeout. It is intentionally an Airflow-compatible
Python callable, not an Airflow Operator; a DAG can use it in a Python task
without forcing the shared package to depend on Airflow.

## Data read by deployment

For `models:/credit-scoring/18`, the service resolves the registered model and
version in MLflow, then reads model-version/registered-model descriptions and
tags, the MLmodel artifact metadata (signature and input example), and the
PyFunc artifact via MLflow. It persists a normalized copy of this metadata before
activation. Prediction, catalog reads, and schema validation then use that local
copy and never call MLflow.

