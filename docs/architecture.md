# Architecture

## Purpose and boundaries

ML Inference Service is a FastAPI-based control plane and data plane for serving
registered MLflow models. Airflow is the only standard initiator of deployment.
MLflow is the source of truth for model artifacts and metadata; MinIO is accessed
only through MLflow URIs or artifact APIs.

```text
Airflow ── deployment request ──> Control plane ──> MLflow / artifact storage
                                      │
                                      ├── PostgreSQL (deployments and routes)
                                      └── Runtime groups (producer image per group)

Clients / agents ── /v1 ──> Data plane ──> active local runtime
```

The data plane does not call MLflow. A temporary MLflow outage can block new
deployments but must not interrupt predictions for already active models.

## Components

| Component | Responsibility |
| --- | --- |
| FastAPI routers | HTTP transport, authentication, OpenAI-style envelopes |
| Model catalog | Serves cached metadata for active models |
| Prediction service | Validates input, resolves the active route, invokes runtime |
| Deployment manager | Contract checks, provisioning, smoke test, warmup, switch, rollback |
| MLflow adapter | Resolves model URI, metadata, signature, example, and artifact loading |
| Schema adapter | Converts MLflow signatures to JSON Schema |
| Routing service | Atomically reads and switches active/previous deployments |
| Runtime-group backend | Lifecycle candidate/active контейнеров, сгруппированных по immutable producer image |
| Repositories | Persistent deployments, routes, idempotency records |

## Deployment lifecycle

The durable deployment states are `CREATED`, `DOWNLOADING`, `LOADING`,
`WARMING_UP`, `READY`, `ACTIVE`, `DRAINING`, `STANDBY`, `FAILED`, and `REMOVED`.

```text
CREATED → DOWNLOADING → LOADING → WARMING_UP → READY → ACTIVE
                              │                   │
                              └──── failure ───→ FAILED

former ACTIVE → DRAINING → STANDBY → REMOVED
```

Менеджер читает `ml_inference.producer_image`, разрешает его в локальный
immutable Docker identity и добавляет модель в candidate только для этой
runtime group. Контейнер загружает все модели группы из producer image,
проходит healthcheck и prediction/output-schema smoke test для каждой модели.
Только затем сервис атомарно обновляет route snapshot. Предыдущая группа
дожидается старых запросов и хранится до rollback TTL.

If any candidate step fails, it becomes `FAILED`; the existing active route is
unchanged. Rollback atomically swaps `active_deployment_id` and
`previous_deployment_id` while the previous runtime still exists. В fleet-модели
rollback намеренно доступен только для последнего fleet-перехода: это исключает
подмену маршрута версией, которой уже нет в retained runtime.

## Runtime boundary

The application depends on this interface rather than a particular isolation
technology:

```python
class RuntimeBackend(Protocol):
    async def resolve_image(self, image: str | None) -> str | None: ...
    async def deploy(self, models: list[ModelMetadata], *, image: str | None) -> RuntimeHandle: ...
    async def load(self, runtime: RuntimeHandle) -> None: ...
    async def predict(self, runtime: RuntimeHandle, model: str, payload: object) -> object: ...
    async def health(self, runtime: RuntimeHandle) -> bool: ...
    async def drain(self, runtime: RuntimeHandle) -> None: ...
    async def stop(self, runtime: RuntimeHandle) -> None: ...
```

Production backend `DockerFleetRuntimeBackend` starts containers from producer
images already present in the local Docker image store (`--pull=never`). It
mounts downloaded artifacts and the API-bundled runner read-only and overrides
the producer entrypoint to launch Uvicorn. Model dependencies come from the
producer environment; deployment never runs pip. Several models with the same
resolved image identity share one group; different identities use separate
containers.

## Persistence

PostgreSQL is the persistent store. In the supplied delivery topology it runs as
one durable Compose project shared by both blue/green service slots. At minimum persist deployment ID,
model name, model version, MLflow URI, slot, status, runtime ID, timestamps, and
error details. Store a route per logical model with active and previous deployment
IDs. Persist model metadata used by discovery and validation alongside a successful
deployment. On startup, restore routes and reconcile persisted runtime state with
the configured runtime backend.

The current implementation contains a PostgreSQL SQLAlchemy repository selected
by `INFERENCE_DATABASE_URL`; it persists deployment, route, idempotency, producer
image identity, and normalized model metadata. `MLFLOW_TRACKING_URI` selects the
MLflow metadata adapter. On restart the service reconstructs active runtime
groups from persisted active metadata without contacting MLflow. Previous
snapshot retention is currently process-local, so rollback history does not
survive API restart.

## API and security

Public endpoints are `/v1/models`, `/v1/models/{model}`, and `/v1/responses`.
They follow OpenAI-style envelopes and use `Authorization: Bearer <token>`.
Deployment endpoints are isolated under `/internal/v1` and require deployment
credentials. Minimum roles are `inference.read`, `inference.predict`,
`deployment.write`, and `deployment.read`.

The catalog contains only currently prediction-ready models. `POST /v1/responses`
validates `input` against the locally cached JSON Schema before invoking the
runtime and returns the serving model version.

## Observability and health

Every prediction emits request ID, model, model version, deployment ID, runtime
ID, status, total latency, model latency, gateway overhead, and timestamp. Raw
inputs and outputs are excluded by default. Export the metrics in the technical
specification with model/version/status labels. `/health/live` reports process
liveness; `/health/ready` reports that the service can accept inference traffic.
Если в PostgreSQL есть active-модели, readiness требует успешно восстановленный
и healthy fleet; поэтому Nginx не переключится на FastAPI-релиз без работающих
runtime-контейнеров.

## MVP delivery sequence

1. Domain models, configuration, PostgreSQL repositories, and migrations.
2. MLflow adapter and signature-to-JSON-Schema conversion.
3. Runtime abstraction plus one isolated backend.
4. Deployment manager with idempotency, smoke testing, atomic routing, and rollback.
5. Public model catalog and prediction APIs with authentication and validation.
6. Logging, metrics, health checks, integration tests, CI, and infrastructure.
