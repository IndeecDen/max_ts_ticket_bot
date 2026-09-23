"""MAX API adapter; write operations are called by the durable outbox."""
import asyncio
import math
import ssl
from urllib.parse import urlsplit

import aiohttp

from app.config import Settings


class MaxAPIError(Exception):
    """Sanitized exception; raw responses and tokens are never exposed."""
    def __init__(self, message, *, status=None, retryable=False, retry_after=None, uncertain=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.uncertain = uncertain


class MaxClient:
    def __init__(self, settings: Settings, *, session=None):
        self.settings = settings
        self._session = session
        self._owns_session = session is None
        self._ssl = None

    async def __aenter__(self):
        self.settings.require_token()
        self._ssl = ssl.create_default_context()
        if self.settings.ca_bundle:
            self._ssl.load_verify_locations(cafile=str(self.settings.ca_bundle))
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @staticmethod
    def _retry_after(value):
        try:
            seconds = float(value)
            return seconds if math.isfinite(seconds) and seconds >= 0 else None
        except (TypeError, ValueError):
            return None

    async def _get(self, path):
        return await self._request('GET', path)

    async def _request(self, method, path, **kwargs):
        if self._session is None or self._ssl is None:
            raise RuntimeError('Используйте MaxClient через async with.')
        try:
            async with self._session.request(
                method, self.settings.api_base_url + path,
                headers={'Authorization': self.settings.token, 'Accept': 'application/json'},
                timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
                ssl=self._ssl, allow_redirects=False, **kwargs,
            ) as response:
                status = response.status
                if status == 429:
                    raise MaxAPIError('MAX ограничил частоту запросов.', status=status, retryable=True,
                                      retry_after=self._retry_after(response.headers.get('Retry-After')))
                if status in (401, 403):
                    raise MaxAPIError('MAX отклонил токен или доступ к ресурсу.', status=status)
                if method == 'POST' and path == '/messages' and status not in (401, 403, 429):
                    try:
                        error_body = await response.json()
                    except (ValueError, aiohttp.ContentTypeError):
                        error_body = None
                    if isinstance(error_body, dict) and error_body.get('code') == 'attachment.not.ready':
                        raise MaxAPIError('MAX ещё обрабатывает вложение.', status=status,
                                          retryable=True, retry_after=5)
                if not 200 <= status < 300:
                    raise MaxAPIError(f'Ошибка HTTP {status} от MAX.', status=status, retryable=status >= 500,
                                      uncertain=method == 'POST' and status >= 500)
                try:
                    payload = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise MaxAPIError('MAX вернул некорректный JSON.', status=status, uncertain=method == 'POST') from None
                if not isinstance(payload, dict):
                    raise MaxAPIError('MAX вернул неожиданный формат ответа.', status=status, uncertain=method == 'POST')
                if payload.get('success') is False or payload.get('code'):
                    raise MaxAPIError('MAX сообщил об ошибке в теле ответа.', status=status)
                return payload
        except (asyncio.TimeoutError, aiohttp.ClientConnectionError):
            raise MaxAPIError('Не удалось связаться с MAX: таймаут, сеть или TLS.', retryable=True,
                              uncertain=method == 'POST') from None
        except aiohttp.ClientError:
            raise MaxAPIError('Не удалось прочитать ответ MAX.', retryable=True, uncertain=method == 'POST') from None

    async def upload_file(self, content, filename):
        if not isinstance(content, bytes) or not 0 < len(content) <= 20 * 1024 * 1024:
            raise MaxAPIError('Размер отчёта должен быть от 1 байта до 20 МиБ.')
        try:
            result = await self._request('POST', '/uploads', params={'type': 'file'})
            url = result.get('url')
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid = (parsed and parsed.scheme == 'https' and parsed.hostname
                         and not parsed.username and not parsed.password and not parsed.fragment
                         and parsed.port in (None, 443))
            except ValueError:
                valid = False
            if not valid:
                raise MaxAPIError('MAX вернул некорректный адрес загрузки.')
            form = aiohttp.FormData()
            form.add_field('data', content, filename=filename,
                           content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            # Signed upload URL: never forward the bot Authorization header.
            async with self._session.request('POST', url, data=form,
                    timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
                    ssl=self._ssl, allow_redirects=False) as response:
                if not 200 <= response.status < 300:
                    raise MaxAPIError('MAX отклонил загрузку файла.', status=response.status,
                                      retryable=response.status == 429 or response.status >= 500,
                                      retry_after=self._retry_after(response.headers.get('Retry-After')))
                try:
                    payload = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise MaxAPIError('Некорректный ответ загрузки файла.', retryable=True) from None
                token = payload.get('token') if isinstance(payload, dict) else None
                if not isinstance(token, str) or not token.strip():
                    raise MaxAPIError('MAX не вернул токен файла.', retryable=True)
                return token
        except MaxAPIError as exc:
            # Uploads never publish a chat message; retrying cannot duplicate one.
            raise MaxAPIError(str(exc), status=exc.status, retryable=exc.retryable,
                              retry_after=exc.retry_after) from None
        except (asyncio.TimeoutError, aiohttp.ClientError):
            raise MaxAPIError('Не удалось загрузить файл в MAX.', retryable=True) from None

    async def send_message(self, chat_id, text, attachments=None, *, forward_mid=None):
        chat = self._chat_id(chat_id)
        if not isinstance(text, str) or not text or len(text.encode('utf-16-le')) // 2 > 4000:
            raise ValueError('Текст карточки должен содержать от 1 до 4000 символов UTF-16.')
        if forward_mid is not None and (not isinstance(forward_mid, str) or not forward_mid.strip()):
            raise ValueError('Некорректный ID исходного сообщения.')
        payload = await self._request('POST', '/messages',
                                      params={'chat_id': chat, 'disable_link_preview': 'true'},
                                      json={'text': text, **({'attachments': attachments} if attachments is not None else {}),
                                            **({'link': {'type': 'forward', 'mid': forward_mid}} if forward_mid is not None else {})})
        message = payload.get('message')
        body = message.get('body') if isinstance(message, dict) else None
        mid = body.get('mid') if isinstance(body, dict) else None
        if not isinstance(mid, str) or not mid.strip():
            raise MaxAPIError('MAX не вернул ID отправленного сообщения.', uncertain=True)
        return mid

    async def edit_message(self, mid, text, attachments):
        if not isinstance(mid, str) or not mid.strip():
            raise ValueError('Некорректный ID сообщения.')
        result = await self._request('PUT', '/messages', params={'message_id': mid},
                                     json={'text': text, 'attachments': attachments})
        if result.get('success') is not True:
            raise MaxAPIError('MAX не подтвердил редактирование.', uncertain=True)
        return mid

    async def delete_message(self, mid):
        if not isinstance(mid, str) or not mid.strip():
            raise ValueError('Некорректный ID удаляемого сообщения.')
        result = await self._request('DELETE', '/messages', params={'message_id': mid})
        if result.get('success') is not True:
            raise MaxAPIError('MAX не подтвердил удаление сообщения.')

    async def answer_callback(self, callback_id):
        result = await self._request('POST', '/answers', params={'callback_id': callback_id}, json={})
        if result.get('success') is not True:
            raise MaxAPIError('MAX не подтвердил ответ на кнопку.', uncertain=True)

    async def get_subscriptions(self):
        payload = await self._get('/subscriptions')
        rows = payload.get('subscriptions')
        if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get('url'), str) for r in rows):
            raise MaxAPIError('MAX вернул некорректный список подписок.')
        return rows

    async def set_subscription(self, url, update_types, secret):
        result = await self._request('POST', '/subscriptions',
                                     json={'url': url, 'update_types': update_types, 'secret': secret})
        if result.get('success') is not True:
            raise MaxAPIError('MAX не подтвердил настройку подписки.', uncertain=True)

    async def get_me(self):
        payload = await self._get('/me')
        if type(payload.get('user_id')) is not int:
            raise MaxAPIError('В ответе MAX отсутствует корректный user_id бота.')
        return payload

    @staticmethod
    def _chat_id(chat_id):
        if type(chat_id) is not int or chat_id == 0 or not -(2**63) <= chat_id < 2**63:
            raise ValueError('Некорректный ID чата.')
        return str(chat_id)

    async def get_chat(self, chat_id):
        return await self._get('/chats/' + self._chat_id(chat_id))

    async def get_bot_membership(self, chat_id):
        return await self._get('/chats/' + self._chat_id(chat_id) + '/members/me')
