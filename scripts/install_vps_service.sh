#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/opt/coindcx-trading-bot}"
if [[ ! -f "$PROJECT_DIR/ops/systemd/coindcx-bot.service" ]]; then
  echo "Run this script from the deployed repository or pass its absolute path." >&2
  exit 2
fi

install -d -m 0750 /etc/coindcx-bot /var/log/coindcx-bot /var/backups/coindcx-bot
install -d -m 0755 /var/log/journal
install -m 0755 "$PROJECT_DIR/scripts/run_live_vps.sh" /usr/local/bin/coindcx-run-live
install -m 0755 "$PROJECT_DIR/scripts/export_vps_audit.sh" /usr/local/bin/coindcx-export-audit
install -m 0644 "$PROJECT_DIR/ops/systemd/coindcx-bot.service" /etc/systemd/system/coindcx-bot.service
install -m 0644 "$PROJECT_DIR/ops/systemd/coindcx-log-archive.service" /etc/systemd/system/coindcx-log-archive.service
install -m 0644 "$PROJECT_DIR/ops/systemd/coindcx-log-archive.timer" /etc/systemd/system/coindcx-log-archive.timer

if [[ ! -f /etc/coindcx-bot/runtime.env ]]; then
  install -m 0600 \
    "$PROJECT_DIR/ops/systemd/coindcx-bot.runtime.env.example" \
    /etc/coindcx-bot/runtime.env
fi

systemctl restart systemd-journald
systemctl daemon-reload
systemctl enable --now coindcx-log-archive.timer

cat <<'EOF'
VPS logging is installed. The trading service was NOT started automatically.

1. Edit runtime settings: nano /etc/coindcx-bot/runtime.env
2. Start the bot:          systemctl enable --now coindcx-bot
3. Follow live logs:       journalctl -u coindcx-bot -f
4. Read app logs:          tail -f /var/log/coindcx-bot/bot.log
5. Create an audit file:   coindcx-export-audit
EOF
