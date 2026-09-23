"""Scheduled deletion of known bot messages, without deleting request history."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.roles import ManagementError, log_change

KEY = 'autoclean'
DEFAULT = {'enabled': False, 'clean_time': '23:00', 'weekdays': [1, 2, 3, 4, 5, 6, 7]}
USAGE = 'Использование: /set_autoclean <ЧЧ:ММ> <дни 1–7 через запятую>; или /set_autoclean off.'


def read_settings(conn):
    row = conn.execute('SELECT settings_json FROM schedules WHERE name=?', (KEY,)).fetchone()
    return json.loads(row[0]) if row else dict(DEFAULT)


def describe(settings, tz):
    if not settings['enabled']:
        return 'Автоочистка отключена.\n' + USAGE
    return (f"Автоочистка: {settings['clean_time']} ({tz}); дни {','.join(map(str, settings['weekdays']))}.\n"
            'Удаляются только сохранённые сообщения бота, если нет открытых заявок. История в базе сохраняется.')


def configure(conn, parts, *, actor, event_id):
    previous = read_settings(conn)
    if parts == ['off']:
        settings = {**previous, 'enabled': False}
    else:
        if (len(parts) != 2 or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', parts[0])
                or not re.fullmatch(r'[1-7](,[1-7])*', parts[1])):
            raise ManagementError(USAGE)
        settings = {'enabled': True, 'clean_time': parts[0],
                    'weekdays': sorted(set(map(int, parts[1].split(','))))}
    if settings != previous:
        conn.execute('''INSERT INTO schedules(name,settings_json,next_at_ms) VALUES (?,?,0)
            ON CONFLICT(name) DO UPDATE SET settings_json=excluded.settings_json''', (KEY, json.dumps(settings)))
        log_change(conn, actor, event_id, 'autoclean_set', 0, {'from': previous, 'to': settings})
    return settings


def permitted(conn):
    return (read_settings(conn)['enabled'] and
            conn.execute("SELECT 1 FROM requests WHERE status IN ('waiting','new','in_progress') LIMIT 1").fetchone() is None and
            conn.execute("""SELECT 1 FROM inbox_events WHERE processed_at IS NULL
                AND update_type IN ('message_created','message_callback') LIMIT 1""").fetchone() is None)


def enqueue_cleanup(conn, tz, now_ms):
    row = conn.execute('SELECT settings_json,last_run_date FROM schedules WHERE name=?', (KEY,)).fetchone()
    if row is None or not permitted(conn):
        return 0
    settings = json.loads(row[0])
    local = datetime.fromtimestamp(now_ms / 1000, timezone.utc).astimezone(ZoneInfo(tz))
    date = local.date().isoformat()
    if (row[1] is not None and row[1] >= date) or local.isoweekday() not in settings['weekdays'] or local.strftime('%H:%M') < settings['clean_time']:
        return 0
    # Only a confirmed send with a known mid can become a deletion target.
    sources = conn.execute('''SELECT o.id,o.chat_id,o.message_id,o.revision FROM outbox o
        WHERE o.state='sent' AND o.message_id IS NOT NULL AND o.callback_id IS NULL
        AND o.delete_mid IS NULL AND o.deleted_at_ms IS NULL
        AND (o.request_id=0 OR EXISTS (SELECT 1 FROM requests r WHERE r.id=o.request_id AND r.status IN ('closed','cancelled')))
        AND NOT EXISTS (SELECT 1 FROM outbox d WHERE d.delete_source_id=o.id) ORDER BY o.id''').fetchall()
    for source, chat, mid, revision in sources:
        conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,delete_mid,delete_source_id,delete_source_revision)
            VALUES (0,?,?,'',?,?,?)''', (f'delete:{source}', chat, mid, source, revision))
    conn.execute('UPDATE schedules SET last_run_date=? WHERE name=?', (date, KEY))
    return len(sources)


def target_valid(conn, job):
    return conn.execute('''SELECT 1 FROM outbox WHERE id=? AND message_id=? AND revision=?
        AND state='sent' AND deleted_at_ms IS NULL AND delete_mid IS NULL''',
        (job['delete_source_id'], job['delete_mid'], job['delete_source_revision'])).fetchone() is not None


def mark_deleted(conn, job, now_ms):
    conn.execute('''UPDATE outbox SET deleted_at_ms=? WHERE id=? AND message_id=? AND revision=?''',
                 (now_ms, job['delete_source_id'], job['delete_mid'], job['delete_source_revision']))
