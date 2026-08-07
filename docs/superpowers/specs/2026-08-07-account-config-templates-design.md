# Account Config Templates — Design

**Date:** 2026-08-07
**Status:** Approved by owner

## Problem

Every account in `config/accounts/config.yaml` repeats full platform
configuration. In the real config today, 4 accounts duplicate byte-identical
`youtube:`, `meta:`, `facebook:`, `instagram:` blocks and the same top-level
`slots:` list, because they share the same YouTube channel and Facebook/IG
identity while having different TikTok accounts. Sharing groups can differ per
platform (two accounts may share YouTube but not Meta), so whole-account
duplication is both verbose and error-prone (a token rotation must be pasted
N times).

## Solution

An **optional** template system inside the same YAML file.

### Syntax

- New optional top-level `templates:` mapping: name → partial account config.
- New optional per-account key `template:` accepting a **string or list of
  strings** (template names).

```yaml
templates:
  yt_main:                 # shared YouTube channel
    youtube:
      refresh_token: "1//03..."
      channel_id: "UCGJOa..."
  meta_spm:                # shared FB page + IG identity
    meta:
      token_mode: "system_user"
      facebook_page_id: "101949..."
      facebook_page_access_token: "EAA..."
      instagram_business_account_id: "17841..."
    facebook: { max_reel_duration_seconds: 14400 }
    instagram: { max_reel_duration_seconds: 180 }
  fr_anime_defaults:
    language: "fr"
    supported_types: ["anime"]
    slots: ["06:00", "12:00", "18:00", "22:00"]

accounts:
  anime_fr:
    template: [fr_anime_defaults, yt_main, meta_spm]
    name: "AnimeSPM"
    avatar: "spm_anime_fr.jpg"
    tiktok:
      slots: ["13:00"]
```

### Merge semantics

- Templates apply **left-to-right**, then the account's own keys apply last —
  later always wins.
- **Dicts deep-merge** key-by-key (an account can override just
  `youtube.slots` and inherit the rest of the `youtube:` block).
- **Lists and scalars replace wholesale** — no concatenation. This matches the
  existing replace-semantics of per-platform `slots:`.
- Templates are plain data: **no template-inside-template nesting**. A
  `template:` key found inside a template is ignored with a logged warning.
- The `template` key is stripped during resolution and never reaches
  `_parse_account`.

### Implementation

All in `backend/app/services/account_service.py`:

- `_deep_merge(base: dict, override: dict) -> dict` — pure helper, recursive
  on dict values, replace otherwise. Never mutates inputs.
- `_resolve_templates(account_raw: dict, templates: dict) -> dict` — coerces
  `template:` (string → one-element list), validates names, folds templates
  left-to-right with `_deep_merge`, applies the account's own keys last,
  strips the `template` key.
- `_load_from_disk` reads the optional top-level `templates:` mapping and
  resolves each account's raw dict before calling the existing
  `_parse_account`, which stays untouched.

### Error handling

- Unknown template name, or a `template:` value that is not a string / list
  of strings → `ValueError` for that account. The existing per-account
  `try/except` in `_load_from_disk` logs the exception and skips **only that
  account**; the rest of the file loads (same behavior as today's invalid
  `post_for_me_platform`).
- A top-level `templates:` that is not a mapping is treated as absent with a
  logged warning.

### Backward compatibility / scope

- Both new keys are optional; a file without them parses exactly as before.
- The VPS server (`server/app/config.py`) reads its **own separate** slim
  config file and is untouched.
- No changes to the API layer, frontend, or serialized account dicts —
  resolution happens entirely at load time.

### Docs

`config/accounts/config.example.yaml` gains a documented `templates:` example
showing the multi-template mixing pattern (shared-identity templates +
defaults template).

### Tests

New cases alongside the existing backend tests (run via `pixi -e dev`):

1. `template:` as a string resolves and merges.
2. `template:` as a list — later template overrides earlier on conflicts.
3. Account keys override all templates (including one nested key inside a
   platform block, proving deep merge).
4. Lists replace wholesale (`slots` from account replaces template's).
5. Unknown template name → that account skipped, sibling accounts still load.
6. Config without `templates:` / without `template:` keys → identical
   behavior to today.
7. `template` key never leaks into the parsed `AccountConfig`.
8. Nested `template:` inside a template is ignored (with warning), not
   resolved.
