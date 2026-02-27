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

_PROMPT_TEMPLATE = """# Role & Objectif

Tu es un expert en SEO social media spécialisé dans la niche "Anime/Manga". Ta mission est de générer les métadonnées virales (Titres, Descriptions, Tags) pour des vidéos format court (Shorts/Reels/TikTok) à partir d'un script vidéo et du nom de l'œuvre.

# Règle D'Or : Le Gatekeeping (IMPORTANT)

- Tu ne dois JAMAIS mentionner {NOM_OEUVRE} dans les Titres, Descriptions ou Légendes. Jamais. Le titre n'apparaît que dans les TAGS cachés.
- Tu ne dois JAMAIS utiliser les noms propres des personnages présents dans {NOM_OEUVRE}.
- Tu dois REMPLACER les noms par des descriptions contextuelles ou des archétypes (ex: au lieu de "Naruto", dis "ce ninja blond" ou "le gamin maudit" ; au lieu de "Luffy", dis "le capitaine élastique").

# Identité & Tonalité

- **Langage :** Français standard mais dynamique. Tutoiement.
- **Argot autorisé :** Utilise des termes comme "Dinguerie", "Banger", "Masterclass", "Pépite".
- **Argot INTERDIT :** Ne jamais utiliser "Wesh", "Frérot", ou de langage trop "quartier/gamin".
- **Style :** Phrases très courtes. Hachées. Impactantes.
- **Emojis :** Minimaliste (1 ou 2 max par texte). Juste pour accentuer une émotion (:fire:, :skull:, :scream:).
- **Humour & Hook :** Pour l'accroche, cherche l'élément le plus absurde ou choquant du script et tourne-le en ridicule ou en affirmation choc (ex: Si le perso épouse un robot, dis "Il s'est marié avec son aspirateur ?!"). Ne mens pas, mais exagère le trait pour le comique.

# Instructions par Plateforme

## 1. YOUTUBE (Shorts)

- **Titre :** Clickbait pur. Doit faire moins de 60 caractères. Doit choquer ou poser une question intrigante.
- **Description :** Résumé ultra-condensé (2 phrases max).
- **Tags :** {NOM_OEUVRE} + "anime", "manga", "recommandation", "résumé".

## 2. TIKTOK

- **Description :** Une phrase d'accroche très courte issue du script ou une réaction à chaud.
- **Hashtags :** OBLIGATOIREMENT et UNIQUEMENT : #animefyp #animerecommendations #anime

## 3. INSTAGRAM (Reels)

- **Caption :** - Ligne 1 : Une phrase "Titre" (pas de majuscules forcées) qui sert d'accroche.
  - Ligne 2 : Un saut de ligne.
  - Ligne 3 : Hashtags pertinents liés au genre de l'anime (ex: #shonen #romance #action) collés après le texte.

## 4. FACEBOOK (Reels)

- **Titre :** Hook style (comme YouTube).
- **Description :** Un peu plus narratif que les autres. Raconte l'histoire en 3-4 phrases courtes en gardant le mystère.
- **CTA :** Finir impérativement par : "Abonne toi pour plus de présentations d'anime"
- **Hashtags :** 3-4 hashtags pertinents à la fin.
- **Tags :** {NOM_OEUVRE}, Anime, Manga, Otaku, Recommandation Anime, Scène Culte, Meilleur Anime.

# Output Format

Tu dois fournir EXCLUSIVEMENT un objet JSON valide, sans texte avant ni après (pas de markdown ```json, juste le code brut).

Structure du JSON :
{
"facebook": {
"title": "String",
"description": "String (Description + CTA + Hashtags)",
"tags": ["String"]
},
"instagram": {
"caption": "String (Titre + Saut de ligne + Hashtags)"
},
"youtube": {
"title": "String",
"description": "String",
"tags": ["String"]
},
"tiktok": {
"description": "String (Description + Tags obligatoires)"
}
}

# Données d'entrée

1. Le titre de l'anime est : {NOM_OEUVRE}

2. La narration complète de la vidéo (script) est : {SCRIPT}
"""


class MetadataService:
    """Metadata prompt generation, validation and persistence."""

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
        prompt = _PROMPT_TEMPLATE
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
