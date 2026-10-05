from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app import service_update as update


class ServiceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.release = self.base / 'releases' / 'release_test'
        (self.release / 'deploy').mkdir(parents=True)
        self.template = f'# Managed\nWorkingDirectory={self.base}\nExecStart={self.base}/.venv/bin/python -m app.main run\n'
        (self.release / 'deploy' / update.SERVICE).write_text(self.template, encoding='utf-8')
        self.unit = self.base / update.SERVICE
        self.unit.write_text(self.template, encoding='utf-8')
        self.profile = self.base / 'config.json'
        self.settings = SimpleNamespace(database_path=Path('/var/lib/max-ts-ticket-bot/max_bot.db'), listen_host='127.0.0.1')
        for name, value in [('BASE', self.base), ('UNIT', self.unit), ('PROFILE', self.profile)]:
            context = patch.object(update, name, value)
            context.start(); self.addCleanup(context.stop)
        context = patch.object(update, 'load_profile', return_value=(self.settings, None))
        context.start(); self.addCleanup(context.stop)
        # The production path is validated as a value, never inspected on this host.
        original = Path.is_symlink
        context = patch.object(Path, 'is_symlink', lambda path: False if path in
            (self.settings.database_path, self.settings.database_path.parent) else original(path))
        context.start(); self.addCleanup(context.stop)
        self.events = []
        self.overrides = {}

    def execute(self, *args):
        self.events.append(args)
        prop = next((a for a in args if a.startswith('--property=')), '')
        if prop in self.overrides:
            return self.overrides[prop]
        return {'--property=FragmentPath': str(self.unit), '--property=DropInPaths': '', '--property=ActiveState': 'inactive'}.get(prop, '')

    def save(self, profile, backup, **kwargs):
        self.assertEqual(self.unit.read_text(encoding='utf-8'), self.template)
        self.assertEqual(kwargs['root'], self.base)
        self.events.append(('backup',))

    def activate(self, **kwargs):
        update.activate(self.release, self.base / 'backup', execute=self.execute,
                        save=kwargs.get('save', self.save), readiness=kwargs.get('readiness', lambda _: self.events.append(('ready',))))

    def test_backup_precedes_switch_and_start(self):
        self.activate()
        self.assertLess(self.events.index(('systemctl', 'stop', update.SERVICE)), self.events.index(('backup',)))
        self.assertLess(self.events.index(('backup',)), self.events.index(('systemctl', 'start', update.SERVICE)))
        self.assertEqual(self.unit.read_text(encoding='utf-8'), update.render_unit(self.template, self.release))

    def test_backup_failure_never_switches_or_starts(self):
        def fail(*args, **kwargs):
            raise OSError('disk full')
        with self.assertRaises(OSError):
            self.activate(save=fail)
        self.assertEqual(self.unit.read_text(encoding='utf-8'), self.template)
        self.assertNotIn(('systemctl', 'start', update.SERVICE), self.events)

    def test_failed_readiness_stops_candidate_without_rollback(self):
        def fail(_):
            raise RuntimeError('not ready')
        with self.assertRaises(RuntimeError):
            self.activate(readiness=fail)
        self.assertEqual(self.events[-1], ('systemctl', 'stop', update.SERVICE))
        self.assertEqual(self.unit.read_text(encoding='utf-8'), update.render_unit(self.template, self.release))
        self.assertEqual(self.events.count(('backup',)), 1)

    def test_dropin_refused_before_stop(self):
        self.overrides['--property=DropInPaths'] = '/etc/systemd/system/custom.conf'
        with self.assertRaises(ValueError):
            self.activate()
        self.assertNotIn(('systemctl', 'stop', update.SERVICE), self.events)

    def test_foreign_unit_refused_before_commands(self):
        self.unit.write_text(self.template + 'ExecStartPost=/bin/true\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.activate()
        self.assertEqual(self.events, [])

    def test_stop_must_be_confirmed(self):
        self.overrides['--property=ActiveState'] = 'deactivating'
        with self.assertRaises(RuntimeError):
            self.activate()
        self.assertNotIn(('backup',), self.events)

    def test_check_api_failure_leaves_current_service_running(self):
        original = self.execute
        def fail(*args):
            if 'check-api' in args:
                raise RuntimeError('API unavailable')
            return original(*args)
        self.execute = fail
        with self.assertRaises(RuntimeError):
            self.activate()
        self.assertNotIn(('systemctl', 'stop', update.SERVICE), self.events)


if __name__ == '__main__':
    unittest.main()
