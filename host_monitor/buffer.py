from __future__ import annotations

import json
import logging
import sqlite3
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
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
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
        self._conn.commit()

    def put(self, payload: dict) -> None:
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self._conn.execute(
                "INSERT INTO {t}(created_utc, payload_json) VALUES(?, ?);".format(t=self._table),
                (utc_now_iso(), payload_json),
            )
            self._conn.commit()
        except Exception:
            log.exception("failed to put payload into buffer")
            raise
        self._trim_if_needed()

    def _trim_if_needed(self) -> None:
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM {t};".format(t=self._table))
            (count,) = cur.fetchone() or (0,)
            if count <= self._max_rows:
                return
            delete_n = count - self._max_rows
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
        cur = self._conn.execute(
            "SELECT id, payload_json FROM {t} ORDER BY id ASC LIMIT ?;".format(t=self._table),
            (limit,),
        )
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]

    def delete_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        q = ",".join(["?"] * len(ids))
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
        cur = self._conn.execute("SELECT COUNT(*) FROM {t};".format(t=self._table))
        (count,) = cur.fetchone() or (0,)
        return int(count)

    def oldest_age_s(self) -> float | None:
        try:
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
        self._conn.close()

