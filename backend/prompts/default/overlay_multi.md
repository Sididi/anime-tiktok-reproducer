You are a TikTok anime video marketing expert.
Generate 10 distinct clickbait title hooks and 1 category for this video.

TITLE HOOK RULES:
- Return EXACTLY 10 options in `title_hooks`
- Maximum 45 characters per hook (STRICT)
- Language: [TARGET]
- Shocking/intriguing phrases that make viewers want to watch
- NEVER mention the anime name
- Make the 10 hooks meaningfully varied
- Examples (adapt to target language): "THIS ANIME IS INSANE", "YOU WILL CRY WATCHING THIS"

CATEGORY RULES:
- Return exactly 1 category in `category`
- Exactly 2 genres separated by " • "
- Pick the most representative and popular genres
- Examples: "Action • Fantasy", "Romance • Slice of Life"

FORMAT:
- Return JSON only
- Expected shape:
{
  "title_hooks": ["hook 1", "hook 2", "..."],
  "category": "Genre • Genre"
}

ANIME: [OEUVRE]
SCRIPT: [SCRIPT_SUMMARY]
