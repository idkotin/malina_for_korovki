#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="/var/backups/korovki-simcom-diagnostics/$(date +%Y%m%d-%H%M%S)"

declare -A FILES=(
  ["${SCRIPT_DIR}/99-korovki-journal.conf"]="/etc/systemd/journald.conf.d/99-korovki-diagnostics.conf"
  ["${SCRIPT_DIR}/simcom-diagnostics"]="/usr/local/sbin/simcom-diagnostics"
  ["${SCRIPT_DIR}/capture-simcom-incident"]="/usr/local/sbin/capture-simcom-incident"
  ["${SCRIPT_DIR}/simcom-diagnostics.service"]="/etc/systemd/system/simcom-diagnostics.service"
  ["${SCRIPT_DIR}/simcom-diagnostics.timer"]="/etc/systemd/system/simcom-diagnostics.timer"
  ["${SCRIPT_DIR}/90-simcom-ppp-transition"]="/etc/ppp/ip-up.d/90-simcom-ppp-transition"
  ["${SCRIPT_DIR}/simcom-ppp-watchdog"]="/usr/local/sbin/simcom-ppp-watchdog"
)

[[ "${EUID}" -eq 0 ]] || { echo "Run this installer as root" >&2; exit 1; }

for source in "${!FILES[@]}"; do
  [[ -f "${source}" ]] || { echo "Missing source: ${source}" >&2; exit 1; }
done

install -d -m 700 "${BACKUP_DIR}"
for destination in "${FILES[@]}" /etc/ppp/ip-down.d/90-simcom-ppp-transition; do
  if [[ -e "${destination}" ]]; then
    cp -a -- "${destination}" "${BACKUP_DIR}/$(printf '%s' "${destination}" | tr '/' '_')"
  fi
done

install -D -m 644 "${SCRIPT_DIR}/99-korovki-journal.conf" \
  /etc/systemd/journald.conf.d/99-korovki-diagnostics.conf
install -D -m 755 "${SCRIPT_DIR}/simcom-diagnostics" /usr/local/sbin/simcom-diagnostics
install -D -m 755 "${SCRIPT_DIR}/capture-simcom-incident" /usr/local/sbin/capture-simcom-incident
install -D -m 644 "${SCRIPT_DIR}/simcom-diagnostics.service" /etc/systemd/system/simcom-diagnostics.service
install -D -m 644 "${SCRIPT_DIR}/simcom-diagnostics.timer" /etc/systemd/system/simcom-diagnostics.timer
install -D -m 755 "${SCRIPT_DIR}/90-simcom-ppp-transition" /etc/ppp/ip-up.d/90-simcom-ppp-transition
install -D -m 755 "${SCRIPT_DIR}/90-simcom-ppp-transition" /etc/ppp/ip-down.d/90-simcom-ppp-transition
install -D -m 755 "${SCRIPT_DIR}/simcom-ppp-watchdog" /usr/local/sbin/simcom-ppp-watchdog

install -d -m 2755 -o root -g systemd-journal /var/log/journal
install -d -m 750 /var/log/simcom-incidents

systemctl daemon-reload
systemctl restart systemd-journald.service
journalctl --flush
systemctl enable --now simcom-diagnostics.timer
systemctl start simcom-diagnostics.service
/usr/local/sbin/capture-simcom-incident installation >/dev/null

cat <<EOF
[DONE] Persistent SIMCOM diagnostics installed.
Backup: ${BACKUP_DIR}
Journal limit: 256 MiB, retention: 14 days, keep-free: 512 MiB
Periodic snapshot: every minute
Incident bundles: /var/log/simcom-incidents (newest 30 kept)
EOF
