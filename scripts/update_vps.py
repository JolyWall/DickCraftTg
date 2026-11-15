#!/usr/bin/env python3
"""Update a running systemd installation without replacing its SQLite database."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import sqlite3
import subprocess
import tempfile
import time


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def fingerprint(db):
    digest = hashlib.sha256()
    count = 0
    for row in db.execute('SELECT chat,user,name,length,day FROM players ORDER BY chat,user'):
        digest.update(json.dumps(row, ensure_ascii=True).encode() + b'\n')
        count += 1
    return count, digest.hexdigest()


def connect_readonly(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', help='systemd service name (prompted if omitted)')
    parser.add_argument('--owner-id', type=int, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run using sudo python3.')
    if args.owner_id <= 0:
        raise SystemExit('OWNER_ID must be positive.')
    service = args.service or input('Имя systemd-сервиса бота (например dickcraft.service): ').strip()
    if not service.endswith('.service'):
        service += '.service'
    if '/' in service or service.startswith('-'):
        raise SystemExit('Invalid service name.')
    if output('systemctl', 'is-active', service) != 'active':
        raise SystemExit('Service must be running so its real DB_PATH can be identified.')
    pid = int(output('systemctl', 'show', '--property=MainPID', '--value', service))
    process = Path('/proc') / str(pid)
    cwd = (process / 'cwd').resolve(strict=True)
    cmd = (process / 'cmdline').read_bytes().split(b'\0')
    if not any(Path(os.fsdecode(arg)).name == 'bot.py' for arg in cmd if arg):
        raise SystemExit('MainPID is not bot.py. Automatic deployment supports a direct systemd Python service only.')
    # Read only the required fields; never print credentials or dump the environment.
    env = dict(item.split(b'=', 1) for item in (process / 'environ').read_bytes().split(b'\0') if b'=' in item)
    db_path = Path(os.fsdecode(env.get(b'DB_PATH', b'data/game.sqlite3')))
    if not db_path.is_absolute():
        db_path = cwd / db_path
    db_path = db_path.resolve(strict=True)
    uid = process.stat().st_uid
    account = pwd.getpwuid(uid).pw_name

    def git(*arguments):
        return output('runuser', '-u', account, '--', 'git', '-C', str(cwd), *arguments)

    root = Path(git('rev-parse', '--show-toplevel')).resolve()
    if root != cwd or not (root / 'bot.py').is_file():
        raise SystemExit('Expected repository root to equal the running bot working directory.')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise SystemExit('Tracked files have local changes; preserve/review them before updating.')
    if Path(git('ls-files', '--', str(db_path))).name:
        raise SystemExit('Database is tracked by Git; refusing deployment.')
    old_commit = git('rev-parse', 'HEAD')
    git('fetch', 'origin', 'main')
    target = git('rev-parse', 'FETCH_HEAD')
    git('merge-base', '--is-ancestor', old_commit, target)
    Path('/var/backups').mkdir(parents=True, exist_ok=True)
    backup_dir = Path(tempfile.mkdtemp(prefix='dickcraft-' + time.strftime('%Y%m%d-%H%M%S-'), dir='/var/backups'))
    os.chown(backup_dir, uid, process.stat().st_gid)
    backup = backup_dir / 'game.sqlite3'
    override_dir = Path('/etc/systemd/system') / (service + '.d')
    override = override_dir / '99-dickcraft-owner.conf'
    old_override = override.read_bytes() if override.exists() else None
    (backup_dir / 'previous-commit.txt').write_text(old_commit + '\n')
    if old_override is not None:
        (backup_dir / 'previous-owner.conf').write_bytes(old_override)
    print('Database:', db_path)
    print('Backup directory:', backup_dir)
    print('Updating:', old_commit, '->', target, flush=True)
    stopped = False
    try:
        run('systemctl', 'stop', service)
        stopped = True
        run('runuser', '-u', account, '--', 'python3', '-c',
            "import sqlite3,sys; from pathlib import Path; "
            "source=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True); "
            "dest=sqlite3.connect(sys.argv[2]); source.backup(dest); dest.close(); source.close()",
            str(db_path), str(backup))
        with connect_readonly(backup) as destination:
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
            before = fingerprint(destination)
        (backup_dir / 'players-fingerprint.json').write_text(json.dumps(before) + '\n')
        git('merge', '--ff-only', target)
        run('runuser', '-u', account, '--', 'sh', '-c',
            'cd "$1" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v',
            'sh', str(root))
        override_dir.mkdir(parents=True, exist_ok=True)
        override.write_text('[Service]\nEnvironment="OWNER_ID=' + str(args.owner_id) + '"\n')
        # Run schema initialisation as the service user, preserving DB/WAL ownership.
        validation = output('runuser', '-u', account, '--', 'python3', '-c',
            "import sys; sys.path.insert(0,sys.argv[1]); "
            "from bot import Administration,Game; "
            "game=Game(sys.argv[2],sys.argv[4]); Administration(game,int(sys.argv[3])); "
            "import hashlib,json; h=hashlib.sha256(); rows=game.db.execute("
            "'SELECT chat,user,name,length,day FROM players ORDER BY chat,user').fetchall(); "
            "[h.update(json.dumps(row,ensure_ascii=True).encode()+b'\\n') for row in rows]; "
            "assert game.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; "
            "print(json.dumps([len(rows),h.hexdigest()])); game.db.close()",
            str(root), str(db_path), str(args.owner_id),
            os.fsdecode(env.get(b'GAME_TIMEZONE', b'Europe/Moscow')))
        if json.loads(validation) != list(before):
            raise RuntimeError('Player data changed during deployment')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', service)
        time.sleep(3)
        if output('systemctl', 'is-active', service) != 'active':
            raise RuntimeError('Service did not stay active')
        new_pid = output('systemctl', 'show', '--property=MainPID', '--value', service)
        new_env = dict(item.split(b'=', 1) for item in (Path('/proc') / new_pid / 'environ').read_bytes().split(b'\0') if b'=' in item)
        if new_env.get(b'OWNER_ID') != str(args.owner_id).encode():
            raise RuntimeError('OWNER_ID is overridden by another service setting')
        if new_env.get(b'DB_PATH', b'data/game.sqlite3') != env.get(b'DB_PATH', b'data/game.sqlite3'):
            raise RuntimeError('DB_PATH changed')
        print('Update completed. Player rows preserved:', before[0])
        print('Backup:', backup)
        print('Check /admin in the bot. Existing groups now require your approval.')
    except BaseException:
        if stopped:
            print('Deployment failed; restoring previous code/config. Database is NOT replaced.', flush=True)
            run('systemctl', 'stop', service)
            git('checkout', '--detach', old_commit)
            if old_override is None:
                override.unlink(missing_ok=True)
            else:
                override.write_bytes(old_override)
            run('systemctl', 'daemon-reload')
            run('systemctl', 'start', service)
        raise


if __name__ == '__main__':
    main()
