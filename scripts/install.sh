#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
trap 'echo "Установка не завершена. Исправьте ошибку и повторите запуск; данные не удалены." >&2' ERR
if [[ ${1:-} == --help ]]; then
    echo 'sudo bash scripts/install.sh — первоначальная установка службы MAX (Debian 13 / Ubuntu 24.04).'
    echo 'HTTPS и подписка MAX настраиваются отдельно. Для обновления: scripts/update.sh.'
    exit 0
fi
[[ $# == 0 ]] || { echo 'Неизвестные параметры.' >&2; exit 1; }
[[ $EUID == 0 ]] || { echo 'Запустите через sudo.' >&2; exit 1; }
[[ -d /run/systemd/system ]] || { echo 'Нужна система с работающим systemd.' >&2; exit 1; }
. /etc/os-release
case "$ID:$VERSION_ID" in
    debian:13|ubuntu:24.04) ;;
    *) echo 'Эта версия установщика рассчитана на Debian 13 и Ubuntu 24.04.' >&2; exit 1 ;;
esac
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
app_dir=/opt/max-ts-ticket-bot
config_dir=/etc/max-ts-ticket-bot
data_dir=/var/lib/max-ts-ticket-bot
unit=/etc/systemd/system/max-ts-ticket-bot.service
service_user=max-ts-ticket-bot
for path in "$app_dir" "$config_dir" "$data_dir" "$unit"; do
    [[ ! -L "$path" ]] || { echo 'Обнаружена ссылка вместо каталога или unit; установка остановлена.' >&2; exit 1; }
done
if [[ -f "$app_dir/.installation-complete" ]]; then
    echo 'MAX уже установлен. Повторный запуск не меняет код, настройки, роли и базу.'
    echo 'Для обновления запустите sudo bash scripts/update.sh из новой версии проекта.'
    systemctl is-active max-ts-ticket-bot.service
    bash "$source_dir/scripts/install-backup.sh"
    exit 0
fi
if [[ ! -f "$app_dir/.installer-owned" ]]; then
    for path in "$app_dir" "$config_dir" "$data_dir" "$unit"; do
        [[ ! -e "$path" ]] || { echo 'Целевой путь уже существует и не принадлежит этой установке.' >&2; exit 1; }
    done
    if getent passwd "$service_user" >/dev/null || getent group "$service_user" >/dev/null; then
        echo 'Учётная запись MAX уже существует вне этой установки.' >&2
        exit 1
    fi
    install -d -m 0755 "$app_dir"
    touch "$app_dir/.installer-owned"
fi
if systemctl is-active --quiet max-ts-ticket-bot.service; then
    echo 'Служба уже работает; код и настройки оставлены без изменений.'
    exit 0
fi
if [[ -e "$unit" ]] && ! grep -q '^# Managed by max-ts-ticket-bot installer$' "$unit"; then
    echo 'Существующий unit не принадлежит установщику.' >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv ca-certificates tzdata
python3 -c 'import sys; assert (3,12) <= sys.version_info[:2] < (3,14), "Нужен Python 3.12 или 3.13"'
if ! getent passwd "$service_user" >/dev/null; then
    useradd --system --user-group --home-dir "$data_dir" --no-create-home --shell /usr/sbin/nologin "$service_user"
fi
install -d -m 0750 -o root -g "$service_user" "$config_dir"
install -d -m 0700 -o "$service_user" -g "$service_user" "$data_dir"
# Resume dependency/configuration failures using the same copied application.
if [[ ! -f "$app_dir/.source-copied" ]]; then
    [[ -f "$source_dir/requirements.txt" && -f "$source_dir/app/main.py" ]] || { echo 'Запускайте установщик из полного проекта.' >&2; exit 1; }
    if [[ -n $(find "$source_dir/app" -type l -print -quit) ]]; then
        echo 'Ссылки внутри исходного app не поддерживаются.' >&2; exit 1
    fi
    tar -C "$source_dir" --exclude=__pycache__ --exclude='*.pyc' -cf - app requirements.txt deploy/max-ts-ticket-bot.service | tar -C "$app_dir" -xf -
    chmod -R go-w "$app_dir"
    find "$app_dir/app" -type d -exec chmod 0755 {} +
    find "$app_dir/app" -type f -exec chmod 0644 {} +
    chmod 0755 "$app_dir/deploy"
    chmod 0644 "$app_dir/requirements.txt" "$app_dir/deploy/max-ts-ticket-bot.service"
    touch "$app_dir/.source-copied"
fi
[[ -x "$app_dir/.venv/bin/python" ]] || python3 -m venv "$app_dir/.venv"
"$app_dir/.venv/bin/python" -m pip install --disable-pip-version-check -r "$app_dir/requirements.txt"
cd "$app_dir"
"$app_dir/.venv/bin/python" -m app.install_config "$config_dir/config.json"
chown root:"$service_user" "$config_dir/config.json"
chmod 0640 "$config_dir/config.json"
runuser -u "$service_user" -- "$app_dir/.venv/bin/python" -m app.main check-config --profile "$config_dir/config.json"
runuser -u "$service_user" -- "$app_dir/.venv/bin/python" -m app.main check-api --profile "$config_dir/config.json"
install -m 0644 "$app_dir/deploy/max-ts-ticket-bot.service" "$unit"
systemctl daemon-reload
systemctl enable --now max-ts-ticket-bot.service
"$app_dir/.venv/bin/python" - <<'PY'
import json, time, urllib.request
from app.service_profile import load_profile
settings, _ = load_profile('/etc/max-ts-ticket-bot/config.json')
url=f'http://127.0.0.1:{settings.listen_port}/readyz'
for attempt in range(30):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            result=json.load(response)
            if response.status == 200 and result.get('mode') == 'bot':
                break
    except (OSError, ValueError):
        pass
    time.sleep(1)
else:
    raise SystemExit('Служба не подтвердила готовность. Проверьте systemctl status и journalctl.')
PY
systemctl is-active --quiet max-ts-ticket-bot.service
touch "$app_dir/.installation-complete"
bash "$source_dir/scripts/install-backup.sh"
echo 'Служба установлена, автозапуск включён, локальная готовность подтверждена.'
echo 'Остались HTTPS/reverse proxy и проверка тестового события.'
echo 'После подготовки HTTPS: sudo -u max-ts-ticket-bot /opt/max-ts-ticket-bot/.venv/bin/python -m app.setup_webhook register --profile /etc/max-ts-ticket-bot/config.json'
echo 'Команду выполняйте из /opt/max-ts-ticket-bot; справка: python -m app.setup_webhook --help.'
echo 'Статус: sudo systemctl status max-ts-ticket-bot.service'
echo 'Журналы: sudo journalctl -u max-ts-ticket-bot.service -n 100 --no-pager'
echo 'Перезапуск: sudo systemctl restart max-ts-ticket-bot.service'
