"""Verified backup bundles; restore only into a new directory, never a live database."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

from app.config import ConfigError, PROJECT_ROOT
from app.service_profile import validate
from app.storage.inbox import APPLICATION_ID, SCHEMA_VERSION


class BackupError(ValueError):
    pass


def sha256(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            result.update(block)
    return result.hexdigest()


def check_database(path):
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        app_id=conn.execute('PRAGMA application_id').fetchone()[0]
        version=conn.execute('PRAGMA user_version').fetchone()[0]
        if app_id!=APPLICATION_ID or not 1<=version<=SCHEMA_VERSION:
            raise BackupError('Это не поддерживаемая база MAX.')
        if conn.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
            raise BackupError('Проверка целостности базы не пройдена.')
        return version
    finally:
        conn.close()


def private_copy(source,target):
    if source.is_symlink() or not source.is_file():
        raise BackupError('Для копии нужны обычные файлы без символических ссылок.')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with source.open('rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src,dst)
    target.chmod(0o600)


def snapshot(source,target):
    check_database(source)
    deadline=time.monotonic()+60
    def progress(status,remaining,total):
        if time.monotonic()>deadline:
            raise BackupError('База занята слишком долго; повторите резервное копирование.')
    src=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    dst=sqlite3.connect(target)
    try:
        src.backup(dst,pages=256,progress=progress,sleep=0.05)
        dst.execute('PRAGMA journal_mode=DELETE')
    finally:
        dst.close();src.close()
    target.chmod(0o600)
    return check_database(target)


def create_backup(profile_path, output, *, root=PROJECT_ROOT, unit=None):
    output=Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise BackupError('Каталог результата уже существует; перезапись запрещена.')
    raw=Path(profile_path).read_bytes()
    settings,_=validate(json.loads(raw.decode('utf-8')))
    if not settings.database_path.is_file():
        raise BackupError('База ещё не существует.')
    output.parent.mkdir(parents=True,exist_ok=True)
    # Work on the same filesystem; only publish a fully verified bundle.
    with tempfile.TemporaryDirectory(prefix='.max-backup-',dir=output.parent) as temp:
        staging=Path(temp)/'bundle';staging.mkdir(mode=0o700)
        version=snapshot(settings.database_path,staging/'database.sqlite3')
        (staging/'config.json').write_bytes(raw);(staging/'config.json').chmod(0o600)
        root=Path(root)
        for source in sorted((root/'app').rglob('*.py')):
            private_copy(source,staging/'code'/source.relative_to(root))
        if not (staging/'code/app/main.py').exists():
            raise BackupError('Не найден код приложения для согласованной копии.')
        private_copy(root/'requirements.txt',staging/'code/requirements.txt')
        if settings.ca_bundle:
            private_copy(settings.ca_bundle,staging/'ca.pem')
        if unit is not None:
            private_copy(Path(unit),staging/'service.unit')
        files={str(p.relative_to(staging).as_posix()):sha256(p) for p in staging.rglob('*') if p.is_file()}
        manifest={'format':1,'created_at':datetime.now(timezone.utc).isoformat(),
                  'schema_version':version,'files':files}
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        (staging/'manifest.json').chmod(0o600)
        verify_backup(staging)
        # mkdir is the exclusive reservation; don't replace an existing destination.
        output.mkdir(mode=0o700)
        try:
            for child in staging.iterdir():
                child.rename(output/child.name)
        except BaseException:
            # A failed publish remains clearly incomplete; never remove user's files.
            marker=output/'INCOMPLETE'
            marker.touch(exist_ok=True)
            raise
    return output


def verify_backup(bundle):
    bundle=Path(bundle)
    if bundle.is_symlink() or (bundle/'INCOMPLETE').exists():
        raise BackupError('Комплект неполный или является ссылкой.')
    if (bundle/'manifest.json').is_symlink():
        raise BackupError('Некорректный manifest.')
    manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest,dict) or manifest.get('format')!=1 or not isinstance(manifest.get('files'),dict):
        raise BackupError('Неподдерживаемый формат резервной копии.')
    files=manifest['files']
    if not {'database.sqlite3','config.json','code/app/main.py','code/requirements.txt'}.issubset(files):
        raise BackupError('В комплекте не хватает обязательных файлов.')
    for name,expected in files.items():
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or not path.parts:
            raise BackupError('Недопустимый путь в manifest.')
        target=bundle.joinpath(*path.parts)
        if any(p.is_symlink() for p in [target,*target.parents] if p!=bundle.parent):
            raise BackupError('Ссылки в резервной копии запрещены.')
        if not target.is_file() or sha256(target)!=expected:
            raise BackupError('Контрольная сумма резервной копии не совпадает.')
    actual={p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file() and p.name!='manifest.json'}
    if actual!=set(files):
        raise BackupError('Обнаружены лишние или отсутствующие файлы.')
    version=check_database(bundle/'database.sqlite3')
    if version!=manifest.get('schema_version'):
        raise BackupError('Версия базы не совпадает с manifest.')
    return manifest


def restore_backup(bundle,output):
    bundle=Path(bundle)
    manifest=verify_backup(bundle)
    output=Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise BackupError('Восстановление допускается только в новый каталог.')
    output.mkdir(parents=True,mode=0o700)
    (output/'INCOMPLETE').touch()
    for name in manifest['files']:
        private_copy(bundle/name,output/name)
    private_copy(bundle/'manifest.json',output/'manifest.json')
    (output/'INCOMPLETE').unlink()
    try:
        verify_backup(output)
    except BaseException:
        (output/'INCOMPLETE').touch()
        raise
    return output


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('create','verify','restore'))
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--unit',type=Path)
    args=parser.parse_args()
    try:
        if args.command=='create':
            if not args.profile or not args.output:raise BackupError('Нужны --profile и --output.')
            create_backup(args.profile,args.output,unit=args.unit)
        elif args.command=='verify':
            if not args.bundle:raise BackupError('Нужен --bundle.')
            verify_backup(args.bundle)
        else:
            if not args.bundle or not args.output:raise BackupError('Нужны --bundle и --output.')
            restore_backup(args.bundle,args.output)
        print('Операция завершена. Проверка целостности и контрольных сумм пройдена; секреты не выводятся.')
        return 0
    except (ConfigError,BackupError,OSError,ValueError,sqlite3.Error):
        print('Операция не завершена. Проверьте пути, права, формат и целостность копии. Работающая база не заменялась.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
