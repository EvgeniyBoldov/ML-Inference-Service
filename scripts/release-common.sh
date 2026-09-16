#!/usr/bin/env bash

if [[ -z "${BASH_VERSION:-}" ]]; then
  echo "This helper is Bash-only." >&2
  return 1 2>/dev/null || exit 1
fi
set -Eeuo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_PHASE="startup"
RELEASE_PHASE_STARTED_AT=$SECONDS
release_log() { printf '[ml-inference-deploy %s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >&2; }
release_phase_start() { RELEASE_PHASE="$1"; RELEASE_PHASE_STARTED_AT=$SECONDS; release_log "START: $RELEASE_PHASE"; }
release_phase_end() { release_log "DONE: $RELEASE_PHASE (duration: $((SECONDS - RELEASE_PHASE_STARTED_AT))s)"; }
release_error_trap() { local status=$?; release_log "ERROR: phase=$RELEASE_PHASE status=$status location=${BASH_SOURCE[1]-unknown}:${BASH_LINENO[0]-unknown} command=$BASH_COMMAND"; }
release_exit_trap() { local status=$?; test "$status" -eq 0 || release_log "EXIT: phase=$RELEASE_PHASE status=$status"; }
trap release_error_trap ERR
trap release_exit_trap EXIT
fail() { release_log "ERROR: $*"; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "Required command is unavailable: $1"; }
load_release_file() {
  local file="$1"
  test -f "$file" || fail "Missing release manifest: $file"
  awk '/^[[:space:]]*($|#)/ { next } /^[A-Z][A-Z0-9_]*=[-A-Za-z0-9._:\/]*$/ { key=$0; sub(/=.*/, "", key); if (seen[key]++) exit 1; next } { exit 1 }' "$file" || fail "Release manifest contains invalid or duplicate entries: $file"
  set -a; source "$file"; set +a
  for key in RELEASE_VERSION RELEASE_COMMIT REGISTRY IMAGE BLUE_PORT GREEN_PORT; do [[ -n "${!key:-}" ]] || fail "$key is required in release.env"; done
  [[ "$RELEASE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "RELEASE_VERSION must be X.Y.Z"
  [[ "$RELEASE_COMMIT" =~ ^[0-9a-f]{7,64}$ ]] || fail "RELEASE_COMMIT must be a Git commit SHA"
  [[ "$BLUE_PORT" =~ ^[0-9]+$ && "$GREEN_PORT" =~ ^[0-9]+$ ]] || fail "BLUE_PORT and GREEN_PORT must be numeric"
}
require_clean_worktree() {
  git -C "$REPO_ROOT" diff --quiet || fail "Working tree has unstaged changes."
  git -C "$REPO_ROOT" diff --cached --quiet || fail "Working tree has staged changes."
  test -z "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal)" || fail "Working tree has untracked changes."
}
