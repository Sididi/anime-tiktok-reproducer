"""A factual intermediate representation between source prose and Facecam writing.

The writer receives only this validated brief. Source scene IDs are retained for
montage continuity, but the source's sentences/hook are not passed through.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from .atomic_files import write_text_atomic

PROMPTS = Path(__file__).parents[2] / "prompts" / "facecam"


class BriefModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NarrativeFact(BriefModel):
    scene_indices: list[StrictInt] = Field(min_length=1)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Empty narrative fact")
        return value.strip()


class NarrativeSection(BriefModel):
    id: str
    facts: list[NarrativeFact] = Field(min_length=1)


class NarrativeBrief(BriefModel):
    premise: str = Field(min_length=1)
    protagonist: str = Field(min_length=1)
    sections: list[NarrativeSection] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)


class FacecamNarrationService:
    @staticmethod
    def prompt(name: str) -> str:
        return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")

    @staticmethod
    def validate_body(value, blocks) -> dict[str, str]:
        bodies = value.get("blocks", []) if isinstance(value, dict) else []
        if (not isinstance(bodies, list)
                or any(not isinstance(b, dict) or not isinstance(b.get("text"), str) for b in bodies)
                or [b.get("id") for b in bodies] != [b.id for b in blocks if b.kind == "body"]):
            raise ValueError("La génération n'a pas respecté les extraits originaux. Réessayez.")
        texts = {b["id"]: b["text"].strip() for b in bodies}
        if any(not text for text in texts.values()):
            raise ValueError("Un passage du récit est vide.")
        return texts

    @staticmethod
    def validate_brief(value, blocks) -> NarrativeBrief:
        brief = NarrativeBrief.model_validate(value)
        bodies = [b for b in blocks if b.kind == "body"]
        if [s.id for s in brief.sections] != [b.id for b in bodies]:
            raise ValueError("La fiche narrative doit conserver les blocs entre les extraits originaux.")
        for section, block in zip(brief.sections, bodies, strict=True):
            indices = [i for fact in section.facts for i in fact.scene_indices]
            if indices != block.scene_indices:
                raise ValueError("La fiche narrative doit couvrir tous les plans dans leur ordre, sans traverser un extrait original.")
        return brief

    @classmethod
    def brief(cls, directory: Path, context: dict, blocks, generate: Callable) -> dict:
        extraction_prompt = cls.prompt("brief")
        fingerprint = hashlib.sha256(json.dumps({
            "context": context, "prompt": extraction_prompt,
            "blocks": [b.model_dump() for b in blocks],
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = directory / "narrative_brief.json"
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached["fingerprint"] == fingerprint:
                    brief = cls.validate_brief(cached["brief"], blocks)
                    return cls.writer_context(context, blocks, brief)
            except (ValueError, KeyError, TypeError, ValidationError):
                pass  # Old/partial cache cannot feed the writer; extract again.
        payload = {**context, "body_blocks": [b.model_dump() for b in blocks if b.kind == "body"]}
        brief = cls.validate_brief(generate(extraction_prompt + "\n" + json.dumps(payload, ensure_ascii=False)), blocks)
        directory.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({"fingerprint": fingerprint, "brief": brief.model_dump()},
                                         ensure_ascii=False, indent=2))
        return cls.writer_context(context, blocks, brief)

    @staticmethod
    def writer_context(context: dict, blocks, brief: NarrativeBrief) -> dict:
        # Only original dialogue survives verbatim: it is a protected audio insert,
        # not narration to imitate. Durations are montage cues, not word budgets.
        return {"anime_title_do_not_say": context["anime"], **brief.model_dump(),
                "timeline": [{"id": b.id, "kind": b.kind, "scene_indices": b.scene_indices}
                             for b in blocks if b.kind in {"body", "raw"}],
                "original_dialogue": [{"scene_index": s["scene_index"], "text": s["text"]}
                                      for s in context["scenes"] if s["raw"]]}
