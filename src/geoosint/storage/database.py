import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geoosint.errors import GeoError


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA secure_delete=ON")
        self.connection.executescript(Path(__file__).with_name("001_initial.sql").read_text())
        version = self.connection.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        if version == 1:
            self.connection.executescript(
                "BEGIN IMMEDIATE;"
                + Path(__file__).with_name("002_search_scope.sql").read_text()
                + "COMMIT;"
            )
        if version <= 2:
            self.connection.executescript(
                "BEGIN IMMEDIATE;"
                + Path(__file__).with_name("003_artifacts.sql").read_text()
                + "COMMIT;"
            )
        if version <= 3:
            self.connection.executescript(
                "BEGIN IMMEDIATE;"
                + Path(__file__).with_name("004_request_traces.sql").read_text()
                + "COMMIT;"
            )
        elif version != 4:
            raise ValueError("Неподдерживаемая версия базы")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    def read(self, owner: str, case_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.connection.execute(
                "SELECT document FROM cases WHERE id=? AND owner=?", (case_id, owner)
            ).fetchone()
        if row is None:
            raise GeoError("NOT_FOUND", "Расследование не найдено")
        return json.loads(row[0])

    def append_event(self, connection, owner: str, document: dict, operation: str) -> None:
        row = connection.execute(
            "SELECT event_hash FROM events WHERE case_id=? AND owner=? ORDER BY revision DESC LIMIT 1",
            (document["id"], owner),
        ).fetchone()
        previous = row[0] if row else ""
        content_hash = digest(document)
        event_hash = digest(
            [previous, document["id"], document["revision"], operation, content_hash]
        )
        connection.execute(
            "INSERT INTO events(case_id,owner,revision,created_at,operation,"
            "content_hash,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
            (
                document["id"],
                owner,
                document["revision"],
                now(),
                operation,
                content_hash,
                previous,
                event_hash,
            ),
        )

    def integrity(self, owner: str) -> dict:
        with self.lock:
            sqlite_status = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
            rows = self.connection.execute(
                "SELECT * FROM events WHERE owner=? ORDER BY case_id,revision", (owner,)
            ).fetchall()
            previous: dict[str, str] = {}
            heads: dict[str, tuple[int, str]] = {}
            for row in rows:
                case_id = row["case_id"]
                expected = digest(
                    [
                        previous.get(case_id, ""),
                        case_id,
                        row["revision"],
                        row["operation"],
                        row["content_hash"],
                    ]
                )
                if expected != row["event_hash"] or row["previous_hash"] != previous.get(
                    case_id, ""
                ):
                    return {"status": "CORRUPTED"}
                previous[case_id] = expected
                heads[case_id] = (row["revision"], row["content_hash"])
            docs = self.connection.execute("SELECT * FROM cases WHERE owner=?", (owner,)).fetchall()
            if any(
                heads.get(row["id"]) != (row["revision"], digest(json.loads(row["document"])))
                for row in docs
            ):
                return {"status": "CORRUPTED"}
        return {
            "status": "OK" if sqlite_status == "ok" else "CORRUPTED",
            "events": len(rows),
            "limitations": ["Хеш-цепочка обнаруживает повреждения, не доказывает происхождение"],
        }

    def backup(self, destination: Path) -> None:
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise ValueError("Нужен новый путь резервной копии")
        with self.lock, sqlite3.connect(destination) as target:
            self.connection.backup(target)

    def close(self) -> None:
        with self.lock:
            self.connection.close()
