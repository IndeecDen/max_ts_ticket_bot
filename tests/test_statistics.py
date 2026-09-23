import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.domain.events import IncomingEvent
from app.services.processor import InboxProcessor, ProcessingPolicy
from app.services.roles import ManagementError
from app.services.statistics import Statistics, period_bounds, render_statistics
from app.storage.inbox import InboxStore


class StatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = InboxStore(Path(self.temp.name) / 'max.db')
        await self.store.initialize()
        self.processor = InboxProcessor(self.store, ProcessingPolicy(
            frozenset({-20}), frozenset({99, 98}), -30, 10, frozenset({77})))
        await self.processor.initialize_roles()
        self.reporter = Statistics(self.store)
        self.period = period_bounds(['2026-09-22', '2026-09-22'], 'Europe/Moscow')

    async def asyncTearDown(self):
        self.temp.cleanup()

    def add(self, ended, *, specialist=99, created=None, started=None, status='closed', user=10):
        with self.store.connect() as conn:
            conn.execute('''INSERT INTO requests(chat_id,user_id,author_name,status,due_at_ms,created_at_ms,
                specialist_id,started_at_ms,ended_at_ms) VALUES (-20,?,'name',?,0,?,?,?,?)''',
                (user, status, created if created is not None else ended - 90000,
                 specialist, started if started is not None else ended - 60000, ended))

    async def report(self, specialist=None):
        return await self.reporter.read(['2026-09-22', '2026-09-22'], 'Europe/Moscow', specialist)

    async def command(self, text, actor=77, chat=-30, mid='command'):
        await self.store.save(IncomingEvent.parse({'update_type': 'message_created', 'timestamp': 1,
            'message': {'sender': {'user_id': actor, 'is_bot': False}, 'recipient': {'chat_id': chat},
                        'body': {'mid': mid, 'text': text}}}))
        await self.processor.tick(now_ms=0)

    def rows(self, table):
        with self.store.connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]

    async def test_inclusive_dates_exclusive_next_day_and_completion_basis(self):
        start, end = self.period['from_ms'], self.period['until_ms']
        for ended in (start - 1, start, end - 1, end):
            self.add(ended)
        self.add(start + 1000, status='cancelled')
        self.add(start + 2000, status='in_progress')
        report = await self.report()
        self.assertEqual(report['totals']['closed'], 2)
        self.assertEqual(report['totals']['wait_ms'], 60000)
        self.assertEqual(report['totals']['work_ms'], 120000)

    async def test_personal_filter_and_weighted_totals(self):
        base = self.period['from_ms'] + 1000000
        self.add(base, specialist=99, created=base - 10000, started=base - 5000)
        self.add(base, specialist=98, created=base - 50000, started=base - 15000)
        self.add(base, specialist=98, created=base - 50000, started=base - 25000)
        personal = await self.report(99)
        self.assertEqual(personal['totals']['closed'], 1)
        overall = await self.report()
        self.assertEqual(overall['totals']['work_ms'], 45000)
        self.assertIn('Среднее выполнение: 0:00:15', render_statistics(overall)[0])

    async def test_missing_or_negative_durations_do_not_count_as_zero_samples(self):
        base = self.period['from_ms'] + 1000000
        self.add(base)
        self.add(base, created=base + 100, started=base + 100)
        with self.store.connect() as conn:
            conn.execute('UPDATE requests SET started_at_ms=NULL WHERE id=2')
        self.add(base, created=base + 200, started=base + 100)
        report = await self.report()
        self.assertEqual(report['totals']['closed'], 3)
        self.assertEqual(report['totals']['work_samples'], 1)
        self.assertEqual(report['totals']['wait_samples'], 1)

    async def test_calendar_periods_timezone_and_dst(self):
        now = datetime(2026, 9, 21, 22, 30, tzinfo=timezone.utc)
        period = period_bounds(['day'], 'Europe/Moscow', now)
        self.assertEqual(period['start_date'], '2026-09-22')
        self.assertEqual(period_bounds(['week'], 'Europe/Moscow', now)['start_date'], '2026-09-16')
        self.assertEqual(period_bounds(['month'], 'Europe/Moscow', now)['start_date'], '2026-08-24')
        dst = period_bounds(['2026-03-08', '2026-03-08'], 'America/New_York')
        self.assertEqual(dst['until_ms'] - dst['from_ms'], 23 * 3600000)
        for parts in (['2026-09-23', '2026-09-22'], ['2026-02-30', '2026-03-01'], ['9999-12-31', '9999-12-31'], ['all']):
            with self.assertRaises(ManagementError):
                period_bounds(parts, 'Europe/Moscow')

    async def test_permissions_and_personal_scope_cannot_be_overridden(self):
        self.add(self.period['from_ms'] + 100000, specialist=99)
        self.add(self.period['from_ms'] + 100000, specialist=98)
        await self.command('/stats 2026-09-22 2026-09-22', actor=99)
        self.assertFalse(any(r['destination'].startswith('stats:') for r in self.rows('outbox')))
        await self.command('/my_stats 2026-09-22 2026-09-22', actor=99, mid='own')
        reply = next(r['text'] for r in self.rows('outbox') if r['destination'].startswith('stats:'))
        self.assertIn('Закрыто: 1', reply)
        self.assertNotIn('специалист 98', reply)
        await self.command('/my_stats 98', actor=99, mid='invalid')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'statistics_rejected')
        await self.command('/my_stats', actor=10, mid='client')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'forbidden_statistics')
        before = len(self.rows('outbox'))
        await self.command('/stats', actor=77, chat=-20, mid='wrongchat')
        self.assertEqual(len(self.rows('outbox')), before)

    async def test_replay_and_queue_failure_do_not_duplicate_report(self):
        await self.command('/stats 2026-09-22 2026-09-22')
        await self.command('/stats 2026-09-22 2026-09-22')
        self.assertEqual(len(self.rows('outbox')), 1)
        self.assertIn('Нет завершённых', self.rows('outbox')[0]['text'])
        with patch('app.services.processor.enqueue_cards', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                await self.command('/stats', mid='second')
        self.assertEqual(len(self.rows('outbox')), 1)
        await self.processor.tick(now_ms=0)
        self.assertEqual(len(self.rows('outbox')), 2)

    async def test_large_report_pages_preserve_all_groups(self):
        report = await self.report()
        report['groups'] = [{'chat_id': -i, 'specialist_id': i, 'closed': 1,
                             'wait_ms': 1000, 'work_ms': 2000} for i in range(1, 301)]
        pages = render_statistics(report)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 4000 for p in pages))
        self.assertEqual(sum(p.count('Чат ') for p in pages), 300)

    async def test_v7_migration_preserves_report_values(self):
        self.add(self.period['from_ms'] + 100000)
        before = await self.report()
        with self.store.connect() as conn:
            conn.execute('DROP INDEX idx_statistics_completed')
            conn.execute('PRAGMA user_version=7')
        await self.store.initialize()
        self.assertEqual(await self.report(), before)
