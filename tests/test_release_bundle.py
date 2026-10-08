import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('build_release', Path(__file__).resolve().parents[1] / 'scripts/build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        for name in builder.ROOT_FILES:
            (self.root / name).write_bytes(b'example\r\n')
        for name, suffixes in builder.TREES.items():
            directory = self.root / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ('sample' + sorted(suffixes)[0])).write_bytes(b'example\r\n')

    def test_private_runtime_files_are_excluded(self):
        for name in ('.env', 'config.json', 'live.db'):
            (self.root / name).write_text('SECRET')
        (self.root / 'app/config.json').write_text('SECRET')
        (self.root / '.venv').mkdir()
        (self.root / '.venv/private.py').write_text('SECRET')
        output = Path(self.temp.name) / 'a.zip'
        builder.build(self.root, output, '1.0.1')
        with zipfile.ZipFile(output) as archive:
            for name in archive.namelist():
                self.assertNotIn(b'SECRET', archive.read(name))
            manifest = json.loads(archive.read('max-ts-ticket-bot-1.0.1/SOURCE_MANIFEST.json'))
            self.assertEqual(manifest['status'], 'release')

    def test_reproducible_and_lf_normalized(self):
        first, second = Path(self.temp.name) / 'a.zip', Path(self.temp.name) / 'b.zip'
        self.assertEqual(builder.build(self.root, first, '1.0.1'), builder.build(self.root, second, '1.0.1'))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertEqual(archive.read('max-ts-ticket-bot-1.0.1/README.md'), b'example\n')

    def test_existing_output_never_overwritten(self):
        output = Path(self.temp.name) / 'existing.zip'
        output.write_bytes(b'original')
        with self.assertRaises(FileExistsError): builder.build(self.root, output, '1.0.1')
        self.assertEqual(output.read_bytes(), b'original')

    def test_version_cannot_escape_archive_directory(self):
        with self.assertRaises(ValueError): builder.build(self.root, Path(self.temp.name) / 'a.zip', '../other')

    def test_brand_assets_are_allowed_in_release(self):
        assets = self.root / 'assets'
        (assets / 'max-ticket-bot.svg').write_text('<svg/>', encoding='utf-8')
        output = Path(self.temp.name) / 'brand.zip'
        builder.build(self.root, output, '1.0.1')
        with zipfile.ZipFile(output) as archive:
            self.assertIn('max-ts-ticket-bot-1.0.1/assets/max-ticket-bot.svg', archive.namelist())
