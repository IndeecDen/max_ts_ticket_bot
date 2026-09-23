import asyncio
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

import test_statistics as fixtures
from app.services import notices
from app.services.delivery import DeliveryQueue
from app.services.roles import ManagementError


class NoticeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StatisticsTests.asyncSetUp
    asyncTearDown = fixtures.StatisticsTests.asyncTearDown
    command = fixtures.StatisticsTests.command
    rows = fixtures.StatisticsTests.rows

    def now(self, text='2026-09-22T10:00:00'):
        return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Europe/Moscow')).timestamp() * 1000)

    def add(self, args='- - Ведутся работы', tz='Europe/Moscow'):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return notices.add(conn, '/set_notice ' + args, tz, self.now(), actor=77, event_id=None)

    def active(self, date):
        with self.store.connect() as conn:
            return notices.active_texts(conn, 'Europe/Moscow', self.now(date))

    def request(self, user=10):
        with self.store.connect() as conn:
            return conn.execute('''INSERT INTO requests(chat_id,user_id,author_name,status,due_at_ms,created_at_ms)
                VALUES (-20,?,'name','new',0,0)''', (user,)).lastrowid

    def card(self, request_id, destination='client'):
        return next(r for r in self.rows('outbox') if r['request_id']==request_id and r['destination']==destination)

    async def test_day_night_and_expiry_boundaries(self):
        self.add('- 08:00-17:00 День')
        self.add('- 22:00-06:00 Ночь')
        self.add('2026-09-23T12:00 - Срок')
        for date, expected in [('2026-09-23T05:59:00', ['Ночь','Срок']),
                               ('2026-09-23T06:00:00', ['Срок']),
                               ('2026-09-23T08:00:00', ['День','Срок']),
                               ('2026-09-23T12:00:00', ['День']),
                               ('2026-09-23T17:00:00', []),
                               ('2026-09-23T22:00:00', ['Ночь'])]:
            self.assertEqual(self.active(date), expected)

    async def test_client_snapshot_survives_deletion_and_restart_and_clears_on_take(self):
        notice_id = self.add()
        request_id = self.request()
        await self.processor.tick(now_ms=self.now())
        original = self.card(request_id)
        self.assertIn('📢 Ведутся работы', original['text'])
        self.assertNotIn('Ведутся работы', self.card(request_id, 'work')['text'])
        with self.store.connect() as conn:
            notices.remove(conn, str(notice_id), actor=77, event_id=None)
        await self.store.initialize()
        await self.processor.tick(now_ms=self.now() + 60000)
        self.assertEqual(self.card(request_id), original)
        with self.store.connect() as conn:
            conn.execute("UPDATE requests SET status='in_progress',specialist_id=99,revision=revision+1 WHERE id=?", (request_id,))
        await self.processor.tick(now_ms=self.now() + 120000)
        self.assertNotIn('Ведутся работы', self.card(request_id)['text'])
        next_id = self.request(user=11)
        await self.processor.tick(now_ms=self.now())
        self.assertNotIn('Ведутся работы', self.card(next_id)['text'])

    async def test_permissions_commands_and_event_deduplication(self):
        await self.command('/set_notice - - Текст', actor=99)
        self.assertEqual(self.rows('announcements'), [])
        await self.command('/set_notice - - Текст', mid='admin')
        await self.command('/set_notice - - Текст', mid='admin')
        self.assertEqual(len(self.rows('announcements')), 1)
        self.assertEqual(len([r for r in self.rows('management_log') if r['action']=='notice_add']), 1)
        await self.command('/get_notice', mid='list')
        self.assertIn('Текст', self.rows('outbox')[-1]['text'])
        await self.command('/del_notice all', actor=99, mid='forbidden')
        await self.command('/del_notice all', chat=-20, mid='wrongchat')
        self.assertEqual(len(self.rows('announcements')), 1)
        await self.command('/del_notice all', mid='delete')
        self.assertEqual(self.rows('announcements'), [])

    async def test_validation_and_dst_rejects_ambiguous_expiry(self):
        for args in ['-', '- -', '- 00:00-00:00 text', '- 25:00-08:00 text',
                     '2026-02-30T12:00 - text', '2026-09-21T12:00 - text',
                     '2026-09-22T10:00 - text', '- - ' + '😀' * 151]:
            with self.assertRaises(ManagementError):
                self.add(args)
        for date in ('2026-11-01T01:30', '2027-03-14T02:30'):
            with self.assertRaises(ManagementError):
                self.add(date + ' - text', tz='America/New_York')
        self.assertEqual(self.rows('announcements'), [])
        self.add('2026-11-01T03:30 - text', tz='America/New_York')

    async def test_maximum_notice_count_and_utf16_card_delivery(self):
        for i in range(10):
            self.add('- - ' + '😀' * 150)
        with self.assertRaises(ManagementError):
            self.add()
        request_id = self.request()
        await self.processor.tick(now_ms=self.now())
        card = self.card(request_id)
        self.assertEqual(card['text'].count('😀'), 1500)
        self.assertLessEqual(len(card['text'].encode('utf-16-le')) // 2, 4000)
        self.assertEqual(len([r for r in self.rows('outbox') if r['request_id']==request_id]), 2)
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent' WHERE destination='work'")
        client = AsyncMock()
        client.send_message.return_value = 'mid'
        self.assertEqual(await DeliveryQueue(self.store).deliver_one(client, lambda: self.now()), 'sent')
        self.assertEqual(client.send_message.call_args.args[1], card['text'])

    async def test_atomic_command_rollback_and_retry(self):
        with patch('app.services.processor.enqueue_cards', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                await self.command('/set_notice - - Текст')
        self.assertEqual(self.rows('announcements'), [])
        self.assertEqual(self.rows('outbox'), [])
        await self.processor.tick(now_ms=0)
        self.assertEqual(len(self.rows('announcements')), 1)
        self.assertEqual(len(self.rows('outbox')), 1)

    async def test_list_preserves_multiline_literal_text_and_all_entries(self):
        self.add('- - <b>Текст</b>\nВторая строка & ещё')
        self.add('- - Второе')
        await self.command('/get_notice')
        replies = [r for r in self.rows('outbox') if r['destination'].startswith('notice:')]
        self.assertEqual(len(replies), 2)
        self.assertIn('<b>Текст</b>\nВторая строка & ещё', replies[0]['text'])
        await self.command('/del_notice bad', mid='bad')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'management_rejected')
        self.assertEqual(len(self.rows('announcements')), 2)

    async def test_v11_migration_preserves_queue_and_schedules(self):
        await self.command('/set_daily_reminder 09:00 1,2,3,4,5')
        queue, schedules = self.rows('outbox'), self.rows('schedules')
        with self.store.connect() as conn:
            conn.execute('DROP TABLE announcements')
            conn.execute('PRAGMA user_version=11')
        await self.store.initialize()
        self.assertEqual(self.rows('outbox'), queue)
        self.assertEqual(self.rows('schedules'), schedules)
        self.assertEqual(self.rows('announcements'), [])
