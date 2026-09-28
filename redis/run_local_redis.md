# Run local Redis (Redis Stack on WSL)

Use **Redis Stack** in Ubuntu WSL so LangGraph checkpoints work (`FT._LIST` / RediSearch). Plain `redis-server` from Ubuntu packages is not enough.

From Windows, prefix commands with:

```powershell
wsl -d Ubuntu --
```

Or open **Ubuntu** and run the bash commands below directly.

---

## Start Redis Stack

**Preferred (systemd available in WSL):**

```bash
sudo systemctl start redis-stack-server
sudo systemctl enable redis-stack-server   # optional: start on boot
```

**If `systemctl` is not usable:**

```bash
sudo service redis-stack-server start
```

**Manual fallback (no systemd / service):**

```bash
sudo nohup /opt/redis-stack/bin/redis-stack-server --port 6379 >/tmp/redis-stack.log 2>&1 &
sleep 2
```

---

## Stop Redis Stack

```bash
sudo systemctl stop redis-stack-server
```

**Alternative:**

```bash
sudo service redis-stack-server stop
```

**If started manually with `nohup`:**

```bash
sudo pkill -f redis-stack-server
```

---

## Stop plain Redis (avoid port 6379 conflict)

If the old Ubuntu `redis-server` package is still installed, stop it before using Stack:

```bash
sudo service redis-server stop 2>/dev/null || true
sudo systemctl stop redis-server 2>/dev/null || true
sudo pkill -x redis-server 2>/dev/null || true
```

---

## Status

```bash
sudo systemctl status redis-stack-server
```

**Process check:**

```bash
pgrep -af redis-stack-server
ss -tlnp | grep 6379
```

---

## Ping and verify Stack (RediSearch)

```bash
redis-cli ping
```

Expected: `PONG`

```bash
redis-cli FT._LIST
```

Expected: `(empty array)` or a list of indexes — not `unknown command`.

**One-liner from Windows PowerShell:**

```powershell
wsl -d Ubuntu -- redis-cli ping
wsl -d Ubuntu -- redis-cli FT._LIST
```

---

## App configuration

In `.env` on Windows:

```env
REDIS_URL=redis://127.0.0.1:6379/0
```

WSL exposes port **6379** to Windows via `wslrelay`; restart workers (e.g. `do_work.py`) after changing `REDIS_URL`.

---

## First-time install

See [deploy/scripts/install_redis_stack_wsl.sh](../deploy/scripts/install_redis_stack_wsl.sh).

```bash
bash /mnt/c/Users/LENOVO/Documents/dermascanAgents/deploy/scripts/install_redis_stack_wsl.sh
```

(Adjust the path if your repo lives elsewhere.)

---

## Useful diagnostics

```bash
redis-cli info server | head -20
redis-cli dbsize
redis-cli config get maxmemory-policy
```

**Logs (manual start):**

```bash
tail -f /tmp/redis-stack.log
```

**Logs (systemd):**

```bash
sudo journalctl -u redis-stack-server -f
```
