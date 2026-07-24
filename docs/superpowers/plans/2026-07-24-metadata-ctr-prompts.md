# Metadata Prompts CTR Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `backend/prompts/default/metadata_fr.md` and `metadata_multi.md` with data-calibrated, 8-slot CTR-optimized prompts, verified by a live light-tier smoke test.

**Architecture:** Prompt-file-only change. The two markdown templates are resolved by `PromptResolver` and substituted by `ScriptPhasePromptService.build_metadata_prompt` (`[OEUVRE]`, `[SCRIPT]`, and `[TARGET]` for multi only). Output is parsed by `MetadataTitleCandidatesPayload` (exactly 8 titles; >62 chars silently truncated). A small pytest guards durable invariants; a scratchpad smoke script validates real LLM output.

**Tech Stack:** Markdown prompt templates, pytest, project OpenRouter light tier (`LLMService.generate_json(..., tier="light")`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-24-metadata-prompts-ctr-rework-design.md` (committed as `ced15f8` on `feat/fast-matching-r2`).
- JSON output structure unchanged: `title_candidates` (exactly 8), `facebook{description,tags}`, `instagram{hashtags}`, `youtube{description,tags}`. No Python/schema changes.
- `metadata_fr.md` must contain `[OEUVRE]` and `[SCRIPT]`, and must NOT contain `[TARGET]` (the fr path never substitutes it).
- `metadata_multi.md` must contain `[OEUVRE]`, `[SCRIPT]`, and `[TARGET]`.
- Titles: target 36–48 chars, hard max 62; mandatory 8 named slots; banned: generic questions, `!`, digits, isekai jargon, «vous»; gatekeeping (never name work/characters) in all visible fields; `[OEUVRE]` required in YouTube+Facebook tags.
- Fixed Facebook CTA («Abonne toi pour plus de présentations d'anime») is REMOVED from both files.
- Work happens on branch `feat/metadata-ctr-prompts` created from `main`, with the spec commit cherry-picked.
- Python for scripts/tests: `.pixi/envs/default/bin/python` from repo root (pytest: `.pixi/envs/default/bin/python -m pytest`).
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Branch + invariant test + rewrite `metadata_fr.md`

**Files:**
- Create: `backend/tests/test_metadata_prompt_templates.py`
- Modify: `backend/prompts/default/metadata_fr.md` (full replacement)

**Interfaces:**
- Consumes: existing `PromptResolver` chain (no code change).
- Produces: the fr template Task 3 smoke-tests; the pytest file Task 2 extends implicitly (it already covers both files).

- [ ] **Step 1: Create branch from main and cherry-pick the spec commit**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git checkout -b feat/metadata-ctr-prompts main
git cherry-pick ced15f8
```

Expected: branch created, spec commit applied cleanly (it only adds a new file under `docs/superpowers/specs/`).

- [ ] **Step 2: Write the invariant test (covers BOTH prompt files)**

Create `backend/tests/test_metadata_prompt_templates.py`:

```python
"""Durable invariants for the default metadata prompt templates.

These guard the plumbing contract (placeholders, output schema keys),
not the prose: ScriptPhasePromptService.build_metadata_prompt substitutes
[OEUVRE]/[SCRIPT] in both files and [TARGET] only on the multi path.
"""
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts" / "default"

FR = (PROMPTS_DIR / "metadata_fr.md").read_text(encoding="utf-8")
MULTI = (PROMPTS_DIR / "metadata_multi.md").read_text(encoding="utf-8")

SCHEMA_KEYS = [
    '"title_candidates"',
    '"facebook"',
    '"instagram"',
    '"youtube"',
    '"description"',
    '"tags"',
    '"hashtags"',
]


def test_fr_placeholders():
    assert "[OEUVRE]" in FR
    assert "[SCRIPT]" in FR
    # The fr path never substitutes [TARGET]; its presence would leak raw.
    assert "[TARGET]" not in FR


def test_multi_placeholders():
    assert "[OEUVRE]" in MULTI
    assert "[SCRIPT]" in MULTI
    assert "[TARGET]" in MULTI


def test_schema_keys_present_in_both():
    for key in SCHEMA_KEYS:
        assert key in FR, f"missing {key} in metadata_fr.md"
        assert key in MULTI, f"missing {key} in metadata_multi.md"


def test_exactly_eight_titles_mentioned():
    assert "8" in FR and "8" in MULTI


def test_dropped_fixed_facebook_cta():
    assert "Abonne toi pour plus de présentations d'anime" not in FR
    assert "Abonne toi pour plus de présentations d'anime" not in MULTI
```

- [ ] **Step 3: Run the test — expect ONE failure (old files still contain the fixed CTA)**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
.pixi/envs/default/bin/python -m pytest backend/tests/test_metadata_prompt_templates.py -v
```

Expected: `test_dropped_fixed_facebook_cta` FAILS (both old files contain the CTA string); the other 4 tests PASS. This proves the test actually reads the real files.

- [ ] **Step 4: Replace `backend/prompts/default/metadata_fr.md` with exactly this content**

```markdown
# Rôle & Objectif

Tu es le stratège titres d'une chaîne YouTube Shorts anime française (50 000+ abonnés). Ton unique métrique de succès : le taux de clic (CTR). Tu génères :

- 8 titres candidats unifiés pour toutes les plateformes
- les descriptions et tags spécifiques à Facebook et YouTube
- les hashtags Instagram

Le titre final sera choisi plus tard dans l'application, puis réinjecté automatiquement dans les métadonnées finales.

# Règle d'Or : Le Gatekeeping (CRITIQUE)

- Ne mentionne JAMAIS [OEUVRE] dans les titres, les descriptions ni les hashtags.
- N'utilise JAMAIS les noms propres des personnages de [OEUVRE].
- Remplace-les par des archétypes ou descriptions contextuelles : «ce lycéen», «cette assassin», «son voisin», «sa femme».
- Preuve chiffrée : les 2 seules vidéos de la chaîne qui nommaient l'œuvre dans le titre sont les 2 pires de toute son histoire (7 900 et 1 500 vues, contre 68 000 de médiane). Le mystère sur l'œuvre EST le moteur de clics et de commentaires.
- Exception unique : les champs `tags` YouTube et Facebook (invisibles au moment du clic) doivent contenir [OEUVRE].

# Ton & Voix

- Français dynamique, tutoiement obligatoire. Le mot «vous» est interdit dans tous les champs.
- Écris comme si tu racontais à un pote le truc le plus dingue que tu as vu aujourd'hui.
- Argot autorisé (avec parcimonie) : «dinguerie», «banger», «masterclass», «pépite».
- Argot interdit : «wesh», «frérot», tout langage «quartier/gamin».
- Phrases courtes. Impactantes. Lisibles en une demi-seconde.

# Bloc 1 : les 8 titres (le cœur de ta mission)

Ces règles sortent de l'analyse statistique des 200 dernières vidéos de la chaîne. Elles ne sont pas des suggestions.

## Règles dures

1. Longueur cible : 36 à 48 caractères, espaces comprises (le sweet spot mesuré : ×1,27). Maximum absolu : 62 — au-delà, l'application tronque le titre en plein mot et le titre est mort.
2. AUCUNE question générique : les titres en «?» font ×0,54 vs la médiane. Seule exception autorisée : la construction «Et si … toi ?» du slot 1.
3. AUCUN point d'exclamation (×0,78). AUCUN chiffre (×0,81).
4. AUCUN jargon isekai/RPG : mage, niveau, guilde, build, boss, stats, level (×0,91 — ce vocabulaire rétrécit l'audience aux gamers).
5. Par défaut, ouvre sur un pronom-personnage : Il / Elle / Son / Sa / Cette (×1,31) — sauf si la formule du slot impose autre chose.
6. Le vague tue : «pas comme les autres», «très particulier», «incroyable» sont interdits. Toujours un détail concret à la place.
7. 0 à 1 emoji maximum, seulement s'il ajoute une émotion (😭 par exemple). Aucun hashtag dans les titres.
8. Chaque titre doit fonctionner tel quel sur YouTube, Facebook, Instagram et TikTok.

## Les 8 slots (dans cet ordre, exactement un titre chacun)

Chaque slot exploite un levier psychologique prouvé sur CETTE chaîne. Les ancres sont de vrais titres à succès de la chaîne : imite leur mécanique, pas leurs mots, et adapte au script.

1. **Menace / hypothèse en 2ᵉ personne** — implique directement le spectateur avec «te», «toi», «ton». Levier n°1 mesuré : ×3,07. Prends la menace ou le pouvoir central du script et retourne-le vers le spectateur.
   Ancre : «Et si quelqu'un pouvait prendre le contrôle de toi ?» (1,9M de vues)
2. **Secret découvert** — une vérité cachée vient d'éclater, sans donner AUCUN détail (lexique secret/caché/découvert : ×1,62). Le manque d'information force le clic.
   Ancre : «Son lourd secret vient d'être découvert» (732k)
3. **Concept absurde assumé** — énonce la prémisse la plus bizarre du script au premier degré, sans la commenter. Plus c'est étrange dit sobrement, plus ça clique.
   Ancres : «Son rencard virtuel était un vieux daron 😭» (1,3M) · «Il fuit sa femme extraterrestre partout» (711k)
4. **Prix sombre en deux temps** — désir énoncé en phrase courte. Point. Conséquence sombre. Le contraste désir/prix crée la tension.
   Ancre : «Elle voulait être belle. Le prix était monstrueux» (729k)
5. **«Quand …» relatable** — format meme situationnel (×1,77) : le spectateur se reconnaît, ou reconnaît quelqu'un.
   Ancre : «Quand ton voisin défonce ton mur pour défendre son anime» (729k)
6. **Interdit / tabou** — la transgression sociale ou relationnelle du script : intrusion, relation interdite, mensonge de trop, place volée.
   Ancre : «Elle était l'intruse de sa propre maison» (709k)
7. **Full-send choc** — l'affirmation la plus agressive que le script peut vaguement soutenir. Sur ce slot uniquement, l'exagération est autorisée : promets l'extrême.
8. **Factuel affûté** — description honnête de la scène, mais avec un verbe d'action fort et un détail concret. C'est le choix de sécurité, pas le choix mou.

Interdiction de paraphrase : les 8 titres doivent exploiter des éléments DIFFÉRENTS du script, pas 8 variantes de la même idée.

# Bloc 2 : contenu par plateforme

## YouTube

- `description` : 2 phrases maximum. Phrase 1 : relance la curiosité avec d'AUTRES mots que le titre, sans JAMAIS résoudre sa promesse — si la description répond au titre, plus personne n'a besoin de cliquer. Phrase 2 : appel à l'action doux, en tutoiement. Aucun nom d'œuvre ni de personnage.
- `tags` : [OEUVRE] + ses graphies alternatives évidentes (romaji, titre anglais) + des termes de recherche français collant au genre de la scène (par exemple «anime romance», «anime horreur», «anime combat») + «anime», «manga».

## Facebook

- `description` : 3 à 4 phrases courtes en mini-teaser : mise en place → escalade → coupe juste avant la révélation. Termine par un appel à l'action naturel de ton cru (abonnement ou question en commentaire), en tutoiement. 1 à 3 hashtags à la fin si c'est naturel.
- `tags` : inclure [OEUVRE], Anime, Manga, Otaku, Recommandation Anime, Scène Culte, Meilleur Anime.

## Instagram

- Retourne seulement `hashtags` : 3 à 5 entrées, chacune commençant déjà par `#`.
- Mélange 1 à 2 hashtags larges (#anime) avec 2 à 3 hashtags de niche collés au genre et au ton de la scène — les tags de niche classent, les tags larges noient.
- Pas de phrase, pas de caption complète.

## TikTok

- Ne retourne AUCUN champ TikTok. Le texte TikTok final sera composé plus tard automatiquement dans l'application.

# Format de sortie

Tu dois fournir EXCLUSIVEMENT un objet JSON valide, sans texte avant ni après.

Structure attendue :
{
"title_candidates": ["Titre 1", "Titre 2", "..."],
"facebook": {
"description": "String",
"tags": ["String"]
},
"instagram": {
"hashtags": ["#String"]
},
"youtube": {
"description": "String",
"tags": ["String"]
}
}

# Données d'entrée

1. Le titre de l'anime est : [OEUVRE]

2. La narration complète de la vidéo (script) est : [SCRIPT]
```

- [ ] **Step 5: Run the invariant tests**

```bash
.pixi/envs/default/bin/python -m pytest backend/tests/test_metadata_prompt_templates.py -v
```

Expected: `test_fr_placeholders`, `test_schema_keys_present_in_both`, `test_exactly_eight_titles_mentioned` PASS. `test_dropped_fixed_facebook_cta` still FAILS (multi file not yet rewritten) — that is the expected state after this task; Task 2 turns it green. `test_multi_placeholders` PASSES (old multi file already has all three placeholders).

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_metadata_prompt_templates.py backend/prompts/default/metadata_fr.md
git commit -m "feat(prompts): CTR-calibrated 8-slot metadata_fr prompt + template invariant tests

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Rewrite `metadata_multi.md`

**Files:**
- Modify: `backend/prompts/default/metadata_multi.md` (full replacement)
- Test: `backend/tests/test_metadata_prompt_templates.py` (already written in Task 1 — no edits)

**Interfaces:**
- Consumes: Task 1's test file as-is.
- Produces: the multi template Task 3 smoke-tests with `target_language="en"`.

- [ ] **Step 1: Replace `backend/prompts/default/metadata_multi.md` with exactly this content**

```markdown
# Role & Objective

You are the title strategist for an anime short-form channel. Your single success metric is click-through rate (CTR). You generate:

- 8 unified metadata title candidates for all platforms
- platform-specific descriptions and tags for Facebook and YouTube
- Instagram hashtags

The final title will be chosen later inside the app and injected automatically into the final platform metadata.

# Language Rule (CRITICAL)

- Every output field must be written natively in [TARGET].
- Write like a native [TARGET] short-form creator, never like a translator: use the hook constructions, informal register, and direct-address forms that natives actually use (for example English "When…" / "What if… you?" patterns, or their natural [TARGET] equivalents).
- If [TARGET] distinguishes formal/informal address, ALWAYS use the informal form. Formal register measurably kills clicks on this channel.

# Golden Rule: Gatekeeping (CRITICAL)

- NEVER mention [OEUVRE] in titles, descriptions, or hashtags.
- NEVER use character proper names from [OEUVRE].
- Replace names with contextual descriptions or archetypes: "this high-schooler", "his alien wife", "her neighbor".
- Hard evidence: the only 2 videos of this channel that ever named the work in the title are its 2 worst videos of all time (7,900 and 1,500 views vs a 68,000 median). Mystery about the work IS the click and comment engine.
- Single exception: the YouTube and Facebook `tags` fields (invisible at click time) must contain [OEUVRE].

# Voice & Tone

- Write like you are telling a friend the craziest thing you saw today.
- Short, punchy sentences. Readable in half a second.
- Emojis: 0 to 1 per title, only when it adds emotion; 0 to 2 per other field.

# Block 1: the 8 titles (your core mission)

These rules come from statistical analysis of the channel's last 200 videos. They are not suggestions.

## Hard rules

1. Target length: 36 to 48 characters including spaces (the measured sweet spot: ×1.27). Absolute maximum: 62 — beyond that the app truncates the title mid-word and the title is dead.
2. NO generic questions: "?" titles perform at ×0.54 vs median. Single allowed exception: the "What if … you?" construction of slot 1.
3. NO exclamation marks (×0.78). NO digits (×0.81).
4. NO isekai/RPG jargon: mage, level, guild, build, boss, stats (×0.91 — that vocabulary shrinks the audience to gamers).
5. By default, open on a character pronoun or possessive ("He / She / His / Her / This …" in [TARGET]): character-story openers measure ×1.31 — unless the slot formula says otherwise.
6. Vagueness kills: "not like the others", "very special", "incredible" are banned. Always a concrete detail instead.
7. No hashtags inside titles.
8. Each title must work as-is on YouTube, Facebook, Instagram, and TikTok.

## The 8 slots (in this order, exactly one title each)

Each slot uses a psychological lever proven on THIS channel. The reference mechanics below are described conceptually — express each one the way a native [TARGET] creator would, adapted to the script.

1. **Second-person threat / hypothetical** — pull the viewer into the story with direct address ("you/your"). Strongest measured lever: ×3.07. Take the central threat or power of the script and turn it toward the viewer. (Reference mechanic: "What if someone could take control of you?" — 1.9M views.)
2. **Exposed secret** — a hidden truth just came out, with ZERO details given (secret/hidden/discovered lexicon: ×1.62). The information gap forces the click. (Reference: "Her heavy secret has just been discovered" — 732k.)
3. **Deadpan absurd premise** — state the weirdest premise of the script matter-of-factly, without commenting on it. The stranger it sounds said soberly, the more it clicks. (References: "Her virtual date was an old geezer 😭" — 1.3M; "He flees his alien wife everywhere" — 711k.)
4. **Dark price in two beats** — desire stated in a short sentence. Period. Dark consequence. The desire/price contrast creates tension. (Reference: "She wanted to be beautiful. The price was monstrous" — 729k.)
5. **Relatable "When…" meme opener** — situational meme format (×1.77): the viewer recognizes themselves or someone they know. Use the native [TARGET] meme construction. (Reference: "When your neighbor smashes your wall to defend his anime" — 729k.)
6. **Forbidden / taboo** — the social or relational transgression in the script: intrusion, forbidden relationship, one lie too many, a stolen place. (Reference: "She was the intruder in her own home" — 709k.)
7. **Full-send shock** — the most aggressive claim the script can remotely support. On this slot only, overselling is allowed: promise the extreme.
8. **Sharp factual** — honest description of the scene, but with a strong action verb and one concrete detail. It is the safety pick, not the weak pick.

No paraphrasing: the 8 titles must exploit DIFFERENT elements of the script, not 8 variants of the same idea.

# Block 2: platform-specific content

## YouTube

- `description`: 2 sentences maximum, in [TARGET]. Sentence 1: re-open the curiosity gap with DIFFERENT words than the title, without EVER resolving its promise — if the description answers the title, nobody needs to click. Sentence 2: soft call to action, informal register. No work or character names.
- `tags`: [OEUVRE] + its obvious alternative spellings (romaji, English title) + [TARGET]-language search terms matching the genre of the scene (for example "romance anime", "horror anime") + "anime", "manga".

## Facebook

- `description`: 3 to 4 short sentences in [TARGET], structured as a mini-teaser: setup → escalation → cut just before the reveal. End with a natural call to action of your own (subscribe or a question driving comments), informal register. 1 to 3 hashtags at the end if natural.
- `tags`: include [OEUVRE], plus the [TARGET]-language equivalents of: Anime, Manga, Otaku, Anime Recommendation, Iconic Scene, Best Anime.

## Instagram

- Return only `hashtags`: 3 to 5 entries, each already starting with `#`.
- Mix 1-2 broad hashtags (#anime) with 2-3 niche hashtags tied to the genre and tone of the scene, in [TARGET] where natural — niche tags rank, broad tags drown.
- No sentence, no full caption.

## TikTok

- Do NOT return any TikTok field. TikTok text will be composed later by the app.

# Output Format

Return VALID JSON only, with no markdown and no extra text.

Expected structure:
{
"title_candidates": ["Title 1", "Title 2", "..."],
"facebook": {
"description": "String",
"tags": ["String"]
},
"instagram": {
"hashtags": ["#String"]
},
"youtube": {
"description": "String",
"tags": ["String"]
}
}

# Input Data

1. Anime title: [OEUVRE]

2. Full video narration (script): [SCRIPT]
```

- [ ] **Step 2: Run the full invariant test file — all green now**

```bash
.pixi/envs/default/bin/python -m pytest backend/tests/test_metadata_prompt_templates.py -v
```

Expected: all 5 tests PASS (the fixed CTA string is now gone from both files).

- [ ] **Step 3: Commit**

```bash
git add backend/prompts/default/metadata_multi.md
git commit -m "feat(prompts): language-generic 8-slot CTR metadata_multi prompt

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Live light-tier smoke test of both prompts

**Files:**
- Create: `/tmp/claude-1000/-home-sid-Projects-anime-tiktok-reproducer/23bc34b9-cb50-4157-9395-7f0ac7924a19/scratchpad/metadata_smoke.py` (scratchpad — NOT committed)
- Possibly modify: the two prompt files, if smoke findings require wording fixes (then re-run Task 1/2 pytest + this smoke test, and commit the fix)

**Interfaces:**
- Consumes: `MetadataService.build_prompt_from_script_payload(anime_name=..., script_payload=..., target_language=..., library_type=...)`, `LLMService.generate_json(prompt, tier="light")`, `MetadataService.validate_candidate_payload(payload)` — all existing code.
- Produces: evidence for the completion claim (verification-before-completion).

- [ ] **Step 1: Write the smoke script**

```python
"""Smoke-test the reworked metadata prompts through the real light-tier LLM."""
import json
import re
import sys

sys.path.insert(0, "/home/sid/Projects/anime-tiktok-reproducer/backend")
from app.services.metadata import MetadataService  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402

PROJECT = "/home/sid/Projects/anime-tiktok-reproducer/backend/data/projects/7052206637f5"
ANIME = "Dragon Girl (Yonghansonyeo, Dragon Girl Problem)"
FORBIDDEN = ["dragon girl", "yonghansonyeo"]

script_payload = json.load(open(f"{PROJECT}/new_script.json"))


def run(lang: str) -> None:
    print(f"\n===== target_language={lang} =====")
    prompt = MetadataService.build_prompt_from_script_payload(
        anime_name=ANIME,
        script_payload=script_payload,
        target_language=lang,
    )
    assert "[SCRIPT]" not in prompt and "[OEUVRE]" not in prompt and "[TARGET]" not in prompt
    raw = LLMService.generate_json(prompt, tier="light")
    validated = MetadataService.validate_candidate_payload(raw)
    titles = validated.title_candidates
    assert len(titles) == 8, f"expected 8 titles, got {len(titles)}"
    in_range = 0
    for i, t in enumerate(titles, 1):
        n = len(t)
        ok_len = 36 <= n <= 48
        in_range += ok_len
        flags = []
        if "!" in t:
            flags.append("EXCLAM")
        if re.search(r"\d", t):
            flags.append("DIGIT")
        for f in FORBIDDEN:
            if f in t.lower():
                flags.append("NAME-LEAK")
        print(f"  {i}. ({n:2d}ch{'*' if not ok_len else ' '}) {t}   {' '.join(flags)}")
    visible = " ".join(
        [validated.youtube.description, validated.facebook.description]
        + validated.instagram.hashtags
    ).lower()
    for f in FORBIDDEN:
        assert f not in visible, f"anime name leaked in visible field: {f}"
    yt_tags = " ".join(validated.youtube.tags).lower()
    fb_tags = " ".join(validated.facebook.tags).lower()
    assert any(f in yt_tags for f in FORBIDDEN), "OEUVRE missing from youtube tags"
    assert any(f in fb_tags for f in FORBIDDEN), "OEUVRE missing from facebook tags"
    print(f"  titles in 36-48 window: {in_range}/8 (soft target: >=5)")
    print(f"  yt_desc: {validated.youtube.description}")
    print(f"  fb_desc: {validated.facebook.description}")
    print(f"  ig_tags: {validated.instagram.hashtags}")


run("fr")
run("en")
print("\nSMOKE OK")
```

Note: if `validate_candidate_payload` returns a dict rather than the pydantic model in the installed version, adapt attribute access (`validated["youtube"]["description"]`, etc.) — check its return type in `backend/app/services/metadata.py` before running.

- [ ] **Step 2: Run it**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
.pixi/envs/default/bin/python /tmp/claude-1000/-home-sid-Projects-anime-tiktok-reproducer/23bc34b9-cb50-4157-9395-7f0ac7924a19/scratchpad/metadata_smoke.py
```

Expected: `SMOKE OK`, with for each language: 8 titles printed with char counts, no NAME-LEAK / EXCLAM / DIGIT flags, ≥5 of 8 titles inside 36–48 chars, all 8 within 62 (validator would have truncated otherwise — visually check no mid-word cuts), the fr titles recognizably matching the 8 slots (2nd-person hypothetical first, «Quand …» in slot 5, etc.), and the en run entirely in English.

- [ ] **Step 3: Judge quality, fix prompts only if hard criteria fail**

Hard failure = wrong language, name leak, >2 titles outside the 36–48 window, missing slot structure (e.g., no «Quand»/When-style title, no 2nd-person title), exclamations/digits present. If any occur: adjust the offending rule's wording in the relevant prompt file (typically strengthening the rule with the imperative at the top of Block 1), re-run pytest + smoke. Do NOT chase subjective taste differences across runs — light-tier output varies.

- [ ] **Step 4: Commit any prompt fixes (skip if none)**

```bash
git add backend/prompts/default/metadata_fr.md backend/prompts/default/metadata_multi.md
git commit -m "fix(prompts): tighten metadata prompt rules after light-tier smoke test

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Final verification + report**

```bash
.pixi/envs/default/bin/python -m pytest backend/tests/test_metadata_prompt_templates.py -v
git log --oneline main..feat/metadata-ctr-prompts
```

Expected: 5/5 tests pass; branch contains the spec commit + 2-3 implementation commits. Report smoke output (the actual generated titles) to the user — they are the deliverable.
