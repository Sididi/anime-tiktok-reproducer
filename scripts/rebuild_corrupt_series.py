#!/usr/bin/env python3
"""Rebuild squish-corrupted series: hydrate -> force reindex -> gate -> publish -> evict.

Reads the triage TSVs produced by audit_index_corruption.py and
triage_remote_series.py, takes every series with a CORRUPT verdict, and repairs
it end to end. Each series is gated on a full re-probe (every indexed file, not
a sample) before anything is published, so a partial or failed re-embed cannot
promote a still-corrupt shard to the canonical release.

Purge policy is "restore prior local state": a series is evicted afterwards only
if it was not local before the campaign, per docs/audit/pre-campaign-local-state.json.

Two series are in flight at once, but they are doing *different* things: the next
series downloads while the current one re-embeds. Two concurrent re-embeds are
deliberately not allowed — they race the shared FAISS manifest (see
indexation-crash-recovery). Downloading is safe to overlap because hydration
touches only video files and AnimeLibraryService's episode manifest, both of
which are separate from the FAISS manifest the indexer writes under flock.

Resumable across reboots: every finished series is journaled to --out as it
completes, and --resume skips those. A series interrupted mid-flight is simply
redone from the top on the next run (``index --force`` re-embeds unconditionally,
and rclone skips bytes already on disk), so an unclean shutdown costs time, never
correctness. Startup also sweeps local dirs left behind by a killed run.

Run with the backend stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sqlite3
import sys
import time
import traceback
from contextlib import suppress
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PIXI_PYTHON = REPO_ROOT / ".pixi" / "envs" / "default" / "bin" / "python"
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "anime_searcher"))

import audit_index_corruption as audit  # noqa: E402
from app.library_types import coerce_library_type  # noqa: E402
from app.services.anime_library import AnimeLibraryService  # noqa: E402
from app.services.library_hydration_service import LibraryHydrationService  # noqa: E402
from app.services.library_state_db import LibraryStateDb  # noqa: E402
from app.services.storage_box_repository import StorageBoxRepository  # noqa: E402


def read_corrupt(tsv: Path) -> list[tuple[str | None, str]]:
    """Return (series_id_or_None, display_name) for CORRUPT rows."""
    if not tsv.exists():
        return []
    lines = tsv.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    has_id = header[0] == "series_id"
    out: list[tuple[str | None, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split("\t")
        series_id = fields[0] if has_id else None
        name = fields[1] if has_id else fields[0]
        verdict = fields[3] if has_id else fields[2]
        if verdict == "CORRUPT":
            out.append((series_id, name))
    return out


def prior_local(snapshot: Path) -> dict[str, bool]:
    """series_id -> did this series have ANY episode on disk pre-campaign?

    Keyed on local_episode_count rather than hydration_status: an `index_ready`
    series can still hold a few local episodes, and evicting it after a rebuild
    would delete files that predate the campaign. Restoring prior state means
    only dropping what we ourselves pulled down.
    """
    if not snapshot.exists():
        return {}
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    return {
        r["series_id"]: (
            r.get("hydration_status") == "fully_local"
            or int(r.get("local_episode_count") or 0) > 0
        )
        for r in payload.get("series_state", [])
    }


def pinned_series(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("select distinct series_id from project_series_pins").fetchall()
        pinned = {r[0] for r in rows}
        rows = con.execute(
            "select series_id from series_state where permanent_pin = 1"
        ).fetchall()
        pinned |= {r[0] for r in rows}
        return pinned
    finally:
        con.close()


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024 ** 3)


def force_reindex(library_root: Path, library_type: str, display_name: str,
                  fps: float) -> tuple[bool, str]:
    """`index --series <name> --force` -- the only primitive that re-embeds
    untouched files. `update --manifest` filters through needs_reindex()
    (mtime_ns + size) and would skip every file we care about.

    Invokes the module directly with the pixi environment's interpreter rather
    than going through `pixi run anime-search`. The pixi task is a *shell
    script*, so pixi re-parses the assembled command line, and 11 series in this
    campaign have an apostrophe in their name ("Xam'd Lost Memories") which
    breaks that parse. A plain argv list never touches a shell.
    """
    # The CLI flag is `--type`, not `--library-type` (see LibraryTypeOption).
    #
    # `--fps` is NOT optional here. The CLI preserves an already-indexed
    # series' fps only when *not* forcing (its help text says so verbatim:
    # "Existing series keep their stored FPS unless reindexed with --force"),
    # and its default is DEFAULT_FPS = 1.0 while the app indexes at 2.0. The
    # first campaign omitted this flag and silently re-indexed all 185 series
    # at half the sampling rate — invisibly to the cosine gate, which reads fps
    # back from the manifest the rebuild just wrote and so stays self-consistent.
    cmd = [
        str(PIXI_PYTHON), "-m", "anime_searcher.cli", "index",
        str(library_root),
        "--type", library_type,
        "--fps", f"{fps:g}",
        "--series", display_name,
        "--force",
    ]
    # cwd mirrors the pixi task definition, which runs from the module root.
    proc = subprocess.run(cmd, cwd=REPO_ROOT / "modules" / "anime_searcher",
                          capture_output=True, text=True)
    tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
    return proc.returncode == 0, tail


def manifest_series_fps(library_path: Path, display_name: str) -> float | None:
    """The fps the local index manifest records for one series, or None."""
    entry = audit.load_manifest(library_path).get("series", {}).get(display_name)
    if not isinstance(entry, dict) or entry.get("fps") is None:
        return None
    return float(entry["fps"])


def probe_all_files(embedder, library_path: Path, display_name: str,
                    start_ordinal: int, count: int, stride: int) -> list[audit.FileProbe]:
    manifest = audit.load_manifest(library_path)
    entry = manifest.get("series", {}).get(display_name)
    if not isinstance(entry, dict):
        return [audit.FileProbe(display_name, "-", "NOT_IN_LOCAL_MANIFEST", None, None,
                                None, 0, None, "series absent from manifest after reindex")]
    fps = float(entry.get("fps") or manifest.get("config", {}).get("default_fps") or 2.0)
    index, frames = audit.load_shard(library_path, str(entry["key"]))
    grouped = audit.group_frames_by_file(frames)
    probes = []
    for rel_path in sorted(grouped):
        file_frames = grouped[rel_path]
        ordinals = audit.pick_ordinals(len(file_frames), start_ordinal, count, stride)
        probes.append(audit.probe_file(
            embedder=embedder, index=index, library_path=library_path,
            series=display_name, rel_path=rel_path, frames=file_frames,
            fps=fps, ordinals=ordinals,
        ))
    return probes


async def force_evict(library_type: str, series_id: str) -> None:
    """Evict local files even when a project still pins the series.

    ``LibraryHydrationService.evict_series`` refuses to drop a pinned series,
    which is right for the app: a project's matches would stop resolving. For
    this campaign it is wrong — 41 refusals stranded ~23.5 GB during triage, and
    the owner's call is that a project going ghost until its matches are recomputed
    is acceptable. Only ever applied to series the campaign itself downloaded
    (``was_local`` false), so a series that predates the campaign is never touched.
    """
    scoped_type = coerce_library_type(library_type)
    manifest = None
    with suppress(Exception):
        manifest = await LibraryHydrationService._load_or_fetch_manifest(
            scoped_type, series_id
        )
    await asyncio.to_thread(
        LibraryHydrationService._evict_local_series_sync,
        scoped_type, series_id, manifest,
    )
    state = await asyncio.to_thread(
        LibraryStateDb.get_series_state, scoped_type, series_id
    )
    await asyncio.to_thread(
        LibraryStateDb.upsert_series_state,
        library_type=scoped_type,
        series_id=series_id,
        release_id=(str(manifest["release_id"]) if manifest
                    else (state.release_id if state else None)),
        permanent_pin=state.permanent_pin if state else False,
        hydration_status="not_hydrated",
        local_episode_count=0,
        expected_episode_count=(
            int(manifest.get("episode_count", len(manifest.get("episodes", []))))
            if manifest else (state.expected_episode_count if state else 0)
        ),
        last_error=None,
    )


async def reconcile_stranded(args, prior: dict[str, bool], done: set[str],
                             resolved: list[tuple[str, str]]) -> None:
    """Drop local files left behind by a run that was killed mid-flight.

    Only touches series in this campaign's own work list that were not local
    before it started — a series the owner had on disk all along, or one outside
    the campaign, is never a candidate.
    """
    library_path = AnimeLibraryService.get_library_path(args.library_type)
    reclaimed = 0
    for series_id, display_name in resolved:
        if series_id in done or prior.get(series_id, False):
            continue
        series_dir = library_path / display_name
        if not series_dir.is_dir():
            continue
        try:
            await force_evict(args.library_type, series_id)
            reclaimed += 1
        except Exception as exc:  # noqa: BLE001 - best effort, never fatal
            print(f"  could not reclaim '{display_name}': {exc}", file=sys.stderr)
    if reclaimed:
        print(f"reclaimed {reclaimed} stranded series dir(s) from a previous run",
              file=sys.stderr)


async def rebuild_one(*, library_type: str, series_id: str, display_name: str,
                      embedder, library_root: Path, library_path: Path,
                      was_local: bool, args, hydrated: bool = False) -> dict:
    result = {"series_id": series_id, "series": display_name, "stage": "", "detail": ""}

    # Per-stage seconds, appended to the journal row. Without them the only
    # throughput signal is wall-clock per series, which cannot say whether it is
    # worth overlapping publish with the next series' re-embed.
    timings: dict[str, float] = {}
    clock = time.monotonic()

    def lap(name: str) -> None:
        nonlocal clock
        now = time.monotonic()
        timings[name] = now - clock
        clock = now

    if not hydrated:
        result["stage"] = "hydrate"
        await LibraryHydrationService.hydrate_series(
            library_type=library_type, series_id=series_id, full_series=True
        )
    lap("hydrate" if not hydrated else "hydrate_prefetched")

    # Both of these are blocking and take minutes. Run them off the event loop
    # or the next series' prefetch download — an asyncio task — never gets a
    # chance to run, and the two-in-flight pipeline silently degrades to
    # download-then-reindex-then-download.
    result["stage"] = "reindex"
    ok, tail = await asyncio.to_thread(
        force_reindex, library_root, library_type, display_name, args.fps
    )
    if not ok:
        result["detail"] = f"force reindex failed: {tail[-400:]}"
        return result
    lap("reindex")

    # fps gate. The cosine gate below cannot see a wrong sampling rate (it
    # probes at whatever fps the manifest now says), so check the manifest
    # directly: the series must record exactly the fps we asked for.
    result["stage"] = "fps-gate"
    recorded = manifest_series_fps(library_path, display_name)
    if recorded is None or abs(recorded - args.fps) > 1e-9:
        result["detail"] = (f"fps gate failed: manifest records {recorded!r}, "
                            f"expected {args.fps:g}")
        return result

    result["stage"] = "verify"
    probes = await asyncio.to_thread(
        probe_all_files, embedder, library_path, display_name,
        args.start_ordinal, args.count, args.stride,
    )
    lap("verify")
    bad = [p for p in probes if p.verdict != "CLEAN"]
    result["files_checked"] = len(probes)
    if bad or not probes:
        result["detail"] = ("gate failed: " +
                            "; ".join(f"{p.rel_path}={p.verdict}/{p.cos_median}" for p in bad[:4]))
        return result

    if args.no_publish:
        result["stage"] = "verified_not_published"
        result["detail"] = f"{len(probes)} files CLEAN; publish skipped by flag"
        return result

    result["stage"] = "publish"
    await LibraryHydrationService.publish_series_release(
        library_type=library_type, display_name=display_name, series_id=series_id,
        merge_existing_release=False,
    )
    lap("publish")

    if not was_local and not args.keep_local:
        result["stage"] = "evict"
        try:
            await LibraryHydrationService.evict_series(
                library_type=library_type, series_id=series_id
            )
        except Exception:
            # Almost always "still pinned by at least one project". The series
            # was not local before the campaign, so dropping it restores prior
            # state; the project it backs goes ghost until matches are recomputed.
            try:
                await force_evict(library_type, series_id)
            except Exception as exc:
                result["detail"] = f"published OK; evict skipped: {exc}"
                result["stage"] = "done_no_evict"
                return result

    lap("evict")
    result["stage"] = "done"
    result["timings"] = " ".join(f"{k}={v:.0f}s" for k, v in timings.items() if v >= 1)
    result["detail"] = f"{len(probes)} files rebuilt and verified CLEAN"
    return result


async def main_async() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-type", default="anime")
    parser.add_argument("--local-tsv", type=Path,
                        default=REPO_ROOT / "docs/audit/triage-local-anime.tsv")
    parser.add_argument("--remote-tsv", type=Path,
                        default=REPO_ROOT / "docs/audit/triage-remote-anime.tsv")
    parser.add_argument("--extra-tsv", type=Path, action="append", default=None,
                        help="Additional triage TSVs to read CORRUPT rows from; repeatable. "
                             "When given, REPLACES the anime-campaign default list (so a "
                             "films_series run does not inherit anime rows).")
    parser.add_argument("--snapshot", type=Path,
                        default=REPO_ROOT / "docs/audit/pre-campaign-local-state.json")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "docs/audit/rebuild-log.tsv")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-free-gb", type=float, default=60.0)
    parser.add_argument("--prefetch-headroom-gb", type=float, default=60.0,
                        help="Extra free space required before the next series is "
                             "downloaded in parallel with the current re-embed.")
    parser.add_argument("--fps", type=float, default=2.0,
                        help="Sampling fps to re-index at and to gate on. The app "
                             "indexes at 2.0; the CLI's own default is 1.0.")
    parser.add_argument("--from-catalog", action="store_true",
                        help="Ignore the triage TSVs and queue every catalog series "
                             "whose recorded fps differs from --fps.")
    parser.add_argument("--only", type=Path, default=None,
                        help="File of series ids, one per line: restrict the queue "
                             "to these (after --from-catalog / TSV selection).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-publish", action="store_true",
                        help="Rebuild and verify but do not publish or evict.")
    parser.add_argument("--keep-local", action="store_true")
    parser.add_argument("--start-ordinal", type=int, default=60)
    parser.add_argument("--count", type=int, default=24)
    # Stride 3, not 10: probe_file decodes sequentially from frame 0 to the
    # highest sampled ordinal (seeking would risk ordinal misalignment and
    # false CORRUPTs), so the stride sets the decoded span, and the decode is
    # what verify's cost actually is — measured 33% of campaign wall-clock.
    # A wide spread buys nothing here: squish corruption is a whole-file
    # property, so 24 frames clustered past the intro (ordinals 60..129,
    # ~65s of video) detect it exactly as well as 60..290 for half the cost.
    parser.add_argument("--stride", type=int, default=3)
    args = parser.parse_args()
    if args.extra_tsv is None:
        args.extra_tsv = [
            REPO_ROOT / "docs/audit/triage-reprobe-winpaths.tsv",
            REPO_ROOT / "docs/audit/triage-unknown-29.tsv",
            REPO_ROOT / "docs/audit/triage-drift-repaired.tsv",
            REPO_ROOT / "docs/audit/triage-unprobeable-2.tsv",
        ]

    LibraryStateDb.initialize()
    from app.config import settings

    library_root = REPO_ROOT / "modules" / "anime_searcher" / "library"
    library_path = AnimeLibraryService.get_library_path(args.library_type)

    # Every triage TSV counts, including the follow-up passes: the Windows-path
    # re-probe and the unknown-29 pass each unmasked CORRUPT verdicts that the
    # first two sweeps could only report as FILE_MISSING.
    catalog = await StorageBoxRepository.list_catalog(args.library_type)
    by_name = {str(e["name"]): str(e["series_id"]) for e in catalog}

    corrupt: list[tuple[str | None, str]] = []
    if args.from_catalog:
        # fps campaign: the defect is "recorded fps != target", readable straight
        # off the catalog — no probing needed to find the queue.
        for entry in catalog:
            recorded = float(entry.get("fps") or 0.0)
            if abs(recorded - args.fps) > 1e-9:
                corrupt.append((str(entry["series_id"]), str(entry["name"])))
    else:
        for tsv in [args.local_tsv, args.remote_tsv, *args.extra_tsv]:
            corrupt += read_corrupt(tsv)
    # Resolve the ids the local sweep could not know (it only had display names).

    resolved: list[tuple[str, str]] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for series_id, name in corrupt:
        sid = series_id or by_name.get(name)
        if not sid:
            unresolved.append(name)
            continue
        if sid in seen:
            continue
        seen.add(sid)
        resolved.append((sid, name))

    if args.only:
        wanted = {line.strip() for line in args.only.read_text(encoding="utf-8").splitlines()
                  if line.strip()}
        resolved = [item for item in resolved if item[0] in wanted]
        missing = wanted - {sid for sid, _ in resolved}
        if missing:
            print(f"--only ids not in the queue (already at target fps, or unknown): "
                  f"{sorted(missing)}", file=sys.stderr)

    prior = prior_local(args.snapshot)
    pinned = pinned_series(settings.library_state_db_path)
    # Pinned first: those series back active projects and must be correct soonest.
    resolved.sort(key=lambda item: (item[0] not in pinned, item[1]))

    done: set[str] = set()
    if args.out.exists() and args.resume:
        for line in args.out.read_text(encoding="utf-8").splitlines()[1:]:
            if line.strip():
                fields = line.split("\t")
                if len(fields) > 2 and fields[2] in {"done", "verified_not_published"}:
                    done.add(fields[0])

    todo = [(sid, name) for sid, name in resolved if sid not in done]
    if args.limit:
        todo = todo[: args.limit]

    print(f"target fps {args.fps:g} | corrupt series: {len(resolved)} ({len(pinned & seen)} pinned) | "
          f"already rebuilt: {len(done)} | this run: {len(todo)}", file=sys.stderr)
    if unresolved:
        print(f"UNRESOLVED (no catalog entry, skipped): {unresolved}", file=sys.stderr)
    if args.dry_run:
        for sid, name in todo:
            flag = "PINNED" if sid in pinned else "      "
            local = "local" if prior.get(sid, False) else "remote"
            print(f"  {flag} {local:6} {sid}  {name}")
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fresh = not args.out.exists()
    handle = args.out.open("a", encoding="utf-8")
    if fresh:
        handle.write("series_id\tseries\tstage\tfiles_checked\ttimings\tdetail\n")
        handle.flush()

    # A previous run may have been killed (or the machine rebooted) between a
    # series' download and its eviction, leaving its files on disk. Nothing
    # downstream cleans those up, and across two nights they would accumulate
    # until the disk floor stops the campaign.
    await reconcile_stranded(args, prior, done, resolved)

    embedder = audit.SSCDEmbedder(audit.resolve_model(library_path, None))

    async def hydrate(series_id: str) -> None:
        await LibraryHydrationService.hydrate_series(
            library_type=args.library_type, series_id=series_id, full_series=True
        )

    prefetch: asyncio.Task | None = None
    prefetch_id: str | None = None

    for position, (series_id, display_name) in enumerate(todo, start=1):
        available = free_gb(library_path)
        if available < args.min_free_gb:
            print(f"stopping: {available:.1f} GB free below the {args.min_free_gb} GB floor",
                  file=sys.stderr)
            break
        print(f"[{position}/{len(todo)}] {display_name} ({available:.0f} GB free)",
              file=sys.stderr)

        # Collect this series' download, whether it was prefetched or not.
        hydrated = False
        try:
            if prefetch is not None and prefetch_id == series_id:
                await prefetch
                hydrated = True
            else:
                if prefetch is not None:
                    prefetch.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await prefetch
                await hydrate(series_id)
                hydrated = True
        except Exception as exc:
            traceback.print_exc()
            row = "\t".join([series_id, display_name, "error", "",
                             f"hydrate failed: {type(exc).__name__}: {exc}"[:300]])
            print(row)
            handle.write(row + "\n")
            handle.flush()
            prefetch, prefetch_id = None, None
            continue
        finally:
            prefetch, prefetch_id = None, None

        # Start the next download now so it overlaps this series' re-embed.
        # Held back when disk is tight: two resident series can be 80+ GB.
        if position < len(todo) and free_gb(library_path) >= args.min_free_gb + args.prefetch_headroom_gb:
            prefetch_id = todo[position][0]
            prefetch = asyncio.create_task(hydrate(prefetch_id))

        try:
            result = await rebuild_one(
                library_type=args.library_type, series_id=series_id,
                display_name=display_name, embedder=embedder,
                library_root=library_root, library_path=library_path,
                was_local=prior.get(series_id, False), args=args,
                hydrated=hydrated,
            )
        except Exception as exc:
            traceback.print_exc()
            result = {"series_id": series_id, "series": display_name, "stage": "error",
                      "detail": f"{type(exc).__name__}: {exc}"[:300]}
        row = "\t".join([
            result.get("series_id", ""), result.get("series", ""),
            result.get("stage", ""), str(result.get("files_checked", "")),
            str(result.get("timings", "")),
            str(result.get("detail", "")).replace("\t", " ").replace("\n", " "),
        ])
        print(row)
        handle.write(row + "\n")
        handle.flush()

    if prefetch is not None:
        prefetch.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await prefetch

    handle.close()


if __name__ == "__main__":
    asyncio.run(main_async())
