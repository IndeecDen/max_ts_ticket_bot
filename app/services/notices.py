"""Independent scheduled notices attached to new client request cards."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.roles import ManagementError, log_change

USAGE = ('Использование: /set_notice <ГГГГ-ММ-ДДTЧЧ:ММ или -> '
         '<ЧЧ:ММ-ЧЧ:ММ или -> <текст до 300 символов UTF-16>.')
TIME = r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'


def add(conn, text, tz, now_ms, *, actor, event_id):
    parts = text.split(maxsplit=3)
    if len(parts) != 4:
        raise ManagementError(USAGE)
    _, expiry, window, body = parts
    if not body.strip() or len(body.encode('utf-16-le')) // 2 > 300:
        raise ManagementError('Текст объявления: от 1 до 300 символов UTF-16.')
    expires = None
    if expiry != '-':
        try:
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}', expiry):
                raise ValueError
            naive = datetime.strptime(expiry, '%Y-%m-%dT%H:%M')
            zone = ZoneInfo(tz)
            candidates = set()
            for fold in (0, 1):
                local = naive.replace(tzinfo=zone, fold=fold)
                utc = local.astimezone(timezone.utc)
                if utc.astimezone(zone).replace(tzinfo=None) == naive:
                    candidates.add(int(utc.timestamp() * 1000))
            if len(candidates) != 1:
                raise ValueError
            expires = candidates.pop()
            if expires <= now_ms:
                raise ValueError
        except (ValueError, OverflowError, OSError):
            raise ManagementError('Укажите будущую дату ГГГГ-ММ-ДДTЧЧ:ММ в TIMEZONE; время должно существовать и быть однозначным.') from None
    start = end = None
    if window != '-':
        if not re.fullmatch(TIME + '-' + TIME, window):
            raise ManagementError(USAGE)
        start, end = window.split('-')
        if start == end:
            raise ManagementError('Начало и конец интервала должны различаться; для круглосуточного показа укажите -.')
    if conn.execute('SELECT count(*) FROM announcements').fetchone()[0] >= 10:
        raise ManagementError('Максимум 10 объявлений. Удалите ненужные через /del_notice <ID>.')
    notice_id = conn.execute('''INSERT INTO announcements(text,expires_at_ms,active_from,active_to,created_by,created_at_ms)
        VALUES (?,?,?,?,?,?)''', (body, expires, start, end, actor, now_ms)).lastrowid
    log_change(conn, actor, event_id, 'notice_add', notice_id,
               {'expires_at_ms': expires, 'active_from': start, 'active_to': end})
    return notice_id


def remove(conn, target, *, actor, event_id):
    if target != 'all' and not re.fullmatch(r'[1-9][0-9]{0,17}', target):
        raise ManagementError('Использование: /del_notice <ID> или /del_notice all.')
    ids = [r[0] for r in conn.execute('SELECT id FROM announcements')
           if target == 'all' or r[0] == int(target)]
    for notice_id in ids:
        conn.execute('DELETE FROM announcements WHERE id=?', (notice_id,))
    if ids:
        log_change(conn, actor, event_id, 'notice_remove', 0, {'ids': ids})
    return len(ids)


def list_pages(conn, tz):
    rows = conn.execute('SELECT id,text,expires_at_ms,active_from,active_to FROM announcements ORDER BY id').fetchall()
    if not rows:
        return ['Объявлений нет.\n' + USAGE]
    pages = []
    for notice_id, text, expires, start, end in rows:
        expiry = (datetime.fromtimestamp(expires / 1000, timezone.utc).astimezone(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M %z')
                  if expires is not None else 'без срока')
        window = f'{start}–{end}' if start else 'круглосуточно'
        pages.append(f'Объявление #{notice_id}\nСрок: {expiry}; часы: {window} ({tz}).\n{text}')
    return pages


def active_texts(conn, tz, now_ms):
    rows = conn.execute('''SELECT text,active_from,active_to FROM announcements
        WHERE expires_at_ms IS NULL OR expires_at_ms>? ORDER BY id''', (now_ms,)).fetchall()
    if not rows:
        return []
    minute = datetime.fromtimestamp(now_ms / 1000, timezone.utc).astimezone(ZoneInfo(tz)).strftime('%H:%M')
    return [text for text, start, end in rows if start is None or
            (start <= minute < end if start < end else minute >= start or minute < end)]
