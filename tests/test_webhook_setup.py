import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from dataclasses import replace
from test_foundation import Session, Response
from test_installation import profile
from app.service_profile import validate
from app.config import ConfigError
from app.adapters.max.client import MaxClient,MaxAPIError
from app.setup_webhook import probe,register,nginx_config,TYPES
from app.webhook_url import public_url
from app.storage.inbox import InboxStore
from app.storage.delivery_lock import DeliveryLock,DeliveryBusy


class WebhookSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=InboxStore(Path(self.temp.name)/'max.db')
        self.settings=replace(validate(profile())[0],database_path=self.store.path)
        self.url='https://example.org/webhook/max'
        self.subscription={'url':self.url,'update_types':TYPES,'time':1}

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_probe_tls_headers_no_auth_redirects_and_no_valid_events(self):
        session=Session(Response({'error':'unauthorized'},401),Response({'error':'invalid_event'},400))
        await probe(self.settings,self.url,session)
        for args,kwargs in session.calls:
            self.assertEqual(args,('POST',self.url))
            self.assertEqual(kwargs['json'],{})
            self.assertFalse(kwargs['allow_redirects'])
            self.assertIsNotNone(kwargs['ssl'])
            self.assertNotIn('Authorization',kwargs['headers'])
        self.assertEqual(session.calls[1][1]['headers']['X-Max-Bot-Api-Secret'],self.settings.webhook_secret)
        for response in (Response({},301),Response({'ok':True},200),Response(error=asyncio.TimeoutError('SECRET'))):
            with self.assertRaises(ConfigError) as error:
                await probe(self.settings,self.url,Session(response))
            self.assertNotIn('SECRET',str(error.exception))

    async def test_register_repeat_and_rotation(self):
        client=AsyncMock()
        client.get_subscriptions.side_effect=[[],[self.subscription],[self.subscription],[self.subscription],[self.subscription]]
        verify=AsyncMock()
        self.assertEqual(await register(self.settings,self.url,client,self.store,verify),'configured')
        self.assertEqual(await register(self.settings,self.url,client,self.store,verify),'unchanged')
        self.assertEqual(client.set_subscription.await_count,1)
        self.assertEqual(await register(replace(self.settings,webhook_secret='changed-secret'),self.url,client,self.store,verify),'configured')
        self.assertEqual(client.set_subscription.await_count,2)
        with self.store.connect() as conn:
            value=conn.execute("SELECT value FROM bot_meta WHERE key='subscription_setup'").fetchone()[0]
        self.assertNotIn('changed-secret',value)
        self.assertNotIn(self.settings.token,value)

    async def test_conflict_probe_failure_and_lock_prevent_post(self):
        client=AsyncMock()
        client.get_subscriptions.return_value=[{'url':'https://elsewhere.example/hook'}]
        with self.assertRaises(ConfigError):
            await register(self.settings,self.url,client,self.store,AsyncMock())
        client.set_subscription.assert_not_called()
        with self.assertRaises(ConfigError):
            await register(self.settings,self.url,client,self.store,AsyncMock(side_effect=ConfigError('probe')))
        with DeliveryLock(self.store.path,'subscription-setup'):
            with self.assertRaises(DeliveryBusy):
                await register(self.settings,self.url,client,self.store,AsyncMock())
        client.set_subscription.assert_not_called()

    async def test_uncertain_post_and_wrong_readback_do_not_save_checkpoint(self):
        client=AsyncMock();client.get_subscriptions.return_value=[]
        client.set_subscription.side_effect=MaxAPIError('timeout',uncertain=True)
        with self.assertRaises(MaxAPIError):
            await register(self.settings,self.url,client,self.store,AsyncMock())
        self.assertEqual(client.set_subscription.await_count,1)
        client.set_subscription.side_effect=None
        client.get_subscriptions.side_effect=[[],[{'url':self.url,'update_types':[]}]]
        with self.assertRaises(ConfigError):
            await register(self.settings,self.url,client,self.store,AsyncMock())
        with self.store.connect() as conn:
            self.assertIsNone(conn.execute("SELECT value FROM bot_meta WHERE key='subscription_setup'").fetchone())

    async def test_max_subscription_contract(self):
        session=Session(Response({'subscriptions':[]}),Response({'success':True}))
        async with MaxClient(self.settings,session=session) as client:
            self.assertEqual(await client.get_subscriptions(),[])
            await client.set_subscription(self.url,TYPES,self.settings.webhook_secret)
        self.assertTrue(session.calls[0][0][1].endswith('/subscriptions'))
        self.assertEqual(session.calls[1][1]['json'],{'url':self.url,'update_types':TYPES,'secret':self.settings.webhook_secret})

    async def test_url_validation_and_nginx_generation(self):
        for url in ('http://example.org/hook','https://example.org:443/hook','https://user:SECRET@example.org/hook',
                    'https://example.org/hook?SECRET','https://example.org/a;bad','https://bad host/hook'):
            with self.assertRaises(ConfigError):public_url(url)
        text=nginx_config(self.url,8080,'/etc/ssl/max/fullchain.pem','/etc/ssl/max/privkey.pem')
        self.assertIn('listen 443 ssl;',text)
        self.assertIn('location = /webhook/max',text)
        self.assertIn('proxy_pass http://127.0.0.1:8080/webhook/max;',text)
        self.assertNotIn(self.settings.token,text)
        self.assertNotIn(self.settings.webhook_secret,text)
        with self.assertRaises(ConfigError):nginx_config(self.url,8080,'/etc/x;bad','/etc/key')
