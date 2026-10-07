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
- [ML engineer guide](docs/ml-engineer-guide.md) — Jupyter logging and Airflow
  deployment of a new MLflow model version.
- [Observability and access](docs/observability-and-access.md) — Prometheus
  metrics and local two-role token administration.
- [Producer image runtime contract](docs/model-runtime-base.md) — producer image
  metadata, local image resolution, runtime grouping, and runner mounts.
- [Delivery](docs/delivery.md) — Docker release, GitLab shell-runner deployment,
  host Nginx blue/green switching, and rollback process.
- [DevOps: основной сервис](docs/devops-service-guide.md) — подготовка VM,
  выпуск FastAPI-образа, GitLab delivery и откат.
- [DevOps: producer images](docs/devops-runtime-base-guide.md) — сборка и доставка
  Airflow/Jupyter environments для model inference.
- [DevOps: эксплуатация](docs/devops-operations.md) — короткий runbook VM,
  выпусков, отката и расположения production-файлов.
- [Development guide](apps/inference-service/README.md) — code-package layout
  and intended implementation sequence.

## Status

Репозиторий содержит FastAPI service, MLflow/Airflow contracts, PostgreSQL
persistence, Docker/GitLab delivery, base runtime subproject и операционные
инструкции. Перед первым production rollout требуется выпуск pinned base image и
проверка интеграции с реальными MLflow, PostgreSQL, registry и Nginx.
