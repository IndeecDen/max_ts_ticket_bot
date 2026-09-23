import asyncio
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.services.delivery import DeliveryQueue
from app.services.recovery import QueueRecovery, RecoveryError
from app.storage.delivery_lock import DeliveryLock, DeliveryBusy
from app.storage.inbox import InboxStore


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = InboxStore(Path(self.temp.name) / 'max.db')
        await self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("""INSERT INTO outbox(request_id,destination,chat_id,text,state,revision,attempted_revision)
                VALUES (1,'work',-30,'private body','uncertain',3,2)""")
        self.recovery = QueueRecovery(self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def row(self):
        with self.store.connect() as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute('SELECT * FROM outbox WHERE id=1').fetchone())

    async def test_inspect_hides_payload_and_shows_attempted_revision(self):
        rows = await self.recovery.inspect()
        self.assertNotIn('private body', str(rows))
        self.assertEqual(rows[0]['attempted_revision'], 2)
        self.assertEqual(await self.recovery.inspect(999), [])

    async def test_confirm_old_revision_schedules_edit_not_duplicate_post(self):
        state = await self.recovery.recover(1, 'confirm', 3, reason='Проверено в чате',
                                           mid='found-mid', delivered_revision=2)
        self.assertEqual(state, 'pending')
        self.assertEqual(self.row()['text'], 'private body')
        client = AsyncMock()
        client.edit_message.return_value = 'found-mid'
        await DeliveryQueue(self.store).deliver_one(client, lambda: 10000)
        client.send_message.assert_not_awaited()
        client.edit_message.assert_awaited_once_with('found-mid', 'private body', [])
        self.assertEqual(self.row()['state'], 'sent')

    async def test_confirm_current_revision_and_audit_survive_restart(self):
        await self.recovery.recover(1, 'confirm', 3, reason='Проверено', mid='mid', delivered_revision=3)
        await self.store.initialize()
        self.assertEqual(self.row()['state'], 'sent')
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT action,previous_state,reason FROM recovery_log').fetchone(),
                             ('confirm', 'uncertain', 'Проверено'))
        with self.assertRaises(RecoveryError):
            await self.recovery.recover(1, 'retry', 3, reason='Повтор')

    async def test_unknown_post_requires_explicit_non_delivery_confirmation(self):
        with self.assertRaises(RecoveryError):
            await self.recovery.recover(1, 'retry', 3, reason='Повтор')
        self.assertEqual(self.row()['state'], 'uncertain')
        await self.recovery.recover(1, 'retry', 3, reason='Карточки нет', confirm_not_delivered=True)
        self.assertEqual(self.row()['state'], 'pending')

    async def test_stale_version_wrong_mid_and_future_version_rejected(self):
        with self.assertRaises(RecoveryError):
            await self.recovery.recover(1, 'retry', 2, reason='Повтор', confirm_not_delivered=True)
        with self.assertRaises(RecoveryError):
            await self.recovery.recover(1, 'confirm', 3, reason='Проверено', mid='mid', delivered_revision=4)
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET message_id='original'")
        with self.assertRaises(RecoveryError):
            await self.recovery.recover(1, 'confirm', 3, reason='Проверено', mid='other', delivered_revision=3)

    async def test_active_send_cannot_be_recovered(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='pending'")
        entered, release = asyncio.Event(), asyncio.Event()
        client = AsyncMock()
        async def send(*args):
            entered.set()
            await release.wait()
            return 'mid'
        client.send_message.side_effect = send
        task = asyncio.create_task(DeliveryQueue(self.store).deliver_one(client))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with self.assertRaises(DeliveryBusy):
                await self.recovery.recover(1, 'retry', 3, reason='Повтор', confirm_not_delivered=True)
        finally:
            release.set()
            await task
        self.assertEqual(self.row()['state'], 'sent')

    async def test_abandoned_sending_and_callback_recovery(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sending',callback_id='click',revision=0")
        with self.assertRaises(RecoveryError):
            await self.recovery.recover(1, 'confirm', 0, reason='Ответ получен', mid='bad', delivered_revision=0)
        await self.recovery.recover(1, 'confirm', 0, reason='Ответ получен', delivered_revision=0)
        self.assertEqual(self.row()['state'], 'sent')

    async def test_failed_audit_rolls_back_and_releases_lock(self):
        with self.store.connect() as conn:
            conn.execute("""CREATE TRIGGER reject_audit BEFORE INSERT ON recovery_log
                BEGIN SELECT RAISE(ABORT,'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.recovery.recover(1, 'confirm', 3, reason='Проверено', mid='mid', delivered_revision=3)
        self.assertEqual(self.row()['state'], 'uncertain')
        with DeliveryLock(self.store.path, 1):
            pass

    async def test_lock_excludes_other_process_and_releases_after_exit(self):
        code = '''from pathlib import Path
from app.storage.delivery_lock import DeliveryLock, DeliveryBusy
import sys
try:
    lock = DeliveryLock(Path(sys.argv[1]),1)
except DeliveryBusy:
    sys.exit(2)
sys.exit(0)
'''
        def probe():
            return subprocess.run([sys.executable, '-c', code, str(self.store.path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=5).returncode
        with DeliveryLock(self.store.path, 1):
            self.assertEqual(await asyncio.to_thread(probe), 2)
        self.assertEqual(await asyncio.to_thread(probe), 0)
        with DeliveryLock(self.store.path, 1):
            pass

    async def test_migrate_v4_keeps_uncertainty_and_records_unknown_attempt(self):
        with self.store.connect() as conn:
            conn.execute('DROP TABLE recovery_log')
            conn.execute('ALTER TABLE outbox DROP COLUMN attempted_revision')
            conn.execute('PRAGMA user_version=4')
        await self.store.initialize()
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertIsNone(self.row()['attempted_revision'])
