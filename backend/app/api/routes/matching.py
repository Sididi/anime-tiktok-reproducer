import asyncio
import logging
from contextlib import suppress
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import json
from pathlib import Path
import re

logger = logging.getLogger("uvicorn.error")

from ...config import settings
from ...models import ProjectPhase, MatchList, SceneMatch, Scene, SceneList
from ...services import (
    ProjectService,
    AnimeMatcherService,
    SceneMergerService,
    AnimeLibraryService,
    LibraryHydrationService,
)
from ...services.match_playback_service import MatchPlaybackService

router = APIRouter(prefix="/projects/{project_id}", tags=["matching"])

KNOWN_MEDIA_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".mov",
    ".avi",
    ".webm",
    ".m4v",
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".aiff",
    ".aif",
)


def _etag_for_path(path: Path) -> str:
    stat = path.stat()
    return f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'


def _media_headers(path: Path, *, cache_control: str) -> dict[str, str]:
    return {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "Cross-Origin-Resource-Policy": "cross-origin",
        "ETag": _etag_for_path(path),
    }


def _strip_known_media_extension(name: str) -> str:
    """Strip only supported media extensions from a filename-like value."""
    clean_name = str(name or "").strip()
    lower_name = clean_name.lower()
    for ext in KNOWN_MEDIA_EXTENSIONS:
        if lower_name.endswith(ext):
            return clean_name[:-len(ext)]
    return clean_name


def _canonical_episode_ref(episode: str, *, library_type: str | None = None) -> str:
    """Persist manual episode refs as canonical bundle-safe clip identifiers."""
    clean_episode = str(episode or "").strip()
    if not clean_episode:
        return clean_episode

    resolved = AnimeLibraryService.resolve_episode_path(
        clean_episode,
        library_type=library_type,
    )
    if resolved is not None and resolved.exists():
        return _strip_known_media_extension(resolved.name)

    candidate = Path(clean_episode)
    if candidate.is_absolute() or candidate.suffix or "/" in clean_episode or "\\" in clean_episode:
        return _strip_known_media_extension(candidate.name or clean_episode)

    return _strip_known_media_extension(clean_episode)


def _serialize_scenes(scenes: SceneList) -> list[dict[str, float | int]]:
    """Serialize scenes with derived duration for frontend consumers."""
    return [
        {
            "index": scene.index,
            "start_time": scene.start_time,
            "end_time": scene.end_time,
            "duration": scene.duration,
        }
        for scene in scenes.scenes
    ]


@router.get("/matches/config")
async def get_matches_config(project_id: str):
    """Get matches feature flags."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"full_auto_enabled": settings.matches_full_auto_enabled}


class SetSourcesRequest(BaseModel):
    paths: list[str]


class FindMatchesRequest(BaseModel):
    source_path: str | None = None  # Optional, defaults to anime_library_path
    merge_continuous: bool = True  # Auto-merge continuous anime scenes


class PreparePlaybackRequest(BaseModel):
    force: bool = False


class PrepareScenePlaybackRequest(BaseModel):
    force: bool = False


def _normalize_name(value: str) -> str:
    """Normalize folder/anime names for robust matching."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _resolve_anime_source_dir(library_root: Path, anime_name: str) -> Path | None:
    """Resolve the anime directory in the library using exact or normalized name match."""
    if not library_root.exists() or not library_root.is_dir():
        return None

    direct = library_root / anime_name
    if direct.exists() and direct.is_dir():
        return direct

    target = _normalize_name(anime_name)
    for child in library_root.iterdir():
        if child.is_dir() and _normalize_name(child.name) == target:
            return child

    return None


def _build_episode_source_dirs(project) -> list[Path]:
    """
    Build candidate source directories for manual episode selection.

    Prefer explicit project source paths when they still exist. Otherwise fall
    back to the resolved anime folder, then to the library root so manual match
    tooling never ends up with an empty episode list just because folder
    resolution missed.
    """
    source_dirs: list[Path] = []
    explicit_paths = [Path(src) for src in project.source_paths if Path(src).exists()]
    if explicit_paths:
        return explicit_paths

    library_root = AnimeLibraryService.get_library_path(project.library_type)
    if not library_root.exists():
        return []

    if project.anime_name:
        scoped_dir = _resolve_anime_source_dir(
            library_root,
            project.anime_name,
        )
        if scoped_dir is not None:
            source_dirs.append(scoped_dir)

    if not source_dirs:
        source_dirs.append(library_root)

    return source_dirs


@router.post("/sources")
async def set_sources(project_id: str, request: SetSourcesRequest):
    """Set source episode paths for the project."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate paths exist
    for path in request.paths:
        if not Path(path).exists():
            raise HTTPException(status_code=400, detail=f"Path not found: {path}")

    project.source_paths = request.paths
    ProjectService.save(project)

    return {"status": "ok", "source_paths": project.source_paths}


@router.get("/sources")
async def get_sources(project_id: str):
    """Get source episode paths."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"source_paths": project.source_paths}


@router.get("/sources/episodes")
async def list_episodes(project_id: str):
    """List all video files in the source paths or anime library."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v"}
    episodes: list[str] = []

    library_root = AnimeLibraryService.get_library_path(project.library_type)
    source_dirs = _build_episode_source_dirs(project)

    def _is_under(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _scan_source_dir_sync(src_path: Path) -> list[str]:
        found: list[str] = []
        if src_path.is_dir():
            for ext in VIDEO_EXTENSIONS:
                found.extend(str(f.resolve()) for f in src_path.glob(f"*{ext}"))
                found.extend(str(f.resolve()) for f in src_path.glob(f"**/*{ext}"))
        elif src_path.is_file() and src_path.suffix.lower() in VIDEO_EXTENSIONS:
            found.append(str(src_path.resolve()))
        return found

    manifest: dict | None = None
    if library_root and any(_is_under(src, library_root) or src.resolve() == library_root.resolve() for src in source_dirs if src.exists()):
        manifest = await AnimeLibraryService.ensure_episode_manifest(
            library_type=project.library_type,
        )

    manifest_episodes: list[str] = (
        AnimeLibraryService.list_episode_paths(
            manifest,
            library_type=project.library_type,
        )
        if manifest
        else []
    )

    for src_path in source_dirs:
        manifest_hits_before = len(episodes)
        if (
            manifest is not None
            and library_root is not None
            and src_path.exists()
            and (_is_under(src_path, library_root) or src_path.resolve() == library_root.resolve())
        ):
            src_resolved = src_path.resolve()
            for episode in manifest_episodes:
                episode_path = Path(episode)
                if src_resolved.is_dir() and _is_under(episode_path, src_resolved):
                    episodes.append(episode)
                elif src_resolved.is_file() and episode_path.resolve() == src_resolved:
                    episodes.append(episode)

        # The cached manifest can lag behind the actual library contents for a
        # scoped series folder. When it yields nothing for that source, fall
        # back to a direct filesystem scan so manual match selection still has
        # episodes to offer.
        if len(episodes) == manifest_hits_before:
            episodes.extend(await asyncio.to_thread(_scan_source_dir_sync, src_path))

    # Remove duplicates and sort
    episodes = sorted(set(episodes))

    return {"episodes": episodes}


@router.post("/matches/find")
async def find_matches(project_id: str, request: FindMatchesRequest):
    """Find anime source matches for all scenes with optional continuous scene merging."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.series_id:
        try:
            await LibraryHydrationService.ensure_matcher_ready_for_project(
                project_id=project.id,
                library_type=project.library_type,
                series_id=project.series_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Use provided source_path or default to anime_library_path
    source_path = (
        Path(request.source_path)
        if request.source_path
        else AnimeLibraryService.get_library_path(project.library_type)
    )
    if not source_path.exists():
        raise HTTPException(status_code=400, detail="Source path not found")

    # Load scenes
    scenes = ProjectService.load_scenes(project_id)
    if not scenes or not scenes.scenes:
        raise HTTPException(status_code=400, detail="No scenes detected yet")

    video_path = Path(project.video_path) if project.video_path else None
    if not video_path or not video_path.exists():
        raise HTTPException(status_code=400, detail="Video not found")

    # Pre-match: absorb tiny scenes that produce poor matches
    TINY_SCENE_THRESHOLD = 0.35
    merged_scenes, tiny_merge_log = scenes.merge_tiny_scenes(TINY_SCENE_THRESHOLD)
    if tiny_merge_log:
        logger.info(
            "Pre-match tiny scene merge: absorbed %d scene(s) below %.2fs",
            len(tiny_merge_log),
            TINY_SCENE_THRESHOLD,
        )
        scenes = merged_scenes
        ProjectService.save_scenes(project_id, scenes)

    # Update phase
    project.phase = ProjectPhase.MATCHING
    ProjectService.save(project)

    # Get anime name for filtering (if set on project)
    anime_name = project.anime_name
    merge_continuous = request.merge_continuous

    async def stream_progress():
        if tiny_merge_log:
            yield "data: " + json.dumps({
                "status": "matching",
                "progress": 0.0,
                "message": f"Merged {len(tiny_merge_log)} tiny scene(s) (< {TINY_SCENE_THRESHOLD}s)",
                "current_scene": 0,
                "total_scenes": len(scenes.scenes),
                "error": None,
            }) + "\n\n"

        # === PASS 1: Match all scenes ===
        first_pass_label = "Pass 1: " if merge_continuous else ""
        first_pass_matches: MatchList | None = None

        async for progress in AnimeMatcherService.match_scenes(
            video_path, scenes, source_path,
            project.library_type,
            anime_name=anime_name,
            pass_label=first_pass_label,
        ):
            if progress.status == "complete" and progress.matches:
                first_pass_matches = progress.matches
                continue

            yield f"data: {json.dumps(progress.to_dict())}\n\n"
            if progress.status == "error":
                project.phase = ProjectPhase.SCENE_VALIDATION
                ProjectService.save(project)
                return

        if not first_pass_matches:
            yield (
                "data: "
                + json.dumps(
                    {
                        "status": "error",
                        "progress": 0.0,
                        "message": "",
                        "current_scene": 0,
                        "total_scenes": len(scenes.scenes),
                        "error": "Matching completed without results",
                    }
                )
                + "\n\n"
            )
            project.phase = ProjectPhase.SCENE_VALIDATION
            ProjectService.save(project)
            return

        final_scenes = scenes
        final_matches = first_pass_matches
        total_scenes_for_progress = len(scenes.scenes)

        # If merge disabled, keep first pass output.
        if not merge_continuous:
            pass
        else:
            # === CONTINUITY DETECTION ===
            yield (
                "data: "
                + json.dumps(
                    {
                        "status": "matching",
                        "progress": 0.5,
                        "message": "Detecting continuous scenes...",
                        "current_scene": 0,
                        "total_scenes": len(scenes.scenes),
                        "error": None,
                    }
                )
                + "\n\n"
            )

            index_fps = AnimeMatcherService.get_index_fps()
            pairs = SceneMergerService.detect_continuous_pairs(
                scenes, first_pass_matches, index_fps=index_fps,
            )
            chains = (
                SceneMergerService.build_merge_chains(
                    pairs, scenes, first_pass_matches, index_fps=index_fps,
                    video_path=video_path,
                    library_path=source_path,
                    library_type=project.library_type,
                    anime_name=anime_name,
                )
                if pairs
                else []
            )

            if chains:
                # === MERGE ===
                merged_count = sum(len(c) for c in chains)
                group_count = len(chains)
                merged_scenes, merged_matches, backup = SceneMergerService.merge_scenes_and_matches(
                    scenes, first_pass_matches, chains,
                )

                SceneMergerService.save_pre_merge_backup(project_id, backup)
                ProjectService.save_scenes(project_id, merged_scenes)

                total_scenes_for_progress = len(merged_scenes.scenes)
                final_scenes = merged_scenes
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "status": "matching",
                            "progress": 0.6,
                            "message": (
                                f"Merged {merged_count} scenes into {group_count} groups. "
                                "Re-matching..."
                            ),
                            "current_scene": 0,
                            "total_scenes": total_scenes_for_progress,
                            "error": None,
                        }
                    )
                    + "\n\n"
                )

                # === PASS 2: Re-match only merged scenes ===
                merged_indices = [
                    i for i, m in enumerate(merged_matches.matches)
                    if m.merged_from is not None
                ]

                pass2_matches: MatchList | None = None
                async for progress in AnimeMatcherService.match_scenes(
                    video_path, merged_scenes, source_path,
                    project.library_type,
                    anime_name=anime_name,
                    scene_indices_to_match=merged_indices,
                    existing_matches=merged_matches,
                    pass_label="Pass 2: ",
                ):
                    if progress.status == "complete" and progress.matches:
                        # Preserve merged_from metadata on re-matched scenes.
                        for i in merged_indices:
                            if i < len(progress.matches.matches) and i < len(merged_matches.matches):
                                progress.matches.matches[i].merged_from = (
                                    merged_matches.matches[i].merged_from
                                )
                        pass2_matches = progress.matches
                        continue

                    yield f"data: {json.dumps(progress.to_dict())}\n\n"
                    if progress.status == "error":
                        # On pass 2 error, still save what we have from pass 1 merge.
                        ProjectService.save_matches(project_id, merged_matches)
                        project.phase = ProjectPhase.MATCH_VALIDATION
                        ProjectService.save(project)
                        return

                final_matches = pass2_matches or merged_matches

        snapped_scenes = SceneMergerService.snap_dense_visual_boundaries(
            video_path,
            final_scenes,
        )
        if snapped_scenes is not final_scenes:
            final_scenes = snapped_scenes
            ProjectService.save_scenes(project_id, final_scenes)

        ProjectService.save_matches(project_id, final_matches)
        project.phase = ProjectPhase.MATCH_VALIDATION
        ProjectService.save(project)

        yield (
            "data: "
            + json.dumps(
                {
                    "status": "complete",
                    "progress": 1.0,
                    "message": f"Matched {len(final_matches.matches)} scenes.",
                    "current_scene": len(final_matches.matches),
                    "total_scenes": total_scenes_for_progress,
                    "error": None,
                    "matches": final_matches.model_dump(),
                }
            )
            + "\n\n"
        )

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/matches/deferred-download")
async def deferred_download(project_id: str):
    """Check for missing source episodes, recover from source or download via qBittorrent.

    Always returns an SSE stream so the frontend can track progress phases.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=400, detail="No matches found")

    library_root = AnimeLibraryService.get_library_path(project.library_type)
    anime_name = project.anime_name
    series_id = project.series_id

    if not anime_name:
        async def _skipped():
            yield f"data: {json.dumps({'status': 'complete', 'phase': 'check', 'message': 'No anime name set', 'progress': 1.0})}\n\n"

        return StreamingResponse(
            _skipped(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    from ...services.deferred_download import DeferredDownloadService

    episode_paths = list({
        m.episode
        for m in matches.matches
        if m.episode
    })

    async def stream_progress():
        event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        latest_network: dict[str, object] = {}

        async def _enqueue(event: dict) -> None:
            await event_queue.put(event)

        async def _on_network_progress(snapshot) -> None:
            latest_network.clear()
            latest_network.update(
                network_bytes_transferred=snapshot.bytes_transferred,
                network_bytes_total=snapshot.bytes_total,
                network_mib_per_sec=snapshot.mib_per_sec,
                network_eta_seconds=snapshot.eta_seconds,
                network_active_transfers=snapshot.active_transfers,
            )
            await _enqueue({
                "status": "running",
                "phase": "hydrate_episode",
                "message": "Hydrating missing episodes from Storage Box...",
                "progress": 0.5,
                **latest_network,
            })

        async def _produce() -> None:
            try:
                if series_id:
                    await _enqueue({
                        "status": "running",
                        "phase": "hydrate_index",
                        "message": "Ensuring local matcher cache is ready...",
                        "progress": 0.05,
                    })
                    await LibraryHydrationService.ensure_matcher_ready_for_project(
                        project_id=project.id,
                        library_type=project.library_type,
                        series_id=series_id,
                    )
                    await _enqueue({
                        "status": "running",
                        "phase": "hydrate_episode",
                        "message": "Hydrating missing episodes from Storage Box...",
                        "progress": 0.15,
                    })
                    try:
                        await LibraryHydrationService.hydrate_series(
                            library_type=project.library_type,
                            series_id=series_id,
                            episode_keys=episode_paths,
                            full_series=False,
                            progress_callback=_on_network_progress,
                        )
                    except Exception as exc:
                        await _enqueue({
                            "status": "warning",
                            "phase": "hydrate_episode",
                            "message": f"Storage Box hydration failed, falling back: {exc}",
                            "progress": 0.2,
                        })
                async for event in DeferredDownloadService.recover_missing_episodes(
                    episode_paths, library_root, anime_name
                ):
                    await _enqueue(event)
            except Exception as e:
                await _enqueue({
                    "status": "error",
                    "phase": "download",
                    "error": str(e),
                    "message": str(e),
                })
            finally:
                await event_queue.put(None)

        producer_task = asyncio.create_task(_produce())
        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            if not producer_task.done():
                producer_task.cancel()
                with suppress(BaseException):
                    await producer_task

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/matches/playback/prepare")
async def prepare_matches_playback(project_id: str, request: PreparePlaybackRequest):
    """Prepare browser-safe clips for /matches playback and Fast Watch."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    async def stream_progress():
        async for progress in MatchPlaybackService.prepare_playback(
            project_id,
            force=request.force,
        ):
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/matches/playback/prepare-scene/{scene_index}")
async def prepare_matches_playback_scene(
    project_id: str,
    scene_index: int,
    request: PrepareScenePlaybackRequest,
):
    """Prepare playback clip assets for one scene after manual match updates."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    async def stream_progress():
        async for progress in MatchPlaybackService.prepare_scene_playback(
            project_id,
            scene_index=scene_index,
            force=request.force,
        ):
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/matches/playback/manifest")
async def get_matches_playback_manifest(project_id: str):
    """Get the current prepared playback manifest for /matches."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return MatchPlaybackService.get_manifest(project_id)


@router.get("/matches/playback/clip/{scene_index}/{track}")
async def get_matches_playback_clip(
    project_id: str,
    scene_index: int,
    track: str,
    fingerprint: str | None = Query(default=None),
):
    """Serve one prepared playback clip."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if track not in {"tiktok", "source"}:
        raise HTTPException(status_code=400, detail="Invalid track")
    track_name = "tiktok" if track == "tiktok" else "source"

    try:
        clip_path = MatchPlaybackService.get_clip_path(
            project_id,
            scene_index=scene_index,
            track=track_name,
            fingerprint=fingerprint,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(
        path=clip_path,
        media_type="video/mp4",
        headers=_media_headers(
            clip_path,
            cache_control="public, max-age=0, must-revalidate",
        ),
    )


@router.get("/matches/playback/clips/{clip_id}")
async def get_matches_playback_clip_by_id(project_id: str, clip_id: str):
    """Serve one prepared playback clip by stable content-addressed clip id."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        clip_path = MatchPlaybackService.get_clip_path_by_id(project_id, clip_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(
        path=clip_path,
        media_type="video/mp4",
        headers=_media_headers(
            clip_path,
            cache_control="public, max-age=31536000, immutable",
        ),
    )


@router.get("/matches")
async def get_matches(project_id: str):
    """Get all matches for a project."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matches = ProjectService.load_matches(project_id)
    if not matches:
        return {"matches": []}

    return {"matches": [m.model_dump() for m in matches.matches]}


@router.post("/matches/merge-with-previous/{scene_index}")
async def merge_with_previous(project_id: str, scene_index: int):
    """Manually merge one scene into the previous scene and re-match only it."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    scenes = ProjectService.load_scenes(project_id)
    if not scenes or not scenes.scenes:
        raise HTTPException(status_code=404, detail="No scenes found")

    matches = ProjectService.load_matches(project_id)
    if not matches or not matches.matches:
        raise HTTPException(status_code=404, detail="No matches found")

    if scene_index <= 0 or scene_index >= len(scenes.scenes):
        raise HTTPException(status_code=400, detail="Invalid scene index")

    if project.series_id:
        try:
            await LibraryHydrationService.ensure_matcher_ready_for_project(
                project_id=project.id,
                library_type=project.library_type,
                series_id=project.series_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    source_path = AnimeLibraryService.get_library_path(project.library_type)
    if not source_path.exists():
        raise HTTPException(status_code=400, detail="Source path not found")

    video_path = Path(project.video_path) if project.video_path else None
    if not video_path or not video_path.exists():
        raise HTTPException(status_code=400, detail="Video not found")

    try:
        merged_scenes, merged_matches, backup, merged_scene_index = (
            SceneMergerService.prepare_manual_merge_with_previous(
                project_id,
                scene_index,
                scenes,
                matches,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    SceneMergerService.save_pre_merge_backup(project_id, backup)

    rematched_matches: MatchList | None = None
    async for progress in AnimeMatcherService.match_scenes(
        video_path,
        merged_scenes,
        source_path,
        project.library_type,
        anime_name=project.anime_name,
        scene_indices_to_match=[merged_scene_index],
        existing_matches=merged_matches,
    ):
        if progress.status == "complete" and progress.matches:
            merged_from = merged_matches.matches[merged_scene_index].merged_from
            if merged_scene_index < len(progress.matches.matches):
                progress.matches.matches[merged_scene_index].merged_from = merged_from
            rematched_matches = progress.matches
            continue

        if progress.status == "error":
            raise HTTPException(
                status_code=500,
                detail=progress.error or "Failed to re-match merged scene",
            )

    if not rematched_matches:
        raise HTTPException(
            status_code=500,
            detail="Merged scene re-match completed without results",
        )

    ProjectService.save_scenes(project_id, merged_scenes)
    ProjectService.save_matches(project_id, rematched_matches)

    project.phase = ProjectPhase.MATCH_VALIDATION
    ProjectService.save(project)

    return {
        "scenes": _serialize_scenes(merged_scenes),
        "matches": [m.model_dump() for m in rematched_matches.matches],
    }


class UpdateMatchRequest(BaseModel):
    episode: str
    start_time: float
    end_time: float
    confirmed: bool = True


class BatchUpdateMatchItem(BaseModel):
    scene_index: int
    episode: str
    start_time: float
    end_time: float
    confirmed: bool = True


class BatchUpdateMatchesRequest(BaseModel):
    updates: list[BatchUpdateMatchItem]


@router.put("/matches/{scene_index}")
async def update_match(project_id: str, scene_index: int, request: UpdateMatchRequest):
    """Update or confirm a match for a scene."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=404, detail="No matches found")

    # Find the match for this scene
    match = next((m for m in matches.matches if m.scene_index == scene_index), None)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found for scene")

    # Update match
    match.episode = _canonical_episode_ref(
        request.episode,
        library_type=project.library_type,
    )
    match.start_time = request.start_time
    match.end_time = request.end_time
    match.confirmed = request.confirmed

    # Set confidence to 1.0 for manually confirmed matches (if it was 0)
    # Also preserve was_no_match flag if it was initially true
    if match.confidence == 0 and request.confirmed:
        match.confidence = 1.0
        # was_no_match should already be set, but ensure it's preserved

    # Recalculate speed ratio using scene index mapping (not positional offset).
    scenes = ProjectService.load_scenes(project_id)
    if scenes:
        scene = next((s for s in scenes.scenes if s.index == scene_index), None)
        if scene is not None:
            scene_duration = scene.end_time - scene.start_time
            source_duration = match.end_time - match.start_time
            if source_duration > 0:
                match.speed_ratio = scene_duration / source_duration

    ProjectService.save_matches(project_id, matches)

    return {"status": "ok", "match": match.model_dump()}


@router.put("/matches")
async def update_matches_batch(project_id: str, request: BatchUpdateMatchesRequest):
    """Batch update multiple scene matches and persist once."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=404, detail="No matches found")

    scenes = ProjectService.load_scenes(project_id)
    scene_by_index = {scene.index: scene for scene in scenes.scenes} if scenes else {}
    match_by_scene_index = {match.scene_index: match for match in matches.matches}

    for update in request.updates:
        match = match_by_scene_index.get(update.scene_index)
        if not match:
            raise HTTPException(
                status_code=404,
                detail=f"Match not found for scene {update.scene_index}",
            )

        match.episode = _canonical_episode_ref(
            update.episode,
            library_type=project.library_type,
        )
        match.start_time = update.start_time
        match.end_time = update.end_time
        match.confirmed = update.confirmed

        if match.confidence == 0 and update.confirmed:
            match.confidence = 1.0

        scene = scene_by_index.get(update.scene_index)
        if scene is not None:
            scene_duration = scene.end_time - scene.start_time
            source_duration = match.end_time - match.start_time
            if source_duration > 0:
                match.speed_ratio = scene_duration / source_duration

    ProjectService.save_matches(project_id, matches)
    return {"status": "ok", "matches": [m.model_dump() for m in matches.matches]}


@router.post("/matches/undo-merge/{scene_index}")
async def undo_merge(project_id: str, scene_index: int):
    """Undo a merge for a specific scene, restoring original sub-scenes."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    result = SceneMergerService.undo_merge(project_id, scene_index)
    if not result:
        raise HTTPException(
            status_code=400,
            detail="Cannot undo: scene is not a merged scene or no backup found",
        )

    restored_scenes, restored_matches = result
    return {
        "scenes": _serialize_scenes(restored_scenes),
        "matches": [m.model_dump() for m in restored_matches.matches],
    }
