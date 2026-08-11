#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RULE_SOURCE="${SCRIPT_DIR}/99-simcom-ppp.rules"
WATCHDOG_SOURCE="${SCRIPT_DIR}/simcom-ppp-watchdog"
WATCHDOG_SERVICE_SOURCE="${SCRIPT_DIR}/simcom-ppp-watchdog.service"
WATCHDOG_TIMER_SOURCE="${SCRIPT_DIR}/simcom-ppp-watchdog.timer"
WATCHDOG_USB_CYCLE_SOURCE="${SCRIPT_DIR}/simcom-ppp-watchdog-usb-cycle.conf"

RULE_DEST="/etc/udev/rules.d/99-simcom-ppp.rules"
WATCHDOG_DEST="/usr/local/sbin/simcom-ppp-watchdog"
WATCHDOG_SERVICE_DEST="/etc/systemd/system/simcom-ppp-watchdog.service"
WATCHDOG_TIMER_DEST="/etc/systemd/system/simcom-ppp-watchdog.timer"
WATCHDOG_USB_CYCLE_DEST="/etc/systemd/system/simcom-ppp-watchdog.service.d/usb-power-cycle.conf"
BACKUP_ROOT="/var/backups/korovki-simcom-ppp"

PEER_NAME="megafon"
RESTART_LTE=0
INSTALL_WATCHDOG=1
ENABLE_USB_POWER_CYCLE=0
BACKUP_DIR=""

usage() {
  cat <<'EOF'
Usage: install-simcom-ppp.sh [options]

Options:
  --peer-name NAME    PPP peer file name, default: megafon
  --restart-lte       Restart lte.service and verify ppp0 after installation
  --no-watchdog       Install only the stable udev alias and peer change
  --enable-usb-power-cycle
                      After 5 minutes with no modem, cycle Pi 4 external USB
  --help              Show this help
EOF
}

log() {
  printf '[INFO] %s\n' "$*"
}

die() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script as root."
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --peer-name)
        [[ $# -ge 2 ]] || die "Missing value after --peer-name"
        PEER_NAME="$2"
        shift 2
        ;;
      --restart-lte)
        RESTART_LTE=1
        shift
        ;;
      --no-watchdog)
        INSTALL_WATCHDOG=0
        shift
        ;;
      --enable-usb-power-cycle)
        ENABLE_USB_POWER_CYCLE=1
        shift
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done
}

validate_peer_name() {
  [[ "${PEER_NAME}" =~ ^[a-zA-Z0-9_.+-]+$ ]] || die "Invalid peer name: ${PEER_NAME}"
  if [[ "${INSTALL_WATCHDOG}" -eq 0 && "${ENABLE_USB_POWER_CYCLE}" -eq 1 ]]; then
    die "--enable-usb-power-cycle cannot be combined with --no-watchdog"
  fi
}

backup_if_exists() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    cp -a -- "${path}" "${BACKUP_DIR}/"
    log "Backed up ${path}"
  fi
}

require_sources() {
  local path
  for path in \
    "${RULE_SOURCE}" \
    "${WATCHDOG_SOURCE}" \
    "${WATCHDOG_SERVICE_SOURCE}" \
    "${WATCHDOG_TIMER_SOURCE}" \
    "${WATCHDOG_USB_CYCLE_SOURCE}"; do
    [[ -f "${path}" ]] || die "Required source file is missing: ${path}"
  done
}

install_alias_rule() {
  install -D -m 644 "${RULE_SOURCE}" "${RULE_DEST}"
  udevadm control --reload-rules
  udevadm trigger --subsystem-match=tty --action=add
  udevadm settle --timeout=15

  [[ -e /dev/simcom-ppp ]] || die "/dev/simcom-ppp was not created. Do not change the PPP peer."

  local properties
  properties="$(udevadm info --query=property --name=/dev/simcom-ppp)"
  grep -q '^ID_VENDOR_ID=1e0e$' <<<"${properties}" || die "Alias vendor verification failed"
  grep -q '^ID_MODEL_ID=9001$' <<<"${properties}" || die "Alias product verification failed"
  grep -q '^ID_USB_INTERFACE_NUM=03$' <<<"${properties}" || die "Alias interface verification failed"
  log "/dev/simcom-ppp -> $(readlink -f /dev/simcom-ppp)"
}

update_peer_file() {
  local peer_path="/etc/ppp/peers/${PEER_NAME}"
  [[ -f "${peer_path}" ]] || die "PPP peer file not found: ${peer_path}"

  if grep -Eq '^[[:space:]]*/dev/simcom-ppp([[:space:]]|$)' "${peer_path}"; then
    log "${peer_path} already uses /dev/simcom-ppp"
    return 0
  fi

  grep -Eq '^[[:space:]]*/dev/ttyUSB[0-9]+([[:space:]]|$)' "${peer_path}" \
    || die "No standalone /dev/ttyUSBN device line found in ${peer_path}"

  backup_if_exists "${peer_path}"
  sed -Ei '0,/^[[:space:]]*\/dev\/ttyUSB[0-9]+([[:space:]]|$)/s#^([[:space:]]*)/dev/ttyUSB[0-9]+#\1/dev/simcom-ppp#' "${peer_path}"
  grep -Eq '^[[:space:]]*/dev/simcom-ppp([[:space:]]|$)' "${peer_path}" \
    || die "Failed to update ${peer_path}"
  log "Updated ${peer_path} to use /dev/simcom-ppp"
}

install_watchdog() {
  [[ "${INSTALL_WATCHDOG}" -eq 1 ]] || return 0
  install -D -m 755 "${WATCHDOG_SOURCE}" "${WATCHDOG_DEST}"
  install -D -m 644 "${WATCHDOG_SERVICE_SOURCE}" "${WATCHDOG_SERVICE_DEST}"
  install -D -m 644 "${WATCHDOG_TIMER_SOURCE}" "${WATCHDOG_TIMER_DEST}"
  if [[ "${ENABLE_USB_POWER_CYCLE}" -eq 1 ]]; then
    command -v uhubctl >/dev/null 2>&1 || die "uhubctl is required for --enable-usb-power-cycle"
    [[ "$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)" == *"Raspberry Pi 4 Model B"* ]] \
      || die "USB power-cycle recovery is validated only for Raspberry Pi 4 Model B"
    uhubctl -l 1-1 >/dev/null 2>&1 || die "uhubctl cannot access Raspberry Pi USB hub 1-1"
    install -D -m 644 "${WATCHDOG_USB_CYCLE_SOURCE}" "${WATCHDOG_USB_CYCLE_DEST}"
    log "Enabled guarded Raspberry Pi 4 USB power-cycle recovery"
  fi
  systemctl daemon-reload
  systemctl enable --now simcom-ppp-watchdog.timer
  log "Enabled simcom-ppp-watchdog.timer"
}

restart_and_verify_lte() {
  [[ "${RESTART_LTE}" -eq 1 ]] || return 0
  systemctl restart lte.service

  local attempt
  for attempt in $(seq 1 30); do
    if [[ -e /sys/class/net/ppp0 ]]; then
      log "ppp0 restored after ${attempt}s"
      return 0
    fi
    sleep 1
  done

  systemctl --no-pager --full status lte.service || true
  journalctl -u lte.service -n 80 --no-pager || true
  die "lte.service restarted, but ppp0 did not appear within 30 seconds"
}

main() {
  parse_args "$@"
  require_root
  validate_peer_name
  require_sources

  local peer_path="/etc/ppp/peers/${PEER_NAME}"
  [[ -f "${peer_path}" ]] || die "PPP peer file not found: ${peer_path}"

  BACKUP_DIR="${BACKUP_ROOT}/$(date +%Y%m%d-%H%M%S)"
  install -d -m 700 "${BACKUP_DIR}"
  backup_if_exists "${RULE_DEST}"
  backup_if_exists "${WATCHDOG_DEST}"
  backup_if_exists "${WATCHDOG_SERVICE_DEST}"
  backup_if_exists "${WATCHDOG_TIMER_DEST}"
  backup_if_exists "${WATCHDOG_USB_CYCLE_DEST}"

  install_alias_rule
  update_peer_file
  install_watchdog
  restart_and_verify_lte

  cat <<EOF
[DONE] Stable SIM7600 PPP device installed.
Alias: /dev/simcom-ppp -> $(readlink -f /dev/simcom-ppp)
Peer: /etc/ppp/peers/${PEER_NAME}
Backup: ${BACKUP_DIR}
Watchdog: $([[ "${INSTALL_WATCHDOG}" -eq 1 ]] && printf 'enabled' || printf 'not installed')
USB power-cycle recovery: $([[ "${ENABLE_USB_POWER_CYCLE}" -eq 1 ]] && printf 'enabled' || printf 'not changed')

Checks:
  udevadm info --query=property --name=/dev/simcom-ppp
  systemctl status lte.service simcom-ppp-watchdog.timer --no-pager
  ip -br address show ppp0
EOF
}

main "$@"
