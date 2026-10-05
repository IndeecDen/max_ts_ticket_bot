import json
import unittest

import test_navigation as fixtures
from app.services.processor import InboxProcessor


class StaffMenuTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NavigationTests.asyncSetUp
    asyncTearDown = fixtures.NavigationTests.asyncTearDown
    rows = fixtures.NavigationTests.rows
    add = fixtures.NavigationTests.add
    private_command = fixtures.NavigationTests.private_command
    command = fixtures.NavigationTests.command
    click = fixtures.NavigationTests.click

    async def choose(self, action, actor=77):
        row = next(r for r in reversed(self.rows('outbox')) if r['menu_owner'] == actor
                   and any(b['payload'] == 'ui:' + action for a in json.loads(r['attachments_json'])
                           for buttons in a['payload']['buttons'] for b in buttons))
        mid = 'delivered-' + str(row['id'])
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent',message_id=? WHERE id=?", (mid, row['id']))
        await self.click(payload='ui:' + action, actor=actor, mid=mid,
                         callback_id='click-' + str(len(self.rows('inbox_events'))))

    async def test_specialist_full_cycle_and_restart_during_input(self):
        target = self.add()
        await self.private_command('/menu', actor=99)
        await self.choose('requests', 99)
        await self.choose('take', 99)
        self.processor = InboxProcessor(self.store, self.processor.policy)
        await self.private_command('wrong', actor=99, mid='invalid')
        self.assertEqual(len(self.rows('menu_sessions')), 1)
        await self.private_command(str(target), actor=99, mid='take')
        self.assertEqual(self.rows('requests')[0]['specialist_id'], 99)
        self.assertEqual(self.rows('requests')[0]['status'], 'in_progress')
        self.assertEqual(self.rows('menu_sessions'), [])
        await self.choose('requests', 99)
        await self.choose('done', 99)
        await self.private_command(str(target), actor=99, mid='done')
        self.assertEqual(self.rows('requests')[0]['status'], 'closed')
        self.assertEqual(len([r for r in self.rows('management_log') if r['action'].startswith('menu_')]), 2)

    async def test_admin_downgrade_invalidates_button_and_pending_input(self):
        await self.private_command('/menu')
        await self.choose('settings')
        await self.choose('set_timeout')
        with self.store.connect() as conn:
            conn.execute("DELETE FROM bot_roles WHERE user_id=77 AND role='admin'")
            conn.execute("INSERT INTO bot_roles VALUES (77,'specialist')")
        await self.private_command('500', mid='revoked-input')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'forbidden_management')
        self.assertEqual(self.rows('menu_sessions'), [])
        await self.click(payload='ui:set_timeout', mid='delivered-1', callback_id='obsolete')
        self.assertIn(self.rows('inbox_events')[-1]['outcome'], ('unknown_navigation_action', 'unknown_navigation_menu'))

    async def test_all_sections_and_reports_are_functional(self):
        await self.private_command('/menu')
        for section, action in [('team', 'roles'), ('settings', 'timeout'),
                                ('announcements', 'notices'), ('schedules', 'digest')]:
            if section != 'team':
                await self.choose('home')
            await self.choose(section)
            await self.choose(action)
            self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'management_done')
        await self.choose('home')
        await self.choose('stats')
        await self.choose('general:month:xlsx')
        self.assertTrue(any(r['report_json'] for r in self.rows('outbox')))
        await self.choose('stats')
        await self.choose('personal:custom')
        await self.private_command('2026-10-01 2026-10-05', mid='dates')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'statistics_done')

    async def test_specialist_cannot_complete_others_request_and_menu_is_private(self):
        target = self.add(status='in_progress', specialist=98)
        await self.command('/menu', actor=99)
        await self.private_command('/menu', actor=10, mid='client')
        self.assertFalse(any(r['menu_owner'] for r in self.rows('outbox')))
        await self.private_command('/menu', actor=99)
        home = self.rows('outbox')[-1]
        self.assertNotIn('ui:team', home['attachments_json'])
        await self.choose('requests', 99)
        await self.choose('done', 99)
        await self.private_command(str(target), actor=99, mid='other')
        self.assertEqual(self.rows('requests')[0]['status'], 'in_progress')
        await self.private_command('/cancel', actor=99, mid='cancel')
        self.assertEqual(self.rows('menu_sessions'), [])

    async def test_paginated_lists_use_buttons(self):
        for user in range(10, 22):
            self.add(user=user)
        await self.private_command('/menu', actor=99)
        await self.choose('requests', 99)
        await self.choose('open', 99)
        await self.choose('open:2', 99)
        self.assertTrue(any('Страница 2/2' in r['text'] for r in self.rows('outbox')))
        await self.choose('open:1', 99)
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'navigation_done')

    async def test_admin_parameter_actions_apply_and_cancel(self):
        target = self.add()
        await self.private_command('/menu')
        await self.choose('settings')
        await self.choose('set_timeout')
        await self.private_command('600', mid='timeout-input')
        await self.choose('settings')
        await self.choose('timeout')
        self.assertTrue(any('600 секунд' in r['text'] for r in self.rows('outbox')))
        await self.choose('home')
        await self.choose('requests')
        await self.choose('assign')
        await self.private_command(f'{target} 99', mid='assign-input')
        self.assertEqual(self.rows('requests')[0]['specialist_id'], 99)
        await self.choose('requests')
        await self.choose('cancel')
        await self.private_command(f'{target} Дубликат', mid='cancel-input')
        self.assertEqual(self.rows('requests')[0]['status'], 'cancelled')

    async def test_schema17_upgrade_preserves_existing_chat_registry(self):
        with self.store.connect() as conn:
            conn.execute('DROP TABLE menu_sessions')
            conn.execute('CREATE TABLE bot_chats(chat_id INTEGER PRIMARY KEY)')
            conn.execute('INSERT INTO bot_chats VALUES (-20)')
            conn.execute('PRAGMA user_version=17')
        await self.store.initialize()
        self.assertEqual(self.rows('menu_sessions'), [])
        self.assertEqual(self.rows('bot_chats')[0]['chat_id'], -20)

    async def test_input_expires_and_menu_is_reused(self):
        await self.private_command('/menu')
        await self.choose('settings')
        await self.choose('set_timeout')
        with self.store.connect() as conn:
            conn.execute('UPDATE menu_sessions SET expires_at_ms=1')
        await self.private_command('700', mid='expired')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'menu_input_expired')
        self.assertEqual(self.rows('menu_sessions'), [])
        menus = [r for r in self.rows('outbox') if r['destination'].startswith('navigation:ui:')]
        self.assertEqual(len(menus), 1)
        self.assertIsNotNone(menus[0]['message_id'])

    async def test_explicit_menu_opens_new_message_after_previous_navigation(self):
        await self.private_command('/menu')
        await self.choose('settings')
        before = [r for r in self.rows('outbox') if r['destination'].startswith('navigation:ui:')]
        self.assertEqual(len(before), 1)
        await self.private_command('/menu', mid='reopen-menu')
        menus = [r for r in self.rows('outbox') if r['destination'].startswith('navigation:ui:')]
        self.assertEqual(len(menus), 2)
        self.assertIsNone(menus[-1]['message_id'])
        self.assertEqual(menus[-1]['state'], 'pending')
        await self.choose('requests')
        self.assertEqual(len([r for r in self.rows('outbox') if r['destination'].startswith('navigation:ui:')]), 2)

    async def test_menu_uses_saved_user_names(self):
        with self.store.connect() as conn:
            conn.execute("INSERT INTO bot_meta(key,value) VALUES ('user_name:77','Анна Администратор')")
            conn.execute("INSERT INTO bot_meta(key,value) VALUES ('user_name:99','Иван Специалист')")
        await self.private_command('/menu')
        home = self.rows('outbox')[-1]['text']
        self.assertIn('Анна Администратор', home)
        await self.choose('team')
        team = next(r['text'] for r in reversed(self.rows('outbox'))
                    if r['destination'].startswith('navigation:ui:'))
        self.assertIn('Анна Администратор', team)
        self.assertIn('Иван Специалист', team)
        self.assertNotIn('admin: 77', team)
