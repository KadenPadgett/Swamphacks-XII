"""Recovery must preserve originals and refuse to overwrite newer user data."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import database

spec = importlib.util.spec_from_file_location('repair_database', Path(__file__).parents[1] / 'scripts/repair_database.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class DatabaseRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / '.cache').mkdir()
        self.source = self.root / 'sites.db'
        self.candidate = self.root / '.cache/recovered.db'
        self.enterContext(patch.object(database, 'DATABASE_PATH', self.candidate))
        database.initialize_database()
        self.manifest = self.root / '.cache/recovery.json'
        self.source.write_bytes(b'TEST ONLY damaged original database')
        self.manifest.write_text(json.dumps({'untouched_source_sha256': repair.digest(self.source),
            'recovered': str(self.candidate), 'tables': {'risk_snapshots': {'failed_rows': []}}}))
        self.enterContext(patch.object(repair, 'ROOT', self.root))
        self.enterContext(patch.object(repair, 'MANIFEST', self.manifest))
        self.enterContext(patch('sys.argv', ['repair_database.py', '--restore']))

    def test_restores_verified_copy_and_keeps_original_bytes_with_audit(self):
        original = self.source.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(repair.main(), 0)
        self.assertEqual(repair.inspect(self.source)['integrity_check'], ['ok'])
        self.assertEqual(repair.inspect(self.source)['site_count'], 61)
        backups = list((self.root / '.cache').glob('sites.pre-repair.*.db'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(len(list((self.root / '.cache').glob('database-repair.*.json'))), 1)

    def test_changed_source_is_never_replaced_by_an_older_recovery(self):
        self.source.write_bytes(b'TEST ONLY newer user data since original recovery')
        before = self.source.read_bytes()
        with self.assertRaisesRegex(SystemExit, 'Source changed'):
            repair.main()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list((self.root / '.cache').glob('sites.pre-repair.*.db')), [])


if __name__ == '__main__':
    unittest.main()
