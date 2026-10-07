# Контекст проекта ML Inference Service

Этот документ описывает фактический контракт и путь исполнения сервиса. Он
подходит как вводный контекст для инженера или новой сессии ChatGPT.

## Назначение и архитектура

ML Inference Service публикует модели, зарегистрированные в MLflow, и выдаёт
предсказания через HTTP API. Сервис не обучает модели и не выбирает версии.
Airflow передаёт точный immutable URI модели, например
`models:/credit-scoring/18`, и сервис возвращает асинхронный deployment ID.

Сервис одновременно control plane, router и HTTP proxy. Control plane получает
registry metadata и artifacts, запускает и проверяет runtime candidate, затем
атомарно меняет маршруты. Prediction path выбирает active deployment, проверяет
input по JSON Schema, вызывает нужную runtime group и проверяет output. MLflow
нужен для deployment, но не для inference уже активных моделей.

```text
Airflow/Jupyter -- deployment request --> FastAPI control plane
                                             |       |
                                      MLflow       PostgreSQL
                                      artifacts     routes/state
                                             |
                                      local Docker Engine
                                  +----------+----------+
                                  |                     |
                           runtime group A       runtime group B
                           producer image A      producer image B
                                  ^                     ^
                                  +------ prediction ---+
                                      API router/proxy
```

Основные компоненты:

- [main.py](../apps/inference-service/app/main.py) собирает FastAPI, adapters,
  auth, metrics и lifecycle recovery.
- [services.py](../apps/inference-service/app/services.py) управляет
  asynchronous deployment, validation, routes, rollback и prediction pinning.
- [mlflow_source.py](../apps/inference-service/app/mlflow_source.py) извлекает
  версию модели, signature, input example, tags и скачивает artifacts в host cache.
- [postgres_repository.py](../apps/inference-service/app/postgres_repository.py)
  хранит deployments, routes, idempotency и metadata.
- [runtime.py](../apps/inference-service/app/runtime.py) управляет runtime
  containers через Docker CLI и host Docker socket.
- [runtime_runner.py](../apps/inference-service/app/runtime_runner.py) — runner,
  который API монтирует read-only в producer container. Runner загружает модели
  через `mlflow.pyfunc.load_model` и обслуживает `/health` и `/predict`.
- [mlflow_logging.py](../packages/ml-inference-contracts/src/ml_inference_contracts/mlflow_logging.py)
  — producer-side package, который записывает договорённые tags в MLflow.

## Producer image contract

Airflow и Jupyter задают environment variable:

```text
ML_INFERENCE_PRODUCER_IMAGE=registry.company.local/ml-airflow:2026.10.06-17
```

Значение должно быть точным, уникальным и никогда не переиспользуемым tag-ом
образа, доступного локальному Docker Engine на production host. Пакет логирования
читает переменную и пишет MLflow model-version tag
`ml_inference.producer_image`. Отсутствующая или пустая переменная блокирует
публикацию модели.

На deployment сервис выполняет локальный `docker image inspect`. Если есть
RepoDigest, он становится immutable runtime identity; иначе берётся Docker image
ID. Он служит ключом группировки. Теги используются в metadata как указатель на
образ, но не являются ключом совместимости после resolution. На production
используется `--pull=never`: образ должен быть предварительно доставлен на host.

Все producer images должны содержать Python, model dependencies, MLflow,
FastAPI, Uvicorn и пользователя/group ID `10001`. Runtime принудительно стартует
с UID/GID `10001:10001`. Producer images и модели обслуживаются по правилам:

```text
один immutable image identity -> одна runtime group
несколько моделей одной среды -> один container
разные image identity -> разные containers
```

Runner-код и model artifacts монтируются в container только для чтения.
Container присоединён к внутренней Docker network `ml-inference-runtime`, не
имеет published host port, Docker socket, DB credentials, MLflow credentials
или object-store credentials. Docker socket есть только у API control plane и
даёт ему полномочия уровня root на Docker host.

## Выпуск API и deployment модели

Это два разных процесса. `make release` собирает API image, проверяет migration
head, публикует образ и коммитит release manifest. Production controller
выполняет blue/green замену API, запускает миграцию и переключает Nginx после
readiness. В release manifest больше нет общего model runtime image. API
control-plane зависимости по-прежнему берутся из
`projects/model-runtime-base/requirements.txt`; модельный Python runtime берётся
из producer image.

Модельный deployment начинается с `POST /internal/v1/deployments`, содержащего
model name и immutable MLflow URI. Обязателен `Idempotency-Key`. Ответ —
`202 Accepted`; вызывающая Airflow задача опрашивает
`GET /internal/v1/deployments/{id}` до завершения. Повтор с тем же ключом и тем
же запросом возвращает ту же запись.

Deployment выполняет следующие этапы:

1. Проверяет URI, создаёт durable deployment record и скачивает MLflow metadata
   и artifacts.
2. Проверяет name/version, описание, owner, input/output signature и input
   example; signature переводится в JSON Schema.
3. Читает `ml_inference.producer_image`, разрешает локальный image identity и
   находит active модели с тем же identity. Модель, которую заменяют, исключается
   из нового состава группы.
4. Формирует новый immutable candidate container для полного состава этой
   runtime group. Он не загружает модели других environments.
5. Runner загружает каждую модель группы. API ждёт health, запускает warmup на
   примерах всех моделей группы, проверяет output schemas и JSON-сериализацию.
6. В PostgreSQL транзакционно активируется deployment; группа получает новый
   runtime ID, а API меняет routing snapshot. Новые predictions идут в новый
   runtime, уже начатые запросы могут закончиться на прежнем runtime.
7. Runtime, на который больше не ссылается active route, сохраняется в течение
   rollback TTL и удаляется после drain/in-flight завершения.

Если модель пришла из нового image, создаётся новая группа, существующие группы
не перестраиваются. Если та же модель мигрирует на другой image, старая группа
продолжает обслуживать другие модели, которые на неё ссылаются.

Ошибки до активации отмечают deployment как failed и очищают candidate; active
маршруты остаются неизменными. Если исход транзакции неизвестен из-за потери
соединения с PostgreSQL, менеджер переводит процесс в recovery-required и не
пытается угадывать active state.

## Prediction и API

Публичный API включает `GET /v1/models`, `GET /v1/models/{model}` и
`POST /v1/responses`. Последний принимает model и JSON input. Сервис находит
active route и runtime group, валидирует input, проксирует вызов runner-а,
валидирует output и возвращает версию модели вместе с результатом. На запрос
выделяется request ID. Есть ограничение конкурентности и timeout. HTTP ошибки
возвращаются в нормализованном envelope.

Внутренний API включает создание/чтение deployment, rollback и operational
status. Bearer auth использует роли и scopes; token file хранит hashes. Health
endpoints `/health/live` и `/health/ready` разделяют процессную живость и
готовность восстановленных runtime groups.

## Источники истины и восстановление

- PostgreSQL — источник истины для active routes, deployment metadata,
  runtime ID/image identity и idempotency.
- `/var/lib/ml-inference-service/model-artifacts` — постоянный host cache,
  общий для API и runtime containers. API пишет artifacts/manifest; runtime
  видит их read-only.
- MLflow — registry и источник metadata/artifacts для нового deployment. Он не
  входит в prediction path.
- `release.env` — версия и provenance API release, без модели runtime image.
- `/etc/ml-inference-service/runtime.env` — production secrets и настройки.

При рестарте API `restore()` читает active records PostgreSQL, группирует их по
сохранённым runtime ID/image identity, подключается к существующим контейнерам
либо создаёт отсутствующие и проверяет health/warmup. Эта операция использует
сохранённые metadata и кэш, не требует MLflow.

Текущее ограничение: active routing восстанавливается из PostgreSQL, но
предыдущий полный routing snapshot и его rollback TTL хранятся в памяти API.
Поэтому rollback transition не переживает перезапуск API. Для долговременного
rollback across restart или нескольких API replicas потребуется persistent
runtime revision/snapshot history и межпроцессная координация. Сейчас следует
эксплуатировать один serving API worker.

## Диагностика и ограничения

Структурированные логи отражают deployment/runtime IDs и этапы manifest,
container startup, healthcheck и per-model load. Container exit `139` означает
native crash/SIGSEGV; Python traceback может отсутствовать. MLflow warnings о
различиях версий показывают потенциальную несовместимость, но сами по себе не
доказывают точную причину. Новый deployment следует проверять по runtime logs,
Docker events и host kernel logs.

В production нет скачивания producer images из deployment path и нет `pip
install`. Если image отсутствует локально, deployment возвращает
`PRODUCER_IMAGE_UNAVAILABLE`. Обновлённая среда — это новый versioned producer
image, заранее доставленный на хост.

См. также [delivery](delivery.md), [producer runtime contract](model-runtime-base.md),
[API README](../apps/inference-service/README.md) и
[MLflow model contract](mlflow-model-contract.md).
