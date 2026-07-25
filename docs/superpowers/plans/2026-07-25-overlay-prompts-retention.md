# Overlay Prompts Retention Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two default overlay prompts with script-anchored, psychology-driven versions that generate retention-optimized title hooks (spec: `docs/superpowers/specs/2026-07-24-overlay-prompts-retention-design.md`).

**Architecture:** Prompt-only change — two Markdown template files consumed by `ScriptPhasePromptService.build_overlay_prompt` via `PromptResolver`. No Python code changes. Each file keeps the exact placeholder tokens and JSON output contract; content is rebuilt around beat extraction, an adequation contract, 8 named psychological slots, hard rules, and a self-check.

**Tech Stack:** Markdown prompt templates; pytest (via pixi `dev` env) for regression checks.

## Global Constraints

- Placeholders must appear verbatim: `[OEUVRE]`, `[SCRIPT_SUMMARY]` in both files; `[TARGET]` in `overlay_multi.md` only.
- Output JSON field names unchanged: `title_hooks`, `category` (code appends scope overrides referencing them).
- Files end with the input block: `ANIME: [OEUVRE]` newline `SCRIPT: [SCRIPT_SUMMARY]`.
- Hook constraints kept: 45-char strict max; never name the anime; emoji rules (1 max on some hooks, ≥3 of 8 without emoji).
- New rule: no character names (roles/relations instead).
- Category format kept: exactly 2 genres separated by `" • "`.
- `overlay_fr.md` written in French with French typography rule (space before `? ! : ;`); `overlay_multi.md` written in English, output natively in `[TARGET]`, with EN/ES/DE style notes.
- Tests: run via `pixi run -e dev pytest` from `backend/`; never overlap two pytest runs.

---

### Task 1: Rewrite `overlay_fr.md`

**Files:**
- Modify: `backend/prompts/default/overlay_fr.md` (full replacement)
- Test: `backend/tests/test_script_automation_overlay_templates.py` (existing, must stay green)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the French overlay prompt template resolved by `PromptResolver.resolve(prompt_group=OVERLAY, language_variant=FR)`; contract per Global Constraints.

- [ ] **Step 1: Replace the file content**

Write exactly this content to `backend/prompts/default/overlay_fr.md`:

```markdown
Tu es spécialiste de la rétention TikTok pour des comptes de storytelling anime.

CONTEXTE :

- Le titre overlay reste affiché à l'écran pendant TOUTE la vidéo.
- Le script ci-dessous est narré en voix off pendant la vidéo.
- Le spectateur décide en moins de 2 secondes s'il reste ou s'il swipe.
- Le rôle du titre : ouvrir une boucle psychologique que seule la vidéo peut refermer.

MISSION : génère 8 hooks de titre distincts et 1 catégorie pour cette vidéo.

MÉTHODE (dans l'ordre, avant d'écrire le moindre hook) :

1. Lis le script en entier et repère : les premières phrases, le pivot, le climax/twist, le cœur émotionnel.
2. Chaque hook doit être construit UNIQUEMENT à partir de ces éléments réels du script.
3. CONTRAT D'ADÉQUATION (obligatoire pour chaque hook) :
   a. Le hook résonne avec les premières phrases du script : dès la seconde 1, le spectateur doit sentir que la vidéo commence à répondre au titre.
   b. Le hook n'est totalement résolu que par un moment situé PLUS LOIN dans le script : la boucle reste ouverte le plus longtemps possible.

LES 8 HOOKS (répartition imposée dans `title_hooks`, dans cet ordre) :

- Hooks 1-2 — CURIOSITY GAP : évoque un événement précis du script sans révéler son issue. Mécanique : "Il n'aurait jamais dû ouvrir cette lettre"
- Hooks 3-4 — CHOC / TRANSGRESSION : annonce frontalement le fait le plus choquant ; la vidéo doit livrer le comment et le pourquoi. Mécanique : "Il a vendu sa propre sœur pour survivre"
- Hooks 5-6 — ENJEU ÉMOTIONNEL : le dilemme ou la relation au cœur du script, promesse d'une charge émotionnelle. Mécanique : "Elle l'aimait, il ne l'a jamais su"
- Hook 7 — HYBRIDE : la moitié du fait choquant, l'issue cachée. Mécanique : "Ce qu'il a fait à son frère est impardonnable"
- Hook 8 — DÉTAIL CONCRET INTRIGANT : un détail étrange ou marquant repris presque mot pour mot du script. Mécanique : "Elle a des dents de lapin"

RÈGLES DURES (chaque hook) :

- Maximum 45 caractères (STRICT — compte chaque caractère, espaces et emoji inclus ; si un hook dépasse, raccourcis-le avant de l'inclure)
- Ne JAMAIS citer le nom de l'anime
- Ne JAMAIS citer le nom d'un personnage — utilise les rôles et relations : "son propre frère", "la fille qu'il aimait"
- Chaque hook contient AU MOINS un élément concret du script (action, objet, relation, chiffre, lieu)
- Typographie française OBLIGATOIRE : toujours un espace AVANT les ? ! : ; (ex : "MOT !" et non "MOT!")
- Emoji : tu PEUX ajouter 1 emoji au début ou à la fin de CERTAINS hooks (pas tous !) ; jamais 2 ou plus ; au moins 3 hooks sur 8 SANS emoji ; emojis simples uniquement : 🔥 💀 😭 🤯 😱 💔 ⚡
- Registre : français oral TikTok, tutoiement, présent dramatique, phrases courtes qui claquent ; pas de tournures écrites ("c'est alors que…")

INTERDITS (échec automatique) :

- "CET ANIME EST INCROYABLE", "TU NE VAS PAS Y CROIRE", "LE MEILLEUR ANIME", "C'EST UNE DINGUERIE" et toute variante vide de contenu
- Tout hook qui pourrait s'appliquer tel quel à n'importe quelle autre vidéo anime

EXEMPLE (mini-script → hooks) :

Script (résumé) : "Un lycéen découvre que sa petite sœur disparue vit en secret dans les murs de leur maison depuis 3 ans…"

- ✗ MAUVAIS : "CET ANIME VA TE CHOQUER" — générique, applicable à toute vidéo, aucune boucle.
- ✓ BON (curiosity gap) : "Sa sœur n'a jamais quitté la maison" (35 car)
- ✓ BON (choc) : "Elle vit dans les murs depuis 3 ans" (35 car)

AUTO-VÉRIFICATION avant de répondre, pour chaque hook :

1. ≤ 45 caractères (comptés) ?
2. Ancré sur un élément réel du script ?
3. Test générique : le hook serait-il crédible sur une autre vidéo ? Si oui, réécris-le.
4. Aucun nom d'anime ni de personnage ?
5. La boucle s'ouvre dès les premières secondes et ne se referme que dans la vidéo ?

RÈGLES CATÉGORIE :

- Retourne UNE SEULE catégorie dans `category`
- Exactement 2 genres séparés par " • "
- Choisis les 2 genres qui amplifient l'émotion dominante du script et des hooks (ex : hook de trahison → "Drame • Psychologique" plutôt que "Action • Aventure")

FORMAT :

- Réponds uniquement avec le JSON demandé
- Structure attendue :
  {
  "title_hooks": ["hook 1", "hook 2", "..."],
  "category": "Genre • Genre"
  }

ANIME: [OEUVRE]
SCRIPT: [SCRIPT_SUMMARY]
```

- [ ] **Step 2: Verify the contract tokens survived**

Run:
```bash
grep -c "\[OEUVRE\]" backend/prompts/default/overlay_fr.md
grep -c "\[SCRIPT_SUMMARY\]" backend/prompts/default/overlay_fr.md
grep -c "title_hooks" backend/prompts/default/overlay_fr.md
grep -c '"category"' backend/prompts/default/overlay_fr.md
grep -c "\[TARGET\]" backend/prompts/default/overlay_fr.md || true
```
Expected: first four commands print ≥1; the `[TARGET]` grep prints 0 (FR file has no TARGET placeholder — the `|| true` keeps the shell happy on the zero-match exit code).

- [ ] **Step 3: Run the existing overlay template tests**

Run: `cd backend && pixi run -e dev pytest tests/test_script_automation_overlay_templates.py -v`
Expected: all tests PASS (they assert code-appended scope strings and payload normalization, not template prose).

- [ ] **Step 4: Commit**

```bash
git add backend/prompts/default/overlay_fr.md
git commit -m "feat(prompts): script-anchored retention rework of overlay_fr" -- backend/prompts/default/overlay_fr.md
```
(Pathspec-limited commit: the working tree carries unrelated staged changes that must not be swept in.)

---

### Task 2: Rewrite `overlay_multi.md`

**Files:**
- Modify: `backend/prompts/default/overlay_multi.md` (full replacement)
- Test: `backend/tests/test_script_automation_overlay_templates.py` (existing, must stay green)

**Interfaces:**
- Consumes: nothing from Task 1 (independent file).
- Produces: the multilingual overlay prompt template resolved by `PromptResolver.resolve(prompt_group=OVERLAY, language_variant=MULTI)`; contract per Global Constraints, including `[TARGET]`.

- [ ] **Step 1: Replace the file content**

Write exactly this content to `backend/prompts/default/overlay_multi.md`:

```markdown
You are a TikTok retention specialist for anime storytelling accounts.

CONTEXT:

- The overlay title stays pinned on screen for the ENTIRE video.
- The script below is narrated as a voice-over during the video.
- Viewers decide in under 2 seconds whether to keep watching or swipe away.
- The title's job: open a psychological loop that only the video can close.

MISSION: generate 8 distinct title hooks and 1 category for this video.

LANGUAGE: write every hook natively in [TARGET] — never translate from English. Use the words, rhythm and slang a native [TARGET] creator would type.

METHOD (in order, before writing any hook):

1. Read the full script and identify: the opening lines, the pivot, the climax/twist, the emotional core.
2. Every hook must be built ONLY from these real script elements.
3. ADEQUATION CONTRACT (mandatory for every hook):
   a. The hook resonates with the script's opening lines: from second 1, the viewer must feel the video is starting to answer the title.
   b. The hook is only fully resolved by a moment LATER in the script: keep the loop open as long as possible.

THE 8 HOOKS (fixed distribution in `title_hooks`, in this order):

- Hooks 1-2 — CURIOSITY GAP: reference a specific script event without revealing its outcome. Mechanic: "He should never have opened that letter"
- Hooks 3-4 — SHOCK / TRANSGRESSION: state the most shocking fact upfront; the video owes the how and the why. Mechanic: "He sold his own sister to survive"
- Hooks 5-6 — EMOTIONAL STAKES: the dilemma or relationship at the script's core, promising the emotional payoff. Mechanic: "She loved him, he never knew"
- Hook 7 — HYBRID: half the shocking fact, outcome hidden. Mechanic: "What he did to his brother is unforgivable"
- Hook 8 — CONCRETE ODD DETAIL: a strange or striking detail lifted almost verbatim from the script. Mechanic: "She has rabbit teeth"

HARD RULES (every hook):

- Maximum 45 characters (STRICT — count every character including spaces and emoji; if a hook exceeds the limit, shorten it before including it)
- NEVER mention the anime name
- NEVER use character names — use roles and relations instead: "his own brother", "the girl he loved"
- Every hook contains AT LEAST one concrete script element (action, object, relation, number, place)
- Emoji: you MAY add 1 emoji at the start or end of SOME hooks (not all!); never 2 or more; at least 3 of the 8 hooks with NO emoji; simple emoji only: 🔥 💀 😭 🤯 😱 💔 ⚡

LANGUAGE STYLE for [TARGET]:

- English: punchy spoken register, contractions welcome, no space before punctuation.
- Español: natural exclamation style (opening ¡ optional, as in social captions), Latin-neutral wording.
- Deutsch: blunt short sentences beat long compounds; German words run long — re-check the 45-character limit extra carefully.
- Any other language: apply that language's native norms (typography, register, idioms).

BANNED (automatic failure):

- "THIS ANIME IS INSANE", "YOU WON'T BELIEVE THIS", "BEST ANIME EVER" and their equivalents in [TARGET]
- Any hook that could be pasted unchanged onto any other anime video

EXAMPLE (mini-script → hooks; shown in English, but YOUR hooks must be in [TARGET]):

Script (summary): "A high schooler discovers his missing little sister has been secretly living inside the walls of their house for 3 years…"

- ✗ BAD: "THIS ANIME WILL SHOCK YOU" — generic, fits any video, no loop.
- ✓ GOOD (curiosity gap): "His sister never left the house" (31 chars)
- ✓ GOOD (shock): "She lived in the walls for 3 years" (34 chars)

SELF-CHECK before answering, for every hook:

1. ≤ 45 characters (counted)?
2. Anchored to a real script element?
3. Generic test: would this hook be believable on another video? If yes, rewrite it.
4. No anime or character names?
5. Does the loop open in the first seconds and close only inside the video?

CATEGORY RULES:

- Return exactly 1 category in `category`
- Exactly 2 genres separated by " • "
- Pick the 2 genres that amplify the dominant emotion of the script and hooks (e.g. a betrayal hook → "Drama • Psychological" over "Action • Adventure"), written in [TARGET]

FORMAT:

- Return JSON only
- Expected shape:
  {
  "title_hooks": ["hook 1", "hook 2", "..."],
  "category": "Genre • Genre"
  }

ANIME: [OEUVRE]
SCRIPT: [SCRIPT_SUMMARY]
```

- [ ] **Step 2: Verify the contract tokens survived**

Run:
```bash
grep -c "\[OEUVRE\]" backend/prompts/default/overlay_multi.md
grep -c "\[SCRIPT_SUMMARY\]" backend/prompts/default/overlay_multi.md
grep -c "\[TARGET\]" backend/prompts/default/overlay_multi.md
grep -c "title_hooks" backend/prompts/default/overlay_multi.md
grep -c '"category"' backend/prompts/default/overlay_multi.md
```
Expected: every command prints ≥1 (`[TARGET]` should print ≥4: LANGUAGE line, style block header, banned line, category rule).

- [ ] **Step 3: Run the existing overlay template tests**

Run: `cd backend && pixi run -e dev pytest tests/test_script_automation_overlay_templates.py -v`
Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/prompts/default/overlay_multi.md
git commit -m "feat(prompts): script-anchored retention rework of overlay_multi" -- backend/prompts/default/overlay_multi.md
```
(Pathspec-limited commit, same reason as Task 1.)

---

### Task 3: End-to-end prompt render sanity check

**Files:**
- No file changes — verification only, using `backend/app/services/script_phase_prompt_service.py` as-is.

**Interfaces:**
- Consumes: the two templates from Tasks 1 and 2 via `ScriptPhasePromptService.build_overlay_prompt(anime_name=..., script_summary=..., target_language=...)`.
- Produces: confidence that placeholder substitution and the code-appended scope overrides still compose correctly.

- [ ] **Step 1: Render both prompts with a sample script and inspect**

Run from `backend/`:
```bash
pixi run -e dev python -c "
from app.services.script_phase_prompt_service import ScriptPhasePromptService as S
for lang in ('fr', 'en'):
    p = S.build_overlay_prompt(
        anime_name='Test Anime',
        script_summary='Un lyceen decouvre un secret dans les murs de sa maison.',
        target_language=lang,
    )
    assert '[OEUVRE]' not in p and '[SCRIPT_SUMMARY]' not in p and '[TARGET]' not in p, lang
    assert 'ANIME: Test Anime' in p, lang
    print(lang, 'OK,', len(p), 'chars')
"
```
Expected: prints `fr OK, <n> chars` and `en OK, <n> chars` with no assertion error (all placeholders substituted in both variants).

- [ ] **Step 2: Render the title-only scope variant**

Run from `backend/`:
```bash
pixi run -e dev python -c "
from app.services.script_phase_prompt_service import ScriptPhasePromptService as S
p = S.build_overlay_prompt(
    anime_name='Test Anime',
    script_summary='Un secret dans les murs.',
    target_language='fr',
    include_category=False,
)
assert 'Generate only \`title_hooks\`' in p
print('scope override OK')
"
```
Expected: prints `scope override OK` (the code-appended override composes with the new template).

- [ ] **Step 3: Owner validation note (manual, post-merge)**

No command. Regenerate overlays on 2–3 existing projects (FR + one multi language) from the UI and check: hooks anchored to the script, 8 slots covered, ≤45 chars, no anime/character names, category echoes the hook. This is the owner's acceptance pass per the spec's Testing section.
