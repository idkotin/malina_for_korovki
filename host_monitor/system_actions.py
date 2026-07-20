from __future__ import annotations

import subprocess


def request_system_reboot() -> None:
    """Request a clean systemd reboot without invoking a shell."""

    subprocess.Popen(
        ["/usr/bin/systemctl", "reboot"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
