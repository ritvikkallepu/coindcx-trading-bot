# VPS logging

The bot writes to two durable stores when installed as a service:

- `/var/log/coindcx-bot/bot.log` plus compressed rotations
- persistent systemd journal entries for `coindcx-bot.service`

Daily audit archives are written to `/var/backups/coindcx-bot` and retained for
30 days. Archives include application logs and runtime state, but exclude dotenv
and secret files.

## Install

```bash
cd /opt/coindcx-trading-bot
chmod +x scripts/*.sh
./scripts/install_vps_service.sh
nano /etc/coindcx-bot/runtime.env
systemctl enable --now coindcx-bot
```

Installing does not start live trading. The explicit `systemctl enable --now`
command is required after reviewing `/etc/coindcx-bot/runtime.env`.

## Operate

```bash
systemctl status coindcx-bot
journalctl -u coindcx-bot -f
tail -f /var/log/coindcx-bot/bot.log
systemctl restart coindcx-bot
systemctl stop coindcx-bot
```

Create an audit archive at any time:

```bash
coindcx-export-audit
ls -lh /var/backups/coindcx-bot
```

Download the newest archive from PowerShell on the local computer:

```powershell
scp root@YOUR_VPS_IP:/var/backups/coindcx-bot/coindcx-audit-*.tgz $HOME\Downloads\
```
