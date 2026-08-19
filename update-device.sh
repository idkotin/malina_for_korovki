#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${repo_dir}"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Tracked source files have local changes; refusing to update:" >&2
  git status --short >&2
  exit 1
fi

git pull --ff-only origin master

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -e .

sudo bash ./systemd/install-host-monitor.sh
sudo systemctl enable host-monitor.service
sudo systemctl restart host-monitor.service

echo "Update complete. Live config was not modified."
systemctl --no-pager --full status host-monitor.service
