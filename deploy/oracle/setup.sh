#!/usr/bin/env bash
# One-time setup of the always-on hedger (practice mode) on a fresh Oracle Cloud "Always Free" Ubuntu VM.
# Run on the VM as the default "ubuntu" user:
#   curl -fsSL https://raw.githubusercontent.com/nxtsgn/project-jontay-porter/main/deploy/oracle/setup.sh | bash
# Safe to re-run. Installs no API keys and sends no orders.
set -euo pipefail
REPO=https://github.com/nxtsgn/project-jontay-porter.git
PUSH=git@github.com:nxtsgn/project-jontay-porter.git
APP=$HOME/project-jontay-porter
DATA=$HOME/hedger-data

sudo apt-get update -qq
sudo apt-get install -y -qq git python3 >/dev/null
sudo timedatectl set-timezone UTC

# 1 GB VMs: add swap so git and Python never run out of memory
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# code (read-only clone of main; the hedger pulls updates itself every 30 min)
[ -d "$APP/.git" ] || git clone -q "$REPO" "$APP"
git -C "$APP" pull -q --ff-only origin main

# a key that lets this server push ONLY to this repo (GitHub "deploy key"); it never leaves the server
[ -f "$HOME/.ssh/hedger_deploy" ] || ssh-keygen -q -t ed25519 -N "" -C "jontay-hedger@oracle" -f "$HOME/.ssh/hedger_deploy"
grep -q "hedger_deploy" "$HOME/.ssh/config" 2>/dev/null || cat >> "$HOME/.ssh/config" <<CFG
Host github.com
  IdentityFile ~/.ssh/hedger_deploy
  IdentitiesOnly yes
CFG
ssh-keyscan -q github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null

# where the hedger's log is committed: its own branch, so it never collides with the price collector
if [ ! -d "$DATA/.git" ]; then
  git init -q "$DATA"
  git -C "$DATA" remote add origin "$PUSH"
  git -C "$DATA" checkout -q -b hedger-data
fi
git -C "$DATA" config user.name "hedger-bot"
git -C "$DATA" config user.email "hedger-bot@users.noreply.github.com"
git -C "$DATA" fetch -q origin hedger-data 2>/dev/null && git -C "$DATA" reset -q --hard origin/hedger-data || true

# run forever, restart on crash or code update, start on boot
sudo tee /etc/systemd/system/jontay-hedger.service >/dev/null <<UNIT
[Unit]
Description=Project Jontay Porter always-on hedger (practice mode, sends no orders)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP
ExecStart=/usr/bin/python3 $APP/scripts/hedger.py --data-dir $DATA
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now jontay-hedger >/dev/null
sudo systemctl restart jontay-hedger

echo
echo "Hedger installed and running. Last step, in GitHub (once):"
echo "  github.com/nxtsgn/project-jontay-porter -> Settings -> Deploy keys -> Add deploy key"
echo "  Title: oracle-hedger   Allow write access: YES   Key:"
echo
cat "$HOME/.ssh/hedger_deploy.pub"
echo
echo "Check it any time with:  journalctl -u jontay-hedger -f"
