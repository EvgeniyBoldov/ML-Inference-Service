# Базовый образ model runtime

`projects/model-runtime-base` — независимый subproject с общим immutable image
для всего набора ML-моделей. В production этот image не устанавливает пакеты и
не требует доступа в интернет.

## Содержимое

- `requirements.txt` — общий совместимый набор библиотек всех моделей;
- `Dockerfile` — сборка base image;
- `runner.py` — generic HTTP runtime, который загружает полный model fleet из
  read-only manifest и artifact cache;
- `base.env` — версия, хеш входных файлов и pinned image digest.

## Выпуск

На ноутбуке DevOps с интернетом:

```bash
make runtime-base-preview
make runtime-base-release
```

Вторая команда повышает patch-версию при изменении Dockerfile, requirements или
runner, собирает и пушит image, затем записывает его digest в `base.env`.
Изменённый `base.env` нужно проверить, закоммитить и отправить в GitLab.

Изменения `projects/model-runtime-base/**` намеренно исключены из
`make release-preview` основного Inference Service. У base image свой lifecycle.
GitLab job копирует актуальный manifest на production VM в
`/etc/ml-inference-service/runtime-base.env`.

## Fleet rollout

Новый deployment создаёт GREEN container из digest, указанного в manifest. В
него монтируется полный набор artifact моделей. Runtime загружает и smoke-тестит
все модели, включая уже существующие. Только после этого сервис атомарно меняет
active fleet на GREEN. BLUE fleet остаётся для rollback TTL.

Если новая base image не совместима хотя бы с одной legacy-моделью, переключения
не будет: Airflow увидит failed deployment, а BLUE fleet останется рабочим.
