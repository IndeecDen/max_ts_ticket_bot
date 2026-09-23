#!/usr/bin/env bash
# Read-only checks of repository templates. Does not install or start services.
set -Eeuo pipefail
if [[ ${1:-} == --help ]]; then
    echo 'bash scripts/check-deploy.sh — проверка Bash и шаблонов systemd на Linux.'
    exit 0
fi
[[ $# == 0 ]] || exit 1
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for script in "$source_dir"/scripts/*.sh; do
    bash -n "$script"
    bash "$script" --help
done
command -v systemd-analyze >/dev/null || { echo 'Для проверки unit нужен systemd-analyze.' >&2; exit 1; }
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
# Installed Python/runner paths do not exist on CI. Substitute only ExecStart
# in temporary copies; verify the other directives and timer dependency intact.
for unit in "$source_dir"/deploy/*.service "$source_dir"/deploy/*.timer; do
    sed 's|^ExecStart=.*|ExecStart=/usr/bin/true|' "$unit" > "$scratch/$(basename "$unit")"
done
systemd-analyze verify --man=no "$scratch"/*.service "$scratch"/*.timer
