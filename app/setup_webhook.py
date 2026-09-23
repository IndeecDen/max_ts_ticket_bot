"""Explicit HTTPS setup commands. Normal runtime never registers subscriptions."""
import argparse
import asyncio
import hashlib
import json
import re
import ssl
import sqlite3
from pathlib import Path
import aiohttp
from app.config import ConfigError
from app.service_profile import load_profile
from app.webhook_url import public_url
from app.adapters.max.client import MaxClient, MaxAPIError
from app.storage.inbox import InboxStore, InboxSchemaError
from app.storage.delivery_lock import DeliveryLock, DeliveryBusy

TYPES=['message_created','message_callback']


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


async def probe(settings,url,session=None):
    public_url(url)
    context=ssl.create_default_context()
    if settings.ca_bundle:
        context.load_verify_locations(cafile=str(settings.ca_bundle))
    owned=session is None
    session=session or aiohttp.ClientSession()
    try:
        for headers,expected,body in [({},401,{'error':'unauthorized'}),
                ({'X-Max-Bot-Api-Secret':settings.webhook_secret},400,{'error':'invalid_event'})]:
            async with session.request('POST',url,headers=headers,json={},ssl=context,
                    allow_redirects=False,timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status!=expected or await response.json()!=body:
                    raise ConfigError('HTTPS-маршрут или секрет Webhook не прошли проверку.')
    except (aiohttp.ClientError,asyncio.TimeoutError,ValueError) as exc:
        if isinstance(exc,ConfigError):
            raise
        raise ConfigError('Не удалось проверить Webhook: проверьте DNS, TLS, proxy и секрет.') from None
    finally:
        if owned:
            await session.close()


async def register(settings,url,client,store,probe_fn=probe):
    public_url(url)
    settings.require_webhook_secret()
    await store.initialize()
    # Reserved lock name cannot collide with numeric outbox jobs.
    with DeliveryLock(store.path,'subscription-setup'):
        await probe_fn(settings,url)
        rows=await client.get_subscriptions()
        if any(r['url']!=url for r in rows) or len(rows)>1:
            raise ConfigError('У бота есть другая подписка или дубли. Сначала разберите их явно; изменения не выполнены.')
        fingerprint=digest([settings.api_base_url,settings.token,url,TYPES,settings.webhook_secret])
        with store.connect() as conn:
            saved=conn.execute("SELECT value FROM bot_meta WHERE key='subscription_setup'").fetchone()
        if rows and saved and json.loads(saved[0])=={'config':fingerprint,'remote':digest(rows[0])}:
            return 'unchanged'
        # MAX documents POST on the same URL as subscription update. No blind retry
        # inside this run; an uncertain result is reconciled by the next invocation.
        await client.set_subscription(url,TYPES,settings.webhook_secret)
        confirmed=await client.get_subscriptions()
        types=confirmed[0].get('update_types') if len(confirmed)==1 else None
        if (len(confirmed)!=1 or confirmed[0].get('url')!=url or not isinstance(types,list)
                or not all(isinstance(kind,str) for kind in types) or set(types)!=set(TYPES)):
            raise ConfigError('MAX не подтвердил ожидаемую подписку. Повторите проверку после разбора результата.')
        with store.connect() as conn:
            conn.execute("""INSERT INTO bot_meta VALUES ('subscription_setup',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (json.dumps({'config':fingerprint,'remote':digest(confirmed[0])}),))
        return 'configured'


def nginx_config(url,port,certificate,key):
    parsed=public_url(url)
    if type(port) is not int or not 1<=port<=65535:
        raise ConfigError('Некорректный локальный порт.')
    for path in (certificate,key):
        if not re.fullmatch(r'/[A-Za-z0-9/_.-]+',str(path)):
            raise ConfigError('Для Nginx укажите абсолютные пути PEM без пробелов и специальных символов.')
    return f'''# MAX ticket bot: dedicated HTTPS virtual host
server {{
    listen 443 ssl;
    server_name {parsed.hostname};
    ssl_certificate {certificate};
    ssl_certificate_key {key};
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 1m;
    location = {parsed.path} {{
        limit_except POST {{ deny all; }}
        proxy_pass http://127.0.0.1:{port}/webhook/max;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_connect_timeout 5s;
        proxy_read_timeout 25s;
        access_log off;
    }}
    location / {{ return 404; }}
}}
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('check','register','nginx'))
    parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--certificate')
    parser.add_argument('--key')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        settings,_=load_profile(args.profile)
        profile=json.loads(args.profile.read_text(encoding='utf-8'))
        url=profile.get('public_webhook_url','')
        public_url(url)
        if args.command=='nginx':
            if not args.certificate or not args.key or not args.output:
                raise ConfigError('Нужны --certificate, --key и --output.')
            result=nginx_config(url,settings.listen_port,args.certificate,args.key)
            with args.output.open('x',encoding='utf-8',newline='\n') as stream:
                stream.write(result)
            print('Конфигурация Nginx создана без перезаписи. Проверьте её перед применением.')
            return 0
        async def run():
            if args.command=='check':
                await probe(settings,url)
                print('HTTPS-маршрут и секрет проверены. Подписка не изменялась.')
            else:
                async with MaxClient(settings) as client:
                    state=await register(settings,url,client,InboxStore(settings.database_path))
                print('Подписка уже соответствует настройкам.' if state=='unchanged' else 'Подписка настроена и проверена.')
        asyncio.run(run())
        return 0
    except (ConfigError,MaxAPIError,InboxSchemaError,DeliveryBusy) as exc:
        print(f'Ошибка: {exc}')
        return 1
    except (OSError,ValueError,sqlite3.Error):
        print('Не удалось прочитать настройки или записать результат; проверьте файлы и права.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
