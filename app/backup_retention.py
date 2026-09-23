"""Opt-in retention for verified scheduled backups; preview by default."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3

from app.backup import BackupError, verify_backup

SCHEDULED = re.compile(r'scheduled_\d{8}T\d{6}Z_\d+')


def checked_root(root):
    root = Path(root).absolute()
    if not root.is_dir() or any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (root, *root.parents)):
        raise BackupError('Нужен обычный каталог копий без ссылок.')
    return root.resolve()


def inspect(bundle):
    if bundle.is_symlink() or (hasattr(bundle, 'is_junction') and bundle.is_junction()):
        raise BackupError('Ссылка вместо комплекта.')
    # Also reject unlisted links and files before deleting anything.
    for item in bundle.rglob('*'):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise BackupError('Ссылки в комплекте запрещены.')
    manifest = verify_backup(bundle)
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file()}
    if actual != set(manifest['files']) | {'manifest.json'}:
        raise BackupError('В комплекте есть неизвестные файлы.')
    created = datetime.fromisoformat(manifest['created_at'])
    if created.tzinfo is None:
        raise BackupError('В дате копии отсутствует часовой пояс.')
    return created.astimezone(timezone.utc), manifest


def plan(root, *, keep=7, days=30, now=None):
    if type(keep) is not int or keep < 1 or type(days) is not int or days < 1:
        raise BackupError('keep и days должны быть положительными целыми числами.')
    root = checked_root(root)
    now = now or datetime.now(timezone.utc)
    valid, skipped = [], []
    for bundle in sorted(root.iterdir()):
        if not SCHEDULED.fullmatch(bundle.name):
            continue
        try:
            if bundle.is_symlink() or bundle.resolve().parent != root or not bundle.is_dir():
                raise BackupError('Неподдерживаемый путь.')
            created, _ = inspect(bundle)
            if created > now:
                raise BackupError('Дата копии находится в будущем.')
            valid.append((created, bundle))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            skipped.append(bundle)
    valid.sort(key=lambda row: (row[0], row[1].name), reverse=True)
    cutoff = now - timedelta(days=days)
    return [p for created, p in valid[keep:] if created < cutoff], skipped


def prune(root, *, keep=7, days=30, now=None, apply=False):
    root = checked_root(root)
    candidates, skipped = plan(root, keep=keep, days=days, now=now)
    if not apply:
        return candidates, skipped
    for bundle in candidates:
        # Re-check containment and the entire bundle immediately before deletion.
        if bundle.is_symlink() or bundle.resolve().parent != root:
            raise BackupError('Путь копии изменился.')
        _, manifest = inspect(bundle)
        files = [bundle / name for name in manifest['files']] + [bundle / 'manifest.json']
        for file in files:
            if not file.resolve().is_relative_to(bundle.resolve()):
                raise BackupError('Файл находится вне комплекта.')
        # No recursive deletion: only verified files, then empty directories.
        # Interruption leaves an invalid bundle which later runs preserve.
        for file in files:
            file.unlink()
        for directory in sorted((p for p in bundle.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            directory.rmdir()
        bundle.rmdir()
    return candidates, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('/var/backups/max-ts-ticket-bot'))
    parser.add_argument('--keep', type=int, default=7)
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        # Same lock as the service updater and scheduled backup. CLI is Linux-only.
        import fcntl
        with open('/run/lock/max-ts-ticket-bot-update.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            candidates, skipped = prune(args.directory, keep=args.keep, days=args.days, apply=args.apply)
        print('Удалено:' if args.apply else 'К удалению (предпросмотр):')
        for path in candidates:
            print(path.name)
        print(f'Количество: {len(candidates)}. Непроверенные комплекты сохранены: {len(skipped)}.')
        return 0
    except (ImportError, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print('Очистка не завершена. Нужны Linux, права на каталог и свободная блокировка. Проверьте целостность копий; повторный запуск начните с предпросмотра.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
