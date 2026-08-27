#!/usr/bin/env bash
set -euo pipefail

manifest="projects/model-runtime-base/base.env"
source_dir="projects/model-runtime-base"
mode="${1:?usage: runtime-base-release.sh <preview|prepare|finalize>}"

die() { echo "runtime-base: $*" >&2; exit 1; }
value() { sed -n "s/^$1=//p" "$manifest" | tail -n 1; }
set_value() {
  temporary_file="$(mktemp)"
  awk -v key="$1" -v value="$2" 'index($0, key "=") == 1 { print key "=" value; next } { print }' "$manifest" > "$temporary_file"
  mv "$temporary_file" "$manifest"
}

version="$(value RUNTIME_BASE_VERSION)"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "RUNTIME_BASE_VERSION must be X.Y.Z"
input_hash="$(shasum -a 256 "$source_dir/Dockerfile" "$source_dir/requirements.txt" "$source_dir/runner.py" | shasum -a 256 | awk '{print $1}')"

case "$mode" in
  preview)
    echo "current version: $version"
    echo "recorded input hash: $(value RUNTIME_BASE_INPUT_SHA256)"
    echo "current input hash:  $input_hash"
    ;;
  prepare)
    if [[ "$(value RUNTIME_BASE_INPUT_SHA256)" == "$input_hash" ]]; then
      die "runtime base inputs did not change"
    fi
    patch="${version##*.}"
    next_version="${version%.*}.$((patch + 1))"
    set_value RUNTIME_BASE_VERSION "$next_version"
    set_value RUNTIME_BASE_INPUT_SHA256 "$input_hash"
    echo "prepared runtime base $next_version"
    ;;
  finalize)
    registry="$(value RUNTIME_BASE_REGISTRY)"
    image_name="$(value RUNTIME_BASE_IMAGE_NAME)"
    version="$(value RUNTIME_BASE_VERSION)"
    digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$registry/$image_name:$version")"
    [[ "$digest" == *"@sha256:"* ]] || die "unable to resolve pushed image digest"
    set_value RUNTIME_BASE_IMAGE "$digest"
    echo "recorded runtime image $digest"
    ;;
  *) die "unsupported mode: $mode" ;;
esac
