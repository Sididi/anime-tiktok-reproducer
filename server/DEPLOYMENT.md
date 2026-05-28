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
curl -s http://127.0.0.1:38271/healthz | jq
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
curl -sI http://127.0.0.1:38271/healthz
docker compose ps
docker compose logs --tail 20
```

### Trouble: Discord ✅ reaction doesn't appear

The bot needs `Add Reactions` permission in the upload channel. Verify in Discord's channel settings → Integrations → Bots and Apps. Same for `Manage Messages` if you ever want it to delete its own posts.

---

## 11. Phase A — VPS upgrade (after the mobile-app drop)

After pulling Phase A (or main once Phase A is merged) and rebuilding:

1. Remove `ATR_MOBILE_TOKEN_*` lines from the VPS `.env` (orphan vars are harmless but tidier to clean up).
2. Optional: remove the `devices:` block from `config/config.yaml` (now ignored — `device:` on each account is now a free-form display label, not validated).
3. Restart: `cd /opt/tiktok/server && git pull && docker compose up -d --build`
4. Verify: `curl https://tiktok.sididi.tv/healthz` still returns OK.
5. Verify the mobile routes are gone: `curl -o /dev/null -w "%{http_code}\n" https://tiktok.sididi.tv/api/mobile/me` should print `404`.

---

## 12. Phase B — Instagram via VPS + reaction listener

Phase B introduces three behavioral changes that need attention at deploy time:

1. **`Job` model rename + new fields** — the on-disk schema in `data/jobs.json` is incompatible with the previous shape. **Wipe `jobs.json` on deploy.**
2. **Instagram publishing moves from n8n to the VPS scheduler** — at slot_time, the scheduler calls Meta Graph API directly. n8n integration is fully retired (you can shut down/remove your n8n workflow).
3. **Discord reaction listener** — the bot now connects to Discord's gateway WebSocket (in addition to its existing REST calls). It listens for `✅` reactions on the upload-channel embed OR the rich reminder, and uses that signal to mark TikTok manually-posted + cancel an unfired reminder.

### Pre-deploy checklist

**Confirm there are no in-flight pending jobs.** Quickest check from your dev machine:
```bash
ssh youruser@vps 'sudo cat /var/lib/docker/volumes/server_jobs-data/_data/jobs.json' | python3 -m json.tool
```
If `"jobs": {}` (or the file is missing), you're clear. Otherwise, finish or delete pending jobs first — they won't deserialize cleanly under the new schema.

**Discord bot permissions.** The bot needs the following on every channel where it operates (upload + reminder channels at minimum):
- `View Channel`
- `Send Messages`
- `Embed Links`
- `Add Reactions`
- `Read Message History`
- `Manage Messages` (so the listener can delete reminder messages on ack)

If you used a permission integer earlier without `Read Message History` or `Manage Messages`, update the bot role's permissions in your Discord server before deploying.

**Discord intents.** `discord.py` requires the `GUILD_MESSAGE_REACTIONS` intent. This is **not a privileged intent**, so it's available to all bots without TikTok-style review. The listener requests `discord.Intents.default()` + `reactions=True`. No portal config needed.

### Deploy steps

```bash
ssh youruser@vps
cd /opt/tiktok
git fetch origin
git checkout main      # or feat/phase-b-instagram if not yet merged
git pull
cd server

# Wipe old jobs.json (model rename = clean break)
docker compose down
docker volume rm $(docker compose config --volumes 2>/dev/null | head -1) 2>/dev/null || \
  sudo rm -f /var/lib/docker/volumes/*jobs-data*/_data/jobs.json

# Rebuild and start
docker compose up -d --build
docker compose logs --tail 50
```

You should see lines like:
```
INFO ... Scheduler started (interval=30.0s)
INFO ... ReactionListener gateway connection starting
INFO ... discord.client logged in as Tiktok Reproducer#XXXX
```

If the gateway fails to connect, check:
- The bot token is valid (`echo $ATR_DISCORD_BOT_TOKEN` from inside the container).
- The bot is invited to the guild and not banned.

### Verify the new flow

1. **Health check still works:**
   ```bash
   curl -s https://tiktok.sididi.tv/healthz | jq
   # Expected: {"status":"ok","jobs_pending":0}
   ```

2. **Process a real test project with Instagram in `platforms_requested`** (low-stakes, deletable). At video-upload time:
   - The embed appears in the upload channel — Instagram line shows `⏳ Instagram — Pending`.
   - **No** request is sent to n8n (you can verify by checking your n8n logs are silent).

3. **At slot_time** (verify in container logs `docker compose logs -f`):
   - For TikTok: the rich reminder + forward appear in the reminder channel.
   - For Instagram: `Instagram publish succeeded for <project_id>` log line. The embed's IG line transitions to `✅ Instagram — https://instagram.com/reel/...`.

4. **React with ✅ on the upload-channel embed** (yourself, from any phone). The bot:
   - Edits the embed: TikTok line becomes `✅ TikTok — Posté`.
   - Adds its own ✅ reaction (visual confirmation).
   - If reminder was already in the channel: deletes it.
   - If reminder hadn't fired yet: scheduler skips it on the next tick.

5. **Cascade delete still works** — `DELETE /api/internal/jobs/<pid>` removes the embed + any reminder messages.

### Instagram Meta prerequisites

Instagram publishing uses the Meta Instagram Graph API Reels content publishing flow from the VPS: create container, upload the local video bytes through `rupload.facebook.com`, poll the container until `FINISHED`, then call `media_publish`. If Meta reports a zero-byte/phase-error rupload ingest (`uploading_phase=error`, `bytes_transferred=0`), the publisher creates a fresh `video_url` container using the VPS `/api/videos/{project_id}` URL and polls that fallback container instead. The VPS persists the current container id, upload URI, upload method, last status, and publish stage in `jobs.json`; if a polling attempt times out, the next scheduler tick resumes that same unexpired healthy container instead of creating a fresh opaque upload.

Backend upload jobs pass `ATR_INSTAGRAM_PUBLISH_POLL_INTERVAL_SECONDS` and `ATR_INSTAGRAM_PUBLISH_TIMEOUT_SECONDS` into the VPS job payload. Defaults are 60 seconds between polls and a 4 hour per-attempt polling window.

Before testing a real publish, confirm:
- The Instagram account is a professional account.
- For the Facebook Login flow, the Instagram account is connected to a Facebook Page.
- The token can access the target IG user id and has `instagram_basic` and `instagram_content_publish`.
- The Facebook user/system user behind the token has the required Page tasks, especially `CREATE_CONTENT` or `MANAGE`.
- The video meets Meta Reels specs: MP4/MOV, H.264 or HEVC, AAC audio, 3 seconds to 15 minutes, max 300 MB, max width 1920px.

### Roll back

If anything breaks:
```bash
cd /opt/tiktok
git checkout <previous-main-sha>
cd server
docker compose up -d --build
```

The previous state still works for TikTok-only flow. Instagram via n8n was working before this; if you need IG to work during a rollback window, re-enable your n8n workflow + restore `ATR_N8N_WEBHOOK_URL` + `ATR_DISCORD_WEBHOOK_URL` in your dev `.env`.
