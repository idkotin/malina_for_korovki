from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger("host_monitor.buffer")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class BufferCfg:
    sqlite_path: str
    max_rows: int


class SqliteQueue:
    """
    SQLite-backed FIFO queue for JSON payloads.
    We keep separate tables for different streams (telemetry vs modem events).
    """

    def __init__(self, *, sqlite_path: str, table: str, max_rows: int):
        self._path = Path(sqlite_path)
        self._table = table
        self._max_rows = max_rows
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            "PRAGMA synchronous={};".format("FULL" if self._table == "telemetry" else "NORMAL")
        )
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_utc TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            """.format(table=self._table)
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_{t}_id ON {t}(id);".format(t=self._table)
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS {table}_dead_letter (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              queue_id INTEGER NOT NULL,
              queued_created_utc TEXT NOT NULL,
              failed_utc TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              failure_reason TEXT NOT NULL
            );
            """.format(table=self._table)
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_{t}_dead_letter_queue_id "
            "ON {t}_dead_letter(queue_id);".format(t=self._table)
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_metadata (
              stream TEXT NOT NULL,
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              PRIMARY KEY(stream, key)
            );
            """
        )
        self._conn.commit()

    def put(self, payload: dict) -> int:
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO {t}(created_utc, payload_json) VALUES(?, ?);".format(t=self._table),
                    (utc_now_iso(), payload_json),
                )
                self._conn.commit()
                row_id = int(cur.lastrowid)
        except Exception:
            log.exception("failed to put payload into buffer")
            raise
        self._trim_if_needed()
        return row_id

    def _trim_if_needed(self) -> None:
        try:
            with self._lock:
                cur = self._conn.execute("SELECT COUNT(*) FROM {t};".format(t=self._table))
                (count,) = cur.fetchone() or (0,)
            if count <= self._max_rows:
                return
            if self._table == "telemetry":
                log.error(
                    "%s has %s rows (configured max_rows=%s); preserving telemetry instead of dropping data",
                    self._table,
                    count,
                    self._max_rows,
                )
                return
            delete_n = count - self._max_rows
            with self._lock:
                self._conn.execute(
                    """
                    DELETE FROM {t}
                    WHERE id IN (SELECT id FROM {t} ORDER BY id ASC LIMIT ?);
                    """.format(t=self._table),
                    (delete_n,),
                )
                self._conn.commit()
            log.warning("%s trimmed by %s rows (max_rows=%s)", self._table, delete_n, self._max_rows)
        except Exception:
            log.exception("failed to trim buffer")

    def peek_batch(self, limit: int) -> list[tuple[int, str]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, payload_json FROM {t} ORDER BY id ASC LIMIT ?;".format(t=self._table),
                (limit,),
            )
            return [(int(r[0]), str(r[1])) for r in cur.fetchall()]

    def peek_fresh_first(self, live_id: int | None, limit: int) -> list[tuple[int, str]]:
        """Return one newest/live row followed by the oldest backlog rows."""
        limit = max(1, int(limit))
        with self._lock:
            live = None
            if live_id is not None:
                live = self._conn.execute(
                    "SELECT id, payload_json FROM {t} WHERE id = ?;".format(t=self._table),
                    (int(live_id),),
                ).fetchone()
            if live is None:
                live = self._conn.execute(
                    "SELECT id, payload_json FROM {t} ORDER BY id DESC LIMIT 1;".format(t=self._table)
                ).fetchone()
            if live is None:
                return []
            remaining = self._conn.execute(
                "SELECT id, payload_json FROM {t} WHERE id <> ? ORDER BY id ASC LIMIT ?;".format(t=self._table),
                (int(live[0]), limit - 1),
            ).fetchall()
            return [(int(live[0]), str(live[1]))] + [(int(r[0]), str(r[1])) for r in remaining]

    def get_or_create_stream_id(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM queue_metadata WHERE stream = ? AND key = 'stream_id';",
                (self._table,),
            ).fetchone()
            if row and row[0]:
                return str(row[0])
            stream_id = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO queue_metadata(stream, key, value) VALUES(?, 'stream_id', ?);",
                (self._table, stream_id),
            )
            self._conn.commit()
            return stream_id

    def delete_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        q = ",".join(["?"] * len(ids))
        with self._lock:
            self._conn.execute("DELETE FROM {t} WHERE id IN ({q});".format(t=self._table, q=q), ids)
            self._conn.commit()

    def move_to_dead_letter(self, row_id: int, reason: str) -> bool:
        """Move one permanently rejected row out of the FIFO queue.

        The insert and delete share one SQLite transaction, so a rejected
        packet is either retained in the queue or retained for diagnostics.
        """
        cur = self._conn.execute(
            "SELECT created_utc, payload_json FROM {t} WHERE id = ?;".format(t=self._table),
            (row_id,),
        )
        row = cur.fetchone()
        if not row:
            return False

        self._conn.execute(
            """
            INSERT INTO {t}_dead_letter(
              queue_id, queued_created_utc, failed_utc, payload_json, failure_reason
            ) VALUES(?, ?, ?, ?, ?);
            """.format(t=self._table),
            (row_id, str(row[0]), utc_now_iso(), str(row[1]), reason),
        )
        self._conn.execute("DELETE FROM {t} WHERE id = ?;".format(t=self._table), (row_id,))
        self._conn.commit()
        return True

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM {t};".format(t=self._table))
            (count,) = cur.fetchone() or (0,)
            return int(count)

    def oldest_age_s(self) -> float | None:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT created_utc FROM {t} ORDER BY id ASC LIMIT 1;".format(t=self._table)
                )
                row = cur.fetchone()
            if not row or not row[0]:
                return None
            created = str(row[0]).replace("Z", "+00:00")
            dt = datetime.fromisoformat(created)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

