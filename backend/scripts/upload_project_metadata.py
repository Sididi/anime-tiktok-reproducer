from __future__ import annotations

import argparse
import logging
import sys

from app.services.google_drive_service import GoogleDriveService
from app.services.project_service import ProjectService

logger = logging.getLogger("upload_project_metadata")


METADATA_FILENAMES = ("metadata.json", "metadata.html")


def _upload_metadata_for_project(project_id: str) -> None:
    project = ProjectService.load(project_id)
    if project is None:
        raise SystemExit(f"Project not found: {project_id}")
    if not project.drive_folder_id:
        raise SystemExit(f"Project {project_id} has no drive_folder_id recorded")

    metadata_json = ProjectService.get_metadata_file(project_id)
    metadata_html = ProjectService.get_metadata_html_file(project_id)
    if not metadata_json.exists():
        raise SystemExit(f"Missing {metadata_json}")
    if not metadata_html.exists():
        raise SystemExit(f"Missing {metadata_html}")

    if not GoogleDriveService.is_configured():
        raise SystemExit("Google Drive is not configured (check .env)")

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

    metadata_subfolder_id = GoogleDriveService.ensure_subfolder(
        folder_id, "metadata", drive=drive
    )
    logger.info("metadata subfolder id=%s", metadata_subfolder_id)

    existing = GoogleDriveService.list_children(metadata_subfolder_id, drive=drive)
    for child in existing:
        name = str(child.get("name") or "")
        if name in METADATA_FILENAMES:
            file_id = str(child["id"])
            logger.info("deleting existing %s (id=%s)", name, file_id)
            drive.files().delete(fileId=file_id, supportsAllDrives=True).execute()

    for filename, local_path in (
        ("metadata.json", metadata_json),
        ("metadata.html", metadata_html),
    ):
        result = GoogleDriveService.upload_local_file(
            parent_id=metadata_subfolder_id,
            filename=filename,
            local_path=local_path,
            drive=drive,
        )
        logger.info("uploaded %s -> id=%s", filename, result.get("id"))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(
        description="Upload only metadata.json/metadata.html for the given project(s) to their existing Drive folder, without clearing other files."
    )
    parser.add_argument("project_ids", nargs="+")
    args = parser.parse_args()

    for pid in args.project_ids:
        _upload_metadata_for_project(pid)


if __name__ == "__main__":
    main()
