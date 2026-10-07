# DevOps: краткая инструкция эксплуатации

## Где хранится состояние

| Место | Назначение |
| --- | --- |
| Git `release.env` | Версия API, base digest, source SHA, DB revision и слоты. |
| `projects/model-runtime-base/requirements.txt` | Зависимости API control plane. |
| Версионированные Airflow/Jupyter images | Python/native dependencies исполняемых моделей, FastAPI и Uvicorn. |
| `/etc/ml-inference-service/runtime.env` | Секреты, PostgreSQL, MLflow/S3 и лимиты. |
| `/etc/ml-inference-service/active-release.env` | Active и standby API releases. |
| `/var/lib/ml-inference-service/model-artifacts/` | Общий host cache artifacts и runtime manifests. |
| PostgreSQL | Deployments, active routes и model/runtime metadata. |

Runtime containers создаёт API через Docker socket. Они не имеют socket или
production credentials. Bootstrap и установка controller описаны в
[delivery.md](delivery.md).

## Producer images

Airflow и Jupyter должны выставлять `ML_INFERENCE_PRODUCER_IMAGE` в services,
которые публикуют модели. Значение — уникальный tag соответствующего image,
например `registry.company.local/ml-airflow:2026.10.06-17`. Image должен быть
предварительно загружен на production host. Внутри image нужны модельные
зависимости, MLflow, FastAPI, Uvicorn и UID/GID `10001`. Не переиспользуйте tag
после изменения содержимого. Подробности — в
[producer image runtime contract](model-runtime-base.md).

## Выпуск API

```bash
git add <изменённые-файлы>
git commit -m "Update inference service"
make release-preview
make release
```

`make release` собирает и публикует API image, записывает API release metadata
в `release.env` и пушит Git commit. Production pipeline выполняет миграцию,
blue/green API startup, readiness check и переключение Nginx. Общий model
runtime image не собирается и не разворачивается.

```bash
sudo /usr/local/sbin/ml-inference-deploy status
curl --fail http://127.0.0.1:<active-port>/health/ready
```

## Откат и диагностика

- `rollback_production` возвращает предыдущий API release. Миграции БД не
  откатываются.
- Failed model candidate не становится active; проверьте API logs и Docker logs
  runtime container-а по labels `ml-inference-service.managed=true`.
- `PRODUCER_IMAGE_UNAVAILABLE` означает, что tag из MLflow отсутствует в local
  Docker image store сервера.
- Проверяйте внутреннюю Docker network, права на общий artifact cache, MLflow
  download settings и лимиты памяти/CPU.
- Не очищайте artifacts и не редактируйте active-state/upstream вручную во время
  rollout.
