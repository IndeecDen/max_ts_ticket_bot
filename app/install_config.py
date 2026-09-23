"""Interactive first-install configuration; existing profiles are never overwritten."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys
from app.config import ConfigError
from app.service_profile import validate, load_profile


def collect(ask=input, secret=getpass.getpass):
    def value(prompt, default=''):
        return ask(prompt + (f' [{default}]' if default else '') + ': ').strip() or default
    def ids(prompt, required=False):
        raw=value(prompt)
        try:
            result=[int(x.strip()) for x in raw.split(',') if x.strip()]
        except ValueError:
            raise ConfigError('ID должны быть целыми числами через запятую.') from None
        if required and not result:
            raise ConfigError('Нужно указать хотя бы один ID.')
        return result
    token=secret('MAX-токен (ввод скрыт): ').strip()
    work=value('ID рабочего чата')
    clients=ids('ID клиентских чатов через запятую',True)
    admins=ids('MAX ID начальных администраторов через запятую',True)
    specialists=ids('MAX ID начальных специалистов (можно пусто)')
    tz=value('Часовой пояс','Europe/Moscow')
    timeout=value('Ожидание ответа специалиста, секунд','300')
    port=value('Локальный HTTP-порт','8080')
    api_timeout=value('Таймаут запросов MAX, секунд','15')
    ca=value('Абсолютный путь к дополнительному CA PEM (можно пусто)')
    webhook=secret('Секрет Webhook (Enter — сгенерировать): ').strip() or secrets.token_urlsafe(32)
    public=value('Публичный HTTPS URL Webhook (можно настроить позже)')
    try:
        seconds=int(timeout)
    except ValueError:
        raise ConfigError('Ожидание должно быть целым числом секунд.') from None
    if ca and not Path(ca).is_absolute():
        raise ConfigError('Путь CA должен быть абсолютным.')
    profile={'version':1,'environment':{'MAX_BOT_TOKEN':token,'WORK_CHAT_ID':work,
        'WEBHOOK_SECRET':webhook,'TIMEZONE':tz,'DATABASE_PATH':'/var/lib/max-ts-ticket-bot/max_bot.db',
        'LISTEN_HOST':'127.0.0.1','LISTEN_PORT':port,'HTTP_TIMEOUT_SECONDS':api_timeout,
        'MAX_API_BASE_URL':'https://platform-api2.max.ru','MAX_CA_BUNDLE':ca,'LOG_LEVEL':'INFO'},
        'policy':{'client_chats':clients,'admins':admins,'specialists':specialists,'timeout_seconds':seconds},
        'public_webhook_url':public}
    validate(profile)
    return profile


def save_new(path, profile):
    validate(profile)
    path=Path(path)
    content=json.dumps(profile,ensure_ascii=False,indent=2)+'\n'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('path',type=Path)
    args=parser.parse_args()
    try:
        if args.path.exists():
            load_profile(args.path)
            print('Существующий профиль проверен и сохранён без изменений.')
            return 0
        if not sys.stdin.isatty():
            raise ConfigError('Для ввода настроек нужен интерактивный терминал.')
        while True:
            try:
                profile=collect()
                break
            except ConfigError as exc:
                print(f'Ошибка настройки: {exc} Повторите ввод; файл ещё не создан.')
        save_new(args.path,profile)
        print('Профиль сохранён. Секреты не выводятся в терминал.')
        return 0
    except (ConfigError,OSError,EOFError,KeyboardInterrupt):
        print('Настройка не завершена. Проверьте профиль и права доступа; служба не запускается.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
