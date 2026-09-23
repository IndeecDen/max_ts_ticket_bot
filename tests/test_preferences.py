import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.domain.events import IncomingEvent
from app.services.preferences import Preferences
from app.services.processor import InboxProcessor, ProcessingPolicy
from app.services.roles import ManagementError
from app.storage.inbox import InboxStore


class PreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = InboxStore(Path(self.temp.name) / 'max.db')
        await self.store.initialize()
        self.policy = ProcessingPolicy(frozenset({-20}), frozenset({99}), -30, 10, frozenset({77}))
        self.processor = InboxProcessor(self.store, self.policy)
        await self.processor.initialize_roles()
        self.preferences = Preferences(self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def rows(self, table):
        with self.store.connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]

    async def message(self, text, *, mid='m1', user=10, chat=-20, attachments=None):
        payload = {'update_type': 'message_created', 'timestamp': 1, 'message': {
            'sender': {'user_id': user, 'is_bot': False}, 'recipient': {'chat_id': chat},
            'body': {'mid': mid, 'text': text, 'attachments': attachments}}}
        await self.store.save(IncomingEvent.parse(payload))
        await self.processor.tick(now_ms=0)

    async def test_timeout_changes_only_new_waits_and_survives_restart(self):
        await self.message('Помогите')
        first = self.rows('requests')[0]
        await self.message('/set_timeout 25', user=77, chat=-30, mid='set')
        await self.message('Подробности', mid='more')
        await self.message('Другая проблема', user=11, mid='second')
        rows = self.rows('requests')
        self.assertEqual(rows[0]['due_at_ms'], first['due_at_ms'])
        self.assertEqual(rows[1]['due_at_ms'] - rows[1]['created_at_ms'], 25000)
        restarted = InboxProcessor(self.store, ProcessingPolicy(frozenset({-20}), frozenset(), -30, 300))
        await restarted.initialize_roles()
        self.assertEqual((await self.preferences.snapshot())['response_timeout'], 25)

    async def test_all_words_match_case_and_ascii_punctuation_but_not_substrings(self):
        await self.preferences.change(words=['Спасибо!', 'ок', 'СПАСИБО'])
        await self.message('СПАСИБО, ок!!!')
        self.assertEqual(self.rows('requests'), [])
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'ignored_words')
        await self.message('Спасибо, но ошибка осталась', mid='problem')
        await self.message('окно', user=11, mid='substring')
        self.assertEqual(len(self.rows('requests')), 2)
        self.assertEqual((await self.preferences.snapshot())['ignored_words'], ['ок', 'спасибо'])

    async def test_ignored_followup_does_not_cancel_or_extend_wait(self):
        await self.message('Ошибка')
        original = self.rows('requests')[0]
        await self.preferences.change(words=['спасибо'])
        await self.message('спасибо', mid='thanks')
        self.assertEqual(self.rows('requests'), [original])
        self.assertEqual(len(self.rows('request_messages')), 1)
        await self.processor.tick(now_ms=original['due_at_ms'])
        self.assertEqual(self.rows('requests')[0]['status'], 'new')

    async def test_staff_reply_bypasses_stop_words_and_commands_do_not_cancel(self):
        await self.preferences.change(words=['ок'])
        await self.message('Ошибка')
        await self.message('/get_timeout', user=99, mid='staff-command')
        self.assertEqual(self.rows('requests')[0]['status'], 'waiting')
        await self.message('ок', user=99, mid='staff-reply')
        self.assertEqual(self.rows('requests')[0]['status'], 'cancelled')

    async def test_empty_media_and_stop_word_caption_match_telegram_rules(self):
        await self.preferences.change(words=['спасибо'])
        await self.message('', attachments=[{'type': 'image'}])
        await self.message('спасибо', user=11, mid='caption', attachments=[{'type': 'image'}])
        await self.message('!!!', user=12, mid='punctuation')
        self.assertEqual(len(self.rows('requests')), 2)

    async def test_permission_validation_and_atomic_multiword_validation(self):
        await self.message('/set_timeout 1', user=10, chat=-30)
        await self.message('/add_ignore спасибо', user=99, chat=-30, mid='non-admin')
        self.assertEqual(await self.preferences.snapshot(), {'response_timeout': 10, 'ignored_words': []})
        for value in (0, 86401, True, None):
            with self.assertRaises(ManagementError):
                await self.preferences.change(seconds=value)
        with self.assertRaises(ManagementError):
            await self.preferences.change(words=['спасибо', '!!!'])
        self.assertEqual((await self.preferences.snapshot())['ignored_words'], [])

    async def test_command_replay_remove_and_transaction_rollback(self):
        await self.message('/add_ignore Спасибо ок', user=77, chat=-30)
        await self.message('/add_ignore Спасибо ок', user=77, chat=-30)
        self.assertEqual(len([r for r in self.rows('management_log') if r['action'] == 'ignore_add']), 1)
        with patch('app.services.processor.enqueue_cards', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await self.message('/del_ignore спасибо', user=77, chat=-30, mid='remove')
        self.assertIn('спасибо', (await self.preferences.snapshot())['ignored_words'])
        await self.processor.tick(now_ms=0)
        self.assertEqual((await self.preferences.snapshot())['ignored_words'], ['ок'])

    async def test_v6_migration_preserves_roles_and_existing_deadline(self):
        await self.message('Ошибка')
        original = self.rows('requests')
        with self.store.connect() as conn:
            conn.execute('DROP TABLE ignored_words')
            conn.execute("DELETE FROM bot_meta WHERE key='response_timeout'")
            conn.execute('PRAGMA user_version=6')
        await self.store.initialize()
        await self.processor.initialize_roles()
        self.assertEqual(self.rows('requests'), original)
        self.assertEqual(len(self.rows('bot_roles')), 2)
        self.assertEqual((await self.preferences.snapshot())['response_timeout'], 10)
