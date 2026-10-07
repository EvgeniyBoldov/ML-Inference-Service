# Инструкция для ML-инженера

Эта инструкция описывает контракт публикации модели из Jupyter или Airflow и
развёртывания новой неизменяемой версии в ML Inference Service.

## Установка и настройка

Установите общую библиотеку в окружение Jupyter или Airflow:

```bash
pip install 'ml-inference-contracts[mlflow]'
```

Настройте MLflow стандартным для проекта способом (`MLFLOW_TRACKING_URI` и
учётные данные artifact storage). Для Airflow-задачи deployment дополнительно
нужны секреты:

```text
INFERENCE_SERVICE_URL=http://ml-inference-service.internal
INFERENCE_DEPLOYMENT_TOKEN=<токен с правами deployment.write и deployment.read>
```

Не храните токены сервиса, credentials artifact storage или персональные данные
в notebook, MLflow tags, model examples или Git-репозитории.

## Чек-лист deployable-модели

Для точной зарегистрированной версии модели в MLflow должны присутствовать:

| Обязательное значение MLflow | Аргумент publisher | Назначение в сервисе |
| --- | --- | --- |
| Registered model name | `registered_model_name` | Стабильный публичный ID модели |
| Неизменяемая версия | Создаётся при регистрации в MLflow | Идентификатор deployment |
| PyFunc artifact | `python_model` | Загрузка runtime |
| Input/output signature | Выводится из обоих examples | JSON Schema API и validation |
| Input example | `input_example` | Smoke test и model discovery |
| Description | `description` | Описание для людей и агентов |
| Owner | `owner` | Поле `owned_by` в API |

Модель должна поддерживать вызов:
`mlflow.pyfunc.load_model(uri).predict(input)`. Логическое имя модели должно
оставаться стабильным; версию нельзя зашивать в имя.

## Публикация из Jupyter

Используйте общий logger. Он записывает PyFunc artifact, вычисляет signature,
регистрирует версию модели и сохраняет description и owner tags.

```python
from ml_inference_contracts import log_pyfunc_model

input_example = {
    "age": 38,
    "income": 150000.0,
    "loan_amount": 2_000_000.0,
}
output_example = {"probability": 0.82, "prediction": 1}

publication = log_pyfunc_model(
    registered_model_name="credit-scoring",
    description="Probability of customer default for loan underwriting.",
    owner="risk-team",
    python_model=model,  # Объект, совместимый с mlflow.pyfunc.PythonModel.
    input_example=input_example,
    output_example=output_example,
    source_git_commit="<training-source-git-sha>",
    tags={"domain": "risk", "dataset": "loan-application-v3"},
)

publication.model_uri  # models:/credit-scoring/18
```

`input_example` должен быть корректным production-запросом, который клиент
передаст в поле `input` endpoint `POST /v1/responses`. `output_example` должен
точно соответствовать результату `predict` для этого входа. Во время deployment
сервис использует `input_example` как smoke test.

## Откуда берутся схемы API

Logger вызывает MLflow `infer_signature(input_example, output_example)` и
сохраняет результат в metadata модели вместе с artifact входного примера.

```text
input example + output example
        ↓ MLflow infer_signature
MLflow Model Signature
        ↓ Schema Adapter сервиса
JSON Schema → GET /v1/models → validation перед predict
```

Именованные поля MLflow становятся properties объектной JSON Schema:
`age: long` превращается в `age: integer`, а `income: double` — в
`income: number`.

## Airflow: публикация и deployment

Airflow — единственный штатный клиент deployment API. Training task публикует
модель, а следующая task получает точный неизменяемый URI
`models:/name/version` и разворачивает именно его. Нельзя использовать alias
(`@champion`, `@latest`) или напрямую формировать artifact-store path.

Полный пример DAG с безопасным для XCom форматом находится в
[`packages/ml-inference-contracts/examples/airflow_publish_and_deploy.py`](../packages/ml-inference-contracts/examples/airflow_publish_and_deploy.py).

Ключевой deployment-вызов:

```python
client = DeploymentClient.from_environment()
deployment = client.deploy_and_wait(
    model=publication["registered_model_name"],
    model_uri=publication["model_uri"],
    idempotency_key=f"{dag_run.run_id}:{publication['registered_model_name']}:{publication['model_version']}",
)
```

Вызов возвращается только после получения статуса `active`. При `failed` или
таймауте выбрасывается `DeploymentError`, поэтому Airflow task завершается
ошибкой. Повтор с тем же idempotency key безопасен для retry той же версии.

## Обновление существующей модели

Публикуйте новую версию с тем же `registered_model_name`, например
`credit-scoring`. MLflow создаст v19, а Airflow должен отправить
`models:/credit-scoring/19`.

Сервис загрузит новую версию, выполнит prediction на input example, проверит
output schema, прогреет runtime и атомарно переключит routing. Candidate
загружает и smoke-тестит все модели runtime group с тем же producer image.
Несовместимые environments остаются в отдельных группах. Предыдущая версия
остаётся на standby до истечения rollback TTL. Зарегистрированную версию нельзя
изменять на месте.

## Зависимости модели и base image

В production зависимости не скачиваются. Runtime запускается из producer image,
указанного при логировании модели в `ML_INFERENCE_PRODUCER_IMAGE`. При
несовместимой библиотеке соберите новую версионированную Airflow/Jupyter image,
доставьте её на production host и обновите переменную в producer service. Затем
залогируйте и зарегистрируйте новую model version в этом environment и передайте
её URI на deployment. Используйте новый `Idempotency-Key` для нового запуска.
Модели с прежними image продолжают работать в своих runtime groups.

Rollback относится к последнему успешному fleet-переходу. После следующего
deployment старый fleet может быть уже не тем набором моделей, который нужен для
предыдущего обновления; в таком случае сервис безопасно вернёт `DEPLOYMENT_FAILED`.

## Типовые ошибки

| Ошибка | Что нужно сделать |
| --- | --- |
| `MODEL_SIGNATURE_REQUIRED` | Передать корректные input и output examples. |
| `INPUT_EXAMPLE_REQUIRED` | Передать JSON-совместимый production-valid input. |
| `MODEL_DESCRIPTION_REQUIRED` | Указать непустое описание модели. |
| `MODEL_LOAD_FAILED` | Сделать artifact совместимым с PyFunc и проверить зависимости. |
| `MODEL_OUTPUT_VALIDATION_FAILED` | Синхронизировать `output_example` с реальным результатом `predict`. |
| `MLFLOW_UNAVAILABLE` | Повторить deployment после восстановления MLflow/artifact storage; активные модели продолжат работать. |

## Граница текущей реализации

Уже реализованы MLflow metadata adapter, PyFunc loading, API и deployment
lifecycle, PostgreSQL repository, Docker/GitLab delivery, publisher и Airflow
client.

Текущий runtime запускает один Docker container для полного active fleet. Это
осознанная граница: новые зависимости допускаются только после успешной проверки
всех active-моделей. Отдельных model environments платформа не поддерживает.
