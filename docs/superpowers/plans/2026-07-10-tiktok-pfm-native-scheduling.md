# TikTok PFM Native Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish TikTok videos at their exact slot time by pre-staging media on Post for Me and using PFM's native `scheduled_at`, with a concurrent scheduler so same-slot jobs never serialize.

**Architecture:** The monolithic `publish_to_tiktok` splits into three phase functions (`stage_media_for_tiktok`, `create_tiktok_post`, `poll_tiktok_post_result`) driven by the VPS scheduler at per-phase due times: media staged as soon as the job exists, post created (with `scheduled_at = sched`) at `sched − 10 min`, results polled from `sched`. `dispatch_due_actions` fires each (job, platform) action as an `asyncio.Task` guarded by an in-memory in-flight registry. The backend edit-lock widens to 15 min so job data is frozen 5 min before the post is created (invariant: server lead ≤ backend lock).

**Tech Stack:** Python 3.12, FastAPI, httpx (+ `httpx.MockTransport` in tests), pytest + pytest-asyncio (`asyncio_mode = "auto"` on the server suite), JSON-file `JobStore`.

**Spec:** `docs/superpowers/specs/2026-07-10-tiktok-pfm-native-scheduling-design.md`

## Global Constraints

- `TIKTOK_SCHEDULE_LEAD_MINUTES = 10` (server) replaces `TIKTOK_LEAD_MINUTES = 10`.
- `TIKTOK_EDIT_LOCK_MINUTES = 15` (backend). Invariant: server lead **≤** backend lock (comments on both constants must state this — no longer equality).
- Late-job rule: if `sched − now < 60 s` at post-creation time, omit `scheduled_at` (instant publish, today's behaviour).
- Double-post guard unchanged: a live `post_id` (stage ≠ `"failed"`) is never re-created; a new post is only created after a definitive failed result, reusing `media_url`.
- PFM API key stays in server `.env` (`ATR_PFM_API_KEY`) — never in jobs.json.
- Server tests: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/ -q`
- Backend tests (scoped — main is NOT globally green, 38 pre-existing failures): `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run -e dev test tests/test_scheduling_service.py tests/test_scheduling_routes.py -q`
- Commit after every task with the trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
| --- | --- |
| `server/app/models/job.py` | `TikTokPublishState` gains `media_attempts` (quiet pre-window staging retry counter) and the `post_scheduled` stage value |
| `server/app/services/post_for_me_publisher.py` | Pure PFM API client: 3 phase functions + `delete_tiktok_post` + thin `publish_to_tiktok` composition (kept for instant-publish path & existing tests) |
| `server/app/services/reminder_scheduler.py` | Scheduling policy: per-phase due times, phase routing, attempts, concurrency (in-flight registry) |
| `server/app/api/internal.py` | `delete_job` additionally cancels a still-scheduled PFM post |
| `server/tests/conftest.py` | autouse fixture clearing the in-flight registry |
| `backend/app/services/scheduling_service.py` | `TIKTOK_EDIT_LOCK_MINUTES = 15` |

---

### Task 1: `TikTokPublishState.media_attempts` + `post_scheduled` stage

**Files:**
- Modify: `server/app/models/job.py:136-184`
- Test: `server/tests/test_job_model.py`

**Interfaces:**
- Produces: `TikTokPublishState(media_attempts: int = 0)` — used by Task 2 (incremented on staging failure) and Task 4 (quiet-retry logging). Stage docstring gains `post_scheduled`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_job_model.py`:

```python
def test_tiktok_publish_state_media_attempts_round_trip():
    state = TikTokPublishState(media_attempts=3, stage="post_scheduled")
    d = state.to_dict()
    assert d["media_attempts"] == 3
    restored = TikTokPublishState.from_dict(d)
    assert restored.media_attempts == 3
    assert restored.stage == "post_scheduled"


def test_tiktok_publish_state_media_attempts_defaults_to_zero_for_legacy_dicts():
    legacy = {"post_id": "sp_1", "stage": "post_created"}
    restored = TikTokPublishState.from_dict(legacy)
    assert restored.media_attempts == 0
```

(If the file doesn't already import `TikTokPublishState`, add `from app.models.job import TikTokPublishState` at the top.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_job_model.py -q`
Expected: 2 FAIL — `TypeError: ... unexpected keyword argument 'media_attempts'` and `AttributeError`/`KeyError`.

- [ ] **Step 3: Implement**

In `server/app/models/job.py`, inside `TikTokPublishState`:

1. Update the class docstring's stage comment line from
   `stage: str | None = None  # media_uploaded | post_created | published | failed` to
   `stage: str | None = None  # media_uploaded | post_scheduled | post_created | published | failed`
2. Add the field after `stage`:

```python
    media_attempts: int = 0
```

3. In `to_dict()`, add to the returned dict:

```python
            "media_attempts": self.media_attempts,
```

4. In `from_dict()`, add to the `cls(...)` call:

```python
            media_attempts=int(d.get("media_attempts", 0)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_job_model.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git add server/app/models/job.py server/tests/test_job_model.py
git commit -m "feat(server): media_attempts + post_scheduled stage on TikTokPublishState

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Split the publisher into phase functions

**Files:**
- Modify: `server/app/services/post_for_me_publisher.py:211-381` (replace `publish_to_tiktok` body; add 3 functions)
- Test: `server/tests/test_post_for_me_publisher.py`

**Interfaces:**
- Consumes: `TikTokPublishState.media_attempts` (Task 1).
- Produces (all return `TikTokPublishResult`; Task 4 imports the first three, Task 3 imports `delete_tiktok_post` — exact signatures):

```python
async def stage_media_for_tiktok(*, api_key: str, download_url: str,
    base_url: str = DEFAULT_BASE_URL, publish_state=None, temp_dir: Path | None = None,
    progress_callback: TikTokProgressCallback | None = None) -> TikTokPublishResult

async def create_tiktok_post(*, api_key: str, social_account_id: str, caption: str,
    privacy_status: str = "public", allow_comment: bool = True, allow_duet: bool = True,
    allow_stitch: bool = True, scheduled_at: datetime | None = None,
    base_url: str = DEFAULT_BASE_URL, publish_state=None,
    progress_callback: TikTokProgressCallback | None = None) -> TikTokPublishResult

async def poll_tiktok_post_result(*, api_key: str, social_account_id: str,
    base_url: str = DEFAULT_BASE_URL, poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS, publish_state=None,
    progress_callback: TikTokProgressCallback | None = None) -> TikTokPublishResult

async def delete_tiktok_post(*, api_key: str, post_id: str,
    base_url: str = DEFAULT_BASE_URL) -> None   # raises httpx.HTTPStatusError on non-404 error

async def publish_to_tiktok(...) -> TikTokPublishResult   # signature UNCHANGED (line 211)
```

Semantics: `stage_media_for_tiktok` no-ops (success) when state already has `media_url` or a live `post_id`; on failure increments `media_attempts` in the returned state. `create_tiktok_post` no-ops when a live `post_id` exists; fails with detail `"create_post: no staged media_url"` when `media_url` missing; sets stage `"post_scheduled"` when `scheduled_at` is not None, else `"post_created"`; serializes `scheduled_at` as `scheduled_at.astimezone(UTC).isoformat()` in the request body. `poll_tiktok_post_result` is the existing poll loop; fails with `"poll_results: no post to poll"` when `post_id` missing.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_post_for_me_publisher.py`. Also extend `FakePfm.handler` with a DELETE route — add this branch right before the final `return httpx.Response(404, ...)` line (line 58), and add `self.deleted_posts: list[str] = []` to `FakePfm.__init__`:

```python
        if url.startswith(f"{BASE}/social-posts/") and request.method == "DELETE":
            self.deleted_posts.append(url.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"success": True})
```

New tests (extend the existing `from app.services.post_for_me_publisher import ...` with `create_tiktok_post, delete_tiktok_post, poll_tiktok_post_result, stage_media_for_tiktok`):

```python
async def test_stage_media_uploads_and_sets_state(fake, tmp_path):
    result = await stage_media_for_tiktok(
        api_key="key", download_url="https://drive.example/video.mp4",
        temp_dir=tmp_path,
    )
    assert result.success is True
    assert result.publish_state.media_url == "https://media.example/abc.mp4"
    assert result.publish_state.stage == "media_uploaded"
    assert fake.upload_puts == [fake.video_bytes]
    assert fake.created_posts == []          # staging never creates a post


async def test_stage_media_noop_when_already_staged(fake, tmp_path):
    state = TikTokPublishState(media_url="https://media.example/abc.mp4",
                               stage="media_uploaded")
    result = await stage_media_for_tiktok(
        api_key="key", download_url="https://drive.example/video.mp4",
        publish_state=state, temp_dir=tmp_path,
    )
    assert result.success is True
    assert fake.upload_puts == []            # no re-download / re-upload


async def test_stage_media_failure_increments_media_attempts(fake, tmp_path):
    fake.fail_upload = True
    result = await stage_media_for_tiktok(
        api_key="key", download_url="https://drive.example/video.mp4",
        temp_dir=tmp_path,
    )
    assert result.success is False
    assert "upload" in result.detail
    assert result.publish_state.media_attempts == 1
    again = await stage_media_for_tiktok(
        api_key="key", download_url="https://drive.example/video.mp4",
        publish_state=result.publish_state, temp_dir=tmp_path,
    )
    assert again.publish_state.media_attempts == 2


async def test_create_post_with_scheduled_at_sends_iso_and_sets_stage(fake, tmp_path):
    when = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
    state = TikTokPublishState(media_url="https://media.example/abc.mp4",
                               stage="media_uploaded")
    result = await create_tiktok_post(
        api_key="key", social_account_id="spc_1", caption="cap",
        scheduled_at=when, publish_state=state,
    )
    assert result.success is True
    assert result.publish_state.post_id == "post_1"
    assert result.publish_state.stage == "post_scheduled"
    assert fake.created_posts[0]["scheduled_at"] == "2026-07-15T20:00:00+00:00"


async def test_create_post_instant_omits_scheduled_at(fake, tmp_path):
    state = TikTokPublishState(media_url="https://media.example/abc.mp4",
                               stage="media_uploaded")
    result = await create_tiktok_post(
        api_key="key", social_account_id="spc_1", caption="cap",
        publish_state=state,
    )
    assert result.success is True
    assert result.publish_state.stage == "post_created"
    assert "scheduled_at" not in fake.created_posts[0]


async def test_create_post_without_media_fails(fake, tmp_path):
    result = await create_tiktok_post(
        api_key="key", social_account_id="spc_1", caption="cap",
        publish_state=None,
    )
    assert result.success is False
    assert "no staged media_url" in result.detail
    assert fake.created_posts == []


async def test_create_post_noop_when_live_post_exists(fake, tmp_path):
    state = TikTokPublishState(post_id="post_9", stage="post_scheduled",
                               media_url="https://media.example/abc.mp4")
    result = await create_tiktok_post(
        api_key="key", social_account_id="spc_1", caption="cap",
        publish_state=state,
    )
    assert result.success is True
    assert fake.created_posts == []          # double-post guard


async def test_poll_result_publishes_scheduled_post(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"url": "https://tiktok.com/@a/video/1"}, "error": None}],
    ]
    state = TikTokPublishState(post_id="post_1", stage="post_scheduled",
                               media_url="https://media.example/abc.mp4")
    result = await poll_tiktok_post_result(
        api_key="key", social_account_id="spc_1",
        poll_interval=0.0, poll_timeout=1.0, publish_state=state,
    )
    assert result.success is True
    assert result.url == "https://tiktok.com/@a/video/1"
    assert result.publish_state.stage == "published"


async def test_poll_result_without_post_id_fails(fake, tmp_path):
    result = await poll_tiktok_post_result(
        api_key="key", social_account_id="spc_1",
        poll_interval=0.0, poll_timeout=1.0, publish_state=None,
    )
    assert result.success is False
    assert "no post to poll" in result.detail


async def test_delete_post_calls_delete_endpoint(fake, tmp_path):
    await delete_tiktok_post(api_key="key", post_id="post_1")
    assert fake.deleted_posts == ["post_1"]
```

- [ ] **Step 2: Run tests to verify the new ones fail and old ones still pass**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_post_for_me_publisher.py -q`
Expected: new tests FAIL with `ImportError` (names don't exist); all pre-existing tests PASS.

- [ ] **Step 3: Implement the split**

In `server/app/services/post_for_me_publisher.py`, replace everything from `async def publish_to_tiktok(` (line 211) to the end of the file with:

```python
def _live_post_id(state: TikTokPublishState | None) -> str | None:
    """post_id of an existing non-failed post, else None (failed → recreate)."""
    if state and state.post_id and state.stage != "failed":
        return state.post_id
    return None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None), follow_redirects=True
    )


async def stage_media_for_tiktok(
    *,
    api_key: str,
    download_url: str,
    base_url: str = DEFAULT_BASE_URL,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    temp_dir: Path | None = None,
    progress_callback: TikTokProgressCallback | None = None,
) -> TikTokPublishResult:
    """Phase 1: Drive download → PFM storage upload. Idempotent: no-ops when
    media is already staged or a live post exists. On failure, increments
    media_attempts in the returned state (quiet pre-window retry counter)."""
    state = _coerce_state(publish_state)
    if state and (state.media_url or _live_post_id(state)):
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)
    async with _client() as client:
        try:
            media_url = await _upload_media(
                client,
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                download_url=download_url,
                temp_dir=temp_dir,
            )
        except httpx.HTTPStatusError as e:
            detail = _stage_detail("upload", _response_detail(e.response))
            state = replace(
                state or TikTokPublishState(),
                media_attempts=(state.media_attempts if state else 0) + 1,
                last_error=detail,
            )
            return TikTokPublishResult(success=False, detail=detail, publish_state=state)
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as e:
            detail = _stage_detail("upload", f"{type(e).__name__}: {e}")
            state = replace(
                state or TikTokPublishState(),
                media_attempts=(state.media_attempts if state else 0) + 1,
                last_error=detail,
            )
            return TikTokPublishResult(success=False, detail=detail, publish_state=state)
    state = replace(
        state or TikTokPublishState(),
        media_url=media_url,
        stage="media_uploaded",
        created_at=_utc_now(),
        last_error=None,
    )
    await _emit_progress(progress_callback, state)
    return TikTokPublishResult(success=True, publish_state=state)


async def create_tiktok_post(
    *,
    api_key: str,
    social_account_id: str,
    caption: str,
    privacy_status: str = "public",
    allow_comment: bool = True,
    allow_duet: bool = True,
    allow_stitch: bool = True,
    scheduled_at: datetime | None = None,
    base_url: str = DEFAULT_BASE_URL,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    progress_callback: TikTokProgressCallback | None = None,
) -> TikTokPublishResult:
    """Phase 2: create the social post. With scheduled_at, PFM publishes
    server-side at that instant (stage "post_scheduled"); without it the
    publish starts immediately (stage "post_created"). Idempotent on a live
    post_id; requires staged media."""
    state = _coerce_state(publish_state)
    if state and state.stage == "published":
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)
    if _live_post_id(state):
        return TikTokPublishResult(success=True, publish_state=state)
    media_url = state.media_url if state else None
    if not media_url:
        return TikTokPublishResult(
            success=False,
            detail=_stage_detail("create_post", "no staged media_url"),
            publish_state=state,
        )
    body: dict[str, Any] = {
        "caption": caption,
        "social_accounts": [social_account_id],
        "media": [{"url": media_url}],
        "platform_configurations": {
            "tiktok": {
                "privacy_status": privacy_status,
                "allow_comment": allow_comment,
                "allow_duet": allow_duet,
                "allow_stitch": allow_stitch,
            }
        },
    }
    if scheduled_at is not None:
        body["scheduled_at"] = scheduled_at.astimezone(UTC).isoformat()
    async with _client() as client:
        try:
            create = await client.post(
                f"{base_url.rstrip('/')}/social-posts",
                headers=_headers(api_key),
                json=body,
            )
            create.raise_for_status()
            post_id = str(_unwrap(create.json())["id"])
        except httpx.HTTPStatusError as e:
            return TikTokPublishResult(
                success=False,
                detail=_stage_detail("create_post", _response_detail(e.response)),
                publish_state=state,
            )
        except (httpx.HTTPError, KeyError, ValueError) as e:
            return TikTokPublishResult(
                success=False,
                detail=_stage_detail("create_post", f"{type(e).__name__}: {e}"),
                publish_state=state,
            )
    state = replace(
        state or TikTokPublishState(),
        post_id=post_id,
        stage="post_scheduled" if scheduled_at is not None else "post_created",
        last_error=None,
    )
    await _emit_progress(progress_callback, state)
    logger.info(
        "PFM post created social_account_id=%s post_id=%s scheduled_at=%s",
        social_account_id, post_id,
        scheduled_at.isoformat() if scheduled_at else "instant",
    )
    return TikTokPublishResult(success=True, publish_state=state)


async def poll_tiktok_post_result(  # noqa: PLR0911, PLR0912
    *,
    api_key: str,
    social_account_id: str,
    base_url: str = DEFAULT_BASE_URL,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    progress_callback: TikTokProgressCallback | None = None,
) -> TikTokPublishResult:
    """Phase 3: poll social-post-results until TikTok reports the outcome."""
    state = _coerce_state(publish_state)
    if state and state.stage == "published":
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)
    post_id = state.post_id if state else None
    if not post_id:
        return TikTokPublishResult(
            success=False,
            detail=_stage_detail("poll_results", "no post to poll"),
            publish_state=state,
        )
    started = time.monotonic()
    async with _client() as client:
        elapsed = 0.0
        while True:
            try:
                results_resp = await client.get(
                    f"{base_url.rstrip('/')}/social-post-results",
                    headers=_headers(api_key),
                    params={"post_id": post_id},
                )
                results_resp.raise_for_status()
                payload = results_resp.json()
            except httpx.HTTPError as e:
                detail = (
                    _response_detail(e.response)
                    if isinstance(e, httpx.HTTPStatusError)
                    else f"{type(e).__name__}: {e}"
                )
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("poll_results", detail),
                    publish_state=state,
                )
            results = payload.get("data") if isinstance(payload, dict) else None
            state = replace(state, last_polled_at=_utc_now())
            if isinstance(results, list) and results:
                result = next(
                    (
                        r for r in results
                        if isinstance(r, dict)
                        and r.get("social_account_id") == social_account_id
                    ),
                    results[0],
                )
                if result.get("success"):
                    platform_data = result.get("platform_data") or {}
                    url = _derive_tiktok_video_url(platform_data) or platform_data.get("url")
                    state = replace(state, stage="published", url=url)
                    await _emit_progress(progress_callback, state)
                    logger.info(
                        "PFM TikTok publish succeeded post_id=%s url=%s "
                        "platform_data=%s elapsed=%.1fs",
                        post_id, url, platform_data, time.monotonic() - started,
                    )
                    return TikTokPublishResult(
                        success=True, url=url, publish_state=state
                    )
                detail = _result_error_detail(result)
                state = replace(state, stage="failed", last_error=detail)
                await _emit_progress(progress_callback, state)
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("result", detail),
                    publish_state=state,
                )
            await _emit_progress(progress_callback, state)
            if elapsed >= poll_timeout:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail(
                        "poll_results",
                        f"timeout after {int(poll_timeout)}s; "
                        f"post_id={post_id}; resumable=true",
                    ),
                    publish_state=state,
                )
            await asyncio.sleep(poll_interval)
            elapsed += max(poll_interval, 0.001)


async def delete_tiktok_post(
    *, api_key: str, post_id: str, base_url: str = DEFAULT_BASE_URL
) -> None:
    """Cancel a scheduled post. 404 (already gone) is treated as success."""
    async with _client() as client:
        response = await client.delete(
            f"{base_url.rstrip('/')}/social-posts/{post_id}",
            headers=_headers(api_key),
        )
        if response.status_code == 404:
            return
        response.raise_for_status()


async def publish_to_tiktok(
    *,
    api_key: str,
    social_account_id: str,
    caption: str,
    download_url: str,
    privacy_status: str = "public",
    allow_comment: bool = True,
    allow_duet: bool = True,
    allow_stitch: bool = True,
    base_url: str = DEFAULT_BASE_URL,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    progress_callback: TikTokProgressCallback | None = None,
    temp_dir: Path | None = None,
) -> TikTokPublishResult:
    """Instant-publish composition of the three phases (stage → create → poll).

    Kept for the late-job path and API compatibility; the scheduler drives the
    phases individually so each gets its own due time."""
    state = _coerce_state(publish_state)
    if state and state.stage == "published":
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)
    staged = await stage_media_for_tiktok(
        api_key=api_key, download_url=download_url, base_url=base_url,
        publish_state=state, temp_dir=temp_dir, progress_callback=progress_callback,
    )
    if not staged.success:
        return staged
    created = await create_tiktok_post(
        api_key=api_key, social_account_id=social_account_id, caption=caption,
        privacy_status=privacy_status, allow_comment=allow_comment,
        allow_duet=allow_duet, allow_stitch=allow_stitch, scheduled_at=None,
        base_url=base_url, publish_state=staged.publish_state,
        progress_callback=progress_callback,
    )
    if not created.success:
        return created
    return await poll_tiktok_post_result(
        api_key=api_key, social_account_id=social_account_id, base_url=base_url,
        poll_interval=poll_interval, poll_timeout=poll_timeout,
        publish_state=created.publish_state, progress_callback=progress_callback,
    )
```

Also update the module docstring's flow description (lines 3-8) to:

```
Flow (three phases, driven by the scheduler at separate due times):
  1. stage_media_for_tiktok: GET download_url → POST media/create-upload-url
     → PUT binary (as soon as the job exists on the VPS)
  2. create_tiktok_post: POST social-posts with scheduled_at = slot
     (at slot − TIKTOK_SCHEDULE_LEAD_MINUTES; PFM fires server-side at slot)
  3. poll_tiktok_post_result: GET social-post-results (from slot)
publish_to_tiktok composes all three for instant publishing (late jobs).
```

Note: keep every existing helper above line 211 (`_upload_media`, `_derive_tiktok_video_url`, `_coerce_state`, `_emit_progress`, `_headers`, `_stage_detail`, `_response_detail`, `_unwrap`, `_result_error_detail`, `_utc_now`, `_put_file_sync`, `_download_video`, constants) unchanged.

- [ ] **Step 4: Run the full publisher suite**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_post_for_me_publisher.py -q`
Expected: ALL PASS (pre-existing tests exercise `publish_to_tiktok` through the composition; new tests exercise the phases).

- [ ] **Step 5: Commit**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git add server/app/services/post_for_me_publisher.py server/tests/test_post_for_me_publisher.py
git commit -m "refactor(server): split PFM publisher into stage/create/poll phases + delete

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Cancel a scheduled PFM post on job deletion

**Files:**
- Modify: `server/app/api/internal.py:307-340` (`delete_job`)
- Test: `server/tests/test_internal_api.py`

**Interfaces:**
- Consumes: `delete_tiktok_post(api_key=..., post_id=..., base_url=...)` (Task 2); `TikTokPublishState` stage `"post_scheduled"` (Task 1).

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_internal_api.py` (it already defines `_make_app`, `JOB_PAYLOAD`, `INTERNAL_AUTH`; add `from unittest.mock import AsyncMock` only if not present — it is at line 7):

```python
def test_delete_job_cancels_scheduled_pfm_post(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState

    monkeypatch.setenv("ATR_PFM_API_KEY", "key")   # BEFORE app creation: Settings
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    deleted = AsyncMock()                          # may be frozen, patch via env
    monkeypatch.setattr("app.api.internal.delete_tiktok_post", deleted)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 201
        asyncio.get_event_loop().run_until_complete(
            app.state.job_store.set_tiktok_publish_state(
                "p1",
                TikTokPublishState(post_id="sp_X", stage="post_scheduled"),
            )
        )
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
        assert r.status_code == 204
    deleted.assert_awaited_once()
    assert deleted.await_args.kwargs["post_id"] == "sp_X"


def test_delete_job_ignores_pfm_delete_failure(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState

    monkeypatch.setenv("ATR_PFM_API_KEY", "key")
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    deleted = AsyncMock(side_effect=RuntimeError("pfm down"))
    monkeypatch.setattr("app.api.internal.delete_tiktok_post", deleted)
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        asyncio.get_event_loop().run_until_complete(
            app.state.job_store.set_tiktok_publish_state(
                "p1",
                TikTokPublishState(post_id="sp_X", stage="post_scheduled"),
            )
        )
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
        assert r.status_code == 204          # deletion proceeds despite PFM error
```

Note for the implementer: if `asyncio.get_event_loop().run_until_complete(...)` conflicts with `TestClient`'s loop on this pytest-asyncio version, follow whatever pattern the neighbouring tests in this file use to seed store state — the assertion targets are what matter. If existing tests never seed state, use `asyncio.run(...)` **before** entering the `TestClient` context (create the job via a first `with TestClient(app)` block, then seed, then re-enter).

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_internal_api.py -q -k pfm`
Expected: FAIL — `AttributeError: module 'app.api.internal' has no attribute 'delete_tiktok_post'`.

- [ ] **Step 3: Implement**

In `server/app/api/internal.py`:

1. Add to the imports: `from app.services.post_for_me_publisher import delete_tiktok_post`
2. In `delete_job` (line 308), insert right before `await store.delete(project_id)` (line 340):

```python
    state = job.tiktok_publish_state
    if (
        state is not None
        and state.post_id
        and state.stage == "post_scheduled"
        and settings.pfm_api_key
    ):
        try:
            await delete_tiktok_post(
                api_key=settings.pfm_api_key,
                post_id=state.post_id,
                base_url=settings.pfm_base_url,
            )
        except Exception as e:
            logger.warning(
                "PFM scheduled-post delete failed for %s (post_id=%s): %s",
                project_id, state.post_id, e,
            )
```

- [ ] **Step 4: Run the internal API suite**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_internal_api.py -q`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git add server/app/api/internal.py server/tests/test_internal_api.py
git commit -m "feat(server): cancel still-scheduled PFM post when a job is deleted

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Phased TikTok dispatch in the scheduler (still sequential)

**Files:**
- Modify: `server/app/services/reminder_scheduler.py:33-194` (imports, constants, `_platform_due_time`, `_dispatch_tiktok_publish`)
- Test: `server/tests/test_reminder_scheduler.py`

**Interfaces:**
- Consumes: `stage_media_for_tiktok`, `create_tiktok_post`, `poll_tiktok_post_result` (Task 2); `media_attempts` (Task 1).
- Produces (Task 5 and tests rely on these exact names):
  - `TIKTOK_SCHEDULE_LEAD_MINUTES = 10` (module constant; `TIKTOK_LEAD_MINUTES` is deleted)
  - `_TT_INSTANT_PUBLISH_CUTOFF_SECONDS = 60`
  - `_platform_due_time(job, platform) -> datetime` — phase-aware for tiktok
  - `_dispatch_tiktok_publish(job, store, settings, discord) -> bool` — same signature, phase-routing body
  - `_record_tiktok_failure(job, store, settings, discord, *, attempts, detail) -> None`

- [ ] **Step 1: Rewrite the TikTok scheduler tests**

In `server/tests/test_reminder_scheduler.py`:

1. Change the import block (lines 22-27) to:

```python
from app.services.reminder_scheduler import (
    TIKTOK_SCHEDULE_LEAD_MINUTES,
    _platform_due_time,
    dispatch_due_actions,
    run_scheduler_loop,
)
```

2. Add a phase-patching helper after `_tiktok_job` (line 121):

```python
def _ok_state(**kw):
    defaults = dict(media_url="https://media.example/abc.mp4", stage="media_uploaded")
    defaults.update(kw)
    return TikTokPublishState(**defaults)


def _patch_phases(monkeypatch, *, stage=None, create=None, poll=None):
    """Patch the three publisher phases in the scheduler namespace.
    Unspecified phases succeed with a sensible state progression."""
    calls: dict[str, list[dict]] = {"stage": [], "create": [], "poll": []}

    async def default_stage(**kwargs):
        calls["stage"].append(kwargs)
        return TikTokPublishResult(success=True, publish_state=_ok_state())

    async def default_create(**kwargs):
        calls["create"].append(kwargs)
        scheduled = kwargs.get("scheduled_at") is not None
        return TikTokPublishResult(
            success=True,
            publish_state=_ok_state(
                post_id="post_1",
                stage="post_scheduled" if scheduled else "post_created",
            ),
        )

    async def default_poll(**kwargs):
        calls["poll"].append(kwargs)
        return TikTokPublishResult(
            success=True,
            url="https://tiktok.com/@a/video/1",
            publish_state=_ok_state(post_id="post_1", stage="published",
                                    url="https://tiktok.com/@a/video/1"),
        )

    monkeypatch.setattr(
        "app.services.reminder_scheduler.stage_media_for_tiktok", stage or default_stage
    )
    monkeypatch.setattr(
        "app.services.reminder_scheduler.create_tiktok_post", create or default_create
    )
    monkeypatch.setattr(
        "app.services.reminder_scheduler.poll_tiktok_post_result", poll or default_poll
    )
    return calls
```

3. Rewrite the existing TikTok dispatch tests that monkeypatch `publish_to_tiktok` (they patch `"app.services.reminder_scheduler.publish_to_tiktok"`, which no longer exists). Replacements:

```python
async def test_dispatch_tiktok_happy_path(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Past-due job with no state runs all three phases in one dispatch
    (instant publish: slot already passed)."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    await store.create(_tiktok_job())
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert actions == 1
    job = await store.get("p1")
    assert job.platform_statuses["tiktok"].status == "uploaded"
    assert job.platform_statuses["tiktok"].url == "https://tiktok.com/@a/video/1"
    assert job.tiktok_publish_state.stage == "published"
    assert len(calls["stage"]) == 1
    assert calls["create"][0]["scheduled_at"] is None      # late job → instant
    assert calls["create"][0]["social_account_id"] == "spc_1"
    assert calls["create"][0]["caption"] == "cap"
    assert calls["stage"][0]["download_url"] == job.drive_video_url
    assert len(calls["poll"]) == 1
```

`test_dispatch_tiktok_missing_payload_skips` and
`test_dispatch_tiktok_skipped_status_missing_payload_no_warning`: unchanged
(no publish patch involved).

```python
async def test_dispatch_tiktok_missing_api_key_counts_attempt(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key=None
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    await store.create(_tiktok_job())          # slot 1 min in the past → in-window
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    job = await store.get("p1")
    tt = job.platform_statuses["tiktok"]
    assert tt.status == "pending"
    assert tt.attempts == 1
    assert "ATR_PFM_API_KEY" in tt.detail


async def test_dispatch_tiktok_fails_after_max_attempts_and_pings(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def failing_create(**kwargs):
        return TikTokPublishResult(success=False, detail="create_post: HTTP 400")

    _patch_phases(monkeypatch, create=failing_create)
    await store.create(_tiktok_job())
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="pending", attempts=4)
    )
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "failed"
    assert updated.platform_statuses["tiktok"].attempts == 5
    contents = [
        str(kwargs.get("content") or (args[1] if len(args) > 1 else ""))
        for args, kwargs in discord.post_message.call_args_list
    ]
    assert any("TikTok" in c for c in contents)
```

`test_dispatch_tiktok_terminal_statuses_are_not_retried`: replace the
`fake_publish` monkeypatch with `calls = _patch_phases(monkeypatch)` and the
final assertion with `assert calls["stage"] == [] and calls["create"] == []`.

```python
async def test_dispatch_tiktok_resumes_uploading_after_crash(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """'uploading' + live post_id + past slot → re-dispatch goes straight to
    polling; the persisted post_id is the double-post protection."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    job = _tiktok_job()
    job.tiktok_publish_state = TikTokPublishState(post_id="post_7", stage="post_created")
    await store.create(job)
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="uploading", attempts=1)
    )
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    assert calls["create"] == []                        # no second post
    assert calls["poll"][0]["publish_state"].post_id == "post_7"
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "uploaded"
    assert updated.platform_statuses["tiktok"].attempts == 2
```

Delete `test_dispatch_tiktok_passes_publish_state_for_resume` (now covered by
the crash-resume test's `publish_state` assertion).

4. Replace the three lead-time tests (lines ~750-770) with:

```python
def test_tiktok_media_staging_due_on_arrival():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    assert job.tiktok_publish_state is None
    assert _platform_due_time(job, "tiktok") == job.created_at


def test_tiktok_post_creation_due_at_lead():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    job.tiktok_publish_state = _ok_state()             # media staged
    assert TIKTOK_SCHEDULE_LEAD_MINUTES == 10
    assert _platform_due_time(job, "tiktok") == slot - timedelta(minutes=10)


def test_tiktok_poll_due_at_slot_once_post_exists():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    job.tiktok_publish_state = _ok_state(post_id="post_1", stage="post_scheduled")
    assert _platform_due_time(job, "tiktok") == slot


def test_tiktok_failed_post_due_at_lead_for_recreate():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    job.tiktok_publish_state = _ok_state(post_id="post_old", stage="failed")
    assert _platform_due_time(job, "tiktok") == slot - timedelta(minutes=10)


def test_instagram_due_time_has_no_lead():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"instagram": slot}
    assert _platform_due_time(job, "instagram") == slot


def test_tiktok_due_does_not_mutate_stored_time():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    _platform_due_time(job, "tiktok")
    assert job.platform_scheduled_at["tiktok"] == slot
```

5. Add new phase-behaviour tests:

```python
async def test_tiktok_media_staged_on_arrival_then_waits(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Job far from its slot: dispatch stages media, then stops (no post)."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    await store.create(_tiktok_job(slot_offset_minutes=120))
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert actions == 1
    assert len(calls["stage"]) == 1
    assert calls["create"] == []
    job = await store.get("p1")
    assert job.tiktok_publish_state.stage == "media_uploaded"
    assert job.platform_statuses["tiktok"].status == "pending"
    assert job.platform_statuses["tiktok"].attempts == 0   # staging is attempt-free


async def test_tiktok_staging_failure_before_window_is_quiet(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def failing_stage(**kwargs):
        prior = kwargs.get("publish_state")
        n = (prior.media_attempts if prior else 0) + 1
        return TikTokPublishResult(
            success=False, detail="upload: boom",
            publish_state=TikTokPublishState(media_attempts=n, last_error="upload: boom"),
        )

    _patch_phases(monkeypatch, stage=failing_stage)
    await store.create(_tiktok_job(slot_offset_minutes=120))
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    job = await store.get("p1")
    assert job.platform_statuses["tiktok"].status == "pending"
    assert job.platform_statuses["tiktok"].attempts == 0   # quiet: no attempts burned
    assert job.tiktok_publish_state.media_attempts == 2
    discord.post_message.assert_not_called()


async def test_tiktok_staging_failure_inside_window_counts_attempts(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def failing_stage(**kwargs):
        return TikTokPublishResult(success=False, detail="upload: boom")

    _patch_phases(monkeypatch, stage=failing_stage)
    await store.create(_tiktok_job(slot_offset_minutes=5))   # inside sched−10
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    job = await store.get("p1")
    assert job.platform_statuses["tiktok"].status == "pending"
    assert job.platform_statuses["tiktok"].attempts == 1


async def test_tiktok_scheduled_create_inside_window(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Slot 5 min out, media staged → create with scheduled_at=sched, no poll."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    job = _tiktok_job(slot_offset_minutes=5)
    job.tiktok_publish_state = _ok_state()
    await store.create(job)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    assert len(calls["create"]) == 1
    sched = calls["create"][0]["scheduled_at"]
    assert sched is not None
    assert sched == job.platform_scheduled_at.get("tiktok") or sched == job.slot_time
    assert calls["poll"] == []                       # slot not reached yet
    updated = await store.get("p1")
    assert updated.tiktok_publish_state.stage == "post_scheduled"
    assert updated.platform_statuses["tiktok"].status == "uploading"


async def test_tiktok_instant_create_when_slot_imminent(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Slot < 60 s away → scheduled_at omitted and poll runs immediately."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    job = _tiktok_job(slot_offset_minutes=0)         # "now" → < 60 s away
    job.tiktok_publish_state = _ok_state()
    await store.create(job)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    assert calls["create"][0]["scheduled_at"] is None
    assert len(calls["poll"]) == 1
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "uploaded"
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_reminder_scheduler.py -q`
Expected: FAIL — `ImportError: cannot import name 'TIKTOK_SCHEDULE_LEAD_MINUTES'`.

- [ ] **Step 3: Implement the scheduler changes**

In `server/app/services/reminder_scheduler.py`:

1. Replace the import from the publisher (line 33) with:

```python
from app.services.post_for_me_publisher import (
    TikTokPublishResult,
    create_tiktok_post,
    poll_tiktok_post_result,
    stage_media_for_tiktok,
)
```

2. Replace line 39 (`TIKTOK_LEAD_MINUTES = 10  # ...`) with:

```python
# Post creation lead: the PFM post (with scheduled_at = the true slot) is
# created this many minutes before the slot. Must stay <= the backend's
# TIKTOK_EDIT_LOCK_MINUTES (backend/app/services/scheduling_service.py):
# job data freezes at sched-15, the post is created from it at sched-10.
TIKTOK_SCHEDULE_LEAD_MINUTES = 10
_TT_INSTANT_PUBLISH_CUTOFF_SECONDS = 60  # sched closer than this → publish instantly
```

3. Replace `_platform_due_time` (lines 76-84) with:

```python
def _tiktok_sched(job: Job) -> datetime:
    """The user-facing TikTok publish instant (PFM fires at exactly this time)."""
    return _normalize_utc(job.platform_scheduled_at.get("tiktok") or job.slot_time)


def _platform_due_time(job: Job, platform: str) -> datetime:
    """Due time of the platform's next pending action.

    TikTok runs three phases: media staging is due as soon as the job exists;
    post creation at sched - TIKTOK_SCHEDULE_LEAD_MINUTES (PFM then publishes
    server-side at sched via scheduled_at); result polling from sched.
    The stored times are never mutated."""
    if platform != "tiktok":
        due_time = job.platform_scheduled_at.get(platform) or job.slot_time
        return _normalize_utc(due_time)
    sched = _tiktok_sched(job)
    state = job.tiktok_publish_state
    if state and state.post_id and state.stage != "failed":
        return sched                                        # poll results at slot
    if state and state.media_url:
        return sched - timedelta(minutes=TIKTOK_SCHEDULE_LEAD_MINUTES)  # create post
    return _normalize_utc(job.created_at)                   # stage media on arrival
```

4. Replace `_dispatch_tiktok_publish` (lines 93-194) with:

```python
async def _record_tiktok_failure(
    job: Job, store: JobStore, settings: Settings, discord, *,
    attempts: int, detail: str | None,
) -> None:
    """Shared attempt-counted failure handling for the create/poll phases."""
    now = datetime.now(tz=UTC)
    if attempts >= _TT_MAX_ATTEMPTS:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="failed", detail=detail, attempts=attempts, completed_at=now
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        await _post_failure_ping(
            job, settings, discord, detail or "publish failed",
            platform_label="TikTok",
        )
        logger.warning(
            "TikTok publish failed for %s after %d attempts: %s",
            job.project_id, attempts, detail,
        )
    else:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(status="pending", detail=detail, attempts=attempts),
        )
        logger.info(
            "TikTok publish attempt %d/%d failed for %s: %s — will retry next tick",
            attempts, _TT_MAX_ATTEMPTS, job.project_id, detail,
        )


async def _dispatch_tiktok_publish(  # noqa: PLR0911, PLR0912, PLR0915
    job: Job, store: JobStore, settings: Settings, discord
) -> bool:
    """Run every currently-due TikTok phase for this job (stage → create → poll).

    'uploading' is NOT terminal: with the in-flight registry preventing
    concurrent dispatch, seeing it here means a previous process crashed
    mid-phase. The persisted publish_state (post_id → never re-create) is the
    double-post protection."""
    current = job.platform_statuses.get("tiktok", PlatformStatus(status="pending"))
    if current.status in ("uploaded", "failed", "skipped"):
        return False
    payload = job.tiktok_payload
    if not payload:
        logger.warning(
            "Job %s has 'tiktok' in platforms_requested but no tiktok_payload",
            job.project_id,
        )
        return False

    now = datetime.now(tz=UTC)
    sched = _tiktok_sched(job)
    create_due = sched - timedelta(minutes=TIKTOK_SCHEDULE_LEAD_MINUTES)
    state = job.tiktok_publish_state

    if not settings.pfm_api_key:
        if now < create_due:
            return False  # stay quiet until the publish window
        await _record_tiktok_failure(
            job, store, settings, discord,
            attempts=current.attempts + 1,
            detail="ATR_PFM_API_KEY is not configured",
        )
        return False

    # ---- Phase 1: stage media (due on arrival; quiet retries pre-window) ----
    if not (state and (state.media_url or (state.post_id and state.stage != "failed"))):
        result = await stage_media_for_tiktok(
            api_key=settings.pfm_api_key,
            base_url=settings.pfm_base_url,
            download_url=job.drive_video_url,
            publish_state=state,
            temp_dir=settings.data_dir / "tmp" / "tiktok",
        )
        if result.publish_state is not None:
            await store.set_tiktok_publish_state(job.project_id, result.publish_state)
            state = result.publish_state
        if not result.success:
            if now < create_due:
                logger.info(
                    "TikTok media staging failed for %s (quiet attempt %d): %s",
                    job.project_id,
                    state.media_attempts if state else 0,
                    result.detail,
                )
                return False
            await _record_tiktok_failure(
                job, store, settings, discord,
                attempts=current.attempts + 1, detail=result.detail,
            )
            return False
        logger.info("TikTok media staged for %s", job.project_id)

    if now < create_due:
        return True  # staged; post creation comes due at sched - lead

    # ---- Phases 2+3 share one attempt increment per dispatch ----
    next_attempts = current.attempts + 1
    await store.merge_platform_status(
        job.project_id, "tiktok",
        PlatformStatus(status="uploading", attempts=next_attempts),
    )

    async def persist_tiktok_state(new_state: TikTokPublishState) -> None:
        await store.set_tiktok_publish_state(job.project_id, new_state)

    # ---- Phase 2: ensure the post exists (scheduled, or instant when late) ----
    instant = False
    if not (state and state.post_id and state.stage != "failed"):
        instant = (sched - now).total_seconds() < _TT_INSTANT_PUBLISH_CUTOFF_SECONDS
        result = await create_tiktok_post(
            api_key=settings.pfm_api_key,
            base_url=settings.pfm_base_url,
            social_account_id=payload["social_account_id"],
            caption=payload["caption"],
            privacy_status=payload.get("privacy_status", "public"),
            allow_comment=bool(payload.get("allow_comment", True)),
            allow_duet=bool(payload.get("allow_duet", True)),
            allow_stitch=bool(payload.get("allow_stitch", True)),
            scheduled_at=None if instant else sched,
            publish_state=state,
        )
        if result.publish_state is not None:
            await store.set_tiktok_publish_state(job.project_id, result.publish_state)
            state = result.publish_state
        if not result.success:
            await _record_tiktok_failure(
                job, store, settings, discord,
                attempts=next_attempts, detail=result.detail,
            )
            return False
        logger.info(
            "TikTok post %s for %s (post_id=%s)",
            "created for instant publish" if instant
            else f"scheduled at {sched.isoformat()}",
            job.project_id, state.post_id,
        )

    # ---- Phase 3: poll results (from sched; instant posts poll right away) ----
    if not instant and now < sched:
        return True  # PFM will fire at sched; polling comes due then

    result = await poll_tiktok_post_result(
        api_key=settings.pfm_api_key,
        base_url=settings.pfm_base_url,
        social_account_id=payload["social_account_id"],
        publish_state=state,
        progress_callback=persist_tiktok_state,
    )
    if result.publish_state is not None:
        await store.set_tiktok_publish_state(job.project_id, result.publish_state)

    if result.success:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="uploaded",
                url=result.url,
                attempts=next_attempts,
                completed_at=datetime.now(tz=UTC),
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        logger.info(
            "TikTok publish succeeded for %s (url=%s)", job.project_id, result.url
        )
        return True
    await _record_tiktok_failure(
        job, store, settings, discord,
        attempts=next_attempts, detail=result.detail,
    )
    return False
```

Note: `_TT_MAX_ATTEMPTS = 5` (line 38) and everything Instagram-related stay unchanged in this task. The docstrings at the top of the module (lines 1-16) should have the tiktok bullet updated to: `- tiktok → stage media on arrival, create a PFM post with scheduled_at at sched − TIKTOK_SCHEDULE_LEAD_MINUTES, poll results from sched.`

- [ ] **Step 4: Run the scheduler suite**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_reminder_scheduler.py -q`
Expected: ALL PASS.

- [ ] **Step 5: Run the whole server suite (regression)**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/ -q`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git add server/app/services/reminder_scheduler.py server/tests/test_reminder_scheduler.py
git commit -m "feat(server): phased TikTok dispatch — stage on arrival, PFM-scheduled post at sched-10, poll from slot

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Concurrent dispatch with an in-flight registry

**Files:**
- Modify: `server/app/services/reminder_scheduler.py:51-73` (`dispatch_due_actions`) + new helpers
- Modify: `server/tests/conftest.py` (autouse registry-clearing fixture)
- Test: `server/tests/test_reminder_scheduler.py`

**Interfaces:**
- Consumes: `_dispatch_tiktok_publish`, `_dispatch_instagram_publish`, `_platform_due_time` (Task 4).
- Produces:
  - `_IN_FLIGHT: dict[tuple[str, str], asyncio.Task]` (module-level)
  - `async def wait_for_inflight() -> None` — awaits all in-flight tasks (tests/shutdown)
  - `_dispatch_worthwhile(job, platform) -> bool` — cheap terminal/payload pre-checks
  - `dispatch_due_actions` now returns the number of dispatch tasks **started** (they complete in the background).

- [ ] **Step 1: Add the autouse fixture and adapt existing tests**

1. Append to `server/tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _clear_dispatch_inflight():
    """Dispatch tasks run in the background; never leak them across tests."""
    from app.services import reminder_scheduler

    reminder_scheduler._IN_FLIGHT.clear()
    yield
    reminder_scheduler._IN_FLIGHT.clear()
```

2. In `server/tests/test_reminder_scheduler.py`, add `wait_for_inflight` to the
`from app.services.reminder_scheduler import (...)` block, then after **every**
`await dispatch_due_actions(...)` call in the file (TikTok *and* Instagram
tests) insert `await wait_for_inflight()` on the next line before any store
assertion. Pattern:

```python
    actions = await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
```

`actions` now counts *started* dispatches, not completed-successful ones.
Reassess every existing `actions == N` / `first_actions == 0` style assertion
in the file (grep `actions ==`):
- not-due, terminal status, missing payload, in-flight → still `0` (pre-checks
  prevent the task from starting);
- **dispatcher ran but the publish failed** → was `0` (dispatch returned
  False), is now `1` (one task started). Example:
  `test_instagram_recoverable_failure...` asserts `first_actions == 0` while
  `publish_mock.await_count == 1` — that first count becomes `1` (the retry
  ran); the second stays `0` only if the job is no longer worthwhile
  (`_should_retry_recoverable_instagram_failure` returns False at the bumped
  attempts). Update each such assertion to the started-count semantics and
  keep the store-state assertions as the real behavioural check.

3. Add the new concurrency tests:

```python
async def test_two_due_jobs_dispatch_concurrently(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Two same-slot TikTok jobs must overlap, not serialize."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    gate = asyncio.Event()
    concurrent = 0
    peak = 0

    async def blocking_poll(**kwargs):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await gate.wait()
        concurrent -= 1
        return TikTokPublishResult(
            success=True, url="https://t/v",
            publish_state=_ok_state(post_id="post_1", stage="published"),
        )

    _patch_phases(monkeypatch, poll=blocking_poll)
    await store.create(_tiktok_job(project_id="pA"))
    await store.create(_tiktok_job(project_id="pB"))
    started = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert started == 2
    await asyncio.sleep(0.05)          # let both tasks reach the gate
    assert peak == 2                   # overlapping, not serialized
    gate.set()
    await wait_for_inflight()
    for pid in ("pA", "pB"):
        job = await store.get(pid)
        assert job.platform_statuses["tiktok"].status == "uploaded"


async def test_inflight_job_is_not_double_dispatched(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    gate = asyncio.Event()
    poll_calls = 0

    async def blocking_poll(**kwargs):
        nonlocal poll_calls
        poll_calls += 1
        await gate.wait()
        return TikTokPublishResult(
            success=True, url="https://t/v",
            publish_state=_ok_state(post_id="post_1", stage="published"),
        )

    _patch_phases(monkeypatch, poll=blocking_poll)
    await store.create(_tiktok_job())
    first = await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await asyncio.sleep(0.05)
    second = await dispatch_due_actions(store=store, settings=settings, discord=discord)
    gate.set()
    await wait_for_inflight()
    assert first == 1
    assert second == 0                 # still in flight → skipped
    assert poll_calls == 1


async def test_dispatch_task_exception_clears_inflight(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """A crashing dispatch must not wedge the (project, platform) key forever."""
    from app.services import reminder_scheduler

    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def exploding_stage(**kwargs):
        raise RuntimeError("boom")

    _patch_phases(monkeypatch, stage=exploding_stage)
    await store.create(_tiktok_job(slot_offset_minutes=120))
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    assert reminder_scheduler._IN_FLIGHT == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_reminder_scheduler.py -q`
Expected: FAIL — `ImportError: cannot import name 'wait_for_inflight'` (and the conftest fixture fails on `_IN_FLIGHT`).

- [ ] **Step 3: Implement concurrency**

In `server/app/services/reminder_scheduler.py`, replace `dispatch_due_actions` (lines 51-73) with:

```python
# (project_id, platform) → running dispatch task. In-memory only: after a
# process restart this is empty, so a job persisted as 'uploading' is
# correctly treated as crashed-mid-phase and re-dispatched.
_IN_FLIGHT: dict[tuple[str, str], asyncio.Task] = {}


def _dispatch_worthwhile(job: Job, platform: str) -> bool:
    """Cheap pre-checks so terminal/misconfigured jobs never spawn a task."""
    status = job.platform_statuses.get(platform, PlatformStatus(status="pending"))
    if platform == "tiktok":
        if status.status in ("uploaded", "failed", "skipped"):
            return False
        if not job.tiktok_payload:
            logger.warning(
                "Job %s has 'tiktok' in platforms_requested but no tiktok_payload",
                job.project_id,
            )
            return False
        return True
    # instagram
    if not job.instagram_payload:
        logger.warning(
            "Job %s has 'instagram' in platforms_requested but no instagram_payload",
            job.project_id,
        )
        return False
    if status.status in ("uploaded", "skipped"):
        return False
    if status.status == "failed":
        if _should_retry_recoverable_instagram_failure(status):
            logger.info("Retrying recoverable Instagram failure for %s", job.project_id)
            return True
        return False
    return True


async def _run_dispatch(key: tuple[str, str], action) -> None:
    try:
        await action
    except Exception:
        logger.exception("Dispatch crashed for %s/%s", key[0], key[1])
    finally:
        _IN_FLIGHT.pop(key, None)


async def wait_for_inflight() -> None:
    """Await completion of every in-flight dispatch task (tests + shutdown)."""
    while _IN_FLIGHT:
        await asyncio.wait(list(_IN_FLIGHT.values()))


async def dispatch_due_actions(
    *,
    store: JobStore,
    settings: Settings,
    discord,
    now: datetime | None = None,
) -> int:
    """Start a background dispatch task for every due (job, platform) action
    not already in flight. Returns the number of tasks started; use
    wait_for_inflight() to await their completion."""
    current = _normalize_utc(now or datetime.now(tz=UTC))
    started = 0
    for job in await store.list_all():
        for platform in job.platforms_requested:
            if platform == "tiktok":
                dispatcher = _dispatch_tiktok_publish
            elif platform == "instagram":
                dispatcher = _dispatch_instagram_publish
            else:
                continue  # youtube + facebook: main backend schedules natively
            key = (job.project_id, platform)
            if key in _IN_FLIGHT:
                continue
            if not _dispatch_worthwhile(job, platform):
                continue
            if _platform_due_time(job, platform) > current:
                continue
            action = dispatcher(job, store, settings, discord)
            _IN_FLIGHT[key] = asyncio.create_task(_run_dispatch(key, action))
            started += 1
    return started
```

Then simplify `_dispatch_instagram_publish`: its own missing-payload warning
and terminal-status/recoverable-retry block (the code from `payload = job.instagram_payload`
through the `if current.status in ("uploaded", "skipped"): return False` check)
now duplicates `_dispatch_worthwhile`. Keep the status read (`current = ...`)
and the payload read, but delete the warning log and the early-return blocks —
replace them with plain guards without logging:

```python
    payload = job.instagram_payload
    if not payload:
        return False
    current = job.platform_statuses.get("instagram", PlatformStatus(status="pending"))
    if current.status in ("uploaded", "skipped"):
        return False
    if current.status == "failed" and not _should_retry_recoverable_instagram_failure(current):
        return False
```

(The duplicate-side guards stay because dispatchers can be invoked directly in
tests; the loop-side checks are authoritative for the started count.)

Same for `_dispatch_tiktok_publish` (Task 4 version): keep its guards but drop
the `logger.warning(... no tiktok_payload ...)` line, replacing that block with
`if not payload: return False` — the warning now lives in `_dispatch_worthwhile`.

- [ ] **Step 4: Run the scheduler suite, then the full server suite**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/test_reminder_scheduler.py -q && .venv/bin/python -m pytest tests/ -q`
Expected: ALL PASS. If `test_dispatch_tiktok_missing_payload_skips` fails on the warning assertion, the warning moved to `_dispatch_worthwhile` — the test still passes because `dispatch_due_actions` triggers it; only if the test called the dispatcher directly would it need updating.

- [ ] **Step 5: Commit**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git add server/app/services/reminder_scheduler.py server/tests/test_reminder_scheduler.py server/tests/conftest.py
git commit -m "feat(server): concurrent scheduler dispatch with in-flight registry

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Backend edit-lock 10 → 15 minutes

**Files:**
- Modify: `backend/app/services/scheduling_service.py:99`
- Test: `backend/tests/test_scheduling_service.py:463-469`

**Interfaces:**
- Produces: `SchedulingService.TIKTOK_EDIT_LOCK_MINUTES = 15` (read by reschedule guards and the `timing_locked` event flag; no other code changes).

- [ ] **Step 1: Update the boundary test**

In `backend/tests/test_scheduling_service.py`, `test_tiktok_timing_not_locked_outside_window` (line ~463): the tiktok time is `now + timedelta(minutes=15)`, which sits exactly on the new lock boundary. Change it to stay clearly outside the window:

```python
def test_tiktok_timing_not_locked_outside_window(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    tiktok_at = now + timedelta(minutes=25)  # lock opens at now+10min
    project = _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    assert SchedulingService.tiktok_timing_locked(project, now=now) is False
```

- [ ] **Step 2: Run to verify current behavior (test passes on 10, must keep passing on 15)**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run -e dev test tests/test_scheduling_service.py -q -k timing_locked`
Expected: PASS (the updated test is valid under both values; the constant change below is what the other lock tests exercise).

- [ ] **Step 3: Change the constant**

In `backend/app/services/scheduling_service.py` line 99, replace:

```python
    TIKTOK_EDIT_LOCK_MINUTES = 10  # Must equal TIKTOK_LEAD_MINUTES in server/app/services/reminder_scheduler.py
```

with:

```python
    # Freeze slot/caption 15 min before the TikTok publish instant. Must stay
    # >= TIKTOK_SCHEDULE_LEAD_MINUTES (server/app/services/reminder_scheduler.py):
    # the VPS creates the PFM scheduled post from this data at sched-10, so the
    # freeze precedes post creation by 5 min.
    TIKTOK_EDIT_LOCK_MINUTES = 15
```

- [ ] **Step 4: Run the scheduling suites**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run -e dev test tests/test_scheduling_service.py tests/test_scheduling_routes.py -q`
Expected: ALL PASS (main's 38 pre-existing failures live in other files; if anything here fails, compare against a stash of the change to confirm it's pre-existing before proceeding).

- [ ] **Step 5: Commit**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_service.py
git commit -m "feat(backend): widen TikTok edit-lock to 15 min (freeze precedes PFM post creation)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Full verification + stale-reference sweep

**Files:**
- Verify only (plus any comment fixes the sweep finds).

- [ ] **Step 1: Sweep for stale references**

Run: `grep -rn "TIKTOK_LEAD_MINUTES" /home/sid/Projects/anime-tiktok-reproducer/server /home/sid/Projects/anime-tiktok-reproducer/backend /home/sid/Projects/anime-tiktok-reproducer/docs --include="*.py" --include="*.md" | grep -v superpowers`
Expected: no hits in `.py` files. Fix any straggler comments to reference `TIKTOK_SCHEDULE_LEAD_MINUTES` / the lock ≥ lead invariant.

- [ ] **Step 2: Full server suite**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer/server && .venv/bin/python -m pytest tests/ -q`
Expected: ALL PASS.

- [ ] **Step 3: Backend scoped suites**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run -e dev test tests/test_scheduling_service.py tests/test_scheduling_routes.py tests/test_platform_reschedule_service.py -q`
Expected: ALL PASS (any failure must be shown to be pre-existing on main before accepting).

- [ ] **Step 4: Commit any sweep fixes**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git add -A server backend
git diff --cached --quiet || git commit -m "chore: sweep stale TIKTOK_LEAD_MINUTES references

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Remind the user about deployment**

Not automatable from this machine: the VPS image is stale (built 07-04 — it never even ran the old head-start). After merge, redeploy per `server/DEPLOYMENT.md` (git pull + docker compose build + up on the VPS). First real publish after deploy should be watched: `docker logs server-server-1 -f | grep -E "PFM|TikTok"` — expect "scheduled at …" at sched−10 and the publish success shortly after sched.
