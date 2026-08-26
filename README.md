# ML Inference Service

OpenAI-style centralized ML model serving with deployments initiated by Airflow,
MLflow-backed model metadata, isolated runtimes, and blue/green traffic switching.

## Repository layout

```text
.
├── apps/
│   └── inference-service/   # FastAPI service source, tests, and Python tooling
├── packages/
│   └── ml-inference-contracts/ # shared MLflow publication and Airflow deployment client
├── docs/                    # architecture and delivery documentation
├── infra/                   # runtime/deployment infrastructure (to be added)
├── .github/                 # CI workflows (to be added)
└── AGENTS.md                # repository working rules for agents
```

## Documentation

- [Architecture](docs/architecture.md) — MVP boundaries, components, lifecycle,
  persistence, and operational properties.
- [API contract](docs/api.md) — planned public and internal HTTP interfaces.
- [MLflow model contract](docs/mlflow-model-contract.md) — required model
  metadata, publisher library, and Airflow deployment handshake.
- [Delivery](docs/delivery.md) — Docker release, GitLab shell-runner deployment,
  host Nginx blue/green switching, and rollback process.
- [Development guide](apps/inference-service/README.md) — code-package layout
  and intended implementation sequence.

## Status

This repository currently contains the agreed project skeleton and architecture.
The application implementation, infrastructure manifests, CI workflows, and
database migrations are intentionally not yet created.
