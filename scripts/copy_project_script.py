#!/usr/bin/env python3
"""Copy a project's generated script to the clipboard.

Resolves the raw script saved during the `/script` phase for a given project id
(or the TikTok URL the project was uploaded to) and pushes it to the system
clipboard. Mirrors the backend resolution order used
by `GET /script/latest-generation`:

  1. The most recently modified automation run -> script_automation_runs/<run>/script.json
  2. Fallback -> <project>/new_script.json

Fails loudly if no script has been saved yet (i.e. the project has not reached
the `/script` phase).

Usage (from repo root):

    pixi run python scripts/copy_project_script.py <project_id>
    pixi run python scripts/copy_project_script.py <project_id> --json
    pixi run python scripts/copy_project_script.py <project_id> --stdout
    pixi run python scripts/copy_project_script.py https://www.tiktok.com/@animespm4/video/7668466849704709409

A TikTok URL is matched by video id against the project's *uploaded* TikTok URL
(`tiktok_pfm.url` / `upload_last_result` tiktok entry), never the source
`tiktok_url`. Projects published in manual TikTok mode have no stored URL.

Clipboard backends (Arch Linux): wl-copy (Wayland), xclip or xsel (X11).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Allow `from _env import ...` when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_dotenv  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR_NAME = "script_automation_runs"
OUTPUT_DIR_NAME = "output"
JSX_FILENAME = "import_project.jsx"
EDITED_AUDIO_FILENAME = "tts_edited.wav"  # post-auto-editor audio
# Mirrors backend/app/config.py:cep_trigger_url_template
DEFAULT_LINK_TEMPLATE = "http://localhost:48653/p/{project_id}"

# TikTok demonetisation appeal message. {video_url}, {cuts}, {duration},
# {interval} and {signature} are filled with the uploaded TikTok URL, real
# montage cut count, post-auto-editor video duration, average seconds-per-cut
# and the project's account ("<account name> - @<tiktok handle>"). Everything else is copied verbatim.
CONTEST_MESSAGE_TEMPLATE = """\
Bonjour,

Je suis résident français (Espace Économique Européen) et je demande formellement une révision humaine de la démonétisation appliquée à la vidéo {video_url} pour motif de "contenu de faible qualité".

En tant que résident EEE, je fais valoir mes droits sous l'Article 17 du Digital Services Act (Règlement UE 2022/2065) : TikTok est légalement tenu de fournir une motivation claire, précise et spécifique justifiant cette restriction. La mention générique "contenu de faible qualité" ne satisfait pas à cette obligation légale. Je demande également, conformément aux garanties applicables aux décisions restreignant la monétisation d'un contenu, que cette révision ne soit pas fondée uniquement sur un traitement automatisé.

– Script de présentation d'anime rédigé intégralement par mes soins
– Montage réalisé manuellement dans Premiere Pro: environ {cuts} coupes sur {duration}, soit une coupe toutes les ~{interval} secondes (cf. capture timeline en pièce jointe)
– Je dispose du projet Premiere Pro complet, de la timeline, du script source et du fichier de voix off.
– La vidéo ne constitue pas un repost, un Duet, un Stitch ou un contenu sponsorisé.
En résumé j'ai: **narration originale, montage original, structure argumentative et sélection éditoriale propre**

Incohérences de la décision :
Premièrement, suite à un précédent retour du support indiquant qu'un texte statique générique posait problème, j'ai modifié mon template en le supprimant. Une vidéo produite avec ce template corrigé a malgré tout été démonétisée sans nouvelle explication concrète, ce qui rend la décision inexplicable et inapplicable.

Deuxièmement, ce format est rigoureusement identique à celui de mes publications précédentes, toutes monétisées sans incident jusqu'au 15/04. Aucune modification de ma part n'est intervenue depuis. La démonétisation cible systématiquement les vidéos à forte audience et non les autres, ce qui est incompatible avec une évaluation cohérente et objective de la qualité.

Preuves disponibles immédiatement :
Capture de la timeline Premiere Pro, projet de montage complet, script source, fichier voix off, horodatages des exports, captures des modifications apportées suite au précédent retour support.

Je demande :
1. La réintégration de l'éligibilité Creator Rewards de cette vidéo.
2. À défaut, une explication précise identifiant quels éléments concrets de cette vidéo spécifique sont considérés comme "faible qualité" et quelles modifications spécifiques permettraient d'être conforme.
3. La confirmation que cette révision a été effectuée par une personne qualifiée et non uniquement par un système automatisé.

En l'absence d'une réponse motivée précise, j'exercerai mon droit d'escalade auprès d'un organisme de règlement extrajudiciaire certifié sous le DSA (Appeals Centre Europe, appealscentre.eu), conformément à l'Article 21 du Règlement UE 2022/2065.

Cordialement,
{signature}"""
VIDEO_URL_PLACEHOLDER = "<URL de la vidéo TikTok>"
SIGNATURE_PLACEHOLDER = "<Nom du compte> - @<compte TikTok>"
# Mirrors backend/app/services/project_service.py
_VALID_ID = re.compile(r"[a-zA-Z0-9_-]+$")
_TIKTOK_VIDEO_ID_RE = re.compile(r"tiktok\.com/.*?/video/(\d+)")
_TIKTOK_HANDLE_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9_.]+)/video/")


def _projects_dir() -> Path:
    """Resolve the projects directory, honoring the ATR_PROJECTS_DIR override."""
    override = os.getenv("ATR_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    return REPO_ROOT / "backend" / "data" / "projects"


def _validate_project_id(project_id: str) -> None:
    if not project_id or not _VALID_ID.fullmatch(project_id):
        raise SystemExit(
            f"Invalid project id {project_id!r}: must be alphanumeric/hyphen/underscore."
        )


def _tiktok_video_id(url: str | None) -> str | None:
    match = _TIKTOK_VIDEO_ID_RE.search(url or "")
    return match.group(1) if match else None


def uploaded_tiktok_urls(project: dict) -> list[str]:
    """The project's published TikTok URLs (PFM state + upload_last_result)."""
    urls: list[str] = []
    pfm = project.get("tiktok_pfm")
    if isinstance(pfm, dict) and pfm.get("url"):
        urls.append(str(pfm["url"]))
    result = project.get("upload_last_result")
    entries = result.get("platforms") if isinstance(result, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("platform") == "tiktok" and entry.get("url"):
            urls.append(str(entry["url"]))
    return [u for u in dict.fromkeys(urls) if _tiktok_video_id(u)]


def _load_project_json(project_dir: Path) -> dict:
    try:
        data = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_project_ref(ref: str) -> str:
    """Return a project id from either a project id or an uploaded TikTok URL."""
    if "tiktok.com" not in ref:
        return ref
    video_id = _tiktok_video_id(ref)
    if video_id is None:
        raise SystemExit(
            f"Unsupported TikTok URL {ref!r}: expected .../@<account>/video/<id> "
            "(short vm.tiktok.com links must be opened first)."
        )
    projects_dir = _projects_dir()
    matches = sorted(
        d.name
        for d in projects_dir.iterdir()
        if d.is_dir()
        and any(
            _tiktok_video_id(u) == video_id
            for u in uploaded_tiktok_urls(_load_project_json(d))
        )
    ) if projects_dir.is_dir() else []
    if not matches:
        raise SystemExit(
            f"No project under {projects_dir} was uploaded to TikTok video {video_id}. "
            "(Manual TikTok mode uploads store no URL: pass the project id instead.)"
        )
    if len(matches) > 1:
        raise SystemExit(
            f"TikTok video {video_id} matches several projects: {', '.join(matches)}. "
            "Pass the project id instead."
        )
    return matches[0]


def _accounts_config_path() -> Path:
    """Mirrors backend/app/config.py:accounts_config_path (ATR_ override)."""
    override = os.getenv("ATR_ACCOUNTS_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return REPO_ROOT / "config" / "accounts" / "config.yaml"


def account_display_name(account_id: str) -> str | None:
    """The account's `name` from config/accounts/config.yaml (templates honored)."""
    import yaml

    path = _accounts_config_path()
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    raw = (config.get("accounts") or {}).get(account_id)
    if not isinstance(raw, dict):
        return None
    if raw.get("name"):
        return str(raw["name"])
    templates = config.get("templates") or {}
    names = raw.get("template") or []
    for name in reversed([names] if isinstance(names, str) else names):
        template = templates.get(name)
        if isinstance(template, dict) and template.get("name"):
            return str(template["name"])
    return None


def _tiktok_handle(urls: list[str]) -> str | None:
    for url in urls:
        match = _TIKTOK_HANDLE_RE.search(url)
        if match:
            return match.group(1)
    return None


def account_tiktok_handle(account_id: str, preferred_urls: list[str]) -> str | None:
    """TikTok handle from the given URLs, else from any sibling project of the account."""
    handle = _tiktok_handle(preferred_urls)
    if handle:
        return handle
    projects_dir = _projects_dir()
    if not projects_dir.is_dir():
        return None
    for project_dir in sorted(projects_dir.iterdir(), key=lambda d: d.name):
        if not project_dir.is_dir():
            continue
        project = _load_project_json(project_dir)
        if project.get("scheduled_account_id") != account_id:
            continue
        handle = _tiktok_handle(uploaded_tiktok_urls(project))
        if handle:
            return handle
    return None


def build_signature(project: dict, video_url: str | None) -> str | None:
    """'<account name> - @<tiktok handle>' for the project's scheduled account."""
    urls = [video_url] if video_url else []
    account_id = project.get("scheduled_account_id")
    if not account_id:
        handle = _tiktok_handle(urls)
        return f"@{handle}" if handle else None
    name = account_display_name(str(account_id)) or str(account_id)
    handle = account_tiktok_handle(str(account_id), urls)
    return f"{name} - @{handle}" if handle else name


def _latest_run_script(project_dir: Path) -> tuple[dict, str] | None:
    """Return (script_json, source_label) for the most recent automation run."""
    runs_dir = project_dir / RUNS_DIR_NAME
    if not runs_dir.is_dir():
        return None
    subdirs = [d for d in runs_dir.iterdir() if d.is_dir()]
    if not subdirs:
        return None
    latest = max(subdirs, key=lambda d: d.stat().st_mtime)
    script_path = latest / "script.json"
    if not script_path.exists():
        return None
    return json.loads(script_path.read_text(encoding="utf-8")), f"automation_run:{latest.name}"


def _fallback_script(project_dir: Path) -> tuple[dict, str] | None:
    fallback = project_dir / "new_script.json"
    if not fallback.exists():
        return None
    return json.loads(fallback.read_text(encoding="utf-8")), "project_root:new_script.json"


def resolve_script(project_id: str) -> tuple[dict, str]:
    """Resolve the saved script JSON for a project, or exit with a clear error."""
    _validate_project_id(project_id)
    project_dir = _projects_dir() / project_id
    if not project_dir.exists():
        raise SystemExit(
            f"Project {project_id!r} not found under {_projects_dir()}."
        )

    found = _latest_run_script(project_dir) or _fallback_script(project_dir)
    if found is None:
        raise SystemExit(
            f"No script saved for project {project_id!r}. "
            "Generate it in the /script phase first."
        )
    return found


def generation_link(project_id: str) -> str:
    """Build the project's generation link, honoring ATR_CEP_TRIGGER_URL_TEMPLATE."""
    template = os.getenv("ATR_CEP_TRIGGER_URL_TEMPLATE") or DEFAULT_LINK_TEMPLATE
    return template.format(project_id=project_id)


def _extract_js_array(text: str, var_name: str) -> str:
    """Return the balanced `[...]` literal following `var_name`, string-aware."""
    idx = text.find(var_name)
    if idx == -1:
        raise ValueError(f"{var_name!r} not found")
    start = text.find("[", idx)
    if start == -1:
        raise ValueError(f"No array after {var_name!r}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError(f"Unbalanced array after {var_name!r}")


def count_montage_cuts(project_dir: Path) -> int:
    """Count the cuts (scene clips) placed by the generated import_project.jsx."""
    jsx_path = project_dir / OUTPUT_DIR_NAME / JSX_FILENAME
    if not jsx_path.exists():
        raise SystemExit(
            f"Montage not generated yet: {jsx_path} is missing. "
            "Run the montage/export phase first."
        )
    scenes = json.loads(_extract_js_array(jsx_path.read_text(encoding="utf-8"), "var scenes"))
    return len(scenes)


def video_duration_seconds(project_dir: Path) -> float:
    """Duration of the post-auto-editor audio (tts_edited.wav), in seconds."""
    import wave

    audio_path = project_dir / OUTPUT_DIR_NAME / EDITED_AUDIO_FILENAME
    if not audio_path.exists():
        raise SystemExit(
            f"Auto-editor output missing: {audio_path}. "
            "Run the auto-editor step first."
        )
    with wave.open(str(audio_path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def format_duration_fr(seconds: float) -> str:
    """Human French duration, e.g. 143.4 -> '2min23', 47 -> '47s'."""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}min{secs:02d}" if minutes else f"{secs}s"


def format_cut_interval_fr(seconds: float, cuts: int) -> str:
    """Average seconds per cut, floored to the nearest 0.5s (e.g. 2.7->'2,5', 2.49->'2')."""
    import math

    interval = seconds / cuts if cuts else 0.0
    floored = math.floor(interval * 2) / 2
    if floored == int(floored):
        return str(int(floored))
    return f"{floored:.1f}".replace(".", ",")


def build_contest_message(
    project_dir: Path, video_url: str | None, signature: str | None
) -> str:
    """Fill the appeal message with the video URL, cut count, duration and account."""
    cuts = count_montage_cuts(project_dir)
    seconds = video_duration_seconds(project_dir)
    return CONTEST_MESSAGE_TEMPLATE.format(
        video_url=video_url or VIDEO_URL_PLACEHOLDER,
        signature=signature or SIGNATURE_PLACEHOLDER,
        cuts=cuts,
        duration=format_duration_fr(seconds),
        interval=format_cut_interval_fr(seconds, cuts),
    )


def script_to_text(script_json: dict) -> str:
    """Pack scene narration into a single block of text, in scene order."""
    scenes = script_json.get("scenes") or []
    ordered = sorted(scenes, key=lambda s: s.get("scene_index", 0))
    parts = [str(s.get("text", "")).strip() for s in ordered]
    return " ".join(part for part in parts if part)


def copy_to_clipboard(text: str) -> str:
    """Send text to the system clipboard. Returns the backend tool used."""
    backends: list[list[str]] = []
    if os.getenv("WAYLAND_DISPLAY"):
        backends.append(["wl-copy"])
    backends.append(["xclip", "-selection", "clipboard"])
    backends.append(["xsel", "--clipboard", "--input"])
    backends.append(["wl-copy"])  # last resort even without WAYLAND_DISPLAY

    tried: list[str] = []
    for cmd in backends:
        if cmd[0] in tried or shutil.which(cmd[0]) is None:
            continue
        tried.append(cmd[0])
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return cmd[0]
        except subprocess.CalledProcessError:
            continue

    raise SystemExit(
        "No working clipboard tool found. Install one of: wl-clipboard (wl-copy), "
        "xclip, or xsel.  e.g.  sudo pacman -S wl-clipboard"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy a project's saved script to the clipboard.")
    parser.add_argument(
        "project",
        help="Project id (12-char hex, e.g. 6f284c2cb490) or the uploaded TikTok video URL "
        "(e.g. https://www.tiktok.com/@animespm4/video/7668466849704709409)",
    )
    parser.add_argument("--json", action="store_true", help="Copy the raw script JSON instead of plain text (no link prepended).")
    parser.add_argument("--no-link", action="store_true", help="Do not prepend the generation link to the text output.")
    parser.add_argument("--stdout", action="store_true", help="Print to stdout instead of the clipboard.")
    parser.add_argument("--env-file", default=".env", help="Env file to load (default: .env)")
    args = parser.parse_args()

    load_dotenv(args.env_file)

    project_id = resolve_project_ref(args.project.strip())
    script_json, source = resolve_script(project_id)

    if args.json:
        payload = json.dumps(script_json, ensure_ascii=False, indent=2)
        kind = "raw JSON"
    else:
        text = script_to_text(script_json)
        if not text:
            raise SystemExit(
                f"Script for {project_id!r} has no scene text (source: {source})."
            )
        project_dir = _projects_dir() / project_id
        project = _load_project_json(project_dir)
        if "tiktok.com" in args.project:
            video_url = args.project.strip()
        else:
            known = uploaded_tiktok_urls(project)
            video_url = known[0] if known else None
        if video_url is None:
            print(
                f"Warning: no uploaded TikTok URL stored for {project_id}; "
                f"appeal message uses the {VIDEO_URL_PLACEHOLDER!r} placeholder.",
                file=sys.stderr,
            )
        signature = build_signature(project, video_url)
        if signature is None:
            print(
                f"Warning: no account tied to {project_id}; appeal message uses the "
                f"{SIGNATURE_PLACEHOLDER!r} placeholder.",
                file=sys.stderr,
            )
        blocks: list[str] = []
        if not args.no_link:
            blocks.append(generation_link(project_id))
        blocks.append(build_contest_message(project_dir, video_url, signature))
        blocks.append(text)
        payload = "\n------\n".join(blocks)
        kind = "appeal + script" if args.no_link else "link + appeal + script"

    if args.stdout:
        sys.stdout.write(payload)
        if not payload.endswith("\n"):
            sys.stdout.write("\n")
        return

    tool = copy_to_clipboard(payload)
    print(
        f"Copied {kind} for project {project_id} to clipboard "
        f"({len(payload)} chars, via {tool}, source: {source})."
    )


if __name__ == "__main__":
    main()
