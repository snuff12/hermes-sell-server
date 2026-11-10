"""Short SQLite transactions; external calls must never run in a transaction."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self.connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions (
              id TEXT PRIMARY KEY, slug TEXT NOT NULL, document TEXT NOT NULL,
              published INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS tests (
              id TEXT PRIMARY KEY, revision TEXT NOT NULL, mode TEXT NOT NULL,
              passed INTEGER NOT NULL, report TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, slug TEXT NOT NULL,
              idem TEXT NOT NULL, request_digest TEXT NOT NULL, document TEXT NOT NULL,
              execution TEXT NOT NULL, payment TEXT NOT NULL, created_at INTEGER NOT NULL,
              UNIQUE(owner,slug,idem));
            CREATE TABLE IF NOT EXISTS authorizations (
              identity TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
              kind TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, owner TEXT NOT NULL, expires INTEGER NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def tx(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def one(self, query: str, args=()):
        with closing(self.connect()) as db:
            return db.execute(query, args).fetchone()

    def all(self, query: str, args=()):
        with closing(self.connect()) as db:
            return db.execute(query, args).fetchall()

    def order(self, order_id: str) -> dict | None:
        row = self.one("SELECT document FROM orders WHERE id=?", (order_id,))
        return json.loads(row[0]) if row else None

    def flag_recovery(self):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery_required','true')")

    @property
    def recovery_required(self):
        return self.one("SELECT 1 FROM meta WHERE key='recovery_required' AND value='true'") is not None
