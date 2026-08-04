#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="${ERPNEXT_BACKEND_MOUNT:-/root/erpnext_docker_volume/backend}"
PASSWORD_FILE="${ERPNEXT_SSH_PASSWORD_FILE:-/root/pass.txt}"
BACKEND_CONTAINER="${ERPNEXT_BACKEND_CONTAINER:-}"

[[ -r "$PASSWORD_FILE" ]] || {
	echo "SSH password file is not readable: $PASSWORD_FILE" >&2
	exit 1
}

if [[ -z "$BACKEND_CONTAINER" ]]; then
	BACKEND_CONTAINER="$(docker ps -q \
		--filter label=com.docker.compose.project=frappe_docker \
		--filter label=com.docker.compose.service=backend)"
fi
[[ -n "$BACKEND_CONTAINER" ]] || {
	echo "Running Frappe backend container not found" >&2
	exit 1
}

docker exec --user root "$BACKEND_CONTAINER" sh -c \
	'pgrep -x sshd >/dev/null || /usr/sbin/sshd'

REMOTE=""
while IFS= read -r backend_ip; do
	if timeout 3 bash -c "</dev/tcp/$backend_ip/22" 2>/dev/null; then
		REMOTE="frappe@$backend_ip:/home/frappe/frappe-bench"
		break
	fi
done < <(docker inspect -f '{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}' "$BACKEND_CONTAINER")

[[ -n "$REMOTE" ]] || {
	echo "Backend SSH service is not reachable from the host" >&2
	exit 1
}

mkdir -p "$TARGET"
if mountpoint -q "$TARGET"; then
	current_source="$(findmnt -n -o SOURCE --target "$TARGET" 2>/dev/null || true)"
	if [[ "$current_source" == "$REMOTE" ]] && [[ -r "$TARGET/sites/apps.txt" ]]; then
		echo "Backend is already mounted from $REMOTE at $TARGET"
		exit 0
	fi
	fusermount3 -uz "$TARGET" 2>/dev/null || umount -l "$TARGET"
fi

sshfs "$REMOTE" "$TARGET" \
	-o "ssh_command=sshpass -f $PASSWORD_FILE ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15" \
	-o allow_other,reconnect,ServerAliveCountMax=3

mountpoint -q "$TARGET"
echo "Mounted $REMOTE at $TARGET"
