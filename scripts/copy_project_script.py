#!/usr/bin/env python3
"""Copy a project's generated script to the clipboard.

Resolves the raw script saved during the `/script` phase for a given project id
and pushes it to the system clipboard. Mirrors the backend resolution order used
by `GET /script/latest-generation`:

  1. The most recently modified automation run -> script_automation_runs/<run>/script.json
  2. Fallback -> <project>/new_script.json

Fails loudly if no script has been saved yet (i.e. the project has not reached
the `/script` phase).

Usage (from repo root):

    pixi run python scripts/copy_project_script.py <project_id>
    pixi run python scripts/copy_project_script.py <project_id> --json
    pixi run python scripts/copy_project_script.py <project_id> --stdout

Clipboard backends (Arch Linux): wl-copy (Wayland), xclip or xsel (X11).
"""
from __future__ import annotations

import argparse
import json
import os
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

# TikTok demonetisation appeal message. {cuts}, {duration} and {interval} are
# filled with the real montage cut count, post-auto-editor video duration and
# average seconds-per-cut. Everything else is copied verbatim.
CONTEST_MESSAGE_TEMPLATE = """\
Bonjour,

Je suis résident français (Espace Économique Européen) et je demande formellement une révision humaine de la démonétisation appliquée à la vidéo https://www.tiktok.com/@animespm4/video/7644259243973037334 pour motif de "contenu de faible qualité".

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
AnimeSPM - @AnimeSPM4"""
# Mirrors backend/app/services/project_service.py
_VALID_ID = __import__("re").compile(r"[a-zA-Z0-9_-]+$")


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


def build_contest_message(project_dir: Path) -> str:
    """Fill the appeal message with the project's real cut count and duration."""
    cuts = count_montage_cuts(project_dir)
    seconds = video_duration_seconds(project_dir)
    return CONTEST_MESSAGE_TEMPLATE.format(
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
    parser.add_argument("project_id", help="Project id (12-char hex, e.g. 6f284c2cb490)")
    parser.add_argument("--json", action="store_true", help="Copy the raw script JSON instead of plain text (no link prepended).")
    parser.add_argument("--no-link", action="store_true", help="Do not prepend the generation link to the text output.")
    parser.add_argument("--stdout", action="store_true", help="Print to stdout instead of the clipboard.")
    parser.add_argument("--env-file", default=".env", help="Env file to load (default: .env)")
    args = parser.parse_args()

    load_dotenv(args.env_file)

    script_json, source = resolve_script(args.project_id)

    if args.json:
        payload = json.dumps(script_json, ensure_ascii=False, indent=2)
        kind = "raw JSON"
    else:
        text = script_to_text(script_json)
        if not text:
            raise SystemExit(
                f"Script for {args.project_id!r} has no scene text (source: {source})."
            )
        project_dir = _projects_dir() / args.project_id
        blocks: list[str] = []
        if not args.no_link:
            blocks.append(generation_link(args.project_id))
        blocks.append(build_contest_message(project_dir))
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
        f"Copied {kind} for project {args.project_id} to clipboard "
        f"({len(payload)} chars, via {tool}, source: {source})."
    )


if __name__ == "__main__":
    main()
