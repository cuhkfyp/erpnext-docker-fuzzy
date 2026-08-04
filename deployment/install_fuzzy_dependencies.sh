#!/usr/bin/env bash
set -Eeuo pipefail

BENCH_DIR="${BENCH_DIR:-/home/frappe/frappe-bench}"
REQUIREMENTS="$BENCH_DIR/apps/db_connector/db_connector/requirements.txt"
TARGET="$BENCH_DIR/sites/.python-dependencies/db_connector"
STAMP="$TARGET/.requirements.sha256"

if [[ ! -f "$REQUIREMENTS" ]]; then
	echo "Missing fuzzy requirements: $REQUIREMENTS" >&2
	exit 1
fi

required_hash="$(sha256sum "$REQUIREMENTS" | awk '{print $1}')"
if [[ -f "$STAMP" ]] && [[ "$(<"$STAMP")" == "$required_hash" ]]; then
	if PYTHONPATH="$TARGET${PYTHONPATH:+:$PYTHONPATH}" "$BENCH_DIR/env/bin/python" -c \
		'import duckdb, hanziconv, pypinyin, rapidfuzz, splink' >/dev/null 2>&1; then
		echo "Pinned fuzzy dependencies are already installed."
		exit 0
	fi
fi

staging="${TARGET}.new.$$"
previous="${TARGET}.previous.$$"
trap 'rm -rf -- "$staging"' EXIT
rm -rf -- "$staging"
mkdir -p "$staging"
"$BENCH_DIR/env/bin/pip" install --disable-pip-version-check --no-compile \
	--target "$staging" -r "$REQUIREMENTS"
printf '%s\n' "$required_hash" > "$staging/.requirements.sha256"

if [[ -d "$TARGET" ]]; then
	mv "$TARGET" "$previous"
fi
mv "$staging" "$TARGET"
rm -rf -- "$previous"
trap - EXIT

PYTHONPATH="$TARGET${PYTHONPATH:+:$PYTHONPATH}" "$BENCH_DIR/env/bin/python" -c \
	'import duckdb, hanziconv, pypinyin, rapidfuzz, splink'
echo "Installed pinned fuzzy dependencies in the persistent sites volume."
