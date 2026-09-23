"""Administrative request transitions inside the inbox transaction."""
import re
from datetime import datetime
from app.services.roles import has_role, reassign, log_change, ManagementError

COMMANDS = frozenset(('/assign_request','/cancel_request','/close_all'))


def request_id(raw):
    if not re.fullmatch(r'[1-9][0-9]{0,18}', raw) or int(raw) >= 2**63:
        raise ManagementError('Номер заявки должен быть положительным целым int64.')
    return int(raw)


def handle(conn, event_id, actor, chat, text, received_at, policy):
    if chat != policy.work_chat:
        return 'deferred_command' if chat in policy.client_chats else 'ignored_chat'
    if not has_role(conn, actor, 'admin'):
        return 'forbidden_management'
    parts = text.split()
    now = int(datetime.fromisoformat(received_at).timestamp()*1000)
    try:
        if parts[0] == '/assign_request':
            if len(parts) != 3:
                raise ManagementError('Использование: /assign_request <заявка> <MAX ID специалиста>.')
            target = request_id(parts[1])
            if not re.fullmatch(r'-?[0-9]{1,19}',parts[2]) or not -(2**63) <= int(parts[2]) < 2**63 or int(parts[2]) == 0:
                raise ManagementError('Некорректный MAX ID специалиста.')
            specialist = int(parts[2])
            if not has_role(conn, specialist, 'specialist'):
                raise ManagementError('Исполнитель должен иметь роль specialist.')
            row = conn.execute('SELECT status,specialist_id FROM requests WHERE id=?',(target,)).fetchone()
            if row is None or row[0] not in ('new','in_progress'):
                raise ManagementError('Назначить можно только новую заявку или заявку в работе.')
            if row[0] == 'in_progress':
                changed = reassign(conn,target,specialist,actor=actor,event_id=event_id)
                reply = 'Исполнитель изменён.' if changed else 'Этот специалист уже назначен.'
            else:
                conn.execute("""UPDATE requests SET status='in_progress',specialist_id=?,started_at_ms=?,
                    revision=revision+1 WHERE id=?""",(specialist,now,target))
                log_change(conn,actor,event_id,'assign_request',target,{'from':row[1],'to':specialist})
                reply = f'Заявка #{target} назначена специалисту {specialist} и переведена в работу.'
        elif parts[0] == '/cancel_request':
            fields = text.split(maxsplit=2)
            if len(fields) != 3 or not fields[2].strip() or len(fields[2]) > 300:
                raise ManagementError('Использование: /cancel_request <заявка> <причина до 300 символов>.')
            target = request_id(fields[1])
            row = conn.execute('SELECT status FROM requests WHERE id=?',(target,)).fetchone()
            if row is None or row[0] not in ('waiting','new','in_progress'):
                raise ManagementError('Отменить можно только незавершённую заявку.')
            conn.execute("UPDATE requests SET status='cancelled',ended_at_ms=?,revision=revision+1 WHERE id=?",(now,target))
            log_change(conn,actor,event_id,'admin_cancel',target,{'from':row[0],'reason':fields[2].strip()})
            reply = f'Заявка #{target} отменена. Причина сохранена в журнале.'
        elif parts == ['/close_all']:
            rows = conn.execute("SELECT id,status FROM requests WHERE status IN ('new','in_progress') ORDER BY id").fetchall()
            conn.execute("UPDATE requests SET status='closed',ended_at_ms=?,revision=revision+1 WHERE status IN ('new','in_progress')",(now,))
            for target, previous in rows:
                log_change(conn,actor,event_id,'admin_close_all',target,{'from':previous})
            reply = f'Закрыто заявок: {len(rows)}. Обращения на стадии ожидания не изменены.'
        else:
            raise ManagementError('Использование: /close_all без параметров.')
        outcome = 'management_done'
    except ManagementError as exc:
        reply, outcome = str(exc),'management_rejected'
    conn.execute('INSERT INTO outbox(request_id,destination,chat_id,text) VALUES (0,?,?,?)',
                 (f'admin_request:{event_id}',chat,reply))
    return outcome
