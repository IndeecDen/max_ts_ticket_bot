import json
from datetime import datetime, timezone
import unittest
from app.backup_retention import plan, prune, BackupError
import test_backup as fixtures


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.BackupTests.asyncSetUp(self)
        self.backups = self.root / 'backups'
        self.backups.mkdir()
        self.now = datetime(2026, 9, 23, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        await fixtures.BackupTests.asyncTearDown(self)

    def bundle(self, day, name=None):
        self.output = self.backups / (name or f'scheduled_202601{day:02d}T030000Z_1')
        fixtures.BackupTests.backup(self)
        manifest = self.output / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['created_at'] = f'2026-01-{day:02d}T03:00:00+00:00'
        manifest.write_text(json.dumps(data), encoding='utf-8')
        return self.output.resolve()

    def test_preview_keeps_newest_and_changes_nothing(self):
        old, new = self.bundle(1), self.bundle(2)
        selected, skipped = prune(self.backups, keep=1, now=self.now)
        self.assertEqual(selected, [old]); self.assertEqual(skipped, [])
        self.assertTrue(old.exists()); self.assertTrue(new.exists())

    def test_apply_deletes_only_eligible_scheduled_bundle(self):
        old, new = self.bundle(1), self.bundle(2)
        release = self.bundle(3, 'release_keep')
        manual = self.bundle(4, 'manual_keep')
        prune(self.backups, keep=1, now=self.now, apply=True)
        self.assertFalse(old.exists())
        for path in (new, release, manual): self.assertTrue(path.exists())
        self.assertTrue(self.store.path.exists())

    def test_invalid_newest_does_not_count_as_retained_copy(self):
        old, damaged = self.bundle(1), self.bundle(2)
        (damaged / 'database.sqlite3').write_bytes(b'broken')
        selected, skipped = plan(self.backups, keep=1, now=self.now)
        self.assertEqual(selected, []); self.assertEqual(skipped, [damaged])
        self.assertTrue(old.exists())

    def test_age_boundary_preserved(self):
        self.bundle(1); self.bundle(2)
        now = datetime(2026, 1, 31, 3, tzinfo=timezone.utc)
        self.assertEqual(plan(self.backups, keep=1, days=30, now=now)[0], [])

    def test_incomplete_bundle_preserved(self):
        incomplete = self.bundle(1)
        (incomplete / 'INCOMPLETE').touch()
        self.bundle(2); self.bundle(3)
        prune(self.backups, keep=1, now=self.now, apply=True)
        self.assertTrue(incomplete.exists())

    def test_zero_keep_refused(self):
        self.bundle(1)
        with self.assertRaises(BackupError): plan(self.backups, keep=0)
