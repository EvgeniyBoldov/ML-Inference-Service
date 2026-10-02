# Observability and service access

## Prometheus

`GET /metrics` exposes Prometheus text format and requires a `deploy` token.
Prometheus should send `Authorization: Bearer <deploy-token>` to the active
loopback slot port recorded in `/etc/ml-inference-service/active-release.env`.
Do not expose `/metrics` publicly through Nginx.

Implemented metrics:

| Metric | Labels | Meaning |
| --- | --- | --- |
| `http_requests_total` | method, route, status_code | Counts every HTTP request, including validation, authentication, 404 and 500 errors |
| `http_request_duration_seconds` | method, route | HTTP latency histogram |
| `http_requests_in_progress` | — | Current HTTP requests |
| `service_start_time_seconds` | — | Process start timestamp |
| `service_uptime_seconds` | — | Process uptime |
| `prediction_requests_total` | model, version, status | Completed/failed prediction count |
| `prediction_errors_total` | model, version, code | API/prediction errors |
| `prediction_latency_seconds` | model, version, kind | `total`, `model`, and `gateway` latency |
| `deployment_total` | model, version, status | Successful or failed deployments |
| `deployment_failed_total` | model, version, code | Failure reason counts |
| `model_load_duration_seconds` | model, version | Candidate deployment duration |
| `model_runtime_status` | model, version, status | Active/standby/failed runtime state |
| `active_model_version` | model, version | One for the current active version |

Inputs and outputs are intentionally not metrics labels and are not emitted by
the service.

Routes use templates such as `/internal/v1/deployments/{deployment_id}`; unknown
paths share the `unmatched` label. Request IDs, arbitrary paths and deployment IDs
are not metric labels. Counters are local to a process and reset on API restart;
Prometheus retains their historical samples. HTTP latency covers endpoint processing
through response creation, rather than transfer of the response body to the client.

## JSON status

`GET /internal/v1/status` requires `metrics.read` (the `deploy` token role). It
returns `status` (`ready` or `degraded`), active model count, pending deployment
count, recovery flag, uptime and request statistics from the same Prometheus counters:
total, successful, client/server errors, requests in progress and per-route counts.
It includes health, metrics and status requests. The current status request is in
progress and is counted as completed after its response is created.

The endpoint returns 200 for an authenticated status response even when readiness
is degraded; load balancers should continue using `/health/ready`. Storage or runtime
failure makes status degraded without suppressing request statistics. Credentials,
request inputs and exception details are excluded.

```bash
curl --fail -H "Authorization: Bearer $DEPLOY_TOKEN" \
  http://127.0.0.1:<active-port>/internal/v1/status
curl --fail -H "Authorization: Bearer $DEPLOY_TOKEN" \
  http://127.0.0.1:<active-port>/metrics
```

Example PromQL queries:

```promql
sum(rate(http_requests_total[5m]))
sum(rate(http_requests_total{status_code=~"5.."}[5m]))
histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket[5m])))
```

## Two token groups

The production container reads `/etc/ml-inference-service/tokens` read-only.
The file contains only SHA-256 hashes, one record per line:

```text
<token-id> <role> <sha256-token-hash>
```

| Role | Permissions |
| --- | --- |
| `predict` | `GET /v1/models`, `GET /v1/models/{model}`, `POST /v1/responses` |
| `deploy` | Internal deployment create/status/rollback, `GET /internal/v1/status` and `GET /metrics` |

Create tokens on the production VM. The command prints a plaintext token only
once; distribute it through the approved secret channel.

```bash
sudo scripts/manage-inference-token.sh create predict
sudo scripts/manage-inference-token.sh create deploy
sudo scripts/manage-inference-token.sh list
sudo scripts/manage-inference-token.sh revoke deploy_20260826120000
```

The service detects token-file modifications on the next authenticated request;
no container restart is necessary after create/revoke. The service checks hashes
with constant-time comparison. Use high-entropy generated tokens only.

Add this to `/etc/ml-inference-service/runtime.env` for clarity (Compose also
sets it explicitly):

```dotenv
INFERENCE_TOKEN_FILE=/etc/ml-inference-service/tokens
```
