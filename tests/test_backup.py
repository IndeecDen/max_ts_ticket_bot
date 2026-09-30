import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.backup import create_backup,verify_backup,restore_backup,BackupError
from app.storage.inbox import InboxStore
from test_installation import profile


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=InboxStore(self.root/'live.db')
        await self.store.initialize()
        self.profile=self.root/'config.json'
        data=profile();data['environment']['DATABASE_PATH']=str(self.store.path)
        self.profile.write_text(json.dumps(data),encoding='utf-8')
        self.code=self.root/'source';(self.code/'app').mkdir(parents=True)
        (self.code/'app/main.py').write_text('print("test")',encoding='utf-8')
        (self.code/'requirements.txt').write_text('example==1',encoding='utf-8')
        self.output=self.root/'backup'

    async def asyncTearDown(self):
        self.temp.cleanup()

    def backup(self):
        return create_backup(self.profile,self.output,root=self.code)

    async def test_live_wal_snapshot_and_restore_are_independent(self):
        live=sqlite3.connect(self.store.path)
        try:
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('INSERT INTO bot_meta VALUES (?,?)',('before','value'))
            live.commit()
            self.assertTrue(Path(str(self.store.path)+'-wal').exists())
            self.backup()
            live.execute('INSERT INTO bot_meta VALUES (?,?)',('after','new'))
            live.commit()
        finally:
            live.close()
        manifest=verify_backup(self.output)
        from app.storage.inbox import SCHEMA_VERSION
        self.assertEqual(manifest['schema_version'], SCHEMA_VERSION)
        restored=restore_backup(self.output,self.root/'restored')
        conn=sqlite3.connect(restored/'database.sqlite3')
        try:
            self.assertEqual(conn.execute('SELECT value FROM bot_meta WHERE key="before"').fetchone(),('value',))
            self.assertIsNone(conn.execute('SELECT value FROM bot_meta WHERE key="after"').fetchone())
        finally:conn.close()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT value FROM bot_meta WHERE key="after"').fetchone(),('new',))
        self.assertEqual((restored/'config.json').read_bytes(),self.profile.read_bytes())

    async def test_existing_destinations_never_overwritten(self):
        self.backup()
        original=(self.output/'database.sqlite3').read_bytes()
        with self.assertRaises(BackupError):self.backup()
        with self.assertRaises(BackupError):restore_backup(self.output,self.output)
        self.assertEqual((self.output/'database.sqlite3').read_bytes(),original)

    async def test_tamper_and_missing_files_rejected(self):
        self.backup()
        (self.output/'code/app/main.py').write_text('changed',encoding='utf-8')
        with self.assertRaises(BackupError):verify_backup(self.output)
        with self.assertRaises(BackupError):restore_backup(self.output,self.root/'restored')
        self.assertFalse((self.root/'restored').exists())

    async def test_foreign_database_rejected_without_modification(self):
        with self.store.connect() as conn:conn.execute('PRAGMA application_id=123')
        with self.assertRaises(BackupError):self.backup()
        self.assertFalse(self.output.exists())
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA application_id').fetchone()[0],123)

    async def test_source_failure_leaves_no_published_bundle(self):
        with patch('app.backup.private_copy',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.backup()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.max-backup-*')))

    async def test_manifest_traversal_and_unexpected_files_rejected(self):
        self.backup()
        manifest=json.loads((self.output/'manifest.json').read_text())
        manifest['files']['../outside']='hash'
        (self.output/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):verify_backup(self.output)
        del manifest['files']['../outside']
        (self.output/'manifest.json').write_text(json.dumps(manifest))
        (self.output/'extra').write_text('extra')
        with self.assertRaises(BackupError):verify_backup(self.output)

    async def test_restore_failure_marks_incomplete(self):
        self.backup()
        with patch('app.backup.private_copy',side_effect=OSError('disk')):
            with self.assertRaises(OSError):restore_backup(self.output,self.root/'restored')
        self.assertTrue((self.root/'restored/INCOMPLETE').exists())
        with self.assertRaises(BackupError):verify_backup(self.root/'restored')

    async def test_optional_unit_and_ca_are_copied(self):
        ca=self.root/'ca.pem';ca.write_text('certificate')
        unit=self.root/'service.unit';unit.write_text('[Service]\n')
        data=json.loads(self.profile.read_text());data['environment']['MAX_CA_BUNDLE']=str(ca)
        self.profile.write_text(json.dumps(data))
        create_backup(self.profile,self.output,root=self.code,unit=unit)
        manifest=verify_backup(self.output)
        self.assertIn('ca.pem',manifest['files'])
        self.assertIn('service.unit',manifest['files'])
