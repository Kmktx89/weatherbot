"""SQLite-backed cache for lab inputs.

Stores a single table of (key, json-value, fetched_at, source, target_date).
Reads check per-source TTL (the caller supplies it). Writes overwrite.
"""
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetches (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    target_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_date ON fetches(source, target_date);
"""


class DataCache:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)

    def get(self, key: str, *, ttl: int | None) -> Any | None:
        """Return the cached value or None.

        `ttl` is per-source max age in seconds. None = no expiry.
        """
        row = self._conn.execute(
            "SELECT value, fetched_at FROM fetches WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value_json, fetched_at = row
        if ttl is not None and (time.time() - fetched_at) > ttl:
            return None
        return json.loads(value_json)

    def set(self, key: str, value: Any, *, source: str, target_date: str | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO fetches (key, value, fetched_at, source, target_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, json.dumps(value), int(time.time()), source, target_date),
        )

    def purge_before(self, target_date: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM fetches WHERE target_date IS NOT NULL AND target_date < ?",
            (target_date,),
        )
        return cur.rowcount

    def stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0]
        by_source = dict(self._conn.execute(
            "SELECT source, COUNT(*) FROM fetches GROUP BY source"
        ).fetchall())
        oldest = self._conn.execute("SELECT MIN(fetched_at) FROM fetches").fetchone()[0]
        return {"total": total, "by_source": by_source, "oldest": oldest}

    def close(self) -> None:
        self._conn.close()


# Module-level singleton for production lab use.
_DEFAULT_PATH = Path(__file__).resolve().parent / ".cache.sqlite"
_default: DataCache | None = None


def default() -> DataCache:
    global _default
    if _default is None:
        _default = DataCache(_DEFAULT_PATH)
    return _default
