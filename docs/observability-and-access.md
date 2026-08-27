# Observability and service access

## Prometheus

`GET /metrics` exposes Prometheus text format and requires a `deploy` token.
Prometheus should send `Authorization: Bearer <deploy-token>` to the active
loopback slot port recorded in `/etc/ml-inference-service/active-release.env`.
Do not expose `/metrics` publicly through Nginx.

Implemented metrics:

| Metric | Labels | Meaning |
| --- | --- | --- |
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

## Two token groups

The production container reads `/etc/ml-inference-service/tokens` read-only.
The file contains only SHA-256 hashes, one record per line:

```text
<token-id> <role> <sha256-token-hash>
```

| Role | Permissions |
| --- | --- |
| `predict` | `GET /v1/models`, `GET /v1/models/{model}`, `POST /v1/responses` |
| `deploy` | Internal deployment create/status/rollback and `GET /metrics` |

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
