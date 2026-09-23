"""Durable receiver with an optional explicitly enabled bot runtime."""
import hmac
import json
import logging
import sqlite3

from aiohttp import web

from app.domain.events import IncomingEvent, EventValidationError
from app.storage.inbox import InboxStore
from app.adapters.max.client import MaxClient
from app.services.runtime import BotRuntime

logger = logging.getLogger('max_ticket_bot.webhook')
STORE = web.AppKey('inbox_store', InboxStore)
SECRET = web.AppKey('webhook_secret', str)
RUNTIME = web.AppKey('runtime', BotRuntime)
MAX_BODY_BYTES = 1024 * 1024


def _reject_constant(value):
    raise ValueError('Non-standard JSON constant')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


async def webhook(request):
    supplied = request.headers.getall('X-Max-Bot-Api-Secret', [])
    if len(supplied) != 1 or not supplied[0].isascii() or not hmac.compare_digest(
        supplied[0].encode('utf-8'), request.app[SECRET].encode('utf-8')
    ):
        return web.json_response({'error': 'unauthorized'}, status=401)
    if request.content_type != 'application/json':
        return web.json_response({'error': 'expected_json'}, status=415)
    try:
        payload = json.loads((await request.read()).decode('utf-8'),
                             parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        event = IncomingEvent.parse(payload)
    except (ValueError, UnicodeError, RecursionError, EventValidationError):
        return web.json_response({'error': 'invalid_event'}, status=400)
    try:
        await request.app[STORE].save(event)
    except (sqlite3.Error, OSError):
        # Do not acknowledge unsaved events; do not log message contents or headers.
        logger.error('Не удалось сохранить событие MAX; ответ 503.')
        return web.json_response({'error': 'storage_unavailable'}, status=503)
    return web.json_response({'ok': True})


async def health(request):
    return web.json_response({'status': 'alive'})


async def ready(request):
    try:
        await request.app[STORE].ping()
        if RUNTIME in request.app:
            ok, details = await request.app[RUNTIME].health()
            return web.json_response({'status': 'ready' if ok else 'degraded',
                                      'mode': 'bot', **details}, status=200 if ok else 503)
    except (sqlite3.Error, OSError):
        return web.json_response({'status': 'unavailable'}, status=503)
    return web.json_response({'status': 'ready', 'mode': 'collect_only'})


def create_app(settings, store=None, *, policy=None, client_factory=MaxClient):
    settings.require_webhook_secret()
    if policy is not None:
        settings.require_token()
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[STORE] = store if store is not None else InboxStore(settings.database_path)
    app[SECRET] = settings.webhook_secret
    if policy is not None:
        app[RUNTIME] = BotRuntime(app[STORE], policy)

    async def lifecycle(application):
        await application[STORE].initialize()
        if RUNTIME not in application:
            yield
            return
        async with client_factory(settings) as client:
            runtime = application[RUNTIME]
            await runtime.start(client)
            try:
                yield
            finally:
                # Finish the active HTTP request and SQLite commit before closing
                # the shared session. Forced cancellation retains 'sending'.
                await runtime.stop(settings.timeout_seconds + 15)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_post('/webhook/max', webhook)
    app.router.add_get('/healthz', health)
    app.router.add_get('/readyz', ready)
    return app
