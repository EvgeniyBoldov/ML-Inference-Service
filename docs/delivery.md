# Release and production delivery

## Topology

```text
DevOps workstation ─ make release ─> production registry
       │                                     │
       └─ commit/push release.env ─> GitLab pipeline / shell runner
                                                  │
                                                  ▼
                                  /opt/ml-inference-service/releases/<version>
                                                  │
                                      direct healthcheck of inactive slot
                                                  │
                                                  ▼
                         host Nginx → 127.0.0.1:18001 (blue) or :18002 (green)
```

The GitLab runner must run on the production VM without Docker access. It may
invoke only the root-owned `/usr/local/sbin/ml-inference-deploy` controller
through passwordless, tightly-scoped `sudo`.

The controller reads root-owned `/etc/ml-inference-service/controller.env`:

```dotenv
APP_ROOT=/opt/ml-inference-service
ETC_ROOT=/etc/ml-inference-service
CI_BUILDS_ROOT=/builds
```

Install `scripts/production-controller.sh` as `root:root`, mode `0750`, and
allow the runner to execute only
`ml-inference-deploy deploy|rollback|status|runtime-base`.
The controller stages only the compose file, release manifest and deploy
helpers into `/opt/ml-inference-service/releases/<commit-sha>`.

## Release manifest

`release.env` is versioned in Git and has no secret values. It contains the
registry address, image name, two host ports, release version, and the exact
source commit that formed the image. Runtime credentials/configuration are only
in `/etc/ml-inference-service/runtime.env` and must never be committed.

`make release-preview` displays the committed/current manifest and the manifest
that a release would create. It compares source changes with `RELEASE_COMMIT`
while deliberately excluding `release.env`: the subsequent manifest-only Git
commit must not trigger a duplicate release.

`make release` requires a clean, committed source tree, increments the patch
version, records `HEAD` in `RELEASE_COMMIT`, builds the image, and pushes tags by
version and source commit. It changes `release.env` but does not commit or push
it. DevOps reviews, commits, and pushes it explicitly.

The GitLab deployment job runs on the default branch only when the release
manifest or deployment assets change. Ordinary source commits therefore do not
redeploy a stale image; the manifest commit produced after `make release` is the
deployment trigger.

To request major/minor, edit `RELEASE_VERSION` to an `X.Y.0` base. The next
`make release` emits `X.Y.1`, even if application sources are unchanged.

## Production bootstrap

Before the first deployment, provision:

- `/etc/ml-inference-service/runtime.env` with MLflow, PostgreSQL, and service secrets; set
  `INFERENCE_DATABASE_URL=postgresql+asyncpg://ml_inference:<password>@postgres:5432/ml_inference`
  plus `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `MLFLOW_TRACKING_URI`
  there. The deployment job starts PostgreSQL from the common Compose file before
  applying migrations.
- `/etc/ml-inference-service/tokens`, created with
  `scripts/manage-inference-token.sh`; it is mounted read-only into the service.
- `/etc/ml-inference-service/runtime-base.env`, copied by the runtime-base
  GitLab job from `projects/model-runtime-base/base.env` and containing a pinned
  `RUNTIME_BASE_IMAGE` digest.
- `/var/lib/ml-inference-service/model-artifacts`, writable by the service and
  shared read-only with fleet runtime containers.
- `/etc/nginx/conf.d/ml-inference-service.conf` from
  `infra/nginx/ml-inference-service.conf`.
- `/etc/nginx/conf.d/ml-inference-service-upstream.conf` from the included
  example, then validate/reload Nginx.
- Docker and Compose plugin; registry access is unauthenticated by current
  infrastructure decision.

The versioned manifests in a fresh repository intentionally contain no release
commit/base digest. They are not deployable initial configuration: first publish
the application with `make release`, publish the runtime base with
`make runtime-base-release`, review the resulting manifests, and commit them
before enabling either production job.

The deployment job executes `alembic upgrade head` using the candidate image
before it starts the candidate service. The database URL is read only from the
host-owned runtime environment file.

For fleet model runtime, add the following values to `runtime.env`:

```dotenv
MODEL_ARTIFACT_CACHE_ROOT=/var/lib/ml-inference-service/model-artifacts
MODEL_RUNTIME_BASE_FILE=/etc/ml-inference-service/runtime-base.env
MODEL_FLEET_MEMORY_LIMIT=24g
MODEL_FLEET_CPU_LIMIT=8
```

Limits are environment-specific examples. A blue/green fleet rollout loads both
the old and new complete model sets, so the VM must have headroom for roughly two
loaded fleets.

## Blue/green and rollback

The deploy job starts the new immutable release in the inactive port slot and
healthchecks it directly. Only on success does it replace Nginx's upstream file
and reload Nginx. The former active slot remains running as one-release standby.
The state file separately records active and standby projects; the next deployment
reclaims only that inactive standby slot, never the active one, before starting a
candidate.

To roll back, use the manual `rollback-production` job from the supplied
`gitlab-ci.example.yml`. It promotes the healthy standby release and swaps the
Nginx upstream atomically. Database migrations remain forward-only.
