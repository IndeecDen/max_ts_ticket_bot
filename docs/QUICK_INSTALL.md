# Быстрая установка

## Требования

- Debian 13 или Ubuntu 24.04 с systemd;
- root/sudo;
- Python 3.12 или 3.13 устанавливается скриптом;
- доступ к APT, PyPI и MAX API;
- созданный и активированный MAX-бот с токеном;
- ID рабочего чата и MAX ID администратора.

## Установка из GitHub

```bash
git clone https://github.com/IndeecDen/max_ts_ticket_bot.git
cd max_ts_ticket_bot
sudo bash scripts/install.sh
```

Мастер запросит токен, рабочий чат, роли, таймаут, часовой пояс, локальный порт и секрет Webhook. Токен и секрет вводятся скрыто. Профиль создаётся в `/etc/max-ts-ticket-bot/config.json`, база — в `/var/lib/max-ts-ticket-bot/max_bot.db`.

После установки проверьте:

```bash
sudo systemctl status max-ts-ticket-bot.service
sudo journalctl -u max-ts-ticket-bot.service -n 100 --no-pager
```

## HTTPS и Webhook

Служба слушает только локальный адрес. Для боевой работы настройте домен, сертификат и reverse proxy Nginx на `/webhook/max`, затем выполните:

```bash
cd /opt/max-ts-ticket-bot
sudo .venv/bin/python -m app.setup_webhook check --profile /etc/max-ts-ticket-bot/config.json
sudo .venv/bin/python -m app.setup_webhook register --profile /etc/max-ts-ticket-bot/config.json
```

Подробная настройка Nginx, обновление, резервное копирование и восстановление описаны в [LINUX_INSTALL.md](LINUX_INSTALL.md) и [BACKUP_UPDATE.md](BACKUP_UPDATE.md).

## Обновление

Не запускайте первоначальный установщик для обновления. Получите новый проект и выполните:

```bash
sudo bash scripts/update.sh
```

Скрипт создаёт отдельное окружение, сохраняет проверенную копию текущего кода, базы, профиля и unit-файла, а затем проверяет `/readyz`.
