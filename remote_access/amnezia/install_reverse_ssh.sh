#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SOURCE="${SCRIPT_DIR}/systemd/reverse-ssh.service"
RUNNER_SOURCE="${SCRIPT_DIR}/run_reverse_ssh.sh"
BACKUP_ROOT="/var/backups/korovki-remote-access"

SERVER_HOST=""
SERVER_PORT="22"
SERVER_USER="pi-tunnel"
IDENTITY_SOURCE=""
KNOWN_HOSTS_SOURCE=""
REMOTE_SSH_PORT="2222"
ENABLE_VNC=0
REMOTE_VNC_PORT="5901"
LOCAL_VNC_PORT="5900"
INSTALL_PACKAGES=0
SKIP_START=0

ENV_DEST="/etc/korovki/remote_access/reverse-ssh.env"
IDENTITY_DEST="/etc/korovki/remote_access/id_ed25519"
KNOWN_HOSTS_DEST="/etc/korovki/remote_access/known_hosts"
RUNNER_DEST="/usr/local/lib/korovki-remote-access/run_reverse_ssh.sh"
UNIT_DEST="/etc/systemd/system/reverse-ssh.service"

usage() {
  cat <<'EOF'
Usage:
  install_reverse_ssh.sh --server-host HOST --identity-file PATH --known-hosts PATH [options]

Options:
  --server-host HOST        Public server hostname or IP
  --server-port PORT        SSH port on the server, default: 22
  --server-user USER        Server user for the tunnel, default: pi-tunnel
  --identity-file PATH      Private key used for the tunnel
  --known-hosts PATH        known_hosts file with server fingerprint
  --remote-ssh-port PORT    Remote localhost SSH port, default: 2222
  --enable-vnc              Also forward VNC
  --remote-vnc-port PORT    Remote localhost VNC port, default: 5901
  --local-vnc-port PORT     Raspberry Pi local VNC port, default: 5900
  --install-packages        Install autossh and openssh-client with apt
  --skip-start              Enable service, but do not start it now
  --help                    Show this help
EOF
}

log() {
  printf '[INFO] %s\n' "$*"
}

die() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script as root."
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --server-host)
        [[ $# -ge 2 ]] || die "Missing value after --server-host"
        SERVER_HOST="$2"
        shift 2
        ;;
      --server-port)
        [[ $# -ge 2 ]] || die "Missing value after --server-port"
        SERVER_PORT="$2"
        shift 2
        ;;
      --server-user)
        [[ $# -ge 2 ]] || die "Missing value after --server-user"
        SERVER_USER="$2"
        shift 2
        ;;
      --identity-file)
        [[ $# -ge 2 ]] || die "Missing value after --identity-file"
        IDENTITY_SOURCE="$2"
        shift 2
        ;;
      --known-hosts)
        [[ $# -ge 2 ]] || die "Missing value after --known-hosts"
        KNOWN_HOSTS_SOURCE="$2"
        shift 2
        ;;
      --remote-ssh-port)
        [[ $# -ge 2 ]] || die "Missing value after --remote-ssh-port"
        REMOTE_SSH_PORT="$2"
        shift 2
        ;;
      --enable-vnc)
        ENABLE_VNC=1
        shift
        ;;
      --remote-vnc-port)
        [[ $# -ge 2 ]] || die "Missing value after --remote-vnc-port"
        REMOTE_VNC_PORT="$2"
        shift 2
        ;;
      --local-vnc-port)
        [[ $# -ge 2 ]] || die "Missing value after --local-vnc-port"
        LOCAL_VNC_PORT="$2"
        shift 2
        ;;
      --install-packages)
        INSTALL_PACKAGES=1
        shift
        ;;
      --skip-start)
        SKIP_START=1
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
  BACKUP_DIR="${BACKUP_ROOT}/${ts}-reverse-ssh"
  install -d -m 700 "${BACKUP_DIR}"
}

backup_if_exists() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    cp -a "${path}" "${BACKUP_DIR}/"
    log "Backed up ${path} -> ${BACKUP_DIR}"
  fi
}

ensure_required_inputs() {
  [[ -n "${SERVER_HOST}" ]] || die "--server-host is required"
  [[ -f "${IDENTITY_SOURCE}" ]] || die "Identity file not found: ${IDENTITY_SOURCE}"
  [[ -f "${KNOWN_HOSTS_SOURCE}" ]] || die "known_hosts file not found: ${KNOWN_HOSTS_SOURCE}"
  [[ -f "${UNIT_SOURCE}" ]] || die "Unit template not found: ${UNIT_SOURCE}"
  [[ -f "${RUNNER_SOURCE}" ]] || die "Runner script not found: ${RUNNER_SOURCE}"
}

install_packages_with_apt() {
  command_exists apt-get || die "apt-get is not available on this system"
  apt-get update
  apt-get install -y autossh openssh-client
}

ensure_required_commands() {
  if ! command_exists autossh || ! command_exists ssh; then
    if [[ "${INSTALL_PACKAGES}" -eq 1 ]]; then
      install_packages_with_apt
    fi
  fi

  command_exists autossh || die "'autossh' is missing. Install it first or rerun with --install-packages."
  command_exists ssh || die "'ssh' is missing. Install it first or rerun with --install-packages."
  command_exists systemctl || die "'systemctl' is required"
  command_exists install || die "'install' is required"
}

same_path() {
  [[ "$(readlink -f "$1")" == "$(readlink -f "$2")" ]]
}

install_payloads() {
  install -d -m 700 /etc/korovki/remote_access
  install -d -m 755 /usr/local/lib/korovki-remote-access

  if same_path "${IDENTITY_SOURCE}" "${IDENTITY_DEST}"; then
    chmod 600 "${IDENTITY_DEST}"
  else
    install -m 600 "${IDENTITY_SOURCE}" "${IDENTITY_DEST}"
  fi

  if same_path "${KNOWN_HOSTS_SOURCE}" "${KNOWN_HOSTS_DEST}"; then
    chmod 644 "${KNOWN_HOSTS_DEST}"
  else
    install -m 644 "${KNOWN_HOSTS_SOURCE}" "${KNOWN_HOSTS_DEST}"
  fi

  install -m 755 "${RUNNER_SOURCE}" "${RUNNER_DEST}"
  install -m 644 "${UNIT_SOURCE}" "${UNIT_DEST}"

  cat > "${ENV_DEST}" <<EOF
SERVER_HOST='${SERVER_HOST}'
SERVER_PORT='${SERVER_PORT}'
SERVER_USER='${SERVER_USER}'
IDENTITY_FILE='${IDENTITY_DEST}'
KNOWN_HOSTS_FILE='${KNOWN_HOSTS_DEST}'
REMOTE_SSH_PORT='${REMOTE_SSH_PORT}'
ENABLE_VNC='${ENABLE_VNC}'
REMOTE_VNC_PORT='${REMOTE_VNC_PORT}'
LOCAL_VNC_PORT='${LOCAL_VNC_PORT}'
EOF
  chmod 600 "${ENV_DEST}"
}

enable_service() {
  systemctl daemon-reload
  systemctl enable reverse-ssh.service
  if [[ "${SKIP_START}" -eq 0 ]]; then
    systemctl restart reverse-ssh.service
    systemctl --no-pager --full status reverse-ssh.service || true
  else
    log "Service enabled but not started: reverse-ssh.service"
  fi
}

main() {
  parse_args "$@"
  require_root
  ensure_required_inputs
  ensure_required_commands
  backup_dir_create
  backup_if_exists "${ENV_DEST}"
  backup_if_exists "${IDENTITY_DEST}"
  backup_if_exists "${KNOWN_HOSTS_DEST}"
  backup_if_exists "${RUNNER_DEST}"
  backup_if_exists "${UNIT_DEST}"
  install_payloads
  enable_service

  cat <<EOF
[DONE] Reverse SSH fallback is installed.
Service: reverse-ssh.service
Env: ${ENV_DEST}
Backup: ${BACKUP_DIR}

Useful commands:
  systemctl status reverse-ssh.service --no-pager
  journalctl -u reverse-ssh.service -n 100 --no-pager
EOF
}

main "$@"
