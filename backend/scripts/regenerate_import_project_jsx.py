"""Regenerate import_project.jsx for existing project(s).

For each project, the JSX is rebuilt from the on-disk artifacts using the
*current* `ProcessingService.generate_jsx_script` logic. Inputs that depend
on probing source files (FPS, audio policies, music) are extracted from the
existing JSX so the source media doesn't need to be locally available.

Optionally re-uploads the regenerated JSX to the project's Drive folder,
replacing the previous import_project.jsx file in-place.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.models import MatchList, Transcription
from app.services.export_service import ExportService
from app.services.google_drive_service import GoogleDriveService
from app.services.otio_timing import FrameRateInfo
from app.services.processing import ProcessingService
from app.services.project_service import ProjectService

logger = logging.getLogger("regenerate_import_project_jsx")


_FPS_NUM_RE = re.compile(r"var SOURCE_FPS_NUM\s*=\s*(\d+);")
_FPS_DEN_RE = re.compile(r"var SOURCE_FPS_DEN\s*=\s*(\d+);")
_MUSIC_FILE_RE = re.compile(r'var MUSIC_FILENAME\s*=\s*"([^"]*)";')
_MUSIC_GAIN_RE = re.compile(r"var MUSIC_GAIN_DB\s*=\s*(-?\d+(?:\.\d+)?);")
_AUDIO_POLICIES_RE = re.compile(
    r"var SOURCE_AUDIO_POLICIES\s*=\s*(\{[\s\S]*?\});", re.MULTILINE
)


def _extract_jsx_constants(jsx_text: str) -> dict[str, Any]:
    """Pull baked-in policies / FPS / music settings from the existing JSX."""
    fps_num_match = _FPS_NUM_RE.search(jsx_text)
    fps_den_match = _FPS_DEN_RE.search(jsx_text)
    music_file_match = _MUSIC_FILE_RE.search(jsx_text)
    music_gain_match = _MUSIC_GAIN_RE.search(jsx_text)
    policies_match = _AUDIO_POLICIES_RE.search(jsx_text)
    if not (fps_num_match and fps_den_match):
        raise RuntimeError("Could not locate SOURCE_FPS_NUM/DEN in existing JSX")
    if not policies_match:
        raise RuntimeError("Could not locate SOURCE_AUDIO_POLICIES block in existing JSX")
    return {
        "source_fps_num": int(fps_num_match.group(1)),
        "source_fps_den": int(fps_den_match.group(1)),
        "music_filename": music_file_match.group(1) if music_file_match else "",
        "music_gain_db": float(music_gain_match.group(1)) if music_gain_match else -24.0,
        "source_audio_policies": json.loads(policies_match.group(1)),
    }


def _frame_rate_from_num_den(num: int, den: int) -> FrameRateInfo:
    """Reconstruct FrameRateInfo from JSX-bound num/den (handles 24000/1001 etc.)."""
    rate = Fraction(num, den)
    if den == 1001:
        timebase = num // 1000
        return FrameRateInfo(timebase=int(timebase), ntsc=True)
    if den == 1:
        return FrameRateInfo(timebase=int(num), ntsc=False)
    return FrameRateInfo.from_fps(float(rate))


def _load_rebuilt_transcription(project_id: str) -> Transcription:
    """Load the post-rebuild transcription saved during processing."""
    output_dir = ExportService.get_output_dir(project_id)
    transcription_path = output_dir / "transcription_timing.json"
    if not transcription_path.exists():
        raise RuntimeError(
            f"Missing rebuilt transcription: {transcription_path}. "
            "Re-run processing for this project before regenerating the JSX."
        )
    return Transcription.model_validate_json(transcription_path.read_text())


def regenerate_jsx_for_project(project_id: str, *, write: bool) -> Path:
    project = ProjectService.load(project_id)
    if project is None:
        raise SystemExit(f"Project not found: {project_id}")

    matches = ProjectService.load_matches(project_id)
    if matches is None:
        raise SystemExit(f"Project {project_id} has no matches.json")

    transcription = _load_rebuilt_transcription(project_id)

    output_dir = ExportService.get_output_dir(project_id)
    jsx_path = output_dir / "import_project.jsx"
    if not jsx_path.exists():
        raise SystemExit(f"Project {project_id} has no existing import_project.jsx")

    constants = _extract_jsx_constants(jsx_path.read_text(encoding="utf-8"))
    source_rate = _frame_rate_from_num_den(
        constants["source_fps_num"], constants["source_fps_den"]
    )

    resolved_scene_sources = ProcessingService.resolve_scene_sources(
        matches.matches,
        source_rate,
        library_type=project.library_type,
    )

    jsx_content = ProcessingService.generate_jsx_script(
        project,
        transcription,
        matches.matches,
        source_rate=source_rate,
        resolved_scene_sources=resolved_scene_sources,
        source_audio_policies=constants["source_audio_policies"],
        subtitle_timing_relative_path=ProcessingService.CLASSIC_SUBTITLE_TIMING_RELATIVE_PATH,
        raw_scene_subtitle_timing_relative_path=ProcessingService.RAW_SCENE_TEXT_SUBTITLE_TIMING_RELATIVE_PATH,
        raw_scene_subtitle_mogrt_relative_dir=ProcessingService.RAW_SCENE_TEXT_SUBTITLE_MOGRT_RELATIVE_DIR,
        music_filename=constants["music_filename"],
        music_gain_db=constants["music_gain_db"],
    )

    if write:
        jsx_path.write_text(jsx_content, encoding="utf-8")
        logger.info("project=%s wrote %s (%d bytes)", project_id, jsx_path, len(jsx_content))
    else:
        logger.info(
            "project=%s dry-run; %d bytes would be written to %s",
            project_id,
            len(jsx_content),
            jsx_path,
        )

    return jsx_path


def upload_jsx_to_drive(project_id: str) -> None:
    project = ProjectService.load(project_id)
    if project is None:
        raise SystemExit(f"Project not found: {project_id}")
    if not project.drive_folder_id:
        raise SystemExit(f"Project {project_id} has no drive_folder_id recorded")
    if not GoogleDriveService.is_configured():
        raise SystemExit("Google Drive is not configured (check .env)")

    output_dir = ExportService.get_output_dir(project_id)
    jsx_path = output_dir / "import_project.jsx"
    if not jsx_path.exists():
        raise SystemExit(f"Missing local JSX to upload for {project_id}: {jsx_path}")

    drive = GoogleDriveService.client()
    folder_id = project.drive_folder_id

    folder_info = drive.files().get(
        fileId=folder_id, fields="id,name", supportsAllDrives=True
    ).execute()
    logger.info(
        "project=%s drive_folder='%s' (id=%s)",
        project_id,
        folder_info.get("name", ""),
        folder_id,
    )

    existing = GoogleDriveService.list_children(folder_id, drive=drive)
    for child in existing:
        if str(child.get("name") or "") == "import_project.jsx":
            file_id = str(child["id"])
            logger.info("deleting existing import_project.jsx (id=%s)", file_id)
            drive.files().delete(fileId=file_id, supportsAllDrives=True).execute()

    result = GoogleDriveService.upload_local_file(
        parent_id=folder_id,
        filename="import_project.jsx",
        local_path=jsx_path,
        drive=drive,
    )
    logger.info("uploaded import_project.jsx -> id=%s", result.get("id"))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate import_project.jsx for one or more projects using the "
            "current ProcessingService.generate_jsx_script logic. Optionally "
            "uploads the new JSX to the project's Drive folder."
        )
    )
    parser.add_argument("project_ids", nargs="+")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute but do not overwrite the on-disk JSX.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="After writing, upload import_project.jsx to the project Drive folder.",
    )
    args = parser.parse_args()

    failures: list[tuple[str, Exception]] = []
    for pid in args.project_ids:
        try:
            regenerate_jsx_for_project(pid, write=not args.dry_run)
            if args.upload and not args.dry_run:
                upload_jsx_to_drive(pid)
        except SystemExit:
            raise
        except Exception as exc:  # pragma: no cover - reported and continued
            logger.exception("project=%s failed: %s", pid, exc)
            failures.append((pid, exc))

    if failures:
        ids = ", ".join(pid for pid, _ in failures)
        raise SystemExit(f"Failures for {len(failures)} project(s): {ids}")


if __name__ == "__main__":
    main()
