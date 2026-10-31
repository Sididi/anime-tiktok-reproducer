"""Cached anime identity and a deterministic, readable end card."""
from __future__ import annotations

import io
import hashlib
import json
import logging
import re
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

from ..config import settings
from .atomic_files import write_text_atomic
from .facecam_service import FacecamService


logger = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"
MEDIA_FIELDS = ("id title{romaji english native} synonyms format episodes seasonYear popularity isAdult "
                "coverImage{extraLarge} siteUrl")
# Release-group noise left in library folder names ("... S01 1080p WEBRip AAC x265-EMBER").
RELEASE_TAGS = re.compile(
    r"\[[^\]]*\]|\{[^}]*\}|\b(?:S\d{1,2}(?:E\d{1,3})?|\d{3,4}p|WEB-?(?:Rip|DL)|Blu-?Ray|BDRip|BD|HEVC|AVC|"
    r"x26[45]|H\.?26[45]|AAC(?:\d\.\d)?|FLAC|E?AC3|DDP?(?:\d\.\d)?|10bit|8bit|AMZN|NF|CR|DSNP|MultiSub|"
    r"Dual[- ]Audio)\b(?:-[^\W_]+)?", re.I)


class FacecamCoverService:
    MAX_DISCOVERY_QUERIES = 4
    MAX_ATTEMPTS = 4
    COVER_BOX = (940, 1420)
    _identity_lock = threading.Lock()

    @staticmethod
    def title_queries(title: str) -> list[str]:
        """Library titles can contain both a romanized title and an English alias."""
        title = re.sub(r"\s+", " ", RELEASE_TAGS.sub(" ", title.replace("_", " "))).strip(" -")
        aliases = [title, re.sub(r"\([^()]*\)", "", title).strip(), *re.findall(r"\(([^()]+)\)", title)]
        aliases += [part for alias in aliases for part in alias.split(" | ") if " | " in alias]
        return list(dict.fromkeys(alias.strip(" -") for alias in aliases if alias.strip(" -")))[:5]

    @staticmethod
    def normalize_title(title: str) -> str:
        """Compare titles across accents, punctuation, spacing and romanization of long vowels."""
        # Drop Latin accents only: Japanese voiced marks are meaningful and recompose under NFKC.
        text = unicodedata.normalize("NFKC", re.sub(r"[\u0300-\u036f]", "", unicodedata.normalize("NFKD", title or "")))
        words = re.findall(r"[^\W_]+", text.casefold().replace("&", " and "))
        text = "".join("o" if word == "wo" else word for word in words).replace("ou", "o")
        return re.sub(r"([aeiou])\1+", r"\1", text)

    @classmethod
    def discovery_queries(cls, title: str) -> list[str]:
        """Broaden retrieval when catalog word spacing defeats full-title search.

        These terms only discover candidates. They are never used to establish
        identity: auto-selection still requires the complete title or an alias.
        """
        primary = re.sub(r"\([^()]*\)", "", cls.display_title(title)).strip()
        # "Chiisana Eiyuu - Modest Heroes": a subtitle half is a better query than any single word.
        parts = [part.strip() for alias in cls.title_queries(title) for part in re.split(r" - |: ", alias)
                 if re.search(r" - |: ", alias) and len(part.strip()) >= 4]
        words = sorted(dict.fromkeys(word for word in re.findall(r"[^\W_]+", primary) if len(word) >= 5),
                       key=len, reverse=True)
        return list(dict.fromkeys([*parts, *words]))[:cls.MAX_DISCOVERY_QUERIES]

    @classmethod
    def display_title(cls, title: str) -> str:
        return next(iter(cls.title_queries(title)), "")

    @staticmethod
    def media_titles(media: dict) -> list[str]:
        return [value for value in [*(media.get("title") or {}).values(), *(media.get("synonyms") or [])] if value]

    @classmethod
    def exact_matches(cls, title: str, results: list[dict]) -> list[dict]:
        titles = {cls.normalize_title(value) for value in cls.title_queries(title)} - {""}
        return [media for media in results if titles.intersection(
            cls.normalize_title(value) for value in cls.media_titles(media)
        )]

    @classmethod
    def shares_word(cls, title: str, media: dict) -> bool:
        """Lexical evidence a model guess must have: one significant word in common with a catalog title."""
        def words(text):
            return {cls.normalize_title(word) for word in re.findall(r"[^\W_]+", text) if len(word) >= 4}
        return bool(words(title) & set().union(*map(words, cls.media_titles(media))))

    @classmethod
    def rank(cls, title: str, matches: list[dict], episodes: int | None) -> list[dict]:
        """Order equally named entries (seasons differing by "!!", remakes, OVAs, spin-offs).

        A main-title match beats a synonym ("Black Clover" over "Petit Clover"), then the library
        episode count decides, then the literal spelling, then real works over specials, then popularity.
        """
        aliases = cls.title_queries(title)
        names = {cls.normalize_title(value) for value in aliases}
        literal = {re.sub(r"\s+", " ", value.casefold()) for value in aliases}
        def key(media):
            main = [value for value in (media.get("title") or {}).values() if value]
            gap = abs(media["episodes"] - episodes) if episodes and media.get("episodes") else 10_000
            return (not names.intersection(cls.normalize_title(value) for value in main), gap,
                    not literal.intersection(re.sub(r"\s+", " ", value.casefold()) for value in main),
                    media.get("format") in {"SPECIAL", "MUSIC", "CM", "PV"}, -(media.get("popularity") or 0))
        return sorted(matches, key=key)

    @classmethod
    def _post(cls, client, body):
        """AniList rate-limits at 30-90 requests/minute; wait as instructed instead of failing processing."""
        for attempt in range(cls.MAX_ATTEMPTS):
            try:
                response = client.post(ANILIST_URL, json=body)
            except httpx.TransportError:
                if attempt == cls.MAX_ATTEMPTS - 1:
                    raise
                time.sleep(2 ** attempt)
                continue
            status = getattr(response, "status_code", 200)
            if (status == 429 or status >= 500) and attempt < cls.MAX_ATTEMPTS - 1:
                time.sleep(min(float(response.headers.get("Retry-After") or 2 ** attempt), 65))
                continue
            response.raise_for_status()
            return response.json()

    @classmethod
    def search(cls, project_id):
        return cls.search_title(FacecamService.project(project_id).anime_name or "")

    @classmethod
    def search_title(cls, title: str) -> list[dict]:
        query = 'query($search:String){Page(perPage:8){media(search:$search,type:ANIME){' + MEDIA_FIELDS + '}}}'
        results = {}
        with httpx.Client(timeout=30) as client:
            queried = set()

            def fetch(search):
                queried.add(search.casefold())
                payload = cls._post(client, {"query": query, "variables": {"search": search}})
                page = (payload.get("data") or {}).get("Page")
                if payload.get("errors") or not isinstance(page, dict) or not isinstance(page.get("media"), list):
                    raise ValueError("La recherche AniList est indisponible. Réessayez dans quelques instants.")
                for media in page["media"]:
                    if (not isinstance(media, dict) or type(media.get("id")) is not int
                            or not isinstance(media.get("title"), dict)):
                        raise ValueError("La recherche AniList a renvoyé une réponse invalide. Réessayez.")
                    results[media["id"]] = media

            for search in cls.title_queries(title):
                fetch(search)
            if not cls.exact_matches(title, list(results.values())):
                for search in cls.discovery_queries(title):
                    if search.casefold() not in queried:
                        fetch(search)
            logger.info("Facecam cover lookup title=%r queries=%d candidates=%d exact=%s",
                        title, len(queried), len(results),
                        [media["id"] for media in cls.exact_matches(title, list(results.values()))])
        return list(results.values())

    @classmethod
    def select(cls, project_id, media_id, *, remember=True):
        # Resolve the URL from AniList itself, never accept an arbitrary download URL from the browser.
        query = 'query($id:Int){Media(id:$id,type:ANIME){id title{romaji english native} synonyms coverImage{extraLarge} siteUrl}}'
        full = cls.normalize_title(cls.display_title(FacecamService.project(project_id).anime_name or ""))
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            media = cls._post(client, {"query": query, "variables": {"id": media_id}})["data"]["Media"]
            response = client.get(media["coverImage"]["extraLarge"])
            response.raise_for_status()
            # The catalog spelling of the very same title reads better on the card ("17-26", not "17_26").
            spelled = next((value for value in cls.media_titles(media) if cls.normalize_title(value) == full), None)
            cls.store(project_id, response.content, {"anilist_id": media["id"], "source_url": media["siteUrl"],
                                                     **({"title": spelled} if spelled else {})}, remember=remember)
        return FacecamService.load(project_id)

    @classmethod
    def store(cls, project_id, content: bytes, identity: dict, *, remember=True):
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
        if remember:
            cls.remember(project_id, identity, path)

    # A series keeps its identity across projects: one choice (automatic, picked or uploaded) serves them all.
    @staticmethod
    def identity_key(project) -> str:
        if project.series_id:
            return f"{getattr(project.library_type, 'value', project.library_type) or 'anime'}:{project.series_id}"
        name = FacecamCoverService.normalize_title(project.anime_name or "")
        return f"name:{name}" if name else ""

    @staticmethod
    def identity_path() -> Path:
        return settings.data_dir / "facecam_cover_identities.json"

    @classmethod
    def identities(cls) -> dict:
        try:
            return json.loads(cls.identity_path().read_text())
        except (OSError, ValueError):
            return {}

    @classmethod
    def remember(cls, project_id, identity, cover_path):
        project = FacecamService.project(project_id)
        key = cls.identity_key(project)
        if not key:
            return
        entry = {"anilist_id": identity["anilist_id"]} if identity.get("anilist_id") else None
        if entry is None and identity.get("manual"):
            shared = settings.data_dir / "facecam_covers" / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".jpg")
            shared.parent.mkdir(parents=True, exist_ok=True)
            shared.write_bytes(Path(cover_path).read_bytes())
            entry = {"manual": True, "path": str(shared)}
        if entry is None:
            return
        with cls._identity_lock:
            identities = cls.identities()
            identities[key] = {**entry, "anime_name": project.anime_name, "project_id": project_id}
            write_text_atomic(cls.identity_path(), json.dumps(identities, ensure_ascii=False, indent=2, sort_keys=True))

    @classmethod
    def remembered(cls, project_id) -> dict | None:
        project = FacecamService.project(project_id)
        key = cls.identity_key(project)
        if not key:
            return None
        if entry := cls.identities().get(key):
            return entry
        # Covers chosen before this registry existed live only in their project's state.
        for state_path in sorted(settings.projects_dir.glob("*/facecam/state.json")):
            other_id = state_path.parent.parent.name
            if other_id == project_id:
                continue
            try:
                other = FacecamService.project(other_id)
                cover = json.loads(state_path.read_text()).get("cover") or {}
            except Exception:
                continue
            if cls.identity_key(other) == key and cover.get("anilist_id"):
                return {"anilist_id": cover["anilist_id"]}
        return None

    @classmethod
    def episode_count(cls, project) -> int | None:
        from .library_state_db import LibraryStateDb
        if not project.series_id:
            return None
        try:
            row = LibraryStateDb.get_series_state(project.library_type or "anime", project.series_id)
        except Exception:
            return None
        return (row.expected_episode_count or row.local_episode_count or None) if row else None

    @classmethod
    def judge(cls, project, results: list[dict]) -> dict | None:
        """Only for names no catalog title equals exactly; the model may only answer with a listed id or none."""
        from .llm_service import LLMService
        # An exact title may be adult; a guessed one must never be.
        results = [media for media in results if not media.get("isAdult")]
        if not results or not LLMService.is_configured():
            return None
        candidates = [{"id": m["id"], "titles": cls.media_titles(m), "format": m.get("format"),
                       "episodes": m.get("episodes"), "year": m.get("seasonYear")} for m in results]
        try:
            episodes = sorted({m.episode for m in FacecamService.source_matches(project.id).matches if m.episode})[:3]
        except Exception:
            episodes = []
        prompt = (
            "A local anime library folder must be identified on AniList to show its cover.\n"
            f"Folder name: {project.anime_name!r}\n"
            f"Episode files in the folder: {cls.episode_count(project) or 'unknown'}\n"
            f"Sample episode file names: {json.dumps(episodes, ensure_ascii=False)}\n"
            f"AniList candidates: {json.dumps(candidates, ensure_ascii=False)}\n\n"
            "Return the id of the candidate that is this exact work: its title must be the same title as the "
            "folder name, possibly translated, romanized, abbreviated, misspelled or followed by release tags. "
            "A similar theme, genre or a shared word is NOT enough. Same series and same season/part; when "
            "several seasons or films are bundled in the folder, choose the first one. "
            "When you are not sure, return 0: a wrong anime is much worse than none."
        )
        schema = {"type": "object", "additionalProperties": False, "required": ["anilist_id"],
                  "properties": {"anilist_id": {"type": "integer", "enum": [*(m["id"] for m in results), 0]}}}
        try:
            answer = LLMService.generate_json(prompt, preset_key=project.resolved_llm_preset_key(), tier="big",
                                              response_schema=schema, schema_name="anilist_identity")
        except Exception:
            logger.exception("Facecam cover identity judge failed project=%s", project.id)
            return None
        chosen = next((m for m in results if m["id"] == answer.get("anilist_id")), None)
        if chosen and not cls.shares_word(project.anime_name or "", chosen):
            logger.warning("Facecam cover judge choice %s shares no word with %r; ignored", chosen["id"], project.anime_name)
            chosen = None
        logger.info("Facecam cover identity judge project=%s name=%r chosen=%s", project.id, project.anime_name,
                    chosen and chosen["id"])
        return chosen

    @classmethod
    def frame_cover(cls, project_id) -> bytes:
        """Last resort: a detailed frame of the matched anime itself is always the right identity."""
        from .anime_library import AnimeLibraryService
        project = FacecamService.project(project_id)
        matches = sorted((m for m in FacecamService.source_matches(project_id).matches if m.episode),
                         key=lambda m: (m.end_time - m.start_time) * (m.confidence or 0), reverse=True)
        best, best_detail = None, -1.0
        for match in matches[:6]:
            path = AnimeLibraryService.resolve_episode_path(match.episode, library_type=project.media_library)
            if path is None:
                continue
            frame = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{(match.start_time + match.end_time) / 2:.3f}", "-i", str(path),
                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True, timeout=60).stdout
            if not frame:
                continue
            detail = sum(ImageStat.Stat(Image.open(io.BytesIO(frame)).convert("L")).stddev)
            if detail > best_detail:
                best, best_detail = frame, detail
        if best is None:
            raise ValueError("Impossible de créer la couverture : aucun épisode source n'est lisible.")
        return best

    @classmethod
    def ensure(cls, project_id):
        state = FacecamService.load(project_id)
        current = state.cover if state.cover and Path(state.cover["path"]).is_file() else None
        if current and not current.get("frame"):
            return cls.render(project_id)
        project = FacecamService.project(project_id)
        title = project.anime_name or ""
        remembered = cls.remembered(project_id)
        if remembered and remembered.get("manual") and Path(remembered["path"]).is_file():
            cls.store(project_id, Path(remembered["path"]).read_bytes(), {"manual": True, "shared": True},
                      remember=False)
            return cls.render(project_id)
        try:
            if remembered and remembered.get("anilist_id"):
                cls.select(project_id, remembered["anilist_id"])
                return cls.render(project_id)
            results = cls.search(project_id)
            exact = cls.exact_matches(title, results)
            media = cls.rank(title, exact, cls.episode_count(project))[0] if exact else cls.judge(project, results)
            if media:
                cls.select(project_id, media["id"])
                return cls.render(project_id)
            logger.warning("Facecam cover: no AniList identity for project=%s name=%r", project_id, title)
        except Exception:
            logger.exception("Facecam cover: AniList lookup failed project=%s name=%r", project_id, title)
        # Never remembered: a later run (or a manual choice) can still find the real cover.
        if current:
            return cls.render(project_id)
        cls.store(project_id, cls.frame_cover(project_id), {"frame": True}, remember=False)
        return cls.render(project_id)

    @classmethod
    def card_cover(cls, project_id, cover: dict) -> Image.Image:
        """Catalog covers (~460 px wide) must fill the card: upscale once per cover, then reuse."""
        from .facecam_cover_upscale import upscale
        image = Image.open(cover["path"]).convert("RGB")
        if image.width >= cls.COVER_BOX[0] or image.height >= cls.COVER_BOX[1]:
            return image
        fingerprint = cover.get("fingerprint") or hashlib.sha256(Path(cover["path"]).read_bytes()).hexdigest()
        cached = FacecamService.directory(project_id) / f"cover_hd-{fingerprint[:16]}.jpg"
        if cached.is_file():
            return Image.open(cached).convert("RGB")
        try:
            # Half the box in, 4x out: bounded memory and still above the card resolution.
            upscaled = upscale(ImageOps.contain(image, (cls.COVER_BOX[0] // 2, cls.COVER_BOX[1] // 2),
                                                Image.Resampling.LANCZOS) if image.width > cls.COVER_BOX[0] // 2
                               or image.height > cls.COVER_BOX[1] // 2 else image)
        except Exception:
            logger.exception("Facecam cover upscale failed project=%s; using a plain resize", project_id)
            return image
        for old in cached.parent.glob("cover_hd-*.jpg"):
            old.unlink(missing_ok=True)
        upscaled.save(cached, quality=95)
        return upscaled

    @classmethod
    def render(cls, project_id):
        state = FacecamService.load(project_id)
        if not state.cover:
            raise ValueError("Couverture manquante.")
        canvas = Image.new("RGB", (1080, 1920), (14, 17, 24))
        cover = ImageOps.contain(cls.card_cover(project_id, state.cover), cls.COVER_BOX, Image.Resampling.LANCZOS)
        canvas.paste(cover, ((1080 - cover.width) // 2, 80 + (1420 - cover.height) // 2))
        title = state.cover.get("title") or cls.display_title(FacecamService.project(project_id).anime_name or "") or "Anime"
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
