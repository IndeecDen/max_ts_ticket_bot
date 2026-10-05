"""Role-scoped navigation and bounded request lists; no request mutations."""
import json
import html
import re
from app.services.roles import has_role, ManagementError
from app.services.user_names import display_name

COMMANDS = frozenset(('/start', '/help', '/menu', '/whoami', '/my_requests',
                      '/open_requests', '/open_unassigned_requests', '/my_active_requests'))
CALLBACKS = {'nav:' + command[1:]: command for command in COMMANDS}
LABELS = {'waiting':'Ожидает ответа', 'new':'Без исполнителя', 'in_progress':'В работе',
          'closed':'Завершена', 'cancelled':'Отменена'}


def handle(conn, key, actor, chat, text, policy, *, private_admin=False):
    if chat != policy.work_chat and not policy.is_client(chat):
        return 'ignored_chat'
    admin = has_role(conn, actor, 'admin')
    staff = admin or has_role(conn, actor, 'specialist')
    work = chat == policy.work_chat
    parts = text.split()
    command = parts[0]
    if command == '/menu' and not private_admin:
        return 'private_menu_only'
    attachments = []
    outcome = 'navigation_done'
    formatted = False
    replies = None
    try:
        if command in ('/start', '/help', '/menu', '/whoami'):
            if len(parts) != 1:
                raise ManagementError(f'Использование: {command} без параметров.')
            if command == '/whoami':
                roles = [role for role in ('admin', 'specialist') if has_role(conn, actor, role)]
                reply = f"Ваш MAX ID: {actor}\nID чата: {chat}\nРоли бота: {', '.join(roles) or 'клиент'}."
            else:
                reply = ('Бот технической поддержки MAX.\n'
                          '/menu — меню сотрудников в личном диалоге; /help — помощь; /whoami — ваши ID и роли.\n')
                buttons = [('Мои ID', 'whoami'), ('Помощь', 'help')]
                if not work:
                    reply += ('Отправьте описание проблемы обычным сообщением. После ожидания ответа сотрудника '
                              'бот создаст карточку заявки.\n/my_requests — ваши обращения в этом чате.\n'
                              'Отмена новой заявки — кнопкой в её карточке.')
                    buttons.append(('Мои обращения', 'my_requests'))
                elif staff:
                    reply += ('/open_requests — открытые заявки; /open_unassigned_requests — без исполнителя; '
                              '/my_active_requests — назначенные вам. К спискам можно добавить номер страницы.\n'
                              '/my_stats и /my_stats_xlsx — личная статистика.\n'
                              'Взять и завершить заявку можно кнопками в рабочих карточках.')
                    buttons += [('Открытые', 'open_requests'), ('Без исполнителя', 'open_unassigned_requests'),
                                ('Мои в работе', 'my_active_requests')]
                    if admin:
                        reply += ('\nАдминистрирование: /roles, /role_grant, /role_revoke, /reassign, /assign_request, /cancel_request, /close_all.\n'
                                  'Статистика: /stats, /stats_xlsx.\n'
                                  'Настройки: /get_timeout, /set_timeout, /list_ignore, /add_ignore, /del_ignore.\n'
                                  'Объявления: /set_notice, /get_notice, /del_notice.\n'
                                  'Расписания: /set_daily_reminder, /get_daily_reminder, '
                                  '/set_unassigned_reminder, /get_unassigned_reminder, /set_autoclean, /get_autoclean.')
                else:
                    reply += 'Рабочие списки доступны специалистам и администраторам бота.'
                if not work or staff:
                    reply += '\n/request <номер> [страница] — полный сохранённый текст обращения.'
                if private_admin:
                    button_rows = [[{'type':'callback','text':label,'payload':'nav:'+name}]
                                   for label, name in buttons]
                    attachments = [{'type':'inline_keyboard','payload':{'buttons':button_rows}}]
        else:
            if command == '/my_requests':
                if work:
                    raise ManagementError('/my_requests доступна в клиентском чате.')
                where, args, title = 'chat_id=? AND user_id=?', [chat, actor], 'Ваши обращения в этом чате'
            else:
                if not work or not staff:
                    raise ManagementError('Этот список доступен сотрудникам только в рабочем чате.')
                if command == '/open_requests':
                    where, args, title = "status IN ('waiting','new','in_progress')", [], 'Открытые заявки'
                elif command == '/open_unassigned_requests':
                    where, args, title = "status='new' AND specialist_id IS NULL", [], 'Заявки без исполнителя'
                elif command == '/my_active_requests':
                    where, args, title = "status='in_progress' AND specialist_id=?", [actor], 'Назначенные вам заявки'
                else:
                    raise ManagementError('Неизвестная команда меню.')
            if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r'[1-9][0-9]{0,6}', parts[1])):
                raise ManagementError(f'Использование: {command} [номер страницы].')
            page = int(parts[1]) if len(parts) == 2 else 1
            count = conn.execute('SELECT count(*) FROM requests WHERE '+where, args).fetchone()[0]
            pages = max(1, (count + 9) // 10)
            if page > pages:
                raise ManagementError(f'Нет такой страницы. Доступно страниц: {pages}.')
            rows = conn.execute('SELECT id,chat_id,status,specialist_id FROM requests WHERE '+where+
                                ' ORDER BY id DESC LIMIT 10 OFFSET ?', [*args,(page-1)*10]).fetchall()
            lines = [f'{title}: {count}. Страница {page}/{pages}.']
            replies = []
            formatted = True
            for request_id, request_chat, status, specialist in rows:
                suffix = f' · специалист {html.escape(display_name(conn, specialist))}' if work and specialist is not None else ''
                location = f'\nЧат: {request_chat}' if work else ''
                header = f'#{request_id} · {LABELS[status]}{suffix}{location}\n'
                texts = []
                for (raw,) in conn.execute('SELECT payload_json FROM request_messages WHERE request_id=? ORDER BY event_id', (request_id,)):
                    body = (json.loads(raw).get('message') or {}).get('body') or {}
                    texts.append(body.get('text') or '[Вложение или пересылка]')
                # Bounded preview keeps ten requests usable; full text remains available.
                preview = '\n'.join(texts)
                bounded = ''
                for char in preview:
                    escaped = html.escape(char)
                    if len((header + bounded + escaped).encode('utf-16-le')) // 2 > 2700:
                        bounded += f'…\nПолный текст: /request {request_id}'
                        break
                    bounded += escaped
                item = header + bounded
                if lines and (len(lines) > 1 or len(('\n\n'.join(lines) + '\n\n' + item).encode('utf-16-le')) // 2 > 2700):
                    replies.append('\n\n'.join(lines))
                    lines = []
                lines.append(item)
            if not rows:
                lines.append('Заявок нет.')
            if pages > 1:
                lines.append(f'Открыть страницу: {command} <номер>.')
            replies.append('\n\n'.join(lines))
            reply = replies[0]
    except ManagementError as exc:
        reply, outcome = str(exc), 'navigation_rejected'
    for index, reply in enumerate(replies or [reply]):
        conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,attachments_json,menu_owner,text_format)
            VALUES (0,?,?,?,?,?,?)''', (f'navigation:{key}' + (f':part:{index}' if index else ''), chat, reply,
            json.dumps(attachments, ensure_ascii=False), actor, 'html' if formatted else None))
    return outcome


def callback(conn, event, policy):
    data = event['callback']
    user, message = data.get('user'), event.get('message')
    if not isinstance(user, dict) or user.get('is_bot') is not False or type(user.get('user_id')) is not int:
        return 'invalid_navigation_callback'
    if not isinstance(message, dict):
        return 'invalid_navigation_callback'
    recipient, body = message.get('recipient'), message.get('body')
    if not isinstance(recipient, dict) or not isinstance(body, dict):
        return 'invalid_navigation_callback'
    chat, mid, actor = recipient.get('chat_id'), body.get('mid'), user['user_id']
    if type(chat) is not int or not isinstance(mid, str):
        return 'invalid_navigation_callback'
    if recipient.get('chat_type') != 'dialog' or not (has_role(conn, actor, 'admin') or has_role(conn, actor, 'specialist')):
        return 'private_menu_only'
    # Bind buttons to an actual menu sent for this user and this chat.
    menu = conn.execute('''SELECT attachments_json FROM outbox WHERE message_id=? AND chat_id=?
        AND menu_owner=? AND deleted_at_ms IS NULL AND destination LIKE 'navigation:%' LIMIT 1''',
        (mid, chat, actor)).fetchone()
    if menu is None:
        return 'unknown_navigation_menu'
    payload = data['payload']
    buttons = [button for attachment in json.loads(menu[0])
               for row in attachment.get('payload', {}).get('buttons', []) for button in row]
    if not any(button.get('payload') == payload for button in buttons):
        return 'unknown_navigation_action'
    from dataclasses import replace
    policy = replace(policy, work_chat=chat, client_chats=policy.client_chats - {chat})
    conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,callback_id)
        VALUES (0,?,?,'',?)''', ('answer:'+data['callback_id'], chat, data['callback_id']))
    return handle(conn, 'callback:'+data['callback_id'], actor, chat, CALLBACKS[payload], policy, private_admin=True)
