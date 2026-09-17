"""Cached anime identity and a deterministic, readable end card."""
from __future__ import annotations

import io
import hashlib
import re
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .facecam_service import FacecamService


class FacecamCoverService:
    @staticmethod
    def title_queries(title: str) -> list[str]:
        """Library titles can contain both a romanized title and an English alias."""
        aliases = [title, re.sub(r"\([^()]*\)", "", title).strip(), *re.findall(r"\(([^()]+)\)", title)]
        return list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip()))[:4]

    @staticmethod
    def normalize_title(title: str) -> str:
        return re.sub(r"\W", "", (title or "").casefold())

    @classmethod
    def exact_matches(cls, title: str, results: list[dict]) -> list[dict]:
        titles = {cls.normalize_title(value) for value in cls.title_queries(title)}
        return [media for media in results if titles.intersection(
            cls.normalize_title(value) for value in [*media["title"].values(), *media.get("synonyms", [])] if value
        )]

    @classmethod
    def search(cls, project_id):
        project = FacecamService.project(project_id)
        query = '''query($search:String){Page(perPage:8){media(search:$search,type:ANIME){id title{romaji english native} synonyms coverImage{extraLarge} siteUrl}}}'''
        results = {}
        with httpx.Client(timeout=30) as client:
            for title in cls.title_queries(project.anime_name or ""):
                response = client.post("https://graphql.anilist.co", json={"query": query, "variables": {"search": title}})
                response.raise_for_status()
                for media in response.json()["data"]["Page"]["media"]:
                    results[media["id"]] = media
        return list(results.values())

    @classmethod
    def select(cls, project_id, media_id):
        # Resolve the URL from AniList itself, never accept an arbitrary download URL from the browser.
        query = '''query($id:Int){Media(id:$id,type:ANIME){id title{romaji english native} coverImage{extraLarge} siteUrl}}'''
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.post("https://graphql.anilist.co", json={"query": query, "variables": {"id": media_id}})
            response.raise_for_status()
            media = response.json()["data"]["Media"]
            response = client.get(media["coverImage"]["extraLarge"])
            response.raise_for_status()
            cls.store(project_id, response.content, {"anilist_id": media["id"], "source_url": media["siteUrl"]})
        return FacecamService.load(project_id)

    @classmethod
    def store(cls, project_id, content: bytes, identity: dict):
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("L'image dépasse 20 Mo.")
        image = Image.open(io.BytesIO(content))
        image.load()
        if image.width < 100 or image.height < 100:
            raise ValueError("La couverture doit mesurer au moins 100 × 100 pixels.")
        root = FacecamService.directory(project_id)
        root.mkdir(parents=True, exist_ok=True)
        path = root / "cover.jpg"
        with FacecamService.lock(project_id):
            FacecamService.update(project_id, {"cover": {**identity, "path": str(path),
                "fingerprint": hashlib.sha256(content).hexdigest()}, "processed_signature": None})
            ImageOps.exif_transpose(image).convert("RGB").save(path, quality=95)
            cls.render(project_id)

    @classmethod
    def ensure(cls, project_id):
        state = FacecamService.load(project_id)
        if state.cover and Path(state.cover["path"]).is_file():
            return cls.render(project_id)
        results = cls.search(project_id)
        exact = cls.exact_matches(FacecamService.project(project_id).anime_name or "", results)
        if len(exact) != 1:
            raise ValueError("Choisissez la bonne couverture dans Facecam, ou importez une image, puis relancez le traitement.")
        cls.select(project_id, exact[0]["id"])
        return cls.render(project_id)

    @classmethod
    def render(cls, project_id):
        state = FacecamService.load(project_id)
        if not state.cover:
            raise ValueError("Couverture manquante.")
        canvas = Image.new("RGB", (1080, 1920), (14, 17, 24))
        cover = Image.open(state.cover["path"]).convert("RGB")
        cover.thumbnail((940, 1420), Image.Resampling.LANCZOS)
        canvas.paste(cover, ((1080 - cover.width) // 2, 80 + (1420 - cover.height) // 2))
        title = FacecamService.project(project_id).anime_name or "Anime"
        font_path = Path(__file__).parents[3] / "assets/fonts/Inter-Black.ttf"
        draw = ImageDraw.Draw(canvas)
        for size in range(64, 23, -2):
            font = ImageFont.truetype(str(font_path), size)
            lines, line = [], ""
            for word in title.split():
                candidate = f"{line} {word}".strip()
                if draw.textlength(candidate, font=font) > 960 and line:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
            if len(lines) <= 4 and all(draw.textlength(line, font=font) <= 960 for line in lines):
                break
        draw.multiline_text((540, 1675), "\n".join(lines), font=font, fill="white", anchor="mm", align="center", spacing=16)
        path = FacecamService.directory(project_id) / "anime_reveal.png"
        canvas.save(path)
        return path
