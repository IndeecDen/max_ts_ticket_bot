import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import ConfigError
from app.domain.events import IncomingEvent
from app.services.processor import InboxProcessor, ProcessingPolicy
from app.services.roles import RoleRegistry, ManagementError
from app.storage.inbox import InboxStore


class RoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = InboxStore(Path(self.temp.name) / 'max.db')
        await self.store.initialize()
        self.policy = ProcessingPolicy(frozenset({-20}), frozenset({99, 98}), -30, 1, frozenset({77}))
        self.processor = InboxProcessor(self.store, self.policy)
        await self.processor.initialize_roles()
        self.roles = RoleRegistry(self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def rows(self, table):
        with self.store.connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]

    async def message(self, text, actor=77, chat=-30, mid=None):
        payload = {'update_type': 'message_created', 'timestamp': 1, 'message': {
            'sender': {'user_id': actor, 'is_bot': False}, 'recipient': {'chat_id': chat},
            'body': {'mid': mid or text, 'text': text}}}
        await self.store.save(IncomingEvent.parse(payload))
        await self.processor.tick(now_ms=0)

    async def make_request(self):
        await self.message('help', actor=10, chat=-20)
        await self.processor.tick(now_ms=2**62)
        with self.store.connect() as conn:
            conn.execute("UPDATE requests SET status='in_progress',specialist_id=99,started_at_ms=123,revision=2")
            conn.execute("UPDATE outbox SET message_id='work-mid' WHERE destination='work'")

    async def callback(self, actor, revision=2, click='done'):
        await self.store.save(IncomingEvent.parse({'update_type': 'message_callback', 'timestamp': 2,
            'callback': {'callback_id': click, 'payload': f'done:1:{revision}',
                         'user': {'user_id': actor, 'is_bot': False}},
            'message': {'recipient': {'chat_id': -30}, 'body': {'mid': 'work-mid'}}}))
        await self.processor.tick()

    async def test_seed_once_and_revocation_survives_restart(self):
        await self.roles.change(99, 'specialist', False)
        await self.store.initialize()
        await InboxProcessor(self.store, self.policy).initialize_roles()
        self.assertNotIn({'user_id': 99, 'role': 'specialist'}, await self.roles.list())
        other = InboxProcessor(self.store, ProcessingPolicy(frozenset({-20}), frozenset({111}), -30))
        await other.initialize_roles()
        self.assertNotIn({'user_id': 111, 'role': 'specialist'}, await self.roles.list())

    async def test_admin_can_grant_and_revoke_with_idempotent_replies(self):
        await self.message('/role_grant specialist 101')
        await self.message('/role_grant specialist 101')
        self.assertIn({'user_id': 101, 'role': 'specialist'}, await self.roles.list())
        replies = [r for r in self.rows('outbox') if r['destination'].startswith('command:')]
        self.assertEqual(len(replies), 1)
        await self.message('/role_revoke specialist 101')
        self.assertNotIn({'user_id': 101, 'role': 'specialist'}, await self.roles.list())

    async def test_non_admin_and_wrong_chat_cannot_manage_roles(self):
        await self.message('/role_grant admin 10', actor=10)
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'forbidden_management')
        await self.message('/role_grant admin 10', chat=-20, mid='wrong-chat')
        self.assertNotIn({'user_id': 10, 'role': 'admin'}, await self.roles.list())

    async def test_last_admin_protected_and_self_revoke_with_backup_allowed(self):
        with self.assertRaises(ManagementError):
            await self.roles.change(77, 'admin', False)
        await self.message('/role_grant admin 78')
        await self.message('/role_revoke admin 77')
        await self.message('/role_grant specialist 101')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'forbidden_management')

    async def test_revoked_assignee_cannot_finish(self):
        await self.make_request()
        await self.roles.change(99, 'specialist', False)
        await self.callback(99)
        self.assertEqual(self.rows('requests')[0]['status'], 'in_progress')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'forbidden_callback')

    async def test_reassign_changes_both_cards_preserves_start_and_invalidates_old_button(self):
        await self.make_request()
        with self.store.connect() as conn:
            conn.execute("INSERT INTO bot_meta VALUES ('user_name:98','Анна')")
        await self.message('/reassign 1 98')
        row = self.rows('requests')[0]
        self.assertEqual((row['specialist_id'], row['revision'], row['started_at_ms']), (98, 3, 123))
        cards = [r for r in self.rows('outbox') if r['destination'] in ('work', 'client')]
        self.assertTrue(all(r['revision'] == 3 and 'Анна' in r['text'] for r in cards))
        await self.callback(99)
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'stale_callback')
        await self.callback(98, revision=3, click='new-done')
        self.assertEqual(self.rows('requests')[0]['status'], 'closed')

    async def test_invalid_target_and_closed_request_rejected(self):
        await self.make_request()
        await self.message('/reassign 1 123')
        self.assertEqual(self.rows('requests')[0]['specialist_id'], 99)
        await self.callback(99)
        await self.message('/reassign 1 98')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'management_rejected')

    async def test_command_changes_and_audit_rollback_on_queue_failure(self):
        with patch('app.services.processor.enqueue_cards', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await self.message('/role_grant specialist 101')
        self.assertNotIn({'user_id': 101, 'role': 'specialist'}, await self.roles.list())
        self.assertEqual(len(await self.store.pending()), 1)
        self.assertFalse(any(r['target_id'] == 101 for r in self.rows('management_log')))
        await self.processor.tick()
        self.assertIn({'user_id': 101, 'role': 'specialist'}, await self.roles.list())

    async def test_fresh_database_needs_explicit_seed_or_local_role(self):
        other = InboxStore(Path(self.temp.name) / 'empty.db')
        await other.initialize()
        processor = InboxProcessor(other, ProcessingPolicy(frozenset({-20}), frozenset(), -30))
        with self.assertRaises(ConfigError):
            await processor.initialize_roles()
        await RoleRegistry(other).change(77, 'admin', True)
        await processor.initialize_roles()

    async def test_migrate_v5_and_import_legacy_specialists_once(self):
        with self.store.connect() as conn:
            for table in ('bot_roles', 'bot_meta', 'management_log'):
                conn.execute(f'DROP TABLE {table}')
            conn.execute('PRAGMA user_version=5')
        await self.store.initialize()
        await self.processor.initialize_roles()
        self.assertEqual(len(await self.roles.list()), 3)
        await self.processor.initialize_roles()
        self.assertEqual(len(self.rows('management_log')), 3)
