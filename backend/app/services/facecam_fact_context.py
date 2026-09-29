"""Episode-local dialogue evidence, with timestamps on the source episode clock."""
from __future__ import annotations

import hashlib
import html
import math
import re
from pathlib import Path

from ..models import MatchList
from .anime_library import AnimeLibraryService
from .facecam_service import FacecamService
from .premiere_subtitle_baker import PremiereSubtitleBakerService
from .project_service import ProjectService


class FacecamFactContext:
    PADDING_SECONDS = 30
    MAX_CUES = 1800
    MAX_TEXT_CHARACTERS = 120_000

    @staticmethod
    def timestamp(seconds: float) -> str:
        milliseconds = round(max(0, seconds) * 1000)
        hours, rest = divmod(milliseconds, 3_600_000)
        minutes, rest = divmod(rest, 60_000)
        seconds, milliseconds = divmod(rest, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"

    @classmethod
    def windows(cls, scenes: list[dict]) -> list[dict]:
        if not scenes:
            return []
        # Preserve dialogue between selected shots: it may explain an event even
        # when that particular shot is absent from the short-form montage.
        start = max(0, min(s["start_seconds"] for s in scenes) - cls.PADDING_SECONDS)
        end = max(s["end_seconds"] for s in scenes) + cls.PADDING_SECONDS
        return [{"start_seconds": round(start, 3), "end_seconds": round(end, 3),
                 "start": cls.timestamp(start), "end": cls.timestamp(end)}]

    @staticmethod
    def clean_text(text: str) -> str:
        text = re.sub(r"\{[^{}]*\}|<[^>]+>", "", text)
        return " ".join(html.unescape(text).replace("\\N", " ").replace("\\n", " ").split())

    @classmethod
    def build(cls, project_id: str) -> dict:
        project = FacecamService.project(project_id)
        reference = FacecamService.directory(project_id) / "reference_matches.json"
        matches = (MatchList.model_validate_json(reference.read_text()) if reference.exists()
                   else ProjectService.load_matches(project_id))
        transcription = FacecamService.reference(project_id)
        if FacecamService.load(project_id).selected_story_id:
            transcription, matches, _ = FacecamService.montage_reference(project_id)
        original = {s.scene_index: s.text for s in transcription.scenes}
        episodes = {}
        for match in matches.matches if matches else []:
            if not match.episode or not all(math.isfinite(t) for t in (match.start_time, match.end_time)) or match.end_time <= match.start_time:
                continue
            episodes.setdefault(match.episode, []).append({
                "scene_index": match.scene_index, "start_seconds": match.start_time,
                "end_seconds": match.end_time, "original_text": original.get(match.scene_index, ""),
            })

        context = {"padding_seconds": cls.PADDING_SECONDS, "clock": "source_episode",
                   "episodes": [], "sources": []}
        remaining_cues, remaining_text = cls.MAX_CUES, cls.MAX_TEXT_CHARACTERS
        for episode, scenes in episodes.items():
            windows = cls.windows(scenes)
            item = {"episode": Path(episode).name, "matched_scenes": scenes, "windows": windows,
                    "status": "unavailable", "language": None, "cues": [], "note": None}
            context["episodes"].append(item)
            try:
                source_path = AnimeLibraryService.resolve_episode_path(episode, library_type=project.library_type)
                if source_path is None:
                    item["note"] = "Épisode introuvable dans la bibliothèque locale."
                    continue
                entries = AnimeLibraryService.load_subtitle_sidecar_entries(source_path)
                groups = AnimeLibraryService.get_preferred_subtitle_language_groups(
                    [entry for entry in entries if entry.kind == "text"], target_language=project.output_language)
                selected = []
                for language, tracks in groups:
                    for track in tracks:
                        asset = AnimeLibraryService.get_subtitle_sidecar_asset_path(source_path, track)
                        if asset is None or not asset.is_file():
                            continue
                        try:
                            cues = PremiereSubtitleBakerService.parse_srt_entries(asset)
                        except (OSError, UnicodeError, ValueError):
                            continue
                        selected = [(cue, cls.clean_text(cue.text)) for cue in cues if any(
                            cue.start < window["end_seconds"] and cue.end > window["start_seconds"] for window in windows)]
                        selected = [(cue, text) for cue, text in selected if text]
                        if selected:
                            item["language"] = language
                            break
                    if selected:
                        break
                if not selected:
                    item["note"] = "Aucun sous-titre textuel disponible sur ces plages (absent, illisible ou uniquement en images)."
                    continue
                item["status"] = "available"
                seen = set()
                for cue, text in sorted(selected, key=lambda c: (c[0].start, c[0].end)):
                    key = (cue.start, cue.end, text)
                    if key in seen:
                        continue
                    seen.add(key)
                    if remaining_cues <= 0 or len(text) > remaining_text:
                        item["status"] = "partial"
                        item["note"] = "Contexte limité : certains sous-titres n'ont pas été transmis."
                        break
                    remaining_cues -= 1
                    remaining_text -= len(text)
                    start, end = cls.timestamp(cue.start), cls.timestamp(cue.end)
                    identity = hashlib.sha256(f"{episode}|{item['language']}|{start}|{end}|{text}".encode()).hexdigest()[:24]
                    url = f"/api/projects/{project_id}/facecam/fact-evidence/{identity}"
                    item["cues"].append({"id": identity, "start": start, "end": end,
                                        "text": text, "source_url": url})
                    context["sources"].append({"id": identity, "url": url, "kind": "subtitle",
                        "title": f"{item['episode']} · {start}–{end}", "text": text,
                        "language": item["language"] or "und"})
            except (OSError, ValueError) as exc:
                item["note"] = f"Sous-titres indisponibles : {type(exc).__name__}."
        return context
