import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..models import VideoMetadataPayload
from .project_service import ProjectService


_LANGUAGE_DISPLAY = {
    "fr": "Français",
    "en": "English",
    "es": "Español",
}


class MetadataService:
    """Metadata prompt generation, validation and persistence."""

    @classmethod
    def _prompt_template_path(cls) -> Path:
        # Repository root /prompt metadata.md
        return Path(__file__).resolve().parents[3] / "prompt metadata.md"

    @classmethod
    def _load_prompt_template(cls) -> str:
        path = cls._prompt_template_path()
        if not path.exists():
            raise FileNotFoundError(f"Metadata prompt template not found: {path}")
        return path.read_text(encoding="utf-8")

    @classmethod
    def _language_block(cls, target_language: str) -> str:
        display = _LANGUAGE_DISPLAY.get(target_language, target_language)
        return (
            "\n\n# Consigne supplémentaire (langue cible)\n\n"
            f"- Toutes les valeurs textuelles de sortie doivent être écrites en {display}.\n"
            "- Ne mélange pas les langues.\n"
            "- Garde strictement le même format JSON demandé.\n"
        )

    @classmethod
    def build_prompt(
        cls,
        anime_name: str,
        script_text: str,
        target_language: str = "fr",
    ) -> str:
        prompt = cls._load_prompt_template()
        prompt = prompt.replace("{NOM_OEUVRE}", anime_name).replace("{SCRIPT}", script_text)
        if target_language != "fr":
            prompt += cls._language_block(target_language)
        return prompt

    @classmethod
    def build_prompt_from_script_json(
        cls,
        anime_name: str,
        script_json: str,
        target_language: str = "fr",
    ) -> str:
        payload = json.loads(script_json)
        scenes = payload.get("scenes", [])
        script_text = " ".join(
            scene.get("text", "").strip()
            for scene in scenes
            if isinstance(scene, dict) and isinstance(scene.get("text"), str)
        ).strip()
        return cls.build_prompt(anime_name=anime_name, script_text=script_text, target_language=target_language)

    @classmethod
    def validate_payload(cls, payload: dict[str, Any]) -> VideoMetadataPayload:
        return VideoMetadataPayload.model_validate(payload)

    @classmethod
    def validate_json_string(cls, raw_json: str) -> VideoMetadataPayload:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid metadata JSON: {exc}") from exc

        try:
            return cls.validate_payload(parsed)
        except ValidationError as exc:
            raise ValueError(f"Invalid metadata schema: {exc}") from exc

    @classmethod
    def load(cls, project_id: str) -> VideoMetadataPayload | None:
        path = ProjectService.get_metadata_file(project_id)
        if not path.exists():
            return None
        return cls.validate_json_string(path.read_text(encoding="utf-8"))

    @classmethod
    def save(cls, project_id: str, payload: VideoMetadataPayload) -> tuple[Path, Path]:
        json_path = ProjectService.get_metadata_file(project_id)
        html_path = ProjectService.get_metadata_html_file(project_id)
        json_path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        html_path.write_text(cls.render_html(payload), encoding="utf-8")
        return json_path, html_path

    @classmethod
    def render_html(cls, payload: VideoMetadataPayload) -> str:
        data = payload.model_dump()
        encoded = json.dumps(data).replace("</", "<\\/")
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Video Metadata</title>
  <style>
    :root {{
      --bg: #0d1117;
      --card: #161b22;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #2f81f7;
      --border: #30363d;
      --ok: #3fb950;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background: radial-gradient(circle at top, #1f2937, var(--bg));
      color: var(--text);
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 14px;
      font-size: 1.6rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 14px;
    }}
    .card {{
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--card);
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .line {{
      background: #0b1220;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 0.92rem;
    }}
    .row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .label {{
      color: var(--muted);
      font-size: 0.86rem;
    }}
    button {{
      border: 0;
      border-radius: 7px;
      background: var(--accent);
      color: white;
      padding: 7px 11px;
      cursor: pointer;
      font-size: 0.82rem;
    }}
    .ok {{
      color: var(--ok);
      font-size: 0.78rem;
      min-height: 1em;
    }}
  </style>
</head>
<body>
  <h1>Metadata Export</h1>
  <div class="grid" id="grid"></div>
  <script>
    const data = {encoded};
    const sections = [
      {{
        title: "YouTube",
        fields: [
          ["Title", data.youtube.title],
          ["Description", data.youtube.description],
          ["Tags", data.youtube.tags.join(", ")],
        ],
      }},
      {{
        title: "TikTok",
        fields: [["Description", data.tiktok.description]],
      }},
      {{
        title: "Instagram",
        fields: [["Caption", data.instagram.caption]],
      }},
      {{
        title: "Facebook",
        fields: [
          ["Title", data.facebook.title],
          ["Description", data.facebook.description],
          ["Tags", data.facebook.tags.join(", ")],
        ],
      }},
    ];

    function copyText(text, target) {{
      navigator.clipboard.writeText(text).then(() => {{
        target.textContent = "Copied";
        setTimeout(() => (target.textContent = ""), 1200);
      }});
    }}

    const root = document.getElementById("grid");
    sections.forEach((section) => {{
      const card = document.createElement("article");
      card.className = "card";
      const h2 = document.createElement("h2");
      h2.textContent = section.title;
      card.appendChild(h2);
      section.fields.forEach(([label, value]) => {{
        const block = document.createElement("div");
        const row = document.createElement("div");
        row.className = "row";
        const span = document.createElement("span");
        span.className = "label";
        span.textContent = label;
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Copy";
        row.appendChild(span);
        row.appendChild(button);
        const line = document.createElement("div");
        line.className = "line";
        line.textContent = String(value);
        const ok = document.createElement("div");
        ok.className = "ok";
        button.addEventListener("click", () => copyText(String(value), ok));
        block.appendChild(row);
        block.appendChild(line);
        block.appendChild(ok);
        card.appendChild(block);
      }});
      root.appendChild(card);
    }});
  </script>
</body>
</html>
"""
