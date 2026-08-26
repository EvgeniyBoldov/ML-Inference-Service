#!/usr/bin/env bash
set -euo pipefail

mode="${1:?usage: release.sh <preview|prepare> <release.env>}"
manifest="${2:?usage: release.sh <preview|prepare> <release.env>}"

die() { echo "release: $*" >&2; exit 1; }
value() { sed -n "s/^$1=//p" "$manifest" | tail -n 1; }

[[ -f "$manifest" ]] || die "manifest not found: $manifest"
version="$(value RELEASE_VERSION)"
previous_commit="$(value RELEASE_COMMIT)"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "RELEASE_VERSION must be X.Y.Z"
current_commit="$(git rev-parse --verify HEAD 2>/dev/null)" || die "a committed Git HEAD is required"
patch="${version##*.}"

source_changed=true
if [[ -n "$previous_commit" ]] && git cat-file -e "${previous_commit}^{commit}" 2>/dev/null; then
  if git diff --quiet "$previous_commit" HEAD -- . ':(exclude)release.env'; then
    source_changed=false
  fi
fi

# X.Y.0 is an explicit DevOps request for a new major/minor release, even when
# the source tree is unchanged. The published release will be X.Y.1.
manual_release=false
[[ "$patch" == "0" ]] && manual_release=true

next_version="${version%.*}.$((patch + 1))"
temp_file="$(mktemp)"
trap 'rm -f "$temp_file"' EXIT
awk -v version="$next_version" -v commit="$current_commit" '
  /^RELEASE_VERSION=/ { print "RELEASE_VERSION=" version; next }
  /^RELEASE_COMMIT=/ { print "RELEASE_COMMIT=" commit; next }
  { print }
' "$manifest" > "$temp_file"

if [[ "$mode" == "preview" ]]; then
  echo "Current release manifest:"
  cat "$manifest"
  echo
  if [[ "$source_changed" == false && "$manual_release" == false ]]; then
    echo "No source changes since RELEASE_COMMIT=$previous_commit; no release would be created."
    exit 0
  fi
  echo "Future release manifest:"
  cat "$temp_file"
  echo
  diff -u "$manifest" "$temp_file" || true
  exit 0
fi

[[ "$mode" == "prepare" ]] || die "unsupported mode: $mode"
if [[ "$source_changed" == false && "$manual_release" == false ]]; then
  die "no source changes since RELEASE_COMMIT=$previous_commit (use a X.Y.0 base for an explicit release)"
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  die "working tree has uncommitted changes; commit source changes before creating a release"
fi
mv "$temp_file" "$manifest"
trap - EXIT
echo "Prepared release $next_version for source commit $current_commit"
