#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${COINDCX_PROJECT_DIR:-/opt/coindcx-trading-bot}"
ARCHIVE_DIR="${COINDCX_ARCHIVE_DIR:-/var/backups/coindcx-bot}"
RETENTION_DAYS="${COINDCX_ARCHIVE_RETENTION_DAYS:-30}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$ARCHIVE_DIR/coindcx-audit-$timestamp.tgz"

install -d -m 0750 "$ARCHIVE_DIR"

tar_args=(
  --ignore-failed-read
  --exclude=.env
  --exclude=.env.local.secret
  --exclude='*.secret'
  -czf "$archive"
)

project_items=()
for item in logs data/live_state data/live_state.json data/paper_state.json data/paper_intrabar_audit.csv; do
  if [[ -e "$PROJECT_DIR/$item" ]]; then
    project_items+=("$item")
  fi
done
if (( ${#project_items[@]} > 0 )); then
  tar_args+=(-C "$PROJECT_DIR" "${project_items[@]}")
fi
if [[ -d /var/log/coindcx-bot ]]; then
  tar_args+=(-C /var/log coindcx-bot)
fi

if (( ${#project_items[@]} == 0 )) && [[ ! -d /var/log/coindcx-bot ]]; then
  echo "No CoinDCX logs or runtime state found to archive." >&2
  exit 1
fi

tar "${tar_args[@]}"
chmod 0640 "$archive"
find "$ARCHIVE_DIR" -type f -name 'coindcx-audit-*.tgz' -mtime "+$RETENTION_DAYS" -delete
echo "$archive"
