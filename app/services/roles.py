"""Local bot roles, independent of messenger chat administrators."""
import asyncio
import json
import time

from app.config import ConfigError


class ManagementError(ValueError):
    pass


def has_role(conn, user_id, role):
    return conn.execute('SELECT 1 FROM bot_roles WHERE user_id=? AND role=?', (user_id, role)).fetchone() is not None


def log_change(conn, actor, event_id, action, target, details):
    conn.execute('''INSERT INTO management_log(actor_id,event_id,action,target_id,details,created_at_ms)
        VALUES (?,?,?,?,?,?)''', (actor, event_id, action, target, json.dumps(details), int(time.time() * 1000)))


def bootstrap_roles(conn, policy):
    if conn.execute("SELECT 1 FROM bot_meta WHERE key='roles_initialized'").fetchone():
        return
    if not policy.specialists and not policy.admins:
        raise ConfigError('Первый запуск: задайте --specialist/--bot-admin или выдайте роли через role-grant.')
    for role, users in [('specialist', policy.specialists), ('admin', policy.admins)]:
        for user in sorted(users):
            change_role(conn, user, role, True, source='bootstrap')
    conn.execute("INSERT OR IGNORE INTO bot_meta VALUES ('roles_initialized','1')")


def change_role(conn, user_id, role, grant, *, actor=None, event_id=None, source='local'):
    if type(user_id) is not int or not -(2**63) <= user_id < 2**63 or user_id == 0 or role not in ('admin', 'specialist'):
        raise ManagementError('Укажите корректный MAX ID и роль admin или specialist.')
    exists = has_role(conn, user_id, role)
    if not grant and role == 'admin' and exists:
        if conn.execute("SELECT count(*) FROM bot_roles WHERE role='admin'").fetchone()[0] == 1:
            raise ManagementError('Нельзя удалить последнего администратора бота. Сначала назначьте другого.')
    if grant:
        conn.execute('INSERT OR IGNORE INTO bot_roles VALUES (?,?)', (user_id, role))
    else:
        conn.execute('DELETE FROM bot_roles WHERE user_id=? AND role=?', (user_id, role))
    conn.execute("INSERT OR IGNORE INTO bot_meta VALUES ('roles_initialized','1')")
    changed = exists != grant
    if changed:
        log_change(conn, actor, event_id, 'grant' if grant else 'revoke', user_id, {'role': role, 'source': source})
    return changed


def reassign(conn, request_id, specialist_id, *, actor, event_id):
    if not has_role(conn, specialist_id, 'specialist'):
        raise ManagementError('Новый исполнитель не имеет роли specialist.')
    row = conn.execute('SELECT status,specialist_id FROM requests WHERE id=?', (request_id,)).fetchone()
    if row is None or row[0] != 'in_progress':
        raise ManagementError('Переназначить можно только заявку в работе.')
    if row[1] == specialist_id:
        return False
    conn.execute('UPDATE requests SET specialist_id=?,revision=revision+1 WHERE id=?', (specialist_id, request_id))
    log_change(conn, actor, event_id, 'reassign', request_id, {'from': row[1], 'to': specialist_id})
    return True


class RoleRegistry:
    def __init__(self, store):
        self.store = store

    async def list(self):
        def read():
            with self.store.connect() as conn:
                return [{'user_id': user, 'role': role} for user, role in conn.execute('SELECT user_id,role FROM bot_roles ORDER BY role,user_id')]
        return await asyncio.to_thread(read)

    async def change(self, user_id, role, grant):
        def write():
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                return change_role(conn, user_id, role, grant)
        return await asyncio.to_thread(write)
