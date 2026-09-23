"""Configuration checks, read-only API probe and durable event receiver."""
import argparse
import asyncio
import ssl
import logging
import sqlite3
import sys
import json
from pathlib import Path
from aiohttp import web

from app.config import ConfigError, load_settings, PROJECT_ROOT
from app.adapters.max.client import MaxClient, MaxAPIError
from app.storage.inbox import InboxSchemaError
from app.web.server import create_app
from app.storage.inbox import InboxStore
from app.services.processor import InboxProcessor, ProcessingPolicy
from app.services.delivery import DeliveryQueue
from app.services.recovery import QueueRecovery, RecoveryError
from app.storage.delivery_lock import DeliveryBusy
from app.services.roles import RoleRegistry, ManagementError
from app.services.preferences import Preferences
from app.services.statistics import Statistics, render_statistics


async def show_statistics(settings, args):
    if args.xlsx and args.json_output:
        raise ManagementError('Выберите --xlsx или --json-output, не оба варианта.')
    if bool(args.from_date) != bool(args.to_date):
        raise ManagementError('Для произвольного периода нужны --from-date и --to-date.')
    if args.period and args.from_date:
        raise ManagementError('Выберите --period или пару дат, не оба варианта.')
    parts = [args.from_date, args.to_date] if args.from_date else [args.period or 'day']
    store = InboxStore(settings.database_path)
    await store.initialize()
    report = await Statistics(store).read(parts, settings.timezone, args.user_id)
    if args.xlsx:
        from app.services.excel_export import export_statistics
        output = Path(args.xlsx)
        if not output.is_absolute():
            output = PROJECT_ROOT / output
        path = await asyncio.to_thread(export_statistics, report, output)
        print(f'Excel-отчёт сохранён: {path}')
        return 0
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json_output else '\n\n'.join(render_statistics(report)))
    return 0


async def manage_preferences(settings, args):
    store = InboxStore(settings.database_path)
    await store.initialize()
    preferences = Preferences(store)
    if args.command == 'timeout-set':
        await preferences.change(seconds=args.seconds)
    elif args.command in ('ignore-add', 'ignore-remove'):
        await preferences.change(words=args.word, add=args.command == 'ignore-add')
    print(json.dumps(await preferences.snapshot(), ensure_ascii=False, indent=2))
    return 0


async def manage_roles(settings, args):
    store = InboxStore(settings.database_path)
    await store.initialize()
    registry = RoleRegistry(store)
    if args.command == 'roles-list':
        print(json.dumps(await registry.list(), ensure_ascii=False, indent=2))
    else:
        changed = await registry.change(args.user_id, args.role, args.command == 'role-grant')
        print('Роль изменена.' if changed else 'Роль уже в указанном состоянии.')
        print('Настройки ролей сохранены в базе MAX; сеть не использовалась.')
    return 0


async def recover_queue(settings, args):
    store = InboxStore(settings.database_path)
    await store.initialize()
    recovery = QueueRecovery(store)
    if args.command == 'queue-inspect':
        print(json.dumps(await recovery.inspect(args.job_id), ensure_ascii=False, indent=2))
    else:
        state = await recovery.recover(args.job_id, 'retry' if args.command == 'queue-retry' else 'confirm',
            args.revision, reason=args.reason, mid=args.message_id, delivered_revision=args.delivered_revision,
            confirm_not_delivered=args.confirm_not_delivered)
        print(f'Задание {args.job_id}: {state}. Изменение записано в журнал восстановления; сеть не использовалась.')
    return 0


async def deliver_outbox(settings, status_only=False):
    if not status_only:
        settings.require_token()
    store = InboxStore(settings.database_path)
    await store.initialize()
    queue = DeliveryQueue(store)
    if not status_only:
        async with MaxClient(settings) as client:
            for _ in range(100):
                if await queue.deliver_one(client) is None:
                    break
    states = await queue.status()
    print('Очередь доставки:', states)
    if not status_only and states.get('pending'):
        print('Остались задания: повторите команду после задержки доставки.')
    return int(not status_only and any(states.get(s) for s in ('failed', 'uncertain', 'sending')))


async def process_inbox(settings, args):
    policy = ProcessingPolicy(frozenset(args.client_chat), frozenset(args.specialist),
                              settings.work_chat_id, args.timeout, frozenset(args.bot_admin), settings.timezone,
                              args.auto_client_chats or not args.client_chat)
    store = InboxStore(settings.database_path)
    await store.initialize()
    result = await InboxProcessor(store, policy).tick()
    print(f"Обработано сообщений: {result['processed']}; новых заявок: {result['promoted']}.")
    print('Есть ещё сообщения: повторите команду.' if result['more_messages'] else 'Пакет завершён.')
    print('Отправка в MAX не выполнялась. Для истёкших ожиданий повторите команду после таймаута.')
    return 0


async def check_api(settings):
    async with MaxClient(settings) as client:
        bot = await client.get_me()
        print(f"MAX API доступен. ID бота: {bot['user_id']}.")
        if settings.work_chat_id is not None:
            await client.get_chat(settings.work_chat_id)
            membership = await client.get_bot_membership(settings.work_chat_id)
            print('Рабочий чат доступен.')
            if membership.get('is_admin') is not True:
                print('Для групповых событий назначьте бота администратором рабочего чата.')
                return 1
            print('Бот имеет статус администратора рабочего чата.')
        else:
            print('WORK_CHAT_ID не задан: доступ к рабочему чату ещё не проверен.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='MAX-бот техподдержки: подготовка подключения')
    parser.add_argument('command', choices=('check-config', 'check-api', 'serve', 'run', 'process-inbox',
                                          'deliver-outbox', 'queue-status', 'queue-inspect', 'queue-retry', 'queue-confirm',
                                          'roles-list', 'role-grant', 'role-revoke',
                                          'settings-show', 'timeout-set', 'ignore-add', 'ignore-remove', 'stats'))
    parser.add_argument('--profile', type=Path, help='JSON-профиль службы вместо .env и флагов чатов/ролей')
    parser.add_argument('--client-chat', action='append', type=int, default=[])
    parser.add_argument('--auto-client-chats', action='store_true', help='Все групповые чаты, кроме рабочего; по умолчанию без --client-chat')
    parser.add_argument('--specialist', action='append', type=int, default=[])
    parser.add_argument('--bot-admin', action='append', type=int, default=[])
    parser.add_argument('--user-id', type=int)
    parser.add_argument('--role', choices=('specialist', 'admin'))
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--seconds', type=int)
    parser.add_argument('--word', action='append', default=[])
    parser.add_argument('--period', choices=('day', 'week', 'month'))
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--json-output', action='store_true')
    parser.add_argument('--xlsx', metavar='PATH')
    parser.add_argument('--job-id', type=int)
    parser.add_argument('--revision', type=int)
    parser.add_argument('--delivered-revision', type=int)
    parser.add_argument('--message-id')
    parser.add_argument('--reason')
    parser.add_argument('--confirm-not-delivered', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.profile:
            if args.client_chat or args.auto_client_chats or args.specialist or args.bot_admin or args.timeout != 300:
                raise ConfigError('Не сочетайте --profile с флагами чатов, начальных ролей и таймаута.')
            from app.service_profile import load_profile
            settings, saved_policy = load_profile(args.profile)
            args.client_chat = list(saved_policy.client_chats)
            args.auto_client_chats = saved_policy.auto_client_chats
            args.specialist = list(saved_policy.specialists)
            args.bot_admin = list(saved_policy.admins)
            args.timeout = saved_policy.timeout_seconds
        else:
            settings = load_settings()
        if args.command == 'stats':
            return asyncio.run(show_statistics(settings, args))
        if args.command in ('settings-show', 'timeout-set', 'ignore-add', 'ignore-remove'):
            return asyncio.run(manage_preferences(settings, args))
        if args.command in ('roles-list', 'role-grant', 'role-revoke'):
            return asyncio.run(manage_roles(settings, args))
        if args.command in ('queue-inspect', 'queue-retry', 'queue-confirm'):
            return asyncio.run(recover_queue(settings, args))
        if args.command in ('deliver-outbox', 'queue-status'):
            return asyncio.run(deliver_outbox(settings, args.command == 'queue-status'))
        if args.command == 'process-inbox':
            return asyncio.run(process_inbox(settings, args))
        if args.command in ('serve', 'run'):
            policy = (ProcessingPolicy(frozenset(args.client_chat), frozenset(args.specialist),
                                       settings.work_chat_id, args.timeout, frozenset(args.bot_admin), settings.timezone,
                                       args.auto_client_chats or not args.client_chat) if args.command == 'run' else None)
            application = create_app(settings, policy=policy)
            logging.basicConfig(level=settings.log_level,
                                format='%(asctime)s %(levelname)s %(name)s: %(message)s')
            print('Режим bot: приём, обработка и отправка карточек включены.' if policy else
                  'Режим collect_only: события сохраняются, ответы и заявки пока не создаются.')
            # Python 3.12 Proactor TCP shutdown raises WinError 10022 on this
            # Windows runtime. This HTTP-only receiver can use Selector safely.
            loop = asyncio.SelectorEventLoop() if sys.platform == 'win32' else None
            web.run_app(application, host=settings.listen_host, port=settings.listen_port,
                        access_log=None, loop=loop)
            return 0
        if args.command == 'check-config':
            # Validate a configured trust bundle without any network requests.
            if settings.ca_bundle:
                ssl.create_default_context().load_verify_locations(cafile=str(settings.ca_bundle))
            print('Конфигурация корректна.')
            print('Токен: задан.' if settings.token else 'Токен: не задан; нужен для команд обращения к MAX API.')
            print('Рабочий чат: задан.' if settings.work_chat_id is not None else 'Рабочий чат: не задан.')
            print(f'База MAX: {settings.database_path}')
            print('Подключение к сети и создание базы не выполнялись.')
            return 0
        return asyncio.run(check_api(settings))
    except (ConfigError, MaxAPIError, InboxSchemaError, RecoveryError, DeliveryBusy, ManagementError) as exc:
        print(f'Ошибка: {exc}')
        return 1
    except (ssl.SSLError, OSError, sqlite3.Error):
        print('Ошибка доступа к конфигурации, сертификатам, базе данных или сети.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
