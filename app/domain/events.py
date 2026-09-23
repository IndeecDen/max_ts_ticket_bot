"""Minimal event envelope, preserving fields not yet understood by the bot."""
import hashlib
import json
import re
from dataclasses import dataclass


class EventValidationError(ValueError):
    pass


@dataclass(frozen=True)
class IncomingEvent:
    key: str
    update_type: str
    timestamp_ms: int
    payload_json: str

    @classmethod
    def parse(cls, payload):
        if not isinstance(payload, dict):
            raise EventValidationError('Expected event object')
        kind, timestamp = payload.get('update_type'), payload.get('timestamp')
        if not isinstance(kind, str) or not re.fullmatch(r'[a-z_]{1,64}', kind):
            raise EventValidationError('Invalid update_type')
        if type(timestamp) is not int or not 0 <= timestamp < 2**63:
            raise EventValidationError('Invalid timestamp')
        try:
            canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=False, allow_nan=False)
            canonical.encode('utf-8')
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise EventValidationError('Invalid event JSON') from None
        identity = canonical
        if kind == 'message_created':
            message = payload.get('message')
            if not isinstance(message, dict):
                raise EventValidationError('Missing message')
            body = message.get('body')
            if body is not None:
                if not isinstance(body, dict) or not isinstance(body.get('mid'), str) or not body['mid'].strip():
                    raise EventValidationError('Invalid message ID')
                # MAX mid is a string. Do not coerce it into Telegram's integer ID.
                identity = json.dumps([kind, body['mid']], ensure_ascii=False)
        elif kind == 'message_callback':
            callback = payload.get('callback')
            if not isinstance(callback, dict) or not isinstance(callback.get('callback_id'), str) or not callback['callback_id'].strip():
                raise EventValidationError('Invalid callback ID')
            identity = json.dumps([kind, callback['callback_id']], ensure_ascii=False)
        # Edited messages and lifecycle events use the full envelope: different edits
        # of the same mid must not be collapsed. Identical retries are deduplicated.
        key = kind + ':' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
        return cls(key, kind, timestamp, canonical)
