"""Small SQLite store owned exclusively by this plugin.

Only account bindings, public API cache, request budgets and bounded diagnostic
summaries are persisted. No conversation transcripts or provider credentials.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "dota2_data.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS bindings (
                    scope TEXT NOT NULL, user_id TEXT NOT NULL,
                    account_id INTEGER NOT NULL, alias TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL, PRIMARY KEY(scope,user_id)
                );
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_usage (
                    day TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS query_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                    status TEXT NOT NULL, at TEXT NOT NULL, duration_ms INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def bindings(self, scope: str | None = None) -> list[dict]:
        with self.connect() as db:
            if scope is None:
                rows = db.execute("SELECT * FROM bindings ORDER BY scope,user_id").fetchall()
            else:
                rows = db.execute("SELECT * FROM bindings WHERE scope=? ORDER BY user_id", (scope,)).fetchall()
        return [dict(row) for row in rows]

    def bind(self, scope: str, user_id: str, account_id: int, alias: str) -> dict:
        row = dict(scope=scope, user_id=user_id, account_id=account_id,
                   alias=alias, updated_at=utc_now())
        with self.connect() as db:
            db.execute("""INSERT INTO bindings(scope,user_id,account_id,alias,updated_at)
                VALUES(:scope,:user_id,:account_id,:alias,:updated_at)
                ON CONFLICT(scope,user_id) DO UPDATE SET
                account_id=excluded.account_id,alias=excluded.alias,updated_at=excluded.updated_at""", row)
        return row

    def unbind(self, scope: str, user_id: str) -> bool:
        with self.connect() as db:
            return db.execute("DELETE FROM bindings WHERE scope=? AND user_id=?", (scope, user_id)).rowcount > 0

    def cached(self, key: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM cache WHERE key=? AND expires_at>?", (key, time.time())).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"]), row["fetched_at"]
        except (ValueError, TypeError):
            with self.connect() as db:
                db.execute("DELETE FROM cache WHERE key=?", (key,))
            return None

    def put_cache(self, key: str, payload, ttl: int, fetched_at: str):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > 4 * 1024 * 1024:
            return
        with self.connect() as db:
            db.execute("DELETE FROM cache WHERE expires_at<=?", (time.time(),))
            db.execute("INSERT OR REPLACE INTO cache(key,payload,fetched_at,expires_at) VALUES(?,?,?,?)",
                       (key, body, fetched_at, time.time() + ttl))
            # Bound disk use even when each request uses different parameters.
            db.execute("DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache ORDER BY expires_at DESC LIMIT 250)")

    def clear_cache(self) -> dict:
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            db.execute("DELETE FROM cache")
        return {"removed": count}

    def reserve_request(self, day: str, daily_limit: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT count FROM request_usage WHERE day=?", (day,)).fetchone()
            if row and row[0] >= daily_limit:
                return False
            db.execute("INSERT INTO request_usage(day,count) VALUES(?,1) ON CONFLICT(day) DO UPDATE SET count=count+1", (day,))
            db.execute("DELETE FROM request_usage WHERE day NOT IN (SELECT day FROM request_usage ORDER BY day DESC LIMIT 40)")
        return True

    def record_query(self, action: str, status: str, duration_ms: int):
        with self.connect() as db:
            db.execute("INSERT INTO query_log(action,status,at,duration_ms) VALUES(?,?,?,?)",
                       (action[:40], status[:30], utc_now(), duration_ms))
            db.execute("DELETE FROM query_log WHERE id NOT IN (SELECT id FROM query_log ORDER BY id DESC LIMIT 200)")

    def recent_queries(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM query_log ORDER BY id DESC LIMIT 50")]

    def status(self, day: str) -> dict:
        with self.connect() as db:
            count = db.execute("SELECT count FROM request_usage WHERE day=?", (day,)).fetchone()
            return {
                "binding_count": db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0],
                "cache_entries": db.execute("SELECT COUNT(*) FROM cache WHERE expires_at>?", (time.time(),)).fetchone()[0],
                "query_count": db.execute("SELECT COUNT(*) FROM query_log").fetchone()[0],
                "requests_today": count[0] if count else 0,
                "last_query_at": db.execute("SELECT MAX(at) FROM query_log").fetchone()[0],
            }
