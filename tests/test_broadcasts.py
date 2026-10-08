import time
import json
import unittest
from unittest.mock import AsyncMock

import test_staff_menu as fixtures
from app.domain.events import IncomingEvent
from app.services import broadcasts
from app.services.delivery import DeliveryQueue
from app.services.roles import ManagementError


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StaffMenuTests.asyncSetUp
    asyncTearDown = fixtures.StaffMenuTests.asyncTearDown
    rows = fixtures.StaffMenuTests.rows
    private_command = fixtures.StaffMenuTests.private_command
    click = fixtures.StaffMenuTests.click
    choose = fixtures.StaffMenuTests.choose

    async def test_photo_caption_survives_restart_and_buttons_follow_preview(self):
        await self.lifecycle(-20)
        await self.private_command('/menu')
        await self.choose('broadcasts')
        await self.choose('broadcast_new')
        await self.store.save(IncomingEvent.parse({'update_type': 'message_created', 'timestamp': 30,
            'message': {'sender': {'user_id': 77, 'is_bot': False},
                'recipient': {'chat_id': 700, 'chat_type': 'dialog'},
                'body': {'mid': 'photo-caption', 'text': '<b>Фото</b>', 'attachments': [
                    {'type': 'image', 'payload': {'token': 'photo-token', 'url': 'https://unused', 'photo_id': 1}}]}}}))
        await self.processor.tick(now_ms=0)
        draft = self.rows('broadcasts')[-1]
        expected = [{'type': 'image', 'payload': {'token': 'photo-token'}}]
        self.assertEqual(json.loads(draft['attachments_json']), expected)
        preview = next(r for r in self.rows('outbox') if r['destination'].startswith('broadcast-preview:'))
        menu = next(r for r in reversed(self.rows('outbox')) if r['menu_owner'] == 77)
        self.assertGreater(menu['id'], preview['id'])
        self.assertIsNone(menu['message_id'])
        self.assertEqual(json.loads(preview['attachments_json']), expected)
        await self.choose(f'broadcast_timer:{draft["id"]}')
        await self.private_command('5', mid='photo-time')
        await self.store.initialize()
        await self.processor.tick(now_ms=self.rows('broadcasts')[-1]['due_at_ms'])
        job = next(r for r in self.rows('outbox') if r['destination'].startswith('broadcast:'))
        self.assertEqual(json.loads(job['attachments_json']), expected)
        self.assertEqual(job['text'], '<b>Фото</b>')
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent' WHERE destination NOT LIKE 'broadcast:%'")
        client = type('Client', (), {'send_message': AsyncMock(return_value='photo-sent')})()
        await DeliveryQueue(self.store).deliver_one(client, lambda: int(time.time()*1000)+999999)
        self.assertEqual(client.send_message.call_args.args[2], expected)

    async def test_attachment_only_and_unsupported_attachment_rejected(self):
        await self.private_command('/menu')
        await self.choose('broadcasts')
        await self.choose('broadcast_new')
        for index, attachment in enumerate([
                {'type': 'image', 'payload': {'url': 'https://no-token'}},
                {'type': 'sticker', 'payload': {'token': 'unsupported'}},
                {'type': 'file', 'payload': {'token': 'file-token'}}]):
            await self.store.save(IncomingEvent.parse({'update_type': 'message_created', 'timestamp': 40+index,
                'message': {'sender': {'user_id': 77, 'is_bot': False},
                    'recipient': {'chat_id': 700, 'chat_type': 'dialog'},
                    'body': {'mid': f'attachment-{index}', 'attachments': [attachment]}}}))
            await self.processor.tick(now_ms=0)
            self.assertEqual(len(self.rows('broadcasts')), int(index == 2))
        self.assertEqual(self.rows('broadcasts')[0]['text'], '')
    async def lifecycle(self, chat, kind='bot_added', stamp=10, channel=False):
        await self.store.save(IncomingEvent.parse({'update_type': kind, 'timestamp': stamp,
                                                  'chat_id': chat, 'is_channel': channel}))
        await self.processor.tick(now_ms=0)

    async def draft(self):
        await self.private_command('/menu')
        await self.choose('broadcasts')
        await self.choose('broadcast_new')
        await self.private_command('<b>Работы</b>\n<a href="https://example.org">Подробнее</a>', mid='broadcast-body')
        return self.rows('broadcasts')[-1]['id']

    async def test_schedule_restart_recipients_and_html_delivery(self):
        await self.lifecycle(-20)
        await self.lifecycle(-30)  # work chat
        await self.lifecycle(-40, channel=True)
        bid = await self.draft()
        await self.choose(f'broadcast_timer:{bid}')
        await self.private_command('5', mid='timer')
        due = self.rows('broadcasts')[0]['due_at_ms']
        await self.lifecycle(-21)
        await self.lifecycle(-20, 'bot_removed', stamp=20)
        await self.store.initialize()
        await self.processor.tick(now_ms=due-1)
        self.assertFalse(any(r['destination'].startswith('broadcast:') for r in self.rows('outbox')))
        await self.processor.tick(now_ms=due)
        await self.processor.tick(now_ms=due+1000)
        jobs = [r for r in self.rows('outbox') if r['destination'].startswith('broadcast:')]
        self.assertEqual([r['chat_id'] for r in jobs], [-21])
        self.assertEqual(jobs[0]['text_format'], 'html')
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent' WHERE destination NOT LIKE 'broadcast:%'")
        client = type('Client', (), {'send_message': AsyncMock(return_value='sent-html')})()
        self.assertEqual(await DeliveryQueue(self.store).deliver_one(client, lambda: due+5000), 'sent')
        self.assertEqual(client.send_message.call_args.kwargs, {'format': 'html'})
        self.assertIn('<b>Работы</b>', client.send_message.call_args.args[1])

    async def test_cancel_and_role_revocation(self):
        await self.lifecycle(-20)
        bid = await self.draft()
        await self.choose(f'broadcast_timer:{bid}')
        await self.private_command('60', mid='time')
        await self.choose('broadcast_list')
        await self.choose(f'broadcast_cancel:{bid}')
        await self.processor.tick(now_ms=int(time.time()*1000)+99999999)
        self.assertEqual(self.rows('broadcasts')[0]['state'], 'cancelled')
        self.assertFalse(any(r['destination'].startswith('broadcast:') for r in self.rows('outbox')))

    async def test_immediate_and_revoked_admin(self):
        await self.lifecycle(-20)
        bid = await self.draft()
        await self.choose(f'broadcast_now:{bid}')
        with self.store.connect() as conn:
            conn.execute("DELETE FROM bot_roles WHERE user_id=77 AND role='admin'")
        await self.processor.tick(now_ms=int(time.time()*1000)+1000)
        self.assertEqual(self.rows('broadcasts')[0]['state'], 'cancelled')
        self.assertFalse(any(r['destination'].startswith('broadcast:') for r in self.rows('outbox')))

    async def test_immediate_deduplicates_and_excludes_work_chat(self):
        for chat in (-20, -21, -30):
            await self.lifecycle(chat)
        bid = await self.draft()
        await self.choose(f'broadcast_now:{bid}')
        now = int(time.time()*1000)+1000
        await self.processor.tick(now_ms=now)
        await self.processor.tick(now_ms=now+1000)
        jobs = [r for r in self.rows('outbox') if r['destination'].startswith('broadcast:')]
        self.assertEqual(sorted(r['chat_id'] for r in jobs), [-21, -20])
        await self.choose('broadcast_list')
        menu = next(r for r in reversed(self.rows('outbox')) if r['menu_owner'] == 77)
        self.assertIn('ожидают: 2', menu['text'])

    async def test_cancel_draft_and_timer_input(self):
        await self.lifecycle(-20)
        bid = await self.draft()
        await self.choose(f'broadcast_cancel:{bid}')
        self.assertEqual(self.rows('broadcasts')[0]['state'], 'cancelled')
        await self.choose('broadcast_new')
        await self.private_command('<b>Вторая</b>', mid='second-broadcast')
        bid = self.rows('broadcasts')[-1]['id']
        await self.choose(f'broadcast_timer:{bid}')
        await self.private_command('invalid', mid='invalid-time')
        await self.choose(f'broadcast_cancel:{bid}')
        self.assertEqual(self.rows('menu_sessions'), [])
        self.assertTrue(all(r['state'] == 'cancelled' for r in self.rows('broadcasts')))
        await self.processor.tick(now_ms=int(time.time()*1000)+99999999)
        self.assertFalse(any(r['destination'].startswith('broadcast:') for r in self.rows('outbox')))

    async def test_migration_and_stale_lifecycle(self):
        await self.lifecycle(-20)
        await self.lifecycle(-20, 'bot_removed', stamp=20)
        await self.lifecycle(-20, stamp=15)
        with self.store.connect() as conn:
            conn.execute('DROP TABLE broadcast_chats')
            conn.execute('PRAGMA user_version=19')
        await self.store.initialize()
        self.assertEqual(self.rows('broadcast_chats')[0]['active'], 0)
        await self.lifecycle(-20, stamp=21)
        self.assertEqual(self.rows('broadcast_chats')[0]['active'], 1)

    async def test_invalid_input_and_specialist(self):
        await self.private_command('/menu', actor=99)
        self.assertNotIn('broadcast', self.rows('outbox')[-1]['attachments_json'])
        for text in ('<b>oops', '<script>x</script>', '<a href="javascript:x">x</a>', 'x'*4001):
            with self.assertRaises(ManagementError):
                broadcasts.validate(text)
        with self.assertRaises(ManagementError):
            broadcasts.due_time('2026-10-25 02:30', 'Europe/Berlin', 0)
        with self.assertRaises(ManagementError):
            broadcasts.due_time('2026-03-29 02:30', 'Europe/Berlin', 0)
        self.assertEqual(broadcasts.due_time('5', 'Europe/Moscow', 100), 300100)
