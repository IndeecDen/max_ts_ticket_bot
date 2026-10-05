import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import ConfigError
from app.domain.events import IncomingEvent
from app.services.processor import InboxProcessor, ProcessingPolicy
from app.storage.inbox import InboxStore


class ProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = InboxStore(Path(self.temp.name) / 'max.db')
        await self.store.initialize()
        self.policy = ProcessingPolicy(frozenset({-20}), frozenset({99}), -30, 10)
        self.processor = InboxProcessor(self.store, self.policy)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def add(self, mid='m1', user=10, text='Помогите', chat=-20, **sender_extra):
        payload = {'update_type': 'message_created', 'timestamp': 1000,
                   'message': {'sender': {'user_id': user, 'is_bot': False, **sender_extra},
                               'recipient': {'chat_id': chat},
                               'body': {'mid': mid, 'text': text}}}
        await self.store.save(IncomingEvent.parse(payload))
        return payload

    def rows(self, table):
        with self.store.connect() as conn:
            conn.row_factory = __import__('sqlite3').Row
            return [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]

    async def test_grouping_timeout_from_first_message_and_full_payload(self):
        payload = await self.add()
        await self.processor.tick(now_ms=0)
        first = self.rows('requests')[0]
        await self.add('m2', text='Подробности')
        await self.processor.tick(now_ms=0)
        self.assertEqual(self.rows('requests'), [first])
        self.assertEqual(len(self.rows('request_messages')), 2)
        self.assertEqual(json.loads(self.rows('request_messages')[0]['payload_json']), payload)
        result = await self.processor.tick(now_ms=first['due_at_ms'])
        self.assertEqual(result['promoted'], 1)
        self.assertEqual(self.rows('requests')[0]['status'], 'new')
        await self.add('m3')
        await self.processor.tick()
        self.assertEqual(len(self.rows('requests')), 1)
        self.assertEqual(len(self.rows('request_messages')), 3)
        self.assertEqual(self.rows('requests')[0]['revision'], 1)
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'request_supplemented')

    async def test_supplements_preserved_without_work_chat_flood(self):
        await self.add('first', text='Первое сообщение')
        await self.processor.tick(now_ms=0)
        await self.add('second', text='Второе сообщение')
        await self.processor.tick(now_ms=2**62)
        before = self.rows('outbox')
        work = next(r for r in before if r['destination'] == 'work')
        self.assertIn('Первое сообщение', work['text'])
        self.assertNotIn('Второе сообщение', work['text'])
        payload = await self.add('third', text='Дополнение с файлом')
        payload['message']['body']['mid'] = 'fourth'
        payload['message']['body']['attachments'] = [{'type': 'file'}]
        await self.store.save(IncomingEvent.parse(payload))
        await self.processor.tick(now_ms=2**62)
        self.assertEqual(self.rows('outbox'), before)
        self.assertEqual(len(self.rows('request_messages')), 4)
        with self.store.connect() as conn:
            conn.execute("UPDATE requests SET status='in_progress',specialist_id=99,revision=revision+1")
        await self.processor.tick(now_ms=2**62)
        work = next(r for r in self.rows('outbox') if r['destination'] == 'work')
        self.assertNotIn('Дополнение', work['text'])
        self.assertIn('В работе', work['text'])

    async def test_specialist_cancels_all_waiting_clients_even_across_batches(self):
        await self.add(user=10)
        await self.add('m2', user=11)
        await self.add('m3', user=99)
        first = await self.processor.tick(limit=2, now_ms=2**62)
        self.assertTrue(first['more_messages'])
        self.assertEqual(first['promoted'], 0)
        await self.processor.tick(now_ms=2**62)
        self.assertEqual([r['status'] for r in self.rows('requests')], ['cancelled', 'cancelled'])
        await self.add('m4')
        await self.processor.tick(now_ms=0)
        self.assertEqual(self.rows('requests')[-1]['status'], 'waiting')

    async def test_duplicate_processing_concurrency_and_restart(self):
        await self.add()
        await self.add()
        await asyncio.gather(self.processor.tick(now_ms=0), self.processor.tick(now_ms=0))
        restarted = InboxStore(self.store.path)
        await restarted.initialize()
        await InboxProcessor(restarted, self.policy).tick(now_ms=0)
        self.assertEqual(len(self.rows('requests')), 1)
        self.assertEqual(len(self.rows('request_messages')), 1)
        self.assertEqual(await restarted.pending(), [])

    async def test_failure_rolls_back_request_and_processing_marker(self):
        await self.add()
        original = self.processor._handle
        def fail(*args):
            original(*args)
            raise RuntimeError('simulated crash')
        with patch.object(self.processor, '_handle', side_effect=fail):
            with self.assertRaises(RuntimeError):
                await self.processor.tick()
        self.assertEqual(self.rows('requests'), [])
        self.assertEqual(self.rows('request_messages'), [])
        self.assertEqual(len(await self.store.pending()), 1)
        await self.processor.tick(now_ms=0)
        self.assertEqual(len(self.rows('requests')), 1)

    async def test_filters_and_unsupported_events_remain_available(self):
        await self.add('bot', is_bot=True)
        await self.add('work', chat=-30)
        await self.add('other', chat=-40)
        await self.add('command', text='/unsupported_command')
        await self.add('bad', user=True)
        await self.store.save(IncomingEvent.parse({'update_type': 'future_event', 'timestamp': 1}))
        await self.processor.tick()
        self.assertEqual(self.rows('requests'), [])
        self.assertEqual(len(await self.store.pending()), 1)
        self.assertEqual({r['outcome'] for r in self.rows('inbox_events')},
                         {None, 'ignored_bot_or_unknown_sender', 'ignored_chat',
                          'deferred_command', 'invalid_id'})

    async def test_attachment_and_forward_only_preserved(self):
        payload = await self.add()
        payload['message']['body'] = None
        payload['message']['link'] = {'type': 'forward', 'message': {'text': 'Forward'}}
        await self.store.save(IncomingEvent.parse(payload))
        await self.processor.tick(now_ms=0)
        self.assertEqual(len(self.rows('request_messages')), 2)
        self.assertIsNone(self.rows('request_messages')[-1]['mid'])

    async def test_policy_rejects_missing_roles_and_work_chat_overlap(self):
        for chats, specialists, work in [(set(), {99}, -30),
                                         ({-20}, {99}, -20), ({-20}, {99}, None)]:
            with self.assertRaises(ConfigError):
                ProcessingPolicy(frozenset(chats), frozenset(specialists), work)

    async def test_v1_migration_preserves_pending_payload(self):
        await self.add()
        before = await self.store.pending()
        with self.store.connect() as conn:
            conn.execute('DROP TABLE request_messages')
            conn.execute('DROP TABLE requests')
            conn.execute('ALTER TABLE inbox_events DROP COLUMN outcome')
            conn.execute('PRAGMA user_version=1')
        await self.store.initialize()
        self.assertEqual(await self.store.pending(), before)
        await self.processor.tick(now_ms=0)
        self.assertEqual(len(self.rows('requests')), 1)
