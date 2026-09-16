"""Persistent editor handoff, independent of the workstation and Premiere Link."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


class FacecamDrive:
    def __init__(self, settings):
        self.settings = settings
        self._token = None
        self._expires = 0
        self._lock = asyncio.Lock()

    async def request(self, method, resource, *, params=None, body=None):
        async with httpx.AsyncClient(timeout=30) as client:
            async with self._lock:
                if not self._token or time.monotonic() >= self._expires:
                    response = await client.post("https://oauth2.googleapis.com/token", data={
                        "client_id": self.settings.facecam_drive_client_id,
                        "client_secret": self.settings.facecam_drive_client_secret,
                        "refresh_token": self.settings.facecam_drive_refresh_token, "grant_type": "refresh_token"})
                    response.raise_for_status()
                    data = response.json()
                    self._token = data["access_token"]
                    self._expires = time.monotonic() + float(data.get("expires_in", 3600)) - 60
            for attempt in range(3):
                response = await client.request(method, "https://www.googleapis.com/drive/v3/" + resource,
                    params=params, json=body, headers={"Authorization": "Bearer " + self._token})
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if response.status_code == 401:
                    self._expires = 0
                response.raise_for_status()
                return response.json() if response.content else {}

    async def children(self, folder_id):
        result, token = [], None
        while True:
            params = {"q": "'" + folder_id.replace("'", "\\'") + "' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,size,parents)", "pageSize": 1000,
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = await self.request("GET", "files", params=params)
            result.extend(data.get("files", []))
            token = data.get("nextPageToken")
            if not token:
                return result

    async def rename(self, file_id, folder_id):
        current = await self.request("GET", "files/" + file_id, params={"fields": "id,parents", "supportsAllDrives": "true"})
        if folder_id not in current.get("parents", []):
            raise ValueError("Le fichier a été déplacé. Rechargez-le dans le dossier du projet.")
        await self.request("PATCH", "files/" + file_id,
            params={"supportsAllDrives": "true"}, body={"name": "output.mp4"})

    async def archive(self, file_id, folder_id):
        children = await self.children(folder_id)
        folders = [f for f in children if f["name"] == "montages_precedents" and f["mimeType"] == "application/vnd.google-apps.folder"]
        if len(folders) > 1:
            raise ValueError("Plusieurs dossiers montages_precedents existent. Conservez-en un.")
        archive = folders[0] if folders else await self.request("POST", "files", params={"supportsAllDrives": "true"},
            body={"name": "montages_precedents", "mimeType": "application/vnd.google-apps.folder", "parents": [folder_id]})
        current = await self.request("GET", "files/" + file_id, params={"fields": "parents", "supportsAllDrives": "true"})
        if folder_id in current.get("parents", []):
            await self.request("PATCH", "files/" + file_id,
                params={"addParents": archive["id"], "removeParents": folder_id, "supportsAllDrives": "true"},
                body={"name": "output_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + ".mp4"})
        elif archive["id"] not in current.get("parents", []):
            raise ValueError("Le montage précédent ne se trouve plus dans le projet.")


class FacecamDelivery:
    def __init__(self, settings, discord_provider, drive=None):
        self.settings = settings
        self.discord_provider = discord_provider
        self.drive = drive or FacecamDrive(settings)
        self.root = settings.data_dir / "facecam"
        self._locks = {}
        self._channel_lock = asyncio.Lock()

    def lock(self, project_id):
        if not project_id or not all(c.isalnum() or c in "_-" for c in project_id):
            raise ValueError("Identifiant de projet invalide.")
        return self._locks.setdefault(project_id, asyncio.Lock())

    def get(self, project_id):
        self.lock(project_id)
        path = self.root / (project_id + ".json")
        if not path.exists():
            raise KeyError(project_id)
        return json.loads(path.read_text())

    def save(self, task):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".task-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(task, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.root / (task["project_id"] + ".json"))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def configured(self):
        return all((self.settings.facecam_drive_client_id, self.settings.facecam_drive_client_secret, self.settings.facecam_drive_refresh_token))

    async def channel(self):
        if self.settings.facecam_channel_id:
            return self.settings.facecam_channel_id
        async with self._channel_lock:
            discord = self.discord_provider()
            response = await discord._request("GET", f"/guilds/{self.settings.discord.guild_id}/channels")
            channels = [c for c in response.json() if c.get("name") == "montage-facecam" and c.get("type") == 0]
            if len(channels) > 1:
                raise ValueError("Configurez ATR_DISCORD_FACECAM_CHANNEL_ID : plusieurs salons montage-facecam existent.")
            if channels:
                return channels[0]["id"]
            try:
                response = await discord._request("POST", f"/guilds/{self.settings.discord.guild_id}/channels",
                    json_body={"name": "montage-facecam", "type": 0, "topic": "Projets Facecam à monter et livraison des vidéos finales."})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 403:
                    raise ValueError("Autorisez le bot à gérer les salons, ou créez #montage-facecam et configurez ATR_DISCORD_FACECAM_CHANNEL_ID sur le VPS.") from exc
                raise
            return response.json()["id"]

    async def preflight(self):
        if not self.configured():
            raise ValueError("Configurez ATR_FACECAM_DRIVE_CLIENT_ID, ATR_FACECAM_DRIVE_CLIENT_SECRET et ATR_FACECAM_DRIVE_REFRESH_TOKEN sur le VPS.")
        await self.drive.request("GET", "about", params={"fields": "user(displayName)"})
        channel_id = await self.channel()
        discord = self.discord_provider()
        try:
            response = await discord._request("GET", f"/channels/{channel_id}")
            channel = response.json()
            if str(channel.get("guild_id")) != self.settings.discord.guild_id or channel.get("type") != 0:
                raise ValueError("Le salon Facecam doit être un salon texte du serveur Discord configuré.")
        except httpx.HTTPStatusError as exc:
            raise ValueError("Le bot ne peut pas accéder au salon Facecam. Vérifiez son identifiant et les permissions du bot.") from exc
        return {"configured": True, "channel_id": channel_id}

    @staticmethod
    def message(task):
        complete = task["status"] == "complete"
        title = str(task["anime_title"]).replace("@", "＠").replace("`", "")
        content = f"**{title} — Facecam**\nProjet `{task['project_id']}`\n"
        content += ("✅ **Montage terminé.** Le fichier final est disponible dans le dossier Drive." if complete else
            "🎬 **Montage à réaliser.** Téléchargez et extrayez le ZIP, puis lancez `run_in_premiere.bat`.\n"
            "Après montage, envoyez **un seul fichier MP4 directement dans le dossier Drive**, puis confirmez ci-dessous.")
        return content, [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": "Télécharger le ZIP", "url": f"https://drive.google.com/file/d/{task['zip_id']}/view"},
            {"type": 2, "style": 5, "label": "Ouvrir le dossier Drive", "url": f"https://drive.google.com/drive/folders/{task['folder_id']}"},
            {"type": 2, "style": 3, "label": "Confirmer montage envoyé", "custom_id": "facecam:confirm:" + task["project_id"], "disabled": complete},
        ]}]

    async def render(self, task):
        content, components = self.message(task)
        discord = self.discord_provider()
        if task.get("message_id"):
            try:
                await discord.edit_message(task["channel_id"], task["message_id"], content=content, components=components)
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        task["message_id"] = await discord.post_message(task["channel_id"], content=content, components=components)
        self.save(task)

    async def register(self, project_id, payload):
        async with self.lock(project_id):
            if not self.configured():
                raise ValueError("Les identifiants Drive Facecam du VPS sont manquants.")
            try:
                task = self.get(project_id)
                if task["folder_id"] != payload["folder_id"]:
                    raise ValueError("Le dossier Drive d'une tâche existante ne peut pas être remplacé.")
            except KeyError:
                task = {"project_id": project_id, "status": "awaiting", "channel_id": await self.channel(), "message_id": None}
            task.update(payload)
            self.save(task)
            await self.render(task)
            return task

    async def confirm(self, project_id):
        async with self.lock(project_id):
            task = self.get(project_id)
            if task["status"] == "complete":
                await self.render(task)  # recover a failed Discord update after successful rename
                return task
            files = [f for f in await self.drive.children(task["folder_id"])
                if f.get("name", "").lower().endswith(".mp4") and f.get("mimeType") != "application/vnd.google-apps.folder"]
            if not files:
                raise ValueError("Aucun MP4 trouvé. Envoyez votre vidéo directement dans le dossier Drive du projet, puis réessayez.")
            if len(files) != 1:
                raise ValueError("Plusieurs MP4 sont présents. Conservez un seul montage dans le dossier du projet, puis réessayez.")
            if int(files[0].get("size") or 0) <= 0:
                raise ValueError("Le MP4 est vide ou son envoi n'est pas terminé. Attendez la fin de l'envoi.")
            task["output_id"] = files[0]["id"]
            self.save(task)
            await self.drive.rename(task["output_id"], task["folder_id"])
            task.update(status="complete", completed_at=datetime.now(UTC).isoformat())
            self.save(task)
            await self.render(task)
            return task

    async def reopen(self, project_id):
        async with self.lock(project_id):
            task = self.get(project_id)
            if task["status"] == "complete" and task.get("output_id"):
                await self.drive.archive(task["output_id"], task["folder_id"])
                task.update(status="awaiting", output_id=None, completed_at=None)
                self.save(task)
            await self.render(task)
            return task

    async def interaction(self, interaction):
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("facecam:confirm:"):
            return
        project_id = custom_id.removeprefix("facecam:confirm:")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            task = self.get(project_id)
            if (str(interaction.guild_id) != self.settings.discord.guild_id or
                str(interaction.channel_id) != task["channel_id"] or
                not interaction.message or str(interaction.message.id) != task["message_id"]):
                raise ValueError("Cette confirmation ne correspond pas à la tâche active.")
            await self.confirm(project_id)
            await interaction.followup.send("✅ Montage reçu et renommé en output.mp4.", ephemeral=True)
        except (KeyError, ValueError) as exc:
            await interaction.followup.send(str(exc) if not isinstance(exc, KeyError) else "Projet introuvable.", ephemeral=True)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Facecam confirmation failed")
            await interaction.followup.send("Drive ou Discord est momentanément indisponible. Réessayez : votre livraison est conservée.", ephemeral=True)
