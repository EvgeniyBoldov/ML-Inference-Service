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
                                      └── Runtime backend (blue / green)

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
| Fleet runtime backend | Lifecycle одного изолированного контейнера для полного набора active-моделей |
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

Для новой версии менеджер проверяет контракт MLflow (registered model, version,
loadable artifact, signature, input example и description), формирует полный
набор active-моделей с кандидатом и создаёт GREEN fleet. Это один изолированный
контейнер, который загружает все модели из общего immutable base image. Он
проходит healthcheck и prediction/output-schema smoke test **для каждой** модели
fleet. Только затем сервис атомарно меняет указатель на fleet. BLUE fleet
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
    async def deploy(self, models: list[ModelMetadata]) -> RuntimeHandle: ...
    async def load(self, runtime: RuntimeHandle) -> None: ...
    async def predict(self, runtime: RuntimeHandle, model: str, payload: object) -> object: ...
    async def health(self, runtime: RuntimeHandle) -> bool: ...
    async def drain(self, runtime: RuntimeHandle) -> None: ...
    async def stop(self, runtime: RuntimeHandle) -> None: ...
```

Production backend `DockerFleetRuntimeBackend` starts one Docker container per
BLUE/GREEN fleet from the digest in `/etc/ml-inference-service/runtime-base.env`.
Artifacts are first downloaded through MLflow into a shared host cache and are
mounted read-only into fleet containers. Пакеты в production не устанавливаются:
изменение зависимостей требует выпуска нового base image. Изоляция сохраняется
между fleet и FastAPI, но не между отдельными моделями; совместимость всех
active-моделей с одним набором зависимостей — обязательное правило платформы.

## Persistence

PostgreSQL is the intended persistent store. At minimum persist deployment ID,
model name, model version, MLflow URI, slot, status, runtime ID, timestamps, and
error details. Store a route per logical model with active and previous deployment
IDs. Persist model metadata used by discovery and validation alongside a successful
deployment. On startup, restore routes and reconcile persisted runtime state with
the configured runtime backend.

The current implementation contains a PostgreSQL SQLAlchemy repository selected
by `INFERENCE_DATABASE_URL`; it persists deployment, route, idempotency, and
normalized model metadata rows. `MLFLOW_TRACKING_URI` selects the MLflow metadata
adapter. On restart the service reconstructs the active fleet from persisted
active metadata without contacting MLflow.

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
