import asyncio
import io
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from openpyxl import load_workbook
from app.adapters.max.client import MaxAPIError, MaxClient
from app.config import load_settings
from app.services.delivery import DeliveryQueue
from test_foundation import Session, Response
import test_statistics as statistics_fixtures


class ReportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = statistics_fixtures.StatisticsTests.asyncSetUp
    asyncTearDown = statistics_fixtures.StatisticsTests.asyncTearDown
    command = statistics_fixtures.StatisticsTests.command
    rows = statistics_fixtures.StatisticsTests.rows

    async def test_permissions_scope_snapshot_and_duplicate(self):
        await self.command('/stats_xlsx', actor=99)
        self.assertEqual(self.rows('outbox'), [])
        await self.command('/my_stats_xlsx', actor=99, mid='own')
        await self.command('/my_stats_xlsx', actor=99, mid='own')
        jobs = self.rows('outbox')
        self.assertEqual(len(jobs), 1)
        snapshot = json.loads(jobs[0]['report_json'])
        self.assertEqual(snapshot['specialist_id'], 99)
        self.assertEqual(snapshot['totals']['closed'], 0)
        with self.store.connect() as conn:
            conn.execute('''INSERT INTO requests(chat_id,user_id,author_name,status,due_at_ms,
                created_at_ms,specialist_id,ended_at_ms) VALUES (-20,10,'name','closed',0,0,99,?)''',
                (snapshot['period']['from_ms'] + 1000,))
        self.assertEqual(json.loads(self.rows('outbox')[0]['report_json'])['totals']['closed'], 0)
        with self.store.connect() as conn:
            conn.execute('DELETE FROM requests')
        await self.command('/stats_xlsx all', mid='invalid')
        self.assertIsNone(self.rows('outbox')[-1]['report_json'])
        await self.command('/stats_xlsx', chat=-20, mid='wrongchat')
        self.assertEqual(len(self.rows('outbox')), 2)

    async def test_upload_delay_restart_not_ready_and_delivery(self):
        await self.command('/stats_xlsx')
        client = AsyncMock()
        client.upload_file.return_value = 'file-token'
        client.send_message.side_effect = [MaxAPIError('processing', retryable=True), 'mid']
        queue = DeliveryQueue(self.store)
        self.assertEqual(await queue.deliver_one(client, lambda: 100), 'pending')
        client.send_message.assert_not_called()
        content, filename = client.upload_file.call_args.args
        book = load_workbook(io.BytesIO(content))
        self.assertEqual(book.active['A1'].value, 'MAX · Статистика завершённых заявок')
        book.close()
        self.assertTrue(filename.endswith('.xlsx'))
        await self.store.initialize()
        queue = DeliveryQueue(self.store)
        self.assertIsNone(await queue.deliver_one(client, lambda: 5099))
        self.assertEqual(await queue.deliver_one(client, lambda: 5100), 'pending')
        self.assertEqual(await queue.deliver_one(client, lambda: 15100), 'sent')
        self.assertEqual(client.upload_file.await_count, 1)
        self.assertEqual(client.send_message.call_args.args[2],
                         [{'type': 'file', 'payload': {'token': 'file-token'}}])

    async def test_upload_retry_then_uncertain_message_is_not_repeated(self):
        await self.command('/stats_xlsx')
        client = AsyncMock()
        client.upload_file.side_effect = [MaxAPIError('network', retryable=True), 'token']
        client.send_message.side_effect = MaxAPIError('timeout', retryable=True, uncertain=True)
        queue = DeliveryQueue(self.store)
        self.assertEqual(await queue.deliver_one(client, lambda: 100), 'pending')
        self.assertEqual(await queue.deliver_one(client, lambda: 5100), 'pending')
        self.assertEqual(await queue.deliver_one(client, lambda: 10100), 'uncertain')
        self.assertIsNone(await queue.deliver_one(client, lambda: 999999))
        self.assertEqual(client.send_message.await_count, 1)

    async def test_token_commit_failure_prevents_message(self):
        await self.command('/stats_xlsx')
        client = AsyncMock()
        client.upload_file.return_value = 'token'
        queue = DeliveryQueue(self.store)
        with patch.object(queue, '_save_file_token', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                await queue.deliver_one(client, lambda: 100)
        client.send_message.assert_not_called()
        self.assertEqual(self.rows('outbox')[0]['state'], 'sending')

    async def test_generation_failure_and_migration(self):
        await self.command('/stats')
        before = self.rows('outbox')
        with self.store.connect() as conn:
            conn.execute('ALTER TABLE outbox DROP COLUMN report_json')
            conn.execute('ALTER TABLE outbox DROP COLUMN file_token')
            conn.execute('PRAGMA user_version=8')
        await self.store.initialize()
        self.assertEqual(self.rows('outbox'), before)
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET report_json='{}'")
        queue = DeliveryQueue(self.store)
        with patch.object(queue, '_report_bytes', side_effect=OSError('disk')):
            self.assertEqual(await queue.deliver_one(AsyncMock(), lambda: 100), 'failed')


class UploadContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_multipart_contract_and_no_authorization_to_upload_host(self):
        session = Session(Response({'url': 'https://fu.oneme.ru/api/upload.do?sig=secret'}),
                          Response({'fileId': 1, 'token': 'token'}))
        settings = load_settings(Path('.test-data'), {'MAX_BOT_TOKEN': 'bot-secret'})
        async with MaxClient(settings, session=session) as client:
            self.assertEqual(await client.upload_file(b'xlsx', 'report.xlsx'), 'token')
        self.assertEqual(session.calls[0][1]['params'], {'type': 'file'})
        kwargs = session.calls[1][1]
        self.assertNotIn('headers', kwargs)
        self.assertFalse(kwargs['allow_redirects'])
        self.assertIsNotNone(kwargs['ssl'])
        self.assertTrue(kwargs['data'].is_multipart)
        self.assertEqual(kwargs['data']._fields[0][0]['name'], 'data')

    async def test_reject_bad_upload_url_and_sanitize_network_error(self):
        settings = load_settings(Path('.test-data'), {'MAX_BOT_TOKEN': 'bot-secret'})
        for url in ('http://fu.oneme.ru/x', 'https://user:secret@fu.oneme.ru/x', 'https://fu.oneme.ru:bad/x'):
            session = Session(Response({'url': url}))
            async with MaxClient(settings, session=session) as client:
                with self.assertRaises(MaxAPIError) as error:
                    await client.upload_file(b'xlsx', 'report.xlsx')
            self.assertNotIn('secret', str(error.exception))
            self.assertEqual(len(session.calls), 1)
        session = Session(Response({'url': 'https://fu.oneme.ru/x'}), Response(error=asyncio.TimeoutError('secret')))
        async with MaxClient(settings, session=session) as client:
            with self.assertRaises(MaxAPIError) as error:
                await client.upload_file(b'xlsx', 'report.xlsx')
        self.assertTrue(error.exception.retryable)
        self.assertFalse(error.exception.uncertain)
        self.assertNotIn('secret', str(error.exception))

    async def test_attachment_processing_error_safe_retry_even_non_2xx(self):
        settings = load_settings(Path('.test-data'), {'MAX_BOT_TOKEN': 'bot-secret'})
        for status in (200, 400, 503):
            session = Session(Response({'code': 'attachment.not.ready', 'message': 'secret'}, status=status))
            async with MaxClient(settings, session=session) as client:
                with self.assertRaises(MaxAPIError) as error:
                    await client.send_message(-30, 'report', [])
            self.assertTrue(error.exception.retryable)
            self.assertFalse(error.exception.uncertain)
            self.assertNotIn('secret', str(error.exception))
