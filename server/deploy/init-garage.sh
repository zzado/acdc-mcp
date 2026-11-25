#!/bin/sh
set -eu

garage() { /garage "$@"; }
node=$(garage node id 2>/dev/null | awk -F@ 'END {print substr($1, 1, 16)}')
test -n "$node"
if ! garage layout show | awk -v node="$node" '/^Zone redundancy/ {exit} $1 == node {found=1} END {exit !found}'; then
    garage layout assign -z local -c "${GARAGE_CAPACITY:-300GB}" "$node"
    garage layout apply --version 1
fi

ensure_bucket() {
    garage bucket info "$1" >/dev/null 2>&1 || garage bucket create "$1"
}

ensure_key() {
    name=$1
    access=$(tr -d '\r\n' < "/run/team-secrets/$name.id")
    material=$(tr -d '\r\n' < "/run/team-secrets/$name.secret")
    if ! garage key info "$access" >/dev/null 2>&1; then
        if ! garage key import --yes -n "$name" "$access" "$material" >/dev/null 2>&1; then
            printf 'Could not import %s key\n' "$name" >&2
            exit 1
        fi
    fi
    unset material
}

ensure_bucket team-staging
ensure_bucket team-final
ensure_key staging
ensure_key final
ensure_key worker
garage bucket allow --write team-staging --key staging
garage bucket allow --read team-final --key final
garage bucket allow --read --write team-staging --key worker
garage bucket allow --read --write team-final --key worker
garage bucket cleanup-incomplete-uploads --older-than 7d team-staging >/dev/null
