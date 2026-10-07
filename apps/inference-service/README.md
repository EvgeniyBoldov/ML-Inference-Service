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
the safe default when `MLFLOW_TRACKING_URI` is absent. In production, the app uses
`MlflowModelSource` and `DockerFleetRuntimeBackend` when MLflow and
`MODEL_ARTIFACT_CACHE_ROOT` are configured. It resolves immutable
`models:/<name>/<version>` URIs, caches artifacts through MLflow, resolves the
model-version tag `ml_inference.producer_image` to a locally available immutable
Docker image identity, and groups compatible models by that identity. A candidate
container loads and smoke-tests its complete group before the route snapshot is
switched. Tests use an in-process predictor runtime to exercise the endpoint
contract.

Set `INFERENCE_DATABASE_URL=postgresql+asyncpg://...` to use the durable
`SqlAlchemyDeploymentRepository`; otherwise the local in-memory repository is
used. The repository persists deployment, route, idempotency, and cached metadata
records. Production applies the Alembic migration before application startup.

## Tooling

`projects/model-runtime-base/requirements.txt` defines API control-plane
dependencies. Producer images provide runtime model dependencies, MLflow,
FastAPI, and Uvicorn and must provide UID/GID `10001`. Configure
`ML_INFERENCE_PRODUCER_IMAGE` in producer containers; see
[`docs/model-runtime-base.md`](../../docs/model-runtime-base.md). `pyproject.toml`
contains package metadata and local test tooling.

For local application tests, install the shared dependencies and test tooling:

```bash
pip install -r projects/model-runtime-base/requirements.txt 'apps/inference-service[test]'
make test
```

`make test-delivery` runs deployment regressions with simulated Docker and a local
Git remote; it requires only Bash, Git and Python 3.10+.

## Failure handling and resource limits

HTTP errors use `{ "error": { "message", "type", "param", "code" } }`, including
invalid JSON, request validation, authentication, unknown endpoints and unexpected
exceptions. `X-Request-ID` identifies each request and appears in prediction and
exception logs. Internal exception details are logged rather than returned to clients.
Invalid tokens return 401; valid tokens without the required permission return 403.
Unreadable token storage returns 503 and does not reuse cached permissions.

| Condition | HTTP status / code |
| --- | --- |
| Invalid request, input, immutable MLflow URI or idempotency header | 400 / `SCHEMA_VALIDATION_ERROR` |
| Unknown active model | 404 / `MODEL_NOT_FOUND` |
| Reused idempotency key with another request | 409 / `SCHEMA_VALIDATION_ERROR` |
| Invalid or unavailable rollback target | 409 / `DEPLOYMENT_FAILED` |
| Failed prediction or invalid model output | 502 / `MODEL_PREDICTION_FAILED`, `MODEL_OUTPUT_VALIDATION_FAILED` |
| Invalid runtime response | 502 / `RUNTIME_INVALID_RESPONSE` |
| Unavailable database, token storage or runtime | 503 / `STORAGE_UNAVAILABLE`, `AUTH_UNAVAILABLE`, `RUNTIME_UNAVAILABLE` |
| Prediction capacity or deployment queue exhausted | 503 / `SERVICE_OVERLOADED`, `DEPLOYMENT_QUEUE_FULL` |
| Prediction timeout | 504 / `MODEL_PREDICTION_TIMEOUT` |
| Unexpected application failure | 500 / `INTERNAL_ERROR` |

Deployments remain asynchronous: failures during download, loading and warmup are
reported by the deployment status endpoint. A cleanup failure cannot suppress the
original deployment failure. Failures during maintenance after promotion preserve
the ACTIVE deployment. Invalid JSON Schemas and invalid input examples are rejected
before runtime provisioning; nonfinite and nonserializable outputs are rejected.

Fleet activation persists the new model route, activation timestamp and runtime
references for every active model in one transaction. Rollback checks the requested
deployment ID and previous runtime health before changing routes, and restores all
fleet runtime references together. PostgreSQL serializes concurrent use of the same
idempotency key even when its row has not yet been inserted. Existing fingerprints
from earlier service releases remain accepted.

| Setting | Default | Purpose |
| --- | --- | --- |
| `PREDICTION_TIMEOUT_SECONDS` | 30 | Gateway prediction deadline and runtime HTTP socket timeout |
| `MAX_CONCURRENT_PREDICTIONS` | 64 | Maximum simultaneous predictions per API process |
| `MAX_PENDING_DEPLOYMENTS` | 32 | Maximum queued/running new deployments per API process; idempotent retries still work when full |
| `DOCKER_COMMAND_TIMEOUT_SECONDS` | 30 | Docker CLI deadline; timed out or cancelled child processes are killed and reaped |
| `FLEET_RUNTIME_STARTUP_TIMEOUT_SECONDS` | 180 | Runtime health polling deadline |
| `PREVIOUS_RUNTIME_TTL_SECONDS` | 3600 | Retention period after promotion or rollback |

Model resolution is bounded at 120 seconds, runtime loading at 240 seconds, and
warmup at 30 seconds per model. Readiness checks runtime health within 2 seconds.
Runtime HTTP responses are limited to 16 MiB. Database connections and pool waits
are bounded at 5 seconds, with a 15 second command timeout.

Retired runtimes wait for predictions pinned to them before removal. Expiry and
rollback are serialized, and stale expiry timers cannot shorten a renewed rollback
retention period. Shutdown awaits cancellation and candidate cleanup before database
disposal. It preserves active and retained runtimes, since a replacement API slot may
have attached to them. Recovery does not delete an existing unhealthy persisted
runtime. Synchronous model prediction runs in a thread pool so health requests can
continue; the runtime also limits simultaneous predictions (64 by default).

### Operational constraints

Run one serving API worker and direct deployment writes to that process. Fleet
snapshots, prediction reference counts, deployment admission and rollout locks are
local to a process. PostgreSQL transactions and idempotency locking do not provide
fleet coordination or snapshot refresh between multiple serving replicas.

If the database connection is lost during promotion or rollback, the transaction
outcome can be uncertain. The manager preserves candidate runtimes and durable
records, makes readiness fail, and rejects further predictions and fleet changes.
Restart the API to reconstruct the fleet from persisted state. A failed readiness
check alone does not make Docker restart a container.

Rollback snapshots and expiry timers are currently held in memory. API restart
restores the active fleet but does not restore rollback availability or retained
runtime expiry. Retained containers therefore need operational cleanup after an API
restart. Durable fleet revisions and a shared coordinator are needed before running
multiple serving replicas or preserving rollback across restart.

Prediction deadlines stop waiting at the gateway; Python cannot forcibly cancel a
synchronous model call already running in a thread. Runtime admission limits further
calls while its existing predictions execute. Apply the fleet memory/CPU settings
and provision models that are safe for concurrent prediction. The in-process backend
is intended for development.
