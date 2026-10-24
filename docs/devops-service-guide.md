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

## Локальный стек в Docker Compose

Локальный Compose поднимает PostgreSQL, API и React-панель. API выполняет
миграции перед запуском, а панель доступна только на loopback-порту `8081`:

```bash
docker compose -f infra/compose/local-inference.compose.yaml up --build -d
```

Откройте `http://localhost:8081/dashboard/`. Локальный пароль PostgreSQL и
режим trusted proxy из этого Compose предназначены только для разработки;
панель привязана к `127.0.0.1`, а API не публикует host-порт. Для подключения
MLflow задайте одновременно `MLFLOW_TRACKING_URI` и
`MODEL_ARTIFACT_CACHE_ROOT` в окружении Compose. Деплой модели также требует,
чтобы её producer image был загружен в локальный Docker Engine.

Остановить стек можно командой:

```bash
docker compose -f infra/compose/local-inference.compose.yaml down
```

## Однократная подготовка production VM

Нужны Docker Engine и Compose plugin, GitLab shell runner, Nginx и сетевой доступ
к внутреннему MLflow, MinIO через MLflow и registry. Для внешнего доступа нужен
DNS hostname этой VM и TLS-сертификат с полной цепочкой. PostgreSQL описан в общем
Compose-файле; deployment job запускает его один раз под постоянным именем
проекта, отдельно от blue/green service slots. Runner **не должен** иметь
доступ к Docker socket или прямой `sudo` к `install`, `nginx` и `systemctl`.
Ему разрешён только passwordless вызов root-owned
`/usr/local/sbin/ml-inference-deploy deploy|rollback|status`.

Создайте host-owned конфигурацию:

```bash
sudo install -d -m 0750 /etc/ml-inference-service
sudo install -d -m 0750 /etc/ml-inference-service/tls
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
# Only when every route is protected by the trusted ingress authentication.
INFERENCE_TRUSTED_PROXY_AUTH=true
```

`PASSWORD` в URL должен соответствовать `POSTGRES_PASSWORD`; URL не должен
содержать неэкранированные символы `@`, `:`, `/` или `#`.

### Входной HTTPS и панель

React-панель собирается вместе с API image и доступна через тот же active
upstream по `/dashboard/`; отдельные frontend-порт, image и переменные для UI в
production не нужны. `make release` собирает UI внутри Docker build и публикует
готовый образ, поэтому production VM не устанавливает Node/npm и не собирает
frontend.

DNS-запись домена сервиса должна вести на production VM, а входящие TCP-порты
`80` и `443` должны быть доступны Nginx. Положите сертификат и закрытый ключ
сюда (или настройте автоматизацию сертификатов создавать эти пути):

```text
/etc/ml-inference-service/tls/fullchain.pem
/etc/ml-inference-service/tls/privkey.pem
```

Закрытый ключ должен читаться master-процессом Nginx (обычно `root:root`, mode
`0600`). В `infra/nginx/ml-inference-service.conf` укажите реальный домен в обоих
`server_name` вместо `_`. Конфигурация перенаправляет HTTP на HTTPS, завершает
TLS на Nginx и проксирует панель и API в активный blue/green slot. Установите её
один раз на VM:

```bash
sudo install -m 0644 infra/nginx/ml-inference-service.conf /etc/nginx/conf.d/ml-inference-service.conf
sudo install -m 0644 infra/nginx/ml-inference-service-upstream.conf.example /etc/nginx/conf.d/ml-inference-service-upstream.conf
sudo nginx -t
sudo systemctl reload nginx
```

Проверьте, что порт в начальном upstream совпадает с `BLUE_PORT` или
`GREEN_PORT` из `release.env`. До первого успешного deployment домен не
направляйте на эту VM; deployment controller сам переключает upstream на
проверенную версию.

При обновлении сертификата выполняйте `nginx -t` и reload Nginx после замены
файлов. TLS сам по себе не заменяет общую авторизацию: если используется
`INFERENCE_TRUSTED_PROXY_AUTH=true`, перед публичным трафиком должен стоять
корпоративный SSO/auth proxy, который защищает `/`, `/dashboard/`, `/v1/`,
`/internal/v1/` и остальные пути. Ограничьте прямой доступ к VM и loopback-портам
blue/green только доверенным источникам. Не включайте trusted proxy mode, если
этот auth gate отсутствует. Этот Nginx завершает TLS, но сам не выполняет вход
через SSO.

Для Nginx дополнительных environment variables нет: hostname задаётся в
`server_name`, сертификатные пути — в `ssl_certificate` и
`ssl_certificate_key`. Переменные API/БД/MLflow остаются в
`/etc/ml-inference-service/runtime.env`, приведённом выше; секреты и пути TLS
не записывайте в `release.env` или Git.

`/etc/ml-inference-service/runtime.env` — единственный env-файл runtime. В нём
обязательны `INFERENCE_DATABASE_URL`, `POSTGRES_DB`, `POSTGRES_USER` и
`POSTGRES_PASSWORD`; для включения model deployment задайте вместе
`MLFLOW_TRACKING_URI` и `MODEL_ARTIFACT_CACHE_ROOT`. `MLFLOW_S3_ENDPOINT_URL`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` и `AWS_DEFAULT_REGION` нужны только
при прямом доступе API к S3/MinIO; лимиты `MODEL_FLEET_MEMORY_LIMIT`,
`MODEL_FLEET_CPU_LIMIT`, `PREVIOUS_RUNTIME_TTL_SECONDS` и
`FLEET_RUNTIME_STARTUP_TIMEOUT_SECONDS` необязательны. `INFERENCE_TRUSTED_PROXY_AUTH`
включайте только за общей авторизацией, как описано выше.

API получает защищённый `runtime.env` через `env_file`; контейнер PostgreSQL
получает только `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD`. `make release`
и `release.env` не требуют новых переменных для панели или HTTPS. MLflow и boto3
читают AWS-переменные для авторизации в MinIO. Используйте
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

После установки сертификата и Nginx-конфигурации выполните `sudo nginx -t` и
`sudo systemctl reload nginx`. Deployment controller атомарно обновляет upstream
только после readiness-check candidate service.

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
проверяет хеш Dockerfile/requirements, при необходимости собирает новый API base,
собирает UI и API в одном production image, публикует image, записывает версию и digest в `release.env`,
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
| Runtime не стартует | Проверить наличие producer image из MLflow tag на локальном Docker Engine, логи runtime container-а, сеть `ml-inference-runtime`, cache-directory и лимиты памяти. |
| Prediction работает, deployment нет | Проверить MLflow/artifact storage; active fleet не зависит от MLflow. |

Для изменения Python-зависимостей моделей выпустите новую версионированную
Airflow/Jupyter image и доставьте её на production host. Затем обновите
`ML_INFERENCE_PRODUCER_IMAGE` и повторно опубликуйте/deploy модель. API release
для этого не нужен.
