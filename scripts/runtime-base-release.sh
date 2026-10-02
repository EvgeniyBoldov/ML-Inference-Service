#!/usr/bin/env bash
# Compatibility entrypoint: dependency base and services form one release.
set -euo pipefail
case "${1:-}" in
  preview) mode=preview ;;
  prepare) mode=publish ;;
  *) echo 'Use make release-preview or make release; there is no separate base finalize.' >&2; exit 2 ;;
esac
exec "$(dirname "$0")/release.sh" "$mode" release.env
