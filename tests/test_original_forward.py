import json
import unittest
from unittest.mock import AsyncMock
import test_delivery as fixtures
from app.services.delivery import enqueue_cards
from app.adapters.max.client import MaxClient, MaxAPIError
from app.config import load_settings
from test_foundation import Session, Response


class OriginalForwardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.DeliveryTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixtures.DeliveryTests.asyncTearDown(self)

    def prepare(self, *, existing=False, mid='m1'):
        with self.store.connect() as conn:
            raw = json.loads(conn.execute('SELECT payload_json FROM request_messages').fetchone()[0])
            raw['message']['body']['attachments'] = [{'type': 'file', 'payload': {'url': 'https://example.invalid/file'}}]
            raw['message']['body']['mid'] = mid
            conn.execute('UPDATE request_messages SET payload_json=?', (json.dumps(raw),))
            if not existing:
                conn.execute('DELETE FROM outbox')
            else:
                conn.execute('UPDATE requests SET revision=revision+1')
            enqueue_cards(conn, -30)

    def rows(self):
        return fixtures.DeliveryTests.rows(self)

    async def test_forward_once_only_to_work_chat(self):
        self.prepare()
        await self.processor.tick()
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        original = rows[-1]
        self.assertEqual((original['chat_id'], original['forward_mid']), (-30, 'm1'))
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent' WHERE forward_mid IS NULL")
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 100), 'sent')
        self.client.send_message.assert_awaited_once_with(-30, original['text'], forward_mid='m1')
        await self.store.initialize()
        await self.processor.tick()
        self.assertEqual(len(self.rows()), 3)

    async def test_existing_card_does_not_backfill_history(self):
        self.prepare(existing=True)
        self.assertEqual(len(self.rows()), 2)

    async def test_missing_mid_preserves_text_without_forward(self):
        self.prepare(mid=None)
        self.assertEqual(len(self.rows()), 2)

    async def test_uncertain_forward_is_not_repeated(self):
        self.prepare()
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent' WHERE forward_mid IS NULL")
        self.client.send_message.side_effect = MaxAPIError('timeout', uncertain=True)
        self.assertEqual(await self.queue.deliver_one(self.client, lambda: 100), 'uncertain')
        self.assertIsNone(await self.queue.deliver_one(self.client, lambda: 10000))
        self.assertEqual(self.client.send_message.await_count, 1)

    async def test_api_forward_contract(self):
        session = Session(Response({'message': {'body': {'mid': 'forwarded'}}}))
        settings = load_settings(self.root, {'MAX_BOT_TOKEN': 'test'})
        async with MaxClient(settings, session=session) as client:
            self.assertEqual(await client.send_message(-30, 'Заявка #1', forward_mid='m1'), 'forwarded')
        self.assertEqual(session.calls[0][1]['json'], {'text': 'Заявка #1', 'link': {'type': 'forward', 'mid': 'm1'}})

    async def test_upgrade_from_14_preserves_jobs(self):
        with self.store.connect() as conn:
            conn.execute('ALTER TABLE outbox DROP COLUMN forward_mid')
            conn.execute('PRAGMA user_version=14')
        await self.store.initialize()
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row['forward_mid'] is None for row in self.rows()))
