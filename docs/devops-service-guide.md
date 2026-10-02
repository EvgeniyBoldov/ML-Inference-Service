# Инструкция DevOps: основной Inference Service

Документ описывает выпуск и доставку контейнера FastAPI. Зависимости API и моделей устанавливаются в общий base по requirements, см.
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

Нужны Docker Engine и Compose plugin, GitLab shell runner, Nginx и сетевой доступ
к внутреннему MLflow, MinIO через MLflow и registry. PostgreSQL описан в общем
Compose-файле; deployment job запускает его один раз под постоянным именем
проекта, отдельно от blue/green service slots. Runner **не должен** иметь
доступ к Docker socket или прямой `sudo` к `install`, `nginx` и `systemctl`.
Ему разрешён только passwordless вызов root-owned
`/usr/local/sbin/ml-inference-deploy deploy|rollback|status`.

Создайте host-owned конфигурацию:

```bash
sudo install -d -m 0750 /etc/ml-inference-service
sudo install -d -m 0750 /var/lib/ml-inference-service/model-artifacts
sudo install -m 0640 /dev/null /etc/ml-inference-service/runtime.env
sudo docker network create ml-inference-runtime || true
```

Пример обязательных значений `/etc/ml-inference-service/runtime.env`:

```dotenv
INFERENCE_DATABASE_URL=postgresql+asyncpg://ml_inference:PASSWORD@postgres:5432/ml_inference
POSTGRES_DB=ml_inference
POSTGRES_USER=ml_inference
POSTGRES_PASSWORD=PASSWORD
MLFLOW_TRACKING_URI=http://mlflow.internal
# Для прямого скачивания артефактов из MinIO (s3://):
MLFLOW_S3_ENDPOINT_URL=https://minio.internal:9000
AWS_ACCESS_KEY_ID=MINIO_SERVICE_ACCESS_KEY
AWS_SECRET_ACCESS_KEY=MINIO_SERVICE_SECRET_KEY
AWS_DEFAULT_REGION=us-east-1
MODEL_ARTIFACT_CACHE_ROOT=/var/lib/ml-inference-service/model-artifacts
MODEL_FLEET_MEMORY_LIMIT=24g
MODEL_FLEET_CPU_LIMIT=8
PREVIOUS_RUNTIME_TTL_SECONDS=3600
FLEET_RUNTIME_STARTUP_TIMEOUT_SECONDS=180
```

`PASSWORD` в URL должен соответствовать `POSTGRES_PASSWORD`; URL не должен
содержать неэкранированные символы `@`, `:`, `/` или `#`.

Compose передаёт весь защищённый `runtime.env` в API-контейнер через `env_file`.
MLflow и boto3 читают указанные AWS-переменные для авторизации в MinIO. Используйте
отдельный MinIO service account с правами чтения нужного bucket; замените
`MINIO_SERVICE_ACCESS_KEY` и `MINIO_SERVICE_SECRET_KEY` его значениями. Секреты
не добавляются в Git, release manifest, логи или ответы status-эндпоинта.
При заданном `MLFLOW_S3_ENDPOINT_URL` сервис проверяет наличие пары ключей или
явно настроенного AWS profile/credentials file. Неполная пара ключей останавливает
запуск. Проверка конфигурации не подтверждает сетевой доступ или права на bucket:
ошибки доступа обнаруживаются при скачивании артефактов.

Если MLflow проксирует артефакты через `mlflow-artifacts:/`, MinIO credentials
задаются на MLflow server; в API не задавайте `MLFLOW_S3_ENDPOINT_URL` и MinIO keys.
Runtime моделей получает уже скачанные артефакты через read-only mount и не
получает MinIO credentials. Для внутреннего CA настройте `AWS_CA_BUNDLE` и
read-only mount сертификата в API.

Права на Docker socket эквивалентны root; поэтому
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
```

`make release` требует чистый закоммиченный tree, автоматически повышает patch,
проверяет хеш Dockerfile/requirements, при необходимости собирает новый base,
собирает и пушит API/runtime, записывает версии и digest в `release.env`,
коммитит и пушит manifest в upstream ветки. Для major/minor заранее задайте
и закоммитьте в `release.env` версию `X.Y.0`; следующая команда выпустит `X.Y.1`.

Не меняйте `RELEASE_COMMIT` вручную. Не добавляйте в `release.env` пароли,
токены или адреса внутренних секретных хранилищ.

## Что выполняет GitLab pipeline

Pipeline на production shell runner сначала поднимает и ожидает PostgreSQL из
общего Compose-файла под постоянным проектом `ml-inference-postgres`, затем копирует compose и `release.env` в
`/opt/ml-inference-service/releases/<версия>`, запускает Alembic на candidate
image, поднимает неактивный BLUE/GREEN slot, проверяет
`/health/ready`, затем атомарно меняет Nginx upstream. После переключения старый API останавливается; его bundle сохраняется для rollback.
Runtime manifest фиксируется отдельно для каждой версии в её release directory.

`DEPLOY_WAIT_TIMEOUT` controller по умолчанию равен 300 секундам и должен быть
больше `FLEET_RUNTIME_STARTUP_TIMEOUT_SECONDS`, чтобы cold start fleet не был
ложно признан неуспешным.

`/health/ready` не станет успешным, если в PostgreSQL есть active-модели, но
новый сервис не восстановил healthy fleet. Поэтому переключение Nginx не может
направить трафик на сервис без работающих моделей.

Проверка job после запуска:

```bash
sudo cat /etc/ml-inference-service/active-release.env
sudo /usr/local/sbin/ml-inference-deploy status
curl --fail http://127.0.0.1:<active-port>/health/ready
```

## Откат основного сервиса

Штатный откат — ручная job `rollback_production`. Она повторно проверяет
сохранённый standby release: запускает его, проверяет readiness и атомарно переключает на него Nginx. Это доступно,
пока standby не был вытеснен следующим deployment. Job не откатывает миграции
БД. Не редактируйте вручную
`/etc/ml-inference-service/active-release.env` и upstream-файл во время job.

## Диагностика

| Симптом | Действие |
| --- | --- |
| Candidate не проходит `/health/ready` | Посмотреть `docker compose logs inference-service`; Nginx останется на старом slot. |
| Ошибка Alembic | Исправить миграцию/DB доступ; candidate не поднимется. |
| Нет доступа к registry | Проверить сетевую доступность VM и наличие image digest в registry. |
| Runtime не стартует | Проверить runtime manifest текущего release bundle, Docker socket, сеть `ml-inference-runtime`, cache-directory и лимиты памяти. |
| Prediction работает, deployment нет | Проверить MLflow/artifact storage; active fleet не зависит от MLflow. |

Для изменения Python-зависимостей моделей добавьте пакеты в общий requirements
и выполните полный `make release`: base пересоберётся автоматически.
