import unittest
from unittest.mock import AsyncMock, patch
import test_statistics as fixtures
from app.domain.events import IncomingEvent
from app.services.request_view import pages, fragments, DENIED
from app.services.delivery import DeliveryQueue


class RequestViewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.StatisticsTests.asyncSetUp
    asyncTearDown=fixtures.StatisticsTests.asyncTearDown
    command=fixtures.StatisticsTests.command
    rows=fixtures.StatisticsTests.rows

    async def message(self, text='Текст проблемы', mid='source', user=10, chat=-20, attachments=None, link=None):
        message={'sender':{'user_id':user,'is_bot':False},'recipient':{'chat_id':chat},
                 'body':{'mid':mid,'text':text,'attachments':attachments}}
        if link is not None:
            message['link']=link
        await self.store.save(IncomingEvent.parse({'update_type':'message_created','timestamp':1,'message':message}))
        await self.processor.tick(now_ms=0)

    def replies(self):
        return [r for r in self.rows('outbox') if r['destination'].startswith('request_view:')]

    async def test_own_chat_and_work_roles(self):
        await self.message()
        await self.command('/request 1',actor=10,chat=-20)
        self.assertIn('Текст проблемы',self.replies()[-1]['text'])
        for index,(actor,chat) in enumerate([(11,-20),(99,-20),(10,-30)]):
            await self.command('/request 1',actor=actor,chat=chat,mid=f'denied-{index}')
            self.assertEqual(self.replies()[-1]['text'],DENIED)
        for actor in (99,77):
            await self.command('/request 1',actor=actor,mid=f'staff-{actor}')
            self.assertIn('Текст проблемы',self.replies()[-1]['text'])
        await self.command('/request 999',mid='missing')
        self.assertEqual(self.replies()[-1]['text'],DENIED)
        count=len(self.replies())
        await self.command('/request 1',chat=-123,mid='unknownchat')
        self.assertEqual(len(self.replies()),count)

    async def test_long_text_multiple_messages_all_codepoints_preserved(self):
        original='Начало <b>буквально</b>\n' + '😀абв\n' * 1700 + 'КОНЕЦ'
        await self.message(original)
        await self.message('Дополнение',mid='second')
        with self.store.connect() as conn:
            expected=''.join(fragments(conn,1))
            chunks=list(pages(fragments(conn,1)))
        self.assertEqual(''.join(chunks),expected)
        self.assertIn(original,expected)
        self.assertTrue(all(len(p.encode('utf-16-le'))//2<=3000 for p in chunks))
        for index,chunk in enumerate(chunks,1):
            await self.command(f'/request 1 {index}',mid=f'page-{index}')
            reply=self.replies()[-1]['text']
            self.assertIn(chunk,reply)
            self.assertLessEqual(len(reply.encode('utf-16-le'))//2,4000)
        await self.command(f'/request 1 {len(chunks)+1}',mid='outside')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'request_view_rejected')

    async def test_metadata_does_not_expose_attachment_tokens_or_urls(self):
        await self.message('',attachments=[{'type':'file','payload':{'token':'SECRET','url':'https://secret.example'}}],
                           link={'message':{'body':{'text':'Пересланный текст'}}})
        await self.command('/request 1')
        reply=self.replies()[-1]['text']
        self.assertIn('Вложения: file',reply)
        self.assertIn('Пересланный текст',reply)
        self.assertNotIn('SECRET',reply)
        self.assertNotIn('secret.example',reply)

    async def test_deduplication_rollback_and_delivery(self):
        await self.message()
        with patch('app.services.processor.enqueue_cards',side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                await self.command('/request 1')
        self.assertEqual(self.replies(),[])
        await self.processor.tick(now_ms=0)
        await self.command('/request 1')
        self.assertEqual(len(self.replies()),1)
        client=AsyncMock()
        client.send_message.return_value='view-mid'
        self.assertEqual(await DeliveryQueue(self.store).deliver_one(client,lambda:1),'sent')
        self.assertIn('Текст проблемы',client.send_message.call_args.args[1])

    async def test_revocation_and_cross_chat_author_cannot_read_payload(self):
        await self.message()
        with self.store.connect() as conn:
            conn.execute('DELETE FROM bot_roles WHERE user_id=99')
        with patch('app.services.request_view.fragments',side_effect=AssertionError('must not read payload')):
            await self.command('/request 1',actor=99)
            self.assertEqual(self.replies()[-1]['text'],DENIED)
            with self.store.connect() as conn:
                conn.execute('UPDATE requests SET chat_id=-21')
            await self.command('/request 1',actor=10,chat=-20,mid='cross')
            self.assertEqual(self.replies()[-1]['text'],DENIED)

    async def test_validation_and_empty_legacy_request(self):
        for i,args in enumerate(['','0','-1','abc','9223372036854775808','1 0','1 2 extra']):
            await self.command('/request '+args,mid=f'bad-{i}')
            self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'request_view_rejected')
        with self.store.connect() as conn:
            conn.execute("INSERT INTO requests(chat_id,user_id,author_name,status,due_at_ms,created_at_ms) VALUES (-20,10,'name','closed',0,0)")
        await self.command('/request 1',mid='empty')
        self.assertIn('Исходные сообщения для этой заявки не сохранены.',self.replies()[-1]['text'])
