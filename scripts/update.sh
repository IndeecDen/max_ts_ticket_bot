#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
if [[ ${1:-} == --help ]]; then
    echo 'sudo bash scripts/update.sh — обновление установленной службы MAX из текущего проекта.'
    echo 'Сначала остановите все ручные обработчики. Старая версия и резервная копия сохраняются.'
    exit 0
fi
[[ $# == 0 && $EUID == 0 ]] || { echo 'Запустите через sudo без параметров.' >&2; exit 1; }
[[ -d /run/systemd/system ]] || { echo 'Нужен systemd.' >&2; exit 1; }
. /etc/os-release
case "$ID:$VERSION_ID" in
    debian:13|ubuntu:24.04) ;;
    *) echo 'Поддерживаются Debian 13 и Ubuntu 24.04.' >&2; exit 1 ;;
esac
exec 9>/run/lock/max-ts-ticket-bot-update.lock
flock -n 9 || { echo 'Другое обновление уже выполняется.' >&2; exit 1; }
base=/opt/max-ts-ticket-bot
[[ -f "$base/.installation-complete" ]] || { echo 'Сначала завершите первоначальную установку.' >&2; exit 1; }
for path in "$base" "$base/releases" /var/backups/max-ts-ticket-bot; do
    [[ ! -L "$path" ]] || { echo 'Ссылки в целевых путях запрещены.' >&2; exit 1; }
done
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -f "$source_dir/app/service_update.py" && -f "$source_dir/requirements.txt" ]] || exit 1
for path in "$source_dir/app" "$source_dir/requirements.txt" "$source_dir/deploy"; do
    [[ -z $(find "$path" -type l -print -quit) ]] || { echo 'Ссылки в исходниках запрещены.' >&2; exit 1; }
done
install -d -m 0755 "$base/releases"
install -d -m 0700 /var/backups/max-ts-ticket-bot
release=$(mktemp -d "$base/releases/release_XXXXXXXX")
chmod 0755 "$release"
backup="/var/backups/max-ts-ticket-bot/$(basename "$release")"
trap 'echo "Обновление прервано. Версия: $release. Копия: $backup. Проверьте status/journalctl; повторный запуск создаст новую версию." >&2' ERR
tar -C "$source_dir" --exclude=__pycache__ --exclude='*.pyc' -cf - app requirements.txt deploy/max-ts-ticket-bot.service | tar -C "$release" -xf -
chown -R root:root "$release"
find "$release" -type d -exec chmod 0755 {} +
find "$release" -type f -exec chmod 0644 {} +
python3 -c 'import sys; assert (3,12) <= sys.version_info[:2] < (3,14), "Нужен Python 3.12 или 3.13"'
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install --disable-pip-version-check -r "$release/requirements.txt"
cd "$release"
"$release/.venv/bin/python" -m app.service_update --backup "$backup"
