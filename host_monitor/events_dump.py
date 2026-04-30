from __future__ import annotations

import argparse
import json
import sqlite3

from host_monitor.config import load_config


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Print buffered modem events without truncating SMS text.")
    p.add_argument("--config", default="./config.yaml")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--full-json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    cfg = load_config(args.config)
    limit = max(1, int(args.limit))

    conn = sqlite3.connect(cfg.buffer.sqlite_path)
    try:
        rows = conn.execute(
            "SELECT id, created_utc, payload_json FROM events ORDER BY id DESC LIMIT ?;",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    for row_id, created_utc, payload_json in rows:
        try:
            payload = json.loads(str(payload_json))
        except Exception:
            print(f"[{row_id}] {created_utc} INVALID_JSON")
            print(payload_json)
            continue

        if args.full_json:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            continue

        event_type = payload.get("type", "")
        from_num = payload.get("from", "")
        text = payload.get("text", "")
        print(f"[{row_id}] {created_utc} type={event_type} from={from_num}")
        if text:
            print(str(text))
        print()

