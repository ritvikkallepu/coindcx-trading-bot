#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${COINDCX_PROJECT_DIR:-/opt/coindcx-trading-bot}"
RUNTIME_FILE="${COINDCX_RUNTIME_FILE:-/etc/coindcx-bot/runtime.env}"

if [[ ! -f "$RUNTIME_FILE" ]]; then
  echo "Missing runtime configuration: $RUNTIME_FILE" >&2
  exit 2
fi

set -a
source "$RUNTIME_FILE"
set +a

: "${BOT_PAIRS:?BOT_PAIRS is required}"
: "${BOT_CAPITAL_PER_PAIR:?BOT_CAPITAL_PER_PAIR is required}"
: "${BOT_LEVERAGE:?BOT_LEVERAGE is required}"
: "${BOT_INTERVAL:?BOT_INTERVAL is required}"
: "${BOT_EXECUTION_INTERVAL:?BOT_EXECUTION_INTERVAL is required}"
: "${BOT_STRATEGY:?BOT_STRATEGY is required}"

PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python virtual environment not found at $PYTHON_BIN" >&2
  exit 127
fi

args=(
  -m app.main live-run
  --pairs "$BOT_PAIRS"
  --capital-per-pair "$BOT_CAPITAL_PER_PAIR"
  --leverage "$BOT_LEVERAGE"
  --interval "$BOT_INTERVAL"
  --execution-interval "$BOT_EXECUTION_INTERVAL"
  --strategy "$BOT_STRATEGY"
)

if [[ -n "${BOT_LEVERAGE_BY_PAIR:-}" ]]; then
  args+=(--leverage-by-pair "$BOT_LEVERAGE_BY_PAIR")
fi
if [[ -n "${BOT_TAKE_PROFIT_PCT:-}" ]]; then
  args+=(--take-profit-pct "$BOT_TAKE_PROFIT_PCT")
fi
if [[ "${BOT_DRY_RUN:-false}" == "true" ]]; then
  args+=(--dry-run)
fi

cd "$PROJECT_DIR"
exec "$PYTHON_BIN" "${args[@]}"
