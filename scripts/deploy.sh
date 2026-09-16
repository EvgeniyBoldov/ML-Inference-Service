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
DEPLOY_WAIT_TIMEOUT="${DEPLOY_WAIT_TIMEOUT:-180}"
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
compose() { SLOT_PORT="${SLOT_PORT:-1}" docker compose --project-name "$COMPOSE_PROJECT" --env-file "$RUNTIME_ENV" --env-file "$RELEASE_DIR/release.env" -f "$RELEASE_DIR/compose.yaml" "$@"; }
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
  RELEASE_DIR="$dir"; load_release_file "$dir/release.env"; COMPOSE_PROJECT="ml-inference-${RELEASE_VERSION//./-}"; compose config --quiet
}
start_postgres() {
  RELEASE_DIR="$1"; COMPOSE_PROJECT=ml-inference-postgres; SLOT_PORT=1; compose up -d postgres
  for _ in $(seq 1 36); do compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 && return; sleep 2; done
  fail "PostgreSQL is not ready"
}
switch_upstream() {
  local port="$1" tmp; tmp="$(mktemp)"; trap 'rm -f "${tmp:-}"' RETURN
  cat > "$tmp" <<EOF
upstream ml_inference_service {
    server 127.0.0.1:${port};
    keepalive 32;
}
EOF
  install -m 0644 "$tmp" "$NGINX_UPSTREAM"; nginx -t; systemctl reload nginx; trap - RETURN; rm -f "$tmp"
}
wait_ready() { local port="$1"; for _ in $(seq 1 "$DEPLOY_WAIT_TIMEOUT"); do curl --fail --silent --show-error "http://127.0.0.1:${port}/health/ready" >/dev/null && return; sleep 1; done; return 1; }
write_state() {
  local slot="$1" port="$2" project="$3" dir="$4" standby_project="$5" standby_dir="$6" tmp
  tmp="$(mktemp "${ETC_ROOT}/.active-release.XXXXXX")"
  printf 'ACTIVE_SLOT=%s\nACTIVE_PORT=%s\nACTIVE_PROJECT=%s\nACTIVE_DIR=%s\nRELEASE_VERSION=%s\nRELEASE_COMMIT=%s\nSTANDBY_PROJECT=%s\nSTANDBY_DIR=%s\n' "$slot" "$port" "$project" "$dir" "$RELEASE_VERSION" "$RELEASE_COMMIT" "$standby_project" "$standby_dir" > "$tmp"
  install -m 0640 "$tmp" "$STATE_FILE"; rm -f "$tmp"
}
deploy() {
  local old_slot old_project old_dir standby_project standby_dir candidate_dir candidate_slot candidate_port project
  load_bundle "$RELEASE_DIR"; old_slot="$(current_value ACTIVE_SLOT)"; old_project="$(current_value ACTIVE_PROJECT)"; old_dir="$(current_value ACTIVE_DIR)"; standby_project="$(current_value STANDBY_PROJECT)"; standby_dir="$(current_value STANDBY_DIR)"
  candidate_dir="$RELEASE_DIR"
  if [[ "$old_slot" == blue ]]; then candidate_slot=green; candidate_port="$GREEN_PORT"; else candidate_slot=blue; candidate_port="$BLUE_PORT"; fi
  project="ml-inference-${RELEASE_VERSION//./-}"
  if [[ -n "$standby_project" && -n "$standby_dir" && -f "$standby_dir/compose.yaml" ]]; then COMPOSE_PROJECT="$standby_project"; RELEASE_DIR="$standby_dir"; SLOT_PORT="$candidate_port" compose down --remove-orphans || true; fi
  # Re-select the immutable candidate after reclaiming the old standby.
  load_bundle "$candidate_dir"
  install -d -m 0750 "$RELEASE_DIR"; COMPOSE_PROJECT="$project"; SLOT_PORT="$candidate_port"
  compose pull; compose run --rm --no-deps inference-service alembic upgrade head; compose up -d --remove-orphans --no-deps inference-service
  if ! wait_ready "$candidate_port"; then compose logs --tail=100 inference-service >&2 || true; fail "Candidate healthcheck failed; active release was not changed"; fi
  switch_upstream "$candidate_port"; write_state "$candidate_slot" "$candidate_port" "$project" "$RELEASE_DIR" "$old_project" "$old_dir"
  release_log "Deployment complete: release=$RELEASE_VERSION source=$RELEASE_COMMIT slot=$candidate_slot"
}
rollback() {
  local target_dir target_project target_slot target_port old_project old_dir
  target_dir="$(current_value STANDBY_DIR)"; test -n "$target_dir" || fail "No standby release is available for rollback."
  load_bundle "$target_dir"; target_project="$(current_value STANDBY_PROJECT)"; target_slot="$(current_value ACTIVE_SLOT)"; [[ "$target_slot" == blue ]] && target_slot=green || target_slot=blue; target_port="$([[ "$target_slot" == blue ]] && echo "$BLUE_PORT" || echo "$GREEN_PORT")"
  COMPOSE_PROJECT="$target_project"; SLOT_PORT="$target_port"; compose up -d --remove-orphans --no-deps inference-service; wait_ready "$target_port" || fail "Standby release failed healthcheck; active release was not changed"
  old_project="$(current_value ACTIVE_PROJECT)"; old_dir="$(current_value ACTIVE_DIR)"; switch_upstream "$target_port"; write_state "$target_slot" "$target_port" "$target_project" "$target_dir" "$old_project" "$old_dir"; release_log "Rollback complete"
}
status() { test -f "$STATE_FILE" || { echo ACTIVE_RELEASE=none; return; }; cat "$STATE_FILE"; local dir; dir="$(current_value ACTIVE_DIR)"; [[ -f "$dir/compose.yaml" ]] || return; load_bundle "$dir"; compose ps; }
main() {
  local command="${1:-}"; shift || true; require_root
  case "$command" in deploy|validate) [[ "${1:-}" == --release-dir && -n "${2:-}" ]] || usage; RELEASE_DIR="$2"; [[ "$command" == validate ]] && { validate_runtime; load_bundle "$RELEASE_DIR"; return; } ;; rollback|status) test "$#" -eq 0 || usage ;; *) usage ;; esac
  validate_runtime; exec 9>"${ETC_ROOT}/deploy.lock"; flock -n 9 || fail "Another deployment is already running."
  case "$command" in deploy) start_postgres "$RELEASE_DIR"; deploy ;; rollback) rollback ;; status) status ;; esac
}
main "$@"
