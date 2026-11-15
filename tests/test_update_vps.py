import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.update_vps import database_is_tracked


class DatabaseGitGuardTests(unittest.TestCase):
    def test_external_database_does_not_call_git(self):
        git = Mock(side_effect=AssertionError('External path passed to Git'))
        self.assertFalse(database_is_tracked(
            git, Path('/opt/dickcraft/app'), Path('/var/lib/dickcraft/game.sqlite3')))
        git.assert_not_called()

    def test_database_tracking_with_real_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'tracked.sqlite3').touch()
            (root / 'untracked.sqlite3').touch()
            subprocess.run(['git', '-C', str(root), 'add', 'tracked.sqlite3'], check=True)

            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True,
                                      text=True, capture_output=True).stdout.strip()

            self.assertTrue(database_is_tracked(git, root, root / 'tracked.sqlite3'))
            self.assertFalse(database_is_tracked(git, root, root / 'untracked.sqlite3'))
            self.assertFalse(database_is_tracked(git, root, root.parent / 'external.sqlite3'))
