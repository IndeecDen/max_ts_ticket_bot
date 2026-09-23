import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.adapters.max.client import MaxClient, MaxAPIError
from app.config import load_settings
from app.domain.events import IncomingEvent
from app.services.delivery import DeliveryQueue, enqueue_cards, clip
from app.services.processor import InboxProcessor, ProcessingPolicy
from app.storage.inbox import InboxStore
from test_foundation import Session, Response


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = InboxStore(self.root / 'max.db')
        await self.store.initialize()
        self.policy = ProcessingPolicy(frozenset({-20}), frozenset({99}), -30, 1)
        self.processor = InboxProcessor(self.store, self.policy)
        await self.store.save(IncomingEvent.parse({'update_type': 'message_created', 'timestamp': 1,
            'message': {'sender': {'user_id': 10, 'is_bot': False, 'name': '<b>Клиент</b>'},
                        'recipient': {'chat_id': -20}, 'body': {'mid': 'm1', 'text': 'Проблема'}}}))
        await self.processor.tick(now_ms=2**62)
        self.queue = DeliveryQueue(self.store)
        self.client = AsyncMock()
        self.client.send_message.return_value = 'sent-mid'

    async def asyncTearDown(self):
        self.temp.cleanup()

    def rows(self):
        with self.store.connect() as conn:
            conn.row_factory = __import__('sqlite3').Row
            return [dict(r) for r in conn.execute('SELECT * FROM outbox ORDER BY id')]

    async def test_enqueued_once_and_success_survives_restart(self):
        await self.processor.tick()
        self.assertEqual(len(self.rows()), 2)
        self.assertIn('Проблема', self.rows()[0]['text'])
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 100), 'sent')
        await self.store.initialize()
        queue = DeliveryQueue(InboxStore(self.store.path))
        self.assertEqual(await queue.deliver_one(self.client, lambda: 100), 'sent')
        self.assertIsNone(await queue.deliver_one(self.client, lambda: 5000))
        self.assertTrue(all(r['message_id'] == 'sent-mid' for r in self.rows()))
        self.assertEqual(self.client.send_message.await_count, 2)

    async def test_concurrent_claim_does_not_repeat_job(self):
        results = await asyncio.gather(*(self.queue.deliver_one(self.client, lambda: 100) for _ in range(4)))
        self.assertEqual(results.count('sent'), 2)
        self.assertEqual(self.client.send_message.await_count, 2)

    async def test_rate_limit_persists_across_queue_instances(self):
        with self.store.connect() as conn:
            conn.execute('UPDATE outbox SET chat_id=-30')
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 100), 'sent')
        another = DeliveryQueue(self.store)
        self.assertIsNone(await another.deliver_one(self.client, lambda: 1099))
        self.assertEqual(await another.deliver_one(self.client, lambda: 1100), 'sent')

    async def test_429_backoff_and_permanent_failure(self):
        self.client.send_message.side_effect = MaxAPIError('secret', status=429, retryable=True, retry_after=12)
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 100), 'pending')
        self.assertEqual(self.rows()[0]['next_at_ms'], 12100)
        self.assertNotIn('secret', str(self.rows()))
        self.client.send_message.side_effect = MaxAPIError('secret', status=403)
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 100), 'failed')
        self.assertIsNone(await self.queue.deliver_one(self.client, lambda: 12099))
        self.client.send_message.side_effect = None
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 12100), 'sent')

    async def test_ambiguous_or_interrupted_send_not_repeated(self):
        self.client.send_message.side_effect = MaxAPIError('timeout', retryable=True, uncertain=True)
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 100), 'uncertain')
        self.client.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.queue.deliver_one(self.client, lambda: 100)
        self.assertEqual(await self.queue.status(), {'sending': 1, 'uncertain': 1})
        self.assertIsNone(await self.queue.deliver_one(self.client, lambda: 100000))

    async def test_failed_enqueue_rolls_back_promotion(self):
        with self.store.connect() as conn:
            conn.execute('DELETE FROM outbox')
            conn.execute("UPDATE requests SET status='waiting'")
        def fail(conn, work_chat, **kwargs):
            enqueue_cards(conn, work_chat, **kwargs)
            raise RuntimeError('crash')
        with patch('app.services.processor.enqueue_cards', side_effect=fail):
            with self.assertRaises(RuntimeError):
                await self.processor.tick(now_ms=2**62)
        self.assertEqual(self.rows(), [])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM requests').fetchone()[0], 'waiting')

    async def test_schema_v2_and_backfill(self):
        with self.store.connect() as conn:
            conn.execute('DROP TABLE outbox')
            conn.execute('DROP TABLE delivery_slots')
            conn.execute('PRAGMA user_version=2')
        await self.store.initialize()
        await self.processor.tick()
        self.assertEqual(len(self.rows()), 2)

    async def test_post_contract_and_ambiguous_response(self):
        settings = load_settings(self.root, {'MAX_BOT_TOKEN': 'test-secret'})
        session = Session(Response({'message': {'body': {'mid': 'string-id'}}}))
        async with MaxClient(settings, session=session) as client:
            self.assertEqual(await client.send_message(-20, 'Карточка'), 'string-id')
        args, kwargs = session.calls[0]
        self.assertEqual(args, ('POST', 'https://platform-api2.max.ru/messages'))
        self.assertEqual(kwargs['params']['chat_id'], '-20')
        self.assertEqual(kwargs['json'], {'text': 'Карточка'})
        self.assertFalse(kwargs['allow_redirects'])
        for response in [Response({}), Response(status=500), Response(error=asyncio.TimeoutError())]:
            async with MaxClient(settings, session=Session(response)) as client:
                with self.assertRaises(MaxAPIError) as caught:
                    await client.send_message(-20, 'Текст')
                self.assertTrue(caught.exception.uncertain)

    async def test_card_limit_handles_emoji(self):
        text = clip('😀' * 5000)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 3900)
        self.assertTrue(text.endswith('…'))

    async def test_unreasonable_retry_after_is_not_retried_early(self):
        self.client.send_message.side_effect = MaxAPIError('limit', status=429, retryable=True, retry_after=1e100)
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 100), 'failed')
