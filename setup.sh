#!/usr/bin/env bash
#
# One-command setup for the Telegram sales bot on your droplet.
#
#   ./setup.sh              # install deps, scaffold .env, run preflight check
#   ./setup.sh --systemd    # also install + start the 24/7 systemd service
#
# Safe to re-run. Assumes Ollama is already installed and running.

set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"

INSTALL_SYSTEMD=0
[ "${1:-}" = "--systemd" ] && INSTALL_SYSTEMD=1

echo "▶ Setting up the sales bot in $HERE"

# ── 1. Python venv + dependencies ────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
  echo "✋ python3 not found. Install it first:  sudo apt install -y python3 python3-venv"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "▶ Creating virtualenv (.venv)…"
  python3 -m venv .venv
fi
echo "▶ Installing dependencies…"
./.venv/bin/pip install -q -U pip
./.venv/bin/pip install -q -r requirements.txt
echo "  ✅ dependencies installed"

# ── 2. .env scaffold ─────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  ✅ created .env from template"
  echo
  echo "  ⚠️  EDIT .env NOW and fill in at least:"
  echo "       TELEGRAM_BOT_TOKEN   (from @BotFather)"
  echo "       SELLER_CHAT_IDS      (your id from @userinfobot)"
  echo "       OLLAMA_MODEL         (must match a model you've pulled)"
  echo "       STRIPE_SECRET_KEY    (for autonomous checkout, optional)"
  echo "     then re-run:  ./setup.sh"
  echo
else
  echo "  ✅ .env already exists (leaving it as-is)"
fi

# ── 3. Preflight check ───────────────────────────────────────────────────────
echo "▶ Running preflight check…"
set +e
./.venv/bin/python run.py --check
CHECK=$?
set -e

# ── 4. (optional) systemd service ────────────────────────────────────────────
if [ "$INSTALL_SYSTEMD" = "1" ]; then
  if [ "$CHECK" != "0" ]; then
    echo "✋ Preflight failed — fix the ❌ items above before installing the service."
    exit 1
  fi
  RUN_USER="${SUDO_USER:-$USER}"
  UNIT=/etc/systemd/system/salesbot.service
  echo "▶ Installing systemd service as user '$RUN_USER'…"
  sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=Telegram Sales Bot (Ollama)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$HERE
ExecStart=$HERE/.venv/bin/python $HERE/run.py
Restart=on-failure
RestartSec=5
EnvironmentFile=$HERE/.env

[Install]
WantedBy=multi-user.target
UNITEOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now salesbot
  echo "  ✅ service installed and started"
  echo "     logs:    journalctl -u salesbot -f"
  echo "     restart: sudo systemctl restart salesbot"
  exit 0
fi

echo
if [ "$CHECK" = "0" ]; then
  echo "✅ Ready. Start the bot with:   ./.venv/bin/python run.py"
  echo "   Run it 24/7 with:            ./setup.sh --systemd"
else
  echo "⚠️  Fix the ❌ items above, then re-run ./setup.sh"
fi
