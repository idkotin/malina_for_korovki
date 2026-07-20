# Manual reboot by SMS

The feature is disabled by default and never reboots the Raspberry Pi
automatically. It accepts only an exact `/reboot` SMS from one configured phone
number.

Keep the real phone number only in `/opt/host-monitor/config.yaml` on the
Raspberry Pi:

```yaml
sms_reboot:
  enabled: true
  allowed_number: "+7XXXXXXXXXX"
  command: "/reboot"
```

`host-monitor.service` must have permission to run `/usr/bin/systemctl reboot`.
The production unit currently runs as root, so no sudo rule is required.

After deploying the code and editing the live config, validate without sending
the command:

```bash
cd /opt/host-monitor
./.venv/bin/python -c 'from host_monitor.config import load_config; c=load_config("config.yaml"); print(c.sms_reboot)'
sudo systemctl restart host-monitor.service
sudo journalctl -u host-monitor.service -n 50 --no-pager
```

An unauthorized sender or any text other than the exact command is handled as
a normal SMS event. A successful authorized command is consumed locally and
requests a clean systemd reboot.
