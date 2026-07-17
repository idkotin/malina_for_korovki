from __future__ import annotations

import logging
import queue
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from host_monitor.buffer import SqliteQueue
from host_monitor.models import Weight
from host_monitor.sender import Sender, is_permanent_http_error


log = logging.getLogger("host_monitor.workers")


class WeightSampler:
    """Continuously reads the scale while the telemetry clock stays independent."""

    def __init__(self, reader: Any):
        self._reader = reader
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._weight = Weight(weight=None)
        self._updated_monotonic: float | None = None
        self._read_duration_s: float | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="weight-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                value = self._reader.read_weight()
                error = None
            except Exception as exc:
                value = Weight(weight=None)
                error = str(exc)
                log.warning("background weight read failed: %s", exc)
            finished = time.monotonic()
            with self._lock:
                self._weight = value
                self._updated_monotonic = finished
                self._read_duration_s = finished - started
                self._last_error = error

    def latest(self) -> Weight:
        with self._lock:
            return self._weight.model_copy(deep=True)

    def status(self) -> dict[str, Any]:
        with self._lock:
            age = None if self._updated_monotonic is None else max(0.0, time.monotonic() - self._updated_monotonic)
            return {
                "age_s": age,
                "read_duration_s": self._read_duration_s,
                "last_error": self._last_error,
                "running": bool(self._thread and self._thread.is_alive()),
            }


class WifiMonitor:
    """Polls AP clients outside the telemetry loop and expires stale snapshots."""

    def __init__(
        self,
        scan: Callable[[], tuple[list[str], str | None]],
        *,
        interval_s: float,
        max_snapshot_age_s: float,
    ):
        self._scan = scan
        self._interval_s = max(0.1, float(interval_s))
        self._max_snapshot_age_s = max(0.1, float(max_snapshot_age_s))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._clients: list[str] = []
        self._updated_monotonic: float | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wifi-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                clients, error = self._scan()
            except Exception as exc:
                clients, error = [], str(exc)
            with self._lock:
                # A failed scan must never preserve an old non-empty list.
                self._clients = list(clients) if not error else []
                self._updated_monotonic = time.monotonic()
                self._last_error = error
            self._stop.wait(self._interval_s)

    def latest(self) -> tuple[list[str], str | None]:
        with self._lock:
            age = None if self._updated_monotonic is None else time.monotonic() - self._updated_monotonic
            if age is None or age > self._max_snapshot_age_s:
                return [], self._last_error or "Wi-Fi client snapshot is stale"
            return list(self._clients), self._last_error

    def status(self) -> dict[str, Any]:
        clients, error = self.latest()
        with self._lock:
            age = None if self._updated_monotonic is None else max(0.0, time.monotonic() - self._updated_monotonic)
        return {"clients": len(clients), "age_s": age, "last_error": error}


class OutboundDispatcher:
    """Sends fresh payloads without blocking the telemetry scheduler."""

    def __init__(
        self,
        *,
        url: str,
        timeout_s: float,
        sqlite_path: str,
        table: str,
        max_rows: int,
        queue_size: int = 100,
        sender_factory: Callable[[str, float], Sender] = Sender,
    ):
        self._url = url
        self._timeout_s = timeout_s
        self._sqlite_path = sqlite_path
        self._table = table
        self._max_rows = max_rows
        self._sender_factory = sender_factory
        # Open worker-owned SQLite connections sequentially during startup;
        # creating several WAL connections concurrently can race on schema init.
        self._buffered = SqliteQueue(sqlite_path=sqlite_path, table=table, max_rows=max_rows)
        self._pending: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, queue_size))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_error: str | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"{self._table}-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(3.0, self._timeout_s + 1.0))

    def submit(self, payload: dict[str, Any]) -> bool:
        try:
            self._pending.put_nowait(payload)
            return True
        except queue.Full:
            return False

    def _run(self) -> None:
        sender = self._sender_factory(self._url, self._timeout_s)
        try:
            while not self._stop.is_set() or not self._pending.empty():
                try:
                    payload = self._pending.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    sender.send_one(payload)
                    error = None
                except Exception as exc:
                    self._buffered.put(payload)
                    error = str(exc)
                finally:
                    self._pending.task_done()
                with self._lock:
                    self._last_error = error
        finally:
            sender.close()
            self._buffered.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"pending": self._pending.qsize(), "last_error": self._last_error}


class TelemetryOutboxWorker:
    """Persist-first, fresh-priority telemetry batch sender.

    One notification produces at most one HTTP request. This ties healthy
    network cadence to send.interval_s and prevents a large backlog from
    turning into an unbounded request flood.
    """

    def __init__(
        self,
        *,
        device_id: str,
        url: str,
        timeout_s: float,
        outbox: SqliteQueue,
        max_batch: int,
        sender_factory: Callable[[str, float], Sender] = Sender,
    ):
        self._device_id = device_id
        self._url = url
        self._timeout_s = timeout_s
        self._outbox = outbox
        self._max_batch = max(1, min(50, int(max_batch)))
        self._sender_factory = sender_factory
        self._stream_id = outbox.get_or_create_stream_id()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest_live_id: int | None = None
        self._backoff_s = 1.0
        self._last_error: str | None = None
        self._last_latency_s: float | None = None
        self._sent = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telemetry-outbox", daemon=True)
        self._thread.start()
        # Recover an existing outbox even before the first new sample arrives.
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=max(3.0, self._timeout_s + 1.0))

    def notify(self, live_id: int) -> None:
        with self._lock:
            self._latest_live_id = int(live_id)
        self._wake.set()

    def _run(self) -> None:
        sender = self._sender_factory(self._url, self._timeout_s)
        try:
            while not self._stop.is_set():
                self._wake.wait(timeout=1.0)
                if self._stop.is_set():
                    break
                if not self._wake.is_set():
                    continue
                self._wake.clear()
                with self._lock:
                    live_id = self._latest_live_id
                rows = self._outbox.peek_fresh_first(live_id, self._max_batch)
                if not rows:
                    self._set_status(backoff_s=1.0, error=None, latency_s=None)
                    continue
                actual_live_id = rows[0][0]
                started = time.monotonic()
                try:
                    acked_ids = sender.send_telemetry_batch(
                        device_id=self._device_id,
                        stream_id=self._stream_id,
                        live_packet_id=actual_live_id,
                        rows=rows,
                    )
                    self._outbox.delete_ids(acked_ids)
                    latency_s = time.monotonic() - started
                    with self._lock:
                        self._sent += len(acked_ids)
                    self._set_status(backoff_s=1.0, error=None, latency_s=latency_s)
                    log.info(
                        "telemetry batch accepted stream=%s live_id=%s rows=%s acked=%s latency_s=%.3f remaining=%s",
                        self._stream_id,
                        actual_live_id,
                        len(rows),
                        len(acked_ids),
                        latency_s,
                        self._outbox.count(),
                    )
                except Exception as exc:
                    delay = self._backoff_s
                    next_delay = min(60.0, delay * 2.0)
                    self._set_status(backoff_s=next_delay, error=str(exc), latency_s=time.monotonic() - started)
                    log.warning(
                        "telemetry batch failed stream=%s live_id=%s rows=%s retry_in_s=%.1f error=%s",
                        self._stream_id,
                        actual_live_id,
                        len(rows),
                        delay,
                        exc,
                    )
                    if self._stop.wait(delay * random.uniform(0.8, 1.2)):
                        break
                    self._wake.set()
        finally:
            sender.close()

    def _set_status(self, *, backoff_s: float, error: str | None, latency_s: float | None) -> None:
        with self._lock:
            self._backoff_s = backoff_s
            self._last_error = error
            self._last_latency_s = latency_s

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stream_id": self._stream_id,
                "latest_live_id": self._latest_live_id,
                "backoff_s": self._backoff_s,
                "last_error": self._last_error,
                "last_latency_s": self._last_latency_s,
                "sent": self._sent,
            }


class BufferFlusher:
    """Drains one SQLite stream in a dedicated thread with retry backoff."""

    def __init__(
        self,
        *,
        url: str,
        timeout_s: float,
        sqlite_path: str,
        table: str,
        max_rows: int,
        max_batch: int,
        telemetry: bool,
        sender_factory: Callable[[str, float], Sender] = Sender,
    ):
        self._url = url
        self._timeout_s = timeout_s
        self._sqlite_path = sqlite_path
        self._table = table
        self._max_rows = max_rows
        self._max_batch = max(1, max_batch)
        self._telemetry = telemetry
        self._sender_factory = sender_factory
        self._buffered = SqliteQueue(sqlite_path=sqlite_path, table=table, max_rows=max_rows)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._backoff_s = 1.0
        self._last_error: str | None = None
        self._sent = 0
        self._dead_lettered = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"{self._table}-flusher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(3.0, self._timeout_s + 1.0))

    def _send(self, sender: Sender, payload_json: str) -> None:
        if self._telemetry:
            sender.send_buffered_telemetry_one(payload_json)
        else:
            sender.send_json_string_one(payload_json)

    def _run(self) -> None:
        sender = self._sender_factory(self._url, self._timeout_s)
        try:
            while not self._stop.is_set():
                batch = self._buffered.peek_batch(self._max_batch)
                if not batch:
                    self._set_status(backoff_s=1.0, error=None)
                    self._stop.wait(1.0)
                    continue
                failed = False
                for row_id, payload_json in batch:
                    if self._stop.is_set():
                        break
                    try:
                        self._send(sender, payload_json)
                    except Exception as exc:
                        if is_permanent_http_error(exc):
                            reason = f"HTTP {exc.response.status_code}: {exc}"
                            if self._buffered.move_to_dead_letter(row_id, reason):
                                with self._lock:
                                    self._dead_lettered += 1
                                log.error("%s row %s moved to dead letter: %s", self._table, row_id, reason)
                            continue
                        failed = True
                        self._set_status(backoff_s=min(self._backoff_s * 2, 60.0), error=str(exc))
                        break
                    self._buffered.delete_ids([row_id])
                    with self._lock:
                        self._sent += 1
                if failed:
                    self._stop.wait(self._backoff_s)
                else:
                    self._set_status(backoff_s=1.0, error=None)
        finally:
            sender.close()
            self._buffered.close()

    def _set_status(self, *, backoff_s: float, error: str | None) -> None:
        with self._lock:
            self._backoff_s = backoff_s
            self._last_error = error

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "backoff_s": self._backoff_s,
                "last_error": self._last_error,
                "sent": self._sent,
                "dead_lettered": self._dead_lettered,
            }
