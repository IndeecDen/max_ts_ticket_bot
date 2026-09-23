"""Persistent service profile. Never execute settings as shell code."""
import json
from pathlib import Path
from app.webhook_url import public_url
from app.config import ConfigError, load_settings, PROJECT_ROOT
from app.services.processor import ProcessingPolicy


def validate(profile):
    try:
        if not isinstance(profile, dict) or type(profile.get('version')) is not int or profile.get('version') != 1:
            raise ValueError
        env, policy = profile['environment'], profile['policy']
        policy = dict(policy)
        policy.setdefault('client_chats', [])
        auto = policy.get('auto_client_chats', not policy['client_chats'])
        if not isinstance(env, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in env.items()):
            raise ValueError
        required = {'MAX_BOT_TOKEN','WORK_CHAT_ID','WEBHOOK_SECRET','DATABASE_PATH','LISTEN_HOST','LISTEN_PORT','TIMEZONE'}
        if not required.issubset(env):
            raise ValueError
        for key in ('client_chats','specialists','admins'):
            if not isinstance(policy[key],list) or not all(type(i) is int for i in policy[key]):
                raise ValueError
        if not policy['admins']:
            raise ConfigError('Профиль установки должен содержать начального администратора.')
        settings = load_settings(PROJECT_ROOT, environ=env, read_dotenv=False)
        settings.require_token()
        settings.require_webhook_secret()
        if profile.get('public_webhook_url'):
            public_url(profile['public_webhook_url'])
        selected = ProcessingPolicy(frozenset(policy['client_chats']),frozenset(policy['specialists']),
                                    settings.work_chat_id,policy['timeout_seconds'],frozenset(policy['admins']),settings.timezone,auto)
        return settings, selected
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,ConfigError):
            raise
        raise ConfigError('Некорректная структура профиля установки.') from None


def load_profile(path):
    try:
        profile = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError,UnicodeError):
        raise ConfigError('Не удалось прочитать JSON-профиль установки.') from None
    return validate(profile)
