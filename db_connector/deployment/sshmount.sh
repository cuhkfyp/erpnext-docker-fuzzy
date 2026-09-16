#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="${ERPNEXT_BACKEND_MOUNT:-/root/erpnext_docker_volume/backend}"
PASSWORD_FILE="${ERPNEXT_SSH_PASSWORD_FILE:-/root/pass.txt}"
REMOTE="${ERPNEXT_BACKEND_SSH_REMOTE:-frappe@erpnext_backend:/home/frappe/frappe-bench}"
ROOT_DIR="${ERPNEXT_VOLUME_ROOT:-/root/erpnext_docker_volume}"

if [[ -x "$ROOT_DIR/sshmount_docker_backend.sh" ]]; then
	exec "$ROOT_DIR/sshmount_docker_backend.sh"
fi

[[ -r "$PASSWORD_FILE" ]] || { echo "SSH password file is not readable: $PASSWORD_FILE" >&2; exit 1; }
mkdir -p "$TARGET"
if mountpoint -q "$TARGET"; then
	fusermount3 -u "$TARGET" 2>/dev/null || umount "$TARGET"
fi

sshfs "$REMOTE" "$TARGET" \
	-o "ssh_command=sshpass -f $PASSWORD_FILE ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15" \
	-o allow_other,reconnect,ServerAliveCountMax=3
mountpoint -q "$TARGET"
echo "Mounted $REMOTE at $TARGET"
