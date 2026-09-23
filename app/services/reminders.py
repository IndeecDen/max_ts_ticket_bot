"""Transactional reminders: schedule progress and outbox pages commit together."""
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.roles import ManagementError, log_change

KEY = 'unassigned'
USAGE = ('Использование: /set_unassigned_reminder <интервал_мин> <дни 1–7 через запятую> '
         '<ЧЧ:ММ начала> <ЧЧ:ММ конца> <порог_мин>; или /set_unassigned_reminder off.')
DEFAULT = {'enabled': False, 'interval_minutes': 60, 'weekdays': [1, 2, 3, 4, 5],
           'work_start': '08:00', 'work_end': '17:00', 'threshold_minutes': 30}


def read_settings(conn):
    row = conn.execute('SELECT settings_json FROM schedules WHERE name=?', (KEY,)).fetchone()
    return json.loads(row[0]) if row else dict(DEFAULT)


def describe(settings, tz):
    if not settings['enabled']:
        return 'Напоминания о неназначенных заявках отключены.\n' + USAGE
    return (f"Напоминания: каждые {settings['interval_minutes']} мин.\n"
            f"Дни: {','.join(map(str, settings['weekdays']))}; "
            f"часы: {settings['work_start']}–{settings['work_end']} ({tz}).\n"
            f"Возраст заявки: более {settings['threshold_minutes']} мин.\n"
            'Для ночного интервала день недели относится к началу смены.')


def configure(conn, parts, now_ms, *, actor, event_id):
    previous = read_settings(conn)
    if parts == ['off']:
        settings = {**previous, 'enabled': False}
    else:
        if (len(parts) != 5 or not re.fullmatch(r'[0-9]{1,5}', parts[0])
                or not re.fullmatch(r'[1-7](,[1-7])*', parts[1])
                or not re.fullmatch(r'[0-9]{1,6}', parts[4])
                or not all(re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', t) for t in parts[2:4])):
            raise ManagementError(USAGE)
        interval, threshold = int(parts[0]), int(parts[4])
        if not 1 <= interval <= 10080 or not 1 <= threshold <= 525600 or parts[2] == parts[3]:
            raise ManagementError('Интервал: 1–10080 мин.; порог: 1–525600 мин.; начало и конец должны различаться.')
        settings = {'enabled': True, 'interval_minutes': interval,
                    'weekdays': sorted(set(map(int, parts[1].split(',')))),
                    'work_start': parts[2], 'work_end': parts[3], 'threshold_minutes': threshold}
    if settings != previous:
        conn.execute('''INSERT INTO schedules(name,settings_json,next_at_ms) VALUES (?,?,?)
            ON CONFLICT(name) DO UPDATE SET settings_json=excluded.settings_json,next_at_ms=excluded.next_at_ms''',
            (KEY, json.dumps(settings), now_ms))
        log_change(conn, actor, event_id, 'unassigned_reminder_set', 0, {'from': previous, 'to': settings})
    return settings


def in_window(settings, local):
    minute = local.hour * 60 + local.minute
    start, end = [int(t[:2]) * 60 + int(t[3:]) for t in (settings['work_start'], settings['work_end'])]
    if start < end:
        return local.isoweekday() in settings['weekdays'] and start <= minute < end
    # Midnight belongs to the shift that began the preceding calendar day.
    anchor = local if minute >= start else local - timedelta(days=1)
    return (minute >= start or minute < end) and anchor.isoweekday() in settings['weekdays']


def enqueue_reminders(conn, work_chat, tz, now_ms):
    row = conn.execute('SELECT settings_json,next_at_ms,run_number FROM schedules WHERE name=?', (KEY,)).fetchone()
    if row is None or now_ms < row[1]:
        return 0
    settings = json.loads(row[0])
    if not settings['enabled']:
        return 0
    local = datetime.fromtimestamp(now_ms / 1000, timezone.utc).astimezone(ZoneInfo(tz))
    if not in_window(settings, local):
        return 0
    # One outstanding batch at a time, including errors requiring reconciliation.
    if conn.execute("SELECT 1 FROM outbox WHERE destination LIKE 'unassigned:%' AND state<>'sent' LIMIT 1").fetchone():
        return 0
    conn.execute('UPDATE schedules SET next_at_ms=? WHERE name=?',
                 (now_ms + settings['interval_minutes'] * 60000, KEY))
    rows = conn.execute('''SELECT id,chat_id,created_at_ms FROM requests
        WHERE status='new' AND specialist_id IS NULL AND created_at_ms<? ORDER BY created_at_ms,id''',
        (now_ms - settings['threshold_minutes'] * 60000,)).fetchall()
    if not rows:
        return 0
    run = row[2] + 1
    header = (f"Заявки без исполнителя: {len(rows)}.\n"
              f"Состояние на {local.strftime('%Y-%m-%d %H:%M %z')} ({tz}).\n")
    pages, page = [], header
    for request_id, chat, created in rows:
        line = f'\n#{request_id} · чат {chat} · возраст {(now_ms - created) // 60000} мин.'
        if len((page + line).encode('utf-16-le')) // 2 > 3500:
            pages.append(page)
            page = header
        page += line
    pages.append(page)
    for index, page in enumerate(pages):
        conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text) VALUES (0,?,?,?)''',
                     (f'unassigned:{run}:{index}', work_chat, page + f'\nСтраница {index + 1}/{len(pages)}'))
    conn.execute('UPDATE schedules SET run_number=? WHERE name=?', (run, KEY))
    return len(pages)
