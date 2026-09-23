import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

import test_statistics as fixtures
from test_foundation import Session, Response
from app.adapters.max.client import MaxClient, MaxAPIError
from app.config import load_settings
from app.services import autoclean
from app.services.delivery import DeliveryQueue
from app.services.recovery import QueueRecovery


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StatisticsTests.asyncSetUp
    asyncTearDown = fixtures.StatisticsTests.asyncTearDown
    command = fixtures.StatisticsTests.command
    rows = fixtures.StatisticsTests.rows

    def now(self, date='2026-09-22T23:00:00'):
        return int(datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('Europe/Moscow')).timestamp()*1000)

    def configure(self, parts=None):
        with self.store.connect() as conn:
            autoclean.configure(conn, parts or ['23:00','1,2,3,4,5,6,7'], actor=77, event_id=None)

    def source(self, mid='bot-mid', state='sent', request=0):
        with self.store.connect() as conn:
            return conn.execute('''INSERT INTO outbox(request_id,destination,chat_id,text,state,message_id)
                VALUES (?,?,-30,'bot text',?,?)''', (request,'source:'+str(mid),state,mid)).lastrowid

    def jobs(self):
        return [r for r in self.rows('outbox') if r['delete_mid']]

    def open_request(self, status='new'):
        with self.store.connect() as conn:
            conn.execute('''INSERT INTO requests(chat_id,user_id,author_name,status,due_at_ms,created_at_ms)
                VALUES (-20,10,'name',?,0,0)''', (status,))

    async def test_off_by_default_and_only_known_confirmed_messages(self):
        source = self.source()
        self.source('uncertain-mid', 'uncertain')
        self.source(None)
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(self.jobs(), [])
        self.configure()
        await self.processor.tick(now_ms=self.now('2026-09-22T22:59:59'))
        self.assertEqual(self.jobs(), [])
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]['delete_source_id'], source)
        await self.store.initialize()
        await asyncio.gather(*(self.processor.tick(now_ms=self.now()) for _ in range(2)))
        self.assertEqual(len(self.jobs()), 1)

    async def test_success_marks_source_without_losing_history_or_reenqueue(self):
        source = self.source()
        self.configure()
        await self.processor.tick(now_ms=self.now())
        client = AsyncMock()
        queue = DeliveryQueue(self.store)
        self.assertEqual(await queue.deliver_one(client, lambda: self.now()), 'sent')
        client.delete_message.assert_awaited_once_with('bot-mid')
        client.send_message.assert_not_called()
        row = next(r for r in self.rows('outbox') if r['id']==source)
        self.assertEqual(row['deleted_at_ms'], self.now())
        self.assertEqual(row['message_id'], 'bot-mid')
        self.assertEqual(row['text'], 'bot text')
        await self.processor.tick(now_ms=self.now('2026-09-23T23:00:00'))
        self.assertEqual(len(self.jobs()), 1)

    async def test_open_requests_block_creation_and_pause_prepared_deletion(self):
        self.source()
        self.configure()
        self.open_request()
        for status in ('waiting', 'new', 'in_progress'):
            with self.store.connect() as conn:
                conn.execute('UPDATE requests SET status=?', (status,))
                self.assertFalse(autoclean.permitted(conn))
                self.assertEqual(autoclean.enqueue_cleanup(conn, 'Europe/Moscow', self.now()), 0)
        with self.store.connect() as conn:
            conn.execute("UPDATE requests SET status='closed'")
        await self.processor.tick(now_ms=self.now())
        with self.store.connect() as conn:
            conn.execute("UPDATE requests SET status='new'")
            # Prevent ordinary unsent cards from being selected in this test.
            conn.execute("UPDATE outbox SET state='sent' WHERE delete_mid IS NULL")
        client = AsyncMock()
        self.assertIsNone(await DeliveryQueue(self.store).deliver_one(client, lambda: self.now()))
        client.delete_message.assert_not_called()

    async def test_paused_deletions_do_not_starve_normal_delivery(self):
        def seed():
            for i in range(105):
                self.source(f'mid-{i}')
        await asyncio.to_thread(seed)
        self.configure()
        await self.processor.tick(now_ms=self.now())
        self.configure(['off'])
        self.source('pending-message', 'pending')
        client = AsyncMock()
        client.edit_message.return_value = 'pending-message'
        self.assertEqual(await DeliveryQueue(self.store).deliver_one(client, lambda: self.now()), 'sent')
        client.delete_message.assert_not_called()
        client.edit_message.assert_awaited_once()

    async def test_retry_and_manual_confirmation_after_interruption(self):
        self.source()
        self.configure()
        await self.processor.tick(now_ms=self.now())
        client = AsyncMock()
        client.delete_message.side_effect = [MaxAPIError('timeout', retryable=True), asyncio.CancelledError()]
        queue = DeliveryQueue(self.store)
        self.assertEqual(await queue.deliver_one(client, lambda: self.now()), 'pending')
        with self.assertRaises(asyncio.CancelledError):
            await queue.deliver_one(client, lambda: self.now()+5000)
        job = self.jobs()[0]
        self.assertEqual(job['state'], 'sending')
        recovery = QueueRecovery(self.store)
        self.assertEqual(await recovery.recover(job['id'], 'retry', 0, reason='repeat DELETE'), 'pending')
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='failed' WHERE id=?", (job['id'],))
        self.assertEqual(await recovery.recover(job['id'], 'confirm', 0, reason='verified absent in MAX', delivered_revision=0), 'sent')
        self.assertIsNotNone(self.rows('outbox')[0]['deleted_at_ms'])

    async def test_target_revision_change_blocks_delete(self):
        source = self.source()
        self.configure()
        await self.processor.tick(now_ms=self.now())
        with self.store.connect() as conn:
            conn.execute('UPDATE outbox SET revision=revision+1 WHERE id=?', (source,))
        client = AsyncMock()
        self.assertIsNone(await DeliveryQueue(self.store).deliver_one(client, lambda: self.now()))
        client.delete_message.assert_not_called()
        self.assertEqual(self.jobs()[0]['error_kind'], 'delete_target_changed')

    async def test_permissions_validation_and_transaction_rollback(self):
        await self.command('/set_autoclean 23:00 1,2,3,4,5,6,7', actor=99)
        self.assertEqual(self.rows('schedules'), [])
        await self.command('/set_autoclean 25:00 1', mid='bad')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'management_rejected')
        self.source()
        self.configure()
        original = autoclean.enqueue_cleanup
        def fail(*args):
            original(*args)
            raise RuntimeError('rollback')
        with patch('app.services.autoclean.enqueue_cleanup', side_effect=fail):
            with self.assertRaises(RuntimeError):
                await self.processor.tick(now_ms=self.now())
        self.assertEqual(self.jobs(), [])
        self.assertIsNone(self.rows('schedules')[0]['last_run_date'])
        await self.command('/get_autoclean', mid='get')
        self.assertIn('23:00 (Europe/Moscow)', self.rows('outbox')[-1]['text'])

    async def test_v12_migration_preserves_sources(self):
        self.source()
        before = self.rows('outbox')
        with self.store.connect() as conn:
            for column in ('delete_mid','delete_source_id','delete_source_revision','deleted_at_ms'):
                conn.execute('ALTER TABLE outbox DROP COLUMN ' + column)
            conn.execute('PRAGMA user_version=12')
        await self.store.initialize()
        self.assertEqual(self.rows('outbox'), before)


class DeleteContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_contract_and_failures(self):
        settings = load_settings(Path('.test-data'), {'MAX_BOT_TOKEN':'fake'})
        session = Session(Response({'success':True}))
        async with MaxClient(settings, session=session) as client:
            await client.delete_message('mid')
        args, kwargs = session.calls[0]
        self.assertEqual(args, ('DELETE', settings.api_base_url + '/messages'))
        self.assertEqual(kwargs['params'], {'message_id':'mid'})
        self.assertFalse(kwargs['allow_redirects'])
        for response, retry in [(Response({'success':False,'message':'secret'}),False),
                                (Response({},status=403),False), (Response({},status=404),False),
                                (Response({},status=500),True),
                                (Response({},status=429,headers={'Retry-After':'3'}),True),
                                (Response(error=asyncio.TimeoutError('secret')),True)]:
            async with MaxClient(settings, session=Session(response)) as client:
                with self.assertRaises(MaxAPIError) as caught:
                    await client.delete_message('mid')
            self.assertEqual(caught.exception.retryable, retry)
            self.assertFalse(caught.exception.uncertain)
            self.assertNotIn('secret', str(caught.exception))
