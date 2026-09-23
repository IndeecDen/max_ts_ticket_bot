"""Closed-ticket statistics by completion time, with explicit calendar boundaries."""
import asyncio
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.roles import ManagementError


def period_bounds(parts, timezone_name, now=None):
    zone = ZoneInfo(timezone_name)
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(zone).date()
    try:
        if len(parts) == 1 and parts[0] in ('day', 'week', 'month'):
            days = {'day': 1, 'week': 7, 'month': 30}[parts[0]]
            start, end = today - timedelta(days=days - 1), today
        elif len(parts) == 2 and all(re.fullmatch(r'\d{4}-\d{2}-\d{2}', p) for p in parts):
            start, end = map(date.fromisoformat, parts)
        else:
            raise ValueError
        if start > end:
            raise ValueError
        following = end + timedelta(days=1)
        lower = datetime.combine(start, time.min, zone)
        upper = datetime.combine(following, time.min, zone)
        return {'from_ms': int(lower.timestamp() * 1000), 'until_ms': int(upper.timestamp() * 1000),
                'start_date': start.isoformat(), 'end_date': end.isoformat(), 'timezone': timezone_name}
    except (ValueError, OverflowError, OSError):
        raise ManagementError('Период: day, week, month или две даты ГГГГ-ММ-ДД (начало и конец включительно).') from None


def read_statistics(conn, period, specialist_id=None):
    if specialist_id is not None and (type(specialist_id) is not int or specialist_id == 0 or not -(2**63) <= specialist_id < 2**63):
        raise ManagementError('Некорректный MAX ID специалиста.')
    query = '''SELECT chat_id,specialist_id,count(*),
        coalesce(sum(CASE WHEN started_at_ms>=created_at_ms THEN started_at_ms-created_at_ms END),0),
        count(CASE WHEN started_at_ms>=created_at_ms THEN 1 END),
        coalesce(sum(CASE WHEN ended_at_ms>=started_at_ms THEN ended_at_ms-started_at_ms END),0),
        count(CASE WHEN ended_at_ms>=started_at_ms THEN 1 END)
        FROM requests WHERE status='closed' AND ended_at_ms>=? AND ended_at_ms<?'''
    params = [period['from_ms'], period['until_ms']]
    if specialist_id is not None:
        query += ' AND specialist_id=?'
        params.append(specialist_id)
    query += ' GROUP BY chat_id,specialist_id ORDER BY chat_id,specialist_id'
    groups = []
    for chat, specialist, count, wait_ms, wait_count, work_ms, work_count in conn.execute(query, params):
        groups.append({'chat_id': chat, 'specialist_id': specialist, 'closed': count,
                       'wait_ms': wait_ms, 'wait_samples': wait_count,
                       'work_ms': work_ms, 'work_samples': work_count})
    totals = {key: sum(g[key] for g in groups) for key in ('closed', 'wait_ms', 'wait_samples', 'work_ms', 'work_samples')}
    return {'period': period, 'specialist_id': specialist_id, 'totals': totals, 'groups': groups}


def duration(milliseconds):
    if milliseconds is None:
        return 'нет данных'
    seconds = int(milliseconds // 1000)
    return f'{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def render_statistics(report):
    period, totals = report['period'], report['totals']
    scope = 'Общая статистика' if report['specialist_id'] is None else f"Статистика специалиста {report['specialist_id']}"
    lines = [scope, f"Завершены {period['start_date']} — {period['end_date']} ({period['timezone']})",
             f"Закрыто: {totals['closed']}",
             'Среднее ожидание: ' + duration(totals['wait_ms'] / totals['wait_samples'] if totals['wait_samples'] else None),
             'Среднее выполнение: ' + duration(totals['work_ms'] / totals['work_samples'] if totals['work_samples'] else None),
             f"Измерений ожидания/выполнения: {totals['wait_samples']}/{totals['work_samples']}",
             'По чатам и исполнителям:']
    for group in report['groups']:
        specialist = group['specialist_id'] if group['specialist_id'] is not None else 'не указан'
        lines.append(f"Чат {group['chat_id']}, специалист {specialist}: {group['closed']}; "
                     f"ожидание Σ {duration(group['wait_ms'])}; выполнение Σ {duration(group['work_ms'])}")
    if not report['groups']:
        lines.append('Нет завершённых заявок за период.')
    pages, page = [], ''
    for line in lines:
        candidate = page + ('\n' if page else '') + line
        if len(candidate.encode('utf-16-le')) // 2 > 3500:
            pages.append(page)
            page = line
        else:
            page = candidate
    if page:
        pages.append(page)
    return [f'{page}\nСтраница {i}/{len(pages)}' for i, page in enumerate(pages, 1)]


class Statistics:
    def __init__(self, store):
        self.store = store

    async def read(self, parts, timezone_name, specialist_id=None, now=None):
        period = period_bounds(parts, timezone_name, now)
        def read():
            with self.store.connect() as conn:
                conn.execute('BEGIN')
                return read_statistics(conn, period, specialist_id)
        return await asyncio.to_thread(read)
