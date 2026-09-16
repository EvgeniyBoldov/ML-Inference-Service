#!/usr/bin/env bash

# Compatibility entrypoint. Production GitLab jobs must use the root-owned
# controller directly; this wrapper intentionally performs no Docker/sudo work.
set -Eeuo pipefail
controller="/usr/local/sbin/ml-inference-deploy"
test -x "$controller" || { echo "Missing production controller: $controller" >&2; exit 1; }
exec "$controller" deploy --source "${CI_PROJECT_DIR:-$(pwd)}" --release "$(git rev-parse HEAD)"
