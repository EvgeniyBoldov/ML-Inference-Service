# Инструкция DevOps: base runtime image моделей

`projects/model-runtime-base` — subproject, выпускающий общий immutable image
для полного model fleet. Production VM не имеет доступа в интернет, поэтому
никакие зависимости не ставятся во время deployment модели.

## Когда нужен новый base image

Выпускайте его только при изменении:

- `projects/model-runtime-base/requirements.txt`;
- `projects/model-runtime-base/Dockerfile`;
- `projects/model-runtime-base/runner.py`.

Обычная новая MLflow-модель с уже доступными совместимыми пакетами base image не
требует его пересборки. Если модель не загружается из-за отсутствующей или
несовместимой библиотеки, ML-инженер передаёт DevOps точные пакет и версию;
DevOps меняет requirements и выпускает новый image.

## Выпуск image

На ноутбуке DevOps с интернетом и registry access:

```bash
git switch main
git pull --ff-only
make runtime-base-preview
make runtime-base-release
git diff -- projects/model-runtime-base/base.env
git add projects/model-runtime-base
git commit -m "runtime-base: <версия>"
git push origin main
```

`make runtime-base-preview` показывает текущую версию и хеш входных файлов.
`make runtime-base-release` при изменении входов повышает patch-версию,
собирает/push-ит образ, затем записывает в `base.env` его pinned digest вида
`registry/...@sha256:...`. Не допускается коммит с
`RUNTIME_BASE_IMAGE=...REPLACE_AFTER_RELEASE`.

Для minor/major DevOps вручную меняет `RUNTIME_BASE_VERSION` на `X.Y.0` до
выпуска; команда затем создаст `X.Y.1`.

## Доставка manifest на production

Изменение `projects/model-runtime-base/**` запускает отдельную GitLab job. Она
копирует `base.env` на VM в:

```text
/etc/ml-inference-service/runtime-base.env
```

Это только меняет manifest следующего fleet rollout; существующий BLUE runtime
не перезапускается и остаётся на старом image. Проверьте:

```bash
sudo sed -n '1,20p' /etc/ml-inference-service/runtime-base.env
```

## Применение нового image ко всем моделям

После успешной job запускается новый Airflow deployment одной из активных моделей
с её неизменяемым URI и **новым** `Idempotency-Key`. Service создаст GREEN fleet
из digest в `runtime-base.env`, примонтирует существующий artifact cache, загрузит
и smoke-тестит все active-модели. Только успешный полный fleet становится active.

Используйте новый ключ только для нового rollout. Тот же ключ — исключительно
для retry идентичного Airflow run.

При ошибке dependency/import/healthcheck Airflow получит `failed`; текущий BLUE
fleet и его image останутся обслуживать запросы. Исправьте requirements или
Dockerfile, выпустите следующий base image и повторите deployment.

## Ресурсы и откат

Docker image layers экономят место на диске, но не память загруженных моделей.
Во время rollout одновременно существуют BLUE и GREEN fleet, поэтому VM должна
вмещать примерно две полные суммы RAM моделей плюс FastAPI, PostgreSQL и запас.
Лимиты `MODEL_FLEET_MEMORY_LIMIT` и `MODEL_FLEET_CPU_LIMIT` задаются в
`runtime.env` и выбираются по измерениям.

Rollback доступен для последнего fleet-перехода, пока не истёк TTL previous
runtime. Если требуется вернуться на старый base image после следующего rollout,
верните в Git соответствующий `base.env` и повторите Airflow deployment, чтобы
сервис собрал и проверил новый GREEN fleet из нужного digest.
