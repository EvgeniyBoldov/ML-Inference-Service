# Release and delivery

DevOps builds on a workstation with registry/PyPI access. Production only pulls
published images. GitLab's shell runner invokes the root-owned controller using
restricted passwordless sudo; it does not receive Docker socket access.

## One release workflow

```bash
# Commit source/dependency changes first; use the production default branch.
make release-preview
make release
```

`make release` requires a clean committed worktree and a configured remote upstream.
It verifies that the local branch contains upstream, then:

1. Calculates SHA256 from `projects/model-runtime-base/Dockerfile` and requirements.
2. Reuses the pinned base when the hash matches; otherwise bumps its version,
   builds and pushes a new dependency base.
3. Builds API and model-runtime images from the same base; code changes rebuild
   these images without rebuilding dependencies.
4. Checks API imports, reads the single Alembic head and pushes release images.
5. Verifies that local source and upstream did not change during the build.
6. Writes `release.env`, commits it and pushes source/release commits to upstream.

The manifest records `RELEASE_VERSION`, `RELEASE_COMMIT`, registry/image name,
`BASE_VERSION`, `BASE_INPUT_SHA256`, pinned `BASE_IMAGE` and `RUNTIME_IMAGE`,
`DB_REVISION`, and blue/green ports. It contains no credentials.

A failed build or image push leaves the manifest and Git history unchanged.
If the final Git push fails, retry `git push`; published images and the release
commit already exist. Published version tags are never overwritten.

The source comparison excludes the manifest-only commit so it does not create
an unnecessary next release. To request a new major/minor, commit an `X.Y.0`
release-version base first; the next release is `X.Y.1`.

## Production bootstrap

Install Docker Engine/Compose, Nginx and the shell runner tagged `production-shell`.
Create external Docker network `ml-inference-runtime`, the artifact cache and
`/etc/ml-inference-service/runtime.env` containing PostgreSQL, MLflow and artifact
storage settings. The cache must be writable by the API and readable by the runtime.

Install `scripts/production-controller.sh` root-owned at
`/usr/local/sbin/ml-inference-deploy`, mode 0750. Its
`/etc/ml-inference-service/controller.env` must be root:root 0600 and configure
`CI_BUILDS_ROOT`, optionally `APP_ROOT` and `ETC_ROOT`. Permit the runner only the
controller's `deploy`, `rollback`, and `status` operations through sudo.

When upgrading an existing installation to this release workflow, reinstall the
controller once with the updated script:

```bash
sudo install -o root -g root -m 0750 scripts/production-controller.sh /usr/local/sbin/ml-inference-deploy
```

Configure registry/image names in `release.env`, commit sources and publish the
first release before deployment. Initial empty hashes/digests are bootstrap values
and cannot be deployed.

## CI and blue/green

The default-branch release-manifest commit triggers `deploy_production` after tests.
The controller stages compose, deploy helpers, manifest and a derived
`runtime-base.env` under `/opt/ml-inference-service/releases/<RELEASE_VERSION>/`.
The directory's release marker records the exact CI commit; a different commit
cannot reuse that version directory.

PostgreSQL runs under the stable `ml-inference-postgres` project. For the new API
slot the controller pulls published API/runtime images, verifies image labels
against the manifest, checks the Alembic head, and applies `DB_REVISION`. It starts
the candidate on the inactive loopback port and waits for `/health/ready`.

After readiness it switches Nginx, records active/standby state and **stops the
previous API container**. Its release bundle/container is retained for rollback.
A failed candidate is stopped and the active API/upstream remain unchanged. Nginx
validation/reload failures restore the previous configuration file. Repeating an
already active deployment does not restart it or switch to the other slot.

The release-local runtime manifest is mounted read-only into API at
`/srv/release/runtime.env`; compose overrides `MODEL_RUNTIME_BASE_FILE`. Global
`/etc/ml-inference-service/runtime-base.env` is no longer used by new releases.

The manual `rollback_production` job starts the retained previous API, checks its
readiness, switches upstream, then stops the replaced API. Database migrations
are not rolled back; schema changes must remain compatible with the retained API.

## New model dependencies

Add libraries to the shared requirements and publish/deploy a service release
before deploying a model needing them. Model deployments use the current release's
runtime image and never install libraries dynamically. Existing active fleets
keep their saved digest until a new model deployment creates a replacement fleet.
