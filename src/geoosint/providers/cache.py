import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from geoosint.storage.database import canonical, digest


class Cache:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.hits = self.misses = 0
        self.cx = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.cx.execute("PRAGMA journal_mode=WAL")
        self.cx.execute("PRAGMA busy_timeout=5000")
        self.cx.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.cx.execute(
            "CREATE TABLE IF NOT EXISTS entries(key TEXT PRIMARY KEY, created REAL, "
            "expires REAL, checksum TEXT, status TEXT, source TEXT, value TEXT)"
        )

    @staticmethod
    def key(parameters: dict) -> str:
        return digest(["osm-overpass", "v1", parameters])

    def get(self, key: str) -> dict | None:
        with self.lock:
            row = self.cx.execute(
                "SELECT expires,checksum,value FROM entries WHERE key=?", (key,)
            ).fetchone()
            if (
                row
                and row[0] > time.time()
                and hashlib.sha256(row[2].encode()).hexdigest() == row[1]
            ):
                self.hits += 1
                return json.loads(row[2])
            if row:
                self.cx.execute("DELETE FROM entries WHERE key=?", (key,))
            self.misses += 1
            return None

    def put(self, key: str, value: dict) -> None:
        data = canonical(value)
        if len(data.encode()) > self.max_bytes:
            return
        ttl = 86400 if value.get("features") else 60
        with self.lock:
            self.cx.execute(
                "INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    time.time(),
                    time.time() + ttl,
                    hashlib.sha256(data.encode()).hexdigest(),
                    "OK",
                    "OpenStreetMap",
                    data,
                ),
            )
            self.cx.execute("DELETE FROM entries WHERE expires<=?", (time.time(),))
            while (
                self.cx.execute(
                    "SELECT coalesce(sum(length(CAST(value AS BLOB))),0) FROM entries"
                ).fetchone()[0]
                > self.max_bytes
            ):
                self.cx.execute(
                    "DELETE FROM entries WHERE key=(SELECT key FROM entries ORDER BY created LIMIT 1)"
                )
            self.cx.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        self.cx.close()
