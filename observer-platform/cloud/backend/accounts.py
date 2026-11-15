"""Persistent viewer registrations; the configured observer stays the administrator."""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

USERNAME = re.compile(r'[a-z0-9_.-]{3,32}', re.ASCII)
PUBLIC_COLUMNS = 'id,username,status,created_at,approved_at'


class AccountConflict(ValueError):
    pass


class AccountCapacity(ValueError):
    pass


def normalize_username(value):
    if not isinstance(value, str):
        raise ValueError('用户名格式不正确')
    candidate = value.strip()
    username = candidate.lower()
    if not candidate.isascii() or not USERNAME.fullmatch(username):
        raise ValueError('用户名需为 3–32 位字母、数字、下划线、点或短横线')
    return username


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class Accounts:
    """Small parameterized registry, independently persisted from observation data."""
    MAXIMUM = 1000

    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'accounts.sqlite3'
        # Create the database privately before SQLite can create any journal.
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved')),
            created_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by TEXT
        )''')
        # The configured administrator has no registration row. Persist its
        # optional changed hash alongside registrations, never plaintext or env.
        self.db.execute('''CREATE TABLE IF NOT EXISTS administrator_passwords (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL
        )''')
        self.db.commit()

    def close(self):
        self.db.close()

    def ready(self):
        return self.db.execute('SELECT 1').fetchone()[0] == 1

    def find(self, username):
        row = self.db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        return dict(row) if row is not None else None

    def list_users(self):
        return [dict(row) for row in self.db.execute(
            f"SELECT {PUBLIC_COLUMNS} FROM users ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC LIMIT ?",
            (self.MAXIMUM,))]

    def register(self, username, password_hash):
        try:
            with self.db:
                # Atomic even if another process uses the same persistent registry.
                self.db.execute('BEGIN IMMEDIATE')
                if self.db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                    raise AccountConflict('用户名已被使用')
                if self.db.execute('SELECT COUNT(*) FROM users').fetchone()[0] >= self.MAXIMUM:
                    raise AccountCapacity('暂时无法注册，请联系管理员')
                cursor = self.db.execute(
                    'INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)',
                    (username, password_hash, utc_now()))
                row = self.db.execute(f'SELECT {PUBLIC_COLUMNS} FROM users WHERE id=?', (cursor.lastrowid,)).fetchone()
                return dict(row)
        except sqlite3.IntegrityError as exc:
            raise AccountConflict('用户名已被使用') from exc

    def approve(self, user_id, administrator):
        with self.db:
            self.db.execute("UPDATE users SET status='approved',approved_at=?,approved_by=? WHERE id=? AND status='pending'",
                            (utc_now(), administrator, user_id))
            row = self.db.execute(f'SELECT {PUBLIC_COLUMNS} FROM users WHERE id=?', (user_id,)).fetchone()
            return dict(row) if row is not None else None

    def administrator_password(self, username, fallback):
        row = self.db.execute('SELECT password_hash FROM administrator_passwords WHERE username=?',
                              (username,)).fetchone()
        return row['password_hash'] if row is not None else fallback

    def change_administrator_password(self, username, previous_hash, new_hash, fallback):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.administrator_password(username, fallback) != previous_hash:
                return False
            self.db.execute('INSERT INTO administrator_passwords(username,password_hash) VALUES(?,?) '
                            'ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash',
                            (username, new_hash))
            return True

    def change_password(self, user_id, previous_hash, new_hash):
        """Compare-and-swap prevents concurrent resets from restoring old credentials."""
        with self.db:
            cursor = self.db.execute('UPDATE users SET password_hash=? WHERE id=? AND password_hash=?',
                                     (new_hash, user_id, previous_hash))
            return cursor.rowcount == 1

    def delete(self, user_id, administrator):
        """Remove a registration without changing historical observation data.

        Account ids are never reused (AUTOINCREMENT), so an authenticated
        session bound to an old id cannot become a later registration's session.
        """
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(f'SELECT {PUBLIC_COLUMNS} FROM users WHERE id=?', (user_id,)).fetchone()
            if row is None:
                return None
            # The configured administrator normally has no registry row. Keep
            # this guard if an older/imported database contains that name.
            if row['username'].casefold() == administrator.casefold():
                raise ValueError('不能删除管理员账号')
            self.db.execute('DELETE FROM users WHERE id=?', (user_id,))
            return dict(row)
