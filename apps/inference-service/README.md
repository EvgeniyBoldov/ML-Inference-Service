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
the safe default when `MLFLOW_TRACKING_URI` is absent. In production, when
`MODEL_RUNTIME_BASE_FILE` and `MODEL_ARTIFACT_CACHE_ROOT` are also configured,
the app uses `MlflowModelSource` and `DockerFleetRuntimeBackend`. It resolves
only immutable `models:/<name>/<version>` URIs, caches artifacts through MLflow,
and starts a GREEN container containing the complete model fleet. Every model is
loaded and smoke-tested before the active fleet pointer is switched. Tests use an
in-process predictor runtime to exercise the endpoint contract.

Set `INFERENCE_DATABASE_URL=postgresql+asyncpg://...` to use the durable
`SqlAlchemyDeploymentRepository`; otherwise the local in-memory repository is
used. The repository persists deployment, route, idempotency, and cached metadata
records. Production applies the Alembic migration before application startup.

## Tooling

`pyproject.toml` defines FastAPI, Pydantic, JSON Schema validation, Uvicorn, and
test dependencies. Add SQLAlchemy/Alembic, MLflow, structured logging, Prometheus,
linting, and typing tools with the corresponding implementation stages.
