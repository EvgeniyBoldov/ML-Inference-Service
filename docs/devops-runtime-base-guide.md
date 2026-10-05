# Инструкция DevOps: библиотеки и base

Production-зависимости API и моделей задаются только в
`projects/model-runtime-base/requirements.txt`. Они устанавливаются при локальной
сборке base. API и model runtime наследуют его; production VM только скачивает образы.

## Добавление библиотеки модели

```bash
# После изменения projects/model-runtime-base/requirements.txt:
git add projects/model-runtime-base/requirements.txt
git commit -m "Add model library"
make release-preview
make release
```

`make release-preview` показывает текущую и следующую версии сервиса/base,
сохранённый и вычисленный хеши, необходимость пересборки base.
`make release` автоматически собирает base при изменении Dockerfile или requirements,
затем собирает API и runtime, пушит образы, коммитит и пушит `release.env`.

При изменении только кода base используется повторно. `runner.py` копируется в
отдельный runtime image каждого релиза и не входит в хеш dependency base.

## Production

GitLab `deploy_production` скачивает API и pinned runtime, проверяет версии, source
commit и хеш base в labels образов. Controller фиксирует runtime в manifest именно
этого релиза. Отдельная job доставки base и общий изменяемый `runtime-base.env` в
`/etc` больше не требуются.

После успешного production deployment ML-инженер повторяет model deployment с
новым `Idempotency-Key`. Без релиза с нужной библиотекой модель не загрузится;
работающий fleet продолжит обслуживать запросы.

Для проверки текущего релиза:

```bash
sudo /usr/local/sbin/ml-inference-deploy status
sudo cat /opt/ml-inference-service/current/release.env
```
