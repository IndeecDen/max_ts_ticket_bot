"""Private staff workspace; actions reuse transactional business handlers."""
import json
import re
import time
from dataclasses import replace
from datetime import datetime

from app.services.roles import has_role, log_change, ManagementError
from app.services import navigation, request_view, admin_requests, reminders, daily_digest, autoclean, notices
from app.services.user_names import display_name


def is_staff(conn, actor):
    return has_role(conn, actor, 'admin') or has_role(conn, actor, 'specialist')


# key: (label, command, argument prompt). Empty prompt means immediate read action.
ACTIONS = {
    'open': ('📋 Открытые', '/open_requests', ''),
    'unassigned': ('🆕 Без исполнителя', '/open_unassigned_requests', ''),
    'mine': ('👨‍🔧 Мои в работе', '/my_active_requests', ''),
    'request': ('🔎 Найти заявку', '/request', 'Введите номер заявки. Можно добавить номер страницы: 12 2.'),
    'take': ('🙋 Взять заявку', '', 'Введите номер новой заявки, которую хотите взять.'),
    'done': ('✅ Завершить', '', 'Введите номер своей заявки в работе.'),
    'assign': ('👤 Назначить / переназначить', '/assign_request', 'Введите номер заявки и MAX ID специалиста: 12 123456.'),
    'cancel': ('✖ Отменить заявку', '/cancel_request', 'Введите номер заявки и причину отмены: 12 Дубликат обращения.'),
    'close_all': ('✅ Закрыть все', '/close_all', ''),
    'roles': ('👥 Список сотрудников', '/roles', ''),
    'grant': ('➕ Выдать роль', '/role_grant', 'Введите роль specialist или admin и MAX ID: specialist 123456.'),
    'revoke': ('➖ Отозвать роль', '/role_revoke', 'Введите роль specialist или admin и MAX ID: specialist 123456.'),
    'timeout': ('⏱ Текущее ожидание', '/get_timeout', ''),
    'set_timeout': ('✏ Изменить ожидание', '/set_timeout', 'Введите время ожидания в секундах: от 1 до 86400.'),
    'ignore': ('📝 Стоп-слова', '/list_ignore', ''),
    'add_ignore': ('➕ Добавить слова', '/add_ignore', 'Введите слова через пробел.'),
    'del_ignore': ('➖ Удалить слова', '/del_ignore', 'Введите слова через пробел.'),
    'notices': ('📢 Объявления', '/get_notice', ''),
    'set_notice': ('➕ Новое объявление', '/set_notice', notices.USAGE + '\nОтправьте параметры без /set_notice.'),
    'del_notice': ('➖ Удалить объявление', '/del_notice', 'Введите ID объявления или all для удаления всех.'),
    'reminder': ('🔔 Неназначенные: настройки', '/get_unassigned_reminder', ''),
    'set_reminder': ('✏ Настроить напоминания', '/set_unassigned_reminder', reminders.USAGE + '\nОтправьте параметры без команды.'),
    'digest': ('📅 Дайджест: настройки', '/get_daily_reminder', ''),
    'set_digest': ('✏ Настроить дайджест', '/set_daily_reminder', daily_digest.USAGE + '\nОтправьте параметры без команды.'),
    'clean': ('🧹 Очистка: настройки', '/get_autoclean', ''),
    'set_clean': ('✏ Настроить очистку', '/set_autoclean', autoclean.USAGE + '\nОтправьте параметры без команды.'),
    'whoami': ('🪪 Мои ID и роли', '/whoami', ''),
}
STAFF = {'open', 'unassigned', 'mine', 'request', 'take', 'done', 'whoami'}
SECTIONS = {
    'requests': ('📋 Заявки', ['open', 'unassigned', 'mine', 'request', 'take', 'done', 'assign', 'cancel', 'close_all']),
    'team': ('👥 Сотрудники и роли', ['roles', 'grant', 'revoke']),
    'settings': ('⚙ Настройки обращений', ['timeout', 'set_timeout', 'ignore', 'add_ignore', 'del_ignore']),
    'announcements': ('📢 Объявления', ['notices', 'set_notice', 'del_notice']),
    'schedules': ('🗓 Расписания', ['reminder', 'set_reminder', 'digest', 'set_digest', 'clean', 'set_clean']),
}


def allowed(conn, actor, action):
    if re.fullmatch(r'(open|unassigned|mine):[1-9][0-9]{0,6}', action):
        action = action.split(':')[0]
    return is_staff(conn, actor) and (has_role(conn, actor, 'admin') or action in STAFF
        or action in ('home', 'requests', 'stats', 'help') or action.startswith('personal:'))


def send(conn, key, actor, chat, text, buttons, *, force_new=False):
    rows = [[{'type': 'callback', 'text': label, 'payload': 'ui:' + action}
             for label, action in buttons[i:i+2]] for i in range(0, len(buttons), 2)]
    attachments = json.dumps([{'type': 'inline_keyboard', 'payload': {'buttons': rows}}], ensure_ascii=False)
    previous = conn.execute('''SELECT id FROM outbox WHERE destination LIKE 'navigation:ui:%'
        AND chat_id=? AND menu_owner=? AND deleted_at_ms IS NULL
        AND state IN ('sent','pending','sending') ORDER BY id DESC LIMIT 1''', (chat, actor)).fetchone()
    if previous and not force_new:
        conn.execute('''UPDATE outbox SET text=?,attachments_json=?,revision=revision+1,
            state=CASE WHEN state='sent' THEN 'pending' ELSE state END,next_at_ms=0,
            attempts=CASE WHEN state='sent' THEN 0 ELSE attempts END WHERE id=?''',
            (text, attachments, previous[0]))
        return
    conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,attachments_json,menu_owner)
        VALUES (0,?,?,?,?,?)''', ('navigation:ui:' + str(key), chat, text,
        attachments, actor))


def clear(conn, actor, chat):
    conn.execute('DELETE FROM menu_sessions WHERE chat_id=? AND user_id=?', (chat, actor))


def screen(conn, key, actor, chat, section='home', *, force_new=False):
    admin = has_role(conn, actor, 'admin')
    clear(conn, actor, chat)
    if section == 'home':
        counts = dict(conn.execute('SELECT status,count(*) FROM requests GROUP BY status'))
        text = ('🛠 Техническая поддержка\n' + display_name(conn, actor) + ' · ' + ('администратор' if admin else 'специалист') + '\n\n'
                f"🆕 Новые: {counts.get('new', 0)}   ⏳ Ожидают: {counts.get('waiting', 0)}\n"
                f"🔧 В работе: {counts.get('in_progress', 0)}\n\nВыберите раздел.")
        buttons = [('📋 Заявки', 'requests'), ('📊 Статистика', 'stats')]
        if admin:
            buttons += [(SECTIONS[s][0], s) for s in ('team', 'settings', 'announcements', 'schedules')]
        buttons += [('🪪 Мои ID', 'whoami'), ('❔ Помощь', 'help')]
    elif section == 'stats':
        text = '📊 Статистика\nВыберите период и формат отчёта. Личная статистика относится к вашим завершённым заявкам.'
        buttons = []
        for scope, label in [('personal', 'Моя')] + ([('general', 'Общая')] if admin else []):
            for period, name in [('day', 'день'), ('week', 'неделя'), ('month', 'месяц')]:
                buttons += [(f'{label}: {name}', f'{scope}:{period}'), (f'📎 XLSX: {name}', f'{scope}:{period}:xlsx')]
            buttons += [(f'{label}: даты…', f'{scope}:custom'), (f'📎 XLSX: даты…', f'{scope}:custom:xlsx')]
    elif section == 'help':
        text = ('❔ Рабочее пространство\n\n📋 Заявки — просмотр, поиск, взятие и завершение. '
                'Завершить можно только назначенную вам заявку.\n📊 Статистика — текст и Excel за период.\n\n'
                'После выбора действия отправьте запрошенные параметры обычным сообщением. '
                'При ошибке можно повторить ввод. /cancel отменяет ввод, /menu открывает главное меню.\n'
                'Меню доступно только сотрудникам в личном диалоге. Роли проверяются при каждом действии.')
        buttons = []
    else:
        title, actions = SECTIONS[section]
        text = title + '\n\nВыберите действие.'
        if section == 'team':
            rows = conn.execute('SELECT user_id,role FROM bot_roles ORDER BY role,user_id').fetchall()
            text += '\n\n' + '\n'.join(f'• {display_name(conn, user)} — {role}' for user, role in rows)
        buttons = [(ACTIONS[a][0], a) for a in actions if allowed(conn, actor, a)]
    if section != 'home':
        buttons += [('🏠 Главное меню', 'home')]
    send(conn, key, actor, chat, text, buttons, force_new=force_new)
    return 'navigation_done'


def execute(conn, key, actor, chat, action, arguments, received_at, processor):
    if not allowed(conn, actor, action):
        clear(conn, actor, chat)
        return 'forbidden_management'
    policy = replace(processor.policy, work_chat=chat, client_chats=processor.policy.client_chats - {chat})
    if re.fullmatch(r'(open|unassigned|mine):[1-9][0-9]{0,6}', action):
        action, arguments = action.split(':')
    if action in ('take', 'done'):
        try:
            target = admin_requests.request_id(arguments.strip())
            row = conn.execute('SELECT status,specialist_id FROM requests WHERE id=?', (target,)).fetchone()
            now = int(datetime.fromisoformat(received_at).timestamp() * 1000)
            if action == 'take' and row and row[0] == 'new':
                conn.execute("UPDATE requests SET status='in_progress',specialist_id=?,started_at_ms=?,revision=revision+1 WHERE id=?", (actor, now, target))
            elif action == 'done' and row and row[0] == 'in_progress' and row[1] == actor:
                conn.execute("UPDATE requests SET status='closed',ended_at_ms=?,revision=revision+1 WHERE id=?", (now, target))
            else:
                raise ManagementError('Взять можно новую заявку; завершить — только свою заявку в работе.')
            log_change(conn, actor, key, 'menu_' + action, target, {})
            text, outcome = f'✅ Заявка #{target}: ' + ('взята в работу.' if action == 'take' else 'завершена.'), 'action_' + action
        except ManagementError as exc:
            text, outcome = str(exc), 'management_rejected'
        conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text) VALUES (0,?,?,?)''',
                     ('menu-result:' + str(key), chat, text))
        return outcome
    if action.startswith(('personal:', 'general:')):
        parts = action.split(':')
        command = '/my_stats' if parts[0] == 'personal' else '/stats'
        if parts[-1] == 'xlsx':
            command += '_xlsx'
        text = command + ' ' + (arguments if parts[1] == 'custom' else parts[1])
    else:
        text = ACTIONS[action][1] + (' ' + arguments if arguments else '')
    command = text.split()[0]
    if command in navigation.COMMANDS:
        return navigation.handle(conn, str(key), actor, chat, text, policy, private_admin=True)
    if command == '/request':
        return request_view.handle(conn, key, actor, chat, text, policy)
    if command in admin_requests.COMMANDS:
        return admin_requests.handle(conn, key, actor, chat, text, received_at, policy)
    if command.startswith(('/stats', '/my_stats')):
        return processor._statistics(conn, key, actor, chat, text, received_at)
    if command in ('/get_notice', '/set_notice', '/del_notice'):
        return processor._notices(conn, key, actor, chat, text, received_at)
    return processor._management(conn, key, actor, chat, text, received_at)


def result(conn, key, actor, chat, action, arguments, received_at, processor):
    outcome = execute(conn, key, actor, chat, action, arguments, received_at, processor)
    if not outcome.endswith('rejected'):
        clear(conn, actor, chat)
    buttons = []
    base = action.split(':')[0]
    if base in ('open', 'unassigned', 'mine') and not outcome.endswith('rejected'):
        page = int(action.split(':')[1]) if ':' in action else 1
        where, params = {
            'open': ("status IN ('waiting','new','in_progress')", ()),
            'unassigned': ("status='new' AND specialist_id IS NULL", ()),
            'mine': ("status='in_progress' AND specialist_id=?", (actor,)),
        }[base]
        count = conn.execute('SELECT count(*) FROM requests WHERE ' + where, params).fetchone()[0]
        if page > 1:
            buttons.append(('◀ Назад', f'{base}:{page-1}'))
        if page * 10 < count:
            buttons.append(('Далее ▶', f'{base}:{page+1}'))
    section = ('stats' if action.startswith(('personal:', 'general:')) else
               next((name for name, (_, actions) in SECTIONS.items() if base in actions), 'requests'))
    buttons += [('↩ К разделу', section), ('🏠 Главное меню', 'home')]
    send(conn, str(key) + ':footer', actor, chat,
         'Исправьте параметры и отправьте снова.' if outcome.endswith('rejected') else 'Выберите следующее действие.',
         buttons)
    return outcome


def message(conn, key, actor, chat, text, received_at, processor):
    if text.strip() in ('/menu', '/start', '/help', '/cancel'):
        return screen(conn, key, actor, chat, 'help' if text.strip() == '/help' else 'home', force_new=True)
    if text.lstrip().startswith('/'):
        clear(conn, actor, chat)
        return None
    pending = conn.execute('SELECT action,expires_at_ms FROM menu_sessions WHERE chat_id=? AND user_id=?', (chat, actor)).fetchone()
    if pending:
        if pending[1] <= int(time.time() * 1000):
            clear(conn, actor, chat)
            send(conn, key, actor, chat, '⌛ Время ввода истекло (15 минут). Выберите действие заново.', [('🏠 Меню', 'home')])
            return 'menu_input_expired'
        return result(conn, key, actor, chat, pending[0], text, received_at, processor)
    return screen(conn, key, actor, chat)


def callback(conn, key, event, received_at, processor):
    data, message = event['callback'], event.get('message') or {}
    if not isinstance(message, dict):
        return 'invalid_navigation_callback'
    user, recipient, body = data.get('user') or {}, message.get('recipient') or {}, message.get('body') or {}
    if not all(isinstance(item, dict) for item in (user, recipient, body)):
        return 'invalid_navigation_callback'
    actor, chat, mid = user.get('user_id'), recipient.get('chat_id'), body.get('mid')
    if (user.get('is_bot') is not False or type(actor) is not int or type(chat) is not int
            or not isinstance(mid, str) or recipient.get('chat_type') != 'dialog' or not is_staff(conn, actor)):
        return 'private_menu_only'
    row = conn.execute('''SELECT attachments_json FROM outbox WHERE message_id=? AND chat_id=?
        AND menu_owner=? AND deleted_at_ms IS NULL AND destination LIKE 'navigation:%' ''', (mid, chat, actor)).fetchone()
    if not row:
        return 'unknown_navigation_menu'
    if not any(b.get('payload') == data['payload'] for a in json.loads(row[0])
               for r in a.get('payload', {}).get('buttons', []) for b in r):
        return 'unknown_navigation_action'
    action = data['payload'][3:]
    if not allowed(conn, actor, action):
        clear(conn, actor, chat)
        return 'forbidden_management'
    conn.execute("INSERT INTO outbox(request_id,destination,chat_id,text,callback_id) VALUES (0,?,?,'',?)",
                 ('answer:' + data['callback_id'], chat, data['callback_id']))
    if action in ('home', 'stats', 'help', *SECTIONS):
        return screen(conn, key, actor, chat, action)
    if action == 'close_all':
        return result(conn, key, actor, chat, action, '', received_at, processor)
    prompt = ('Введите начальную и конечную даты: 2026-10-01 2026-10-31.'
              if ':custom' in action else ACTIONS.get(action, ('', '', ''))[2])
    clear(conn, actor, chat)
    if prompt:
        conn.execute('INSERT INTO menu_sessions VALUES (?,?,?,?)', (chat, actor, action, int(time.time() * 1000) + 900000))
        title = ACTIONS[action][0] if action in ACTIONS else '📊 Отчёт за период'
        send(conn, key, actor, chat, title + '\n\n' + prompt + '\n\nВремя ввода: 15 минут. /cancel — отменить.', [('↩ Отмена', 'home')])
        return 'menu_input'
    return result(conn, key, actor, chat, action, '', received_at, processor)
