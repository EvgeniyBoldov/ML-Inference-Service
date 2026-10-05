# Общий base образ

`projects/model-runtime-base/requirements.txt` — единый список production-библиотек
API и всех ML-моделей. `Dockerfile` устанавливает их в общий dependency base.
Production-контейнеры наследуют этот образ и не устанавливают пакеты при запуске.
`pyproject.toml` API содержит только метаданные Python-пакета и настройки тестов.

## Состав релиза

- dependency base: Dockerfile и requirements;
- API image: общий base плюс `apps/inference-service/app` и миграции;
- model runtime image: тот же base плюс `runner.py` из `Dockerfile.runtime`.

`make release` сравнивает хеш **только Dockerfile base и requirements.txt** с
`BASE_INPUT_SHA256` в `release.env`. При изменении этих файлов повышается
`BASE_VERSION` и публикуется новый base. Изменения API, миграций или runner не
пересобирают base: кодовые образы API и model runtime собираются для нового релиза.

## Выпуск

На рабочей станции DevOps с доступом к registry и production GitLab:

```bash
git add projects/model-runtime-base/requirements.txt
git commit -m "Add model dependencies"
make release-preview
make release
```

Последняя команда строит и пушит образы, записывает версии и digest в `release.env`,
коммитит manifest и пушит текущую ветку в её upstream. Изменённый manifest запускает
GitLab deployment. `make runtime-base-preview` и `make runtime-base-release` —
совместимые aliases единого процесса; отдельного `base.env` больше нет.

## Использование на production

Controller создаёт release bundle в `/opt/ml-inference-service/releases/<версия>/`.
Его `release.env` содержит `RUNTIME_IMAGE` и монтируется в API
как `/srv/release/runtime.env`. Каждая версия API использует свой pinned runtime.

Новая модель загружается в model runtime текущего релиза. Если библиотека отсутствует,
загрузка завершается ошибкой и текущий fleet продолжает работать. Добавьте библиотеку
в requirements, выполните полный release и production deployment, затем повторите
model deployment с новым `Idempotency-Key`. Пакеты из требований самой MLflow-модели
автоматически не устанавливаются.

Активный fleet сохраняет записанный при загрузке digest. Новый model deployment
строит fleet из runtime текущего релиза и проверяет все включённые модели перед
переключением; предыдущий fleet остаётся на rollback TTL.
