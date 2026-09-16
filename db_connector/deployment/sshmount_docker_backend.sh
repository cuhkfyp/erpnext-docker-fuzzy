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

mount_sources() {
	findmnt -rn -o SOURCE --target "$TARGET" 2>/dev/null || true
}

mount_types() {
	findmnt -rn -o FSTYPE --target "$TARGET" 2>/dev/null || true
}

mkdir -p "$TARGET"
if mountpoint -q "$TARGET"; then
	mapfile -t mounted_sources < <(mount_sources)
	mapfile -t mounted_types < <(mount_types)
	all_current=1
	for source in "${mounted_sources[@]}"; do
		[[ "$source" == "$REMOTE" ]] || all_current=0
	done
	if (( ${#mounted_sources[@]} > 0 && all_current )) && timeout 5 test -r "$TARGET/sites/apps.txt"; then
		echo "Backend is already mounted from $REMOTE at $TARGET (${#mounted_sources[@]} visible layer(s)); no remount performed"
		exit 0
	fi
	for filesystem_type in "${mounted_types[@]}"; do
		[[ "$filesystem_type" == "fuse.sshfs" ]] || {
			echo "Refusing to unmount non-SSHFS layer $filesystem_type at $TARGET" >&2
			exit 1
		}
	done
	for _attempt in {1..16}; do
		mountpoint -q "$TARGET" || break
		before_count="$(mount_sources | wc -l)"
		fusermount3 -uz "$TARGET" 2>/dev/null || umount -l "$TARGET"
		after_count="$(mount_sources | wc -l)"
		if (( after_count >= before_count )); then
			echo "SSHFS layer count did not decrease at $TARGET; refusing to add another layer" >&2
			exit 1
		fi
	done
	mountpoint -q "$TARGET" && {
		echo "Unable to clear stale SSHFS layers at $TARGET" >&2
		exit 1
	}
fi

sshfs "$REMOTE" "$TARGET" \
	-o "ssh_command=sshpass -f $PASSWORD_FILE ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15" \
	-o allow_other,reconnect,ServerAliveCountMax=3

mapfile -t final_sources < <(mount_sources)
[[ ${#final_sources[@]} -eq 1 && "${final_sources[0]}" == "$REMOTE" ]]
timeout 5 test -r "$TARGET/sites/apps.txt"
echo "Mounted $REMOTE at $TARGET"
