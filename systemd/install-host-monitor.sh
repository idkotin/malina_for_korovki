#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo bash ./systemd/install-host-monitor.sh" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(dirname -- "${script_dir}")"
runtime_dir="/etc/host-monitor"
runtime_config="${runtime_dir}/config.yaml"

install -d -m 755 "${runtime_dir}"
if [[ ! -e "${runtime_config}" ]]; then
  # On the first migration config.yaml is still the known-good live file in
  # the checkout. Copy it outside Git before the checkout is cleaned.
  install -m 600 "${repo_dir}/config.yaml" "${runtime_config}"
  echo "Migrated live config to ${runtime_config}"
else
  echo "Keeping existing live config: ${runtime_config}"
fi

install -m 644 "${script_dir}/host-monitor.service" /etc/systemd/system/host-monitor.service
systemctl daemon-reload

echo "Installed host-monitor.service"
echo "Repository config.yaml is now only a public example; production uses ${runtime_config}"
