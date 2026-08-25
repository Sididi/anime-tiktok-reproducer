"""Reconcile the _SPM_SHARED_SOURCES Drive folder against live project manifests.

Dry-run by default: prints orphans (on Drive, referenced by no live project),
missing files (referenced but absent on Drive — those projects need a Drive
re-export), and unreadable manifests. ``--apply`` deletes the orphans (refused
while any manifest is unreadable).

Run from backend/ (e.g. ``pixi run python scripts/audit_drive_shared_sources.py``).
"""

from __future__ import annotations

import argparse
import json

from app.services.drive_shared_sources import DriveSharedSources
from app.services.google_drive_service import GoogleDriveService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete orphaned shared files (default: report only)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the raw report as JSON"
    )
    args = parser.parse_args()

    if not GoogleDriveService.is_configured():
        raise SystemExit("Google Drive integration is not configured")

    report = DriveSharedSources.audit(apply=args.apply)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print(f"Shared folder: {report['shared_folder_id']}")
    print(
        f"Files on Drive: {report['drive_file_count']}  "
        f"referenced by manifests: {report['referenced_count']}"
    )

    if report["unreadable_manifests"]:
        print(
            "\nUNREADABLE manifests (blocks --apply): "
            + ", ".join(report["unreadable_manifests"])
        )

    if report["orphans"]:
        print(f"\nOrphans ({len(report['orphans'])} — on Drive, referenced by nobody):")
        for name in report["orphans"]:
            print(f"  {name}")
    else:
        print("\nNo orphans.")

    if report["missing"]:
        print(
            f"\nMISSING ({len(report['missing'])} — referenced but absent on Drive; "
            "re-run the Drive export for the listed projects):"
        )
        for item in report["missing"]:
            print(f"  {item['shared_name']}  <- {', '.join(item['projects'])}")
    else:
        print("No missing shared files.")

    if args.apply:
        if report["applied"]:
            print(f"\nDeleted {len(report['deleted'])} orphan(s).")
        else:
            print("\n--apply refused (unreadable manifests present).")


if __name__ == "__main__":
    main()
