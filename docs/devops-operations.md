# DevOps: краткая инструкция эксплуатации

## Что где лежит

| Место | Владелец | Назначение |
| --- | --- | --- |
| Git repository | DevOps/разработчики | Код, `release.env`, compose, CI и base-runtime manifest. Секретов нет. |
| Registry | DevOps | Готовые образы основного сервиса и base runtime. Production VM только скачивает их. |
| `/etc/ml-inference-service/runtime.env` | root | Секреты, PostgreSQL, MLflow и лимиты runtime. Не коммитить. |
| `/etc/ml-inference-service/tokens` | root | Хеши predict/deploy токенов. |
| `/etc/ml-inference-service/runtime-base.env` | controller | Последний разрешённый pinned digest base runtime. |
| `/etc/ml-inference-service/active-release.env` | controller | Текущий и standby релизы сервиса. Не редактировать вручную. |
| `/opt/ml-inference-service/releases/` | controller | Staged bundles релизов. |
| `/var/lib/ml-inference-service/model-artifacts/` | сервис | Кэш MLflow artifacts. Не очищать во время работы. |
| `/etc/nginx/conf.d/ml-inference-service*.conf` | root/controller | Public proxy и переключаемый upstream. |

GitLab runner работает на production VM, но не имеет Docker socket и прямого
доступа к `/etc`. Его единственный production sudo-вызов — root-owned
`/usr/local/sbin/ml-inference-deploy`.

## Однократная подготовка VM

1. Установить Docker Engine с Compose plugin, Nginx и GitLab shell runner.
2. Создать внешнюю Docker network `ml-inference-runtime`.
3. Создать `/etc/ml-inference-service` и `/var/lib/ml-inference-service/model-artifacts` с правами root; каталог artifacts должен быть доступен контейнеру сервиса.
4. Создать `runtime.env` с `INFERENCE_DATABASE_URL`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `MLFLOW_TRACKING_URI`, `MODEL_ARTIFACT_CACHE_ROOT`, `MODEL_RUNTIME_BASE_FILE`, лимитами fleet и при необходимости `PREVIOUS_RUNTIME_TTL_SECONDS`.
5. Создать predict/deploy tokens через `scripts/manage-inference-token.sh`.
6. Установить Nginx-конфигурацию из `infra/nginx/`, проверить `nginx -t` и reload.
7. Установить `scripts/production-controller.sh` как `/usr/local/sbin/ml-inference-deploy` (`root:root`, `0750`), а его `controller.env` — `root:root`, `0600`. Разрешить runner только команды `deploy`, `rollback`, `status`, `runtime-base`.

До первого rollout выпустите и закоммитьте оба manifest: `release.env` и
`projects/model-runtime-base/base.env`. Placeholder digest и пустой release
commit запускать нельзя.

## Обычный выпуск сервиса

На рабочей машине DevOps с доступом к registry:

```bash
git switch main
git pull --ff-only
make test
make release-preview
make release
git diff -- release.env
git add release.env
git commit -m "release: <version>"
git push origin main
```

После pipeline вручную запустить `deploy-production`. Он применяет миграции,
проверяет candidate и только затем меняет Nginx upstream. Для проверки:

```bash
sudo /usr/local/sbin/ml-inference-deploy status
curl --fail http://127.0.0.1:<active-port>/health/ready
```

## Выпуск base runtime

Меняйте base runtime только при изменении общих зависимостей моделей, Dockerfile
или runner:

```bash
make runtime-base-preview
make runtime-base-release
git add projects/model-runtime-base/base.env
git commit -m "runtime-base: <version>"
git push origin main
```

Запустите ручную job `deploy-runtime-base-config`; она устанавливает manifest с
pinned digest, но не перезапускает сервис. Затем ML-инженер запускает новый
Airflow deployment одной active модели с новым `Idempotency-Key`.

## Откат и инциденты

- Для отката сервиса используйте только ручную job `rollback-production` пока
  есть healthy standby. Миграции БД не откатываются.
- Если candidate не ready, Nginx остаётся на прежнем slot; смотреть логи candidate
  через `docker compose` в его staged release directory.
- Если сервис не ready после reboot, проверить `runtime.env`, digest в
  `runtime-base.env`, Docker network, artifact cache, лимиты VM и состояние
  PostgreSQL. Не переключать upstream вручную.
- Не удалять artifacts, containers или файлы active-release во время rollout.
