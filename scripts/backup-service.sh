#!/usr/bin/env bash
# Managed by max-ts-ticket-bot backup installer
set -Eeuo pipefail
umask 077
if [[ ${1:-} == --help ]]; then
    echo 'Создаёт проверенную копию текущей версии службы; вызывается таймером systemd.'
    exit 0
fi
[[ $# == 0 && $EUID == 0 ]] || exit 1
# Share the update lock so code, unit and database belong to the same release.
exec 9>/run/lock/max-ts-ticket-bot-update.lock
flock -w 60 9 || { echo 'Копия не создана: выполняется обновление. Повторите запуск позже.' >&2; exit 1; }
service=max-ts-ticket-bot.service
unit=/etc/systemd/system/$service
[[ $(systemctl show "$service" --property=FragmentPath --value) == "$unit" ]] || exit 1
[[ -z $(systemctl show "$service" --property=DropInPaths --value) ]] || exit 1
release=$(systemctl show "$service" --property=WorkingDirectory --value)
[[ "$release" == /opt/max-ts-ticket-bot || "$release" =~ ^/opt/max-ts-ticket-bot/releases/[A-Za-z0-9_-]+$ ]] || exit 1
for path in /opt/max-ts-ticket-bot /opt/max-ts-ticket-bot/releases "$release" "$unit" /etc/max-ts-ticket-bot/config.json /var/backups/max-ts-ticket-bot; do
    [[ ! -L "$path" ]] || exit 1
done
[[ -f "$release/app/backup.py" ]] || exit 1
cd "$release"
backup="/var/backups/max-ts-ticket-bot/scheduled_$(date -u +%Y%m%dT%H%M%SZ)_${RANDOM}"
"$release/.venv/bin/python" -m app.backup create --profile /etc/max-ts-ticket-bot/config.json --unit "$unit" --output "$backup"
echo "Проверенная копия создана: $backup"
