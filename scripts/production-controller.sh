#!/usr/bin/env bash

# Install as /usr/local/sbin/ml-inference-deploy, root:root, mode 0750.
set -Eeuo pipefail
CONFIG_FILE="/etc/ml-inference-service/controller.env"
log() { printf '[ml-inference-controller %s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }
test "$EUID" -eq 0 || fail "Controller must run as root"; test -r "$CONFIG_FILE" || fail "Missing controller configuration: $CONFIG_FILE"; test "$(stat -c '%U:%G:%a' "$CONFIG_FILE")" = "root:root:600" || fail "Controller configuration must be root:root mode 0600"; source "$CONFIG_FILE"
: "${APP_ROOT:=/opt/ml-inference-service}"; : "${ETC_ROOT:=/etc/ml-inference-service}"; : "${CI_BUILDS_ROOT:?CI_BUILDS_ROOT is required in controller.env}"
HELPER="${APP_ROOT}/current/scripts/deploy.sh"
usage() { echo "Usage: ml-inference-deploy deploy --source CI_CHECKOUT --release SHA | runtime-base --source CI_CHECKOUT --release SHA | rollback | status" >&2; exit 2; }
set_current() { local target="$1" tmp="${APP_ROOT}/.current.$$.new"; ln -s "$target" "$tmp"; mv -Tf "$tmp" "$APP_ROOT/current"; }
safe_source() { local source="$(realpath -e "$1")" root="$(realpath -e "$CI_BUILDS_ROOT")"; case "$source" in "$root"/*) printf '%s\n' "$source" ;; *) fail "CI source must be inside CI_BUILDS_ROOT" ;; esac; }
stage() {
  local source="$1" id="$2" dest="${APP_ROOT}/releases/${id}" tmp file
  if [[ -e "$dest" ]]; then [[ -f "$dest/.release-id" && "$(<"$dest/.release-id")" == "$id" ]] || fail "Invalid existing release: $dest"; echo "$dest"; return; fi
  for file in release.env infra/compose/inference-service.compose.yaml scripts/release-common.sh scripts/deploy.sh; do [[ -f "$source/$file" ]] || fail "Release bundle is missing $file"; done
  tmp="$(mktemp -d "${APP_ROOT}/releases/.${id}.XXXXXX")"; trap 'rm -rf "${tmp:-}"' RETURN
  install -D -o root -m 0640 "$source/release.env" "$tmp/release.env"; install -D -o root -m 0640 "$source/infra/compose/inference-service.compose.yaml" "$tmp/compose.yaml"; install -D -o root -m 0640 "$source/scripts/release-common.sh" "$tmp/scripts/release-common.sh"; install -D -o root -m 0750 "$source/scripts/deploy.sh" "$tmp/scripts/deploy.sh"; printf '%s\n' "$id" > "$tmp/.release-id"; chmod 0640 "$tmp/.release-id"; chmod 0750 "$tmp" "$tmp/scripts"; mv "$tmp" "$dest"; trap - RETURN; echo "$dest"
}
install_runtime_base() {
  local source="$1" manifest="$1/projects/model-runtime-base/base.env" image count
  [[ -f "$manifest" ]] || fail "Runtime base manifest is missing: $manifest"
  count="$(grep -c '^RUNTIME_BASE_IMAGE=' "$manifest" || true)"
  [[ "$count" == 1 ]] || fail "Runtime base manifest must contain exactly one RUNTIME_BASE_IMAGE"
  image="$(sed -n 's/^RUNTIME_BASE_IMAGE=//p' "$manifest")"
  [[ "$image" =~ ^[A-Za-z0-9._/:@-]+@sha256:[a-f0-9]{64}$ ]] || fail "RUNTIME_BASE_IMAGE must be pinned by a sha256 digest"
  install -D -o root -g root -m 0644 "$manifest" "$ETC_ROOT/runtime-base.env"
  log "Installed runtime base manifest: $image"
}
command="${1:-}"; shift || true; mkdir -p "$APP_ROOT/releases"
case "$command" in
  deploy) [[ "${1:-}" == --source && -n "${2:-}" && "${3:-}" == --release && -n "${4:-}" ]] || usage; [[ "$4" =~ ^[0-9a-f]{7,64}$ ]] || fail "Release ID must be a Git SHA"; dir="$(stage "$(safe_source "$2")" "$4")"; env ML_INFERENCE_APP_ROOT="$APP_ROOT" ML_INFERENCE_ETC_ROOT="$ETC_ROOT" bash "$dir/scripts/deploy.sh" deploy --release-dir "$dir"; set_current "$dir" ;;
  runtime-base) [[ "${1:-}" == --source && -n "${2:-}" && "${3:-}" == --release && -n "${4:-}" ]] || usage; [[ "$4" =~ ^[0-9a-f]{7,64}$ ]] || fail "Release ID must be a Git SHA"; install_runtime_base "$(safe_source "$2")" ;;
  rollback) test "$#" -eq 0 || usage; env ML_INFERENCE_APP_ROOT="$APP_ROOT" ML_INFERENCE_ETC_ROOT="$ETC_ROOT" bash "$HELPER" rollback; active_dir="$(sed -n 's/^ACTIVE_DIR=//p' "$ETC_ROOT/active-release.env" | tail -n 1)"; set_current "$active_dir" ;;
  status) test "$#" -eq 0 || usage; [[ -x "$HELPER" ]] || { echo ACTIVE_RELEASE=none; exit 0; }; exec env ML_INFERENCE_APP_ROOT="$APP_ROOT" ML_INFERENCE_ETC_ROOT="$ETC_ROOT" bash "$HELPER" status ;;
  *) usage;;
esac
