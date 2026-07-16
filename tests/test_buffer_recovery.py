from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from host_monitor.buffer import SqliteQueue


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeHttpStatusError(Exception):
    def __init__(self, status_code: int):
        self.response = _FakeResponse(status_code)


fake_httpx = types.ModuleType("httpx")
fake_httpx.HTTPStatusError = _FakeHttpStatusError
sys.modules.setdefault("httpx", fake_httpx)

from host_monitor.sender import _sanitize_telemetry_payload, is_permanent_http_error


class BufferRecoveryTests(unittest.TestCase):
    def test_invalid_coordinates_are_sanitized(self) -> None:
        payload = _sanitize_telemetry_payload({"lat": 52.4, "lon": 182.85, "gps_valid": True, "speed_kmh": 8})
        self.assertEqual(payload["lat"], 0.0)
        self.assertEqual(payload["lon"], 0.0)
        self.assertFalse(payload["gps_valid"])
        self.assertEqual(payload["speed_kmh"], 0.0)

    def test_permanent_http_errors_are_classified(self) -> None:
        self.assertTrue(is_permanent_http_error(_FakeHttpStatusError(400)))
        self.assertFalse(is_permanent_http_error(_FakeHttpStatusError(429)))

    def test_dead_letter_move_removes_only_rejected_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "buffer.sqlite3"
            queue = SqliteQueue(sqlite_path=str(db_path), table="telemetry", max_rows=10)
            queue.put({"id": 1})
            queue.put({"id": 2})
            first_id, first_payload = queue.peek_batch(1)[0]
            self.assertEqual(json.loads(first_payload), {"id": 1})
            self.assertTrue(queue.move_to_dead_letter(first_id, "HTTP 400"))
            self.assertEqual([json.loads(payload) for _, payload in queue.peek_batch(10)], [{"id": 2}])

            dead_letter = queue._conn.execute(
                "SELECT queue_id, failure_reason, payload_json FROM telemetry_dead_letter"
            ).fetchone()
            self.assertEqual(dead_letter[0], first_id)
            self.assertEqual(dead_letter[1], "HTTP 400")
            self.assertEqual(json.loads(dead_letter[2]), {"id": 1})
            queue._conn.close()


if __name__ == "__main__":
    unittest.main()
