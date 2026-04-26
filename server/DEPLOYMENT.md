# Deployment Guide — Ubuntu 24.04 + nginx

End-to-end guide for deploying the `tiktok-server` to a fresh Ubuntu 24.04 VPS, fronted by nginx with a Let's Encrypt TLS cert.

**Target:** `https://tiktok.sididi.tv` resolved by your VPS's public IP.

**Prerequisite:** the DNS A (and AAAA, if you have IPv6) record for `tiktok.sididi.tv` already points to your VPS. Verify before starting:

```bash
dig +short tiktok.sididi.tv
# Should print your VPS's public IPv4
```

If DNS isn't ready, the certbot step at the end will fail.

---

## 1. Initial server hardening

SSH in as `root` (or whatever user your provider gave you). Replace `youruser` below with whatever username you want.

```bash
# Create a non-root user with sudo
adduser youruser
usermod -aG sudo youruser

# Copy your SSH authorized keys to the new user
mkdir -p /home/youruser/.ssh
cp ~/.ssh/authorized_keys /home/youruser/.ssh/
chown -R youruser:youruser /home/youruser/.ssh
chmod 700 /home/youruser/.ssh
chmod 600 /home/youruser/.ssh/authorized_keys

# Disable root SSH login (optional but recommended)
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl reload ssh
```

Log out and SSH back in as `youruser` from now on. Everything below uses `sudo`.

```bash
# Update everything
sudo apt update && sudo apt upgrade -y

# Firewall: allow SSH, HTTP, HTTPS only
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
sudo ufw status
```

---

## 2. Install Docker + Compose

```bash
# Remove any old docker packages from the Ubuntu archive (none exist on a fresh
# 24.04, but harmless if so).
sudo apt remove -y docker.io docker-doc docker-compose podman-docker containerd runc 2>/dev/null || true

# Docker's official apt repo
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Verify
docker --version
docker compose version

# Allow your user to run docker without sudo
sudo usermod -aG docker $USER
# Log out and back in (or run `newgrp docker`) for this to take effect
```

After re-login, confirm `docker ps` works without sudo.

---

## 3. Install nginx + certbot

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo systemctl enable --now nginx

# Confirm
sudo systemctl status nginx --no-pager
curl -sI http://127.0.0.1 | head -1
# HTTP/1.1 200 OK
```

---

## 4. Get the server code onto the VPS

You have three reasonable options. Pick one.

**Branch caveat (first deploy only).** The first time you deploy, the `server/` subtree may live on a feature branch (e.g. `feat/vps-server`) that hasn't been merged to `main` yet. The commands below default to `main`; if `main` doesn't yet contain `server/`, replace `main` with the relevant feature branch in the `git checkout` step. After the smoke test passes, merge the branch to `main` on your dev machine, push, and `git checkout main && git pull` on the VPS.

### Option A — Full clone of the monorepo (simplest)

```bash
sudo mkdir -p /opt
cd /opt
sudo git clone <git-url-of-anime-tiktok-reproducer> tiktok
sudo chown -R $USER:$USER tiktok
cd tiktok
git checkout main      # or feat/vps-server for the first deploy
cd server
```

Disk cost: the full project (modules, frontend, etc.) is on the VPS. Acceptable for personal use. The Docker build uses only `server/` as its build context, so the rest is dead weight on disk only.

### Option B — Sparse checkout of just `server/`

```bash
sudo mkdir -p /opt
cd /opt
sudo git clone --filter=blob:none --no-checkout <git-url-of-anime-tiktok-reproducer> tiktok
sudo chown -R $USER:$USER tiktok
cd tiktok
git sparse-checkout init --cone
git sparse-checkout set server
git checkout main      # or feat/vps-server for the first deploy
cd server
```

Disk cost: just `server/`. Updates via `git pull` work normally.

### Option C — Build the image elsewhere, push, pull

If you already have a container registry: build the image on your dev machine (`docker build -t registry.example/tiktok-server:vX.Y.Z .`), push, then `docker pull` on the VPS. Skip git on the VPS entirely. Worth it only if you don't want code on the VPS at all.

I'll assume Option A or B for the rest of the guide. Working dir is `/opt/tiktok/server`.

---

## 5. Configure secrets and config

```bash
cd /opt/tiktok/server

# .env: real Discord bot token, channel/role IDs, internal token, per-device tokens
cp .env.example .env
nano .env   # fill in real values
chmod 600 .env

# Slim VPS config
cp config/config.example.yaml config/config.yaml
nano config/config.yaml   # accounts + devices

# Avatar files: PNG/JPG per account
# Either commit them to the repo (they're already in server/avatars/ if you
# moved them in Plan B), or scp them in:
#   scp avatars/anime_fr.jpg youruser@vps:/opt/tiktok/server/avatars/
ls server/avatars/
```

**Key checklist for `.env`:**
- `ATR_TIKTOK_SERVER_INTERNAL_TOKEN` — generate a long random string (e.g., `openssl rand -hex 32`). Save the same value on your dev machine for the main backend's `ATR_TIKTOK_SERVER_INTERNAL_TOKEN`.
- `ATR_MOBILE_TOKEN_<DEVICE>` — one per device id, also random. You'll paste these into each phone's mobile-app settings later.
- `ATR_DISCORD_BOT_TOKEN` — from your Discord developer portal application's Bot tab.
- `ATR_DISCORD_GUILD_ID`, `ATR_DISCORD_UPLOAD_CHANNEL_ID`, `ATR_DISCORD_REMINDER_CHANNEL_ID`, `ATR_DISCORD_REMINDER_ROLE_ID` — right-click each in Discord (developer mode on) → Copy ID.
- `ATR_PUBLIC_BASE_URL=https://tiktok.sididi.tv`

**Key checklist for `config/config.yaml`:**
- One `devices` entry per phone you'll use. Device id should match the suffix in your `ATR_MOBILE_TOKEN_<DEVICE>` env vars (e.g., `iphone_13_pro` → `ATR_MOBILE_TOKEN_IPHONE_13_PRO`).
- One `accounts` entry per TikTok account, each with `device:` referencing a device id and `avatar:` matching a real file in `server/avatars/`.

---

## 6. Configure nginx

```bash
cd /opt/tiktok/server
sudo cp deploy/nginx-tiktok-server.conf /etc/nginx/sites-available/tiktok-server.conf

# If a default site exists and points at port 80, disable it (or leave it, it
# only catches requests not matching the server_name).
sudo rm -f /etc/nginx/sites-enabled/default

# Enable the site
sudo ln -s /etc/nginx/sites-available/tiktok-server.conf /etc/nginx/sites-enabled/

# Test config + reload
sudo nginx -t
sudo systemctl reload nginx
```

The site is now serving HTTP on port 80, but the upstream isn't running yet — `curl http://tiktok.sididi.tv/healthz` would 502. That's expected; we'll bring up the container next.

---

## 7. Bring up the container

```bash
cd /opt/tiktok/server
docker compose up -d --build

# Check it's running
docker compose ps
docker compose logs --tail 50
```

Verify the upstream is healthy:

```bash
curl -s http://127.0.0.1:8000/healthz | jq
# Expected: {"status":"ok","jobs_pending":0}

# And via nginx (still HTTP)
curl -s http://tiktok.sididi.tv/healthz | jq
# Same response.
```

If `docker compose logs` shows a `ConfigError` at startup, fix the relevant env var or YAML field, then `docker compose up -d --build` to recreate the container.

---

## 8. Provision Let's Encrypt cert

```bash
sudo certbot --nginx -d tiktok.sididi.tv
```

Certbot will:
- Verify domain control via the HTTP-01 challenge on your active nginx site
- Edit `/etc/nginx/sites-available/tiktok-server.conf` in place to add a `listen 443 ssl http2;` server block + an HTTP→HTTPS redirect
- Reload nginx

Pick option `2` ("Redirect — Make all requests redirect to secure HTTPS access") when prompted.

Confirm:

```bash
curl -sI https://tiktok.sididi.tv/healthz
# HTTP/2 200
# server: nginx/...

curl -s https://tiktok.sididi.tv/healthz | jq
# {"status":"ok","jobs_pending":0}
```

Auto-renewal is set up automatically by certbot's systemd timer (`certbot.timer`); confirm with:

```bash
systemctl list-timers certbot.timer
sudo certbot renew --dry-run
```

---

## 9. Smoke-test the full surface

Run the full curl checklist from `README.md` § "Smoke test (post-deploy)". Set the env vars at the top of that section to real values, then walk through steps 1-8. The expected results are:

1. `/healthz` → `{"status":"ok",…}`
2. Avatar serves with `image/jpeg` content-type
3. Mobile auth gate rejects unauthenticated, accepts the right token
4. Creating a fake job posts a Discord embed + reminder
5. Updating a platform status edits the embed in place
6. Mobile API lists the job, returns video URL, accepts ack (Discord reaction added)
7. Cascade delete removes the embed + reminder
8. Mobile job list is empty again

If any step fails, the most common causes are:
- Wrong/missing env vars (`docker compose logs` will say which one).
- Discord bot not in the guild, or missing `Send Messages` / `Add Reactions` perms in the configured channels.
- Channel/role IDs swapped or wrong.
- Avatar file not committed/uploaded to `server/avatars/`.

---

## 10. Day-2 ops

### Update flow (after a `git pull` or new commit)

```bash
cd /opt/tiktok/server
git pull
docker compose up -d --build
docker compose logs --tail 50
```

### Auto-start on reboot

`docker-compose.yml` already declares `restart: unless-stopped`. Combined with Docker's `docker.service` being `enabled` (it is by default after install), the container will start on boot. No systemd unit is required.

If you want extra belt-and-suspenders (e.g., to ensure the compose stack starts cleanly even if Docker doesn't pick up the project on its own), create a small unit:

```ini
# /etc/systemd/system/tiktok-server.service
[Unit]
Description=TikTok Server (docker compose)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/tiktok/server
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable tiktok-server.service
```

### Backups

The only stateful data is `data/jobs.json` in the named Docker volume `jobs-data`. Tiny (~kilobytes). A weekly tarball is plenty:

```bash
# /etc/cron.weekly/tiktok-server-backup (chmod +x)
#!/bin/bash
set -e
TS=$(date +%Y%m%d-%H%M)
BACKUP_DIR=/var/backups/tiktok-server
mkdir -p "$BACKUP_DIR"
docker run --rm -v jobs-data:/data -v "$BACKUP_DIR:/backup" alpine \
    tar czf /backup/jobs-$TS.tar.gz -C /data .
# Keep last 12 weeks
ls -1t "$BACKUP_DIR"/jobs-*.tar.gz | tail -n +13 | xargs -r rm
```

If you want off-VPS backups, scp the result to another host or push to S3-compatible storage in the same script.

### Logs

```bash
docker compose logs -f               # follow live
docker compose logs --tail 200       # last 200 lines
journalctl -u nginx                  # nginx access/error
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

### Rollback

```bash
cd /opt/tiktok/server
git log --oneline -10            # find the previous good commit
git checkout <prev-sha>
docker compose up -d --build
```

To restore from a backup:

```bash
docker compose down
docker run --rm -v jobs-data:/data -v /var/backups/tiktok-server:/backup alpine \
    sh -c 'cd /data && rm -rf * && tar xzf /backup/jobs-YYYYMMDD-HHMM.tar.gz'
docker compose up -d
```

### Trouble: container won't start

```bash
docker compose logs
# Look for ConfigError messages — they name the missing env var or YAML field
# explicitly. Fix and retry:
docker compose up -d --build
```

### Trouble: 502 from nginx

```bash
# Is the upstream up?
curl -sI http://127.0.0.1:8000/healthz
docker compose ps
docker compose logs --tail 20
```

### Trouble: Discord ✅ reaction doesn't appear

The bot needs `Add Reactions` permission in the upload channel. Verify in Discord's channel settings → Integrations → Bots and Apps. Same for `Manage Messages` if you ever want it to delete its own posts.

---

## 11. What to do next

After this deployment is verified end-to-end:

1. Save `ATR_TIKTOK_SERVER_INTERNAL_TOKEN` somewhere your dev machine can reach it (it's needed for Plan B).
2. Save each `ATR_MOBILE_TOKEN_<DEVICE>` somewhere you can paste it into a phone's app settings (needed for Plan C).
3. Merge the `feat/vps-server` branch to `main` once you're satisfied.
4. Move on to **Plan B** (main backend swap) — that work depends on this VPS being reachable from your dev machine.
