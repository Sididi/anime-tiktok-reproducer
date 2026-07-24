# Metadata Prompts CTR Rework — Design

**Date:** 2026-07-24
**Scope:** `backend/prompts/default/metadata_fr.md` and `backend/prompts/default/metadata_multi.md` only. No Python, schema, or validator changes.

## Goal

Rework both metadata prompts to generate higher-CTR title candidates, using psychological title patterns **calibrated on real channel performance data** (200 videos pulled from the Anime SPM YouTube channel via the Data API, 176 mature ones analyzed against median views).

## Evidence base (drives every design choice)

Median views 68.4k. Feature lifts measured on titles of mature videos:

| Feature | n | Lift vs median |
|---|---|---|
| 2nd-person address (te/toi/ton) | 12 | ×3.07 |
| «Quand …» meme format | 9 | ×1.77 |
| Secret/caché/découvert lexicon | 15 | ×1.62 |
| Character-story opener (Il/Elle/Son/Sa/Cette) | 85 | ×1.31 |
| Length 36–45 chars | 76 | ×1.27 |
| Question mark (generic questions) | 7 | ×0.54 |
| Exclamation mark | 11 | ×0.78 |
| Numbers in title | 21 | ×0.81 |
| Isekai/RPG jargon (mage, niveau, build…) | 16 | ×0.91 |

Additional facts: the channel's 2 worst videos ever (7.9k, 1.5k) are the only ones that named the anime in the title — gatekeeping is empirically validated. The single «vous» title is the worst on the channel. Titles >62 chars get silently truncated at a word boundary by the app validator (`MetadataTitleCandidatesPayload`).

Runtime constraint: metadata generation runs on the **light LLM tier** (default gpt-5.4-mini, medium thinking) — the prompt must be explicit and pattern-driven, not rely on subtle judgment.

## Owner decisions

- Optimize for **YouTube Shorts first** (title visible in feed/search); titles stay unified across platforms.
- Clickbait policy: **portfolio mix** — mostly full-send + aggressive-but-honest, one factual fallback.
- Full prompt rework (titles + descriptions + tags), JSON schema and 8-candidate structure untouched.
- `metadata_multi` stays **language-generic** (no per-language sections).
- `default/` prompts stay **anime-centric** (library override folders remain empty; overrides can be created later if Simpsons/films pipelines are used).
- The mandatory Facebook CTA («Abonne toi pour plus de présentations d'anime») is **dropped** in both files; the model writes a natural native CTA itself.

## Block 1 — Title design (both files): 8 named slots

`title_candidates` is generated as 8 fixed slots, in order. Each slot = one line of formula + psychology + real anchor example (French file) or conceptual anchor (multi file).

1. **Menace/hypothèse 2ᵉ personne** — direct address, implicit threat or hypothetical («Et si… toi ?»). Self-referential processing + loss framing. Anchor: «Et si quelqu'un pouvait prendre le contrôle de toi ?» (1.9M).
2. **Secret découvert** — a hidden truth just exposed, zero details. Pure information-gap. Anchor: «Son lourd secret vient d'être découvert» (732k).
3. **Concept absurde assumé** — deadpan statement of a bizarre premise. Schema violation. Anchors: «Son rencard virtuel était un vieux daron 😭» (1.3M); «Il fuit sa femme extraterrestre partout» (711k).
4. **Prix sombre en deux temps** — short sentence, period, dark consequence. Negativity bias + tension. Anchor: «Elle voulait être belle. Le prix était monstrueux» (729k).
5. **«Quand …» relatable** — meme-format situational opener. In-group identity + humor. Anchor: «Quand ton voisin défonce ton mur pour défendre son anime» (729k).
6. **Interdit/tabou** — forbidden relationship or social transgression in the scene. Anchor: «Elle était l'intruse de sa propre maison» (709k).
7. **Full-send choc** — most aggressive claim the script can remotely support; overselling explicitly allowed on this slot only.
8. **Factuel affûté** — honest scene description with a story verb + one concrete detail (safety pick).

### Hard title rules (data-derived)

- Target **36–48 characters**; absolute max 62 (over-limit titles are truncated mid-thought by the app — a truncated title is a dead title).
- **No generic questions.** The only «?» permitted is the slot-1 «Et si … toi ?» construction.
- No exclamation marks. No numbers. No isekai/RPG jargon (mage, niveau, guilde, build, boss…). No «vous» ever.
- Default to a character opener (Il/Elle/Son/Sa/Cette) unless the slot's formula says otherwise.
- Gatekeeping unchanged and reinforced with the empirical warning: never name the work or its characters.
- No hashtags in titles; emojis 0–1 per title, only when they add tone (😭-style).
- Tone: tutoiement, dynamic French; allowed argot list kept («dinguerie», «banger», «masterclass», «pépite»); framing line: "write like you're telling a friend the craziest thing you saw today."

## Block 2 — Platform content

### YouTube
- `description` (max 2 sentences): sentence 1 re-opens the curiosity gap **without resolving the title's promise**; sentence 2 = soft tutoiement CTA. No anime name (gatekeeping applies to visible descriptions).
- `tags`: `[OEUVRE]` + obvious alt spellings/romaji + genre-specific French search terms matching the scene (tags are invisible → gatekeeping does not apply; search relevance is free).

### Facebook
- `description` (3–4 short sentences): mini-narrative teaser — setup, escalation, cut before the payoff. Ends with a natural model-written CTA (fixed CTA removed). 1–3 hashtags allowed at the end.
- `tags`: keep current fixed list ([OEUVRE], Anime, Manga, Otaku, Recommandation Anime, Scène Culte, Meilleur Anime).

### Instagram
- `hashtags`: 3–5; mix 1–2 broad («#anime») + 2–3 niche genre tags.

### TikTok
- Still absent from output (composed later by the app). Unchanged.

## metadata_multi.md specifics

- Same 8-slot architecture, same hard rules; slots described as universal psychology with conceptual anchors (no French example strings).
- Governing rule: **write natively in [TARGET]** — never translate French/English phrasing literally; use the constructions a native short-form creator would use (e.g. English «POV:»/«When…»; local informal register where the language distinguishes).
- French artifacts removed: no French CTA, Facebook tag list rendered natively in [TARGET] (keeping [OEUVRE]).
- Char rules identical (validator is language-blind).

## Output format & plumbing (unchanged)

- JSON structure identical: `title_candidates` (exactly 8), `facebook{description,tags}`, `instagram{hashtags}`, `youtube{description,tags}`.
- Placeholders: `[OEUVRE]`, `[SCRIPT]` in both files; `[TARGET]` in multi only (the fr path never substitutes it — the fr file must not contain `[TARGET]`).

## Validation plan

1. Static checks: placeholders correct per file; JSON structure block matches `MetadataTitleCandidatesPayload`; no `[TARGET]` token in the fr file.
2. Live smoke test: run both prompts through the project's light-tier model via OpenRouter with a real script from `backend/data/projects/*/`; assert output parses through the validator, 8 titles present, majority in the 36–48 char window, no anime-name leakage, slots recognizable.

## Out of scope

- Any Python/schema change; per-library override files; TikTok text; A/B measurement tooling; overlay/script prompts.
