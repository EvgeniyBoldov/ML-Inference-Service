# DevOps: краткая инструкция эксплуатации

## Где хранится состояние

| Место | Назначение |
| --- | --- |
| Git `release.env` | Версии сервиса/base, хеш, pinned images, source SHA, DB revision. |
| `projects/model-runtime-base/requirements.txt` | Все production-библиотеки API и моделей. |
| `/etc/ml-inference-service/runtime.env` | Секреты, PostgreSQL, MLflow/S3, лимиты. |
| `/etc/ml-inference-service/active-release.env` | Active и остановленный standby релизы. |
| `/opt/ml-inference-service/releases/<версия>/` | Immutable bundle и runtime manifest каждого релиза. |
| `/var/lib/ml-inference-service/model-artifacts/` | Кэш моделей, общий для API и fleet. |

Runner не имеет Docker socket. Он вызывает root-owned
`/usr/local/sbin/ml-inference-deploy` через ограниченный passwordless sudo.
Bootstrap и установка controller описаны в [delivery.md](delivery.md).
При переходе на единый release обновите установленный controller из репозитория.

## Выпуск

На рабочей станции DevOps в production GitLab clone/default branch:

```bash
git add <изменённые-файлы>
git commit -m "Update service or model dependencies"
make release-preview
make release
```

`make release` сам собирает base при изменении Dockerfile/requirements,
собирает API/runtime, пушит образы, записывает и коммитит `release.env`, пушит Git.
Pipeline автоматически запускает `deploy_production` после проверок.

На production новая версия проходит миграцию/healthcheck, переключается Nginx,
затем останавливается прежний API. PostgreSQL и активный model fleet не останавливаются.

```bash
sudo /usr/local/sbin/ml-inference-deploy status
curl --fail http://127.0.0.1:<active-port>/health/ready
```

## Новые библиотеки моделей

Добавьте библиотеку в общий requirements, закоммитьте и выполните `make release`.
Дождитесь production deployment и повторите model deployment с новым Idempotency-Key.
Отдельный release/delivery base больше не требуется.

## Откат и диагностика

- `rollback_production` запускает сохранённый standby API, проверяет readiness,
  переключает upstream и останавливает заменённый API. Миграции БД не откатываются.
- Failed candidate не становится active; смотрите его логи в staged release.
- Ошибки MLflow/загрузки моделей содержат traceback в JSON-поле `exception`.
- Проверяйте runtime manifest текущего bundle, Docker network/cache/лимиты,
  MLflow и S3 credentials в контейнере API.
- Не очищайте artifacts и не редактируйте active-state/upstream вручную во время rollout.
