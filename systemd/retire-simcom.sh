#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo bash ./systemd/retire-simcom.sh" >&2
  exit 1
fi

backup_dir="/var/backups/korovki-retired-simcom/$(date +%Y%m%d-%H%M%S)"
install -d -m 700 "${backup_dir}"

units=(
  lte.service
  sim7600-ppp.service
  simcom-ppp-watchdog.timer
  simcom-ppp-watchdog.service
  simcom-diagnostics.timer
  simcom-diagnostics.service
)

# These units belong to the removed SIMCom/PPP path. None is used by the
# replacement LTE Wi-Fi router or by the M100 on /dev/serial0.
for unit in "${units[@]}"; do
  systemctl disable --now "${unit}" >/dev/null 2>&1 || true
  systemctl reset-failed "${unit}" >/dev/null 2>&1 || true
done

paths=(
  /etc/udev/rules.d/99-simcom-ppp.rules
  /etc/systemd/system/simcom-ppp-watchdog.service
  /etc/systemd/system/simcom-ppp-watchdog.timer
  /etc/systemd/system/simcom-ppp-watchdog.service.d/usb-power-cycle.conf
  /etc/systemd/system/simcom-diagnostics.service
  /etc/systemd/system/simcom-diagnostics.timer
  /usr/local/sbin/simcom-ppp-watchdog
  /usr/local/sbin/simcom-diagnostics
  /usr/local/sbin/capture-simcom-incident
  /etc/ppp/ip-up.d/90-simcom-ppp-transition
  /etc/ppp/ip-down.d/90-simcom-ppp-transition
)

for path in "${paths[@]}"; do
  if [[ -e "${path}" || -L "${path}" ]]; then
    destination="${backup_dir}${path}"
    install -d -m 700 "$(dirname "${destination}")"
    cp -a "${path}" "${destination}"
    rm -f -- "${path}"
  fi
done

rmdir /etc/systemd/system/simcom-ppp-watchdog.service.d 2>/dev/null || true
systemctl daemon-reload
udevadm control --reload-rules

echo "SIMCom runtime retired. Backup: ${backup_dir}"
echo "The script did not reset or power-cycle USB; the LTE Wi-Fi router remains untouched."
