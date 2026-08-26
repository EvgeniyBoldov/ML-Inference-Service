#!/usr/bin/env bash
# Invoked by the GitLab shell runner on the production VM.
set -euo pipefail

repo_root="${CI_PROJECT_DIR:-$(pwd)}"
manifest="$repo_root/release.env"
compose_source="$repo_root/infra/compose/inference-service.compose.yaml"
project_root="${PROJECT_ROOT:-/opt/ml-inference-service}"
etc_root="${ETC_ROOT:-/etc/ml-inference-service}"
runtime_env="$etc_root/runtime.env"
state_file="$etc_root/active-release.env"
nginx_upstream="/etc/nginx/conf.d/ml-inference-service-upstream.conf"

die() { echo "deploy: $*" >&2; exit 1; }
[[ -f "$manifest" && -f "$compose_source" ]] || die "release manifest or compose source is missing"
[[ -f "$runtime_env" ]] || die "production runtime env is missing: $runtime_env"

set -a
. "$manifest"
set +a
for required in RELEASE_VERSION RELEASE_COMMIT REGISTRY IMAGE BLUE_PORT GREEN_PORT; do
  [[ -n "${!required:-}" ]] || die "$required is required in release.env"
done

release_dir="$project_root/releases/$RELEASE_VERSION"
project_name="ml-inference-${RELEASE_VERSION//./-}"
active_slot=""
active_port=""
active_project=""
active_dir=""
standby_project=""
standby_dir=""
if [[ -f "$state_file" ]]; then
  set -a
  . "$state_file"
  set +a
  active_slot="${ACTIVE_SLOT:-}"
  active_port="${ACTIVE_PORT:-}"
  active_project="${ACTIVE_PROJECT:-}"
  active_dir="${ACTIVE_DIR:-}"
  standby_project="${STANDBY_PROJECT:-}"
  standby_dir="${STANDBY_DIR:-}"
fi

if [[ "$active_slot" == "blue" ]]; then
  candidate_slot="green"; candidate_port="$GREEN_PORT"
else
  candidate_slot="blue"; candidate_port="$BLUE_PORT"
fi

# Only the inactive slot can be reclaimed. The currently active release remains
# untouched until the candidate passes a direct healthcheck and Nginx is switched.
if [[ -n "$standby_project" && -n "$standby_dir" && -f "$standby_dir/compose.yaml" ]]; then
  SLOT_PORT="$candidate_port" docker compose --project-name "$standby_project" --env-file "$standby_dir/release.env" -f "$standby_dir/compose.yaml" down --remove-orphans || true
fi

install -d "$release_dir"
install -m 0644 "$compose_source" "$release_dir/compose.yaml"
install -m 0644 "$manifest" "$release_dir/release.env"

SLOT_PORT="$candidate_port" docker compose --project-name "$project_name" --env-file "$release_dir/release.env" -f "$release_dir/compose.yaml" pull
SLOT_PORT="$candidate_port" docker compose --project-name "$project_name" --env-file "$release_dir/release.env" -f "$release_dir/compose.yaml" up -d --remove-orphans

for _ in $(seq 1 24); do
  if curl --fail --silent --show-error "http://127.0.0.1:$candidate_port/health/ready" >/dev/null; then
    break
  fi
  sleep 5
done
curl --fail --silent --show-error "http://127.0.0.1:$candidate_port/health/ready" >/dev/null || die "candidate healthcheck failed; active release was not changed"

upstream_tmp="$(mktemp)"
state_tmp="$(mktemp)"
trap 'rm -f "$upstream_tmp" "$state_tmp"' EXIT
cat > "$upstream_tmp" <<EOF
upstream ml_inference_service {
    server 127.0.0.1:$candidate_port;
    keepalive 32;
}
EOF
sudo install -m 0644 "$upstream_tmp" "$nginx_upstream"
sudo nginx -t
sudo systemctl reload nginx

cat > "$state_tmp" <<EOF
ACTIVE_SLOT=$candidate_slot
ACTIVE_PORT=$candidate_port
ACTIVE_PROJECT=$project_name
ACTIVE_DIR=$release_dir
RELEASE_VERSION=$RELEASE_VERSION
RELEASE_COMMIT=$RELEASE_COMMIT
STANDBY_PROJECT=$active_project
STANDBY_DIR=$active_dir
EOF
sudo install -d -m 0750 "$etc_root"
sudo install -m 0640 "$state_tmp" "$state_file"
echo "Activated release $RELEASE_VERSION ($RELEASE_COMMIT) in $candidate_slot on port $candidate_port"
