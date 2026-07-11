#!/usr/bin/env python3
"""Non-public Meta Reel duration capability probe.

Creates standards-compliant synthetic media, lets Meta process unpublished
containers/drafts, and prints JSON Lines without ever serialising credentials.
Run from the repository root, for example:

  pixi run python scripts/meta_reel_duration_probe.py \
    --account-id anime_fr --platform instagram --candidate 899 900 901 \
    --confirm-nonpublic-uploads
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.services.account_service import AccountService  # noqa: E402
from app.services.google_drive_service import GoogleDriveService  # noqa: E402
from app.services.social_upload_service import SocialUploadService  # noqa: E402


def generate_fixture(path: Path, duration: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=540x960:r=24:d={duration}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={duration}",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-g", "48", "-b:v", "120k",
            "-maxrate", "160k", "-bufsize", "320k", "-c:a", "aac",
            "-b:a", "32k", "-shortest", "-movflags", "+faststart", str(path),
        ],
        check=True,
    )
    measured = float(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            text=True,
        ).strip()
    )
    if abs(measured - duration) > 0.01:
        raise RuntimeError(f"fixture duration mismatch: requested={duration}, measured={measured}")


def poll_instagram(base: str, container_id: str, token: str, timeout: int) -> tuple[bool, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = None
        for fields in ("status_code,status", "status_code"):
            candidate = requests.get(
                f"{base}/{container_id}", params={"fields": fields, "access_token": token}, timeout=30
            )
            if candidate.status_code < 400:
                response = candidate
                break
        if response is None:
            return False, "status query rejected"
        payload = response.json()
        code = str(payload.get("status_code") or "").upper()
        if code == "FINISHED":
            return True, code
        if code in {"ERROR", "EXPIRED"}:
            return False, str(payload.get("status") or code)[:300]
        time.sleep(10)
    return False, "processing timeout"


def facebook_processing_result(payload: dict) -> tuple[bool, bool, str]:
    """Return (terminal, accepted, detail) for Graph's varying status shapes."""
    status = payload.get("status")
    if not isinstance(status, dict):
        return False, False, "status unavailable"
    phase = status.get("processing_phase")
    if isinstance(phase, dict) and phase.get("error"):
        return True, False, str(phase["error"])[:300]
    phase_status = str(phase.get("status") or "").lower() if isinstance(phase, dict) else ""
    video_status = str(status.get("video_status") or "").lower()
    if phase_status in {"complete", "completed", "ready"} or video_status in {"ready", "complete", "completed"}:
        return True, True, f"processing {phase_status or video_status}"
    if phase_status in {"error", "failed"} or video_status in {"error", "failed"}:
        return True, False, f"processing {phase_status or video_status}"
    return False, False, f"processing {phase_status or video_status or 'pending'}"


def probe_instagram(account_id: str, candidates: list[int], timeout: int) -> list[dict]:
    creds = AccountService.get_meta_credentials(account_id)
    base = f"https://graph.facebook.com/{settings.meta_graph_api_version}"
    drive = GoogleDriveService.client()
    folder = GoogleDriveService.ensure_subfolder(
        settings.google_drive_parent_folder_id,
        f"_meta_probe_{uuid.uuid4().hex[:10]}",
        drive=drive,
    )
    results: list[dict] = []
    try:
        with tempfile.TemporaryDirectory(prefix="atr-meta-probe-") as tmp:
            for duration in candidates:
                path = Path(tmp) / f"instagram-{duration}.mp4"
                generate_fixture(path, duration)
                uploaded = GoogleDriveService.upload_local_file(
                    parent_id=folder, filename=path.name, local_path=path, drive=drive
                )
                file_id = str(uploaded["id"])
                try:
                    GoogleDriveService.set_public_read(file_id, drive=drive)
                    response = requests.post(
                        f"{base}/{creds.instagram_business_account_id}/media",
                        data={
                            "media_type": "REELS",
                            "video_url": GoogleDriveService.get_direct_download_url(file_id),
                            "caption": "",
                            "share_to_feed": "false",
                            "access_token": creds.instagram_access_token,
                        },
                        timeout=60,
                    )
                    accepted = False
                    detail = "container creation rejected"
                    if response.status_code < 400:
                        accepted, detail = poll_instagram(
                            base, str(response.json().get("id") or ""), creds.instagram_access_token, timeout
                        )
                    result = {"platform": "instagram", "duration_seconds": duration, "accepted": accepted, "detail": detail}
                    results.append(result)
                    print(json.dumps(result, sort_keys=True), flush=True)
                finally:
                    GoogleDriveService.delete_file(file_id, drive=drive)
    finally:
        GoogleDriveService.delete_file(folder, drive=drive)
    return results


def probe_facebook_reels(account_id: str, candidates: list[int], timeout: int) -> list[dict]:
    creds = AccountService.get_meta_credentials(account_id)
    base = f"https://graph.facebook.com/{settings.meta_graph_api_version}"
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="atr-meta-probe-") as tmp:
        for duration in candidates:
            path = Path(tmp) / f"facebook-{duration}.mp4"
            generate_fixture(path, duration)
            video_id = ""
            accepted = False
            detail = "start rejected"
            session = requests.Session()
            try:
                start = session.post(
                    f"{base}/{creds.page_id}/video_reels",
                    data={"upload_phase": "start", "access_token": creds.facebook_page_access_token},
                    timeout=60,
                )
                if start.status_code < 400:
                    payload = start.json()
                    video_id = str(payload.get("video_id") or "")
                    upload_url = str(payload.get("upload_url") or "")
                    with path.open("rb") as source:
                        upload = session.post(
                            upload_url,
                            headers={
                                "Authorization": f"OAuth {creds.facebook_page_access_token}",
                                "offset": "0", "file_size": str(path.stat().st_size),
                                "Content-Type": "application/octet-stream",
                            },
                            data=source,
                            timeout=600,
                        )
                    if upload.status_code < 400:
                        ready, detail = SocialUploadService._ensure_facebook_reel_upload_ready_for_finish(
                            session=session, base=base, video_id=video_id, upload_url=upload_url,
                            token=creds.facebook_page_access_token, video_path=path,
                            file_size=path.stat().st_size, deadline=time.monotonic() + timeout,
                        )
                        if ready:
                            finish = session.post(
                                f"{base}/{creds.page_id}/video_reels",
                                data={"upload_phase": "finish", "video_id": video_id,
                                      "video_state": "DRAFT", "access_token": creds.facebook_page_access_token},
                                timeout=60,
                            )
                            if finish.status_code < 400:
                                deadline = time.time() + timeout
                                while time.time() < deadline:
                                    status_payload = SocialUploadService._get_facebook_video_status_payload(
                                        session=session,
                                        base=base,
                                        video_id=video_id,
                                        token=creds.facebook_page_access_token,
                                        deadline=time.monotonic() + 60,
                                    ) or {}
                                    terminal, accepted, detail = facebook_processing_result(status_payload)
                                    if terminal:
                                        break
                                    time.sleep(10)
                                else:
                                    detail = "processing timeout"
                            else:
                                detail = "finish rejected"
                result = {"platform": "facebook", "path": "reels_draft", "duration_seconds": duration,
                          "accepted": accepted, "detail": detail[:300]}
                results.append(result)
                print(json.dumps(result, sort_keys=True), flush=True)
            finally:
                if video_id:
                    try:
                        session.delete(f"{base}/{video_id}", params={"access_token": creds.facebook_page_access_token}, timeout=30)
                    except Exception:
                        pass
    return results


def probe_facebook_hosted(account_id: str, candidates: list[int], timeout: int) -> list[dict]:
    """Exercise the production /videos + hosted Drive URL draft path."""
    creds = AccountService.get_meta_credentials(account_id)
    base = f"https://graph.facebook.com/{settings.meta_graph_api_version}"
    drive = GoogleDriveService.client()
    folder = GoogleDriveService.ensure_subfolder(
        settings.google_drive_parent_folder_id,
        f"_meta_probe_{uuid.uuid4().hex[:10]}",
        drive=drive,
    )
    results: list[dict] = []
    try:
        with tempfile.TemporaryDirectory(prefix="atr-meta-probe-") as tmp:
            for duration in candidates:
                path = Path(tmp) / f"facebook-hosted-{duration}.mp4"
                generate_fixture(path, duration)
                uploaded = GoogleDriveService.upload_local_file(
                    parent_id=folder, filename=path.name, local_path=path, drive=drive
                )
                file_id = str(uploaded["id"])
                video_id = ""
                session = requests.Session()
                accepted = False
                detail = "draft creation rejected"
                try:
                    GoogleDriveService.set_public_read(file_id, drive=drive)
                    create = session.post(
                        f"{base}/{creds.page_id}/videos",
                        data={
                            "published": "false",
                            "file_url": GoogleDriveService.get_direct_download_url(file_id),
                            "title": "Capability probe",
                            "description": "Unpublished capability probe",
                            "access_token": creds.facebook_page_access_token,
                        },
                        timeout=60,
                    )
                    if create.status_code < 400:
                        video_id = str(create.json().get("id") or "")
                        deadline = time.time() + timeout
                        while time.time() < deadline:
                            payload = SocialUploadService._get_facebook_video_status_payload(
                                session=session, base=base, video_id=video_id,
                                token=creds.facebook_page_access_token,
                                deadline=time.monotonic() + 60,
                            ) or {}
                            terminal, accepted, detail = facebook_processing_result(payload)
                            if terminal:
                                break
                            time.sleep(10)
                        else:
                            detail = "processing timeout"
                    result = {"platform": "facebook", "path": "hosted_videos_draft",
                              "duration_seconds": duration, "accepted": accepted, "detail": detail}
                    results.append(result)
                    print(json.dumps(result, sort_keys=True), flush=True)
                finally:
                    if video_id:
                        try:
                            session.delete(f"{base}/{video_id}", params={"access_token": creds.facebook_page_access_token}, timeout=30)
                        except Exception:
                            pass
                    GoogleDriveService.delete_file(file_id, drive=drive)
    finally:
        GoogleDriveService.delete_file(folder, drive=drive)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--platform", choices=("facebook", "instagram"), required=True)
    parser.add_argument("--candidate", type=int, nargs="+", required=True)
    parser.add_argument("--processing-timeout", type=int, default=1200)
    parser.add_argument("--confirm-nonpublic-uploads", action="store_true")
    args = parser.parse_args()
    if not args.confirm_nonpublic_uploads:
        parser.error("--confirm-nonpublic-uploads is required")
    if any(value < 3 for value in args.candidate):
        parser.error("candidate durations must be at least 3 seconds")
    if args.platform == "instagram":
        results = probe_instagram(args.account_id, args.candidate, args.processing_timeout)
    else:
        results = probe_facebook_reels(args.account_id, args.candidate, args.processing_timeout)
        results.extend(
            probe_facebook_hosted(args.account_id, args.candidate, args.processing_timeout)
        )
    print(json.dumps({"account_id": args.account_id, "platform": args.platform, "results": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
