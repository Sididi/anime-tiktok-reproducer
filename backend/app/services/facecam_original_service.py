"""Write an anime introduction from researched facts, without the source story."""
from __future__ import annotations

import json
import math
import uuid

from pydantic import BaseModel, ConfigDict, Field

from .atomic_files import write_text_atomic
from .facecam_narration_service import FacecamNarrationService
from .facecam_service import FacecamService, StaleFacecamRevision
from .facecam_story_service import FacecamStoryService
from .openrouter_service import OpenRouterService


class OriginalFact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1)
    source_urls: list[str] = Field(min_length=1)


class OriginalBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    premise: str = Field(min_length=1)
    protagonist: str = Field(min_length=1)
    narrative_engine: str = Field(min_length=1)
    setup_facts: list[OriginalFact]
    spoiler_boundary: str = Field(min_length=1)
    uncertainties: list[str]


class FacecamOriginalService:
    TARGET_SECONDS = (60, 75)

    @staticmethod
    def current(project_id, revision):
        state = FacecamService.load(project_id)
        if state.revision != revision:
            raise StaleFacecamRevision("Le script a changé. Rechargez la version actuelle.")
        return state

    @staticmethod
    def validate_brief(value, evidence):
        brief = OriginalBrief.model_validate(value)
        if not brief.setup_facts:
            raise ValueError("La prémisse de l'anime n'est pas assez étayée. Le script actuel est conservé.")
        urls = {s["url"] for s in evidence["sources"]}
        if any(url not in urls for fact in brief.setup_facts for url in fact.source_urls):
            raise ValueError("La fiche de l'anime cite une source absente de la recherche. Réessayez.")
        return {**brief.model_dump(), "sources": evidence["sources"]}

    @classmethod
    def research_brief(cls, project_id, revision, angle, checkpoint):
        evidence = OpenRouterService.research(
            FacecamNarrationService.prompt("original_research") + "\n" + json.dumps(angle, ensure_ascii=False),
            preset_key=FacecamService.project(project_id).resolved_llm_preset_key(),
        )
        checkpoint(research=evidence)
        if not evidence.get("text", "").strip() or not evidence.get("sources"):
            raise ValueError("La recherche sur l'anime n'a pas fourni de sources. Le script actuel est conservé.")
        FacecamService.update(project_id, {"job": {"status": "processing", "step": "Construction de la présentation originale", "progress": 0.3}}, revision)
        response = FacecamService.llm(project_id,
            FacecamNarrationService.prompt("original_brief") + "\n" + json.dumps({
                **angle, "evidence": evidence,
            }, ensure_ascii=False), response_schema=OriginalBrief.model_json_schema(), schema_name="facecam_original_brief")
        checkpoint(brief_response=response)
        return cls.validate_brief(response, evidence)

    @classmethod
    def ensure_brief(cls, project_id, revision):
        """Share one researched anime brief across choices and the first script."""
        state = cls.current(project_id, revision)
        if state.original_brief:
            return state.original_brief
        angle = {"anime": FacecamService.project(project_id).anime_name,
                 "hook": state.selected_hook, "opinion": state.selected_opinion,
                 "spoiler_policy": "premise_and_setup_only"}
        archive = {"revision": revision, "angle": angle}
        path = FacecamService.directory(project_id) / "generations" / f"original-brief-{uuid.uuid4().hex}.json"

        def checkpoint(**values):
            archive.update(values)
            write_text_atomic(path, json.dumps(archive, ensure_ascii=False, indent=2))
            cls.current(project_id, revision)

        brief = cls.research_brief(project_id, revision, angle, checkpoint)
        FacecamService.update(project_id, {"original_brief": brief}, revision)
        return brief

    @classmethod
    def generate(cls, project_id: str, revision: int, *, hook: str | None = None, opinion: str | None = None):
        state = cls.current(project_id, revision)
        if state.generation_mode != "original" and not any(b.kind == "body" and b.text.strip() for b in state.blocks):
            raise ValueError("Générez d'abord un script avant de repartir de zéro.")
        # Read the current editor blocks, including manual edits. Selected choices
        # can refer to a different, not-yet-generated version of the angle.
        hook = hook if hook is not None else next((b.text for b in state.blocks if b.kind == "hook"),
                                                 state.selected_hook if not state.blocks else "")
        opinion = opinion if opinion is not None else next((b.text for b in state.blocks if b.kind == "opinion"),
                                                          state.selected_opinion if not state.blocks else "")
        if not hook.strip():
            raise ValueError("Complétez l'accroche avant de repartir de zéro.")
        blocks = FacecamService.skeleton(project_id, include_opinion=bool(opinion.strip()), generation_mode="original")
        anime = FacecamService.project(project_id).anime_name
        # This is the entire research input. No source transcript, selected arc,
        # previous body, source duration or scene description reaches the writer.
        angle = {"anime": anime, "hook": hook, "opinion": opinion,
                 "spoiler_policy": "premise_and_setup_only"}
        archive_path = FacecamService.directory(project_id) / "generations" / f"original-{uuid.uuid4().hex}.json"
        archive = {"revision": revision, "generation_mode": "original", "angle": angle,
                   "previous_state": state.model_dump(exclude={"full_story"})}

        def checkpoint(**values):
            archive.update(values)
            write_text_atomic(archive_path, json.dumps(archive, ensure_ascii=False, indent=2))
            cls.current(project_id, revision)

        if state.generation_mode == "original" and not state.blocks:
            # Cache even when the hook was typed directly; a failed first draft
            # can be retried without paying for the research again.
            if state.original_brief:
                brief = state.original_brief
            else:
                brief = cls.research_brief(project_id, revision, angle, checkpoint)
                FacecamService.update(project_id, {"original_brief": brief}, revision)
            checkpoint(reused_brief=brief)
        else:
            brief = cls.research_brief(project_id, revision, angle, checkpoint)
        fixed_words = len((hook + " " + opinion + " " + blocks[-1].text).split())
        rate, cover = FacecamStoryService.WORDS_PER_SECOND, FacecamStoryService.COVER_SECONDS
        payload = {**angle, "anime_brief": brief, "body_id": "body-original",
            "body_budget": {"min_words": max(0, math.ceil((cls.TARGET_SECONDS[0] - cover) * rate) - fixed_words),
                            "max_words": max(0, math.floor((cls.TARGET_SECONDS[1] - cover) * rate) - fixed_words),
                            "target_seconds": list(cls.TARGET_SECONDS), "advisory": True}}
        voice = (FacecamNarrationService.prompt("original_voice") + "\n"
                 + FacecamNarrationService.prompt("narrative_tense"))
        result = FacecamService.llm(project_id, voice + "\n" + FacecamNarrationService.prompt("original_script")
                                    + "\n" + json.dumps(payload, ensure_ascii=False))
        checkpoint(draft_response=result)
        FacecamNarrationService.validate_body(result, blocks)
        FacecamService.update(project_id, {"job": {"status": "processing", "step": "Réécriture orale et condensation", "progress": 0.6}}, revision)
        edited = FacecamService.llm(project_id, voice + "\n" + FacecamNarrationService.prompt("original_edit")
                                    + "\n" + json.dumps({**payload, "draft_blocks": result["blocks"]}, ensure_ascii=False))
        checkpoint(response=edited)
        by_id = FacecamNarrationService.validate_body(edited, blocks)
        for block in blocks:
            if block.kind == "hook":
                block.text = hook
            elif block.kind == "opinion":
                block.text = opinion
            elif block.kind == "body":
                block.text = by_id[block.id]
        # Mode, text, illustrations and invalidation change together only after
        # every response has validated. Failure leaves the current draft usable.
        return FacecamService.save_draft(project_id, revision, blocks, hook, opinion,
                                         generation_mode="original", original_brief=brief)
