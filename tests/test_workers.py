from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from host_monitor.buffer import SqliteQueue
from host_monitor.models import Weight
from host_monitor.workers import BufferFlusher, OutboundDispatcher, TelemetryOutboxWorker, WeightSampler, WifiMonitor
from host_monitor.wifi_clients import _parse_active_ip_neigh


def wait_until(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _SlowWeightReader:
    def __init__(self):
        self.calls = 0

    def read_weight(self) -> Weight:
        time.sleep(0.08)
        self.calls += 1
        return Weight(weight=float(self.calls), raw=float(self.calls))


class _PermanentError(Exception):
    response = type("Response", (), {"status_code": 400})()


class _RejectFirstSender:
    sent: list[int] = []

    def __init__(self, url: str, timeout_s: float):
        self._rejected = False

    def send_buffered_telemetry_one(self, payload_json: str) -> None:
        payload = json.loads(payload_json)
        if not self._rejected:
            self._rejected = True
            raise _PermanentError("bad packet")
        self.sent.append(payload["id"])

    def send_json_string_one(self, payload_json: str) -> None:
        self.send_buffered_telemetry_one(payload_json)

    def close(self) -> None:
        pass


class _SlowSender:
    sent: list[int] = []

    def __init__(self, url: str, timeout_s: float):
        pass

    def send_one(self, payload: dict) -> None:
        time.sleep(0.1)
        self.sent.append(payload["id"])

    def close(self) -> None:
        pass


class _BatchSender:
    calls: list[dict] = []

    def __init__(self, url: str, timeout_s: float):
        pass

    def send_telemetry_batch(self, **kwargs) -> list[int]:
        self.calls.append(kwargs)
        return [row_id for row_id, _ in kwargs["rows"]]

    def close(self) -> None:
        pass


class WorkerTests(unittest.TestCase):
    def test_outbox_selects_live_packet_then_oldest_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = SqliteQueue(
                sqlite_path=str(Path(temp_dir) / "buffer.sqlite3"),
                table="telemetry",
                max_rows=100,
            )
            ids = [queue.put({"id": value}) for value in range(1, 6)]
            selected = queue.peek_fresh_first(ids[-1], 3)
            self.assertEqual([row_id for row_id, _ in selected], [ids[-1], ids[0], ids[1]])
            first_stream = queue.get_or_create_stream_id()
            queue.close()
            reopened = SqliteQueue(
                sqlite_path=str(Path(temp_dir) / "buffer.sqlite3"),
                table="telemetry",
                max_rows=100,
            )
            self.assertEqual(reopened.get_or_create_stream_id(), first_stream)
            reopened.close()

    def test_telemetry_worker_deletes_only_acknowledged_batch(self) -> None:
        _BatchSender.calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = SqliteQueue(
                sqlite_path=str(Path(temp_dir) / "buffer.sqlite3"),
                table="telemetry",
                max_rows=100,
            )
            old_ids = [queue.put({"id": value}) for value in range(3)]
            live_id = queue.put({"id": 99})
            worker = TelemetryOutboxWorker(
                device_id="Hozain_01",
                url="https://example.invalid/api/telemetry/host",
                timeout_s=0.1,
                outbox=queue,
                max_batch=3,
                sender_factory=_BatchSender,
            )
            worker.start()
            try:
                worker.notify(live_id)
                self.assertTrue(wait_until(lambda: len(_BatchSender.calls) >= 1))
                self.assertTrue(wait_until(lambda: queue.count() == 1))
                self.assertIsNotNone(worker.status()["last_success_age_s"])
            finally:
                worker.stop()
            sent_ids = [row_id for row_id, _ in _BatchSender.calls[-1]["rows"]]
            self.assertEqual(sent_ids, [live_id, old_ids[0], old_ids[1]])
            self.assertEqual([row_id for row_id, _ in queue.peek_batch(10)], [old_ids[2]])
            queue.close()

    def test_wifi_fallback_excludes_stale_disconnected_neighbors(self) -> None:
        output = "\n".join([
            "192.168.4.2 dev wlan0 lladdr aa:bb:cc:dd:ee:01 REACHABLE",
            "192.168.4.3 dev wlan0 lladdr aa:bb:cc:dd:ee:02 STALE",
            "192.168.4.4 dev wlan0 lladdr aa:bb:cc:dd:ee:03 FAILED",
        ])
        self.assertEqual(_parse_active_ip_neigh(output), ["aa:bb:cc:dd:ee:01"])

    def test_slow_http_dispatch_does_not_block_submission(self) -> None:
        _SlowSender.sent = []
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = OutboundDispatcher(
                url="https://example.invalid",
                timeout_s=1.0,
                sqlite_path=str(Path(temp_dir) / "buffer.sqlite3"),
                table="telemetry",
                max_rows=100,
                sender_factory=_SlowSender,
            )
            worker.start()
            try:
                started = time.monotonic()
                for item_id in range(5):
                    self.assertTrue(worker.submit({"id": item_id}))
                self.assertLess(time.monotonic() - started, 0.03)
                self.assertTrue(wait_until(lambda: len(_SlowSender.sent) == 5))
            finally:
                worker.stop()

    def test_weight_sampling_does_not_block_snapshot_reads(self) -> None:
        worker = WeightSampler(_SlowWeightReader())
        worker.start()
        try:
            started = time.monotonic()
            worker.latest()
            self.assertLess(time.monotonic() - started, 0.03)
            self.assertTrue(wait_until(lambda: worker.latest().weight is not None))
        finally:
            worker.stop()

    def test_wifi_error_clears_previous_clients(self) -> None:
        answers = iter([(["aa:bb:cc:dd:ee:ff"], None), ([], "scan failed")])
        worker = WifiMonitor(lambda: next(answers), interval_s=0.02, max_snapshot_age_s=1.0)
        worker.start()
        try:
            self.assertTrue(wait_until(lambda: worker.latest()[0] == ["aa:bb:cc:dd:ee:ff"]))
            self.assertTrue(wait_until(lambda: worker.latest()[1] == "scan failed"))
            self.assertEqual(worker.latest()[0], [])
        finally:
            worker.stop()

    def test_wifi_snapshot_expires_instead_of_reporting_old_clients(self) -> None:
        worker = WifiMonitor(lambda: (["aa:bb:cc:dd:ee:ff"], None), interval_s=10.0, max_snapshot_age_s=0.05)
        worker.start()
        try:
            self.assertTrue(wait_until(lambda: bool(worker.latest()[0])))
            time.sleep(0.12)
            clients, error = worker.latest()
            self.assertEqual(clients, [])
            self.assertIn("stale", error.lower())
        finally:
            worker.stop()

    def test_flusher_dead_letters_bad_head_and_continues(self) -> None:
        _RejectFirstSender.sent = []
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "buffer.sqlite3"
            queue = SqliteQueue(sqlite_path=str(db_path), table="telemetry", max_rows=10)
            queue.put({"id": 1})
            queue.put({"id": 2})
            worker = BufferFlusher(
                url="https://example.invalid",
                timeout_s=0.1,
                sqlite_path=str(db_path),
                table="telemetry",
                max_rows=10,
                max_batch=10,
                telemetry=True,
                sender_factory=_RejectFirstSender,
            )
            with patch("host_monitor.workers.is_permanent_http_error", return_value=True):
                worker.start()
                try:
                    self.assertTrue(wait_until(lambda: queue.count() == 0))
                finally:
                    worker.stop()
            self.assertEqual(_RejectFirstSender.sent, [2])
            dead = queue._conn.execute("SELECT queue_id FROM telemetry_dead_letter ORDER BY id").fetchall()
            self.assertEqual(dead, [(1,)])
            queue.close()


if __name__ == "__main__":
    unittest.main()
