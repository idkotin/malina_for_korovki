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


class SqliteBuffer:
    def __init__(self, cfg: BufferCfg):
        self._path = Path(cfg.sqlite_path)
        self._max_rows = cfg.max_rows
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buffer (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_utc TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_buffer_id ON buffer(id);")
        self._conn.commit()

    def put(self, payload: dict) -> None:
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self._conn.execute(
                "INSERT INTO buffer(created_utc, payload_json) VALUES(?, ?);",
                (utc_now_iso(), payload_json),
            )
            self._conn.commit()
            self._trim_if_needed()
        except Exception:
            log.exception("failed to put payload into buffer")

    def _trim_if_needed(self) -> None:
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM buffer;")
            (count,) = cur.fetchone() or (0,)
            if count <= self._max_rows:
                return
            delete_n = count - self._max_rows
            self._conn.execute(
                """
                DELETE FROM buffer
                WHERE id IN (SELECT id FROM buffer ORDER BY id ASC LIMIT ?);
                """,
                (delete_n,),
            )
            self._conn.commit()
            log.warning("buffer trimmed by %s rows (max_rows=%s)", delete_n, self._max_rows)
        except Exception:
            log.exception("failed to trim buffer")

    def peek_batch(self, limit: int) -> list[tuple[int, str]]:
        cur = self._conn.execute(
            "SELECT id, payload_json FROM buffer ORDER BY id ASC LIMIT ?;",
            (limit,),
        )
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]

    def delete_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        q = ",".join(["?"] * len(ids))
        self._conn.execute(f"DELETE FROM buffer WHERE id IN ({q});", ids)
        self._conn.commit()

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM buffer;")
        (count,) = cur.fetchone() or (0,)
        return int(count)

