"""Explicit configuration loading: importing modules never reads secrets."""
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    api_base_url: str
    work_chat_id: int | None
    timeout_seconds: float
    timezone: str
    database_path: Path
    log_level: str
    ca_bundle: Path | None = None
    webhook_secret: str = field(default='', repr=False)
    listen_host: str = '127.0.0.1'
    listen_port: int = 8080

    def require_token(self) -> None:
        if not self.token:
            raise ConfigError('Заполните MAX_BOT_TOKEN в локальном .env.')

    def require_webhook_secret(self) -> None:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{5,256}', self.webhook_secret):
            raise ConfigError('WEBHOOK_SECRET: нужны 5–256 латинских букв, цифр, дефисов или подчёркиваний.')


def load_settings(root: Path = PROJECT_ROOT, environ=None, *, read_dotenv=True) -> Settings:
    root = root.resolve()
    values = dict(dotenv_values(root / '.env', encoding='utf-8', interpolate=False)) if read_dotenv else {}
    values.update(os.environ if environ is None else environ)

    def value(name, default=''):
        return str(values.get(name) or default).strip()

    base_url = value('MAX_API_BASE_URL', 'https://platform-api2.max.ru').rstrip('/')
    try:
        parsed = urlsplit(base_url)
        valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and not parsed.path and parsed.port != 0
                 and not any(c.isspace() or ord(c) < 32 for c in base_url))
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError('MAX_API_BASE_URL должен быть HTTPS-адресом сервера без пути, токена и параметров.')

    token = value('MAX_BOT_TOKEN')
    if any(c.isspace() or ord(c) < 32 for c in token):
        raise ConfigError('MAX_BOT_TOKEN не должен содержать пробелы или управляющие символы.')
    try:
        raw_chat = value('WORK_CHAT_ID')
        chat_id = int(raw_chat) if raw_chat else None
        if chat_id is not None and (chat_id == 0 or not -(2**63) <= chat_id < 2**63):
            raise ValueError
    except ValueError:
        raise ConfigError('WORK_CHAT_ID должен быть ненулевым целым числом int64.') from None
    try:
        timeout = float(value('HTTP_TIMEOUT_SECONDS', '15'))
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError
    except ValueError:
        raise ConfigError('HTTP_TIMEOUT_SECONDS должен быть числом от 0 (не включая) до 120.') from None
    timezone = value('TIMEZONE', 'Europe/Moscow')
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError('TIMEZONE не найден в базе часовых поясов.') from None
    level = value('LOG_LEVEL', 'INFO').upper()
    if level not in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
        raise ConfigError('Некорректный LOG_LEVEL.')

    def local_path(raw):
        path = Path(raw)
        return (path if path.is_absolute() else root / path).resolve()

    database_path = local_path(value('DATABASE_PATH', 'data/max_bot.db'))
    ca_bundle = local_path(value('MAX_CA_BUNDLE')) if value('MAX_CA_BUNDLE') else None
    if ca_bundle is not None and not ca_bundle.is_file():
        raise ConfigError('Файл MAX_CA_BUNDLE не найден.')
    secret = value('WEBHOOK_SECRET')
    if secret and not re.fullmatch(r'[a-zA-Z0-9_-]{5,256}', secret):
        raise ConfigError('Некорректный WEBHOOK_SECRET.')
    try:
        port = int(value('LISTEN_PORT', '8080'))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ConfigError('LISTEN_PORT должен быть числом от 1 до 65535.') from None
    return Settings(token, base_url, chat_id, timeout, timezone, database_path, level, ca_bundle,
                    secret, value('LISTEN_HOST', '127.0.0.1'), port)
