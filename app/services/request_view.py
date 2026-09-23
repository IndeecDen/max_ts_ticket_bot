"""Authorized, paginated access to the text actually preserved with a request."""
import json
import re
from app.services.roles import has_role, ManagementError

USAGE = 'Использование: /request <номер заявки> [страница].'
DENIED = 'Заявка не найдена или недоступна в этом чате.'
STATUS = {'waiting':'Ожидает ответа','new':'Без исполнителя','in_progress':'В работе',
          'closed':'Завершена','cancelled':'Отменена'}


def fragments(conn, request_id):
    found = False
    for index, (raw,) in enumerate(conn.execute('SELECT payload_json FROM request_messages WHERE request_id=? ORDER BY event_id', (request_id,)), 1):
        found = True
        message = json.loads(raw)['message']
        body = message.get('body') or {}
        yield f'Сообщение {index}\n'
        text = body.get('text')
        yield text if isinstance(text, str) and text else '[Без текста]'
        attachments = body.get('attachments')
        if isinstance(attachments, list) and attachments:
            types = []
            for item in attachments:
                kind = item.get('type') if isinstance(item, dict) else None
                types.append(kind if kind in ('image','video','audio','file','sticker','contact','location','share') else 'неизвестный тип')
            yield '\nВложения: ' + ', '.join(types) + '. Файлы здесь не пересылаются.'
        link = message.get('link')
        if isinstance(link, dict):
            yield '\nСвязанное/пересланное сообщение:\n'
            linked = link.get('message')
            linked_body = linked.get('body') if isinstance(linked, dict) else None
            linked_text = (linked_body.get('text') if isinstance(linked_body, dict) else None)
            if not isinstance(linked_text, str) and isinstance(linked, dict):
                linked_text = linked.get('text')
            yield linked_text if isinstance(linked_text, str) and linked_text else '[Текст не сохранён в событии]'
        yield '\n\n'
    if not found:
        yield 'Исходные сообщения для этой заявки не сохранены.'


def pages(fragments, limit=3000):
    buffer, units = [], 0
    for fragment in fragments:
        for char in fragment:
            size = 2 if ord(char) > 0xffff else 1
            if units + size > limit:
                yield ''.join(buffer)
                buffer, units = [], 0
            buffer.append(char)
            units += size
    if buffer:
        yield ''.join(buffer)


def handle(conn, event_id, actor, chat, text, policy):
    if chat != policy.work_chat and not policy.is_client(chat):
        return 'ignored_chat'
    try:
        parts = text.split()
        if (len(parts) not in (2,3) or not re.fullmatch(r'[1-9][0-9]{0,18}', parts[1])
                or int(parts[1]) >= 2**63 or (len(parts)==3 and not re.fullmatch(r'[1-9][0-9]{0,8}', parts[2]))):
            raise ManagementError(USAGE)
        request_id, requested = int(parts[1]), int(parts[2]) if len(parts)==3 else 1
        row = conn.execute('SELECT chat_id,user_id,status,specialist_id FROM requests WHERE id=?', (request_id,)).fetchone()
        work = chat == policy.work_chat
        allowed = ((has_role(conn,actor,'admin') or has_role(conn,actor,'specialist')) if work
                   else row is not None and row[0]==chat and row[1]==actor)
        if row is None or not allowed:
            raise ManagementError(DENIED)
        selected, count = '', 0
        for count, page in enumerate(pages(fragments(conn, request_id)),1):
            if count == requested:
                selected = page
        if requested > count:
            raise ManagementError(f'Нет такой страницы. Доступно страниц: {count}.')
        header = f'Заявка #{request_id} · {STATUS[row[2]]}\nСтраница {requested}/{count}.'
        if work:
            header += f' Чат {row[0]}; автор {row[1]}; специалист {row[3] if row[3] is not None else "не назначен"}.'
        reply = header + '\n\n' + selected
        if requested < count:
            reply += f'\nДалее: /request {request_id} {requested+1}'
        outcome = 'request_view_done'
    except ManagementError as exc:
        reply, outcome = str(exc), 'request_view_rejected'
    conn.execute('INSERT INTO outbox(request_id,destination,chat_id,text) VALUES (0,?,?,?)',
                 (f'request_view:{event_id}',chat,reply))
    return outcome
