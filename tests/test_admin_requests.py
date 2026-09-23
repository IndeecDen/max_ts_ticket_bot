import asyncio
import json
import unittest
from unittest.mock import patch
import test_statistics as fixtures
from app.services.processor import InboxProcessor


class AdminRequestTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.StatisticsTests.asyncSetUp
    asyncTearDown=fixtures.StatisticsTests.asyncTearDown
    command=fixtures.StatisticsTests.command
    rows=fixtures.StatisticsTests.rows

    def add(self,status='new',user=10,specialist=None):
        with self.store.connect() as conn:
            return conn.execute('''INSERT INTO requests(chat_id,user_id,author_name,status,due_at_ms,
                created_at_ms,specialist_id,started_at_ms) VALUES (-20,?,'name',?,9999999999999,0,?,?)''',
                (user,status,specialist,12345 if status=='in_progress' else None)).lastrowid

    async def test_assign_new_reassign_preserves_start_and_duplicate_is_noop(self):
        self.add()
        await self.command('/assign_request 1 99')
        first=self.rows('requests')[0]
        self.assertEqual(first['status'],'in_progress')
        self.assertEqual(first['specialist_id'],99)
        self.assertEqual(first['revision'],2)
        self.assertIsNotNone(first['started_at_ms'])
        await self.command('/assign_request 1 99')
        await self.command('/assign_request 1 99',mid='same')
        self.assertEqual(self.rows('requests')[0],first)
        await self.command('/assign_request 1 98',mid='reassign')
        changed=self.rows('requests')[0]
        self.assertEqual(changed['specialist_id'],98)
        self.assertEqual(changed['started_at_ms'],first['started_at_ms'])
        self.assertEqual(changed['revision'],3)
        cards=[r for r in self.rows('outbox') if r['request_id']==1]
        self.assertEqual(len(cards),2)
        self.assertTrue(all(r['revision']==3 for r in cards))

    async def test_permissions_invalid_role_and_terminal_state(self):
        self.add()
        original=self.rows('requests')
        for i,(command,actor,chat) in enumerate([('/assign_request 1 99',99,-30),
                                                ('/close_all',10,-30),('/close_all',77,-20),
                                                ('/cancel_request 1 reason',99,-30)]):
            await self.command(command,actor=actor,chat=chat,mid=f'denied-{i}')
        self.assertEqual(self.rows('requests'),original)
        for i,command in enumerate(['/assign_request 1 77','/assign_request 1 0','/assign_request 1 9223372036854775808',
                                    '/assign_request 999 99','/cancel_request 1','/close_all extra']):
            await self.command(command,mid=f'invalid-{i}')
            self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'management_rejected')
        self.assertEqual(self.rows('requests'),original)
        with self.store.connect() as conn:
            conn.execute("UPDATE requests SET status='closed'")
        await self.command('/assign_request 1 99',mid='terminal')
        self.assertEqual(self.rows('requests')[0]['status'],'closed')

    async def test_cancel_waiting_new_and_inprogress_with_audit(self):
        for i,status in enumerate(('waiting','new','in_progress'),1):
            self.add(status,user=i,specialist=99 if status=='in_progress' else None)
        await self.processor.tick(now_ms=0)
        for i in (1,2,3):
            await self.command(f'/cancel_request {i} Ошибочное обращение',mid=f'cancel-{i}')
        self.assertTrue(all(r['status']=='cancelled' and r['ended_at_ms'] is not None for r in self.rows('requests')))
        logs=[r for r in self.rows('management_log') if r['action']=='admin_cancel']
        self.assertEqual(len(logs),3)
        self.assertTrue(all(json.loads(r['details'])['reason']=='Ошибочное обращение' for r in logs))
        self.assertFalse(any(r['request_id']==1 for r in self.rows('outbox'))) # no waiting card existed
        for r in self.rows('outbox'):
            if r['request_id'] in (2,3):
                self.assertEqual(json.loads(r['attachments_json']),[])
        await self.command('/cancel_request 3 Again',mid='again')
        self.assertEqual(self.rows('inbox_events')[-1]['outcome'],'management_rejected')

    async def test_close_all_only_published_open_states_and_concurrency(self):
        for i,status in enumerate(('waiting','new','in_progress','closed','cancelled'),1):
            self.add(status,user=i,specialist=99 if status=='in_progress' else None)
        await self.command('/close_all')
        rows=self.rows('requests')
        self.assertEqual([r['status'] for r in rows],['waiting','closed','closed','closed','cancelled'])
        self.assertEqual([r['revision'] for r in rows],[1,2,2,1,1])
        await asyncio.gather(self.processor.tick(now_ms=0),InboxProcessor(self.store,self.processor.policy).tick(now_ms=0))
        await self.command('/close_all',mid='repeat')
        self.assertEqual(self.rows('requests'),rows)
        logs=[r for r in self.rows('management_log') if r['action']=='admin_close_all']
        self.assertEqual(len(logs),2)
        self.assertEqual({r['target_id'] for r in logs},{2,3})
        self.assertTrue(all(r['ended_at_ms'] is not None for r in rows[1:3]))

    async def test_failure_rolls_back_status_audit_and_cards(self):
        self.add()
        with patch('app.services.processor.enqueue_cards',side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                await self.command('/assign_request 1 99')
        self.assertEqual(self.rows('requests')[0]['status'],'new')
        self.assertEqual(self.rows('outbox'),[])
        self.assertFalse(any(r['action']=='assign_request' for r in self.rows('management_log')))
        await self.processor.tick(now_ms=0)
        self.assertEqual(self.rows('requests')[0]['status'],'in_progress')
        self.assertEqual(len([r for r in self.rows('management_log') if r['action']=='assign_request']),1)

    async def test_updates_keep_mid_and_problem_delivery_state(self):
        self.add()
        await self.processor.tick(now_ms=0)
        with self.store.connect() as conn:
            conn.execute("UPDATE outbox SET state='sent',message_id='client-mid' WHERE destination='client'")
            conn.execute("UPDATE outbox SET state='uncertain' WHERE destination='work'")
        await self.command('/cancel_request 1 Duplicate')
        cards={r['destination']:r for r in self.rows('outbox') if r['request_id']==1}
        self.assertEqual(cards['client']['message_id'],'client-mid')
        self.assertEqual(cards['client']['state'],'pending')
        self.assertEqual(cards['work']['state'],'uncertain')
        self.assertTrue(all(r['revision']==2 for r in cards.values()))
