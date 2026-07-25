# Overlay Prompts Retention Rework — Design

**Date:** 2026-07-24
**Scope:** `backend/prompts/default/overlay_fr.md` and `backend/prompts/default/overlay_multi.md` — prompt-only rework, no code changes.

## Problem

The current overlay prompts produce generic hype titles disconnected from the video's script (real output: `"CET ANIME EST INCROYABLE !"`). The prompts never instruct the model to use the script content. Since the overlay title is pinned on screen for the **entire video**, it should act as a sustained psychological open loop: resonate with the script's opening lines, and only be fully resolved by a later beat — maximizing watch-through.

## Requirements (validated with owner)

- The owner **manually picks one** of the 8 hooks → optimize for diversity of psychological angles.
- Title displayed **whole video** → sustained open-loop design.
- Scripts are **anime recap/story** narrations.
- Psychological levers to use: **curiosity gap**, **emotional stakes**, **shock/transgression**. (Challenge/superiority excluded.)
- Spoiler policy: **anything that retains** — per-hook freedom, including stating the shocking fact when the how/why carries the video.
- Keep: **45-char strict max**, **never name the anime**, **current emoji rules** (1 max on some hooks, ≥3 of 8 without).
- Casing irrelevant: the overlay generator force-uppercases downstream.
- `overlay_multi`: targets **EN/ES/DE** with per-language cultural adaptation, native writing (not translation).
- Category: keep `"Genre • Genre"` format but pick genres that **amplify the hook/script's dominant emotion**.

## Contract preserved (no code changes)

- Placeholders: `[OEUVRE]`, `[SCRIPT_SUMMARY]` (full script text, scenes joined), `[TARGET]` (multi only).
- Output JSON shape: `{"title_hooks": ["..."], "category": "Genre • Genre"}` — field names unchanged (code appends scope overrides referencing `title_hooks`/`category`).
- Trailing input block `ANIME: [OEUVRE]` / `SCRIPT: [SCRIPT_SUMMARY]` kept.
- Consumers: `ScriptPhasePromptService.build_overlay_prompt`, `ScriptAutomationService.generate_video_overlay` (light LLM tier, thinking enabled). Tests only assert code-appended strings, not template content.

## Design (approach A: script-anchored angle slots)

Shared architecture for both files:

1. **Role + stakes framing.** Retention specialist for anime storytelling accounts. Explicit context: title pinned whole video; viewer decides in <2 s; the title's job is to open a loop only the video can close.
2. **Beat extraction step (internal).** Before writing, identify from the script: opening line(s), pivot, climax/twist, emotional core. Hooks may only be built from these real elements.
3. **Adequation contract** (testable rule): (a) the hook must resonate with the script's opening lines so the viewer immediately feels the video is answering the title; (b) the hook is only fully resolved by a later beat, keeping the loop open as long as possible.
4. **8 named slots** for guaranteed diversity:
   - 2× curiosity gap — tease a specific event, withhold the outcome
   - 2× shock/transgression — state the shocking fact; the video owes the how/why
   - 2× emotional stakes — dilemma/relationship, promised emotional payoff
   - 1× hybrid — half the shocking fact, outcome hidden
   - 1× concrete odd detail — near-verbatim intriguing detail from the script (e.g. "ELLE A DES DENTS DE LAPIN")
5. **Concreteness enforcement.** Every hook must contain ≥1 specific script element (action, object, relation, number, place). Banned list of generic hooks: "CET ANIME EST INCROYABLE", "TU NE VAS PAS Y CROIRE", "LE MEILLEUR ANIME", and any hook that fits any anime video unchanged.
6. **Name rules.** No anime name (kept) + **no character names** — use roles/relations ("son propre frère", "la fille qu'il aimait").
7. **Self-check before output.** Per hook: ≤45 chars counted; anchored to a real script element; passes the generic test; no names; loop opens at second 1 and closes only inside the video.
8. **Category.** 2 genres, `" • "` separator, chosen to amplify the dominant emotion of script + hooks.
9. **Few-shot contrast pair.** Mini script snippet → 1 bad hook (generic, annotated why it fails) + 2 good hooks (annotated with slot/technique).

### overlay_fr.md specifics

- Written natively in French; French typography kept (space before `? ! : ;`).
- French TikTok register: tutoiement, oral punchy phrasing, dramatic present tense; avoid written-French turns.
- Examples and few-shot in French.

### overlay_multi.md specifics

- Written in English; output in `[TARGET]`; explicit "write natively in [TARGET], never translate from English".
- Per-language style block:
  - **English:** punchy slang register, contractions fine, no space before punctuation.
  - **Español:** natural exclamation style (opening `¡` optional in social captions), Latin-neutral wording by default.
  - **Deutsch:** blunt short sentences over long compounds; German word length inflates char count — check the 45-char limit extra carefully.
  - Fallback: apply native norms of any other `[TARGET]`.
- Few-shot in English with note that hooks must be produced in `[TARGET]`.

## Error handling

Unchanged: `_normalize_overlay_payload` already truncates over-length titles and tolerates malformed lists; the prompt's self-check reduces (not replaces) reliance on it.

## Testing

- Existing tests unaffected (they assert code-appended scope strings only).
- Manual validation: regenerate overlays on 2–3 existing projects (FR + one multi language) and check hooks are script-anchored, diverse across slots, ≤45 chars, and free of anime/character names.
