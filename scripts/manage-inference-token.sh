#!/usr/bin/env bash
# Creates/revokes service tokens stored as SHA-256 hashes in a host-local file.
set -euo pipefail

usage() {
  echo "usage: $0 <create|revoke|list> [predict|deploy|token-id] [token-file]" >&2
  exit 2
}

command="${1:-}"
argument="${2:-}"
token_file="${3:-/etc/ml-inference-service/tokens}"

case "$command" in
  create)
    [[ "$argument" == "predict" || "$argument" == "deploy" ]] || usage
    token_id="${argument}_$(date +%Y%m%d%H%M%S)"
    raw_token="mis_${argument}_$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    token_hash="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$raw_token")"
    umask 077
    install -d -m 0750 "$(dirname "$token_file")"
    touch "$token_file"
    chmod 0640 "$token_file"
    printf '%s %s %s\n' "$token_id" "$argument" "$token_hash" >> "$token_file"
    echo "Created token id: $token_id"
    echo "Role: $argument"
    echo "Token (copy now; it cannot be displayed again): $raw_token"
    ;;
  revoke)
    [[ -n "$argument" && -f "$token_file" ]] || usage
    temporary_file="$(mktemp "${token_file}.XXXXXX")"
    trap 'rm -f "$temporary_file"' EXIT
    awk -v id="$argument" '$1 != id { print }' "$token_file" > "$temporary_file"
    chmod 0640 "$temporary_file"
    mv "$temporary_file" "$token_file"
    trap - EXIT
    echo "Revoked token id (if present): $argument"
    ;;
  list)
    [[ -f "$token_file" ]] || exit 0
    awk '!/^#/ && NF == 3 { print $1, $2 }' "$token_file"
    ;;
  *) usage ;;
esac
