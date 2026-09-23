"""Independent inbox and delivery loops owned by the HTTP application's lifetime."""
import asyncio
import logging

from app.services.processor import InboxProcessor
from app.services.delivery import DeliveryQueue

logger = logging.getLogger('max_ticket_bot.runtime')


class BotRuntime:
    def __init__(self, store, policy, *, interval=1.0):
        self.processor = InboxProcessor(store, policy)
        self.queue = DeliveryQueue(store)
        self.interval = interval
        self.stop_event = asyncio.Event()
        self.tasks = {}
        self.errors = {'inbox': 'starting', 'delivery': 'starting'}
        self.interrupted_sends = 0
        self.delivery_active = False

    async def start(self, client):
        await self.processor.initialize_roles()
        self.interrupted_sends = (await self.queue.status()).get('sending', 0)
        self.tasks = {
            'inbox': asyncio.create_task(self._loop('inbox', self.processor.tick), name='max-inbox'),
            'delivery': asyncio.create_task(self._loop('delivery', lambda: self.queue.deliver_one(client)),
                                            name='max-delivery'),
        }

    async def _loop(self, name, action):
        while not self.stop_event.is_set():
            if name == 'delivery':
                self.delivery_active = True
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Payloads, tokens and raw exception text never enter logs.
                self.errors[name] = type(exc).__name__
                logger.error('Фоновый цикл %s: %s; повтор после задержки.', name, type(exc).__name__)
            else:
                self.errors[name] = None
            finally:
                if name == 'delivery':
                    self.delivery_active = False
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self, grace_seconds):
        self.stop_event.set()
        if not self.tasks:
            return
        _, pending = await asyncio.wait(self.tasks.values(), timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def health(self):
        counts = await self.queue.status()
        loops_ok = bool(self.tasks) and all(not t.done() for t in self.tasks.values())
        ok = (loops_ok and not self.stop_event.is_set() and not any(self.errors.values())
              and not self.interrupted_sends and not counts.get('failed') and not counts.get('uncertain')
              and counts.get('sending', 0) <= len(self.queue.active_jobs))
        return ok, {'workers': dict(self.errors), 'outbox': counts,
                    'interrupted_sends': self.interrupted_sends}
