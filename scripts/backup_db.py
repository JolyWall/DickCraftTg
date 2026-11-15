#!/usr/bin/env python3
"""Root-managed verified SQLite snapshots; no service interruption required."""
import argparse
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone


def secure_directory(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid != 0:
        raise RuntimeError('Backup directory must be root-owned and not a symlink')
    os.chmod(directory, 0o700)
    return directory


def snapshot(source, directory, user, keep=14):
    if keep < 1:
        raise ValueError('keep must be positive')
    directory = secure_directory(directory)
    # The service account only sees staging; finished copies remain root-owned.
    staging_parent = Path(tempfile.mkdtemp(prefix='dickcraft-snapshot-'))
    account = pwd.getpwnam(user)
    os.chown(staging_parent, account.pw_uid, account.pw_gid)
    staged = staging_parent / 'snapshot.sqlite3'
    temporary = directory / ('.snapshot-' + staging_parent.name)
    try:
        subprocess.run(['runuser', '-u', user, '--', 'python3', '-c',
                        "import sqlite3,sys; from pathlib import Path; "
                        "src=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True,timeout=30); "
                        "dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()",
                        str(Path(source).resolve(strict=True)), str(staged)], check=True, timeout=300)
        # Take a root-owned independent copy before verification/publication.
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        source_fd = os.open(staged, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'wb') as destination, os.fdopen(source_fd, 'rb') as data:
            shutil.copyfileobj(data, destination)
            destination.flush()
            os.fsync(destination.fileno())
        with sqlite3.connect(temporary.as_uri()+'?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Snapshot integrity check failed')
        name = datetime.now(timezone.utc).strftime('game-%Y%m%dT%H%M%S%fZ.sqlite3')
        final = directory / name
        temporary.replace(final)
        # Only rotate our own snapshots, and only after a verified new copy exists.
        for old in sorted(directory.glob('game-*.sqlite3'), reverse=True)[keep:]:
            if old.is_file() and not old.is_symlink():
                old.unlink()
        print('Verified snapshot:', final)
        return final
    finally:
        temporary.unlink(missing_ok=True)
        shutil.rmtree(staging_parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--directory', default='/var/backups/dickcraft-daily')
    parser.add_argument('--keep', type=int, default=14)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run using sudo.')
    snapshot(args.db, args.directory, args.user, args.keep)


if __name__ == '__main__':
    main()
