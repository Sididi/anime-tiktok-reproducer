#!/usr/bin/env python3
from __future__ import annotations

import difflib
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = REPO_ROOT / "working_premiere_script.jsx"
TEMPLATE_PATH = (
    REPO_ROOT / "backend" / "app" / "services" / "templates" / "premiere_import_project_v77.jsx"
)


def _normalize(text: str) -> str:
    out = text.replace("\r\n", "\n").replace("\r", "\n")

    # Dynamic runtime values injected by backend.
    out = re.sub(r"var scenes = \[[\s\S]*?\];", "var scenes = <SCENES>;", out, count=1)
    out = re.sub(r"var SOURCE_FPS_NUM = \d+;", "var SOURCE_FPS_NUM = <SOURCE_FPS_NUM>;", out, count=1)
    out = re.sub(r"var SOURCE_FPS_DEN = \d+;", "var SOURCE_FPS_DEN = <SOURCE_FPS_DEN>;", out, count=1)
    out = re.sub(r'var MUSIC_FILENAME = "[^"]*";', 'var MUSIC_FILENAME = "<MUSIC_FILENAME>";', out, count=1)
    out = re.sub(r"var MUSIC_GAIN_DB = -?\d+(?:\.\d+)?;", "var MUSIC_GAIN_DB = <MUSIC_GAIN_DB>;", out, count=1)
    out = re.sub(
        r'var SUBTITLE_SRT_PATH = ROOT_DIR \+ "[^"]*";',
        'var SUBTITLE_SRT_PATH = ROOT_DIR + "/<SUBTITLE_SRT_PATH>";',
        out,
        count=1,
    )

    # Allowed contract override: root /subtitles.
    out = re.sub(
        r"\* - Loads pre-generated subtitle MOGRT files from [^\n]+\.",
        "* - Loads pre-generated subtitle MOGRT files from <SUBTITLE_MOGRT_DIR>.",
        out,
        count=1,
    )
    out = re.sub(
        r"var SUBTITLE_MOGRT_DIR = [^;]+;",
        "var SUBTITLE_MOGRT_DIR = <SUBTITLE_MOGRT_DIR>;",
        out,
        count=1,
    )
    out = re.sub(
        r"var KNOWN_MEDIA_EXTENSIONS = \{[\s\S]*?\};",
        "",
        out,
        count=1,
    )

    # Allowed cleanName and optional-music hardening divergence.
    out = re.sub(
        r"function (?:stripKnownExtension|normalizeNameKey)\(name\)[\s\S]*?(?=\n  function cacheProjectItemByName\()",
        "function <NAME_NORMALIZATION_BLOCK>() {}\n",
        out,
        count=1,
    )
    out = re.sub(
        r"function isItemNameMatch\(itemName, nameRef\)[\s\S]*?(?=\n  function isTrackItemMatch\()",
        "function <ITEM_MATCH_BLOCK>() {}\n",
        out,
        count=1,
    )
    out = re.sub(
        r"function findProjectItem\(name\)[\s\S]*?(?=\n  // ========================================================================\n  // 3\. MAIN LOGIC)",
        "function <CLIP_RESOLUTION_BLOCK>() {}\n",
        out,
        count=1,
    )
    out = re.sub(
        r"cacheProjectItemByName\(itemName,\s*item\);\n\s*cacheProjectItemByName\([^;]+\);",
        "cacheProjectItemByName(itemName, item);\n    cacheProjectItemByName(<NORMALIZED_ITEM_NAME>, item);",
        out,
        count=1,
    )
    out = re.sub(
        r"preloadNames\[TITLE_OVERLAY_FILENAME\] = true;\n\s*(?:if \(trimSpaces\(MUSIC_FILENAME\) !== \"\"\) \{\n\s*preloadNames\[MUSIC_FILENAME\] = true;\n\s*\}|preloadNames\[MUSIC_FILENAME\] = true;)",
        "preloadNames[TITLE_OVERLAY_FILENAME] = true;\n    preloadNames[MUSIC_FILENAME] = <OPTIONAL_MUSIC_PRELOAD>;",
        out,
        count=1,
    )
    out = re.sub(
        r"var nameCleaner = function \(n\) \{[\s\S]*?\}; // Helper",
        "var nameCleaner = <NAME_CLEANER>; // Helper",
        out,
        count=1,
    )
    out = re.sub(
        r"var ttsNameNoExt = [\s\S]*?(?=\n\n    // --- APPLY VIDEO PRESETS ---)",
        "var <TTS_AND_MUSIC_BLOCK> = true;",
        out,
        count=1,
    )
    out = re.sub(r"nm = nm \? [^;]+ : \"\";", "nm = <STRIP_NAME>(nm);", out)
    out = re.sub(
        r"var cleanNameRef = nameRef \? [^;]+ : \"\";",
        "var cleanNameRef = <STRIP_NAME>(nameRef);",
        out,
    )
    out = re.sub(r"var filenameNoExt = [^;]+;", "var filenameNoExt = <STRIP_NAME>(filename);", out)
    out = re.sub(r"var clipNameNoExt = [^;]+;", "var clipNameNoExt = <STRIP_NAME>(clipName);", out)

    # Cosmetic-only spacing from template injection.
    out = re.sub(
        r"(var LUMETRI_LOOK_PATH_CACHE = \{\};)\n[ \t]*\n+",
        r"\1\n",
        out,
    )
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def main() -> int:
    if not CANONICAL_PATH.exists():
        print(f"Missing canonical file: {CANONICAL_PATH}")
        return 2
    if not TEMPLATE_PATH.exists():
        print(f"Missing template file: {TEMPLATE_PATH}")
        return 2

    canonical = _normalize(CANONICAL_PATH.read_text(encoding="utf-8"))
    template = _normalize(TEMPLATE_PATH.read_text(encoding="utf-8"))
    if canonical == template:
        print("Premiere JSX parity check: OK")
        return 0

    print("Premiere JSX parity check: MISMATCH")
    diff = difflib.unified_diff(
        canonical.splitlines(),
        template.splitlines(),
        fromfile=str(CANONICAL_PATH),
        tofile=str(TEMPLATE_PATH),
        lineterm="",
    )
    for line in diff:
        print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
