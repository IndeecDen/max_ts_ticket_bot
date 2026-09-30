from dataclasses import replace
import unittest
from app.domain.events import IncomingEvent
from app.services.processor import InboxProcessor
from app.service_profile import validate
from app.config import ConfigError
import test_processor as fixtures
from test_installation import profile


class AutoChatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ProcessorTests.asyncSetUp(self)
        self.policy = replace(self.policy, client_chats=frozenset(), auto_client_chats=True)
        self.processor = InboxProcessor(self.store, self.policy)
        self.serial = 0

    async def asyncTearDown(self):
        await fixtures.ProcessorTests.asyncTearDown(self)

    def rows(self, table):
        return fixtures.ProcessorTests.rows(self, table)

    async def message(self, chat, text='Помогите', user=10, kind='chat'):
        self.serial += 1
        recipient = {'chat_id': chat}
        if kind is not None: recipient['chat_type'] = kind
        await self.store.save(IncomingEvent.parse({'update_type':'message_created','timestamp':1,
            'message':{'sender':{'user_id':user,'is_bot':False}, 'recipient':recipient,
                       'body':{'mid':f'auto-{self.serial}','text':text}}}))
        await self.processor.tick(now_ms=0)

    async def test_new_groups_accepted_without_restart_and_work_excluded(self):
        await self.message(-111)
        await self.message(222)
        await self.message(-30)
        self.assertEqual({row['chat_id'] for row in self.rows('requests')}, {-111,222})
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'], 'ignored_chat')

    async def test_dialog_channel_unknown_type_ignored_including_menu(self):
        for kind in ('dialog','channel',None,'other'):
            await self.message(-111,kind=kind)
            await self.message(-111,text='/menu',kind=kind)
        self.assertEqual(self.rows('requests'), [])
        self.assertEqual(self.rows('outbox'), [])

    async def test_menu_request_view_and_ownership_in_new_chat(self):
        await self.message(-111)
        await self.message(-111,text='/menu')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'private_menu_only')
        await self.message(-111,text='/request 1')
        self.assertIn('Помогите', self.rows('outbox')[-1]['text'])
        await self.message(-222,text='/request 1')
        self.assertIn('недоступна', self.rows('outbox')[-1]['text'])
        await self.message(-111,text='/request 1',user=11)
        self.assertIn('недоступна', self.rows('outbox')[-1]['text'])

    async def test_specialist_response_cancels_wait_in_new_chat(self):
        await self.message(-111)
        await self.message(-111,user=99)
        self.assertEqual(self.rows('requests')[0]['status'],'cancelled')

    async def test_cancel_button_in_auto_chat_still_requires_author(self):
        await self.message(-111)
        await self.processor.tick(now_ms=2**62)
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET message_id='client-card',state='sent' WHERE destination='client'")
        for user in (11,10):
            await self.store.save(IncomingEvent.parse({'update_type':'message_callback','timestamp':2,
                'callback':{'callback_id':f'cancel-{user}','payload':'cancel:1:1','user':{'user_id':user,'is_bot':False}},
                'message':{'recipient':{'chat_id':-111,'chat_type':'chat'},'body':{'mid':'client-card'}}}))
            await self.processor.tick(now_ms=2**62)
            self.assertEqual(self.rows('requests')[0]['status'], 'new' if user==11 else 'cancelled')

    async def test_legacy_profile_and_new_profile_modes(self):
        old = profile()
        self.assertFalse(validate(old)[1].auto_client_chats)
        old['policy'].pop('client_chats')
        automatic = validate(old)[1]
        self.assertTrue(automatic.auto_client_chats)
        self.assertTrue(automatic.is_client(-111))
        self.assertFalse(automatic.is_client(-30))
        old['policy']['auto_client_chats'] = 'false'
        with self.assertRaises(ConfigError): validate(old)
