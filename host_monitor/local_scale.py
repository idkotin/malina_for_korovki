"""One immutable measurement for LAN, telemetry and the enclosure display."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def atomic_json(path: str, value: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, target)
    if os.name != 'nt':
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def measurement(sampler, device_id: str) -> dict:
    snap = sampler.snapshot()
    value = snap['weight'].weight
    valid = (value is not None and math.isfinite(value) and snap['age_s'] is not None
             and snap['age_s'] <= 3 and snap['calibrated'])
    return dict(version=1, deviceId=device_id, packetId=snap['packet_id'],
                timestampMs=snap['timestamp_ms'], weightKg=value if valid else None,
                valid=valid, calibrationId=snap['calibration_id'], ageMs=None if
                snap['age_s'] is None else int(snap['age_s'] * 1000))


class LocalWeightServer:
    def __init__(self, sampler, device_id: str, listen: str, port: int):
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def do_GET(self):
                if self.path != '/v1/weight':
                    self.send_error(404)
                    return
                body = json.dumps(measurement(sampler, device_id), allow_nan=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((listen, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='local-weight-http')

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
