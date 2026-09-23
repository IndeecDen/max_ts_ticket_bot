"""Daily in-progress digest with a durable calendar-date checkpoint."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.roles import ManagementError, log_change

KEY = 'daily'
DEFAULT = {'enabled': False, 'remind_time': '09:00', 'weekdays': [1, 2, 3, 4, 5]}
USAGE = 'Использование: /set_daily_reminder <ЧЧ:ММ> <дни 1–7 через запятую>; или /set_daily_reminder off.'


def read_settings(conn):
    row = conn.execute('SELECT settings_json FROM schedules WHERE name=?', (KEY,)).fetchone()
    return json.loads(row[0]) if row else dict(DEFAULT)


def describe(settings, tz):
    if not settings['enabled']:
        return 'Ежедневный дайджест заявок в работе отключён.\n' + USAGE
    return (f"Ежедневный дайджест заявок в работе: {settings['remind_time']} ({tz}).\n"
            f"Дни: {','.join(map(str, settings['weekdays']))}.\n"
            'Не более одного снимка в день; после простоя — только за текущий день.')


def configure(conn, parts, *, actor, event_id):
    previous = read_settings(conn)
    if parts == ['off']:
        settings = {**previous, 'enabled': False}
    else:
        if (len(parts) != 2
                or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', parts[0])
                or not re.fullmatch(r'[1-7](,[1-7])*', parts[1])):
            raise ManagementError(USAGE)
        settings = {'enabled': True, 'remind_time': parts[0],
                    'weekdays': sorted(set(map(int, parts[1].split(','))))}
    if settings != previous:
        # Configuration changes never erase a completed day's checkpoint.
        conn.execute('''INSERT INTO schedules(name,settings_json,next_at_ms) VALUES (?,?,0)
            ON CONFLICT(name) DO UPDATE SET settings_json=excluded.settings_json''',
            (KEY, json.dumps(settings)))
        log_change(conn, actor, event_id, 'daily_reminder_set', 0, {'from': previous, 'to': settings})
    return settings


def enqueue_daily(conn, work_chat, tz, now_ms):
    row = conn.execute('SELECT settings_json,last_run_date,run_number FROM schedules WHERE name=?', (KEY,)).fetchone()
    if row is None:
        return 0
    settings = json.loads(row[0])
    if not settings['enabled']:
        return 0
    local = datetime.fromtimestamp(now_ms / 1000, timezone.utc).astimezone(ZoneInfo(tz))
    date = local.date().isoformat()
    # Lexicographic ISO dates also protect against a clock moving backwards.
    if (row[1] is not None and row[1] >= date) or local.isoweekday() not in settings['weekdays']:
        return 0
    if local.strftime('%H:%M') < settings['remind_time']:
        return 0
    if conn.execute("SELECT 1 FROM outbox WHERE destination LIKE 'daily:%' AND state<>'sent' LIMIT 1").fetchone():
        return 0
    rows = conn.execute('''SELECT id,chat_id,specialist_id,started_at_ms FROM requests
        WHERE status='in_progress' AND specialist_id IS NOT NULL ORDER BY started_at_ms,id''').fetchall()
    # An empty snapshot counts as the day's check and does not send a message.
    conn.execute('UPDATE schedules SET last_run_date=? WHERE name=?', (date, KEY))
    if not rows:
        return 0
    run = row[2] + 1
    header = (f'Ежедневный дайджест: заявок в работе — {len(rows)}.\n'
              f"Состояние на {local.strftime('%Y-%m-%d %H:%M %z')} ({tz}).\n")
    pages, page = [], header
    for request_id, chat, specialist, started in rows:
        duration = f'{(now_ms - started) // 60000} мин.' if started is not None and started <= now_ms else 'нет данных'
        line = f'\n#{request_id} · чат {chat} · специалист {specialist} · в работе: {duration}'
        if len((page + line).encode('utf-16-le')) // 2 > 3500:
            pages.append(page)
            page = header
        page += line
    pages.append(page)
    for index, page in enumerate(pages):
        conn.execute('INSERT INTO outbox(request_id,destination,chat_id,text) VALUES (0,?,?,?)',
                     (f'daily:{run}:{index}', work_chat, page + f'\nСтраница {index + 1}/{len(pages)}'))
    conn.execute('UPDATE schedules SET run_number=? WHERE name=?', (run, KEY))
    return len(pages)
