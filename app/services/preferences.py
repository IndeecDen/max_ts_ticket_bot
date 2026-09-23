"""Durable collection preferences with Telegram-compatible stop-word matching."""
import asyncio
import string

from app.services.roles import ManagementError, log_change

PUNCTUATION = str.maketrans('', '', string.punctuation)


def tokens(text):
    return text.translate(PUNCTUATION).lower().split()


def bootstrap_preferences(conn, seconds):
    conn.execute("INSERT OR IGNORE INTO bot_meta(key,value) VALUES ('response_timeout',?)", (str(seconds),))


def get_timeout(conn):
    row = conn.execute("SELECT value FROM bot_meta WHERE key='response_timeout'").fetchone()
    return int(row[0]) if row else None


def set_timeout(conn, seconds, *, actor=None, event_id=None):
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise ManagementError('Таймаут должен быть целым числом от 1 до 86400 секунд.')
    previous = get_timeout(conn)
    if previous == seconds:
        return False
    conn.execute("""INSERT INTO bot_meta VALUES ('response_timeout',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(seconds),))
    log_change(conn, actor, event_id, 'timeout_set', 0, {'from': previous, 'to': seconds})
    return True


def change_words(conn, words, add, *, actor=None, event_id=None):
    if not words or len(words) > 100:
        raise ManagementError('Укажите от 1 до 100 отдельных слов.')
    normalized = set()
    for word in words:
        if not isinstance(word, str) or len(word) > 128:
            raise ManagementError('Некорректное слово-исключение.')
        parts = tokens(word)
        if len(parts) != 1 or len(parts[0]) > 64:
            raise ManagementError('Каждое исключение должно быть одним непустым словом до 64 символов.')
        normalized.add(parts[0])
    existing = {row[0] for row in conn.execute('SELECT word FROM ignored_words')}
    if add and len(existing | normalized) > 1000:
        raise ManagementError('Допускается не более 1000 слов-исключений.')
    changed = sorted(normalized - existing if add else normalized & existing)
    for word in changed:
        if add:
            conn.execute('INSERT INTO ignored_words VALUES (?)', (word,))
        else:
            conn.execute('DELETE FROM ignored_words WHERE word=?', (word,))
    if changed:
        log_change(conn, actor, event_id, 'ignore_add' if add else 'ignore_remove', 0, {'words': changed})
    return len(changed)


def is_ignored(conn, text):
    words = tokens(text)
    if not words:
        return False
    ignored = {row[0] for row in conn.execute('SELECT word FROM ignored_words')}
    return all(word in ignored for word in words)


class Preferences:
    def __init__(self, store):
        self.store = store

    async def snapshot(self):
        def read():
            with self.store.connect() as conn:
                conn.execute('BEGIN')
                return {'response_timeout': get_timeout(conn),
                        'ignored_words': [r[0] for r in conn.execute('SELECT word FROM ignored_words ORDER BY word')]}
        return await asyncio.to_thread(read)

    async def change(self, *, seconds=None, words=None, add=True):
        def write():
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if words is not None:
                    return change_words(conn, words, add)
                return set_timeout(conn, seconds)
        return await asyncio.to_thread(write)
