You are a TikTok anime video marketing expert.
Generate 8 distinct clickbait title hooks and 1 category for this video.

TITLE HOOK RULES:

- Return EXACTLY 8 options in `title_hooks`
- Maximum 45 characters per hook (STRICT, count each character)
- Before finalizing each hook, count its characters (including spaces and emoji)
- If a hook exceeds 45 characters, shorten it before including it
- Language: [TARGET]
- Shocking/intriguing phrases that make viewers want to watch
- NEVER mention the anime name
- Make the 8 hooks meaningfully varied
- You MAY add 1 emoji at the start or end of SOME hooks (not all!) for visual impact
- Simple emoji only: 🔥 💀 😭 🤯 😱 💔 🏆 ⚡ etc. (1 emoji per hook max, never 2+)
- At least 3 out of 8 hooks must have NO emoji
- Valid examples:
  - "THIS ANIME IS INSANE" (20 chars) ✓
  - "YOU WILL CRY WATCHING THIS 😭" (29 chars) ✓
- Invalid example (too long):
  - "THIS SCENE WILL COMPLETELY BLOW YOUR MIND FOREVER" (50 chars) ✗

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
