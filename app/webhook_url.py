"""Validate public URLs before HTTPS probing or generating proxy configuration."""
import re
from urllib.parse import urlsplit
from app.config import ConfigError


def public_url(value):
    try:
        parsed=urlsplit(value)
        host=parsed.hostname
        if (not isinstance(value,str) or parsed.scheme!='https' or not host or parsed.port is not None
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() or ord(c)<32 for c in value)
                or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',host)
                or '..' in host or not re.fullmatch(r'/[A-Za-z0-9/_-]+',parsed.path)):
            raise ValueError
        return parsed
    except (ValueError,TypeError,AttributeError):
        raise ConfigError('Webhook: нужен HTTPS URL без порта, учётных данных и параметров, с простым путём /webhook/max.') from None
