# Phase A — Scrub Mobile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Remove the mobile React Native app, the VPS mobile API surface, and the mobile-specific bits of the VPS configuration, while keeping the device label visible on the Discord embed. After Phase A the system runs identically to today (TikTok manual via Discord reminder, Instagram via n8n) — just with ~3500 fewer lines of code.

**Architecture:** Pure deletion + small simplification. No new behavior. The `device:` field stays on `AccountConfig` in the main backend and continues to populate the embed's "📱 Device" line via the `device_id` field on jobs.

**Tech Stack:** Same as today. No new dependencies.

**Reference spec:** No new spec; the design lives in `docs/superpowers/specs/2026-04-26-mobile-tiktok-app-design.md` (sections 6-7-8 are now historical).

---

## File Structure

```
.                                    # repo root
├── mobile/                          # DELETE entirely (~3500 lines)
├── docs/superpowers/specs/
│   └── 2026-04-26-mobile-tiktok-app-design.md  # KEEP, historical
├── docs/superpowers/plans/
│   └── 2026-04-26-mobile-app.md     # KEEP, historical
├── server/
│   ├── app/
│   │   ├── api/mobile.py            # DELETE
│   │   ├── auth/dependencies.py     # MOD: drop require_device_token
│   │   ├── config.py                # MOD: drop devices block + _device_tokens
│   │   └── main.py                  # MOD: drop mobile_router include
│   ├── config/
│   │   └── config.example.yaml      # MOD: drop devices block
│   ├── .env.example                 # MOD: drop ATR_MOBILE_TOKEN_* lines
│   └── tests/
│       ├── test_mobile_api.py       # DELETE
│       ├── test_auth.py             # MOD: drop require_device_token tests
│       └── test_config.py           # MOD: drop device-token tests
└── (main backend unchanged in this phase)
```

---

## Task 1: Delete the `mobile/` tree

**Files:**
- Delete: `mobile/` (entire directory)

- [ ] **Step 1: Verify nothing on main references `mobile/`**

```bash
grep -rn "mobile/" --include="*.py" --include="*.ts" --include="*.tsx" --include="*.md" --include="*.yaml" --include="*.toml" backend/ server/ docs/ scripts/ 2>/dev/null | grep -v "node_modules\|.pixi\|/.worktrees/"
```
Expected: only references in `docs/superpowers/specs/` and `docs/superpowers/plans/` (historical docs we're keeping). Anything else needs cleanup before deletion.

- [ ] **Step 2: Delete the directory**

```bash
git rm -r mobile/
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: drop mobile React Native app (Phase A)"
```

---

## Task 2: Drop `/api/mobile/*` routes from VPS

**Files:**
- Delete: `server/app/api/mobile.py`
- Delete: `server/tests/test_mobile_api.py`
- Modify: `server/app/main.py` (drop the import + include_router call)

- [ ] **Step 1: Delete the route module + its tests**

```bash
git rm server/app/api/mobile.py server/tests/test_mobile_api.py
```

- [ ] **Step 2: Edit `server/app/main.py`**

Remove the line `from app.api.mobile import router as mobile_router` (top of file).
Remove the line `app.include_router(mobile_router)` inside `create_app()`.

- [ ] **Step 3: Verify the test suite still passes**

```bash
cd server && uv run pytest -v
```
Expected: all remaining tests pass (some count below today's 73).

- [ ] **Step 4: Commit**

```bash
git add server/app/main.py
git commit -m "refactor(server): drop /api/mobile/* routes"
```

---

## Task 3: Drop `require_device_token` and `_device_tokens` map

**Files:**
- Modify: `server/app/auth/dependencies.py` (drop `require_device_token`)
- Modify: `server/app/config.py` (drop `_device_tokens` field + token resolution in `Settings.load`)
- Modify: `server/tests/test_auth.py` (drop the 3 mobile tests)

- [ ] **Step 1: Edit `server/app/auth/dependencies.py`**

Delete the entire `require_device_token` function. Keep `require_internal_token` and `_bearer`. Drop the `import hmac` if it's no longer used elsewhere in the file (the `require_internal_token` function still uses it — verify by re-reading).

- [ ] **Step 2: Edit `server/app/config.py`**

In the `Settings` dataclass, delete the `_device_tokens: dict[str, str]` field.
Delete the `resolve_device_for_token` method.
In `Settings.load(...)`, delete the per-device-token lookup loop (the part that reads `ATR_MOBILE_TOKEN_<UPPER(device_id)>` env vars and builds `device_tokens`).

- [ ] **Step 3: Edit `server/tests/test_auth.py`**

Delete `test_mobile_route_returns_resolved_device` and `test_mobile_route_rejects_unknown_token`. Update the throwaway-app fixture to drop the `/mobile` route (delete those lines from the fixture).

Keep the 3 internal-route tests.

- [ ] **Step 4: Run the suite**

```bash
cd server && uv run pytest -v
```
Expected: passes.

- [ ] **Step 5: Commit**

```bash
git add server/app/auth/dependencies.py server/app/config.py server/tests/test_auth.py
git commit -m "refactor(server): drop require_device_token + per-device token map"
```

---

## Task 4: Drop `devices:` block from VPS config

**Files:**
- Modify: `server/app/config.py` (drop `Settings.devices` field + the validation)
- Modify: `server/config/config.example.yaml` (drop devices block)
- Modify: `server/tests/test_config.py` (drop devices-related tests)
- Modify: `server/app/services/embed_builder.py` (drop the unused `devices` parameter)
- Modify: `server/app/api/internal.py` (call site of `build_embed`)
- Modify: `server/app/api/health.py` (was iterating `settings.devices` — replace with iteration over jobs file directly)

- [ ] **Step 1: Edit `server/app/config.py`**

Drop the `DeviceConfig` dataclass entirely.
Drop the `devices: dict[str, DeviceConfig]` field on `Settings`.
In `Settings.load(...)`, drop the parsing of the `devices:` YAML block AND the validation that "every account's device exists in devices" (the device label is now opaque to the VPS — it's just a string).

- [ ] **Step 2: Edit `server/config/config.example.yaml`**

Delete the entire `devices:` block at the top. Update the comments accordingly. Accounts keep their `device:` field (it's a free-form label now, not validated).

- [ ] **Step 3: Edit `server/tests/test_config.py`**

Delete `test_account_device_must_exist_in_devices` (no longer applicable).
Delete the `s.devices["iphone_13_pro"].platform == "ios"` assertion in `test_load_minimal_valid_config`.

- [ ] **Step 4: Edit `server/app/services/embed_builder.py`**

Drop the `devices` parameter from `build_embed`'s signature. Drop the `_ = devices` line. Drop the `DeviceConfig` import.

- [ ] **Step 5: Edit `server/app/api/internal.py`**

Find the two `build_embed(...)` call sites (in `create_job` and in `platform_status`). Drop the `settings.devices` argument from both.

- [ ] **Step 6: Edit `server/app/api/health.py`**

The current implementation iterates `settings.devices` to count pending jobs across all devices. Replace with a direct read of the job store. Add a method `JobStore.count_pending_jobs() -> int` if needed, OR change `/healthz` to call `store.list_for_device("__any__", status="pending")` if that path makes sense. Cleanest: add a `JobStore.list_all() -> list[Job]` method, and have `/healthz` count from that.

Concrete change: rewrite the function as:

```python
@router.get("/healthz")
async def healthz(request: Request) -> dict:
    store = request.app.state.job_store
    all_jobs = await store.list_all()  # NEW method
    pending = sum(1 for j in all_jobs if j.status == "pending")
    return {"status": "ok", "jobs_pending": pending}
```

And add to `JobStore`:

```python
async def list_all(self) -> list[Job]:
    async with self._lock:
        jobs = self._read()
        return [Job.from_dict(d) for d in jobs.values()]
```

- [ ] **Step 7: Edit `server/tests/test_embed_builder.py`**

The fixtures pass a `devices` dict to `build_embed`. Drop that parameter. Drop the `_devices()` helper.

- [ ] **Step 8: Run the suite**

```bash
cd server && uv run pytest -v
```
Expected: passes.

- [ ] **Step 9: Commit**

```bash
git add server/app/config.py server/config/config.example.yaml \
        server/tests/test_config.py server/tests/test_embed_builder.py \
        server/app/services/embed_builder.py server/app/api/internal.py \
        server/app/api/health.py server/app/services/job_store.py
git commit -m "refactor(server): drop devices block + Settings.devices field"
```

---

## Task 5: Drop `ATR_MOBILE_TOKEN_*` from env example

**Files:**
- Modify: `server/.env.example`

- [ ] **Step 1: Edit `server/.env.example`**

Delete the lines:
```
ATR_MOBILE_TOKEN_IPHONE_13_PRO=replace_me
ATR_MOBILE_TOKEN_PIXEL_8=replace_me
```
And drop the `# Authn` group label if it now only contains `ATR_TIKTOK_SERVER_INTERNAL_TOKEN`.

- [ ] **Step 2: Commit**

```bash
git add server/.env.example
git commit -m "chore(server): drop ATR_MOBILE_TOKEN_* from env example"
```

---

## Task 6: Verify deployment-side cleanup

**Files:**
- Read-only audit; no changes (the user updates their VPS env separately).

- [ ] **Step 1: Note for the user**

After this branch deploys to the VPS, the operator should:
1. Remove `ATR_MOBILE_TOKEN_*` lines from the VPS's `.env` (orphan env vars are harmless, but cleaning up is tidy).
2. Remove the `devices:` block from the VPS's `config.yaml` (the slim config — now unused; if left in, will cause no error since the YAML loader simply ignores unknown top-level keys).

Document this in the commit message.

- [ ] **Step 2: Commit a deployment note in DEPLOYMENT.md** (the server one)

Add a section to `server/DEPLOYMENT.md`:

```markdown
## Phase A — VPS upgrade notes

After pulling this branch and rebuilding the container:
- Remove `ATR_MOBILE_TOKEN_*` lines from `.env` (no longer used).
- Optional: remove the `devices:` block from `config/config.yaml` (now ignored, leaving in is harmless).
- Restart: `docker compose up -d --build`.
- Verify: `curl https://tiktok.sididi.tv/healthz` still returns `{"status":"ok",...}`.
```

```bash
git add server/DEPLOYMENT.md
git commit -m "docs(server): Phase A upgrade notes"
```

---

## Self-Review Notes

After all 6 tasks:

1. **No `mobile/` directory anywhere in the working tree** (`ls -d mobile 2>/dev/null` should print nothing).
2. **`grep -rn "/api/mobile/" server/`** returns zero matches.
3. **`grep -rn "ATR_MOBILE_TOKEN" server/`** returns zero matches in source code (might appear in DEPLOYMENT.md as part of the upgrade notes — that's fine).
4. **`grep -rn "require_device_token\|_device_tokens\|resolve_device_for_token" server/`** returns zero matches.
5. **`grep -rn "DeviceConfig\|settings.devices" server/`** returns zero matches.
6. **`server/tests/`** has fewer test files; the remaining ones all pass.
7. **The Discord embed still shows the device line** — verified by `test_embed_builder.py::test_embed_inline_fields_include_device_and_project` (untouched, just receives `device_id` from the job dict).
8. **No regressions in main backend** — main backend was not modified in this phase.
