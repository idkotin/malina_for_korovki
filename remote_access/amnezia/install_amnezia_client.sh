#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SOURCE="${SCRIPT_DIR}/systemd/amneziawg-client@.service"
BACKUP_ROOT="/var/backups/korovki-remote-access"
AWG_CONFIG_DIR="/etc/amnezia/amneziawg"
UNIT_DEST="/etc/systemd/system/amneziawg-client@.service"

INTERFACE_NAME="awg0"
CONFIG_SOURCE=""
INSTALL_PACKAGES=0
ALLOW_DEFAULT_ROUTE=0
SKIP_START=0
SERVICE_ALLOWED_IPS=""
KEEP_DNS=0
PREPARED_CONFIG=""
TMP_GNUPG=""

usage() {
  cat <<'EOF'
Usage:
  install_amnezia_client.sh --config /path/to/exported.conf [options]

Options:
  --config PATH            Path to exported AmneziaWG native config
  --interface NAME         Interface name to install, default: awg0
  --install-packages       Try to install amneziawg package with apt
  --allow-default-route    Allow configs with AllowedIPs = 0.0.0.0/0 or ::/0
  --service-allowed-ips IPs
                           Replace peer AllowedIPs before install, for example: 10.8.1.0/24
  --keep-dns               Keep DNS lines when --service-allowed-ips is used
  --skip-start             Install files and enable service, but do not start it now
  --help                   Show this help
EOF
}

log() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
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
      --config)
        [[ $# -ge 2 ]] || die "Missing value after --config"
        CONFIG_SOURCE="$2"
        shift 2
        ;;
      --interface)
        [[ $# -ge 2 ]] || die "Missing value after --interface"
        INTERFACE_NAME="$2"
        shift 2
        ;;
      --install-packages)
        INSTALL_PACKAGES=1
        shift
        ;;
      --allow-default-route)
        ALLOW_DEFAULT_ROUTE=1
        shift
        ;;
      --service-allowed-ips)
        [[ $# -ge 2 ]] || die "Missing value after --service-allowed-ips"
        SERVICE_ALLOWED_IPS="$2"
        shift 2
        ;;
      --keep-dns)
        KEEP_DNS=1
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

validate_interface_name() {
  [[ "${INTERFACE_NAME}" =~ ^[a-zA-Z0-9_=+.-]{1,15}$ ]] || die "Invalid interface name: ${INTERFACE_NAME}"
}

backup_dir_create() {
  local ts
  ts="$(date +%Y%m%d-%H%M%S)"
  BACKUP_DIR="${BACKUP_ROOT}/${ts}"
  install -d -m 700 "${BACKUP_DIR}"
}

backup_if_exists() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    cp -a "${path}" "${BACKUP_DIR}/"
    log "Backed up ${path} -> ${BACKUP_DIR}"
  fi
}

ensure_config_source() {
  [[ -n "${CONFIG_SOURCE}" ]] || die "--config is required"
  [[ -f "${CONFIG_SOURCE}" ]] || die "Config file not found: ${CONFIG_SOURCE}"
}

config_has_full_tunnel() {
  grep -Eiq '^[[:space:]]*AllowedIPs[[:space:]]*=[[:space:]]*([^#]*)(0\.0\.0\.0/0|::/0)' "$1"
}

validate_config_file() {
  local cfg="$1"
  grep -Eq '^[[:space:]]*\[Interface\][[:space:]]*$' "${cfg}" || die "Config is missing [Interface] section"
  grep -Eq '^[[:space:]]*\[Peer\][[:space:]]*$' "${cfg}" || die "Config is missing [Peer] section"

  if config_has_full_tunnel "${cfg}" && [[ "${ALLOW_DEFAULT_ROUTE}" -ne 1 ]]; then
    cat >&2 <<'EOF'
[ERROR] Refusing to install a full-tunnel config.
The exported config contains AllowedIPs = 0.0.0.0/0 and/or ::/0.
This can redirect the Raspberry Pi default route through VPN and break telemetry or modem connectivity.

Review the exported config first and keep only the minimum VPN networks required for service access.
If you really want to allow this behavior, rerun with --allow-default-route.
EOF
    exit 1
  fi
}

prepare_config_file() {
  local source_cfg="$1"
  local tmp_cfg

  if [[ -z "${SERVICE_ALLOWED_IPS}" ]]; then
    PREPARED_CONFIG="${source_cfg}"
    return 0
  fi

  tmp_cfg="$(mktemp)"
  awk -v service_ips="${SERVICE_ALLOWED_IPS}" -v keep_dns="${KEEP_DNS}" '
    function flush_peer_allowed() {
      if (section == "Peer" && peer_allowed_written == 0) {
        print "AllowedIPs = " service_ips
        peer_allowed_written = 1
      }
    }
    /^[[:space:]]*\[/ {
      flush_peer_allowed()
      section = $0
      gsub(/^[[:space:]]*\[/, "", section)
      gsub(/\][[:space:]]*$/, "", section)
      peer_allowed_written = 0
      print
      next
    }
    section == "Interface" && keep_dns != 1 && /^[[:space:]]*DNS[[:space:]]*=/ {
      next
    }
    section == "Peer" && /^[[:space:]]*AllowedIPs[[:space:]]*=/ {
      print "AllowedIPs = " service_ips
      peer_allowed_written = 1
      next
    }
    { print }
    END {
      flush_peer_allowed()
    }
  ' "${source_cfg}" > "${tmp_cfg}"

  PREPARED_CONFIG="${tmp_cfg}"
}

ensure_system_commands() {
  command_exists install || die "'install' command is required"
  command_exists systemctl || die "'systemctl' command is required"
  command_exists ip || die "'ip' command is required"
}

install_packages_with_apt() {
  local os_id os_like header_pkg keyring_path

  command_exists apt-get || die "apt-get is not available on this system"
  [[ -r /etc/os-release ]] || die "Cannot detect OS: /etc/os-release is missing"

  # shellcheck disable=SC1091
  . /etc/os-release
  os_id="${ID:-}"
  os_like="${ID_LIKE:-}"
  header_pkg="linux-headers-$(uname -r)"
  keyring_path="/usr/share/keyrings/amneziawg-archive-keyring.gpg"

  log "Installing AmneziaWG package with apt"
  apt-get update
  if ! apt-get install -y gnupg2 ca-certificates dkms; then
    apt-get install -y gnupg ca-certificates dkms
  fi

  if apt-cache show "${header_pkg}" >/dev/null 2>&1; then
    apt-get install -y "${header_pkg}"
  elif apt-cache show raspberrypi-kernel-headers >/dev/null 2>&1; then
    apt-get install -y raspberrypi-kernel-headers
  else
    warn "Kernel headers were not found automatically. AmneziaWG package install may ask for kernel sources."
  fi

  if [[ "${os_id}" == "ubuntu" || "${os_like}" == *ubuntu* ]]; then
    apt-get install -y software-properties-common python3-launchpadlib
    if ! grep -Rq 'ppa.launchpadcontent.net/amnezia/ppa' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
      add-apt-repository -y ppa:amnezia/ppa
    fi
  elif [[ "${os_id}" == "debian" || "${os_id}" == "raspbian" || "${os_like}" == *debian* ]]; then
    warn "Using the Debian/Raspberry Pi OS package path from the current upstream AmneziaWG README."
    if ! grep -Rq 'ppa.launchpadcontent.net/amnezia/ppa/ubuntu focal main' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
      TMP_GNUPG="$(mktemp -d)"
      chmod 700 "${TMP_GNUPG}"
      GNUPGHOME="${TMP_GNUPG}" gpg --batch --keyserver keyserver.ubuntu.com --recv-keys 57290828
      GNUPGHOME="${TMP_GNUPG}" gpg --batch --yes --output "${keyring_path}" --export 57290828
      rm -rf "${TMP_GNUPG}"
      TMP_GNUPG=""
      install -d -m 755 /etc/apt/sources.list.d
      printf '%s\n' \
        "deb [signed-by=${keyring_path}] https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu focal main" \
        "deb-src [signed-by=${keyring_path}] https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu focal main" > /etc/apt/sources.list.d/amneziawg-ppa.list
    fi
  else
    die "Automatic package install is only prepared for Debian/Ubuntu-like systems."
  fi

  apt-get update
  apt-get install -y amneziawg
}

ensure_awg_installed() {
  if command_exists awg && command_exists awg-quick; then
    return 0
  fi

  if [[ "${INSTALL_PACKAGES}" -eq 1 ]]; then
    install_packages_with_apt
  fi

  command_exists awg || die "'awg' is missing. Install AmneziaWG first or rerun with --install-packages."
  command_exists awg-quick || die "'awg-quick' is missing. Install AmneziaWG first or rerun with --install-packages."
}

install_unit_file() {
  [[ -f "${UNIT_SOURCE}" ]] || die "Unit template not found: ${UNIT_SOURCE}"
  install -D -m 644 "${UNIT_SOURCE}" "${UNIT_DEST}"
}

install_config_file() {
  local dest="${AWG_CONFIG_DIR}/${INTERFACE_NAME}.conf"
  install -d -m 700 "${AWG_CONFIG_DIR}"
  install -m 600 "${PREPARED_CONFIG}" "${dest}"
  log "Installed config to ${dest}"
}

enable_service() {
  local service_name="amneziawg-client@${INTERFACE_NAME}.service"
  systemctl daemon-reload
  systemctl enable "${service_name}"
  if [[ "${SKIP_START}" -eq 0 ]]; then
    systemctl restart "${service_name}"
    systemctl --no-pager --full status "${service_name}" || true
  else
    log "Service enabled but not started: ${service_name}"
  fi
}

main() {
  parse_args "$@"
  require_root
  ensure_system_commands
  validate_interface_name
  ensure_config_source
  prepare_config_file "${CONFIG_SOURCE}"
  validate_config_file "${PREPARED_CONFIG}"
  ensure_awg_installed
  backup_dir_create
  backup_if_exists "${AWG_CONFIG_DIR}/${INTERFACE_NAME}.conf"
  backup_if_exists "${UNIT_DEST}"
  install_unit_file
  install_config_file
  enable_service

  cat <<EOF
[DONE] AmneziaWG client files are installed.
Interface: ${INTERFACE_NAME}
Config: ${AWG_CONFIG_DIR}/${INTERFACE_NAME}.conf
Unit: amneziawg-client@${INTERFACE_NAME}.service
Backup: ${BACKUP_DIR}

Useful commands:
  systemctl status amneziawg-client@${INTERFACE_NAME} --no-pager
  journalctl -u amneziawg-client@${INTERFACE_NAME} -n 100 --no-pager
  awg show ${INTERFACE_NAME}
EOF
}

trap '[[ -n "${PREPARED_CONFIG}" && "${PREPARED_CONFIG}" != "${CONFIG_SOURCE}" ]] && rm -f "${PREPARED_CONFIG}"; [[ -n "${TMP_GNUPG}" ]] && rm -rf "${TMP_GNUPG}"' EXIT
main "$@"
