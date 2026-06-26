#!/usr/bin/env bash
set -euo pipefail

INTERFACE_NAME="awg0"
SERVICE_NAME=""
CONFIG_PATH=""

usage() {
  cat <<'EOF'
Usage:
  check_remote_access.sh [--interface awg0]
EOF
}

section() {
  printf '\n==== %s ====\n' "$1"
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --interface)
        [[ $# -ge 2 ]] || { echo "Missing value after --interface" >&2; exit 1; }
        INTERFACE_NAME="$2"
        shift 2
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        exit 1
        ;;
    esac
  done
}

print_binary_status() {
  section "Binaries"
  for bin in awg awg-quick ip systemctl journalctl; do
    if command_exists "${bin}"; then
      printf '[OK] %s -> %s\n' "${bin}" "$(command -v "${bin}")"
    else
      printf '[MISS] %s\n' "${bin}"
    fi
  done
}

print_service_status() {
  section "Service"
  if command_exists systemctl; then
    systemctl is-enabled "${SERVICE_NAME}" 2>/dev/null || true
    systemctl is-active "${SERVICE_NAME}" 2>/dev/null || true
    systemctl --no-pager --full status "${SERVICE_NAME}" || true
  else
    echo "systemctl is not available"
  fi
}

print_interface_status() {
  section "Interface"
  ip link show dev "${INTERFACE_NAME}" || true
  ip addr show dev "${INTERFACE_NAME}" || true
}

print_awg_status() {
  section "Protocol"
  if command_exists awg; then
    awg show "${INTERFACE_NAME}" || true
  else
    echo "awg command is missing"
  fi
}

print_routes() {
  section "Routes"
  ip route show table all | grep -F "${INTERFACE_NAME}" || true
}

print_endpoint_hint() {
  section "Config summary"
  if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "Config not found: ${CONFIG_PATH}"
    return 0
  fi

  awk '
    BEGIN { IGNORECASE=1 }
    /^[[:space:]]*PrivateKey[[:space:]]*=/ { next }
    /^[[:space:]]*PresharedKey[[:space:]]*=/ { next }
    /^[[:space:]]*Address[[:space:]]*=/ { print }
    /^[[:space:]]*DNS[[:space:]]*=/ { print }
    /^[[:space:]]*Endpoint[[:space:]]*=/ { print }
    /^[[:space:]]*AllowedIPs[[:space:]]*=/ { print }
  ' "${CONFIG_PATH}"

  if grep -Eiq '^[[:space:]]*AllowedIPs[[:space:]]*=[[:space:]]*([^#]*)(0\.0\.0\.0/0|::/0)' "${CONFIG_PATH}"; then
    echo "WARNING: config includes a default-route AllowedIPs entry"
  fi
}

print_logs() {
  section "Recent logs"
  if command_exists journalctl; then
    journalctl -u "${SERVICE_NAME}" -n 40 --no-pager || true
  fi
}

main() {
  parse_args "$@"
  SERVICE_NAME="amneziawg-client@${INTERFACE_NAME}.service"
  CONFIG_PATH="/etc/amnezia/amneziawg/${INTERFACE_NAME}.conf"
  print_binary_status
  print_service_status
  print_interface_status
  print_awg_status
  print_routes
  print_endpoint_hint
  print_logs
}

main "$@"

