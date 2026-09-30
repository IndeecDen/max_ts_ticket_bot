"""Remember MAX display names without changing persistent role identifiers."""
import json


def remember(conn, user):
    if not isinstance(user, dict) or type(user.get('user_id')) is not int or user.get('is_bot') is not False:
        return
    name = ' '.join(str(user.get(key) or '').strip() for key in ('first_name', 'last_name')).strip()
    name = name or str(user.get('name') or '').strip()
    if name:
        conn.execute("INSERT INTO bot_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f'user_name:{user["user_id"]}', name[:200]))


def display_name(conn, user_id):
    row = conn.execute('SELECT value FROM bot_meta WHERE key=?', (f'user_name:{user_id}',)).fetchone()
    if row:
        return row[0]
    # Existing installations already retain users in durable incoming events.
    rows = conn.execute("""SELECT payload_json FROM inbox_events
        WHERE json_extract(payload_json,'$.callback.user.user_id')=?
           OR json_extract(payload_json,'$.message.sender.user_id')=? ORDER BY id DESC""", (user_id, user_id))
    for (raw,) in rows:
        event = json.loads(raw)
        user = (event.get('callback') or {}).get('user') or (event.get('message') or {}).get('sender')
        remember(conn, user)
        row = conn.execute('SELECT value FROM bot_meta WHERE key=?', (f'user_name:{user_id}',)).fetchone()
        if row:
            return row[0]
    return 'Специалист'
