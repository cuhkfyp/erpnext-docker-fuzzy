#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ERPNEXT_VOLUME_ROOT:-/root/erpnext_docker_volume}"
LIVE_APP="$ROOT_DIR/backend/apps/db_connector"
PERSISTENT_APP="$ROOT_DIR/persistent_apps/db_connector"
APP_IN_CONTAINER="/home/frappe/frappe-bench/apps/db_connector"
SITE="${FRAPPE_SITE:-frontend}"
CAPTURE=1
CAPTURE_ONLY=0
CODE_ONLY=0

for option in "$@"; do
	case "$option" in
		--no-capture) CAPTURE=0 ;;
		--capture-only) CAPTURE_ONLY=1 ;;
		--code-only) CODE_ONLY=1 ;;
		*) echo "Unknown option: $option" >&2; exit 2 ;;
	esac
done

valid_app() {
	[[ -f "$1/pyproject.toml" && -f "$1/db_connector/hooks.py" ]]
}

if (( CAPTURE )); then
	if valid_app "$LIVE_APP"; then
		mkdir -p "$PERSISTENT_APP"
		rsync -a --delete \
			--exclude='.git/' --exclude='db_connector/.git/' \
			--exclude='**/__pycache__/' --exclude='*.py[co]' \
			--exclude='.pytest_cache/' --exclude='.ruff_cache/' \
			"$LIVE_APP/" "$PERSISTENT_APP/"
		echo "Captured db_connector into $PERSISTENT_APP"
	elif ! valid_app "$PERSISTENT_APP"; then
		echo "Neither live nor persistent db_connector source is valid." >&2
		exit 1
	else
		echo "Live SSHFS source unavailable; retaining the persistent copy."
	fi
fi

(( CAPTURE_ONLY )) && exit 0
valid_app "$PERSISTENT_APP" || {
	echo "Persistent db_connector source is missing or invalid: $PERSISTENT_APP" >&2
	exit 1
}

containers=(
	frappe_docker-backend-1
	frappe_docker-scheduler-1
	frappe_docker-queue-long-1
	frappe_docker-queue-short-1
)

for container in "${containers[@]}"; do
	docker inspect "$container" >/dev/null
	docker start "$container" >/dev/null
	docker exec -u root "$container" mkdir -p "$APP_IN_CONTAINER"
	if docker exec "$container" test -f "$APP_IN_CONTAINER/pyproject.toml"; then
		# Existing containers already contain the full private app. Overlay only
		# the versioned fuzzy component and its Frappe module controllers.
		for relative in \
			db_connector/api_ccd_fuzzy.py \
			db_connector/api_fuzzy_evaluation.py \
			db_connector/api_fuzzy_canary.py \
			db_connector/api_fuzzy_review_queue.py \
			db_connector/api_ccd_fuzzy.md \
			db_connector/MATCHING_PILOT.md \
			db_connector/requirements.txt \
			db_connector/fuzzy_matching \
			db_connector/db_connector \
			db_connector/deployment; do
			docker cp "$PERSISTENT_APP/$relative" "$container:$APP_IN_CONTAINER/$(dirname "$relative")/"
		done
	else
		docker cp "$PERSISTENT_APP/." "$container:$APP_IN_CONTAINER/"
	fi
	docker exec -u root "$container" chown -R frappe:frappe "$APP_IN_CONTAINER"
done

# The private hksr app is installed on the site but is not part of the stock
# ERPNext image.  Backend is its existing source of truth; workers must have
# the same Python package before a restart or Frappe cannot build its module
# map and enters a crash loop with ModuleNotFoundError.
HKSR_IN_CONTAINER="/home/frappe/frappe-bench/apps/hksr"
if docker exec frappe_docker-backend-1 test -f "$HKSR_IN_CONTAINER/pyproject.toml"; then
	runtime_app_stage="$(mktemp -d "$ROOT_DIR/.db-connector-runtime.XXXXXX")"
	trap 'rm -rf -- "$runtime_app_stage"' EXIT
	mkdir -p "$runtime_app_stage/hksr"
	docker cp "frappe_docker-backend-1:$HKSR_IN_CONTAINER/." "$runtime_app_stage/hksr/"
	printf '%s\n' "$HKSR_IN_CONTAINER" > "$runtime_app_stage/hksr.pth"
	for container in \
		frappe_docker-scheduler-1 \
		frappe_docker-queue-long-1 \
		frappe_docker-queue-short-1; do
		docker exec -u root "$container" mkdir -p "$HKSR_IN_CONTAINER"
		docker cp "$runtime_app_stage/hksr/." "$container:$HKSR_IN_CONTAINER/"
		docker cp "$runtime_app_stage/hksr.pth" \
			"$container:/home/frappe/frappe-bench/env/lib/python3.11/site-packages/hksr.pth"
		docker exec -u root "$container" chown -R frappe:frappe "$HKSR_IN_CONTAINER"
	done
	rm -rf -- "$runtime_app_stage"
	trap - EXIT
fi

if (( ! CODE_ONLY )); then
	docker exec frappe_docker-backend-1 \
		bash "$APP_IN_CONTAINER/db_connector/deployment/install_fuzzy_dependencies.sh"
	docker exec frappe_docker-backend-1 bench --site "$SITE" migrate
	docker exec frappe_docker-backend-1 bench --site "$SITE" execute \
		db_connector.api_fuzzy_evaluation.install_matching_roles
	docker exec frappe_docker-backend-1 bench --site "$SITE" execute \
		db_connector.api_fuzzy_evaluation.install_default_pilot_policy
	docker exec frappe_docker-backend-1 bench --site "$SITE" execute \
		db_connector.api_fuzzy_canary.install_existing_canary_review_workflows
	docker exec frappe_docker-backend-1 bench --site "$SITE" build --app db_connector
fi
docker exec frappe_docker-backend-1 bench --site "$SITE" clear-cache

if (( ! CODE_ONLY )); then
	asset_stage="$(mktemp -d "$ROOT_DIR/.db-connector-assets.XXXXXX")"
	trap 'rm -rf -- "$asset_stage"' EXIT
	if docker cp \
		frappe_docker-backend-1:/home/frappe/frappe-bench/sites/assets/db_connector \
		"$asset_stage/" 2>/dev/null; then
		if ! docker exec frappe_docker-frontend-1 mkdir -p \
				/home/frappe/frappe-bench/sites/assets/db_connector \
			|| ! docker cp "$asset_stage/db_connector/." \
				frappe_docker-frontend-1:/home/frappe/frappe-bench/sites/assets/db_connector/; then
			echo "Warning: optional db_connector frontend asset copy failed." >&2
		fi
	fi
	rm -rf -- "$asset_stage"
	trap - EXIT
fi

docker restart \
	frappe_docker-backend-1 \
	frappe_docker-scheduler-1 \
	frappe_docker-queue-long-1 \
	frappe_docker-queue-short-1 >/dev/null

if [[ -x "$ROOT_DIR/sshmount_docker_backend.sh" ]]; then
	(cd /tmp && "$ROOT_DIR/sshmount_docker_backend.sh") || \
		echo "Warning: db_connector deployed, but the optional backend SSHFS remount failed." >&2
fi

if (( CODE_ONLY )); then
	echo "db_connector code deployed and restarted from persistent host source."
else
	echo "db_connector deployed, migrated, and restarted from persistent host source."
fi
