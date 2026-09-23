"""Local transactional processing; deliberately no outgoing MAX operations."""
import asyncio
import json
import time
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import ConfigError
from app.services.delivery import enqueue_cards, clip
from app.services.roles import bootstrap_roles, has_role, change_role, reassign, ManagementError
from app.services.preferences import bootstrap_preferences, get_timeout, set_timeout, change_words, is_ignored
from app.services import daily_digest, notices, autoclean, navigation, request_view, admin_requests
from app.services.reminders import configure, read_settings, describe, enqueue_reminders
from app.services.statistics import period_bounds, read_statistics, render_statistics


def valid_id(value):
    return type(value) is int and value != 0 and -(2**63) <= value < 2**63


@dataclass(frozen=True)
class ProcessingPolicy:
    client_chats: frozenset[int]
    specialists: frozenset[int]
    work_chat: int
    timeout_seconds: int = 300
    admins: frozenset[int] = frozenset()
    timezone: str = 'Europe/Moscow'
    auto_client_chats: bool = False

    def is_client(self, chat):
        return valid_id(chat) and chat != self.work_chat and (self.auto_client_chats or chat in self.client_chats)

    def __post_init__(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ConfigError('Некорректный часовой пояс статистики.') from None
        if type(self.auto_client_chats) is not bool:
            raise ConfigError('Автоматический выбор чатов должен быть boolean.')
        if not self.client_chats and not self.auto_client_chats:
            raise ConfigError('Задайте клиентские чаты для обработки.')
        if not all(valid_id(x) for x in (*self.client_chats, *self.specialists, *self.admins, self.work_chat)):
            raise ConfigError('ID должны быть ненулевыми целыми числами int64.')
        if self.work_chat in self.client_chats:
            raise ConfigError('Рабочий чат не может быть клиентским.')
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 86400:
            raise ConfigError('Время ожидания должно быть от 1 до 86400 секунд.')


class InboxProcessor:
    def __init__(self, store, policy):
        self.store, self.policy = store, policy

    async def initialize_roles(self):
        def initialize():
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                bootstrap_roles(conn, self.policy)
                bootstrap_preferences(conn, self.policy.timeout_seconds)
        await asyncio.to_thread(initialize)

    async def tick(self, limit=100, now_ms=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return await asyncio.to_thread(self._tick, limit, now_ms)

    def _tick(self, limit, now_ms):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            bootstrap_roles(conn, self.policy)
            bootstrap_preferences(conn, self.policy.timeout_seconds)
            rows = conn.execute("""SELECT id,payload_json,received_at FROM inbox_events
                WHERE processed_at IS NULL AND update_type IN ('message_created','message_callback')
                ORDER BY id LIMIT ?""", (limit,)).fetchall()
            for event_id, payload, received_at in rows:
                outcome = self._handle(conn, event_id, payload, received_at)
                conn.execute('UPDATE inbox_events SET processed_at=?,outcome=? WHERE id=?',
                             (datetime.now(timezone.utc).isoformat(), outcome, event_id))
            # Drain received messages before expiring waits: a queued specialist
            # response must be able to cancel them, even across batch boundaries.
            backlog = conn.execute("""SELECT 1 FROM inbox_events WHERE processed_at IS NULL
                AND update_type IN ('message_created','message_callback') LIMIT 1""").fetchone() is not None
            promoted = 0
            if not backlog:
                promoted = conn.execute("""UPDATE requests SET status='new'
                    WHERE status='waiting' AND due_at_ms<=?""", (now_ms,)).rowcount
            enqueue_cards(conn, self.policy.work_chat, now_ms=now_ms, timezone=self.policy.timezone)
            if not backlog:
                enqueue_reminders(conn, self.policy.work_chat, self.policy.timezone, now_ms)
                daily_digest.enqueue_daily(conn, self.policy.work_chat, self.policy.timezone, now_ms)
                autoclean.enqueue_cleanup(conn, self.policy.timezone, now_ms)
            return {'processed': len(rows), 'promoted': promoted, 'more_messages': backlog}

    def _handle(self, conn, event_id, payload, received_at):
        event = json.loads(payload)
        if event.get('update_type') == 'message_callback':
            return self._callback(conn, event, received_at)
        msg = event.get('message')
        if not isinstance(msg, dict):
            return 'invalid_message'
        sender, recipient = msg.get('sender'), msg.get('recipient')
        if not isinstance(sender, dict) or not isinstance(recipient, dict):
            return 'invalid_sender_or_recipient'
        user, chat = sender.get('user_id'), recipient.get('chat_id')
        if not valid_id(user) or not valid_id(chat):
            return 'invalid_id'
        if self.policy.auto_client_chats and chat != self.policy.work_chat and recipient.get('chat_type') != 'chat':
            return 'ignored_chat'
        # Missing bot flag is not assumed to mean a human.
        if sender.get('is_bot') is not False:
            return 'ignored_bot_or_unknown_sender'
        body = msg.get('body')
        if body is not None and not isinstance(body, dict):
            return 'invalid_body'
        body = body or {}
        text = body.get('text') or ''
        if not isinstance(text, str):
            return 'invalid_text'
        if text.split() and text.split()[0] in admin_requests.COMMANDS:
            return admin_requests.handle(conn, event_id, user, chat, text, received_at, self.policy)
        if text.split() and text.split()[0] == '/request':
            return request_view.handle(conn, event_id, user, chat, text, self.policy)
        if text.split() and text.split()[0] in navigation.COMMANDS:
            return navigation.handle(conn, str(event_id), user, chat, text, self.policy)
        if chat == self.policy.work_chat and text.lstrip().startswith('/'):
            if text.split()[0] in ('/stats', '/my_stats', '/stats_xlsx', '/my_stats_xlsx'):
                return self._statistics(conn, event_id, user, chat, text, received_at)
            if text.split()[0] in ('/set_notice', '/get_notice', '/del_notice'):
                return self._notices(conn, event_id, user, chat, text, received_at)
            return self._management(conn, event_id, user, chat, text, received_at)
        if not self.policy.is_client(chat):
            return 'ignored_chat'
        if text.lstrip().startswith('/'):
            return 'deferred_command'
        if has_role(conn, user, 'specialist') or has_role(conn, user, 'admin'):
            conn.execute("UPDATE requests SET status='cancelled' WHERE chat_id=? AND status='waiting'", (chat,))
            return 'specialist_response'
        if is_ignored(conn, text):
            return 'ignored_words'
        if not text.strip() and not body.get('attachments') and not msg.get('link'):
            return 'ignored_empty'
        existing = conn.execute("""SELECT id,status FROM requests WHERE chat_id=? AND user_id=?
            AND status IN ('waiting','new','in_progress')""", (chat, user)).fetchone()
        if existing and existing[1] in ('new', 'in_progress'):
            return 'active_request_exists'
        if existing:
            request_id = existing[0]
        else:
            received_ms = int(datetime.fromisoformat(received_at).timestamp() * 1000)
            name = sender.get('name') or sender.get('first_name') or str(user)
            if not isinstance(name, str):
                name = str(user)
            request_id = conn.execute("""INSERT INTO requests
                (chat_id,user_id,author_name,status,due_at_ms,created_at_ms)
                VALUES (?,?,?,'waiting',?,?)""", (chat, user, name,
                received_ms + get_timeout(conn) * 1000, received_ms)).lastrowid
        conn.execute('INSERT INTO request_messages VALUES (?,?,?,?)',
                     (event_id, request_id, body.get('mid'), payload))
        return 'collected'

    def _callback(self, conn, event, received_at):
        callback = event.get('callback') or {}
        data = callback.get('payload')
        if isinstance(data, str) and data in navigation.CALLBACKS:
            return navigation.callback(conn, event, self.policy)
        match = re.fullmatch(r'(take|done|cancel):([1-9][0-9]{0,17}):([1-9][0-9]{0,17})', data) if isinstance(data, str) else None
        user = callback.get('user')
        msg = event.get('message')
        if not match or not isinstance(user, dict) or not isinstance(msg, dict):
            return 'invalid_callback'
        if not valid_id(user.get('user_id')) or user.get('is_bot') is not False:
            return 'invalid_callback_user'
        recipient, body = msg.get('recipient'), msg.get('body')
        if not isinstance(recipient, dict) or not isinstance(body, dict):
            return 'invalid_callback_message'
        chat, mid = recipient.get('chat_id'), body.get('mid')
        if not valid_id(chat) or not isinstance(mid, str):
            return 'invalid_callback_message'
        action, request_id, revision = match[1], int(match[2]), int(match[3])
        destination = 'client' if action == 'cancel' else 'work'
        card = conn.execute('''SELECT chat_id FROM outbox WHERE request_id=? AND destination=?
            AND message_id=?''', (request_id, destination, mid)).fetchone()
        if card is None or card[0] != chat:
            return 'unknown_callback_card'
        # Acknowledge only callbacks tied to one of our known cards. State change
        # and acknowledgement are committed together, including rejected actions.
        conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,callback_id)
            VALUES (?,?,?,'',?) ON CONFLICT(request_id,destination) DO NOTHING''',
            (request_id, 'answer:' + callback['callback_id'], chat, callback['callback_id']))
        row = conn.execute('SELECT status,user_id,specialist_id,revision,chat_id FROM requests WHERE id=?',
                           (request_id,)).fetchone()
        if row is None:
            return 'missing_request'
        status, author, assigned, current_revision, client_chat = row
        if revision != current_revision:
            return 'stale_callback'
        actor = user['user_id']
        if action in ('take', 'done'):
            if not has_role(conn, actor, 'specialist') or chat != self.policy.work_chat:
                return 'forbidden_callback'
        elif actor != author or chat != client_chat or not self.policy.is_client(chat):
            return 'forbidden_callback'
        now = int(datetime.fromisoformat(received_at).timestamp() * 1000)
        if action == 'take' and status == 'new':
            conn.execute("""UPDATE requests SET status='in_progress',specialist_id=?,started_at_ms=?,
                revision=revision+1 WHERE id=?""", (actor, now, request_id))
        elif action == 'done' and status == 'in_progress' and actor == assigned:
            conn.execute("UPDATE requests SET status='closed',ended_at_ms=?,revision=revision+1 WHERE id=?",
                         (now, request_id))
        elif action == 'cancel' and status == 'new':
            conn.execute("UPDATE requests SET status='cancelled',ended_at_ms=?,revision=revision+1 WHERE id=?",
                         (now, request_id))
        else:
            return 'invalid_transition'
        return 'action_' + action

    def _statistics(self, conn, event_id, actor, chat, text, received_at):
        parts = text.split()
        personal = parts[0] in ('/my_stats', '/my_stats_xlsx')
        if not has_role(conn, actor, 'admin') and not (personal and has_role(conn, actor, 'specialist')):
            return 'forbidden_statistics'
        try:
            period = period_bounds(parts[1:] or ['day'], self.policy.timezone, datetime.fromisoformat(received_at))
            report = read_statistics(conn, period, actor if personal else None)
            if parts[0].endswith('_xlsx'):
                if len(report['groups']) > 10000:
                    raise ManagementError('Для Excel выберите меньший период: максимум 10000 строк.')
                caption = (f"Excel: {period['start_date']} — {period['end_date']}. "
                           + (f'Специалист: {actor}.' if personal else 'Общая статистика.'))
                conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,report_json)
                    VALUES (0,?,?,?,?) ON CONFLICT(request_id,destination) DO NOTHING''',
                    (f'stats:{event_id}:xlsx', chat, caption, json.dumps(report, ensure_ascii=False)))
                return 'statistics_done'
            pages = render_statistics(report)
            outcome = 'statistics_done'
        except ManagementError as exc:
            pages, outcome = [str(exc)], 'statistics_rejected'
        for index, page in enumerate(pages):
            conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text)
                VALUES (0,?,?,?) ON CONFLICT(request_id,destination) DO NOTHING''',
                (f'stats:{event_id}:{index}', chat, page))
        return outcome

    def _notices(self, conn, event_id, actor, chat, text, received_at):
        if not has_role(conn, actor, 'admin'):
            return 'forbidden_management'
        parts = text.split()
        try:
            if parts[0] == '/set_notice':
                notice_id = notices.add(conn, text, self.policy.timezone,
                    int(datetime.fromisoformat(received_at).timestamp() * 1000), actor=actor, event_id=event_id)
                pages = [f'Объявление #{notice_id} сохранено для новых клиентских карточек.']
            elif parts == ['/get_notice']:
                pages = notices.list_pages(conn, self.policy.timezone)
            elif len(parts) == 2 and parts[0] == '/del_notice':
                count = notices.remove(conn, parts[1], actor=actor, event_id=event_id)
                pages = [f'Удалено объявлений: {count}.']
            else:
                raise ManagementError('Команды: /get_notice; /del_notice <ID|all>. ' + notices.USAGE)
            outcome = 'management_done'
        except ManagementError as exc:
            pages, outcome = [str(exc)], 'management_rejected'
        for index, page in enumerate(pages):
            conn.execute('INSERT INTO outbox(request_id,destination,chat_id,text) VALUES (0,?,?,?)',
                         (f'notice:{event_id}:{index}', chat, page))
        return outcome

    def _management(self, conn, event_id, actor, chat, text, received_at):
        parts = text.split()
        if parts[0] not in ('/roles', '/role_grant', '/role_revoke', '/reassign',
                            '/set_timeout', '/get_timeout', '/add_ignore', '/del_ignore', '/list_ignore',
                            '/set_unassigned_reminder', '/get_unassigned_reminder',
                            '/set_daily_reminder', '/get_daily_reminder', '/set_autoclean', '/get_autoclean'):
            return 'deferred_command'
        if not has_role(conn, actor, 'admin'):
            return 'forbidden_management'
        try:
            if parts[0] == '/set_autoclean':
                settings = autoclean.configure(conn, parts[1:], actor=actor, event_id=event_id)
                reply = autoclean.describe(settings, self.policy.timezone)
            elif parts == ['/get_autoclean']:
                reply = autoclean.describe(autoclean.read_settings(conn), self.policy.timezone)
            elif parts[0] == '/get_autoclean':
                raise ManagementError('Использование: /get_autoclean без параметров.')
            elif parts[0] == '/set_daily_reminder':
                settings = daily_digest.configure(conn, parts[1:], actor=actor, event_id=event_id)
                reply = daily_digest.describe(settings, self.policy.timezone)
            elif parts == ['/get_daily_reminder']:
                reply = daily_digest.describe(daily_digest.read_settings(conn), self.policy.timezone)
            elif parts[0] == '/get_daily_reminder':
                raise ManagementError('Использование: /get_daily_reminder без параметров.')
            elif parts[0] == '/set_unassigned_reminder':
                settings = configure(conn, parts[1:], int(datetime.fromisoformat(received_at).timestamp() * 1000),
                                     actor=actor, event_id=event_id)
                reply = describe(settings, self.policy.timezone)
            elif parts == ['/get_unassigned_reminder']:
                reply = describe(read_settings(conn), self.policy.timezone)
            elif parts[0] == '/get_unassigned_reminder':
                raise ManagementError('Использование: /get_unassigned_reminder без параметров.')
            elif parts == ['/get_timeout']:
                reply = f'Время ожидания: {get_timeout(conn)} секунд.'
            elif len(parts) == 2 and parts[0] == '/set_timeout':
                if not re.fullmatch(r'[0-9]{1,5}', parts[1]):
                    raise ManagementError('Использование: /set_timeout <1–86400 секунд>.')
                set_timeout(conn, int(parts[1]), actor=actor, event_id=event_id)
                reply = f'Время ожидания новых обращений: {get_timeout(conn)} секунд. Текущие ожидания не изменены.'
            elif parts == ['/list_ignore']:
                words = [r[0] for r in conn.execute('SELECT word FROM ignored_words ORDER BY word')]
                reply = clip('Слова-исключения:\n' + '\n'.join(words)) if words else 'Список исключений пуст.'
            elif len(parts) > 1 and parts[0] in ('/add_ignore', '/del_ignore'):
                count = change_words(conn, parts[1:], parts[0] == '/add_ignore', actor=actor, event_id=event_id)
                reply = f'Изменено слов-исключений: {count}.'
            elif parts[0] in ('/get_timeout', '/set_timeout', '/list_ignore', '/add_ignore', '/del_ignore'):
                raise ManagementError('Команды: /get_timeout; /set_timeout <секунды>; /list_ignore; /add_ignore <слова>; /del_ignore <слова>.')
            elif parts == ['/roles']:
                rows = conn.execute('SELECT role,user_id FROM bot_roles ORDER BY role,user_id').fetchall()
                reply = clip('Роли бота:\n' + '\n'.join(f'{role}: {user}' for role, user in rows))
            elif len(parts) == 3 and parts[0] in ('/role_grant', '/role_revoke'):
                if not re.fullmatch(r'-?[0-9]{1,19}', parts[2]):
                    raise ManagementError('Некорректный MAX ID.')
                changed = change_role(conn, int(parts[2]), parts[1], parts[0] == '/role_grant', actor=actor, event_id=event_id, source='max')
                reply = 'Роль изменена.' if changed else 'Роль уже в указанном состоянии.'
            elif len(parts) == 3 and parts[0] == '/reassign':
                if not all(re.fullmatch(r'[1-9][0-9]{0,18}', p) for p in parts[1:]):
                    raise ManagementError('Укажите номер заявки и MAX ID нового специалиста.')
                if not all(valid_id(int(p)) for p in parts[1:]):
                    raise ManagementError('Номер заявки и MAX ID должны помещаться в int64.')
                changed = reassign(conn, int(parts[1]), int(parts[2]), actor=actor, event_id=event_id)
                reply = 'Исполнитель изменён.' if changed else 'Этот специалист уже назначен.'
            else:
                raise ManagementError('Команды: /roles; /role_grant <роль> <ID>; /role_revoke <роль> <ID>; /reassign <заявка> <ID>.')
        except ManagementError as exc:
            reply = str(exc)
            outcome = 'management_rejected'
        else:
            outcome = 'management_done'
        # A separate unique destination prevents replayed commands from duplicating replies.
        conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text)
            VALUES (0,?,?,?) ON CONFLICT(request_id,destination) DO NOTHING''',
            (f'command:{event_id}', chat, reply))
        return outcome
