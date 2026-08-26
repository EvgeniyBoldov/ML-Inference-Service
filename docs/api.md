# API contract

## Public API

All public endpoints use the `/v1` prefix and Bearer authentication.

| Method | Path | Required role | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/models` | `inference.read` | List models currently available for prediction |
| `GET` | `/v1/models/{model}` | `inference.read` | Get discovery metadata for one active model |
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

The deployment create request requires a logical model name and an unambiguous
MLflow source URI. It returns `202 Accepted`. Clients must provide an
`Idempotency-Key`; a retry with the same key and equivalent request returns the
same deployment record rather than creating another runtime.

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

