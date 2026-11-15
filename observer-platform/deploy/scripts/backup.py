"""Consistent SQLite online backup. Does not delete or overwrite existing backups."""
import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('database', type=Path)
p.add_argument('--output-dir', type=Path, default=Path('backups'))
args = p.parse_args()
if not args.database.is_file():
    p.error('database does not exist')
args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
target = args.output_dir / ('observer-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.sqlite')
descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(descriptor)
with sqlite3.connect('file:' + str(args.database.resolve()) + '?mode=ro', uri=True) as source:
    with sqlite3.connect(target) as backup:
        source.backup(backup)
target.chmod(0o600)
print(target)
