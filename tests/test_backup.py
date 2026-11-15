import os
from pathlib import Path
import pwd
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.backup_db import snapshot
from scripts.install_backup import units


class BackupTests(unittest.TestCase):
    def test_live_wal_snapshot_restore_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'live.db'
            copies=root/'copies'
            copies.mkdir()
            live=sqlite3.connect(source)
            self.addCleanup(live.close)
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('CREATE TABLE players(id INTEGER PRIMARY KEY,name TEXT)')
            live.execute('INSERT INTO players VALUES(1,?)',('Игрок',))
            live.commit()
            account=pwd.getpwuid(os.getuid()).pw_name
            real_run=subprocess.run
            def run_as_service(command, **kwargs):
                # Test actual backup subprocess under the test account, without needing root/runuser.
                return real_run(command[4:], **kwargs)
            with patch('scripts.backup_db.secure_directory',return_value=copies), patch('scripts.backup_db.os.chown'), patch('scripts.backup_db.subprocess.run',side_effect=run_as_service):
                for _ in range(3):
                    final=snapshot(source,copies,account,keep=2)
                self.assertEqual(len(list(copies.glob('game-*.sqlite3'))),2)
                self.assertEqual(final.stat().st_mode & 0o777,0o600)
                # Restore to a separate path, never replace the live DB during a test.
                restored=root/'restored.db'
                restored.write_bytes(final.read_bytes())
                with sqlite3.connect(restored) as db:
                    self.assertEqual(db.execute('SELECT * FROM players').fetchall(),[(1,'Игрок')])
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                existing=set(copies.iterdir())
                with self.assertRaises(FileNotFoundError):
                    snapshot(root/'missing.db',copies,account,keep=2)
                self.assertEqual(set(copies.iterdir()),existing)

    def test_timer_and_unit_escaping(self):
        service,timer=units('/var/lib/a b/100%game.sqlite3','dickcraft')
        self.assertIn('100%%game',service)
        self.assertIn('UMask=0077',service)
        self.assertIn('Persistent=true',timer)
        self.assertIn('OnCalendar=*-*-* 03:00:00 Europe/Moscow',timer)
        with self.assertRaises(ValueError):
            units('/tmp/x\nExecStart=bad','user')
