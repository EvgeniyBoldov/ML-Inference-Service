# Inference Service application

This directory is the implementation boundary for the FastAPI ML Inference
Service. Repository-level CI, infrastructure, and documentation deliberately live
outside this directory.

## Intended package layout

```text
app/
├── main.py
├── api/             # models, responses, deployments, health routers
├── domain/          # model, deployment, prediction entities
├── integrations/
│   └── mlflow/      # client, metadata retrieval, schema adapter
├── repositories/    # deployments and routes persistence
├── runtime/         # base, local, and future Kubernetes implementations
├── schemas/         # Pydantic HTTP DTOs
└── services/        # catalog, prediction, deployment, routing
tests/
```

The implementation must follow the repository rules in the root
[`AGENTS.md`](../../AGENTS.md) and the design in
[`docs/architecture.md`](../../docs/architecture.md).

The producer-side MLflow and Airflow contract used by this application lives in
[`packages/ml-inference-contracts`](../../packages/ml-inference-contracts).

## Current endpoint slice

The initial FastAPI implementation is available in `app/main.py`:

- OpenAI-style `GET /v1/models`, `GET /v1/models/{model}`, and `POST /v1/responses`;
- internal asynchronous deployment creation/status and rollback endpoints;
- bearer token role checks, idempotency keys, JSON Schema input/output validation,
  OpenAI-style error envelopes, and atomic in-memory active/previous routing.

Adapters are deliberately injected at `create_app()`. `UnavailableModelSource` is
the safe default when `MLFLOW_TRACKING_URI` is absent. With that variable set,
the app uses `MlflowModelSource` and `MlflowPyfuncRuntimeBackend`, which resolve
only immutable `models:/<name>/<version>` URIs and read metadata/artifacts through
MLflow. Tests use an in-process predictor runtime to exercise the endpoint
contract.

Set `INFERENCE_DATABASE_URL=postgresql+asyncpg://...` to use the durable
`SqlAlchemyDeploymentRepository`; otherwise the local in-memory repository is
used. The repository persists deployment, route, idempotency, and cached metadata
records. Its `initialize()` method is a local bootstrap convenience; production
must apply a matching Alembic migration before application startup.

## Tooling

`pyproject.toml` defines FastAPI, Pydantic, JSON Schema validation, Uvicorn, and
test dependencies. Add SQLAlchemy/Alembic, MLflow, structured logging, Prometheus,
linting, and typing tools with the corresponding implementation stages.
