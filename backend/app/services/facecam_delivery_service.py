"""Facecam exports contain one self-contained ZIP; never synchronize its Drive folder."""
from __future__ import annotations

import asyncio
from pathlib import Path

from .discord_service import _client
from .facecam_service import FacecamService
from .google_drive_service import GoogleDriveService
from .project_service import ProjectService


class FacecamDeliveryService:
    @staticmethod
    def request(method, path, payload=None):
        with _client() as client:
            response = client.request(method, "/api/internal/facecam" + path, json=payload)
            if response.is_error:
                try:
                    detail = response.json().get("detail", "")
                except ValueError:
                    detail = ""
                raise ValueError(detail or f"Livraison Facecam indisponible (VPS HTTP {response.status_code}). Vérifiez le déploiement et la configuration du serveur.")
            return response.json()

    @classmethod
    def preflight(cls):
        return cls.request("GET", "/health")

    @staticmethod
    def share_folder(drive, folder_id):
        """Upsert the individual folder ACL, never its ancestors."""
        permissions = GoogleDriveService._execute_with_retries(lambda: drive.permissions().list(
            fileId=folder_id, fields="permissions(id,type,role,allowFileDiscovery,permissionDetails)",
            supportsAllDrives=True).execute(), operation="facecam_folder_permissions", drive=drive)
        direct = next((p for p in permissions.get("permissions", []) if p.get("type") == "anyone"
            and not any(d.get("inherited") for d in p.get("permissionDetails", []))), None)
        body = {"role": "writer", "allowFileDiscovery": False}
        if direct:
            return GoogleDriveService._execute_with_retries(lambda: drive.permissions().update(
                fileId=folder_id, permissionId=direct["id"], body=body, fields="id", supportsAllDrives=True).execute(),
                operation="facecam_folder_sharing", drive=drive)
        return GoogleDriveService._execute_with_retries(lambda: drive.permissions().create(
            fileId=folder_id, body={"type": "anyone", **body}, fields="id", supportsAllDrives=True).execute(),
            operation="facecam_folder_sharing", drive=drive)

    @classmethod
    async def upload(cls, project, matches, progress_callback=None):
        with FacecamService.lock(project.id):
            state = FacecamService.load(project.id)
            if project.id in FacecamService._processing or (state.job and state.job.get("status") == "processing"):
                raise ValueError("Attendez la fin de l'opération Facecam avant l'export.")
            FacecamService._processing.add(project.id)
        try:
            return await cls._upload(project, matches, progress_callback)
        finally:
            FacecamService._processing.discard(project.id)

    @classmethod
    async def _upload(cls, project, matches, progress_callback=None):
        from .export_service import ExportService
        from googleapiclient.http import MediaFileUpload
        await asyncio.to_thread(cls.preflight)
        def publish(**values):
            if progress_callback:
                progress_callback({"phase": "upload", "file_count": 1, "files_completed": 0, **values})
        publish(message="Création du ZIP Facecam autonome", uploaded_bytes=0, total_bytes=0)
        bundle = await asyncio.to_thread(ExportService.build_bundle, project, matches)
        folder_name = ExportService.output_folder_name(project)
        def transfer():
            folder_id, folder_url = GoogleDriveService.ensure_project_folder(folder_name, project.drive_folder_id)
            drive = GoogleDriveService.client()
            # Local ACL only. No permission request ever targets the parent folder.
            cls.share_folder(drive, folder_id)
            existing = GoogleDriveService.list_children_named(folder_id, bundle.name, drive=drive)
            if len(existing) > 1:
                raise ValueError("Plusieurs ZIP portent ce nom dans Drive. Conservez-en un et réessayez.")
            callback = lambda frame: publish(current_file=bundle.name, message="Envoi du ZIP Facecam", **frame)
            if existing:
                media = MediaFileUpload(str(bundle), mimetype="application/zip", resumable=True, chunksize=8 * 1024 * 1024)
                request = drive.files().update(fileId=existing[0]["id"], media_body=media,
                    fields="id,name,webViewLink", supportsAllDrives=True)
                uploaded = GoogleDriveService._upload_resumable_request(request, file_size=bundle.stat().st_size,
                    operation="facecam_zip_update", progress_callback=callback)
            else:
                uploaded = GoogleDriveService.upload_local_file(parent_id=folder_id, filename=bundle.name,
                    local_path=bundle, progress_callback=callback, drive=drive)
            return folder_id, folder_url, uploaded
        folder_id, folder_url, uploaded = await asyncio.to_thread(transfer)
        # Persist identifiers before notification so retry updates the same package/folder.
        async with ProjectService.edit_lock(project.id):
            current = await ProjectService.aload(project.id)
            current.drive_folder_id = folder_id
            current.drive_folder_url = folder_url or f"https://drive.google.com/drive/folders/{folder_id}"
            await ProjectService.asave(current)
        task = await asyncio.to_thread(cls.request, "PUT", "/tasks/" + project.id, {
            "anime_title": project.anime_name or "Anime", "folder_id": folder_id,
            "zip_id": uploaded["id"], "zip_name": bundle.name,
        })
        await asyncio.to_thread(cls.persist, project.id, task)
        return {"folder_id": folder_id, "folder_url": current.drive_folder_url, "file_count": 1,
            "total_bytes": bundle.stat().st_size}

    @staticmethod
    def persist(project_id, task):
        FacecamService.update(project_id, {"delivery": task})
        return task

    @classmethod
    def sync(cls, project_id):
        project = FacecamService.project(project_id)
        if not project.drive_folder_id:
            return {"status": "not_sent"}
        return cls.persist(project_id, cls.request("GET", "/tasks/" + project_id))

    @classmethod
    def reopen(cls, project_id):
        FacecamService.project(project_id)
        return cls.persist(project_id, cls.request("POST", "/tasks/" + project_id + "/reopen"))
