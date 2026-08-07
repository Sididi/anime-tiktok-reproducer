# Account Config Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optional `templates:` section in `config/accounts/config.yaml` so accounts can share platform credential blocks instead of duplicating them.

**Architecture:** Two pure module-level helpers in `account_service.py` (`_deep_merge`, `_resolve_templates`) resolve each account's raw dict before it reaches the existing `_parse_account`, which stays untouched. Resolution happens entirely at load time; API layer, frontend, and VPS server are unaffected.

**Tech Stack:** Python 3 / FastAPI backend, PyYAML, pytest (run via `pixi -e dev`).

**Spec:** `docs/superpowers/specs/2026-08-07-account-config-templates-design.md`

## Global Constraints

- Merge semantics: templates apply left-to-right, account keys last; dicts deep-merge; lists and scalars replace wholesale.
- `template:` accepts a string or a list of strings; anything else → `ValueError` for that account.
- Unknown template name → `ValueError` for that account; the existing per-account `try/except` in `_load_from_disk` skips only that account.
- No template nesting: a `template` key inside a template is ignored with a logged warning.
- Both new keys optional — a file without them must parse exactly as before.
- Tests run from repo root: `pixi run -e dev pytest backend/tests/test_account_service.py -v` (default pixi env lacks pytest plugins; always use `-e dev`). Never overlap two pytest runs.

---

### Task 1: Merge/resolution helpers

**Files:**
- Modify: `backend/app/services/account_service.py` (add two module-level functions after `_normalize_slots`, around line 32)
- Test: `backend/tests/test_account_service.py` (append)

**Interfaces:**
- Consumes: nothing new (stdlib + existing `logger`).
- Produces: `_deep_merge(base: dict, override: dict) -> dict` and `_resolve_templates(account_id: str, account_raw: dict, templates: dict) -> dict`, both importable from `app.services.account_service`. Task 2 calls `_resolve_templates` inside `_load_from_disk`.

- [ ] **Step 1: Write the failing unit tests**

Append to `backend/tests/test_account_service.py`:

```python
# ---------------------------------------------------------------------------
# Template system: unit tests for the merge/resolution helpers


def test_deep_merge_nested_dicts_and_list_replacement():
    from app.services.account_service import _deep_merge

    base = {
        "slots": ["06:00", "12:00"],
        "youtube": {"refresh_token": "tok", "slots": ["10:00"]},
        "name": "Base",
    }
    override = {
        "slots": ["08:00"],
        "youtube": {"slots": ["11:00"]},
    }
    merged = _deep_merge(base, override)
    # Lists replace wholesale, dicts merge key-by-key.
    assert merged["slots"] == ["08:00"]
    assert merged["youtube"] == {"refresh_token": "tok", "slots": ["11:00"]}
    assert merged["name"] == "Base"
    # Inputs are not mutated.
    assert base["slots"] == ["06:00", "12:00"]
    assert base["youtube"]["slots"] == ["10:00"]
    assert override["youtube"] == {"slots": ["11:00"]}


def test_resolve_templates_order_and_strip():
    from app.services.account_service import _resolve_templates

    templates = {
        "a": {"language": "fr", "slots": ["06:00"]},
        "b": {"slots": ["09:00"], "device": "iphone_16"},
    }
    resolved = _resolve_templates(
        "acc",
        {"template": ["a", "b"], "name": "Acc", "device": "poco_x7_pro"},
        templates,
    )
    # Later template wins over earlier; account keys win over all.
    assert resolved == {
        "language": "fr",
        "slots": ["09:00"],
        "device": "poco_x7_pro",
        "name": "Acc",
    }
    assert "template" not in resolved


def test_resolve_templates_string_form_and_no_template():
    from app.services.account_service import _resolve_templates

    templates = {"a": {"language": "fr"}}
    resolved = _resolve_templates("acc", {"template": "a", "name": "Acc"}, templates)
    assert resolved == {"language": "fr", "name": "Acc"}
    # Without a template key the dict passes through unchanged.
    raw = {"name": "Acc"}
    assert _resolve_templates("acc", raw, templates) == {"name": "Acc"}


def test_resolve_templates_errors():
    from app.services.account_service import _resolve_templates

    with pytest.raises(ValueError, match="nope"):
        _resolve_templates("acc", {"template": "nope"}, {})
    with pytest.raises(ValueError, match="template"):
        _resolve_templates("acc", {"template": 42}, {})
    with pytest.raises(ValueError, match="template"):
        _resolve_templates("acc", {"template": ["a", 42]}, {"a": {}})


def test_resolve_templates_ignores_nested_template_key(caplog):
    from app.services.account_service import _resolve_templates

    templates = {"a": {"template": "b", "language": "fr"}, "b": {"language": "en"}}
    resolved = _resolve_templates("acc", {"template": "a", "name": "Acc"}, templates)
    # Nested reference is NOT resolved: language comes from "a", not "b".
    assert resolved == {"language": "fr", "name": "Acc"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_account_service.py -v -k "deep_merge or resolve_templates"`
Expected: FAIL — `ImportError: cannot import name '_deep_merge'`

- [ ] **Step 3: Write the implementation**

In `backend/app/services/account_service.py`, insert after `_normalize_slots` (line 32), before the `AccountYouTubeConfig` dataclass:

```python
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override into base without mutating either input.

    Dict values merge recursively key-by-key; every other type — lists
    included — replaces wholesale, matching per-platform slots semantics.
    """
    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _resolve_templates(
    account_id: str,
    account_raw: dict[str, Any],
    templates: dict[str, Any],
) -> dict[str, Any]:
    """Fold `template:` references left-to-right, account's own keys last.

    Returns a new dict with the `template` key stripped. Raises ValueError on
    an unknown template name or a malformed `template:` value so the caller's
    per-account error handling skips only this account.
    """
    if "template" not in account_raw:
        return account_raw
    ref = account_raw["template"]
    names = [ref] if isinstance(ref, str) else ref
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError(
            f"Invalid template reference {ref!r} for account {account_id}; "
            "expected a template name or list of names"
        )
    merged: dict[str, Any] = {}
    for name in names:
        template_raw = templates.get(name)
        if not isinstance(template_raw, dict):
            raise ValueError(
                f"Unknown template {name!r} referenced by account {account_id}"
            )
        if "template" in template_raw:
            logger.warning(
                "Template %r contains a nested 'template' key; templates cannot "
                "reference other templates — ignoring it",
                name,
            )
            template_raw = {k: v for k, v in template_raw.items() if k != "template"}
        merged = _deep_merge(merged, template_raw)
    account_own = {k: v for k, v in account_raw.items() if k != "template"}
    return _deep_merge(merged, account_own)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_account_service.py -v -k "deep_merge or resolve_templates"`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/account_service.py backend/tests/test_account_service.py
git commit -m "feat: add template merge/resolution helpers to AccountService"
```

---

### Task 2: Wire resolution into `_load_from_disk`

**Files:**
- Modify: `backend/app/services/account_service.py:285-310` (`_load_from_disk`)
- Test: `backend/tests/test_account_service.py` (append)

**Interfaces:**
- Consumes: `_resolve_templates(account_id, account_raw, templates)` from Task 1.
- Produces: end-to-end behavior — `AccountService.list_accounts()` / `get_account()` return template-resolved accounts. No signature changes.

- [ ] **Step 1: Write the failing integration tests**

Append to `backend/tests/test_account_service.py`:

```python
# ---------------------------------------------------------------------------
# Template system: integration through _load_from_disk


def test_templates_resolved_from_yaml(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
templates:
  yt_main:
    youtube:
      refresh_token: "tok-main"
      channel_id: "UC123"
  fr_defaults:
    language: "fr"
    supported_types: ["anime"]
    slots: ["06:00", "12:00", "18:00", "22:00"]

accounts:
  anime_fr:
    template: [fr_defaults, yt_main]
    name: "AnimeSPM"
    avatar: "anime_fr.jpg"
    slots: ["07:00"]
    youtube:
      slots: ["16:00"]
  standalone:
    name: "Standalone"
    language: "en"
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    acc = AccountService.get_account("anime_fr")
    assert acc is not None
    # Template scalars/lists inherited or replaced by account keys.
    assert acc.language == "fr"
    assert acc.slots == ["07:00"]  # account list replaces template list
    # Deep merge: youtube.slots overridden, refresh_token/channel_id inherited.
    assert acc.youtube is not None
    assert acc.youtube.refresh_token == "tok-main"
    assert acc.youtube.channel_id == "UC123"
    assert acc.slots_for("youtube") == ["16:00"]
    # Account without template: unchanged behavior.
    standalone = AccountService.get_account("standalone")
    assert standalone is not None and standalone.language == "en"


def test_unknown_template_skips_only_that_account(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
templates:
  good:
    language: "fr"

accounts:
  broken:
    template: missing
    name: "Broken"
  fine:
    template: good
    name: "Fine"
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    assert AccountService.get_account("broken") is None
    fine = AccountService.get_account("fine")
    assert fine is not None and fine.language == "fr"


def test_non_mapping_templates_section_ignored(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
templates: "oops"

accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    acc = AccountService.get_account("anime_fr")
    assert acc is not None and acc.language == "fr"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_account_service.py -v -k "templates"`
Expected: `test_templates_resolved_from_yaml` and `test_unknown_template_skips_only_that_account` FAIL (template key not resolved yet — accounts parse but ignore templates, so assertions on inherited values fail). `test_non_mapping_templates_section_ignored` may already pass.

- [ ] **Step 3: Wire into `_load_from_disk`**

In `backend/app/services/account_service.py`, `_load_from_disk`, replace the block after `accounts_raw` validation:

```python
        accounts_raw = raw.get("accounts", {})
        if not isinstance(accounts_raw, dict):
            return {}
        templates_raw = raw.get("templates") or {}
        if not isinstance(templates_raw, dict):
            logger.warning(
                "Top-level 'templates' in %s is not a mapping; ignoring it", path
            )
            templates_raw = {}
        result: dict[str, AccountConfig] = {}
        for account_id, account_raw in accounts_raw.items():
            if not isinstance(account_raw, dict):
                continue
            try:
                resolved = _resolve_templates(
                    str(account_id), account_raw, templates_raw
                )
                result[str(account_id)] = cls._parse_account(str(account_id), resolved)
            except Exception:
                logger.exception("Failed to parse account %s", account_id)
```

- [ ] **Step 4: Run the full account-service test file**

Run: `pixi run -e dev pytest backend/tests/test_account_service.py -v`
Expected: all PASS (new + pre-existing device/tiktok/reel tests — proves backward compatibility).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/account_service.py backend/tests/test_account_service.py
git commit -m "feat: resolve optional account templates in accounts config"
```

---

### Task 3: Document templates in config.example.yaml

**Files:**
- Modify: `config/accounts/config.example.yaml` (insert documented `templates:` example above `accounts:`)

**Interfaces:**
- Consumes: the syntax shipped in Tasks 1–2.
- Produces: docs only — no code.

- [ ] **Step 1: Add the documented example**

Insert into `config/accounts/config.example.yaml`, after the header comment block (line 5) and before `accounts:`:

```yaml
# --- Optional templates -----------------------------------------------------
#
# Accounts often share a social identity (same YouTube channel, same Facebook
# Page) while differing elsewhere. Define partial account configs here and
# reference them from accounts with `template:` (a name, or a list of names).
#
# Merge rules:
#   - Templates apply left-to-right; the account's own keys always win last.
#   - Nested blocks (youtube:, meta:, ...) merge key-by-key, so an account can
#     override just `youtube.slots` and inherit the rest from the template.
#   - Lists and scalars replace wholesale (same replace-semantics as slots).
#   - Templates cannot reference other templates.
#   - An unknown template name skips that account (logged), others still load.
#
# templates:
#   yt_main:                      # shared YouTube channel
#     youtube:
#       refresh_token: "1//0..."
#       channel_id: "UC..."
#   meta_main:                    # shared Facebook Page + Instagram identity
#     meta:
#       token_mode: "system_user"
#       facebook_page_id: "123..."
#       facebook_page_access_token: "EAA..."
#       instagram_business_account_id: "456..."
#     facebook:
#       max_reel_duration_seconds: 90
#     instagram:
#       max_reel_duration_seconds: 90
#   fr_defaults:                  # shared scheduling defaults
#     language: "fr"
#     supported_types: ["anime"]
#     slots: ["06:00", "12:00", "18:00", "22:00"]
#
# accounts:
#   anime_fr:
#     template: [fr_defaults, yt_main, meta_main]
#     name: "Anime FR"
#     avatar: "anime_fr.jpg"
#     tiktok:                     # TikTok differs per account — keep it inline
#       post_for_me_account_id: "spc_..."
```

- [ ] **Step 2: Sanity-check the example parses**

Run: `pixi run -e dev python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('config/accounts/config.example.yaml').read_text()); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add config/accounts/config.example.yaml
git commit -m "docs: document optional account templates in config example"
```
