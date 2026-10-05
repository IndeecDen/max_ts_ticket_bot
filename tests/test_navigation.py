import json
import unittest
from unittest.mock import AsyncMock, patch
import test_statistics as fixtures
from app.domain.events import IncomingEvent
from app.services.delivery import DeliveryQueue


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StatisticsTests.asyncSetUp
    asyncTearDown = fixtures.StatisticsTests.asyncTearDown
    command = fixtures.StatisticsTests.command
    rows = fixtures.StatisticsTests.rows

    def add(self, user=10, chat=-20, status='new', specialist=None):
        with self.store.connect() as conn:
            return conn.execute('''INSERT INTO requests(chat_id,user_id,author_name,status,due_at_ms,
                created_at_ms,specialist_id) VALUES (?,?,'name',?,9999999999999,0,?)''',
                (chat,user,status,specialist)).lastrowid

    def replies(self):
        groups = {}
        for row in self.rows('outbox'):
            if not row['destination'].startswith('navigation:'):
                continue
            key = row['destination'].split(':part:')[0]
            if key not in groups:
                groups[key] = dict(row)
            else:
                groups[key]['text'] += '\n\n' + row['text']
        return list(groups.values())

    async def test_work_lists_include_escaped_request_text(self):
        request_id = self.add()
        with self.store.connect() as conn:
            conn.execute('INSERT INTO request_messages VALUES (?,?,?,?)',
                         (999, request_id, 'source', json.dumps({'message': {'body': {'text': '<b>Проверка заявки</b>'}}})))
        for index, command in enumerate(('/open_requests', '/open_unassigned_requests', '/my_active_requests')):
            if index == 2:
                with self.store.connect() as conn:
                    conn.execute("UPDATE requests SET status='in_progress',specialist_id=99 WHERE id=?", (request_id,))
            await self.command(command, actor=99, mid='preview-' + str(index))
            reply = self.replies()[-1]
            self.assertIn('&lt;b&gt;Проверка заявки&lt;/b&gt;', reply['text'])
            self.assertIn('Чат: -20', reply['text'])
            self.assertEqual(reply['text_format'], 'html')

    async def click(self, payload='ui:requests', actor=77, chat=700, mid='menu-mid', callback_id='cb'):
        await self.store.save(IncomingEvent.parse({'update_type':'message_callback','timestamp':1,
            'callback':{'callback_id':callback_id,'payload':payload,'user':{'user_id':actor,'is_bot':False}},
            'message':{'recipient':{'chat_id':chat,'chat_type':'dialog'},'body':{'mid':mid}}}))
        await self.processor.tick(now_ms=0)

    async def menu(self):
        await self.private_command('/menu')
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET message_id='menu-mid',state='sent' WHERE destination LIKE 'navigation:%'")

    async def private_command(self, text, actor=77, mid='private-menu'):
        await self.store.save(IncomingEvent.parse({'update_type':'message_created','timestamp':1,
            'message':{'sender':{'user_id':actor,'is_bot':False},
                'recipient':{'chat_id':700,'chat_type':'dialog'},'body':{'mid':mid,'text':text}}}))
        await self.processor.tick(now_ms=0)

    async def test_menu_only_in_staff_private_dialog(self):
        await self.command('/menu', actor=77)
        await self.command('/menu', actor=77, chat=-20, mid='client-menu')
        await self.private_command('/menu', actor=10, mid='non-staff')
        self.assertEqual(self.replies(), [])
        await self.private_command('/menu', actor=99, mid='specialist')
        self.assertTrue(json.loads(self.replies()[-1]['attachments_json']))
        await self.private_command('/menu')
        self.assertTrue(json.loads(self.replies()[-1]['attachments_json']))
        await self.private_command('/get_timeout', mid='private-setting')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'management_done')

    async def test_client_lists_only_own_requests_in_current_chat(self):
        own = self.add()
        self.add(user=11)
        self.add(chat=-21)
        await self.command('/my_requests', actor=10, chat=-20)
        text = self.replies()[-1]['text']
        self.assertIn('обращения в этом чате: 1.', text)
        self.assertIn(f'#{own} ·', text)
        self.assertNotIn('#2 ·', text)
        self.assertNotIn('#3 ·', text)
        await self.command('/open_requests', actor=10, chat=-20, mid='deny')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'navigation_rejected')

    async def test_work_lists_scopes_and_owner_cannot_be_overridden(self):
        self.add(status='in_progress', specialist=99)
        self.add(user=11,status='in_progress',specialist=98)
        self.add(user=12)
        self.add(user=13,status='closed')
        await self.command('/my_active_requests', actor=99)
        self.assertIn('Назначенные вам заявки: 1.', self.replies()[-1]['text'])
        await self.command('/open_requests', actor=99, mid='all')
        self.assertIn('Открытые заявки: 3.', self.replies()[-1]['text'])
        await self.command('/open_unassigned_requests', actor=77, mid='new')
        self.assertIn('Заявки без исполнителя: 1.', self.replies()[-1]['text'])
        await self.command('/my_active_requests 98', actor=99, mid='not-user')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'navigation_rejected')

    async def test_pagination_all_rows_and_invalid_input(self):
        for i in range(23):
            self.add(user=i+10,status='closed')
        await self.command('/my_requests', actor=10, chat=-20)
        for i in range(23):
            with self.store.connect() as conn:
                conn.execute('UPDATE requests SET user_id=10 WHERE id=?',(i+1,))
        texts=[]
        for page in (1,2,3):
            await self.command(f'/my_requests {page}',actor=10,chat=-20,mid=f'page-{page}')
            texts.append(self.replies()[-1]['text'])
        for i in range(1,24):
            self.assertEqual('\n'.join(texts).count(f'#{i} ·'),1)
        for value in ('0','-1','4','1 extra','abc'):
            await self.command('/my_requests '+value,actor=10,chat=-20,mid='bad'+value)
            self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'navigation_rejected')
        self.assertTrue(all(len(t.encode('utf-16-le'))//2<=4000 for t in texts))

    async def test_menu_buttons_binding_revocation_and_deduplication(self):
        self.add()
        await self.menu()
        before=len(self.replies())
        await self.click(actor=98,callback_id='other')
        await self.click(chat=-20,callback_id='chat')
        await self.click(mid='unknown',callback_id='unknown')
        self.assertEqual(len(self.replies()),before)
        await self.click()
        await self.click()
        self.assertEqual(len(self.replies()),before)
        self.assertEqual(len([r for r in self.rows('outbox') if r['callback_id']=='cb']),1)
        with self.store.connect() as conn:
            conn.execute("DELETE FROM bot_roles WHERE user_id=77")
        await self.click(callback_id='revoked')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'private_menu_only')
        self.assertEqual(len(self.replies()),before)

    async def test_menu_delivery_and_client_help_does_not_create_or_cancel_request(self):
        request_id=self.add(status='waiting')
        await self.command('/start',actor=10,chat=-20)
        reply=self.replies()[-1]
        self.assertIn('/my_requests',reply['text'])
        self.assertNotIn('/role_grant',reply['text'])
        self.assertEqual(self.rows('requests')[0]['status'],'waiting')
        client=AsyncMock()
        client.send_message.return_value='help-mid'
        self.assertEqual(await DeliveryQueue(self.store).deliver_one(client,lambda:1),'sent')
        attachments=client.send_message.call_args.args[2]
        self.assertEqual(attachments, [])
        await self.command('/help',actor=99,chat=-20,mid='staffhelp')
        self.assertEqual(self.rows('requests')[0]['status'],'waiting')
        await self.command('/whoami',actor=77,mid='who')
        self.assertIn('Ваш MAX ID: 77',self.replies()[-1]['text'])
        self.assertIn('admin',self.replies()[-1]['text'])

    async def test_unconfigured_chat_no_reply_and_atomic_rollback(self):
        await self.command('/menu',chat=-12345)
        self.assertEqual(self.replies(),[])
        with patch('app.services.processor.enqueue_cards',side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                await self.command('/help',mid='fail')
        self.assertEqual(self.replies(),[])
        await self.processor.tick(now_ms=0)
        self.assertEqual(len(self.replies()),1)
        await self.command('/help',mid='fail')
        self.assertEqual(len(self.replies()),1)

    async def test_menu_deleted_or_action_not_present_is_rejected(self):
        await self.menu()
        await self.click(payload='ui:team',callback_id='not-present',mid='unknown')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'unknown_navigation_menu')
        await self.click(payload='ui:take',callback_id='absent')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'unknown_navigation_action')
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET deleted_at_ms=1 WHERE message_id='menu-mid'")
        await self.click(callback_id='deleted')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'unknown_navigation_menu')

    async def test_v13_migration_preserves_outbox(self):
        await self.command('/stats')
        before=self.rows('outbox')
        with self.store.connect() as conn:
            conn.execute('ALTER TABLE outbox DROP COLUMN menu_owner')
            conn.execute('PRAGMA user_version=13')
        await self.store.initialize()
        self.assertEqual(self.rows('outbox'),before)
