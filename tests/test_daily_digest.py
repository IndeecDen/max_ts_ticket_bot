import asyncio
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

import test_statistics as fixtures
from app.domain.events import IncomingEvent
from app.services import daily_digest
from app.services.delivery import DeliveryQueue
from app.services.processor import InboxProcessor
from app.services.reminders import configure as configure_unassigned


class DailyDigestTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StatisticsTests.asyncSetUp
    asyncTearDown = fixtures.StatisticsTests.asyncTearDown
    command = fixtures.StatisticsTests.command
    rows = fixtures.StatisticsTests.rows

    def now(self, text='2026-09-22T09:00:00', tz='Europe/Moscow', fold=0):
        return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(tz), fold=fold).timestamp() * 1000)

    def configure(self, parts=None):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return daily_digest.configure(conn, parts or ['09:00', '1,2,3,4,5'], actor=77, event_id=None)

    def add(self, status='in_progress', specialist=99, user=10, started='normal'):
        started = self.now() - 3600000 if started == 'normal' else started
        with self.store.connect() as conn:
            return conn.execute('''INSERT INTO requests(chat_id,user_id,author_name,status,due_at_ms,
                created_at_ms,specialist_id,started_at_ms) VALUES (-20,?,'name',?,0,0,?,?)''',
                (user, status, specialist, started)).lastrowid

    def digests(self):
        return [r for r in self.rows('outbox') if r['destination'].startswith('daily:')]

    def mark_sent(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent' WHERE destination LIKE 'daily:%'")

    async def test_default_off_time_boundary_and_scope(self):
        expected = self.add()
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(self.digests(), [])
        self.configure()
        self.add('new', None, user=11)
        self.add('closed', user=12)
        self.add('cancelled', user=13)
        self.add(specialist=None, user=14)
        await self.processor.tick(now_ms=self.now('2026-09-22T08:59:59'))
        self.assertEqual(self.digests(), [])
        await self.processor.tick(now_ms=self.now())
        text = self.digests()[0]['text']
        self.assertIn('заявок в работе — 1.', text)
        self.assertIn(f'#{expected} ·', text)
        self.assertIn('специалист 99 · в работе: 60 мин.', text)

    async def test_concurrent_restart_and_next_day(self):
        self.configure()
        self.add()
        other = InboxProcessor(self.store, self.processor.policy)
        await asyncio.gather(*(p.tick(now_ms=self.now()) for p in (self.processor, other)))
        self.assertEqual(len(self.digests()), 1)
        self.mark_sent()
        await self.store.initialize()
        await other.tick(now_ms=self.now('2026-09-22T23:59:59'))
        self.assertEqual(len(self.digests()), 1)
        await other.tick(now_ms=self.now('2026-09-23T09:00:00'))
        self.assertEqual(len(self.digests()), 2)

    async def test_empty_snapshot_recorded_and_no_late_same_day_digest(self):
        self.configure()
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(self.digests(), [])
        self.assertEqual(self.rows('schedules')[0]['last_run_date'], '2026-09-22')
        self.add()
        await self.processor.tick(now_ms=self.now('2026-09-22T15:00:00'))
        self.assertEqual(self.digests(), [])
        await self.processor.tick(now_ms=self.now('2026-09-23T09:00:00'))
        self.assertEqual(len(self.digests()), 1)

    async def test_downtime_weekday_and_clock_backwards(self):
        self.configure()
        self.add()
        await self.processor.tick(now_ms=self.now('2026-10-03T12:00:00'))
        self.assertEqual(self.digests(), [])  # Saturday
        await self.processor.tick(now_ms=self.now('2026-10-05T12:00:00'))
        self.assertEqual(len(self.digests()), 1)
        self.mark_sent()
        await self.processor.tick(now_ms=self.now('2026-10-02T12:00:00'))
        self.assertEqual(len(self.digests()), 1)
        self.assertEqual(self.rows('schedules')[0]['last_run_date'], '2026-10-05')

    async def test_dst_repeated_hour_and_missing_scheduled_minute(self):
        self.configure(['01:30', '7'])
        self.add()
        def tick(now):
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                return daily_digest.enqueue_daily(conn, -30, 'America/New_York', now)
        self.assertEqual(tick(self.now('2026-11-01T01:30:00', 'America/New_York', 0)), 1)
        self.mark_sent()
        self.assertEqual(tick(self.now('2026-11-01T01:30:00', 'America/New_York', 1)), 0)
        # Use the next spring date, so the monotonic checkpoint remains meaningful.
        self.configure(['02:30', '7'])
        self.assertEqual(tick(self.now('2027-03-14T03:00:00', 'America/New_York')), 1)

    async def test_command_validation_roles_and_reconfiguration_does_not_reset_date(self):
        command = '/set_daily_reminder 09:00 1,2,3,4,5'
        await self.command(command, actor=99)
        self.assertEqual(self.rows('schedules'), [])
        await self.command(command, mid='admin')
        await self.command(command, mid='admin')
        before = self.rows('schedules')
        for i, args in enumerate(['25:00 1', '09:00 8', '9:00 1', 'off extra', '09:00']):
            await self.command('/set_daily_reminder ' + args, mid=f'bad-{i}')
            self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'management_rejected')
        self.assertEqual(self.rows('schedules'), before)
        self.add()
        await self.processor.tick(now_ms=self.now())
        self.mark_sent()
        await self.command('/set_daily_reminder off', mid='off')
        await self.command('/set_daily_reminder 08:00 1,2,3,4,5', mid='enable')
        await self.processor.tick(now_ms=self.now('2026-09-22T12:00:00'))
        self.assertEqual(len(self.digests()), 1)
        await self.command('/get_daily_reminder', mid='get')
        self.assertIn('08:00 (Europe/Moscow)', self.rows('outbox')[-1]['text'])
        await self.command('/set_daily_reminder off', chat=-20, mid='wrongchat')
        with self.store.connect() as conn:
            self.assertTrue(daily_digest.read_settings(conn)['enabled'])

    async def test_failed_previous_batch_blocks_until_reconciled(self):
        self.configure()
        self.add()
        await self.processor.tick(now_ms=self.now())
        for state in ('pending', 'sending', 'uncertain', 'failed'):
            with self.store.connect() as conn:
                conn.execute("UPDATE outbox SET state=? WHERE destination LIKE 'daily:%'", (state,))
            await self.processor.tick(now_ms=self.now('2026-09-23T12:00:00'))
            self.assertEqual(len(self.digests()), 1)
        self.mark_sent()
        await self.processor.tick(now_ms=self.now('2026-09-23T12:00:00'))
        self.assertEqual(len(self.digests()), 2)

    async def test_atomic_rollback_and_paginated_delivery(self):
        self.configure()
        def seed():
            return [self.add(user=i, started=None if i == 1 else 'normal') for i in range(1, 90)]
        ids = await asyncio.to_thread(seed)
        original = daily_digest.enqueue_daily
        def fail(*args):
            original(*args)
            raise RuntimeError('rollback')
        with patch('app.services.daily_digest.enqueue_daily', side_effect=fail):
            with self.assertRaises(RuntimeError):
                await self.processor.tick(now_ms=self.now())
        self.assertEqual(self.rows('outbox'), [])
        self.assertIsNone(self.rows('schedules')[0]['last_run_date'])
        await self.processor.tick(now_ms=self.now())
        pages = self.digests()
        self.assertGreater(len(pages), 1)
        all_text = '\n'.join(r['text'] for r in pages)
        for request_id in ids:
            self.assertEqual(all_text.count(f'#{request_id} ·'), 1)
        self.assertIn('в работе: нет данных', all_text)
        self.assertTrue(all(len(r['text'].encode('utf-16-le')) // 2 <= 4000 for r in pages))
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent' WHERE destination NOT LIKE 'daily:%'")
        client = AsyncMock()
        client.send_message.return_value = 'digest-mid'
        self.assertEqual(await DeliveryQueue(self.store).deliver_one(client, lambda: self.now()), 'sent')
        self.assertEqual(client.send_message.call_args.args, (-30, pages[0]['text'], []))

    async def test_inbox_backlog_defers_snapshot(self):
        self.configure()
        self.add()
        for i in range(2):
            await self.store.save(IncomingEvent.parse({'update_type':'message_created','timestamp':1,
                'message': {'sender':{'user_id':5,'is_bot':True},'recipient':{'chat_id':-20},
                            'body':{'mid':f'backlog-{i}','text':'ignored'}}}))
        await self.processor.tick(limit=1, now_ms=self.now())
        self.assertEqual(self.digests(), [])
        await self.processor.tick(now_ms=self.now())
        self.assertEqual(len(self.digests()), 1)

    async def test_v10_migration_preserves_unassigned_schedule_and_queue(self):
        with self.store.connect() as conn:
            configure_unassigned(conn, ['60','2','08:00','17:00','30'], self.now(), actor=77, event_id=None)
        await self.command('/stats')
        queue, schedules = self.rows('outbox'), self.rows('schedules')
        with self.store.connect() as conn:
            conn.execute('ALTER TABLE schedules DROP COLUMN last_run_date')
            conn.execute('PRAGMA user_version=10')
        await self.store.initialize()
        self.assertEqual(self.rows('outbox'), queue)
        self.assertEqual(self.rows('schedules'), schedules)
        with self.store.connect() as conn:
            self.assertFalse(daily_digest.read_settings(conn)['enabled'])
