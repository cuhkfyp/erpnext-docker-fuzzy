#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ERPNEXT_VOLUME_ROOT:-/root/erpnext_docker_volume}"
REQUIREMENTS_FILE="${HKSR_REQUIREMENTS_FILE:-$ROOT_DIR/hksr_requirement.txt}"
container="${1:-}"

[[ "$container" =~ ^[A-Za-z0-9_.-]+$ ]] || {
	echo "Usage: $0 <container_name>" >&2
	exit 1
}
[[ -f "$REQUIREMENTS_FILE" ]] || {
	echo "Requirements file not found: $REQUIREMENTS_FILE" >&2
	exit 1
}
docker inspect "$container" >/dev/null

while IFS= read -r line || [[ -n "$line" ]]; do
	[[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
	package="${line%%,*}"
	command="${line#*,}"
	package="$(xargs <<<"$package")"
	command="$(xargs <<<"$command")"
	[[ -n "$package" && "$command" != "$line" ]] || continue
	if docker exec "$container" bench pip show "$package" >/dev/null 2>&1; then
		echo "$container: $package already installed"
		continue
	fi
	command="${command//#1/$container}"
	command="${command// -it / }"
	command="${command// -i / }"
	command="${command// -t / }"
	command="${command/ apt install / apt install -y }"
	echo "$container: installing $package"
	bash -lc "$command"
done < "$REQUIREMENTS_FILE"
