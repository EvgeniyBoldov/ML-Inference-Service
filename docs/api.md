# API contract

## Public API

The React operations dashboard is served from `/dashboard/` by the dashboard
container in the local Compose stack. It uses the same-origin endpoints below
and refreshes model and service status every five seconds.
The dashboard disables deployment actions when
`capabilities.deployments.available` in the status response is false. This means
MLflow is not configured; an available capability does not guarantee upstream
MLflow or artifact-storage connectivity.

The dashboard reads model counters from `GET /internal/v1/runtimes`, grouped by
the active runtime container. Each model includes `requests` with `total`,
`completed`, `failed`, `in_progress`, and `average_latency_ms`. The runner counts
actual model invocations and excludes health checks, API validation failures,
and deployment warmup. These counters survive an API restart and reset when the
model container restarts. Unavailable runner statistics are represented by null,
never by fabricated zeroes or API counters.

Status `predictions` and Prometheus prediction metrics remain API-side
observations for existing consumers. General HTTP counters remain in `requests`.

All public endpoints use the `/v1` prefix and Bearer authentication by default.
Deployments behind a trusted, authenticated ingress may set
`INFERENCE_TRUSTED_PROXY_AUTH=true`; in that mode the service accepts requests
without its own Bearer token, so the ingress must protect every route including
deployment and metrics endpoints.

| Method | Path | Required role | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/models` | `inference.read` | List models currently available for prediction |
| `GET` | `/v1/models/{model}` | `inference.read` | Get discovery metadata for one active model |
| `GET` | `/v1/models/{model}/health` | `inference.read` | Check the runtime health for one active model |
| `POST` | `/v1/responses` | `inference.predict` | Run prediction against the current active version |
| `GET` | `/health/live` | — | Process liveness |
| `GET` | `/health/ready` | — | Readiness for inference traffic |

`GET /v1/models` returns `{ "object": "list", "data": [...] }`. A model object
includes `id`, `object`, `created`, `owned_by`, `version`, `description`,
`input_schema`, `output_schema`, and `input_example`.

`POST /v1/responses` accepts a logical model ID and model input:

```json
{"model":"credit-scoring","input":{"age":38,"income":150000,"loan_amount":2000000}}
```

The response identifies the actual model version that served the request:

```json
{"id":"resp_...","object":"response","created_at":1787750100,"status":"completed","model":"credit-scoring","model_version":"18","output":[{"type":"prediction","content":{}}]}
```

## Internal deployment API

| Method | Path | Required role | Purpose |
| --- | --- | --- | --- |
| `POST` | `/internal/v1/deployments` | `deployment.write` | Begin an asynchronous MLflow deployment |
| `GET` | `/internal/v1/deployments/{deployment_id}` | `deployment.read` | Poll deployment result |
| `POST` | `/internal/v1/deployments/{deployment_id}/rollback` | `deployment.write` | Restore the previous successful runtime |
| `GET` | `/internal/v1/status` | `metrics.read` | Service readiness and request statistics in JSON |
| `GET` | `/internal/v1/runtimes` | `metrics.read` | Active runtime containers, grouped models, health and model-process counters |
| `GET` | `/internal/v1/runtimes/{runtime_id}/health` | `metrics.read` | Explicit health check of one active runtime |
| `GET` | `/metrics` | `metrics.read` | Request, prediction and deployment metrics in Prometheus format |

The deployment create request requires a logical model name and an unambiguous
MLflow source URI. It returns `202 Accepted`. Clients must provide an
`Idempotency-Key`; a retry with the same key and equivalent request returns the
same deployment record rather than creating another runtime.

Runtime inventory includes `container_name`, image references, `checked_at`,
`started_at`, `uptime_seconds`, and `statistics_status`. A healthy container can
have unavailable statistics if it uses an older runner. Redeploy that runtime's
models to load the new runner; its health and model identities remain visible.

## Errors

All client-facing errors use this envelope:

```json
{"error":{"message":"Field 'age' is required","type":"invalid_request_error","param":"input.age","code":"schema_validation_error"}}
```

Important codes: `MODEL_NOT_FOUND`, `MODEL_NOT_READY`, `MODEL_NOT_ACTIVE`,
`SCHEMA_VALIDATION_ERROR`, `MODEL_SIGNATURE_REQUIRED`,
`MODEL_DESCRIPTION_REQUIRED`, `INPUT_EXAMPLE_REQUIRED`, `MODEL_LOAD_FAILED`,
`MODEL_HEALTHCHECK_FAILED`, `MODEL_WARMUP_FAILED`,
`MODEL_OUTPUT_VALIDATION_FAILED`, `DEPLOYMENT_NOT_FOUND`, `DEPLOYMENT_FAILED`,
`RUNTIME_UNAVAILABLE`, `MLFLOW_UNAVAILABLE`, and `INTERNAL_ERROR`.
