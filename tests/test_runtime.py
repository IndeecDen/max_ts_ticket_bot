import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from app.config import ConfigError, load_settings
from app.services.processor import ProcessingPolicy
from app.services.runtime import BotRuntime
from app.storage.inbox import InboxStore
from app.web.server import create_app, RUNTIME


def setUpModule():
    global previous_policy
    previous_policy = asyncio.get_event_loop_policy()
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def tearDownModule():
    asyncio.set_event_loop_policy(previous_policy)


class FakeMax:
    def __init__(self):
        self.send_message = AsyncMock(side_effect=lambda chat, text, attachments=None: f'mid-{chat}')
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


async def eventually(predicate, seconds=4):
    async with asyncio.timeout(seconds):
        while not await predicate():
            await asyncio.sleep(0.02)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = load_settings(self.root, {'WEBHOOK_SECRET': 'test-secret', 'MAX_BOT_TOKEN': 'fake'})
        self.store = InboxStore(self.settings.database_path)
        self.policy = ProcessingPolicy(frozenset({-20}), frozenset({99}), -30, 1)
        self.fake = FakeMax()
        self.app = create_app(self.settings, self.store, policy=self.policy, client_factory=lambda _: self.fake)
        self.app[RUNTIME].interval = 0.02
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_webhook_to_cards_and_restart_without_resending(self):
        payload = {'update_type': 'message_created', 'timestamp': 1, 'message': {
            'sender': {'user_id': 10, 'is_bot': False}, 'recipient': {'chat_id': -20},
            'body': {'mid': 'incoming', 'text': 'Не работает'}}}
        for _ in range(2):
            response = await self.client.post('/webhook/max', json=payload,
                                              headers={'X-Max-Bot-Api-Secret': 'test-secret'})
            self.assertEqual(response.status, 200)
            await response.read()
        async def sent():
            return (await self.app[RUNTIME].queue.status()).get('sent') == 2
        await eventually(sent)
        self.assertEqual(self.fake.send_message.await_count, 2)
        response = await self.client.get('/readyz')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['mode'], 'bot')
        await self.client.close()
        self.assertTrue(self.fake.closed)
        self.assertTrue(all(t.done() for t in self.app[RUNTIME].tasks.values()))
        second = BotRuntime(self.store, self.policy, interval=0.02)
        await second.start(self.fake)
        try:
            async def checked():
                return not any(second.errors.values())
            await eventually(checked)
            self.assertEqual(self.fake.send_message.await_count, 2)
        finally:
            await second.stop(1)

    async def test_inbox_error_does_not_stop_delivery_and_recovers(self):
        runtime = self.app[RUNTIME]
        with patch.object(runtime.processor, '_tick', side_effect=sqlite3.OperationalError('private-payload')):
            async def failed():
                return runtime.errors['inbox'] == 'OperationalError'
            with self.assertLogs('max_ticket_bot.runtime', level='ERROR') as captured:
                await eventually(failed)
            self.assertNotIn('private-payload', str(captured.output))
            response = await self.client.get('/readyz')
            self.assertEqual(response.status, 503)
            await response.read()
            self.assertFalse(runtime.tasks['delivery'].done())
        async def recovered():
            return runtime.errors['inbox'] is None
        await eventually(recovered)

    async def test_graceful_stop_waits_for_current_send(self):
        runtime = self.app[RUNTIME]
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(_client):
            entered.set()
            await release.wait()
        with patch.object(runtime.queue, 'deliver_one', side_effect=delayed):
            await asyncio.wait_for(entered.wait(), 2)
            closing = asyncio.create_task(self.client.close())
            await asyncio.sleep(0.05)
            self.assertFalse(closing.done())
            self.assertFalse(self.fake.closed)
            release.set()
            await asyncio.wait_for(closing, 2)
        self.assertTrue(self.fake.closed)

    async def test_forced_stop_cancels_worker_and_unready(self):
        runtime = self.app[RUNTIME]
        entered = asyncio.Event()
        async def stuck(_client):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(runtime.queue, 'deliver_one', side_effect=stuck):
            await asyncio.wait_for(entered.wait(), 2)
            await runtime.stop(0.01)
        self.assertTrue(all(t.done() for t in runtime.tasks.values()))
        self.assertFalse((await runtime.health())[0])

    async def test_run_requires_token_before_database_creation(self):
        settings = load_settings(self.root / 'unused', {'WEBHOOK_SECRET': 'test-secret'})
        with self.assertRaises(ConfigError):
            create_app(settings, policy=self.policy)
        self.assertFalse(settings.database_path.exists())

    async def test_interrupted_sending_reported_without_retry(self):
        await self.client.close()
        with self.store.connect() as conn:
            conn.execute("""INSERT INTO outbox(request_id,destination,chat_id,text,state)
                VALUES (1,'work',-30,'test','sending')""")
        runtime = BotRuntime(self.store, self.policy, interval=0.02)
        await runtime.start(self.fake)
        try:
            async def checked():
                return not any(runtime.errors.values())
            await eventually(checked)
            ok, details = await runtime.health()
            self.assertFalse(ok)
            self.assertEqual(details['interrupted_sends'], 1)
            self.fake.send_message.assert_not_awaited()
        finally:
            await runtime.stop(1)

    async def test_failed_commit_after_send_stays_unready(self):
        runtime = self.app[RUNTIME]
        with self.store.connect() as conn:
            conn.execute("""INSERT INTO outbox(request_id,destination,chat_id,text)
                VALUES (1,'work',-30,'test')""")
        with patch.object(runtime.queue, '_finish', side_effect=sqlite3.OperationalError('disk')):
            async def stranded():
                return (await runtime.queue.status()).get('sending') == 1 and not runtime.delivery_active
            with self.assertLogs('max_ticket_bot.runtime', level='ERROR'):
                await eventually(stranded)
        # The following empty poll must not conceal a sent-but-uncommitted job.
        await asyncio.sleep(0.1)
        self.assertFalse((await runtime.health())[0])
        self.fake.send_message.assert_awaited_once()
