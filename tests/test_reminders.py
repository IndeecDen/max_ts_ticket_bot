import asyncio
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import test_statistics as fixtures
from app.services.processor import InboxProcessor
from app.services.reminders import configure, read_settings, in_window, DEFAULT
from app.services.roles import ManagementError


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StatisticsTests.asyncSetUp
    asyncTearDown = fixtures.StatisticsTests.asyncTearDown
    command = fixtures.StatisticsTests.command
    rows = fixtures.StatisticsTests.rows

    def now(self, text='2026-09-22T10:00:00'):
        return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Europe/Moscow')).timestamp() * 1000)

    def configure(self, parts=None, now=None):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return configure(conn, parts or ['60', '1,2,3,4,5', '08:00', '17:00', '30'],
                             self.now() if now is None else now, actor=77, event_id=None)

    def add(self, *, status='new', age=31, user=10, assigned=None):
        with self.store.connect() as conn:
            return conn.execute('''INSERT INTO requests(chat_id,user_id,author_name,status,
                due_at_ms,created_at_ms,specialist_id) VALUES (-20,?,'name',?,0,?,?)''',
                (user, status, self.now() - age * 60000, assigned)).lastrowid

    def reminders(self):
        return [r for r in self.rows('outbox') if r['destination'].startswith('unassigned:')]

    async def test_disabled_by_default_and_only_overdue_unassigned_new(self):
        expected = self.add()
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(self.reminders(), [])
        self.configure()
        self.add(age=30, user=11)
        self.add(status='in_progress', user=12, assigned=99)
        self.add(status='closed', user=13)
        self.add(status='cancelled', user=14)
        self.add(user=15, assigned=99)
        await self.processor.tick(now_ms=self.now())
        text = self.reminders()[0]['text']
        self.assertIn('Заявки без исполнителя: 1.', text)
        self.assertIn(f'#{expected} ·', text)
        self.assertIn('2026-09-22 10:00 +0300', text)

    async def test_restart_and_concurrent_ticks_enqueue_once(self):
        self.configure()
        self.add()
        other = InboxProcessor(self.store, self.processor.policy)
        await asyncio.gather(*(p.tick(now_ms=self.now()) for p in (self.processor, other)))
        self.assertEqual(len(self.reminders()), 1)
        await self.store.initialize()
        await other.tick(now_ms=self.now() + 60000)
        self.assertEqual(len(self.reminders()), 1)
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent' WHERE destination LIKE 'unassigned:%'")
        await other.tick(now_ms=self.now() + 3600000)
        self.assertEqual(len(self.reminders()), 2)

    async def test_outstanding_or_ambiguous_batch_blocks_accumulation(self):
        self.configure()
        self.add()
        await self.processor.tick(now_ms=self.now())
        for state in ('pending', 'sending', 'uncertain', 'failed'):
            with self.store.connect() as conn:
                conn.execute("UPDATE outbox SET state=? WHERE destination LIKE 'unassigned:%'", (state,))
            await self.processor.tick(now_ms=self.now('2026-09-23T12:00:00'))
            self.assertEqual(len(self.reminders()), 1)

    async def test_downtime_skips_missed_intervals(self):
        self.configure(now=self.now('2026-09-01T08:00:00'))
        self.add()
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(len(self.reminders()), 1)
        self.assertEqual(self.rows('schedules')[0]['next_at_ms'], self.now() + 3600000)

    async def test_atomic_rollback_after_outbox_insert(self):
        self.configure()
        self.add()
        from app.services.reminders import enqueue_reminders
        def fail(*args):
            enqueue_reminders(*args)
            raise RuntimeError('interrupted transaction')
        with patch('app.services.processor.enqueue_reminders', side_effect=fail):
            with self.assertRaises(RuntimeError):
                await self.processor.tick(now_ms=self.now())
        self.assertEqual(self.rows('outbox'), [])
        self.assertEqual(self.rows('schedules')[0]['run_number'], 0)
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(len(self.reminders()), 1)

    async def test_commands_permissions_validation_idempotence_and_disable(self):
        command = '/set_unassigned_reminder 60 1,2,3,4,5 08:00 17:00 30'
        await self.command(command, actor=99)
        self.assertEqual(self.rows('schedules'), [])
        await self.command(command, mid='admin')
        before = self.rows('schedules')
        await self.command(command, mid='admin')
        await self.command(command, mid='same-settings')
        self.assertEqual(self.rows('schedules'), before)
        self.assertEqual(len([r for r in self.rows('management_log') if r['action']=='unassigned_reminder_set']), 1)
        await self.command('/set_unassigned_reminder 0 2 08:00 17:00 30', mid='bad')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'management_rejected')
        self.assertEqual(self.rows('schedules'), before)
        await self.command('/get_unassigned_reminder', mid='get')
        self.assertIn('Europe/Moscow', self.rows('outbox')[-1]['text'])
        await self.command('/set_unassigned_reminder off', mid='off')
        self.add()
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(self.reminders(), [])
        await self.command(command, chat=-20, mid='wrongchat')
        with self.store.connect() as conn:
            self.assertFalse(read_settings(conn)['enabled'])

    async def test_window_weekdays_midnight_and_dst(self):
        settings = {**DEFAULT, 'weekdays': [2], 'work_start': '22:00', 'work_end': '06:00'}
        for date, expected in [('2026-09-22T21:59', False), ('2026-09-22T22:00', True),
                               ('2026-09-23T05:59', True), ('2026-09-23T06:00', False),
                               ('2026-09-23T22:00', False)]:
            self.assertEqual(in_window(settings, datetime.fromisoformat(date)), expected)
        settings = {**DEFAULT, 'weekdays': [7], 'work_start': '01:00', 'work_end': '03:00'}
        for fold in (0, 1):
            self.assertTrue(in_window(settings, datetime(2026, 11, 1, 1, 30,
                            tzinfo=ZoneInfo('America/New_York'), fold=fold)))
        self.configure()
        self.add()
        await self.processor.tick(now_ms=self.now('2026-09-22T17:00:00'))
        self.assertEqual(self.reminders(), [])

    async def test_paginated_list_preserves_ids_and_pending_inbox_delays_schedule(self):
        self.configure()
        ids = [self.add(user=i) for i in range(1, 151)]
        # Any supported inbox backlog defers the snapshot until actions have drained.
        from app.domain.events import IncomingEvent
        for i in range(2):
            await self.store.save(IncomingEvent.parse({'update_type':'message_created','timestamp':1,
                'message': {'sender': {'user_id':5,'is_bot':True}, 'recipient':{'chat_id':-20},
                            'body': {'mid':f'backlog-{i}','text':'ignored'}}}))
        await self.processor.tick(limit=1, now_ms=self.now())
        self.assertEqual(self.reminders(), [])
        await self.processor.tick(now_ms=self.now())
        pages = self.reminders()
        self.assertGreater(len(pages), 1)
        text = '\n'.join(r['text'] for r in pages)
        for request_id in ids:
            self.assertEqual(text.count(f'#{request_id} ·'), 1)
        self.assertTrue(all(len(r['text'].encode('utf-16-le')) // 2 <= 4000 for r in pages))

    async def test_v9_migration_keeps_queue_and_new_schedule_disabled(self):
        await self.command('/stats')
        before = self.rows('outbox')
        with self.store.connect() as conn:
            conn.execute('DROP TABLE schedules')
            conn.execute('PRAGMA user_version=9')
        await self.store.initialize()
        self.assertEqual(self.rows('outbox'), before)
        self.assertEqual(self.rows('schedules'), [])
        for parts in (['1','8','00:00','01:00','1'], ['1','1','00:00','00:00','1'],
                      ['1','1','25:00','01:00','1'], ['off','extra']):
            with self.assertRaises(ManagementError):
                self.configure(parts)
