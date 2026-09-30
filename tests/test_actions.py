import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.adapters.max.client import MaxClient, MaxAPIError
from app.config import load_settings
from app.domain.events import IncomingEvent
from app.services.delivery import DeliveryQueue
from app.services.processor import InboxProcessor, ProcessingPolicy
from app.storage.inbox import InboxStore
from test_foundation import Session, Response


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = InboxStore(self.root / 'max.db')
        await self.store.initialize()
        self.processor = InboxProcessor(self.store, ProcessingPolicy(frozenset({-20}), frozenset({99, 98}), -30, 1))
        await self.store.save(IncomingEvent.parse({'update_type': 'message_created', 'timestamp': 1,
            'message': {'sender': {'user_id': 10, 'is_bot': False}, 'recipient': {'chat_id': -20},
                        'body': {'mid': 'incoming', 'text': 'Нужна помощь'}}}))
        await self.processor.tick(now_ms=2**62)
        self.client = AsyncMock()
        self.client.send_message.side_effect = lambda chat, text, attachments: f'mid-{chat}'
        self.client.edit_message.side_effect = lambda mid, text, attachments: mid
        self.queue = DeliveryQueue(self.store)
        await self.queue.deliver_one(self.client, lambda: 100)
        await self.queue.deliver_one(self.client, lambda: 100)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def rows(self, table):
        with self.store.connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]

    async def click(self, action='take', actor=99, rev=1, click='click1', chat=-30, mid=None):
        await self.store.save(IncomingEvent.parse({'update_type': 'message_callback', 'timestamp': 2,
            'callback': {'callback_id': click, 'payload': f'{action}:1:{rev}',
                         'user': {'user_id': actor, 'is_bot': False}},
            'message': {'recipient': {'chat_id': chat}, 'body': {'mid': mid or f'mid-{chat}'}}}))

    async def test_take_done_and_card_edits(self):
        await self.click()
        await self.processor.tick()
        row = self.rows('requests')[0]
        self.assertEqual((row['status'], row['specialist_id'], row['revision']), ('in_progress', 99, 2))
        await self.queue.deliver_one(self.client, lambda: 2000)
        self.client.edit_message.assert_awaited_once()
        args = self.client.edit_message.await_args.args
        self.assertEqual(args[0], 'mid--30')
        self.assertEqual(args[2][0]['payload']['buttons'][0][0]['payload'], 'done:1:2')
        await self.click('done', rev=2, click='click2')
        await self.processor.tick()
        self.assertEqual(self.rows('requests')[0]['status'], 'closed')
        cards = [r for r in self.rows('outbox') if r['destination'] in ('work', 'client')]
        self.assertTrue(all(json.loads(r['attachments_json']) == [] for r in cards))
        self.assertTrue(all(r['revision'] == 3 for r in cards))

    async def test_admin_can_take_without_specialist_role(self):
        with self.store.connect() as conn:
            conn.execute("INSERT INTO bot_roles VALUES (77,'admin')")
        await self.click(actor=77)
        await self.processor.tick()
        self.assertEqual(self.rows('requests')[0]['specialist_id'], 77)
        await self.click('done', actor=77, rev=2, click='admin-done')
        await self.processor.tick()
        self.assertEqual(self.rows('requests')[0]['status'], 'closed')

    async def test_permissions_and_wrong_card(self):
        for kwargs, expected in [({'actor': 10}, 'forbidden_callback'),
                                  ({'chat': -40, 'mid': 'mid--30'}, 'unknown_callback_card'),
                                  ({'mid': 'unknown'}, 'unknown_callback_card'),
                                  ({'action': 'cancel', 'actor': 11, 'chat': -20}, 'forbidden_callback')]:
            await self.click(click=str(kwargs), **kwargs)
            await self.processor.tick()
            self.assertEqual(self.rows('inbox_events')[-1]['outcome'], expected)
            self.assertEqual(self.rows('requests')[0]['status'], 'new')

    async def test_only_assigned_specialist_can_finish(self):
        await self.click()
        await self.processor.tick()
        await self.click('done', actor=98, rev=2, click='other')
        await self.processor.tick()
        self.assertEqual(self.rows('requests')[0]['status'], 'in_progress')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'invalid_transition')

    async def test_cancel_by_author_and_new_request_allowed(self):
        await self.click('cancel', actor=10, chat=-20)
        await self.processor.tick()
        self.assertEqual(self.rows('requests')[0]['status'], 'cancelled')
        payload = json.loads(self.rows('inbox_events')[0]['payload_json'])
        payload['message']['body']['mid'] = 'new-incoming'
        await self.store.save(IncomingEvent.parse(payload))
        await self.processor.tick(now_ms=0)
        self.assertEqual(len(self.rows('requests')), 2)

    async def test_duplicate_stale_and_competing_take(self):
        await self.click()
        await self.click()
        await self.click(actor=98, click='competitor')
        await asyncio.gather(self.processor.tick(), self.processor.tick())
        self.assertEqual(self.rows('requests')[0]['specialist_id'], 99)
        self.assertEqual(self.rows('requests')[0]['revision'], 2)
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'stale_callback')
        self.assertEqual(len([r for r in self.rows('outbox') if r['callback_id']]), 2)

    async def test_change_during_delivery_does_not_lose_new_revision(self):
        await self.click()
        await self.processor.tick()
        job = self.queue._claim(2000)
        self.assertEqual(job['revision'], 2)
        await self.click('done', rev=2, click='finish')
        await self.processor.tick()
        self.queue._finish(job, 'sent', 2001, job['message_id'])
        job['_lock'].close()
        work = self.rows('outbox')[0]
        self.assertEqual((work['state'], work['revision'], work['message_id']), ('pending', 3, 'mid--30'))

    async def test_action_and_answers_roll_back_with_inbox_on_failure(self):
        await self.click()
        with patch('app.services.processor.enqueue_cards', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await self.processor.tick()
        self.assertEqual(self.rows('requests')[0]['status'], 'new')
        self.assertEqual(len(self.rows('outbox')), 2)
        self.assertEqual(len(await self.store.pending()), 1)

    async def test_edit_and_answer_api_contract(self):
        settings = load_settings(self.root, {'MAX_BOT_TOKEN': 'test'})
        session = Session(Response({'success': True}), Response({'success': True}))
        async with MaxClient(settings, session=session) as client:
            self.assertEqual(await client.edit_message('mid', 'Готово', []), 'mid')
            await client.answer_callback('click')
        self.assertEqual(session.calls[0][0][0], 'PUT')
        self.assertEqual(session.calls[0][1]['json']['attachments'], [])
        self.assertEqual(session.calls[1][1]['params'], {'callback_id': 'click'})
        self.assertTrue(session.calls[1][1]['json']['notification'])
        async with MaxClient(settings, session=Session(Response({'success': False}))) as client:
            with self.assertRaises(MaxAPIError):
                await client.edit_message('mid', 'Готово', [])

    async def test_callback_answer_is_delivered_and_active_request_blocks_new_one(self):
        await self.click()
        await self.processor.tick()
        payload = json.loads(self.rows('inbox_events')[0]['payload_json'])
        payload['message']['body']['mid'] = 'extra-in-progress'
        await self.store.save(IncomingEvent.parse(payload))
        await self.processor.tick()
        self.assertEqual(len(self.rows('requests')), 1)
        for now in (2000, 3000, 4000):
            await self.queue.deliver_one(self.client, lambda: now)
        self.client.answer_callback.assert_awaited_once_with('click1')

    async def test_migration_preserves_v3_cards_and_message_ids(self):
        with self.store.connect() as conn:
            conn.execute('''CREATE TABLE old_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER,user_id INTEGER,
                author_name TEXT,status TEXT CHECK(status IN ('waiting','new','cancelled')),
                due_at_ms INTEGER,created_at_ms INTEGER)''')
            conn.execute('INSERT INTO old_requests SELECT id,chat_id,user_id,author_name,status,due_at_ms,created_at_ms FROM requests')
            conn.execute('DROP TABLE requests')
            conn.execute('ALTER TABLE old_requests RENAME TO requests')
            for column in ('revision', 'attachments_json', 'callback_id'):
                conn.execute(f'ALTER TABLE outbox DROP COLUMN {column}')
            conn.execute('PRAGMA user_version=3')
        await self.store.initialize()
        await self.processor.tick()
        self.assertEqual(self.rows('outbox')[0]['message_id'], 'mid--30')
        self.assertEqual(self.rows('outbox')[0]['state'], 'pending')
        self.assertEqual(len(self.rows('request_messages')), 1)
