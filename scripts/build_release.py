"""Build a deterministic source release, excluding runtime files and secrets."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
import zipfile
import shutil

ROOT_FILES = ('.env.example', '.gitignore', '.gitattributes', 'README.md',
              'MIGRATION_PLAN.md', 'requirements.in', 'requirements.txt')
TREES = {'app': {'.py'}, 'tests': {'.py'}, 'scripts': {'.py', '.sh'}, 'assets': {'.svg'},
         'docs': {'.md'}, 'deploy': {'.service', '.timer'}, '.github/workflows': {'.yml', '.yaml'}}


def is_link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def sources(root):
    files = [root / name for name in ROOT_FILES]
    for name, suffixes in TREES.items():
        base = root / name
        if not base.is_dir() or any(is_link(p) for p in (base, *base.parents)):
            raise ValueError('Source directory missing or linked')
        pending = [base]
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                if path.name == '__pycache__':
                    continue
                if is_link(path):
                    raise ValueError('Linked source is not allowed')
                if path.is_dir():
                    pending.append(path)
                elif path.suffix in suffixes:
                    files.append(path)
    for path in files:
        if is_link(path) or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Invalid source path')
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def build(root, output, version):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('Expected X.Y.Z version')
    root, output = Path(root).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Output already exists')
    contents = {}
    for path in sources(root):
        name = path.relative_to(root).as_posix()
        # Text files use LF even when the working checkout is on Windows.
        contents[name] = path.read_text(encoding='utf-8').replace('\r\n', '\n').encode('utf-8')
    manifest = {'version': version, 'status': 'release',
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
    contents['SOURCE_MANIFEST.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='max-release-') as directory:
        staging = Path(directory) / 'source.zip'
        with zipfile.ZipFile(staging, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(contents.items()):
                info = zipfile.ZipInfo(f'max-ts-ticket-bot-{version}/{name}', date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        with zipfile.ZipFile(staging) as archive:
            if archive.testzip() is not None:
                raise ValueError('Invalid generated archive')
        digest = hashlib.sha256(staging.read_bytes()).hexdigest()
        with output.open('xb') as target, staging.open('rb') as source:
            shutil.copyfileobj(source, target)
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        digest = build(Path(__file__).resolve().parents[1], args.output, args.version)
    except (OSError, ValueError):
        print('Сборка не завершена. Проверьте исходники и новый путь результата; существующие файлы не перезаписываются.')
        return 1
    print(f'Архив собран: {args.output}\nSHA-256: {digest}\nПроверка сервера и публикация релиза выполняются отдельно.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
