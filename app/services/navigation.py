"""Role-scoped navigation and bounded request lists; no request mutations."""
import json
import re
from app.services.roles import has_role, ManagementError

COMMANDS = frozenset(('/start', '/help', '/menu', '/whoami', '/my_requests',
                      '/open_requests', '/open_unassigned_requests', '/my_active_requests'))
CALLBACKS = {'nav:' + command[1:]: command for command in COMMANDS}
LABELS = {'waiting':'Ожидает ответа', 'new':'Без исполнителя', 'in_progress':'В работе',
          'closed':'Завершена', 'cancelled':'Отменена'}


def handle(conn, key, actor, chat, text, policy):
    if chat != policy.work_chat and chat not in policy.client_chats:
        return 'ignored_chat'
    admin = has_role(conn, actor, 'admin')
    staff = admin or has_role(conn, actor, 'specialist')
    work = chat == policy.work_chat
    parts = text.split()
    command = parts[0]
    attachments = []
    outcome = 'navigation_done'
    try:
        if command in ('/start', '/help', '/menu', '/whoami'):
            if len(parts) != 1:
                raise ManagementError(f'Использование: {command} без параметров.')
            if command == '/whoami':
                roles = [role for role in ('admin', 'specialist') if has_role(conn, actor, role)]
                reply = f"Ваш MAX ID: {actor}\nID чата: {chat}\nРоли бота: {', '.join(roles) or 'клиент'}."
            else:
                reply = ('Бот технической поддержки MAX.\n'
                         '/menu — меню; /help — помощь; /whoami — ваши ID и роли.\n')
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
                attachments = [{'type':'inline_keyboard','payload':{'buttons':[
                    [{'type':'callback','text':label,'payload':'nav:'+name}] for label, name in buttons]}}]
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
            for request_id, request_chat, status, specialist in rows:
                suffix = f' · специалист {specialist}' if work and specialist is not None else ''
                location = f' · чат {request_chat}' if work else ''
                lines.append(f'#{request_id} · {LABELS[status]}{location}{suffix}')
            if not rows:
                lines.append('Заявок нет.')
            if rows:
                lines.append('Полный текст: /request <номер заявки>.')
            if pages > 1:
                lines.append(f'Открыть страницу: {command} <номер>.')
            reply = '\n'.join(lines)
    except ManagementError as exc:
        reply, outcome = str(exc), 'navigation_rejected'
    conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,attachments_json,menu_owner)
        VALUES (0,?,?,?,?,?)''', (f'navigation:{key}', chat, reply, json.dumps(attachments, ensure_ascii=False), actor))
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
    conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,callback_id)
        VALUES (0,?,?,'',?)''', ('answer:'+data['callback_id'], chat, data['callback_id']))
    return handle(conn, 'callback:'+data['callback_id'], actor, chat, CALLBACKS[payload], policy)
