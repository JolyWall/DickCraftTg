#!/usr/bin/env python3
"""Install a daily systemd backup timer using the running bot's database path."""
import argparse
import os
from pathlib import Path
import pwd
import shutil
import subprocess


def units(db, user):
    def quote(value):
        if any(c in str(value) for c in '\n\r\x00'):
            raise ValueError('Invalid unit argument')
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
    service = ('[Unit]\nDescription=Verified DickCraft SQLite backup\n\n[Service]\nType=oneshot\n'
               'UMask=0077\nExecStart=/usr/bin/python3 /usr/local/lib/dickcraft/backup_db.py --db '
               + quote(db) + ' --user ' + quote(user) + '\nTimeoutStartSec=360\n')
    timer = ('[Unit]\nDescription=Daily DickCraft backup\n\n[Timer]\nOnCalendar=*-*-* 03:00:00 Europe/Moscow\n'
             'Persistent=true\nRandomizedDelaySec=15m\n\n[Install]\nWantedBy=timers.target\n')
    return service, timer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', default='dickcraft.service')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run using sudo.')
    pid = subprocess.check_output(['systemctl', 'show', '--property=MainPID', '--value', args.service], text=True).strip()
    process = Path('/proc') / pid
    env = dict(x.split(b'=', 1) for x in (process/'environ').read_bytes().split(b'\0') if b'=' in x)
    path = Path(os.fsdecode(env.get(b'DB_PATH', b'data/game.sqlite3')))
    if not path.is_absolute():
        path = (process/'cwd').resolve() / path
    path = path.resolve(strict=True)
    user = pwd.getpwuid(process.stat().st_uid).pw_name
    service, timer = units(path, user)
    destination = Path('/usr/local/lib/dickcraft')
    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
    shutil.copyfile(Path(__file__).with_name('backup_db.py'), destination/'backup_db.py')
    os.chown(destination/'backup_db.py', 0, 0)
    os.chmod(destination/'backup_db.py', 0o644)
    for extension, content in [('service', service), ('timer', timer)]:
        unit = Path('/etc/systemd/system') / ('dickcraft-backup.'+extension)
        unit.write_text(content)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    # Verify a real snapshot before enabling scheduling.
    subprocess.run(['systemctl', 'start', 'dickcraft-backup.service'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'dickcraft-backup.timer'], check=True)
    print('Daily backups enabled; 14 verified snapshots retained in /var/backups/dickcraft-daily')


if __name__ == '__main__':
    main()
