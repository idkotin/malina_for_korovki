# Server audit template before AmneziaWG setup

Do not commit real server IPs, passwords, private keys, exported VPN configs, or production-only values.

## Inputs

```text
Server IP: <SERVER_IP>
SSH port: <SSH_PORT>
SSH user: <SSH_USER>
Recommended AmneziaWG UDP port: <AMNEZIAWG_PORT>
```

## Read-only audit commands

Run these before any installation or firewall change:

```bash
uname -a
cat /etc/os-release
ip a
ip route
ss -tulpen
systemctl status ssh --no-pager || systemctl status sshd --no-pager
command -v nginx >/dev/null 2>&1 && systemctl status nginx --no-pager || true
command -v nginx >/dev/null 2>&1 && nginx -t || true
command -v docker >/dev/null 2>&1 && systemctl status docker --no-pager || true
command -v docker >/dev/null 2>&1 && docker ps || true
command -v ufw >/dev/null 2>&1 && ufw status verbose || true
iptables-save
command -v nft >/dev/null 2>&1 && nft list ruleset || true
ls -la /etc/systemd/system
```

## Backup before changes

Create a dated backup directory:

```bash
TS=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/root/remote-access-backup-${TS}"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
```

Save the important state:

```bash
[ -d /etc/nginx ] && cp -a /etc/nginx "$BACKUP_DIR/nginx"
[ -f /etc/ssh/sshd_config ] && cp -a /etc/ssh/sshd_config "$BACKUP_DIR/sshd_config"
[ -d /etc/ssh/sshd_config.d ] && cp -a /etc/ssh/sshd_config.d "$BACKUP_DIR/sshd_config.d"
iptables-save > "$BACKUP_DIR/iptables-save.txt" 2>&1 || true
nft list ruleset > "$BACKUP_DIR/nft-ruleset.txt" 2>&1 || true
ufw status verbose > "$BACKUP_DIR/ufw-status.txt" 2>&1 || true
systemctl list-units --type=service --all > "$BACKUP_DIR/systemd-services.txt" 2>&1 || true
ss -tulpen > "$BACKUP_DIR/open-ports-ss-tulpen.txt" 2>&1 || true
docker ps > "$BACKUP_DIR/docker-ps.txt" 2>&1 || true
ip a > "$BACKUP_DIR/ip-a.txt" 2>&1 || true
ip route > "$BACKUP_DIR/ip-route.txt" 2>&1 || true
nginx -t > "$BACKUP_DIR/nginx-test.txt" 2>&1 || true
ls -la /etc/systemd/system > "$BACKUP_DIR/systemd-dir.txt" 2>&1 || true
```

## Port decision

Prefer a dedicated UDP port for AmneziaWG, for example:

```text
<AMNEZIAWG_PORT>/udp
```

Avoid reusing website ports such as `80/tcp` and `443/tcp`.

## Report checklist

- OS and version.
- Public interface and route.
- Busy TCP/UDP ports.
- What owns `80/tcp`.
- What owns `443/tcp`.
- Whether `<AMNEZIAWG_PORT>/udp` is free.
- Whether Docker exists.
- Whether Nginx exists and config test passes.
- Whether firewall rules could block the chosen UDP port.
- Whether Amnezia installation can touch existing site/backend services.
