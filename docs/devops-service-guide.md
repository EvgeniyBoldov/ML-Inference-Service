# Инструкция DevOps: основной Inference Service

Документ описывает выпуск и доставку контейнера FastAPI. Он не описывает
зависимости моделей: для них используется отдельный base runtime image, см.
[`devops-runtime-base-guide.md`](devops-runtime-base-guide.md).

## Границы ответственности

DevOps собирает образ только на рабочей станции с доступом к интернету и registry.
Production VM ничего не собирает и не устанавливает из PyPI: shell runner получает
состояние из Git, скачивает уже собранные образы из внутреннего registry и
переключает Nginx между двумя localhost-портами.

`release.env` — versioned, не содержит секретов и является единственной точкой
решения, какой образ основного сервиса разворачивать. Секреты и окружение живут
только в `/etc/ml-inference-service/runtime.env`.

## Однократная подготовка production VM

Нужны Docker Engine и Compose plugin, GitLab shell runner, Nginx, PostgreSQL и
сетевой доступ к внутреннему MLflow, MinIO через MLflow и registry. Runner должен
иметь Docker access и ограниченный passwordless `sudo` для `install`, `nginx -t`
и `systemctl reload nginx`.

Создайте host-owned конфигурацию:

```bash
sudo install -d -m 0750 /etc/ml-inference-service
sudo install -d -m 0750 /var/lib/ml-inference-service/model-artifacts
sudo install -m 0640 /dev/null /etc/ml-inference-service/runtime.env
sudo docker network create ml-inference-runtime || true
```

Пример обязательных значений `/etc/ml-inference-service/runtime.env`:

```dotenv
INFERENCE_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@POSTGRES_HOST:5432/ml_inference
MLFLOW_TRACKING_URI=http://mlflow.internal
MODEL_ARTIFACT_CACHE_ROOT=/var/lib/ml-inference-service/model-artifacts
MODEL_RUNTIME_BASE_FILE=/etc/ml-inference-service/runtime-base.env
MODEL_FLEET_MEMORY_LIMIT=24g
MODEL_FLEET_CPU_LIMIT=8
PREVIOUS_RUNTIME_TTL_SECONDS=3600
```

Значения MLflow/MinIO credentials добавляются тем же защищённым файлом по правилам
конкретной установки MLflow. Права на Docker socket эквивалентны root; поэтому
сервисный контейнер намеренно получает этот доступ только на выделенной VM.

Создайте токены, не добавляя их в Git:

```bash
sudo scripts/manage-inference-token.sh create predict
sudo scripts/manage-inference-token.sh create deploy
```

Скопируйте Nginx-конфигурацию из `infra/nginx/`, выполните `sudo nginx -t` и
`sudo systemctl reload nginx`. Первичный upstream должен указывать на один из
локальных портов из `release.env` только после первого успешного deployment.

## Обычный выпуск приложения

На ноутбуке DevOps с интернетом и доступом к registry:

```bash
git switch main
git pull --ff-only
make test
make release-preview
make release
git diff -- release.env
git add release.env
git commit -m "release: <версия>"
git push origin main
```

`make release` требует чистый закоммиченный tree, автоматически повышает patch,
собирает и пушит образ с тегами версии и SHA, и записывает точный commit в
`release.env`. Для major/minor заранее вручную задайте в `release.env` версию
`X.Y.0`; следующая команда выпустит `X.Y.1`.

Не меняйте `RELEASE_COMMIT` вручную. Не добавляйте в `release.env` пароли,
токены или адреса внутренних секретных хранилищ.

## Что выполняет GitLab pipeline

Pipeline на production shell runner копирует compose и `release.env` в
`/opt/ml-inference-service/releases/<версия>`, запускает Alembic на candidate
image, поднимает неактивный BLUE/GREEN slot, проверяет
`/health/ready`, затем атомарно меняет Nginx upstream. Старый slot остаётся
standby до следующего выпуска.

`/health/ready` не станет успешным, если в PostgreSQL есть active-модели, но
новый сервис не восстановил healthy fleet. Поэтому переключение Nginx не может
направить трафик на сервис без работающих моделей.

Проверка job после запуска:

```bash
sudo cat /etc/ml-inference-service/active-release.env
docker compose --project-directory /opt/ml-inference-service/releases/<версия> ps
curl --fail http://127.0.0.1:<active-port>/health/ready
```

## Откат основного сервиса

Откат — повторный запуск deploy job GitLab для коммита, содержащего нужный
`release.env`. Job поднимет старый immutable image в неактивном slot, проверит
его и переключит Nginx. Не редактируйте вручную
`/etc/ml-inference-service/active-release.env` и upstream-файл во время job.

## Диагностика

| Симптом | Действие |
| --- | --- |
| Candidate не проходит `/health/ready` | Посмотреть `docker compose logs inference-service`; Nginx останется на старом slot. |
| Ошибка Alembic | Исправить миграцию/DB доступ; candidate не поднимется. |
| Нет доступа к registry | Проверить сетевую доступность VM и наличие image digest в registry. |
| Runtime не стартует | Проверить `runtime-base.env`, Docker socket, сеть `ml-inference-runtime`, cache-directory и лимиты памяти. |
| Prediction работает, deployment нет | Проверить MLflow/artifact storage; active fleet не зависит от MLflow. |

Для изменения Python-зависимостей моделей не пересобирайте основной сервис:
используйте процедуру base image.
