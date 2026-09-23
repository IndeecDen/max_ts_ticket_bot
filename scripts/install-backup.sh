#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${1:-} == --help ]]; then
    echo 'sudo bash scripts/install-backup.sh — проверка первой копии и включение ежедневного backup.'
    exit 0
fi
[[ $# == 0 && $EUID == 0 && -d /run/systemd/system ]] || { echo 'Нужны sudo и systemd.' >&2; exit 1; }
[[ -f /opt/max-ts-ticket-bot/.installation-complete ]] || { echo 'Сначала завершите установку бота.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
runner=/usr/local/libexec/max-ts-ticket-bot-backup
service=/etc/systemd/system/max-ts-ticket-bot-backup.service
timer=/etc/systemd/system/max-ts-ticket-bot-backup.timer
for path in /usr/local/libexec /var/backups/max-ts-ticket-bot "$runner" "$service" "$timer"; do
    [[ ! -L "$path" ]] || { echo 'Ссылка в целевом пути.' >&2; exit 1; }
done
for path in "$runner" "$service" "$timer"; do
    if [[ -e "$path" ]] && ! grep -q '^# Managed by max-ts-ticket-bot backup installer$' "$path"; then
        echo 'Целевой файл не принадлежит установщику backup.' >&2; exit 1
    fi
done
install -d -m 0755 /usr/local/libexec
install -d -m 0700 /var/backups/max-ts-ticket-bot
install -m 0755 "$source_dir/scripts/backup-service.sh" "$runner"
install -m 0644 "$source_dir/deploy/max-ts-ticket-bot-backup.service" "$service"
install -m 0644 "$source_dir/deploy/max-ts-ticket-bot-backup.timer" "$timer"
systemctl daemon-reload
systemctl start max-ts-ticket-bot-backup.service
systemctl enable --now max-ts-ticket-bot-backup.timer
echo 'Первая копия проверена. Ежедневный таймер включён; старые копии сохраняются.'
