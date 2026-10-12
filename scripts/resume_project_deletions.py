"""Resume an explicit saved Project Manager selection with bounded concurrency.

Only IDs in the supplied manifest are submitted. Completed archives are
skipped, and every outcome is checkpointed so interruption loses no selection.
Run against the local backend whose project directory is supplied here.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

import httpx


def archived(projects_dir: Path, project_id: str) -> bool:
    path = projects_dir / project_id / "project.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    return bool(data.get("archived_at"))


async def run(args):
    manifest = json.loads(args.manifest.read_text())
    ids = [project["project_id"] for project in manifest["projects"]]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[a-zA-Z0-9_-]+", id) for id in ids):
        raise ValueError("Selection must contain unique, valid project IDs")
    report = {
        "manifest": str(args.manifest.resolve()),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "concurrency": args.workers,
        "projects": {},
    }
    args.results.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        temp = args.results.with_suffix(".tmp")
        temp.write_text(json.dumps(report, indent=2))
        temp.replace(args.results)

    pending = asyncio.Queue()
    for project_id in ids:
        if archived(args.projects_dir, project_id):
            report["projects"][project_id] = {"status": "already_archived"}
        else:
            pending.put_nowait(project_id)
    checkpoint()
    print(f"Selection: {len(ids)}; already archived: {len(report['projects'])}; pending: {pending.qsize()}", flush=True)
    started = time.monotonic()

    async with httpx.AsyncClient(base_url=args.api, timeout=httpx.Timeout(600, connect=10)) as client:
        async def worker():
            while not pending.empty():
                project_id = pending.get_nowait()
                before = time.monotonic()
                report["projects"][project_id] = {"status": "running"}
                checkpoint()
                try:
                    # Recheck immediately before submission, including retries
                    # of an earlier run whose response was lost.
                    if archived(args.projects_dir, project_id):
                        outcome = {"status": "already_archived"}
                    else:
                        response = await client.delete(f"/api/project-manager/projects/{project_id}", params={"confirmed": "true"})
                        data = response.json()
                        if response.status_code == 410 and archived(args.projects_dir, project_id):
                            outcome = {"status": "already_archived"}
                        elif response.is_success and data.get("local_deleted") and archived(args.projects_dir, project_id):
                            outcome = {"status": "archived", "archive": data.get("archive"), "warnings": data.get("cleanup_warnings", [])}
                        else:
                            outcome = {"status": "failed", "http_status": response.status_code, "detail": data.get("detail", data)}
                except Exception as error:
                    outcome = {"status": "needs_verification" if archived(args.projects_dir, project_id) else "failed", "detail": str(error)}
                outcome["elapsed_seconds"] = round(time.monotonic() - before, 2)
                report["projects"][project_id] = outcome
                checkpoint()
                print(f"{project_id}: {outcome['status']} ({outcome['elapsed_seconds']}s)" + (f" {outcome['detail']}" if "detail" in outcome else ""), flush=True)
                pending.task_done()

        await asyncio.gather(*(worker() for _ in range(args.workers)))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    checkpoint()
    totals = {}
    for outcome in report["projects"].values():
        totals[outcome["status"]] = totals.get(outcome["status"], 0) + 1
    print(json.dumps({"totals": totals, "elapsed_seconds": report["elapsed_seconds"], "results": str(args.results)}), flush=True)
    return 1 if any(status in totals for status in ("failed", "needs_verification")) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--projects-dir", type=Path, default=Path(__file__).resolve().parents[1] / "backend/data/projects")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--workers", type=int, choices=range(1, 7), default=6)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
