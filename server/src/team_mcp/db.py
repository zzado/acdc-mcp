import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now() -> int:
    return int(time.time())


def iso(seconds: int | None) -> str | None:
    return datetime.fromtimestamp(seconds, UTC).isoformat() if seconds is not None else None


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(data) -> str:
    return hashlib.sha256(canonical(data).encode()).hexdigest()


class DomainError(Exception):
    def __init__(self, code: str, message: str, *, retryable=False, retry_after_ms=None):
        super().__init__(message)
        self.body = {"code": code, "message": message, "retryable": retryable}
        if retry_after_ms is not None:
            self.body["retry_after_ms"] = retry_after_ms


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def transaction(self, *, write=False):
        connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.create_function("casefold", 1, str.casefold, deterministic=True)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self):
        if sqlite3.sqlite_version_info < (3, 51, 3):
            raise RuntimeError("SQLite 3.51.3 or later is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise RuntimeError("The database must support WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            migrations = sorted(Path(__file__).with_name("migrations").glob("[0-9]*.sql"))
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if not migrations or version > len(migrations):
                raise RuntimeError("Unsupported SQLite schema version")
            for number, migration in enumerate(migrations, 1):
                if number > version:
                    connection.executescript(f"BEGIN IMMEDIATE;\n{migration.read_text(encoding='utf-8')}\n"
                                             f"PRAGMA user_version={number};\nCOMMIT;")
            connection.execute("INSERT OR IGNORE INTO settings VALUES ('event_epoch', ?)", (uuid4().hex,))
        finally:
            connection.close()


def audit(cx, action, *, actor=None, entity=None, outcome="ok"):
    cx.execute("INSERT INTO audit_log(action,actor_id,entity_id,outcome,created_at) VALUES (?,?,?,?,?)",
               (action, actor, entity, outcome, now()))


def event(cx, kind, entity, channel=None, user=None, data=None):
    return cx.execute("INSERT INTO events(kind,entity_id,channel_id,user_id,data,created_at) VALUES (?,?,?,?,?,?)",
                      (kind, str(entity), channel, user, canonical(data or {}), now())).lastrowid


def idempotent(cx, actor, tool, key, args, mutate):
    request_hash = digest(args)
    row = cx.execute("SELECT request_hash,result FROM idempotency_keys WHERE agent_id=? AND tool=? AND key=?",
                     (actor, tool, key)).fetchone()
    if row:
        if row["request_hash"] != request_hash:
            raise DomainError("CONFLICT", "Idempotency key was already used for different input")
        return json.loads(row["result"])
    result = mutate()
    cx.execute("INSERT INTO idempotency_keys VALUES (?,?,?,?,?)", (actor, tool, key, request_hash, canonical(result)))
    return result
