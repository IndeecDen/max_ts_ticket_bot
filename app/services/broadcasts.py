"""Durable one-shot HTML broadcasts using the normal delivery queue."""
import re
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from urllib.parse import urlsplit

from app.services.roles import has_role, ManagementError, log_change


def remember_chat(conn, event):
    kind = event.get('update_type')
    message = event.get('message')
    recipient = message.get('recipient') if isinstance(message, dict) else None
    recipient = recipient if isinstance(recipient, dict) else {}
    chat = event.get('chat_id') if kind in ('bot_added', 'bot_removed') else recipient.get('chat_id')
    if type(chat) is not int or not chat or not -(2**63) <= chat < 2**63:
        return
    if kind == 'bot_added' and event.get('is_channel') is not False:
        return
    if kind not in ('bot_added', 'bot_removed') and recipient.get('chat_type') != 'chat':
        return
    stamp = event.get('timestamp', 0)
    if type(stamp) is not int:
        return
    conn.execute('''INSERT INTO broadcast_chats VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
        active=excluded.active,timestamp_ms=excluded.timestamp_ms
        WHERE excluded.timestamp_ms>=broadcast_chats.timestamp_ms''', (chat, int(kind != 'bot_removed'), stamp))


class ValidHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.content = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ('b', 'strong', 'i', 'em', 'u', 's', 'del', 'code', 'pre', 'a'):
            raise ValueError
        if tag == 'a':
            if len(attrs) != 1 or attrs[0][0] != 'href':
                raise ValueError
            url = urlsplit(attrs[0][1] or '')
            if url.scheme not in ('http', 'https') or not url.netloc:
                raise ValueError
        elif attrs:
            raise ValueError
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError

    def handle_data(self, data):
        self.content.append(data)

    def handle_comment(self, data):
        raise ValueError

    def handle_decl(self, decl):
        raise ValueError

    def handle_pi(self, data):
        raise ValueError


def validate(text):
    try:
        parser = ValidHTML()
        parser.feed(text)
        parser.close()
        if parser.stack or not ''.join(parser.content).strip() or len(text.encode('utf-16-le')) // 2 > 4000:
            raise ValueError
    except ValueError:
        raise ManagementError('Введите текст до 4000 символов UTF-16 с корректными HTML-тегами b, i, u, s, code, pre или a href="https://…".') from None
    return text


def due_time(value, tz, now):
    if re.fullmatch(r'[1-9][0-9]{0,5}', value.strip()):
        return now + int(value) * 60000
    try:
        local = datetime.strptime(value.strip(), '%Y-%m-%d %H:%M')
        zone = ZoneInfo(tz)
        candidates = set()
        for fold in (0, 1):
            utc = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == local:
                candidates.add(int(utc.timestamp() * 1000))
        if len(candidates) != 1 or min(candidates) <= now:
            raise ValueError
        return candidates.pop()
    except (ValueError, OverflowError):
        raise ManagementError('Укажите число минут (1–999999) или будущую дату ГГГГ-ММ-ДД ЧЧ:ММ; время должно быть однозначным.') from None


def media(conn, key):
    row = conn.execute('SELECT payload_json FROM inbox_events WHERE id=?', (key,)).fetchone()
    message = (json.loads(row[0]).get('message') or {}) if row else {}
    items = (message.get('body') or {}).get('attachments') or []
    if not isinstance(items, list):
        raise ManagementError('Некорректные вложения. Отправьте сообщение заново.')
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get('type') not in ('image', 'file', 'video', 'audio'):
            raise ManagementError('Поддерживаются фото, файлы, видео и аудио. Этот тип вложения не поддерживается.')
        payload = item.get('payload')
        token = payload.get('token') if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ManagementError('MAX не передал токен вложения. Загрузите фото или файл напрямую, а не пересылкой.')
        result.append({'type': item['type'], 'payload': {'token': token}})
    if len(result) > 1 and any(item['type'] in ('file', 'audio') for item in result):
        raise ManagementError('Файл или аудио отправляйте одним вложением в сообщении.')
    if (message.get('link') or {}).get('type') == 'forward' and not result:
        raise ManagementError('Загрузите вложение напрямую, а не пересылкой.')
    return json.dumps(result, ensure_ascii=False)


def enqueue(conn, work_chat, now):
    for bid, actor, text, attachments in conn.execute("SELECT id,actor,text,attachments_json FROM broadcasts WHERE state='scheduled' AND due_at_ms<=?", (now,)).fetchall():
        if not has_role(conn, actor, 'admin'):
            conn.execute("UPDATE broadcasts SET state='cancelled' WHERE id=?", (bid,))
            continue
        for (chat,) in conn.execute('SELECT chat_id FROM broadcast_chats WHERE active=1 AND chat_id<>?', (work_chat,)).fetchall():
            conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,text_format,attachments_json)
                VALUES (0,?,?,?,'html',?) ON CONFLICT(request_id,destination) DO NOTHING''',
                (f'broadcast:{bid}:{chat}', chat, text, attachments))
        conn.execute("UPDATE broadcasts SET state='queued' WHERE id=?", (bid,))


def handle(conn, key, actor, chat, action, value, policy):
    from app.services.staff_menu import send, clear
    if not has_role(conn, actor, 'admin'):
        clear(conn, actor, chat)
        return 'forbidden_management'
    now = int(time.time() * 1000)
    def prompt(step, text, buttons=None):
        conn.execute('INSERT OR REPLACE INTO menu_sessions VALUES (?,?,?,?)', (chat, actor, step, now + 900000))
        send(conn, key, actor, chat, text, buttons or [('↩ Рассылки', 'broadcasts')])
    def home(text):
        clear(conn, actor, chat)
        send(conn, key, actor, chat, text, [('✏ Новая рассылка', 'broadcast_new'), ('📋 Список / отмена', 'broadcast_list'), ('🏠 Главное меню', 'home')])
    if action in ('broadcasts', 'broadcast_list'):
        count = conn.execute('SELECT count(*) FROM broadcast_chats WHERE active=1 AND chat_id<>?', (policy.work_chat,)).fetchone()[0]
        text = f'📣 Рассылки · известных групп: {count}\nЧасовой пояс: {policy.timezone}\nРабочий чат исключён. Формат текста — HTML.\n'
        buttons = [('✏ Новая рассылка', 'broadcast_new')]
        for bid, state, due in conn.execute('SELECT id,state,due_at_ms FROM broadcasts WHERE state<>\'draft\' ORDER BY id DESC LIMIT 10'):
            counts = dict(conn.execute('SELECT state,count(*) FROM outbox WHERE destination LIKE ? GROUP BY state', (f'broadcast:{bid}:%',)))
            label = {'scheduled': 'Запланирована', 'queued': 'В очереди', 'cancelled': 'Отменена'}.get(state, state)
            when = datetime.fromtimestamp(due / 1000, ZoneInfo(policy.timezone)).strftime('%d.%m.%Y %H:%M')
            text += f'\n#{bid} · {label} · {when}\nОтправлено: {counts.get("sent", 0)}, ожидают: {counts.get("pending", 0)}, ошибки: {counts.get("failed", 0)}, неопределённые: {counts.get("uncertain", 0)}, отправляются: {counts.get("sending", 0)}\n'
            if state == 'scheduled':
                buttons.append((f'✖ Отменить #{bid}', f'broadcast_cancel:{bid}'))
        clear(conn, actor, chat)
        send(conn, key, actor, chat, text, buttons + [('🔄 Обновить', 'broadcast_list'), ('🏠 Меню', 'home')])
    elif action == 'broadcast_new':
        prompt('broadcast_text', 'Отправьте текст рассылки (до 4000 символов) в HTML или фото/файл с подписью.\nПример: <b>Внимание!</b>\n<i>Плановые работы</i>\n<a href="https://example.org">Подробнее</a>\n\nВложение и подпись отправляйте одним сообщением. /cancel — отменить ввод.')
    elif action == 'broadcast_text':
        try:
            attachments = media(conn, key)
            text = validate(value) if value.strip() or attachments == '[]' else ''
        except ManagementError as exc:
            prompt(action, str(exc))
            return 'management_rejected'
        bid = conn.execute('INSERT INTO broadcasts(actor,chat_id,text,created_at_ms,attachments_json) VALUES (?,?,?,?,?)', (actor, chat, text, now, attachments)).lastrowid
        conn.execute("INSERT INTO outbox(request_id,destination,chat_id,text,text_format,attachments_json) VALUES (0,?,?,?,'html',?)", (f'broadcast-preview:{key}', chat, text, attachments))
        clear(conn, actor, chat)
        send(conn, key, actor, chat, f'Предпросмотр рассылки #{bid} — отдельным сообщением. Отправьте сейчас, задайте таймер или отмените рассылку.', [('🚀 Отправить сейчас', f'broadcast_now:{bid}'), ('🕒 По таймеру', f'broadcast_timer:{bid}'), ('✖ Отменить', f'broadcast_cancel:{bid}')], force_new=True)
    else:
        op, raw = action.split(':', 1)
        bid = int(raw)
        row = conn.execute('SELECT actor,chat_id,state FROM broadcasts WHERE id=?', (bid,)).fetchone()
        if (not row or (op != 'broadcast_cancel' and row != (actor, chat, 'draft'))
                or (op == 'broadcast_cancel' and row[2] == 'draft' and row[:2] != (actor, chat))):
            home('Рассылка уже обработана или недоступна.')
            return 'management_rejected'
        if op == 'broadcast_cancel':
            changed = conn.execute("UPDATE broadcasts SET state='cancelled' WHERE id=? AND state IN ('draft','scheduled')", (bid,)).rowcount
            if changed:
                log_change(conn, actor, key, 'broadcast_cancel', bid, {})
            home(f'Рассылка #{bid}: {"отменена" if changed or row[2] == "cancelled" else "отправка уже началась"}.')
        elif op == 'broadcast_timer':
            prompt(f'broadcast_time:{bid}', f'Введите число минут до отправки или дату ГГГГ-ММ-ДД ЧЧ:ММ ({policy.timezone}).', [('✖ Отменить', f'broadcast_cancel:{bid}')])
        else:
            try:
                due = now if op == 'broadcast_now' else due_time(value, policy.timezone, now)
            except ManagementError as exc:
                prompt(action, str(exc), [('✖ Отменить', f'broadcast_cancel:{bid}')])
                return 'management_rejected'
            conn.execute("UPDATE broadcasts SET state='scheduled',due_at_ms=? WHERE id=?", (due, bid))
            log_change(conn, actor, key, 'broadcast_schedule', bid, {'due_at_ms': due})
            home(f'✅ Рассылка #{bid} запланирована. Получатели определяются в момент запуска. Состояние доставки — в списке рассылок.')
    return 'management_done'
