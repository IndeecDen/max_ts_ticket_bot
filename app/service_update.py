"""Update an installer-managed service; never roll a live database back."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request

from app.backup import create_backup
from app.config import PROJECT_ROOT
from app.service_profile import load_profile

BASE = Path('/opt/max-ts-ticket-bot')
PROFILE = Path('/etc/max-ts-ticket-bot/config.json')
UNIT = Path('/etc/systemd/system/max-ts-ticket-bot.service')
SERVICE = 'max-ts-ticket-bot.service'


def render_unit(template, root):
    return template.replace('WorkingDirectory=' + str(BASE), 'WorkingDirectory=' + str(root)).replace(
        'ExecStart=' + str(BASE) + '/.venv/', 'ExecStart=' + str(root) + '/.venv/')


def current_root(unit_text, template):
    matches = re.findall(r'^WorkingDirectory=(.+)$', unit_text, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError('Не удалось определить установленную версию.')
    root = Path(matches[0])
    if root != BASE and not re.fullmatch(re.escape(str(BASE)) + r'/releases/[A-Za-z0-9_-]+', str(root)):
        raise ValueError('Неизвестный каталог установленной версии.')
    if unit_text != render_unit(template, root):
        raise ValueError('Unit изменён вручную; требуется отдельная процедура обновления.')
    return root


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def wait_ready(settings):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(30):
        try:
            with opener.open(f'http://127.0.0.1:{settings.listen_port}/readyz', timeout=2) as response:
                if response.status == 200 and json.load(response).get('mode') == 'bot':
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('Служба не подтвердила готовность.')


def activate(candidate, backup, *, execute=run, readiness=wait_ready, save=create_backup):
    template = (candidate / 'deploy' / SERVICE).read_text(encoding='utf-8')
    old = current_root(UNIT.read_text(encoding='utf-8'), template)
    for path in (old, UNIT, PROFILE, BASE / 'releases'):
        if path.is_symlink():
            raise ValueError('Ссылки в путях установки не поддерживаются.')
    if execute('systemctl', 'show', SERVICE, '--property=FragmentPath', '--value') != str(UNIT):
        raise ValueError('Служба использует другой unit.')
    if execute('systemctl', 'show', SERVICE, '--property=DropInPaths', '--value'):
        raise ValueError('Обнаружены дополнительные настройки systemd.')
    settings, _ = load_profile(PROFILE)
    if settings.database_path != Path('/var/lib/max-ts-ticket-bot/max_bot.db') or settings.listen_host != '127.0.0.1':
        raise ValueError('Нужны стандартные пути базы и локальный адрес установщика.')
    if settings.database_path.is_symlink() or settings.database_path.parent.is_symlink():
        raise ValueError('Ссылки в пути базы не поддерживаются.')
    python = str(candidate / '.venv/bin/python')
    for command in ('check-config', 'check-api'):
        execute('runuser', '-u', 'max-ts-ticket-bot', '--', python, '-m', 'app.main', command, '--profile', str(PROFILE))
    # From this point errors leave the service stopped and preserve all data.
    execute('systemctl', 'stop', SERVICE)
    if execute('systemctl', 'show', SERVICE, '--property=ActiveState', '--value') != 'inactive':
        raise RuntimeError('Остановка службы не подтверждена.')
    save(PROFILE, backup, root=old, unit=UNIT)
    temporary = UNIT.with_suffix('.service.update')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(render_unit(template, candidate))
        temporary.chmod(0o644)
        temporary.replace(UNIT)
        execute('systemctl', 'daemon-reload')
        # Normal application startup migrates SQLite before accepting events.
        execute('systemctl', 'start', SERVICE)
        readiness(settings)
        execute('systemctl', 'is-active', '--quiet', SERVICE)
    except BaseException:
        execute('systemctl', 'stop', SERVICE)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', required=True, type=Path)
    args = parser.parse_args()
    try:
        activate(PROJECT_ROOT, args.backup)
    except Exception:
        print('Обновление не завершено. Проверьте статус службы и journalctl. После остановки службы ошибки оставляют её остановленной; база автоматически не откатывается.')
        print(f'Каталог резервной копии (проверьте наличие и целостность): {args.backup}')
        return 1
    print(f'Обновление завершено; локальная готовность подтверждена. Копия: {args.backup}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
