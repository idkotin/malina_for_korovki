#!/usr/bin/env bash
set -euo pipefail

: "${SERVER_HOST:?SERVER_HOST is required}"
: "${SERVER_PORT:?SERVER_PORT is required}"
: "${SERVER_USER:?SERVER_USER is required}"
: "${IDENTITY_FILE:?IDENTITY_FILE is required}"
: "${KNOWN_HOSTS_FILE:?KNOWN_HOSTS_FILE is required}"
: "${REMOTE_SSH_PORT:?REMOTE_SSH_PORT is required}"
: "${ENABLE_VNC:?ENABLE_VNC is required}"
: "${REMOTE_VNC_PORT:?REMOTE_VNC_PORT is required}"
: "${LOCAL_VNC_PORT:?LOCAL_VNC_PORT is required}"

args=(
  -M 0
  -N
  -o BatchMode=yes
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="${KNOWN_HOSTS_FILE}"
  -o IdentitiesOnly=yes
  -o ConnectTimeout=10
  -i "${IDENTITY_FILE}"
  -p "${SERVER_PORT}"
  -R "127.0.0.1:${REMOTE_SSH_PORT}:127.0.0.1:22"
)

if [[ "${ENABLE_VNC}" == "1" ]]; then
  args+=(-R "127.0.0.1:${REMOTE_VNC_PORT}:127.0.0.1:${LOCAL_VNC_PORT}")
fi

exec /usr/bin/autossh "${args[@]}" "${SERVER_USER}@${SERVER_HOST}"

