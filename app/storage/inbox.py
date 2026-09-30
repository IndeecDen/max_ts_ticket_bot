"""Durable inbox. Each call owns its SQLite connection and closes it."""
import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.domain.events import IncomingEvent

APPLICATION_ID = 0x4D585442  # MXTB; refuse unrelated databases, including Telegram.
SCHEMA_VERSION = 16


class InboxSchemaError(RuntimeError):
    pass


class InboxStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            app_id = conn.execute('PRAGMA application_id').fetchone()[0]
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            if app_id not in (0, APPLICATION_ID) or (app_id == 0 and has_tables):
                raise InboxSchemaError('DATABASE_PATH указывает на чужую базу. Укажите отдельную базу MAX.')
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, SCHEMA_VERSION):
                raise InboxSchemaError('Версия базы MAX не поддерживается этим приложением.')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE IF NOT EXISTS inbox_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                update_type TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_inbox_pending ON inbox_events(processed_at, id)')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(inbox_events)')}
            if 'outcome' not in columns:
                conn.execute('ALTER TABLE inbox_events ADD COLUMN outcome TEXT')
            conn.execute('''CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                author_name TEXT NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('waiting','new','cancelled')),
                due_at_ms INTEGER NOT NULL, created_at_ms INTEGER NOT NULL
            )''')
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_active_request
                ON requests(chat_id,user_id) WHERE status IN ('waiting','new')""")
            conn.execute('''CREATE TABLE IF NOT EXISTS request_messages (
                event_id INTEGER PRIMARY KEY REFERENCES inbox_events(id),
                request_id INTEGER NOT NULL REFERENCES requests(id),
                mid TEXT, payload_json TEXT NOT NULL
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_request_messages ON request_messages(request_id,event_id)')
            conn.execute('''CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
                destination TEXT NOT NULL, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(state IN ('pending','sending','sent','failed','uncertain')),
                attempts INTEGER NOT NULL DEFAULT 0, next_at_ms INTEGER NOT NULL DEFAULT 0,
                message_id TEXT, error_kind TEXT,
                UNIQUE(request_id,destination)
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(state,next_at_ms,id)')
            conn.execute('''CREATE TABLE IF NOT EXISTS delivery_slots (
                chat_id INTEGER PRIMARY KEY, next_at_ms INTEGER NOT NULL
            )''')
            request_columns = {r[1] for r in conn.execute('PRAGMA table_info(requests)')}
            if 'revision' not in request_columns:
                conn.execute('''CREATE TABLE requests_v4 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, author_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('waiting','new','in_progress','closed','cancelled')),
                    due_at_ms INTEGER NOT NULL, created_at_ms INTEGER NOT NULL,
                    specialist_id INTEGER, revision INTEGER NOT NULL DEFAULT 1,
                    started_at_ms INTEGER, ended_at_ms INTEGER
                )''')
                conn.execute('''INSERT INTO requests_v4(id,chat_id,user_id,author_name,status,due_at_ms,created_at_ms)
                    SELECT id,chat_id,user_id,author_name,status,due_at_ms,created_at_ms FROM requests''')
                conn.execute('DROP TABLE requests')
                conn.execute('ALTER TABLE requests_v4 RENAME TO requests')
                conn.execute("""CREATE UNIQUE INDEX idx_active_request ON requests(chat_id,user_id)
                    WHERE status IN ('waiting','new','in_progress')""")
            out_columns = {r[1] for r in conn.execute('PRAGMA table_info(outbox)')}
            for name, definition in [('revision', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('attempted_revision', 'INTEGER'),
                                     ('attachments_json', "TEXT NOT NULL DEFAULT '[]'"),
                                     ('callback_id', 'TEXT'),
                                     ('report_json', 'TEXT'), ('file_token', 'TEXT'),
                                     ('delete_mid', 'TEXT'), ('delete_source_id', 'INTEGER'),
                                     ('delete_source_revision', 'INTEGER'), ('deleted_at_ms', 'INTEGER'), ('menu_owner', 'INTEGER'),
                                     ('forward_mid', 'TEXT')]:
                if name not in out_columns:
                    conn.execute(f'ALTER TABLE outbox ADD COLUMN {name} {definition}')
            conn.execute('''CREATE TABLE IF NOT EXISTS recovery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
                action TEXT NOT NULL, previous_state TEXT NOT NULL,
                revision INTEGER NOT NULL, delivered_revision INTEGER,
                message_id TEXT, reason TEXT NOT NULL, created_at_ms INTEGER NOT NULL
            )''')
            conn.execute(f'PRAGMA application_id={APPLICATION_ID}')
            conn.execute('''CREATE TABLE IF NOT EXISTS bot_roles (
                user_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('specialist','admin')),
                PRIMARY KEY(user_id,role)
            )''')
            conn.execute('CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS ignored_words (word TEXT PRIMARY KEY)')
            conn.execute('''CREATE TABLE IF NOT EXISTS management_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, event_id INTEGER,
                action TEXT NOT NULL, target_id INTEGER NOT NULL, details TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS schedules (
                name TEXT PRIMARY KEY, settings_json TEXT NOT NULL,
                next_at_ms INTEGER NOT NULL, run_number INTEGER NOT NULL DEFAULT 0
            )''')
            schedule_columns = {r[1] for r in conn.execute('PRAGMA table_info(schedules)')}
            if 'last_run_date' not in schedule_columns:
                conn.execute('ALTER TABLE schedules ADD COLUMN last_run_date TEXT')
            conn.execute('''CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
                expires_at_ms INTEGER, active_from TEXT, active_to TEXT,
                created_by INTEGER NOT NULL, created_at_ms INTEGER NOT NULL
            )''')
            if 'text_format' not in {r[1] for r in conn.execute('PRAGMA table_info(outbox)')}:
                conn.execute('ALTER TABLE outbox ADD COLUMN text_format TEXT')
            conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_statistics_completed ON requests(status,ended_at_ms,specialist_id)')

    async def initialize(self):
        await asyncio.to_thread(self._initialize)

    def _save(self, event: IncomingEvent):
        with self.connect() as conn:
            cursor = conn.execute('''INSERT INTO inbox_events
                (event_key, update_type, timestamp_ms, payload_json, received_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(event_key) DO NOTHING''',
                (event.key, event.update_type, event.timestamp_ms, event.payload_json,
                 datetime.now(timezone.utc).isoformat()))
            inserted = cursor.rowcount == 1
        # Context manager has committed before the caller can acknowledge the request.
        return inserted

    async def save(self, event: IncomingEvent):
        return await asyncio.to_thread(self._save, event)

    def _pending(self, limit):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('''SELECT * FROM inbox_events
                WHERE processed_at IS NULL ORDER BY id LIMIT ?''', (limit,))]

    async def pending(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        return await asyncio.to_thread(self._pending, limit)

    def _ping(self):
        with self.connect() as conn:
            conn.execute('SELECT id FROM inbox_events LIMIT 1').fetchone()

    async def ping(self):
        await asyncio.to_thread(self._ping)
