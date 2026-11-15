#!/usr/bin/env python3
"""Generate VIEWER_PASSWORD_HASH interactively; never accepts plaintext argv."""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cloud.backend.security import hash_password


def main():
    password = getpass.getpass('Viewer password: ')
    if password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords do not match')
    try:
        print(hash_password(password))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == '__main__':
    main()
