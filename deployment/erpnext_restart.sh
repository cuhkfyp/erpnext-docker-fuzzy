#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ERPNEXT_VOLUME_ROOT:-/root/erpnext_docker_volume}"
DEPLOY_SCRIPT="$ROOT_DIR/deploy_db_connector.sh"
ASSUME_YES=0
[[ "${1:-}" == "--yes" ]] && ASSUME_YES=1

if (( ! ASSUME_YES )); then
	read -r -p "Restart ERPNext, n8n, and Surfshark VPN? (y/N): " answer
	[[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi

[[ -x "$DEPLOY_SCRIPT" ]] || { echo "Missing deployment script: $DEPLOY_SCRIPT" >&2; exit 1; }
"$DEPLOY_SCRIPT" --capture-only

stop_if_present() {
	docker inspect "$1" >/dev/null 2>&1 || return 0
	docker stop "$1" >/dev/null
}

start_if_present() {
	docker inspect "$1" >/dev/null 2>&1 || return 0
	docker start "$1" >/dev/null
}

for container in \
	frappe_docker-frontend-1 frappe_docker-scheduler-1 frappe_docker-websocket-1 \
	frappe_docker-queue-long-1 frappe_docker-queue-short-1 frappe_docker-backend-1 \
	frappe_docker-redis-queue-1 frappe_docker-redis-cache-1 frappe_docker-db-1 \
	n8n n8n-redis surfshark-vpn; do
	stop_if_present "$container"
done

for container in \
	surfshark-vpn frappe_docker-db-1 frappe_docker-redis-cache-1 \
	frappe_docker-redis-queue-1 frappe_docker-backend-1 \
	frappe_docker-queue-short-1 frappe_docker-queue-long-1 \
	frappe_docker-websocket-1 frappe_docker-scheduler-1 frappe_docker-frontend-1 \
	n8n-redis n8n; do
	start_if_present "$container"
done

for attempt in {1..60}; do
	if docker exec frappe_docker-backend-1 true >/dev/null 2>&1; then break; fi
	(( attempt == 60 )) && { echo "ERPNext backend did not become ready." >&2; exit 1; }
	sleep 2
done

"$DEPLOY_SCRIPT" --no-capture

if [[ -f "$ROOT_DIR/frappe_nginx_current.conf" ]]; then
	docker cp "$ROOT_DIR/frappe_nginx_current.conf" \
		frappe_docker-frontend-1:/etc/nginx/conf.d/frappe.conf
	docker exec frappe_docker-frontend-1 nginx -s reload
fi

docker exec frappe_docker-backend-1 bench --site frontend build --app hksr
docker exec frappe_docker-backend-1 bench --site frontend clear-cache

asset_stage="$(mktemp -d)"
trap 'rm -rf -- "$asset_stage"' EXIT
for asset in \
	sites/assets/hksr/js/n8n_chat.js \
	sites/assets/hksr/js/n8n_chat_umd.js \
	sites/assets/hksr/css/n8n_chat_style.css; do
	if docker cp "frappe_docker-backend-1:/home/frappe/frappe-bench/$asset" "$asset_stage/" 2>/dev/null; then
		docker exec frappe_docker-frontend-1 mkdir -p "/home/frappe/frappe-bench/$(dirname "$asset")"
		docker cp "$asset_stage/$(basename "$asset")" \
			"frappe_docker-frontend-1:/home/frappe/frappe-bench/$asset"
	fi
done
rm -rf -- "$asset_stage"
trap - EXIT

for container in frappe_docker-backend-1 frappe_docker-scheduler-1 frappe_docker-queue-long-1 frappe_docker-queue-short-1; do
	"$ROOT_DIR/check_requirement.sh" "$container"
done

backend_ip="$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' frappe_docker-backend-1)"
db_ip="$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' frappe_docker-db-1)"
vpn_ip="$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' surfshark-vpn)"
sed -i '/### Added by docker hostname ###/,/### End of docker hostname ###/d' /etc/hosts
{
	echo "### Added by docker hostname ###"
	echo "$db_ip erpnext_db"
	echo "$backend_ip erpnext_backend"
	echo "$vpn_ip surfshark_vpn"
	echo "### End of docker hostname ###"
} >> /etc/hosts

docker exec frappe_docker-backend-1 bench --site frontend set-config \
	surfshark_vpn "http://$vpn_ip:8888"
if [[ -x "$ROOT_DIR/sshmount_docker_backend.sh" ]]; then
	(cd /tmp && "$ROOT_DIR/sshmount_docker_backend.sh") || echo "Warning: optional SSHFS remount failed." >&2
elif docker exec -u root frappe_docker-backend-1 test -x /etc/init.d/ssh; then
	docker exec -u root frappe_docker-backend-1 /etc/init.d/ssh start
	"$ROOT_DIR/sshmount.sh" || echo "Warning: optional SSHFS remount failed." >&2
else
	echo "Warning: backend image has no SSH service; persistent app deployment is still active." >&2
fi

docker exec -u root frappe_docker-websocket-1 sh -c \
	'grep -qF "172.17.0.1 hksrfam.hksr.org.hk" /etc/hosts || echo "172.17.0.1 hksrfam.hksr.org.hk" >> /etc/hosts'

echo "ERPNext, n8n, and Surfshark restart completed successfully."
