import asyncio
import io
import ssl
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

from app.config import ConfigError, Settings, load_settings
from app.adapters.max.client import MaxAPIError, MaxClient
from app.main import main, check_api

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.test-data'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to((ROOT / '.test-data').resolve()))
        self.temp.cleanup()

    def test_offline_defaults_need_no_token_and_create_no_database(self):
        settings = load_settings(self.root, {})
        self.assertEqual(settings.token, '')
        self.assertEqual(settings.database_path, self.root / 'data' / 'max_bot.db')
        self.assertFalse(settings.database_path.exists())

    def test_local_dotenv_and_environment_precedence(self):
        (self.root / '.env').write_text('MAX_BOT_TOKEN=file-secret\nWORK_CHAT_ID=-123\n', encoding='utf-8')
        settings = load_settings(self.root, {'MAX_BOT_TOKEN': 'env-secret'})
        self.assertEqual(settings.token, 'env-secret')
        self.assertEqual(settings.work_chat_id, -123)
        self.assertNotIn('env-secret', repr(settings))

    def test_rejects_insecure_or_credential_bearing_urls(self):
        for url in ('http://example.com', 'https://user:secret@example.com',
                    'https://example.com?token=secret', 'https://example.com/path',
                    'https://example.com:bad', 'https://bad host.com', 'https://example.com#secret'):
            with self.subTest(url=url), self.assertRaises(ConfigError) as caught:
                load_settings(self.root, {'MAX_API_BASE_URL': url})
            self.assertNotIn('secret', str(caught.exception))

    def test_rejects_invalid_numbers_and_timezone(self):
        for key, value in [('WORK_CHAT_ID', '0'), ('WORK_CHAT_ID', str(2**63)),
                           ('WORK_CHAT_ID', 'abc'), ('HTTP_TIMEOUT_SECONDS', 'nan'),
                           ('HTTP_TIMEOUT_SECONDS', '-1'), ('HTTP_TIMEOUT_SECONDS', 'inf'),
                           ('TIMEZONE', 'Unknown/Unknown')]:
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                load_settings(self.root, {key: value})

    def test_missing_ca_and_header_injection_are_rejected(self):
        for env in ({'MAX_CA_BUNDLE': 'missing.pem'}, {'MAX_BOT_TOKEN': 'secret\nInjected: yes'}):
            with self.assertRaises(ConfigError):
                load_settings(self.root, env)

    def test_offline_command_does_not_contact_api_or_print_token(self):
        settings = load_settings(self.root, {'MAX_BOT_TOKEN': 'top-secret'})
        output = io.StringIO()
        with patch('app.main.load_settings', return_value=settings), \
             patch('app.main.check_api', new_callable=AsyncMock) as api, redirect_stdout(output):
            self.assertEqual(main(['check-config']), 0)
        api.assert_not_called()
        self.assertNotIn('top-secret', output.getvalue())


class Response:
    def __init__(self, payload=None, status=200, headers=None, error=None):
        self.status, self.headers = status, headers or {}
        self.payload, self.error = payload, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.close = AsyncMock()

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return next(self.responses)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings('top-secret', 'https://platform-api2.max.ru', None,
                                 10, 'Europe/Moscow', ROOT / 'data/max_bot.db', 'INFO')

    async def test_get_me_sends_token_only_in_header_with_tls_and_no_redirects(self):
        session = Session(Response({'user_id': 100, 'name': 'Bot'}))
        async with MaxClient(self.settings, session=session) as client:
            self.assertEqual((await client.get_me())['user_id'], 100)
        args, options = session.calls[0]
        self.assertEqual(args, ('GET', 'https://platform-api2.max.ru/me'))
        self.assertEqual(options['headers']['Authorization'], 'top-secret')
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(options['ssl'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(options['ssl'].check_hostname)
        self.assertEqual(options['timeout'].total, 10)
        session.close.assert_not_awaited()

    async def test_owned_session_is_closed_even_after_error(self):
        session = Session(Response(status=401))
        with patch('app.adapters.max.client.aiohttp.ClientSession', return_value=session):
            with self.assertRaises(MaxAPIError):
                async with MaxClient(self.settings) as client:
                    await client.get_me()
        session.close.assert_awaited_once()

    async def test_missing_token_does_not_create_a_session(self):
        with patch('app.adapters.max.client.aiohttp.ClientSession') as session:
            with self.assertRaises(ConfigError):
                async with MaxClient(replace(self.settings, token='')):
                    pass
        session.assert_not_called()

    async def test_rate_limit_is_reported_without_automatic_retries(self):
        session = Session(Response(status=429, headers={'Retry-After': '12'}))
        async with MaxClient(self.settings, session=session) as client:
            with self.assertRaises(MaxAPIError) as caught:
                await client.get_me()
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 12)
        self.assertEqual(len(session.calls), 1)

    async def test_errors_do_not_leak_response_secrets(self):
        cases = [Response({'message': 'top-secret'}, status=401),
                 Response({'message': 'top-secret'}, status=503),
                 Response({'success': False, 'message': 'top-secret'}),
                 Response({'code': 'failure', 'message': 'top-secret'}),
                 Response(ValueError('top-secret')), Response(['top-secret']),
                 Response({'user_id': 'top-secret'}), Response(status=302)]
        for response in cases:
            async with MaxClient(self.settings, session=Session(response)) as client:
                with self.assertRaises(MaxAPIError) as caught:
                    await client.get_me()
                self.assertNotIn('top-secret', str(caught.exception))

    async def test_network_errors_are_sanitized_and_cancellation_propagates(self):
        for error in (asyncio.TimeoutError('top-secret'), aiohttp.ClientConnectionError('top-secret')):
            async with MaxClient(self.settings, session=Session(Response(error=error))) as client:
                with self.assertRaises(MaxAPIError) as caught:
                    await client.get_me()
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn('top-secret', str(caught.exception))
        async with MaxClient(self.settings, session=Session(Response(error=asyncio.CancelledError()))) as client:
            with self.assertRaises(asyncio.CancelledError):
                await client.get_me()

    async def test_membership_path_and_chat_id_validation(self):
        session = Session(Response({'is_admin': True}))
        async with MaxClient(self.settings, session=session) as client:
            await client.get_bot_membership(-100)
            for invalid in ('../me', True, 0, 2**63):
                with self.assertRaises(ValueError):
                    await client.get_chat(invalid)
        self.assertEqual(session.calls[0][0][1], 'https://platform-api2.max.ru/chats/-100/members/me')
        self.assertEqual(len(session.calls), 1)

    async def test_probe_reports_missing_admin_rights(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get_me.return_value = {'user_id': 100}
        client.get_bot_membership.return_value = {'is_admin': False}
        with patch('app.main.MaxClient', return_value=client), redirect_stdout(io.StringIO()):
            self.assertEqual(await check_api(replace(self.settings, work_chat_id=-100)), 1)
        client.get_chat.assert_awaited_once_with(-100)


if __name__ == '__main__':
    unittest.main()
