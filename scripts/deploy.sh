#!/usr/bin/env bash

# Executed only by the root-owned ml-inference-deploy controller.
set -Eeuo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/release-common.sh"
APP_ROOT="${ML_INFERENCE_APP_ROOT:-/opt/ml-inference-service}"
ETC_ROOT="${ML_INFERENCE_ETC_ROOT:-/etc/ml-inference-service}"
RUNTIME_ENV="${ML_INFERENCE_RUNTIME_ENV:-${ETC_ROOT}/runtime.env}"
STATE_FILE="${ML_INFERENCE_STATE_FILE:-${ETC_ROOT}/active-release.env}"
NGINX_UPSTREAM="${ML_INFERENCE_NGINX_UPSTREAM:-/etc/nginx/conf.d/ml-inference-service-upstream.conf}"
RELEASE_DIR=""
DEPLOY_WAIT_TIMEOUT="${DEPLOY_WAIT_TIMEOUT:-300}"
usage() { cat >&2 <<'EOF'
Usage:
  deploy.sh deploy --release-dir DIR
  deploy.sh rollback
  deploy.sh status
  deploy.sh validate --release-dir DIR
EOF
  exit 2
}
require_root() { test "$EUID" -eq 0 || fail "deploy.sh must run through the root-owned controller."; }
current_value() { local key="$1"; [[ -f "$STATE_FILE" ]] && sed -n "s/^${key}=//p" "$STATE_FILE" | tail -n 1 || true; }
compose() {
  test -f "$RELEASE_DIR/release.env" && test -r "$RELEASE_DIR/release.env" || fail "Release manifest must be a readable file: $RELEASE_DIR/release.env"
  INFERENCE_RUNTIME_ENV_FILE="$RUNTIME_ENV" SLOT_PORT="${SLOT_PORT:-1}" docker compose --project-name "$COMPOSE_PROJECT" --env-file "$RUNTIME_ENV" --env-file "$RELEASE_DIR/release.env" -f "$RELEASE_DIR/compose.yaml" "$@"
}
validate_runtime() {
  require_command docker; require_command curl; require_command flock; require_command nginx; require_command systemctl
  test -r "$RUNTIME_ENV" || fail "Runtime environment is missing: $RUNTIME_ENV"
  docker network inspect ml-inference-runtime >/dev/null 2>&1 || fail "Docker network is missing: ml-inference-runtime"
  test -d /var/lib/ml-inference-service/model-artifacts || fail "Model artifact directory is missing"
  for key in INFERENCE_DATABASE_URL POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD; do grep -Eq "^${key}=.+" "$RUNTIME_ENV" || fail "$key is required in $RUNTIME_ENV"; done
}
load_bundle() {
  local dir="$1"; test -d "$dir" || fail "Release directory is missing: $dir"
  test -f "$dir/.release-id" || fail "Release marker is missing: $dir"; test -f "$dir/compose.yaml" || fail "Release compose is missing: $dir"; test -f "$dir/release.env" || fail "Release manifest is missing: $dir"
  RELEASE_DIR="$dir"; load_release_file "$dir/release.env" "${2:-}"; COMPOSE_PROJECT="ml-inference-${RELEASE_VERSION//./-}"; compose config --quiet
}
start_postgres() {
  RELEASE_DIR="$1"; COMPOSE_PROJECT=ml-inference-postgres; SLOT_PORT=1; compose up -d postgres
  for _ in $(seq 1 36); do compose exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1 && return; sleep 2; done
  fail "PostgreSQL is not ready"
}
switch_upstream() {
  local port="$1" tmp backup had_upstream=false
  tmp="$(mktemp)"; backup="$(mktemp)"
  if [[ -f "$NGINX_UPSTREAM" ]]; then cp "$NGINX_UPSTREAM" "$backup"; had_upstream=true; fi
  cat > "$tmp" <<EOF
upstream ml_inference_service {
    server 127.0.0.1:${port};
    keepalive 32;
}
EOF
  install -m 0644 "$tmp" "$NGINX_UPSTREAM" || { rm -f "$tmp" "$backup"; return 1; }
  if nginx -t && systemctl reload nginx; then rm -f "$tmp" "$backup"; return; fi
  if [[ "$had_upstream" == true ]]; then install -m 0644 "$backup" "$NGINX_UPSTREAM"; else rm -f "$NGINX_UPSTREAM"; fi
  rm -f "$tmp" "$backup"
  release_log "ERROR: Nginx switch failed; previous upstream configuration was restored"
  return 1
}
wait_ready() { local port="$1"; for _ in $(seq 1 "$DEPLOY_WAIT_TIMEOUT"); do curl --fail --silent --show-error "http://127.0.0.1:${port}/health/ready" >/dev/null && return; sleep 1; done; return 1; }
write_state() {
  local slot="$1" port="$2" project="$3" dir="$4" standby_project="$5" standby_dir="$6" tmp
  tmp="$(mktemp "${ETC_ROOT}/.active-release.XXXXXX")"
  printf 'ACTIVE_SLOT=%s\nACTIVE_PORT=%s\nACTIVE_PROJECT=%s\nACTIVE_DIR=%s\nRELEASE_VERSION=%s\nRELEASE_COMMIT=%s\nSTANDBY_PROJECT=%s\nSTANDBY_DIR=%s\n' "$slot" "$port" "$project" "$dir" "$RELEASE_VERSION" "$RELEASE_COMMIT" "$standby_project" "$standby_dir" > "$tmp"
  chmod 0640 "$tmp"; mv -f "$tmp" "$STATE_FILE"
}
stop_release_service() (
  local dir="$1" project="$2" port="$3"
  [[ -n "$project" && -n "$dir" ]] || return 0
  load_bundle "$dir" legacy
  COMPOSE_PROJECT="$project"; SLOT_PORT="$port"
  compose stop inference-service
)
verify_release_image() {
  local image actual_version actual_commit actual_base_sha
  for image in "$REGISTRY/$IMAGE:$RELEASE_VERSION"; do
    actual_version="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$image")"
    actual_commit="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")"
    actual_base_sha="$(docker image inspect --format '{{index .Config.Labels "com.ml-inference.runtime-input-sha256"}}' "$image")"
    [[ "$actual_version" == "$RELEASE_VERSION" && "$actual_commit" == "$RELEASE_COMMIT" && "$actual_base_sha" == "$BASE_INPUT_SHA256" ]] || fail "Pulled image labels do not match release.env: $image"
  done
}
verify_migrations() {
  local heads revision
  heads="$(compose run --rm --no-deps inference-service alembic heads)"
  revision="$(awk '/\(head\)/ {print $1}' <<< "$heads")"
  [[ "$revision" == "$DB_REVISION" ]] || fail "Release DB_REVISION does not match the image Alembic head"
}
deploy() {
  local old_slot old_port old_project old_dir standby_project standby_dir candidate_dir candidate_slot candidate_port project
  load_bundle "$RELEASE_DIR"; old_slot="$(current_value ACTIVE_SLOT)"; old_project="$(current_value ACTIVE_PROJECT)"; old_dir="$(current_value ACTIVE_DIR)"; standby_project="$(current_value STANDBY_PROJECT)"; standby_dir="$(current_value STANDBY_DIR)"
  candidate_dir="$RELEASE_DIR"
  old_port="$(current_value ACTIVE_PORT)"
  if [[ "$old_dir" == "$candidate_dir" ]]; then
    wait_ready "$old_port" || fail "Already active release is not ready"
    # A retry also finishes cleanup if the prior job stopped after promotion.
    if [[ "$old_slot" == blue ]]; then candidate_port="$GREEN_PORT"; else candidate_port="$BLUE_PORT"; fi
    [[ -z "$standby_project" || "$standby_project" != "$old_project" ]] || fail "Standby project matches active project"
    stop_release_service "$standby_dir" "$standby_project" "$candidate_port"
    release_log "Release is already active: $RELEASE_VERSION"
    return
  fi
  if [[ "$old_slot" == blue ]]; then candidate_slot=green; candidate_port="$GREEN_PORT"; else candidate_slot=blue; candidate_port="$BLUE_PORT"; fi
  project="ml-inference-${RELEASE_VERSION//./-}"
  [[ "$project" != "$old_project" ]] || fail "Release version is already active from a different bundle; publish a new version"
  [[ -z "$standby_project" || "$standby_project" != "$old_project" ]] || fail "Standby project matches active project"
  if [[ -n "$standby_project" && -n "$standby_dir" && -f "$standby_dir/compose.yaml" ]]; then COMPOSE_PROJECT="$standby_project"; RELEASE_DIR="$standby_dir"; SLOT_PORT="$candidate_port" compose down --remove-orphans; fi
  # Re-select the immutable candidate after reclaiming the old standby.
  load_bundle "$candidate_dir"
  install -d -m 0750 "$RELEASE_DIR"; COMPOSE_PROJECT="$project"; SLOT_PORT="$candidate_port"
  compose pull inference-service
  verify_release_image; verify_migrations
  compose run --rm --no-deps inference-service alembic upgrade "$DB_REVISION"
  compose up -d --force-recreate --remove-orphans --no-deps inference-service
  if ! wait_ready "$candidate_port"; then
    compose logs --tail=100 inference-service >&2 || true
    compose stop inference-service || true
    fail "Candidate healthcheck failed; active release was not changed"
  fi
  if ! switch_upstream "$candidate_port"; then compose stop inference-service || true; fail "Nginx switch failed; active release was not changed"; fi
  write_state "$candidate_slot" "$candidate_port" "$project" "$RELEASE_DIR" "$old_project" "$old_dir"
  stop_release_service "$old_dir" "$old_project" "$old_port"
  release_log "Deployment complete: release=$RELEASE_VERSION source=$RELEASE_COMMIT slot=$candidate_slot"
}
rollback() {
  local target_dir target_project target_slot target_port old_project old_dir old_port
  target_dir="$(current_value STANDBY_DIR)"; test -n "$target_dir" || fail "No standby release is available for rollback."
  load_bundle "$target_dir" legacy; target_project="$(current_value STANDBY_PROJECT)"; target_slot="$(current_value ACTIVE_SLOT)"; [[ "$target_slot" == blue ]] && target_slot=green || target_slot=blue; target_port="$([[ "$target_slot" == blue ]] && echo "$BLUE_PORT" || echo "$GREEN_PORT")"
  COMPOSE_PROJECT="$target_project"; SLOT_PORT="$target_port"; compose up -d --force-recreate --remove-orphans --no-deps inference-service
  if ! wait_ready "$target_port"; then compose logs --tail=100 inference-service >&2 || true; compose stop inference-service || true; fail "Standby release failed healthcheck; active release was not changed"; fi
  old_project="$(current_value ACTIVE_PROJECT)"; old_dir="$(current_value ACTIVE_DIR)"; old_port="$(current_value ACTIVE_PORT)"
  if ! switch_upstream "$target_port"; then compose stop inference-service || true; fail "Rollback upstream switch failed; active release was not changed"; fi
  write_state "$target_slot" "$target_port" "$target_project" "$target_dir" "$old_project" "$old_dir"
  stop_release_service "$old_dir" "$old_project" "$old_port"
  release_log "Rollback complete"
}
status() { test -f "$STATE_FILE" || { echo ACTIVE_RELEASE=none; return; }; cat "$STATE_FILE"; local dir; dir="$(current_value ACTIVE_DIR)"; [[ -f "$dir/compose.yaml" ]] || return; load_bundle "$dir" legacy; SLOT_PORT="$(current_value ACTIVE_PORT)"; compose ps; }
main() {
  local command="${1:-}"; shift || true; require_root
  case "$command" in deploy|validate) [[ "${1:-}" == --release-dir && -n "${2:-}" ]] || usage; RELEASE_DIR="$2"; [[ "$command" == validate ]] && { validate_runtime; load_bundle "$RELEASE_DIR"; return; } ;; rollback|status) test "$#" -eq 0 || usage ;; *) usage ;; esac
  validate_runtime; exec 9>"${ETC_ROOT}/deploy.lock"; flock -n 9 || fail "Another deployment is already running."
  case "$command" in deploy) load_bundle "$RELEASE_DIR"; start_postgres "$RELEASE_DIR"; deploy ;; rollback) rollback ;; status) status ;; esac
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
