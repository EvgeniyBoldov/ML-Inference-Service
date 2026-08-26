# AGENTS.md

## Scope

This repository hosts the ML Inference Service and its delivery assets. Application
code belongs in `apps/inference-service/`; repository-level CI, infrastructure,
and documentation belong at the root.

## Working agreement

- Read the relevant documents in `docs/` before changing architecture or API
  contracts. `docs/architecture.md` is the source of truth for the MVP design.
- Keep the public API OpenAI-style under `/v1`; keep deployment endpoints under
  `/internal/v1`. Do not expose runtime identifiers in public responses.
- Treat MLflow as the source of truth for model artifacts and model metadata.
  Do not construct MinIO paths in application code.
- Prediction requests must use only locally cached deployment metadata and the
  active runtime. No MLflow call is permitted on the prediction hot path.
- Preserve blue/green semantics: do not replace or stop an active runtime before
  a candidate has loaded, passed smoke tests, warmed up, and routing has switched
  atomically.
- Runtime state and routing state must be persisted; in-memory state alone is not
  sufficient for a production feature.
- Validate request inputs against the deployed JSON Schema before calling a model.
  Errors use the OpenAI-style `{ "error": { ... } }` envelope.
- Never log raw inference inputs or prediction outputs unless an explicitly
  approved, data-safe configuration enables it.

## Code layout

- `apps/inference-service/app/` — FastAPI application and domain code.
- `apps/inference-service/tests/` — tests matching the application layout.
- `docs/` — product, API, operational, and architecture documentation.
- `infra/` — deployment manifests and local infrastructure configuration.
- `.github/` — CI workflows and repository automation.

## Python conventions

- Target Python 3.12 unless a project-level dependency constraint says otherwise.
- Use type hints for production code and Pydantic models at API boundaries.
- Keep API routers thin; place orchestration in services and persistence in
  repositories.
- Depend on the `RuntimeBackend` abstraction, never a concrete runtime backend
  from routing or API code.
- Add or update tests with behavioral changes. Cover successful and failed
  deployment paths, rollback, schema validation, and active-route behavior.

## Verification

From `apps/inference-service/`, run the checks declared in its `README.md` or
`pyproject.toml`. Before handing off a substantial change, at minimum run the
relevant unit tests and a formatting/lint check when those tools are configured.

