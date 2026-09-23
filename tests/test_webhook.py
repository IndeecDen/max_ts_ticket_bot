import asyncio
import copy
import json
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from app.config import ConfigError, load_settings
from app.domain.events import IncomingEvent
from app.storage.inbox import InboxStore, InboxSchemaError
from app.web.server import create_app, MAX_BODY_BYTES

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'test-secret-123456789'


def setUpModule():
    global previous_policy
    previous_policy = asyncio.get_event_loop_policy()
    if sys.platform == 'win32':
        # Match the loop used by the receiver's CLI on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def tearDownModule():
    asyncio.set_event_loop_policy(previous_policy)


def message(mid='string-mid_123'):
    return {'update_type': 'message_created', 'timestamp': 1780000000000,
            'message': {'sender': {'user_id': 10}, 'recipient': {'chat_id': -20},
                        'body': {'mid': mid, 'text': 'problem'}}}


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = ROOT / '.test-data'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.settings = load_settings(self.root, {'WEBHOOK_SECRET': SECRET})
        self.store = InboxStore(self.settings.database_path)
        self.client = TestClient(TestServer(create_app(self.settings, self.store)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.assertTrue(self.root.resolve().is_relative_to((ROOT / '.test-data').resolve()))
        self.temp.cleanup()

    async def send(self, payload, secret=SECRET):
        response = await self.client.post('/webhook/max', json=payload,
            headers={'X-Max-Bot-Api-Secret': secret})
        await response.read()
        return response

    async def test_event_is_committed_before_ack_and_survives_restart(self):
        response = await self.send(message())
        self.assertEqual(response.status, 200)
        # A fresh independent connection can see the committed payload immediately.
        restarted = InboxStore(self.settings.database_path)
        await restarted.initialize()
        events = await restarted.pending()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]['payload_json']), message())
        self.assertIsNone(events[0]['processed_at'])

    async def test_duplicate_mid_ignores_delivery_timestamp_changes(self):
        first = message()
        second = copy.deepcopy(first)
        second['timestamp'] += 1
        responses = await asyncio.gather(self.send(first), self.send(second))
        self.assertEqual([r.status for r in responses], [200, 200])
        self.assertEqual(len(await self.store.pending()), 1)

    async def test_duplicate_callback_is_stored_once_but_new_click_is_distinct(self):
        payload = {'update_type': 'message_callback', 'timestamp': 10,
                   'callback': {'callback_id': 'click-a', 'payload': 'take:1'}}
        self.assertEqual((await self.send(payload)).status, 200)
        self.assertEqual((await self.send(payload)).status, 200)
        payload['callback']['callback_id'] = 'click-b'
        self.assertEqual((await self.send(payload)).status, 200)
        self.assertEqual(len(await self.store.pending()), 2)

    async def test_different_edits_are_not_collapsed(self):
        payload = message()
        payload['update_type'] = 'message_edited'
        await self.send(payload)
        payload['message']['body']['text'] = 'updated problem'
        await self.send(payload)
        await self.send(payload)
        self.assertEqual(len(await self.store.pending()), 2)

    async def test_unknown_type_is_preserved_with_extra_fields(self):
        payload = {'update_type': 'future_event', 'timestamp': 10, 'extra': {'nested': [1, 2]}}
        self.assertEqual((await self.send(payload)).status, 200)
        self.assertEqual(json.loads((await self.store.pending())[0]['payload_json']), payload)

    async def test_nullable_body_is_preserved(self):
        payload = message()
        payload['message']['body'] = None
        payload['message']['link'] = {'type': 'forward', 'message': {'text': 'forwarded'}}
        self.assertEqual((await self.send(payload)).status, 200)
        self.assertEqual(len(await self.store.pending()), 1)

    async def test_wrong_missing_or_multiple_secrets_never_touch_storage(self):
        with patch.object(self.store, 'save', new_callable=AsyncMock) as save:
            self.assertEqual((await self.send(message(), 'wrong')).status, 401)
            self.assertEqual((await self.client.post('/webhook/max', json=message())).status, 401)
            response = await self.client.post('/webhook/max', json=message(), headers=[
                ('X-Max-Bot-Api-Secret', SECRET), ('X-Max-Bot-Api-Secret', SECRET)])
            self.assertEqual(response.status, 401)
            save.assert_not_awaited()

    async def test_invalid_json_and_event_envelopes_are_rejected(self):
        payloads = [[], {}, {'update_type': 'message_created', 'timestamp': True},
                    {'update_type': 'message_callback', 'timestamp': 1, 'callback': {}},
                    {'update_type': 'message_created', 'timestamp': 1, 'message': {'body': {'mid': 123}}}]
        for payload in payloads:
            self.assertEqual((await self.send(payload)).status, 400)
        for raw in ('{', '{"update_type":"one","update_type":"two","timestamp":1}',
                    '{"update_type":"future_event","timestamp":1,"x":NaN}'):
            response = await self.client.post('/webhook/max', data=raw,
                headers={'X-Max-Bot-Api-Secret': SECRET, 'Content-Type': 'application/json'})
            self.assertEqual(response.status, 400)
        self.assertEqual(await self.store.pending(), [])

    async def test_wrong_content_type_and_oversized_body(self):
        headers = {'X-Max-Bot-Api-Secret': SECRET}
        self.assertEqual((await self.client.post('/webhook/max', data='text', headers=headers)).status, 415)
        headers['Content-Type'] = 'application/json'
        response = await self.client.post('/webhook/max', data=io.BytesIO(b'x' * (MAX_BODY_BYTES + 1)), headers=headers)
        self.assertEqual(response.status, 413)
        self.assertEqual(await self.store.pending(), [])

    async def test_failed_storage_returns_503_and_retry_can_be_saved(self):
        with patch.object(self.store, 'save', side_effect=sqlite3.OperationalError('private details')):
            response = await self.send(message())
            self.assertEqual(response.status, 503)
            self.assertNotIn('private details', await response.text())
        self.assertEqual((await self.send(message())).status, 200)
        self.assertEqual(len(await self.store.pending()), 1)

    async def test_ack_waits_for_save(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.store.save
        async def delayed(event):
            entered.set()
            await release.wait()
            return await original(event)
        with patch.object(self.store, 'save', side_effect=delayed):
            task = asyncio.create_task(self.send(message()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertFalse(task.done())
            finally:
                release.set()
            self.assertEqual((await asyncio.wait_for(task, 2)).status, 200)

    async def test_health_and_readiness_report_storage_failure(self):
        self.assertEqual((await self.client.get('/healthz')).status, 200)
        response = await self.client.get('/readyz')
        self.assertEqual((await response.json())['mode'], 'collect_only')
        with patch.object(self.store, 'ping', side_effect=sqlite3.OperationalError('disk')):
            self.assertEqual((await self.client.get('/readyz')).status, 503)

    async def test_foreign_database_is_not_migrated(self):
        foreign = self.root / 'foreign.db'
        with closing(sqlite3.connect(foreign)) as conn:
            conn.execute('CREATE TABLE telegram_requests (id INTEGER)')
            conn.commit()
        before = foreign.read_bytes()
        with self.assertRaises(InboxSchemaError):
            await InboxStore(foreign).initialize()
        self.assertEqual(foreign.read_bytes(), before)

    async def test_server_requires_secret_before_database_creation(self):
        path = self.root / 'unused.db'
        with self.assertRaises(ConfigError):
            create_app(replace(self.settings, webhook_secret='', database_path=path))
        self.assertFalse(path.exists())

    async def test_sqlite_work_is_off_event_loop(self):
        import threading
        main_thread = threading.get_ident()
        observed = []
        def save(event):
            observed.append(threading.get_ident())
            return True
        with patch.object(self.store, '_save', side_effect=save):
            await self.store.save(IncomingEvent.parse(message()))
        self.assertNotEqual(observed, [main_thread])


if __name__ == '__main__':
    unittest.main()
