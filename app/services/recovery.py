"""Explicit offline reconciliation; no automatic assumptions about delivery."""
import asyncio
import sqlite3
import time

from app.services import autoclean
from app.storage.delivery_lock import DeliveryLock


class RecoveryError(ValueError):
    pass


class QueueRecovery:
    def __init__(self, store):
        self.store = store

    async def inspect(self, job_id=None):
        if job_id is not None and (type(job_id) is not int or not 0 < job_id < 2**63):
            raise RecoveryError('ID задания должен быть положительным числом int64.')
        def read():
            with self.store.connect() as conn:
                conn.row_factory = sqlite3.Row
                columns = '''id,request_id,CASE WHEN callback_id IS NULL THEN destination ELSE 'callback' END AS destination,
                    chat_id,state,revision,attempted_revision,attempts,next_at_ms,message_id,error_kind,
                    delete_mid,delete_source_id,deleted_at_ms'''
                if job_id is not None:
                    return [dict(r) for r in conn.execute(f'SELECT {columns} FROM outbox WHERE id=?', (job_id,))]
                return [dict(r) for r in conn.execute(f'''SELECT {columns} FROM outbox
                    WHERE state IN ('failed','uncertain','sending') ORDER BY id LIMIT 100''')]
        return await asyncio.to_thread(read)

    async def recover(self, job_id, action, expected_revision, *, reason, mid=None,
                      delivered_revision=None, confirm_not_delivered=False):
        if (type(job_id) is not int or not 0 < job_id < 2**63 or
                type(expected_revision) is not int or not 0 <= expected_revision < 2**63):
            raise RecoveryError('Укажите положительный ID задания и его текущую версию.')
        if action not in ('retry', 'confirm'):
            raise RecoveryError('Неизвестное действие восстановления.')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
            raise RecoveryError('Нужна причина восстановления длиной до 300 символов.')
        if mid is not None and (not isinstance(mid, str) or not mid.strip() or len(mid) > 1000):
            raise RecoveryError('Некорректный mid сообщения.')
        return await asyncio.to_thread(self._recover, job_id, action, expected_revision,
                                       reason, mid, delivered_revision, confirm_not_delivered)

    def _recover(self, job_id, action, expected_revision, reason, mid, delivered_revision, confirm_not_delivered):
        with DeliveryLock(self.store.path, job_id), self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.row_factory = sqlite3.Row
            job = conn.execute('SELECT * FROM outbox WHERE id=?', (job_id,)).fetchone()
            if job is None:
                raise RecoveryError('Задание не найдено.')
            if job['state'] not in ('failed', 'uncertain', 'sending'):
                raise RecoveryError('Восстанавливать можно только failed, uncertain или прерванное sending.')
            if job['revision'] != expected_revision:
                raise RecoveryError('Версия задания изменилась: сначала перечитайте queue-inspect.')
            target_mid = job['message_id']
            if action == 'confirm':
                if type(delivered_revision) is not int or not 0 <= delivered_revision <= expected_revision:
                    raise RecoveryError('Укажите версию карточки, фактически найденной в MAX.')
                if job['delete_mid']:
                    if mid is not None or delivered_revision != 0:
                        raise RecoveryError('Для подтверждения удаления не задают mid; версия равна 0.')
                    autoclean.mark_deleted(conn, job, int(time.time() * 1000))
                elif job['callback_id']:
                    if mid is not None or delivered_revision != 0:
                        raise RecoveryError('Для callback не задают mid; версия ответа равна 0.')
                else:
                    if mid is None:
                        raise RecoveryError('Для подтверждения карточки нужен её mid из MAX.')
                    if target_mid is not None and target_mid != mid:
                        raise RecoveryError('mid отличается от сохранённой карточки.')
                    other = conn.execute('SELECT 1 FROM outbox WHERE message_id=? AND id<>? LIMIT 1', (mid, job_id)).fetchone()
                    if other:
                        raise RecoveryError('Этот mid уже привязан к другому заданию.')
                    target_mid = mid
                state = 'sent' if delivered_revision == expected_revision else 'pending'
            else:
                if mid is not None or delivered_revision is not None:
                    raise RecoveryError('Для retry не задают mid и delivered-revision.')
                if job['state'] in ('uncertain', 'sending') and not target_mid and not job['delete_mid'] and not confirm_not_delivered:
                    raise RecoveryError('Для повторной отправки с неизвестным результатом подтвердите отсутствие доставки: --confirm-not-delivered.')
                state = 'pending'
            # Keep the current text, buttons, revision, recipient and rate-limit slot.
            conn.execute('''UPDATE outbox SET state=?,message_id=?,attempts=0,
                error_kind=NULL WHERE id=?''', (state, target_mid, job_id))
            conn.execute('''INSERT INTO recovery_log
                (job_id,action,previous_state,revision,delivered_revision,message_id,reason,created_at_ms)
                VALUES (?,?,?,?,?,?,?,?)''', (job_id, action, job['state'], expected_revision,
                delivered_revision, target_mid, reason.strip(), int(time.time() * 1000)))
            return state
