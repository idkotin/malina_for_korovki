#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="/var/backups/korovki-remote-access"
INTERFACE_NAME="awg0"
PURGE_CONFIG=0
PURGE_REVERSE_SSH=0

usage() {
  cat <<'EOF'
Usage:
  uninstall_remote_access.sh [options]

Options:
  --interface NAME       Interface name, default: awg0
  --purge-config         Remove /etc/amnezia/amneziawg/<interface>.conf
  --purge-reverse-ssh    Remove reverse SSH service and its config files
  --help                 Show this help
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
      --interface)
        [[ $# -ge 2 ]] || die "Missing value after --interface"
        INTERFACE_NAME="$2"
        shift 2
        ;;
      --purge-config)
        PURGE_CONFIG=1
        shift
        ;;
      --purge-reverse-ssh)
        PURGE_REVERSE_SSH=1
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

backup_dir_create() {
  local ts
  ts="$(date +%Y%m%d-%H%M%S)"
  BACKUP_DIR="${BACKUP_ROOT}/${ts}-uninstall"
  install -d -m 700 "${BACKUP_DIR}"
}

backup_if_exists() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    cp -a "${path}" "${BACKUP_DIR}/"
    log "Backed up ${path} -> ${BACKUP_DIR}"
  fi
}

disable_service_if_present() {
  local name="$1"
  systemctl disable --now "${name}" 2>/dev/null || systemctl stop "${name}" 2>/dev/null || true
}

remove_file_if_present() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    rm -f "${path}"
    log "Removed ${path}"
  fi
}

main() {
  parse_args "$@"
  require_root

  local awg_service="amneziawg-client@${INTERFACE_NAME}.service"
  local awg_config="/etc/amnezia/amneziawg/${INTERFACE_NAME}.conf"
  local awg_unit="/etc/systemd/system/amneziawg-client@.service"
  local reverse_service="reverse-ssh.service"
  local reverse_unit="/etc/systemd/system/reverse-ssh.service"
  local reverse_env="/etc/korovki/remote_access/reverse-ssh.env"
  local reverse_key="/etc/korovki/remote_access/id_ed25519"
  local reverse_known_hosts="/etc/korovki/remote_access/known_hosts"
  local reverse_runner="/usr/local/lib/korovki-remote-access/run_reverse_ssh.sh"

  backup_dir_create

  backup_if_exists "${awg_unit}"
  backup_if_exists "${awg_config}"
  backup_if_exists "${reverse_unit}"
  backup_if_exists "${reverse_env}"
  backup_if_exists "${reverse_key}"
  backup_if_exists "${reverse_known_hosts}"
  backup_if_exists "${reverse_runner}"

  disable_service_if_present "${awg_service}"
  remove_file_if_present "${awg_unit}"
  if [[ "${PURGE_CONFIG}" -eq 1 ]]; then
    remove_file_if_present "${awg_config}"
  fi

  if [[ "${PURGE_REVERSE_SSH}" -eq 1 ]]; then
    disable_service_if_present "${reverse_service}"
    remove_file_if_present "${reverse_unit}"
    remove_file_if_present "${reverse_env}"
    remove_file_if_present "${reverse_key}"
    remove_file_if_present "${reverse_known_hosts}"
    remove_file_if_present "${reverse_runner}"
  fi

  systemctl daemon-reload

  cat <<EOF
[DONE] Remote access components were removed.
Backup: ${BACKUP_DIR}

Notes:
  - The main host-monitor project was not touched.
  - The amneziawg package was not uninstalled.
  - Use --purge-config and/or --purge-reverse-ssh if you need a deeper cleanup.
EOF
}

main "$@"
