#!/usr/bin/env bash
set -euo pipefail

TUNNEL_USER="pi-tunnel"
PUBLIC_KEY_FILE=""
REMOTE_SSH_PORT="2222"
REMOTE_VNC_PORT="5901"
ALLOW_VNC=1

usage() {
  cat <<'EOF'
Usage:
  prepare_reverse_ssh_server.sh --public-key-file PATH [options]

Options:
  --public-key-file PATH   Public key for the Raspberry Pi tunnel client
  --user USER              Tunnel user to create/update, default: pi-tunnel
  --remote-ssh-port PORT   Localhost SSH reverse port, default: 2222
  --remote-vnc-port PORT   Localhost VNC reverse port, default: 5901
  --no-vnc                 Do not allow the VNC reverse port
  --help                   Show this help
EOF
}

die() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

log() {
  printf '[INFO] %s\n' "$*"
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script as root on the server."
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --public-key-file)
        [[ $# -ge 2 ]] || die "Missing value after --public-key-file"
        PUBLIC_KEY_FILE="$2"
        shift 2
        ;;
      --user)
        [[ $# -ge 2 ]] || die "Missing value after --user"
        TUNNEL_USER="$2"
        shift 2
        ;;
      --remote-ssh-port)
        [[ $# -ge 2 ]] || die "Missing value after --remote-ssh-port"
        REMOTE_SSH_PORT="$2"
        shift 2
        ;;
      --remote-vnc-port)
        [[ $# -ge 2 ]] || die "Missing value after --remote-vnc-port"
        REMOTE_VNC_PORT="$2"
        shift 2
        ;;
      --no-vnc)
        ALLOW_VNC=0
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

validate_inputs() {
  [[ -n "${PUBLIC_KEY_FILE}" ]] || die "--public-key-file is required"
  [[ -f "${PUBLIC_KEY_FILE}" ]] || die "Public key file not found: ${PUBLIC_KEY_FILE}"
  [[ "${REMOTE_SSH_PORT}" =~ ^[0-9]+$ ]] || die "Invalid SSH port: ${REMOTE_SSH_PORT}"
  [[ "${REMOTE_VNC_PORT}" =~ ^[0-9]+$ ]] || die "Invalid VNC port: ${REMOTE_VNC_PORT}"
}

ensure_user() {
  if ! id "${TUNNEL_USER}" >/dev/null 2>&1; then
    useradd --create-home --shell /usr/sbin/nologin --user-group "${TUNNEL_USER}"
    log "Created user ${TUNNEL_USER}"
  fi

  passwd -l "${TUNNEL_USER}" >/dev/null 2>&1 || true
  usermod --shell /usr/sbin/nologin "${TUNNEL_USER}"
}

install_authorized_key() {
  local ssh_dir auth_file auth_options pubkey tmp_file backup

  ssh_dir="/home/${TUNNEL_USER}/.ssh"
  auth_file="${ssh_dir}/authorized_keys"
  pubkey="$(tr -d '\r\n' < "${PUBLIC_KEY_FILE}")"
  [[ "${pubkey}" == ssh-* ]] || die "Public key file does not look like an OpenSSH public key"

  auth_options="restrict,port-forwarding,permitlisten=\"127.0.0.1:${REMOTE_SSH_PORT}\""
  if [[ "${ALLOW_VNC}" -eq 1 ]]; then
    auth_options="${auth_options},permitlisten=\"127.0.0.1:${REMOTE_VNC_PORT}\""
  fi

  install -d -m 700 -o "${TUNNEL_USER}" -g "${TUNNEL_USER}" "${ssh_dir}"
  if [[ -f "${auth_file}" ]]; then
    backup="${auth_file}.bak-$(date +%Y%m%d-%H%M%S)"
    cp -a "${auth_file}" "${backup}"
    log "Backed up ${auth_file} -> ${backup}"
  fi

  tmp_file="$(mktemp)"
  if [[ -f "${auth_file}" ]]; then
    grep -Fv "${pubkey}" "${auth_file}" > "${tmp_file}" || true
  fi
  printf '%s %s\n' "${auth_options}" "${pubkey}" >> "${tmp_file}"
  install -m 600 -o "${TUNNEL_USER}" -g "${TUNNEL_USER}" "${tmp_file}" "${auth_file}"
  rm -f "${tmp_file}"
}

print_status() {
  printf 'user='
  id "${TUNNEL_USER}"
  printf 'shell='
  getent passwd "${TUNNEL_USER}" | cut -d: -f7
  printf 'authorized_keys='
  wc -l < "/home/${TUNNEL_USER}/.ssh/authorized_keys"
  printf 'sshd_allowtcpforwarding='
  sshd -T | awk '/^allowtcpforwarding / { print $2; exit }'
  printf 'sshd_gatewayports='
  sshd -T | awk '/^gatewayports / { print $2; exit }'
}

main() {
  parse_args "$@"
  require_root
  validate_inputs
  ensure_user
  install_authorized_key
  print_status
}

main "$@"
