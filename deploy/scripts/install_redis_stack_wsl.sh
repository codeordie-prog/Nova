#!/usr/bin/env bash
# Install Redis Stack on Ubuntu WSL (RediSearch / FT.* for LangGraph checkpoints).
# Run inside Ubuntu: bash /mnt/c/Users/LENOVO/Documents/dermascanAgents/deploy/scripts/install_redis_stack_wsl.sh

set -euo pipefail

echo "==> 1. Stop plain Redis (Ubuntu package)"
sudo service redis-server stop 2>/dev/null || true
sudo systemctl stop redis-server 2>/dev/null || true
sudo systemctl disable redis-server 2>/dev/null || true

if pgrep -x redis-server >/dev/null 2>&1; then
  echo "Plain redis-server still running; stopping..."
  sudo pkill -x redis-server || true
  sleep 1
fi

echo "==> 2. Remove distro redis-server so it does not bind :6379 on boot"
if dpkg -l redis-server 2>/dev/null | grep -q '^ii'; then
  sudo apt-get remove -y redis-server
fi

echo "==> 3. Add Redis official APT repo"
sudo apt-get install -y lsb-release curl gpg
curl -fsSL https://packages.redis.io/gpg | sudo gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg
sudo chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg
CODENAME="$(lsb_release -cs)"
echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb ${CODENAME} main" | sudo tee /etc/apt/sources.list.d/redis.list
sudo apt-get update

install_stack() {
  sudo apt-get install -y redis-stack-server
}

echo "==> 4. Install redis-stack-server (Ubuntu ${CODENAME})"
if apt-cache show redis-stack-server &>/dev/null; then
  install_stack
else
  echo "Package not found for ${CODENAME}; trying jammy repo (common on 24.04)..."
  echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb jammy main" | sudo tee /etc/apt/sources.list.d/redis.list
  sudo apt-get update
  install_stack
fi

echo "==> 5. Start Redis Stack"
if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
  sudo systemctl enable redis-stack-server
  sudo systemctl start redis-stack-server
else
  sudo service redis-stack-server start 2>/dev/null || true
  if ! pgrep -f redis-stack-server >/dev/null 2>&1; then
    echo "Starting redis-stack-server in background (no systemd)..."
    sudo nohup /opt/redis-stack/bin/redis-stack-server --port 6379 >/tmp/redis-stack.log 2>&1 &
    sleep 2
  fi
fi

echo "==> 6. Verify RediSearch + PING"
redis-cli ping
redis-cli FT._LIST || { echo "FT._LIST failed — Stack may not be running"; exit 1; }

echo "Done. Use REDIS_URL=redis://127.0.0.1:6379/0 on Windows and restart do_work.py"
