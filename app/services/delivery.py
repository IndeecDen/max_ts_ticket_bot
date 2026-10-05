"""Transactional queue with conservative handling of ambiguous POST results."""
import asyncio
import json
import html
import re
from urllib.parse import urlsplit
import sqlite3
import time
import tempfile
from pathlib import Path

from app.services import autoclean
from app.services.notices import active_texts
from app.services.user_names import display_name
from app.adapters.max.client import MaxAPIError
from app.storage.delivery_lock import DeliveryLock, DeliveryBusy


def clip(text, limit=3900):
    raw = text.encode('utf-16-le')
    if len(raw) <= limit * 2:
        return text
    return raw[:(limit - 1) * 2].decode('utf-16-le', errors='ignore') + '…'


def render_work_card(text, author_id, chat_info):
    lines = text.split('\n')
    if len(lines) < 4:
        return html.escape(text)
    author = lines[1].removeprefix('Автор: ')
    title = chat_info.get('title') or lines[2].removeprefix('Чат: ')
    url = chat_info.get('link')
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        safe = parsed and parsed.scheme == 'https' and parsed.hostname == 'max.ru' and not parsed.username and not parsed.password and len(url) < 512
    except ValueError:
        safe = False
    title = html.escape(str(title)[:200])
    chat = f'<a href="{html.escape(url, quote=True)}">{title}</a>' if safe else title
    header = f'{html.escape(lines[0])}\nАвтор: <a href="max://user/{author_id}">{html.escape(author[:200])}</a>\nЧат: {chat}\n'
    body = ''
    for char in '\n'.join(lines[3:]):
        escaped = html.escape(char)
        if len((header + body + escaped).encode('utf-16-le')) // 2 > 3899:
            body += '…'
            break
        body += escaped
    return header + body


def enqueue_cards(conn, work_chat, *, now_ms=None, timezone="Europe/Moscow"):
    rows = conn.execute("""SELECT id,chat_id,author_name,status,specialist_id,revision FROM requests
        WHERE (status IN ('new','in_progress','closed') OR
        (status='cancelled' AND EXISTS(SELECT 1 FROM outbox WHERE request_id=requests.id)))
        AND (SELECT count(*) FROM outbox WHERE request_id=requests.id
             AND destination IN ('work','client') AND revision>=requests.revision)<2""").fetchall()
    labels = {'new': 'Ожидает специалиста', 'in_progress': 'В работе',
              'closed': 'Завершена', 'cancelled': 'Отменена'}
    for request_id, chat, author, status, specialist, revision in rows:
        first_card = conn.execute("SELECT 1 FROM outbox WHERE request_id=? AND destination='work'", (request_id,)).fetchone() is None
        snippets = []
        forwards = []
        # The work card and optional original contain only the first message.
        # Supplements remain accessible through the full request view.
        for event_id, raw in conn.execute('SELECT event_id,payload_json FROM request_messages WHERE request_id=? ORDER BY event_id LIMIT 1', (request_id,)):
            message = json.loads(raw)['message']
            body = message.get('body') or {}
            snippets.append(body.get('text') or '[Сообщение без текста: вложение или пересылка]')
            mid = body.get('mid')
            link = message.get('link')
            if (body.get('attachments') or (isinstance(link, dict) and link.get('type') == 'forward')) and isinstance(mid, str) and mid.strip():
                forwards.append((event_id, mid))
        specialist_name = display_name(conn, specialist) if specialist else 'Специалист'
        assigned = f'\nСпециалист: {specialist_name}' if specialist else ''
        work = clip(f'Заявка #{request_id}\nАвтор: {author}\nЧат: {chat}\n{labels[status]}{assigned}\n\n' + '\n'.join(snippets))
        client_status = {
            'new': 'Заявка ожидает специалиста 👨‍🔧',
            'in_progress': f'Заявка в работе у 👨‍🔧 "{specialist_name}"',
            'closed': f'Заявка закрыта. Спасибо за обращение!👨‍🔧 "{specialist_name}"',
            'cancelled': 'Заявка отменена.',
        }[status]
        client = f'Заявка #{request_id}\n{client_status}'
        if status == 'new':
            notices = active_texts(conn, timezone, int(time.time() * 1000) if now_ms is None else now_ms)
            if notices:
                client += '\n\n' + '\n\n'.join('📢 ' + notice for notice in notices)
        for destination, target, text in [('work', work_chat, work), ('client', chat, client)]:
            action = ('take' if status == 'new' else 'done' if status == 'in_progress' else None) if destination == 'work' else ('cancel' if status == 'new' else None)
            attachments = []
            if action:
                attachments = [{'type': 'inline_keyboard', 'payload': {'buttons': [[{
                    'type': 'callback', 'text': {'take': 'Взять', 'done': 'Завершить', 'cancel': 'Отменить'}[action],
                    'payload': f'{action}:{request_id}:{revision}'}]]}}]
            conn.execute("""INSERT INTO outbox(request_id,destination,chat_id,text,revision,attachments_json)
                VALUES (?,?,?,?,?,?) ON CONFLICT(request_id,destination) DO UPDATE SET
                text=excluded.text,revision=excluded.revision,attachments_json=excluded.attachments_json,
                state=CASE WHEN outbox.state='sent' THEN 'pending' ELSE outbox.state END,
                attempts=CASE WHEN outbox.state='sent' THEN 0 ELSE outbox.attempts END,
                next_at_ms=CASE WHEN outbox.state='sent' THEN 0 ELSE outbox.next_at_ms END
                WHERE outbox.revision<excluded.revision""",
                (request_id, destination, target, text, revision, json.dumps(attachments, ensure_ascii=False)))
        # Only newly published cards enqueue originals. Editing old cards must not
        # backfill historical media or re-send originals after restart.
        if first_card and status == 'new':
            for event_id, mid in forwards:
                conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,forward_mid)
                    VALUES (?,?,?,?,?) ON CONFLICT(request_id,destination) DO NOTHING''',
                    (request_id, f'original:{event_id}', work_chat,
                     f'Заявка #{request_id}: исходное сообщение с вложением или пересылкой.', mid))



class DeliveryQueue:
    def __init__(self, store):
        self.store = store
        self.active_jobs = set()
        self.chat_cache = {}

    def _claim(self, now):
        lock = None
        try:
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.row_factory = sqlite3.Row
                cleanup_allowed = autoclean.permitted(conn)
                rows = conn.execute('''SELECT o.* FROM outbox o
                    LEFT JOIN delivery_slots s ON s.chat_id=o.chat_id
                    WHERE o.state='pending' AND o.next_at_ms<=? AND coalesce(s.next_at_ms,0)<=?
                    AND (o.delete_mid IS NULL OR ?)
                    AND NOT EXISTS (SELECT 1 FROM outbox busy WHERE busy.chat_id=o.chat_id AND busy.state='sending')
                     ORDER BY (o.callback_id IS NOT NULL) DESC,o.id LIMIT 100''', (now, now, cleanup_allowed)).fetchall()
                row = None
                for candidate in rows:
                    if candidate['delete_mid']:
                        if not cleanup_allowed:
                            continue
                        if not autoclean.target_valid(conn, candidate):
                            conn.execute("UPDATE outbox SET state='failed',error_kind='delete_target_changed' WHERE id=?", (candidate['id'],))
                            continue
                    try:
                        lock = DeliveryLock(self.store.path, candidate['id'])
                    except DeliveryBusy:
                        continue
                    row = candidate
                    break
                if row is None:
                    return None
                conn.execute("UPDATE outbox SET state='sending',attempts=attempts+1,attempted_revision=revision WHERE id=?", (row['id'],))
                conn.execute('''INSERT INTO delivery_slots VALUES (?,?) ON CONFLICT(chat_id)
                    DO UPDATE SET next_at_ms=excluded.next_at_ms''', (row['chat_id'], now + 1000))
                job = dict(row)
                job['attempts'] += 1
                job['_lock'] = lock
                return job
        except BaseException:
            if lock is not None:
                lock.close()
            raise

    def _finish(self, job, state, now, mid=None, error=None, delay=0):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("""UPDATE outbox SET state=CASE WHEN ?='sent' AND revision>? THEN 'pending' ELSE ? END,
                message_id=coalesce(?,message_id),error_kind=?,next_at_ms=?
                WHERE id=? AND state='sending'""", (state, job['revision'], state, mid, error, now + delay, job['id']))
            if state == 'sent' and job['delete_mid']:
                autoclean.mark_deleted(conn, job, now)
            conn.execute('UPDATE delivery_slots SET next_at_ms=max(next_at_ms,?) WHERE chat_id=?',
                         (now + max(1000, delay), job['chat_id']))

    @staticmethod
    def _report_bytes(raw):
        from app.services.excel_export import export_statistics
        with tempfile.TemporaryDirectory(prefix='max-report-') as directory:
            path = export_statistics(json.loads(raw), Path(directory) / 'report.xlsx')
            if path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError('report_too_large')
            return path.read_bytes()

    def _save_file_token(self, job, token):
        with self.store.connect() as conn:
            changed = conn.execute("UPDATE outbox SET file_token=? WHERE id=? AND state='sending'",
                                   (token, job['id'])).rowcount
            if changed != 1:
                raise RuntimeError('File job changed during upload')

    async def deliver_one(self, client, clock=None):
        clock = clock or (lambda: int(time.time() * 1000))
        claim = asyncio.create_task(asyncio.to_thread(self._claim, clock()))
        try:
            job = await asyncio.shield(claim)
        except asyncio.CancelledError:
            job = await claim
            if job is not None:
                job['_lock'].close()
            raise
        if job is None:
            return None
        self.active_jobs.add(job['id'])
        try:
            try:
                if job['report_json'] and not job['file_token']:
                    try:
                        content = await asyncio.to_thread(self._report_bytes, job['report_json'])
                    except (OSError, ValueError):
                        await asyncio.to_thread(self._finish, job, 'failed', clock(), None, 'report_generation')
                        return 'failed'
                    token = await client.upload_file(content, f"statistics_{job['id']}.xlsx")
                    await asyncio.to_thread(self._save_file_token, job, token)
                    # Allow MAX to process the uploaded file; durable delay survives restart.
                    await asyncio.to_thread(self._finish, job, 'pending', clock(), None, None, 5000)
                    return 'pending'
                attachments = ([{'type': 'file', 'payload': {'token': job['file_token']}}]
                               if job['report_json'] else json.loads(job['attachments_json']))
                text = job['text']
                formatting = {'format': job['text_format']} if job['text_format'] else {}
                if job['destination'].startswith('navigation:') and job['text_format'] == 'html' and callable(getattr(client, 'get_chat', None)):
                    for source_chat in set(re.findall(r'^Чат: (-?\d+)$', text, re.MULTILINE)):
                        source_id = int(source_chat)
                        info = self.chat_cache.get(source_id)
                        if info is None:
                            try:
                                info = await client.get_chat(source_id)
                            except MaxAPIError:
                                info = {}
                            if isinstance(info, dict) and info:
                                self.chat_cache[source_id] = info
                        if isinstance(info, dict):
                            # Reuse the card link renderer, including escaping and URL validation.
                            link_info = dict(info)
                            link_info['title'] = str(info.get('title') or source_chat)[:80]
                            if len(str(link_info.get('link') or '')) > 256:
                                link_info['link'] = None
                            title_line = render_work_card(f'Заявка\nАвтор: A\nЧат: {source_chat}\nСтатус', 1, link_info).split('\n')[2]
                            text = re.sub(r'^Чат: ' + re.escape(source_chat) + r'$', lambda _: title_line, text, flags=re.MULTILINE)
                if job['destination'] == 'work' and job['request_id'] and callable(getattr(client, 'get_chat', None)):
                    with self.store.connect() as conn:
                        source = conn.execute('SELECT chat_id,user_id FROM requests WHERE id=?', (job['request_id'],)).fetchone()
                    if source:
                        info = self.chat_cache.get(source[0])
                        if info is None:
                            try:
                                info = await client.get_chat(source[0])
                            except MaxAPIError:
                                info = {}
                            if isinstance(info, dict) and info:
                                self.chat_cache[source[0]] = info
                        if isinstance(info, dict):
                            text = render_work_card(text, source[1], info)
                            formatting = {'format': 'html'}
                if job['delete_mid']:
                    await client.delete_message(job['delete_mid'])
                    mid = None
                elif job['callback_id']:
                    await client.answer_callback(job['callback_id'])
                    mid = None
                elif job['message_id']:
                    mid = await client.edit_message(job['message_id'], text, attachments, **formatting)
                elif job['forward_mid']:
                    mid = await client.send_message(job['chat_id'], job['text'], forward_mid=job['forward_mid'])
                else:
                    mid = await client.send_message(job['chat_id'], text, attachments, **formatting)
            except MaxAPIError as exc:
                state = 'uncertain' if exc.uncertain else ('pending' if exc.retryable else 'failed')
                if state == 'pending' and job['attempts'] >= 10:
                    state = 'failed'
                # Reject implausible server delays instead of overflowing SQLite or
                # retrying sooner than the advertised Retry-After.
                if state == 'pending' and (exc.retry_after or 0) > 86400:
                    state = 'failed'
                delay = max(1000, int((exc.retry_after or 0) * 1000),
                            min(3600000, 5000 * 2 ** min(job['attempts'] - 1, 10))) if state == 'pending' else 0
                await asyncio.to_thread(self._finish, job, state, clock(), None,
                                        f'http_{exc.status}' if exc.status else 'transport_or_response', delay)
                return state
            # Cancellation or process death leaves 'sending' for manual reconciliation.
            # Never automatically repeat a POST whose delivery cannot be established.
            await asyncio.to_thread(self._finish, job, 'sent', clock(), mid)
            return 'sent'
        finally:
            self.active_jobs.discard(job['id'])
            job['_lock'].close()

    async def status(self):
        def read():
            with self.store.connect() as conn:
                return dict(conn.execute('SELECT state,count(*) FROM outbox GROUP BY state'))
        return await asyncio.to_thread(read)
