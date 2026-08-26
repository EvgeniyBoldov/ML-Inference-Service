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
| Runtime backend | Isolated lifecycle: deploy, load, predict, health, drain, stop |
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

For a new version, the manager validates the MLflow contract (registered model,
version, loadable artifact, signature, input example, and description), selects
the free blue/green slot, creates an isolated runtime, loads it, runs health and
example-based output-schema checks, and warms it up. Only then does it perform an
atomic route switch. The former active runtime drains existing requests and stays
in `STANDBY` for the configured rollback TTL.

If any candidate step fails, it becomes `FAILED`; the existing active route is
unchanged. Rollback atomically swaps `active_deployment_id` and
`previous_deployment_id` while the previous runtime still exists.

## Runtime boundary

The application depends on this interface rather than a particular isolation
technology:

```python
class RuntimeBackend(Protocol):
    async def deploy(self, deployment: Deployment) -> RuntimeHandle: ...
    async def load(self, runtime: RuntimeHandle) -> None: ...
    async def predict(self, runtime: RuntimeHandle, payload: object) -> object: ...
    async def health(self, runtime: RuntimeHandle) -> bool: ...
    async def drain(self, runtime: RuntimeHandle) -> None: ...
    async def stop(self, runtime: RuntimeHandle) -> None: ...
```

The MVP can provide a process-based backend. A container or Kubernetes backend
must be interchangeable without changing routers, routing, or domain services.

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
adapter and PyFunc runtime adapter. The included PyFunc runtime is suitable only
when service and model dependencies are compatible; process/container isolation
is still the required production backend for heterogeneous model environments.

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

## MVP delivery sequence

1. Domain models, configuration, PostgreSQL repositories, and migrations.
2. MLflow adapter and signature-to-JSON-Schema conversion.
3. Runtime abstraction plus one isolated backend.
4. Deployment manager with idempotency, smoke testing, atomic routing, and rollback.
5. Public model catalog and prediction APIs with authentication and validation.
6. Logging, metrics, health checks, integration tests, CI, and infrastructure.
