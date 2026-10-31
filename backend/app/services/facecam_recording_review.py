"""Editable, versioned recording review. No media analysis or model calls here."""
from __future__ import annotations

import difflib
import re
import uuid

from ..models.facecam import FacecamPlacement, RecordingReview, RecordingWordChange, ScriptBlock
from .facecam_service import FacecamService, StaleFacecamRevision
from .facecam_visual_index import fingerprint


def suggestions(blocks: list[ScriptBlock], transcript: str) -> list[RecordingWordChange]:
    """Compare lexical words while preserving actual spelling and punctuation in edits.

    Offsets count Unicode code points within a block, like the existing fact editor.
    A change crossing a block boundary is split; raw-dialogue markers never move.
    """
    pattern = r"[^\W_]+(?:['’][^\W_]+)*"
    expected = [(b, m) for b in blocks if b.kind != "raw" for m in re.finditer(pattern, b.text)]
    actual = list(re.finditer(pattern, transcript))
    normalize = lambda value: value.casefold().replace("’", "'")
    matcher = difflib.SequenceMatcher(None, [normalize(m.group()) for _, m in expected],
                                    [normalize(m.group()) for m in actual], autojunk=False)
    result = []

    def add(block, start, end, spoken, kind):
        replacement = spoken
        if kind == "insert":
            replacement = ((" " if start and not block.text[start - 1].isspace() else "") + spoken
                           + (" " if start < len(block.text) and block.text[start].isalnum() else ""))
        elif kind == "delete":
            if end < len(block.text) and block.text[end].isspace():
                while end < len(block.text) and block.text[end].isspace():
                    end += 1
            else:
                while start and block.text[start - 1].isspace():
                    start -= 1
        result.append(RecordingWordChange(id=f"change-{len(result)}", block_id=block.id, kind=kind,
            start=start, end=end, expected=block.text[start:end], spoken=spoken, replacement=replacement))

    for kind, a, b, c, d in matcher.get_opcodes():
        if kind == "equal":
            continue
        if a == b:
            if not expected:
                continue
            block, token = expected[a] if a < len(expected) else expected[-1]
            position = token.start() if a < len(expected) else token.end()
            add(block, position, position, transcript[actual[c].start():actual[d - 1].end()], "insert")
            continue
        cursor = a
        while cursor < b:
            block = expected[cursor][0]
            end = cursor + 1
            while end < b and expected[end][0].id == block.id:
                end += 1
            lo = c + round((cursor - a) * (d - c) / (b - a))
            hi = c + round((end - a) * (d - c) / (b - a))
            spoken = transcript[actual[lo].start():actual[hi - 1].end()] if hi > lo else ""
            add(block, expected[cursor][1].start(), expected[end - 1][1].end(), spoken,
                "replace" if spoken else "delete")
            cursor = end
    return result


def remap_changes(before, after, changes):
    old, new = {b.id: b.text for b in before}, {b.id: b.text for b in after}
    operations = {key: difflib.SequenceMatcher(None, value, new[key], autojunk=False).get_opcodes()
                  for key, value in old.items()}
    result = []
    for change in changes:
        # Only unchanged spans keep a suggestion. Manual edits resolve touched proposals.
        ops = operations[change.block_id]
        if change.start == change.end and any(tag != "equal" and a <= change.start <= b for tag, a, b, _, _ in ops):
            continue
        for tag, a, b, c, _ in ops:
            if tag != "equal":
                continue
            if a <= change.start <= change.end <= b:
                result.append(change.model_copy(update={"start": c + change.start - a, "end": c + change.end - a}))
                break
    return result


class FacecamRecordingReview:
    @staticmethod
    def require_recording(state):
        if not state.recording or not state.recording.get("transcript", "").strip():
            raise ValueError("Attendez la transcription définitive de l'enregistrement.")
        if state.recording_edit and not state.recording_edit.applied:
            raise ValueError("Validez d'abord les coupes de l'enregistrement.")

    @classmethod
    def ensure(cls, project_id):
        with FacecamService.lock(project_id):
            state = FacecamService.load(project_id)
            cls.require_recording(state)
            review = state.recording_review
            if review and review.revision == state.revision and review.fingerprint == state.recording["fingerprint"]:
                return state
            review = RecordingReview(id=uuid.uuid4().hex, revision=state.revision,
                fingerprint=state.recording["fingerprint"], blocks=state.blocks,
                changes=suggestions(state.blocks, state.recording["transcript"]))
            return FacecamService.update(project_id, {"recording_review": review}, state.revision)

    @staticmethod
    def check(state, revision, review_id, version):
        review = state.recording_review
        if (state.revision != revision or not review or review.id != review_id or review.version != version
                or not state.recording or review.fingerprint != state.recording["fingerprint"]):
            raise StaleFacecamRevision("La relecture ou l'enregistrement a changé. Rechargez la version actuelle.")
        FacecamRecordingReview.require_recording(state)
        return review

    @staticmethod
    def validate_blocks(before, blocks):
        if [(b.id, b.kind, b.scene_indices) for b in before] != [(b.id, b.kind, b.scene_indices) for b in blocks]:
            raise ValueError("Conservez les sections et les repères des extraits audio originaux.")
        if any(b.kind == "raw" and b.text for b in blocks):
            raise ValueError("Les extraits audio originaux ne sont pas modifiables dans la relecture.")

    @classmethod
    def save(cls, project_id, revision, review_id, version, blocks, resolved_ids):
        with FacecamService.lock(project_id):
            state = FacecamService.load(project_id)
            review = cls.check(state, revision, review_id, version)
            cls.validate_blocks(review.blocks, blocks)
            if not set(resolved_ids) <= {c.id for c in review.changes}:
                raise ValueError("Une suggestion de relecture n'est plus disponible.")
            if blocks == review.blocks and not resolved_ids:
                return state
            changes = remap_changes(review.blocks, blocks, [c for c in review.changes if c.id not in resolved_ids])
            updated = review.model_copy(update={"version": version + 1, "blocks": blocks, "changes": changes,
                                                "validated_revision": None})
            result = FacecamService.update(project_id, {"recording_review": updated,
                "recording": {**state.recording, "reviewed_revision": None}, "placement": None,
                "prepared": None, "processed_signature": None}, revision)
            FacecamService.invalidate_output(project_id)
            return result

    @classmethod
    def validate(cls, project_id, revision, review_id, version):
        with FacecamService.lock(project_id):
            state = FacecamService.load(project_id)
            review = state.recording_review
            # An identical double click or HTTP retry returns the same committed review.
            if (review and review.id == review_id and review.version == version
                    and review.validated_revision == state.revision and state.placement
                    and state.placement.revision == state.revision and state.recording
                    and state.recording.get("reviewed_revision") == state.revision
                    and state.placement.fingerprint == state.recording["fingerprint"]):
                return state
            review = cls.check(state, revision, review_id, version)
            cls.validate_blocks(state.blocks, review.blocks)
            if not any(b.text.strip() for b in review.blocks if b.kind in {"hook", "body"}):
                raise ValueError("Conservez une accroche ou un récit dans le texte enregistré.")
            from .facecam_findings import remap_findings
            from .facecam_media_service import compare_words
            changed = review.blocks != state.blocks
            revision += int(changed)
            spoken = "\n\n".join(b.text for b in review.blocks if b.kind != "raw")
            reusable = (not changed and bool(state.assignment) and bool(state.montage_map)
                        and " ".join(row["text"] for row in state.assignment).split() == spoken.split())
            key = fingerprint([1, revision, review.fingerprint, [b.model_dump() for b in review.blocks],
                               state.generation_mode, state.selected_story_id])
            result = FacecamService.update(project_id, {
                "blocks": review.blocks, "revision": revision, "approved_revision": revision,
                "selected_hook": next((b.text for b in review.blocks if b.kind == "hook"), ""),
                "selected_opinion": next((b.text for b in review.blocks if b.kind == "opinion"), ""),
                "assignment": state.assignment if reusable else [], "montage_map": state.montage_map if reusable else [],
                "recording_review": review.model_copy(update={"revision": revision, "validated_revision": revision}),
                "recording": {**state.recording, "reviewed_revision": revision,
                              "differences": compare_words(spoken, state.recording["transcript"])},
                "placement": FacecamPlacement(key=key, revision=revision, fingerprint=review.fingerprint,
                                              assignment_complete=reusable),
                "findings": remap_findings(state.blocks, review.blocks, state.findings), "findings_revision": revision,
                "prepared": state.prepared if reusable else None, "processed_signature": None,
            }, state.revision)
            FacecamService.invalidate_output(project_id)
            return result
