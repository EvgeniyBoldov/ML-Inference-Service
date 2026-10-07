#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/release-common.sh"
cd "$REPO_ROOT"
mode="${1:-publish}"
manifest="${2:-release.env}"
load_release_file "$manifest" bootstrap
current_commit="$(git rev-parse --verify HEAD)"
base_sha="$(base_input_sha)"
base_version="${BASE_VERSION:-0.1.0}"
[[ "$base_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "BASE_VERSION must be X.Y.Z"
base_changed=false
[[ "$base_sha" == "${BASE_INPUT_SHA256:-}" ]] || base_changed=true
next_version="${RELEASE_VERSION%.*}.$((${RELEASE_VERSION##*.} + 1))"
next_base_version="$base_version"
if [[ "$base_changed" == true ]]; then next_base_version="${base_version%.*}.$((${base_version##*.} + 1))"; fi
source_changed=true
if [[ -n "${RELEASE_COMMIT:-}" ]] && git cat-file -e "${RELEASE_COMMIT}^{commit}" 2>/dev/null; then
  if git diff --quiet "$RELEASE_COMMIT" HEAD -- . ":(exclude)$manifest"; then source_changed=false; fi
fi
if [[ "$mode" == preview ]]; then
  printf 'Release: %s -> %s\nBase: %s -> %s\nStored base SHA: %s\nCurrent base SHA: %s\nRebuild base: %s\nSource changes: %s\n' "$RELEASE_VERSION" "$next_version" "$base_version" "$next_base_version" "${BASE_INPUT_SHA256:-none}" "$base_sha" "$base_changed" "$source_changed"
  exit 0
fi
[[ "$mode" == publish ]] || fail "Usage: release.sh <preview|publish> [release.env]"
require_command docker; require_command git
require_clean_worktree
[[ "$source_changed" == true || "$base_changed" == true || "${RELEASE_VERSION##*.}" == 0 ]] || fail "No changes since the published release"
branch="$(git symbolic-ref --quiet --short HEAD)" || fail "Release requires a branch"
remote="$(git config "branch.$branch.remote")" || fail "Configure an upstream branch before releasing"
merge_ref="$(git config "branch.$branch.merge")" || fail "Configure an upstream branch before releasing"
[[ "$remote" != . && "$merge_ref" == refs/heads/* ]] || fail "Release requires a remote upstream branch"
release_phase_start "verify Git upstream"
git fetch "$remote" "$merge_ref"
git merge-base --is-ancestor '@{upstream}' HEAD || fail "Local branch is behind upstream; merge it before releasing"
upstream_commit="$(git rev-parse '@{upstream}')"
release_phase_end
api_ref="$REGISTRY/$IMAGE:$next_version"
base_repository="$REGISTRY/$IMAGE-base"
base_ref="$base_repository:$next_base_version"
for image in "$api_ref"; do
  if docker manifest inspect "$image" >/dev/null 2>&1; then fail "Refusing to overwrite published release image: $image"; fi
done
pushed_digest() {
  local image="$1" repository="$2" digest
  while IFS= read -r digest; do
    if [[ "$digest" == "$repository"@sha256:* && "${digest##*@sha256:}" =~ ^[a-f0-9]{64}$ ]]; then printf '%s\n' "$digest"; return; fi
  done < <(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image")
  fail "Unable to resolve pushed digest: $image"
}
release_phase_start "prepare dependency base"
if [[ "$base_changed" == true ]]; then
  if docker manifest inspect "$base_ref" >/dev/null 2>&1; then
    # A previous attempt may have published the base before a service build failed.
    docker pull "$base_ref"
    published_sha="$(docker image inspect --format '{{index .Config.Labels "com.ml-inference.runtime-input-sha256"}}' "$base_ref")"
    published_version="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$base_ref")"
    [[ "$published_sha" == "$base_sha" && "$published_version" == "$next_base_version" ]] || fail "Published base version has different inputs: $base_ref"
    release_log "Reusing already published base with matching inputs: $base_ref"
  else
    docker build --pull --build-arg RUNTIME_BASE_VERSION="$next_base_version" --build-arg RUNTIME_BASE_INPUT_SHA256="$base_sha" --tag "$base_ref" projects/model-runtime-base
    docker push "$base_ref"
  fi
  base_image="$(pushed_digest "$base_ref" "$base_repository")"
else
  base_image="${BASE_IMAGE:-}"
  [[ "$base_image" =~ @sha256:[a-f0-9]{64}$ ]] || fail "Unchanged base has no pinned BASE_IMAGE"
  docker pull "$base_image"
fi
release_phase_end
release_phase_start "build inference service"
docker build --build-arg BASE_IMAGE="$base_image" --build-arg RELEASE_VERSION="$next_version" --build-arg SOURCE_COMMIT="$current_commit" --tag "$api_ref" --tag "$REGISTRY/$IMAGE:$current_commit" --file apps/inference-service/Dockerfile apps/inference-service
release_phase_end
release_phase_start "verify release dependencies and migrations"
docker run --rm --entrypoint python "$api_ref" -c 'import boto3; import mlflow; import asyncpg; import alembic'
heads="$(docker run --rm --entrypoint alembic "$api_ref" heads)"
db_revision="$(awk '/\(head\)/ {print $1}' <<< "$heads")"
[[ "$db_revision" =~ ^[A-Za-z0-9_]+$ ]] || fail "Release image must contain exactly one Alembic head"
release_phase_end
release_phase_start "push release images"
docker push "$api_ref"
docker push "$REGISTRY/$IMAGE:$current_commit"
release_phase_end
release_phase_start "verify source and upstream did not change"
require_clean_worktree
[[ "$(git rev-parse HEAD)" == "$current_commit" && "$(base_input_sha)" == "$base_sha" ]] || fail "Source changed during build; no release manifest was committed"
git fetch "$remote" "$merge_ref"
[[ "$(git rev-parse '@{upstream}')" == "$upstream_commit" ]] || fail "Upstream changed during build; no release manifest was committed"
release_phase_end
release_phase_start "write, commit and push release manifest"
tmp="$(mktemp "${manifest}.XXXXXX")"
trap 'rm -f "${tmp:-}"' EXIT
printf '%s\n' '# Immutable release state. Generated by make release; contains no secrets.' "RELEASE_VERSION=$next_version" "RELEASE_COMMIT=$current_commit" "REGISTRY=$REGISTRY" "IMAGE=$IMAGE" "BASE_VERSION=$next_base_version" "BASE_INPUT_SHA256=$base_sha" "BASE_IMAGE=$base_image" "DB_REVISION=$db_revision" "BLUE_PORT=$BLUE_PORT" "GREEN_PORT=$GREEN_PORT" > "$tmp"
mv "$tmp" "$manifest"
trap release_exit_trap EXIT
git add -- "$manifest"
git commit -m "release: $next_version"
git push "$remote" "HEAD:$merge_ref" || fail "Release commit was created; retry git push without rebuilding"
release_phase_end
release_log "Published release=$next_version base=$next_base_version; GitLab deployment is triggered by the manifest commit"
