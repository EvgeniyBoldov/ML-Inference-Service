#!/usr/bin/env bash
set -euo pipefail

source_file="${CI_PROJECT_DIR:-$(pwd)}/projects/model-runtime-base/base.env"
target_file="/etc/ml-inference-service/runtime-base.env"

[[ -f "$source_file" ]] || { echo "runtime-base config is missing" >&2; exit 1; }
image="$(sed -n 's/^RUNTIME_BASE_IMAGE=//p' "$source_file" | tail -n 1)"
[[ "$image" == *"@sha256:"* ]] || { echo "RUNTIME_BASE_IMAGE must be pinned by digest" >&2; exit 1; }
sudo install -d -m 0750 /etc/ml-inference-service
sudo install -m 0644 "$source_file" "$target_file"
echo "Installed runtime base manifest: $image"
