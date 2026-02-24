import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Loader2,
  Copy,
  Check,
  ArrowRight,
  Upload,
  FileAudio,
  Files,
  FileAudio2,
  Pencil,
  ChevronDown,
  ChevronUp,
  FileText,
  Bot,
  Square,
  Play,
  Pause,
} from "lucide-react";
import { Button } from "@/components/ui";
import { useProjectStore, useSceneStore } from "@/stores";
import { api } from "@/api/client";
import type {
  Transcription,
  Project,
  PlatformMetadata,
  ScriptAutomationConfig,
  ScriptAutomationEvent,
  ScriptAutomationPart,
} from "@/types";
import { ScriptEditorModal, MetadataEditorModal } from "@/components/script";
import { readSSEStream } from "@/utils/sse";

// Upload mode types
type UploadMode = "single" | "multiple";

// Segment type for multi-file upload
interface AudioSegment {
  id: number;
  sceneIndices: number[];
  text: string;
  characterCount: number;
}

// French-only prompt template (when target is French)
const PROMPT_FR_TEMPLATE = `# RÔLE

Tu es un Expert en Adaptation de Scripts Vidéo (Post-Synchro).
Ta mission : Réécrire un script de [SOURCE] vers Français pour un format vidéo court (TikTok).
Le but est d'obtenir un texte **indétectable comme copie (anti-plagiat)**, fluide à l'oreille, et parfaitement synchronisé temporellement.

# CONTEXTE

Titre de l'œuvre : [OEUVRE]
_Instruction : Utilise ce titre pour comprendre le contexte et le vocabulaire spécifique (sport, magie, scifi...), mais NE CITE JAMAIS ce titre ni les noms des personnages dans le script final._

# DONNÉES D'ENTRÉE

Tu reçois un JSON contenant des scènes. Chaque scène possède :

- \`text\` : Le script original.
- \`duration_seconds\` : La durée stricte de la scène.
- \`estimated_word_count\` : Indication de la densité originale.

# RÈGLES D'EXÉCUTION (Priorité Absolue)

### 1. LA "RÈGLE DU HOOK" (Première phrase - Exception)

- La **première phrase** est l'accroche virale. Tu dois la **garder telle quelle** et la **traduire** le plus fidèlement possible. Fais une traduction contextuelle (plus naturel).

### 2. FLUIDITÉ & RESTRUCTURATION (Anti-Plagiat)

- **Ne traduis jamais phrase par phrase.** Lis le script par blocs pour comprendre le sens global et identifier les scènes (Règle 7).
- **Reformulation totalement :** Modifie la structure syntaxique pour éviter le plagiat. Utilise des verbes forts et des synonymes percutants.
- **Voix Active :** Pour le dynamisme TikTok, privilégie la voix active.
  - _Mauvais :_ "Il a été surpris par l'attaque."
  - _Bon :_ "L'attaque l'a surpris."
- **Objectif :** Le texte français doit sembler avoir été écrit nativement, pas traduit.

### 3. LA "RÈGLE DU CAFÉ" (Ton & Registre)

- **Ton :** Tu ne rédiges pas un livre, tu racontes une histoire à un pote dans un café. C'est du "Storytime".
- **Vocabulaire :** BANNIS le langage soutenu ("Néanmoins", "Cependant", "Demeurer", "Auparavant", "Impérial", "Dédain").
  - _Remplace par :_ "Mais", "Juste avant", "Incroyable", "Mépris".
- **Expressions Datées/Ringardes :** BANNIS les expressions idiomatiques vieillottes comme "Faire le pied de grue", "En mettre plein la vue", "Tomber des nues", "Prendre ses jambes à son cou".
  - _Remplace par du concret/visuel :_ Au lieu de "Faire le pied de grue", dis "Rester planté là". Au lieu de "10 points dans la vue", dis "10 points d'écart" ou "Se prendre 10 points".
- **Les Transitions (Crucial) :** Remplace les connecteurs écrits ("Par conséquent", "Ensuite") par des connecteurs oraux fluides : **"Du coup", "Alors", "Et là", "Bref", "Au final".**
- **Structure :** Fais des phrases courtes et directes (Sujet + Verbe + Complément).
- **Interdit :** Pas de passé simple (sauf effet dramatique), pas d'inversion sujet-verbe complexe. Ça doit sonner parlé.

### 4. GESTION DES PRÉNOMS (Anonymisation)

- **Suppression Totale :** Aucun prénom ne doit apparaître.
- **L'introduction :** À la première apparition, remplace le nom par une description naturelle (ex: "La jeune prodige", "Le nouvel élève").
- **Ensuite :** Utilise STRICTEMENT des pronoms (Elle, Il, Lui, Son) pour 90% des cas. Ne réutilise une description ("La fille") que si l'ambiguïté est totale.
- **La Règle de Clarté (IMPORTANT) :**
  - _Cas simple (Genres différents ou personnage seul) :_ Utilise massivement les pronoms (Il, Elle, Lui) pour la fluidité.
  - _Cas complexe (Même genre) :_ Si l'action implique deux hommes (ou deux femmes), **l'utilisation seule de "Il" est interdite** car elle crée la confusion. Tu dois alterner les pronoms avec des **désignations fonctionnelles** (ex: "L'agresseur", "La victime", "Le coach", "Son frère").
- **Critère de réussite :** On doit savoir INSTANTANÉMENT qui fait l'action, sans avoir l'image.

### 5. SYNCHRONISATION, DENSITÉ & FLEXIBILITÉ TEMPORELLE (Calcul Technique)

Le français est plus long, MAIS notre voix TTS parle vite (x1.15) et la vidéo est "élastique" (on peut la ralentir/accélérer au montage).

- **La Règle d'Or du débit :** Vise une moyenne de **3 à 4 mots par seconde** de \`duration_seconds\`.
  - *Exemple :* Si une scène dure 2.0s, tu as la place pour 6 à 8 mots.
- **Priorité à l'Impact :** Ne cherche pas à "remplir" le temps si ce n'est pas nécessaire. Une phrase courte et tranchante ("Il est mort.") est meilleure qu'une phrase longue, car on peut accélérer la vidéo (cut) massivement.
- **Gestion du débordement :** Tu as le droit de déborder légèrement de la durée théorique ou d'être plus court. Ce qui compte, c'est que le texte soit percutant.

### 6. STRUCTURE DE RÉTENTION

Si possible, chaque séquence (aggrégat de plans de coupe) doit suivre au moins une de ces logiques :

## Curiosity
Créer une attente :
- “Sauf que…”
- “Le problème, c’est que…”
- “Il ne le sait pas encore, mais…”

## Escalade
Chaque séquence doit :
- augmenter le danger
- ou augmenter l’enjeu
- ou révéler une info clé

## Payoff visuel
Quand une action arrive à l’écran :
- elle doit être annoncée
- puis livrée

### 7. PRINCIPE DE "MACRO-SÉQUENCE" & ANCRAGE VISUEL

Ton input JSON découpe la vidéo en "plans de coupe" (cuts) très courts. Ne traduis pas cut par cut, cela rendrait le texte robotique.

1. **Regroupement (Macro-Séquence) :** Identifie des groupes de 2 à 5 cuts qui forment une idée narrative complète. Écris ta phrase française sur l'ensemble de ce groupe pour qu'elle soit fluide.
2. **Redistribution :** Découpe ensuite cette phrase pour la répartir dans les objets JSON correspondants.
3. **L'Ancrage Visuel (IMPÉRATIF) :** C'est ta seule contrainte rigide lors de la redistribution.
   - Si la scène X montre une action spécifique (ex: un coup de poing), le mot correspondant ("frappe", "cogne") DOIT être dans l'objet JSON de la scène X.
   - *Méthode :* Écris l'histoire fluide, puis "épingle" les mots-clés sur les bons index temporels.

### 8. FORMATTAGE AUDIO

- Le texte est destiné à un TTS (Text-To-Speech).
- Utilise une ponctuation rythmique (virgule, point d'exclamation, point d'interrogation) pour guider l'IA vocale.

# FORMAT DE SORTIE

- Retourne **UNIQUEMENT** un JSON valide.
- Garde **STRICTEMENT** la même structure (mêmes clés, même nombre d'objets).
- Ne mets aucun markdown (pas de \`\`\`json), pas d'intro, pas de conclusion. Juste le raw JSON string.

DONNÉES D'ENTRÉE :
`;

// Multilingual prompt template (when target is not French)
const PROMPT_MULTILINGUAL_TEMPLATE = `# RÔLE

Tu es un Expert en Adaptation de Scripts Vidéo (Post-Synchro).
Ta mission : Réécrire un script de [SOURCE] vers [TARGET] pour un format vidéo court (TikTok/Reels).
Le but est d'obtenir un texte **indétectable comme copie (anti-plagiat)**, fluide à l'oreille, et parfaitement synchronisé temporellement.

# CONTEXTE

Titre de l'œuvre : [OEUVRE]
_Instruction : Utilise ce titre pour comprendre le contexte et le vocabulaire spécifique (sport, magie, scifi...), mais NE CITE JAMAIS ce titre ni les noms des personnages dans le script final._

# DONNÉES D'ENTRÉE

Tu reçois un JSON contenant des scènes. Chaque scène possède :

- \`text\` : Le script original en [SOURCE].
- \`duration_seconds\` : La durée stricte de la scène.
- \`estimated_word_count\` : Indication de la densité originale.

# RÈGLES D'EXÉCUTION (Priorité Absolue)

### 1. LA "RÈGLE DU HOOK" (Première phrase - Exception)

- La **première phrase** est l'accroche virale. Tu dois la **garder telle quelle** sur le fond mais la **traduire** en [TARGET] le plus fidèlement possible. Fais une traduction contextuelle (plus naturel).

### 2. FLUIDITÉ & RESTRUCTURATION (Anti-Plagiat)

- **Ne traduis jamais phrase par phrase.** Lis le script par blocs pour comprendre le sens global et identifier les scènes (Règle 7).
- **Reformulation totale :** Modifie la structure syntaxique pour éviter le calque de la langue [SOURCE]. Utilise des verbes forts et des synonymes percutants propres à la langue [TARGET].
- **Voix Active :** Pour le dynamisme TikTok, privilégie la voix active.
- **Objectif :** Le texte en [TARGET] doit sembler avoir été écrit nativement, pas traduit.

### 3. LA "RÈGLE DU CAFÉ" (Ton & Registre)

- **Ton :** Tu ne rédiges pas un livre, tu racontes une histoire à un pote dans un café. C'est du "Storytime".
- **Vocabulaire :** BANNIS le langage soutenu, académique ou littéraire de la langue [TARGET].
  - _Exemple de logique :_ Ne dis pas "Néanmoins" ou "Cependant", dis "Mais" ou "Pourtant" (utilise les équivalents oraux de [TARGET]).
- **Expressions Datées/Ringardes :** BANNIS les idiomes vieillots.
  - _Remplace par du concret/visuel :_ Utilise le langage courant et moderne parlé actuellement par les jeunes adultes natifs en [TARGET].
- **Les Transitions (Crucial) :** Remplace les connecteurs écrits par des connecteurs oraux fluides typiques de [TARGET] (équivalents de "Du coup", "Alors", "Bref", "Au final").
- **Structure :** Fais des phrases courtes et directes.
- **Interdit :** Pas de temps verbaux purement littéraires (comme le Passé Simple en français), sauf effet dramatique. Ça doit sonner parlé.

### 4. GESTION DES PRÉNOMS (Anonymisation)

- **Suppression Totale :** Aucun prénom ne doit apparaître.
- **L'introduction :** À la première apparition, remplace le nom par une description naturelle (ex: "La jeune prodige", "Le nouvel élève").
- **Ensuite :** Utilise STRICTEMENT des pronoms de la langue [TARGET] pour 90% des cas. Ne réutilise une description que si l'ambiguïté est totale.
- **La Règle de Clarté (IMPORTANT) :**
  - _Cas simple (Genres différents ou personnage seul) :_ Utilise massivement les pronoms pour la fluidité.
  - _Cas complexe (Même genre) :_ Si l'action implique deux personnages du même genre, l'utilisation seule du pronom est interdite car elle crée la confusion. Tu dois alterner les pronoms avec des **désignations fonctionnelles** (ex: "L'agresseur", "La victime", "Le coach", "Son frère").
- **Critère de réussite :** On doit savoir INSTANTANÉMENT qui fait l'action, sans avoir l'image.

### 5. SYNCHRONISATION, DENSITÉ & FLEXIBILITÉ TEMPORELLE

La langue [TARGET] peut avoir une densité syllabique différente de la langue [SOURCE].

- **La Règle d'Or du débit :** Vise une moyenne de **3 à 4 mots par seconde** de \`duration_seconds\` (à ajuster légèrement selon la rapidité naturelle de la langue [TARGET]).
  - *Exemple :* Si une scène dure 2.0s, tu as la place pour environ 6 à 8 mots.
- **Priorité à l'Impact :** Ne cherche pas à "remplir" le temps si ce n'est pas nécessaire. Une phrase courte et tranchante est meilleure qu'une phrase longue.
- **Gestion du débordement :** Tu as le droit de déborder légèrement de la durée théorique ou d'être plus court. Ce qui compte, c'est que le texte soit percutant.

### 6. STRUCTURE DE RÉTENTION

Si possible, chaque séquence (aggrégat de plans de coupe) doit suivre au moins une de ces logiques :

## Curiosity
Créer une attente (utilisant les formulations typiques de [TARGET] pour le suspense) :
- “Sauf que…”
- “Le problème, c’est que…”
- “Il ne le sait pas encore, mais…”

## Escalade
Chaque séquence doit :
- augmenter le danger
- ou augmenter l’enjeu
- ou révéler une info clé

## Payoff visuel
Quand une action arrive à l’écran :
- elle doit être annoncée
- puis livrée

### 7. PRINCIPE DE "MACRO-SÉQUENCE" & ANCRAGE VISUEL

Ton input JSON découpe la vidéo en "plans de coupe" (cuts) très courts. Ne traduis pas cut par cut, cela rendrait le texte robotique.

1. **Regroupement (Macro-Séquence) :** Identifie des groupes de 2 à 5 cuts qui forment une idée narrative complète. Écris ta phrase en [TARGET] sur l'ensemble de ce groupe pour qu'elle soit fluide.
2. **Redistribution :** Découpe ensuite cette phrase pour la répartir dans les objets JSON correspondants.
3. **L'Ancrage Visuel (IMPÉRATIF) :** C'est ta seule contrainte rigide lors de la redistribution.
   - Si la scène X montre une action spécifique (ex: un coup de poing), le mot correspondant en [TARGET] DOIT être dans l'objet JSON de la scène X.
   - *Méthode :* Écris l'histoire fluide, puis "épingle" les mots-clés sur les bons index temporels.

### 8. FORMATTAGE AUDIO

- Le texte est destiné à un TTS (Text-To-Speech) en langue [TARGET].
- Utilise une ponctuation rythmique (virgule, point d'exclamation, point d'interrogation) pour guider l'IA vocale.

# FORMAT DE SORTIE

- Retourne **UNIQUEMENT** un JSON valide.
- Garde **STRICTEMENT** la même structure (mêmes clés, même nombre d'objets).
- Change la valeur de la clé \`"language"\` pour le code ISO de [TARGET] (ex: "fr", "es", "de").
- Ne mets aucun markdown (pas de \`\`\`json), pas d'intro, pas de conclusion. Juste le raw JSON string.

DONNÉES D'ENTRÉE :
`;

// Language options for the selector
const LANGUAGE_OPTIONS = [
  { value: "fr", label: "Francais" },
  { value: "en", label: "Anglais" },
  { value: "es", label: "Espagnol" },
] as const;

// Display names for languages (used in prompts)
const LANGUAGE_DISPLAY_NAMES: Record<string, string> = {
  en: "Anglais",
  fr: "Francais",
  es: "Espagnol",
};

type TargetLanguage = "fr" | "en" | "es";

// Check if text ends with sentence-ending punctuation
function endsWithSentence(text: string): boolean {
  const trimmed = text.trim();
  return /[.!?…]["')\]]*$/.test(trimmed);
}

const ELEVENLABS_TARGET = 300;
const ELEVENLABS_MIN = 200;
const ELEVENLABS_MAX = 400;

function ensureSentenceEnd(text: string): string {
  const cleaned = text.trim();
  if (!cleaned) return cleaned;
  if (endsWithSentence(cleaned)) return cleaned;
  return `${cleaned}.`;
}

// Segment scenes into groups based on character limit for ElevenLabs
// Target: ~300 chars, preferred range 200-400, always ending on sentence boundaries.
function segmentScenes(
  scenes: Array<{ scene_index: number; text: string }>,
): AudioSegment[] {
  if (scenes.length === 0) {
    return [];
  }

  const segments: AudioSegment[] = [];
  let currentSegment: AudioSegment = {
    id: 1,
    sceneIndices: [],
    text: "",
    characterCount: 0,
  };

  for (let i = 0; i < scenes.length; i++) {
    const scene = scenes[i];
    const sceneText = scene.text.trim();
    if (!sceneText) {
      continue;
    }

    const mergedText = currentSegment.text
      ? `${currentSegment.text} ${sceneText}`
      : sceneText;
    currentSegment.sceneIndices.push(scene.scene_index);
    currentSegment.text = mergedText;
    currentSegment.characterCount = mergedText.length;

    const isLastScene = i === scenes.length - 1;
    const sentenceBoundary = endsWithSentence(currentSegment.text);
    if (!sentenceBoundary && !isLastScene) {
      continue;
    }

    if (isLastScene) {
      currentSegment.text = ensureSentenceEnd(currentSegment.text);
      currentSegment.characterCount = currentSegment.text.length;
      segments.push(currentSegment);
      currentSegment = {
        id: segments.length + 1,
        sceneIndices: [],
        text: "",
        characterCount: 0,
      };
      continue;
    }

    const nextSceneText = (scenes[i + 1]?.text || "").trim();
    const withNextLength = nextSceneText
      ? `${currentSegment.text} ${nextSceneText}`.length
      : currentSegment.characterCount;
    const closeNow =
      currentSegment.characterCount >= ELEVENLABS_MIN &&
      (currentSegment.characterCount > ELEVENLABS_MAX ||
        withNextLength > ELEVENLABS_MAX ||
        Math.abs(currentSegment.characterCount - ELEVENLABS_TARGET) <=
          Math.abs(withNextLength - ELEVENLABS_TARGET));

    if (closeNow) {
      currentSegment.text = ensureSentenceEnd(currentSegment.text);
      currentSegment.characterCount = currentSegment.text.length;
      segments.push(currentSegment);
      currentSegment = {
        id: segments.length + 1,
        sceneIndices: [],
        text: "",
        characterCount: 0,
      };
    }
  }

  if (currentSegment.sceneIndices.length > 0) {
    currentSegment.text = ensureSentenceEnd(currentSegment.text);
    currentSegment.characterCount = currentSegment.text.length;
    segments.push(currentSegment);
  }

  if (segments.length >= 2) {
    const last = segments[segments.length - 1];
    if (last.characterCount < ELEVENLABS_MIN) {
      const prev = segments[segments.length - 2];
      prev.text = ensureSentenceEnd(`${prev.text} ${last.text}`);
      prev.characterCount = prev.text.length;
      prev.sceneIndices = [...prev.sceneIndices, ...last.sceneIndices];
      segments.pop();
    }
  }

  for (let i = 0; i < segments.length; i++) {
    segments[i].id = i + 1;
  }

  return segments;
}

function generatePrompt(
  transcription: Transcription,
  project: Project | null,
  targetLang: TargetLanguage,
): string {
  // Choose the right template based on target language
  const template =
    targetLang === "fr" ? PROMPT_FR_TEMPLATE : PROMPT_MULTILINGUAL_TEMPLATE;

  // Source language from transcription
  const sourceLanguage =
    LANGUAGE_DISPLAY_NAMES[transcription.language] || transcription.language;
  const targetLanguage = LANGUAGE_DISPLAY_NAMES[targetLang];

  // Get anime name from project
  const animeName = project?.anime_name || "Inconnu";

  // Replace template variables
  let prompt = template
    .replace(/\[SOURCE\]/g, sourceLanguage)
    .replace(/\[OEUVRE\]/g, animeName);

  // For multilingual template, also replace [TARGET]
  if (targetLang !== "fr") {
    prompt = prompt.replace(/\[TARGET\]/g, targetLanguage);
  }

  // Build scene data for JSON
  const sceneData = transcription.scenes.map((scene) => ({
    scene_index: scene.scene_index,
    text: scene.text,
    duration_seconds: (scene.end_time - scene.start_time).toFixed(2),
    estimated_word_count: scene.text.split(/\s+/).filter((w) => w).length,
  }));

  // Append JSON data
  return (
    prompt +
    JSON.stringify(
      {
        language: targetLang,
        scenes: sceneData,
      },
      null,
      2,
    )
  );
}

function validateMetadataObject(
  payload: unknown,
): { valid: boolean; error: string | null } {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    return { valid: false, error: "Metadata JSON must be an object" };
  }
  const obj = payload as Record<string, unknown>;
  const expectedKeys = ["facebook", "instagram", "youtube", "tiktok"];
  const keys = Object.keys(obj);
  const missing = expectedKeys.filter((k) => !(k in obj));
  const extras = keys.filter((k) => !expectedKeys.includes(k));
  if (missing.length > 0) {
    return { valid: false, error: `Missing keys: ${missing.join(", ")}` };
  }
  if (extras.length > 0) {
    return { valid: false, error: `Unexpected keys: ${extras.join(", ")}` };
  }

  const asRecord = (value: unknown, label: string) => {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new Error(`${label} must be an object`);
    }
    return value as Record<string, unknown>;
  };
  const asString = (value: unknown, label: string) => {
    if (typeof value !== "string" || !value.trim()) {
      throw new Error(`${label} must be a non-empty string`);
    }
  };
  const asStringArray = (value: unknown, label: string) => {
    if (!Array.isArray(value) || value.length === 0) {
      throw new Error(`${label} must be a non-empty string array`);
    }
    for (const item of value) {
      if (typeof item !== "string" || !item.trim()) {
        throw new Error(`${label} entries must be non-empty strings`);
      }
    }
  };

  try {
    const facebook = asRecord(obj.facebook, "facebook");
    asString(facebook.title, "facebook.title");
    asString(facebook.description, "facebook.description");
    asStringArray(facebook.tags, "facebook.tags");

    const instagram = asRecord(obj.instagram, "instagram");
    asString(instagram.caption, "instagram.caption");

    const youtube = asRecord(obj.youtube, "youtube");
    asString(youtube.title, "youtube.title");
    asString(youtube.description, "youtube.description");
    asStringArray(youtube.tags, "youtube.tags");

    const tiktok = asRecord(obj.tiktok, "tiktok");
    asString(tiktok.description, "tiktok.description");
  } catch (err) {
    return { valid: false, error: (err as Error).message };
  }

  return { valid: true, error: null };
}

function createEmptyMetadata(): PlatformMetadata {
  return {
    facebook: {
      title: "",
      description: "",
      tags: [],
    },
    instagram: {
      caption: "",
    },
    youtube: {
      title: "",
      description: "",
      tags: [],
    },
    tiktok: {
      description: "",
    },
  };
}

function coerceMetadataForEditor(payload: unknown): PlatformMetadata {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    return createEmptyMetadata();
  }

  const obj = payload as Record<string, unknown>;
  const asRecord = (value: unknown): Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  const asString = (value: unknown): string =>
    typeof value === "string" ? value : "";
  const asStringArray = (value: unknown): string[] =>
    Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string")
      : [];

  const facebook = asRecord(obj.facebook);
  const instagram = asRecord(obj.instagram);
  const youtube = asRecord(obj.youtube);
  const tiktok = asRecord(obj.tiktok);

  return {
    facebook: {
      title: asString(facebook.title),
      description: asString(facebook.description),
      tags: asStringArray(facebook.tags),
    },
    instagram: {
      caption: asString(instagram.caption),
    },
    youtube: {
      title: asString(youtube.title),
      description: asString(youtube.description),
      tags: asStringArray(youtube.tags),
    },
    tiktok: {
      description: asString(tiktok.description),
    },
  };
}

export function ScriptRestructurePage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { project, loadProject } = useProjectStore();
  const { loadScenes } = useSceneStore();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const automationAbortRef = useRef<AbortController | null>(null);

  const [transcription, setTranscription] = useState<Transcription | null>(
    null,
  );
  const [targetLanguage, setTargetLanguage] = useState<TargetLanguage>("fr");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [promptCopied, setPromptCopied] = useState(false);
  const [promptCopiedIndicator, setPromptCopiedIndicator] = useState(false);

  // New script state
  const [newScriptJson, setNewScriptJson] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [jsonValid, setJsonValid] = useState(false);

  // Optional metadata state
  const [metadataExpanded, setMetadataExpanded] = useState(false);
  const [metadataJson, setMetadataJson] = useState("");
  const [metadataValid, setMetadataValid] = useState(false);
  const [metadataError, setMetadataError] = useState<string | null>(null);
  const [metadataCopiedPrompt, setMetadataCopiedPrompt] = useState(false);
  const [metadataDetected, setMetadataDetected] = useState(false);
  const [metadataPromptLoading, setMetadataPromptLoading] = useState(false);
  const [automationMetadataWarning, setAutomationMetadataWarning] = useState<
    string | null
  >(null);

  // Script automation state
  const [automationConfig, setAutomationConfig] =
    useState<ScriptAutomationConfig | null>(null);
  const [automationConfigError, setAutomationConfigError] = useState<
    string | null
  >(null);
  const [automationVoiceKey, setAutomationVoiceKey] = useState("");
  const [automationRunning, setAutomationRunning] = useState(false);
  const [automationStep, setAutomationStep] = useState<string | null>(null);
  const [automationMessage, setAutomationMessage] = useState<string | null>(
    null,
  );
  const [playingVoiceKey, setPlayingVoiceKey] = useState<string | null>(null);
  const voiceAudioRef = useRef<HTMLAudioElement | null>(null);

  // Audio file state
  const [uploadMode, setUploadMode] = useState<UploadMode>("multiple");
  const [audioFile, setAudioFile] = useState<File | null>(null);
  const [segmentFiles, setSegmentFiles] = useState<Map<number, File>>(
    new Map(),
  );
  const [requiredSegmentIds, setRequiredSegmentIds] = useState<number[] | null>(
    null,
  );
  const [copiedSegment, setCopiedSegment] = useState<number | null>(null);
  const [copiedFullScript, setCopiedFullScript] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [scriptEditorOpen, setScriptEditorOpen] = useState(false);
  const [metadataEditorOpen, setMetadataEditorOpen] = useState(false);

  // Parse scenes from JSON for segmentation
  const parsedScenes = useMemo(() => {
    if (!jsonValid || !newScriptJson) return null;
    try {
      const parsed = JSON.parse(newScriptJson);
      return parsed.scenes as Array<{ scene_index: number; text: string }>;
    } catch {
      return null;
    }
  }, [jsonValid, newScriptJson]);

  // Compute segments when JSON is valid
  const audioSegments = useMemo(() => {
    if (!parsedScenes) return [];
    return segmentScenes(parsedScenes);
  }, [parsedScenes]);

  const metadataEditorValue = useMemo<PlatformMetadata>(() => {
    if (!metadataJson.trim()) {
      return createEmptyMetadata();
    }

    try {
      return coerceMetadataForEditor(JSON.parse(metadataJson));
    } catch {
      return createEmptyMetadata();
    }
  }, [metadataJson]);

  // Load data
  useEffect(() => {
    if (!projectId) return;

    const loadData = async () => {
      setLoading(true);
      try {
        await loadProject(projectId); // Stores project in Zustand store
        await loadScenes(projectId);
        const { transcription: loaded } = await api.getTranscription(projectId);
        setTranscription(loaded);
        try {
          const automation = await api.getScriptAutomationConfig(projectId);
          setAutomationConfig(automation);
          setAutomationConfigError(null);
          setAutomationVoiceKey(
            automation.default_voice_key || automation.voices[0]?.key || "",
          );
        } catch (automationErr) {
          setAutomationConfig(null);
          setAutomationConfigError((automationErr as Error).message);
          setAutomationVoiceKey("");
        }
        const metadataResult = await api.getProjectMetadata(projectId);
        if (metadataResult.exists && metadataResult.metadata) {
          const pretty = JSON.stringify(metadataResult.metadata, null, 2);
          setMetadataJson(pretty);
          setMetadataValid(true);
          setMetadataDetected(true);
        }
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [projectId, loadProject, loadScenes]);

  useEffect(() => {
    return () => {
      automationAbortRef.current?.abort();
    };
  }, []);

  const handleCopyPrompt = useCallback(async () => {
    if (!transcription) return;

    const prompt = generatePrompt(transcription, project, targetLanguage);
    try {
      await navigator.clipboard.writeText(prompt);
      setPromptCopied(true);
      setPromptCopiedIndicator(true);
      setTimeout(() => setPromptCopiedIndicator(false), 1500);
    } catch {
      // Clipboard API may fail in insecure contexts
    }
  }, [transcription, project, targetLanguage]);

  const handleJsonChange = useCallback((value: string) => {
    setNewScriptJson(value);
    setRequiredSegmentIds(null);
    setJsonError(null);
    setJsonValid(false);

    if (!value.trim()) {
      return;
    }

    try {
      const parsed = JSON.parse(value);
      // Validate structure
      if (!parsed.scenes || !Array.isArray(parsed.scenes)) {
        setJsonError('JSON must contain a "scenes" array');
        return;
      }

      for (const scene of parsed.scenes) {
        if (typeof scene.scene_index !== "number") {
          setJsonError('Each scene must have a numeric "scene_index"');
          return;
        }
        if (typeof scene.text !== "string") {
          setJsonError('Each scene must have a "text" string');
          return;
        }
      }

      setJsonValid(true);
    } catch (e) {
      setJsonError(`Invalid JSON: ${(e as Error).message}`);
    }
  }, []);

  const handleMetadataJsonChange = useCallback((value: string) => {
    setMetadataJson(value);
    setMetadataError(null);
    setMetadataValid(false);
    if (!value.trim()) {
      setMetadataDetected(false);
      return;
    }
    try {
      const parsed = JSON.parse(value);
      const validation = validateMetadataObject(parsed);
      if (!validation.valid) {
        setMetadataError(validation.error);
        return;
      }
      setMetadataValid(true);
    } catch (err) {
      setMetadataError(`Invalid JSON: ${(err as Error).message}`);
    }
  }, []);

  const handleMetadataEditorSave = useCallback(
    (metadata: PlatformMetadata) => {
      const pretty = JSON.stringify(metadata, null, 2);
      handleMetadataJsonChange(pretty);
      setMetadataExpanded(true);
    },
    [handleMetadataJsonChange],
  );

  const handleCopyMetadataPrompt = useCallback(async () => {
    if (!projectId || !jsonValid || !newScriptJson) return;
    setMetadataPromptLoading(true);
    try {
      const { prompt } = await api.buildMetadataPrompt(projectId, {
        script: newScriptJson,
        target_language: targetLanguage,
      });
      await navigator.clipboard.writeText(prompt);
      setMetadataCopiedPrompt(true);
      setTimeout(() => setMetadataCopiedPrompt(false), 1500);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setMetadataPromptLoading(false);
    }
  }, [projectId, jsonValid, newScriptJson, targetLanguage]);

  const hydrateAutomationParts = useCallback(
    async (runId: string, parts: ScriptAutomationPart[]): Promise<Map<number, File>> => {
      if (!projectId) {
        throw new Error("Missing project id");
      }

      const map = new Map<number, File>();
      for (let i = 0; i < parts.length; i++) {
        const part = parts[i];
        const response = await api.downloadAutomationPart(projectId, runId, part.id);
        if (!response.ok) {
          throw new Error(`Failed to download audio part ${part.id}`);
        }

        const blob = await response.blob();
        const contentDisposition = response.headers.get("Content-Disposition");
        const fileNameMatch = contentDisposition?.match(/filename="?([^";]+)"?/i);
        const fallbackExt = blob.type.includes("wav") ? "wav" : "mp3";
        const fileName = fileNameMatch?.[1] || `part_${part.id}.${fallbackExt}`;
        const file = new File([blob], fileName, {
          type: blob.type || "audio/mpeg",
        });

        const parsedId = Number.parseInt(part.id, 10);
        const segmentId = Number.isFinite(parsedId) && parsedId > 0 ? parsedId : i + 1;
        map.set(segmentId, file);
      }
      return map;
    },
    [projectId],
  );

  const handleCancelAutomation = useCallback(() => {
    automationAbortRef.current?.abort();
    setAutomationRunning(false);
    setAutomationMessage("Automation cancelled");
  }, []);

  const playVoicePreview = useCallback(
    (url: string, voiceKey: string) => {
      if (voiceAudioRef.current) {
        voiceAudioRef.current.pause();
        voiceAudioRef.current = null;
      }
      if (playingVoiceKey === voiceKey) {
        setPlayingVoiceKey(null);
        return;
      }
      const audio = new Audio(url);
      voiceAudioRef.current = audio;
      setPlayingVoiceKey(voiceKey);
      audio.play().catch(() => {});
      audio.onended = () => {
        setPlayingVoiceKey(null);
        voiceAudioRef.current = null;
      };
    },
    [playingVoiceKey],
  );

  const handleAutomate = useCallback(async () => {
    if (!projectId || !automationConfig) return;
    if (!automationConfig.enabled) {
      setError("Automation is disabled on backend");
      return;
    }
    if (!automationVoiceKey) {
      setError("Please select a voice before starting automation");
      return;
    }

    setError(null);
    setAutomationMetadataWarning(null);
    setAutomationStep("starting");
    setAutomationMessage("Starting automation...");
    setAutomationRunning(true);
    setPromptCopied(true);

    const controller = new AbortController();
    automationAbortRef.current = controller;

    // Compute skip flags based on what's already filled
    const skipScript = jsonValid && newScriptJson.trim() !== "";
    const skipMetadata = metadataValid && metadataJson.trim() !== "";
    const skipTts =
      (uploadMode === "single" && audioFile !== null) ||
      (uploadMode === "multiple" && segmentFiles.size > 0);

    try {
      const response = await api.automateScript(
        projectId,
        {
          target_language: targetLanguage,
          voice_key: automationVoiceKey,
          existing_script_json: skipScript ? JSON.parse(newScriptJson) : undefined,
          skip_metadata: skipMetadata,
          skip_tts: skipTts,
        },
        controller.signal,
      );

      const finalEvent = await readSSEStream<ScriptAutomationEvent>(
        response,
        (event) => {
          setAutomationStep(event.event);
          setAutomationMessage(event.message || null);
          if (event.warning) {
            setAutomationMetadataWarning(event.warning);
          }
        },
        controller.signal,
      );

      if (!finalEvent) {
        if (controller.signal.aborted) return;
        throw new Error("Automation ended without completion event");
      }

      if (finalEvent.event !== "complete") {
        throw new Error(
          finalEvent.error || finalEvent.message || "Automation failed before completion",
        );
      }

      if (!finalEvent.script_json) {
        throw new Error("Automation response did not include script_json");
      }

      const prettyScript = JSON.stringify(finalEvent.script_json, null, 2);
      handleJsonChange(prettyScript);

      if (finalEvent.metadata_json) {
        const prettyMetadata = JSON.stringify(finalEvent.metadata_json, null, 2);
        handleMetadataJsonChange(prettyMetadata);
        setMetadataDetected(true);
        setMetadataExpanded(true);
      }

      if (finalEvent.metadata_warning) {
        setAutomationMetadataWarning(finalEvent.metadata_warning);
        setMetadataExpanded(true);
      }

      if (!finalEvent.run_id) {
        throw new Error("Automation response did not include run_id");
      }

      const parts = finalEvent.parts || [];
      if (parts.length > 0) {
        const files = await hydrateAutomationParts(finalEvent.run_id, parts);
        setUploadMode("multiple");
        setAudioFile(null);
        setSegmentFiles(files);
        setRequiredSegmentIds([...files.keys()].sort((a, b) => a - b));
        setAutomationMessage(
          `Automation complete (${parts.length} part${parts.length > 1 ? "s" : ""} loaded)`,
        );
      } else {
        setAutomationMessage("Automation complete (audio kept as-is)");
      }
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        return;
      }
      setError((err as Error).message);
    } finally {
      setAutomationRunning(false);
      automationAbortRef.current = null;
    }
  }, [
    projectId,
    automationConfig,
    automationVoiceKey,
    targetLanguage,
    jsonValid,
    newScriptJson,
    metadataValid,
    metadataJson,
    uploadMode,
    audioFile,
    segmentFiles,
    handleJsonChange,
    handleMetadataJsonChange,
    hydrateAutomationParts,
  ]);

  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) {
        // Validate audio file
        if (!file.type.startsWith("audio/")) {
          setError("Please select an audio file");
          return;
        }
        setAudioFile(file);
        setError(null);
      }
    },
    [],
  );

  const handleSegmentFileSelect = useCallback(
    (segmentId: number, e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) {
        if (!file.type.startsWith("audio/")) {
          setError("Please select an audio file");
          return;
        }
        setSegmentFiles((prev) => {
          const next = new Map(prev);
          next.set(segmentId, file);
          return next;
        });
        setError(null);
      }
    },
    [],
  );

  const handleCopySegment = useCallback(async (segment: AudioSegment) => {
    try {
      await navigator.clipboard.writeText(segment.text);
      setCopiedSegment(segment.id);
      setTimeout(() => setCopiedSegment(null), 2000);
    } catch {
      // Clipboard API may fail in insecure contexts
    }
  }, []);

  const handleCopyFullScript = useCallback(async () => {
    if (!parsedScenes) return;
    const fullText = parsedScenes.map((s) => s.text).join(" ");
    try {
      await navigator.clipboard.writeText(fullText);
      setCopiedFullScript(true);
      setTimeout(() => setCopiedFullScript(false), 2000);
    } catch {
      // Clipboard API may fail in insecure contexts
    }
  }, [parsedScenes]);

  const handleDrop = useCallback(
    (e: React.DragEvent, target: "single" | number) => {
      e.preventDefault();
      e.stopPropagation();
      const file = e.dataTransfer.files[0];
      if (!file) return;
      if (!file.type.startsWith("audio/")) {
        setError("Please drop an audio file");
        return;
      }
      if (target === "single") {
        setAudioFile(file);
      } else {
        setSegmentFiles((prev) => {
          const next = new Map(prev);
          next.set(target, file);
          return next;
        });
      }
      setError(null);
    },
    [],
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleContinue = useCallback(async () => {
    if (!projectId || !jsonValid) return;

    // Validate based on upload mode
    if (uploadMode === "single" && !audioFile) return;
    if (uploadMode === "multiple") {
      const expectedSegmentOrder =
        requiredSegmentIds ?? audioSegments.map((seg) => seg.id);
      const allSegmentsHaveFiles =
        expectedSegmentOrder.length > 0 &&
        expectedSegmentOrder.every((segmentId) => segmentFiles.has(segmentId));
      if (!allSegmentsHaveFiles) return;
    }

    setUploading(true);
    setError(null);

    try {
      const formData = new FormData();
      formData.append("script", newScriptJson);
      if (metadataValid && metadataJson.trim()) {
        formData.append("metadata_json", metadataJson);
      }

      if (uploadMode === "single") {
        formData.append("audio", audioFile!);
      } else {
        // Send multiple files in order
        const expectedSegmentOrder =
          requiredSegmentIds ?? audioSegments.map((seg) => seg.id);
        for (const segmentId of expectedSegmentOrder) {
          const file = segmentFiles.get(segmentId);
          if (file) {
            formData.append("audio_parts", file);
          }
        }
      }

      const response = await fetch(
        `/api/projects/${projectId}/script/restructured`,
        {
          method: "POST",
          body: formData,
        },
      );

      if (!response.ok) {
        const err = await response
          .json()
          .catch(() => ({ detail: "Upload failed" }));
        throw new Error(err.detail || "Upload failed");
      }

      navigate(`/project/${projectId}/processing`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setUploading(false);
    }
  }, [
    projectId,
    jsonValid,
    audioFile,
    newScriptJson,
    navigate,
    uploadMode,
    audioSegments,
    requiredSegmentIds,
    segmentFiles,
    metadataValid,
    metadataJson,
  ]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  if (!transcription) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-[hsl(var(--muted-foreground))]">
          No transcription found. Please complete the transcription step first.
        </p>
      </div>
    );
  }

  const prompt = generatePrompt(transcription, project, targetLanguage);

  const allFieldsFilled =
    jsonValid &&
    newScriptJson.trim() !== "" &&
    metadataValid &&
    metadataJson.trim() !== "" &&
    ((uploadMode === "single" && audioFile !== null) ||
      (uploadMode === "multiple" && segmentFiles.size > 0));

  const automationBlockedReason = automationConfigError
    ? `Automation config error: ${automationConfigError}`
    : !automationConfig
      ? "Loading automation config..."
      : !automationConfig.enabled
        ? "Automation disabled on backend"
        : !automationConfig.gemini.configured
          ? "Gemini API key missing on backend"
          : !automationConfig.elevenlabs.configured
            ? "ElevenLabs API key missing on backend"
            : automationConfig.voice_config_error
              ? automationConfig.voice_config_error
              : !automationVoiceKey
                ? "Select a voice to automate"
                : allFieldsFilled
                  ? "All fields already filled"
                  : null;
  const canRunAutomation = automationBlockedReason === null && !automationRunning;
  const expectedSegmentOrder =
    requiredSegmentIds ?? audioSegments.map((seg) => seg.id);
  const displaySegments =
    requiredSegmentIds && requiredSegmentIds.length !== audioSegments.length
      ? requiredSegmentIds.map((id) => ({
          id,
          sceneIndices: [id],
          text: "",
          characterCount: 0,
        }))
      : audioSegments;
  const canContinue =
    jsonValid &&
    (uploadMode === "single"
      ? audioFile !== null
      : expectedSegmentOrder.length > 0 &&
        expectedSegmentOrder.every((segmentId) => segmentFiles.has(segmentId)));
  const uploadedSegmentsCount = expectedSegmentOrder.filter((segmentId) =>
    segmentFiles.has(segmentId),
  ).length;
  const metadataDone = metadataValid || metadataDetected;

  return (
    <div className="min-h-screen p-4">
      <div className="max-w-6xl mx-auto space-y-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold flex items-center gap-2">
              Script Restructuration
              {metadataDetected && (
                <span className="inline-flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-full bg-green-500/15 text-green-500">
                  <Check className="h-3.5 w-3.5" />
                  Metadata detected
                </span>
              )}
            </h1>
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              Generate a new script and TTS audio for your video
            </p>
          </div>
          <Button onClick={handleContinue} disabled={!canContinue || uploading}>
            {uploading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                Processing...
              </>
            ) : (
              <>
                Continue
                <ArrowRight className="h-4 w-4 ml-2" />
              </>
            )}
          </Button>
        </header>

        {error && (
          <div className="p-3 bg-[hsl(var(--destructive))]/10 rounded-lg">
            <p className="text-sm text-[hsl(var(--destructive))]">{error}</p>
          </div>
        )}

        <div className="lg:grid lg:grid-cols-[minmax(0,1fr)_280px] lg:gap-6">
          <div className="space-y-6">
            {/* Language Selection */}
            <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
              <h2 className="font-semibold">Langue de sortie</h2>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Choisissez la langue cible pour le script restructuré.
              </p>
              <select
                value={targetLanguage}
                onChange={(e) => {
                  setTargetLanguage(e.target.value as TargetLanguage);
                  setPromptCopied(false);
                  setPromptCopiedIndicator(false);
                  setMetadataCopiedPrompt(false);
                }}
                className="w-full p-2 rounded-md border border-[hsl(var(--input))] bg-[hsl(var(--background))] text-sm"
              >
                {LANGUAGE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Step 1: Copy Prompt */}
            <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="font-semibold">
                  Step 1: Copy Restructuration Prompt
                </h2>
                <Button variant="outline" size="sm" onClick={handleCopyPrompt}>
                  {promptCopiedIndicator ? (
                    <>
                      <Check className="h-4 w-4 mr-2" />
                      Copied!
                    </>
                  ) : (
                    <>
                      <Copy className="h-4 w-4 mr-2" />
                      Copy Prompt
                    </>
                  )}
                </Button>
              </div>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Use this prompt with an AI (Claude, ChatGPT, etc.) to generate
                a new{" "}
                {LANGUAGE_OPTIONS.find((l) => l.value === targetLanguage)?.label}{" "}
                script. The AI will return JSON that you can paste below.
              </p>
              <div className="max-h-48 overflow-y-auto bg-[hsl(var(--muted))] rounded-lg p-3">
                <pre className="text-xs whitespace-pre-wrap font-mono">
                  {prompt}
                </pre>
              </div>
            </div>

            {/* Step 2: Paste New Script */}
            <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="font-semibold">Step 2: Paste New Script JSON</h2>
                <div className="flex items-center gap-2">
                  {jsonValid && (
                    <>
                      <span className="text-sm text-green-500 flex items-center gap-1">
                        <Check className="h-4 w-4" />
                        Valid JSON
                      </span>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setScriptEditorOpen(true)}
                      >
                        <Pencil className="h-4 w-4 mr-1.5" />
                        Edit Script
                      </Button>
                    </>
                  )}
                </div>
              </div>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Paste the JSON response from the AI here. It should contain the
                restructured script.
              </p>
              <textarea
                value={newScriptJson}
                onChange={(e) => handleJsonChange(e.target.value)}
                placeholder='{"language": "fr", "scenes": [...]}'
                className="w-full min-h-[200px] p-3 rounded-md border border-[hsl(var(--input))] bg-transparent font-mono text-sm resize-y"
              />
              {jsonError && (
                <p className="text-sm text-[hsl(var(--destructive))]">
                  {jsonError}
                </p>
              )}
            </div>

            {/* Optional metadata step */}
            <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
              <div className="flex items-center justify-between gap-2">
                <div className="space-y-1">
                  <h2 className="font-semibold flex items-center gap-2">
                    <FileText className="h-4 w-4" />
                    Optional: Generate Platform Metadata
                    {metadataDone && (
                      <span className="inline-flex items-center gap-1 text-xs font-medium px-2 py-1 rounded-full bg-green-500/15 text-green-500">
                        <Check className="h-3.5 w-3.5" />
                        Ready
                      </span>
                    )}
                  </h2>
                  <p className="text-sm text-[hsl(var(--muted-foreground))]">
                    Build JSON metadata for YouTube, Facebook, Instagram, and
                    TikTok.
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setMetadataExpanded(true);
                      setMetadataEditorOpen(true);
                    }}
                  >
                    <Pencil className="h-4 w-4 mr-1.5" />
                    Edit Script
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setMetadataExpanded((prev) => !prev)}
                  >
                    {metadataExpanded ? (
                      <>
                        Hide
                        <ChevronUp className="h-4 w-4 ml-2" />
                      </>
                    ) : (
                      <>
                        Show
                        <ChevronDown className="h-4 w-4 ml-2" />
                      </>
                    )}
                  </Button>
                </div>
              </div>

              {metadataExpanded && (
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <p className="text-sm text-[hsl(var(--muted-foreground))]">
                      Copy the metadata prompt, run it in your LLM, and paste
                      the JSON response.
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleCopyMetadataPrompt}
                      disabled={!jsonValid || metadataPromptLoading}
                    >
                      {metadataPromptLoading ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          Building...
                        </>
                      ) : metadataCopiedPrompt ? (
                        <>
                          <Check className="h-4 w-4 mr-2" />
                          Copied
                        </>
                      ) : (
                        <>
                          <Copy className="h-4 w-4 mr-2" />
                          Copy Metadata Prompt
                        </>
                      )}
                    </Button>
                  </div>

                  {!jsonValid && (
                    <div className="p-3 rounded-md bg-[hsl(var(--muted))] text-sm text-[hsl(var(--muted-foreground))]">
                      Validate script JSON in Step 2 first to enable metadata
                      prompt generation.
                    </div>
                  )}

                  <textarea
                    value={metadataJson}
                    onChange={(e) => handleMetadataJsonChange(e.target.value)}
                    placeholder='{"facebook": {...}, "instagram": {...}, "youtube": {...}, "tiktok": {...}}'
                    className="w-full min-h-[180px] p-3 rounded-md border border-[hsl(var(--input))] bg-transparent font-mono text-sm resize-y"
                  />
                  {metadataError && (
                    <p className="text-sm text-[hsl(var(--destructive))]">
                      {metadataError}
                    </p>
                  )}
                </div>
              )}
            </div>

            {/* Step 3: Upload Audio */}
            <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
              <h2 className="font-semibold">Step 3: Upload TTS Audio</h2>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Generate TTS audio from the new script (using ElevenLabs or
                similar) and upload it here.
              </p>

              {/* Upload Mode Selector */}
              <div className="flex items-center gap-2">
                <select
                  value={uploadMode}
                  onChange={(e) => {
                    setUploadMode(e.target.value as UploadMode);
                    setAudioFile(null);
                    setSegmentFiles(new Map());
                    setRequiredSegmentIds(null);
                  }}
                  className="p-2 rounded-md border border-[hsl(var(--input))] bg-[hsl(var(--background))] text-sm"
                >
                  <option value="multiple">Multiple files (Recommended)</option>
                  <option value="single">Single file</option>
                </select>
                <span className="text-xs text-[hsl(var(--muted-foreground))]">
                  {uploadMode === "multiple"
                    ? "Split into sentence-based parts (target ~300 chars)"
                    : "Upload one combined audio file"}
                </span>
              </div>

              <input
                ref={fileInputRef}
                type="file"
                accept="audio/*"
                onChange={handleFileSelect}
                className="hidden"
              />

              {uploadMode === "single" ? (
                // Single file upload (original behavior)
                <>
                  {jsonValid && parsedScenes && (
                    <div className="flex items-center justify-between p-2 bg-[hsl(var(--muted))] rounded-lg">
                      <span className="text-xs text-[hsl(var(--muted-foreground))] truncate flex-1 mx-2 italic">
                        "
                        {parsedScenes
                          .map((s) => s.text)
                          .join(" ")
                          .slice(0, 120)}
                        ..."
                      </span>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={handleCopyFullScript}
                        className="h-7 px-2 shrink-0"
                      >
                        {copiedFullScript ? (
                          <>
                            <Check className="h-3 w-3 mr-1" />
                            Copied
                          </>
                        ) : (
                          <>
                            <Copy className="h-3 w-3 mr-1" />
                            Copy text
                          </>
                        )}
                      </Button>
                    </div>
                  )}
                  {audioFile ? (
                    <div
                      className="flex items-center gap-3 p-3 bg-[hsl(var(--muted))] rounded-lg"
                      onDrop={(e) => handleDrop(e, "single")}
                      onDragOver={handleDragOver}
                    >
                      <FileAudio className="h-8 w-8 text-[hsl(var(--primary))]" />
                      <div className="flex-1 min-w-0">
                        <p className="font-medium truncate">{audioFile.name}</p>
                        <p className="text-xs text-[hsl(var(--muted-foreground))]">
                          {(audioFile.size / (1024 * 1024)).toFixed(2)} MB
                        </p>
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => fileInputRef.current?.click()}
                      >
                        Change
                      </Button>
                    </div>
                  ) : (
                    <Button
                      variant="outline"
                      className="w-full h-24 border-dashed"
                      onClick={() => fileInputRef.current?.click()}
                      onDrop={(e) => handleDrop(e, "single")}
                      onDragOver={handleDragOver}
                    >
                      <Upload className="h-6 w-6 mr-2" />
                      Drop or click to upload audio file
                    </Button>
                  )}
                </>
              ) : (
                // Multiple files upload
                <>
                  {!jsonValid ? (
                    <div className="p-4 bg-[hsl(var(--muted))] rounded-lg text-center">
                      <Files className="h-8 w-8 mx-auto mb-2 text-[hsl(var(--muted-foreground))]" />
                      <p className="text-sm text-[hsl(var(--muted-foreground))]">
                        Paste valid JSON in Step 2 to see audio segments
                      </p>
                    </div>
                  ) : expectedSegmentOrder.length === 0 ? (
                    <div className="p-4 bg-[hsl(var(--muted))] rounded-lg text-center">
                      <p className="text-sm text-[hsl(var(--muted-foreground))]">
                        No segments generated from script
                      </p>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      <div className="flex items-center justify-between text-sm">
                        <span className="text-[hsl(var(--muted-foreground))]">
                          {expectedSegmentOrder.length} segment
                          {expectedSegmentOrder.length > 1 ? "s" : ""} (target
                          200-400 chars, ideal ~300)
                        </span>
                        <span className="text-[hsl(var(--muted-foreground))]">
                          {uploadedSegmentsCount}/{expectedSegmentOrder.length} uploaded
                        </span>
                      </div>
                      {displaySegments.map((segment) => {
                        const file = segmentFiles.get(segment.id);
                        const inputId = `segment-file-${segment.id}`;
                        return (
                          <div
                            key={segment.id}
                            className="border border-[hsl(var(--border))] rounded-lg p-3 space-y-2"
                          >
                            <div className="flex items-center justify-between">
                              <div className="flex items-center gap-2">
                                <span className="font-medium text-sm">
                                  Part {segment.id}
                                </span>
                                <span className="text-xs px-2 py-0.5 bg-[hsl(var(--muted))] rounded">
                                  Scenes{" "}
                                  {segment.sceneIndices.length === 1
                                    ? segment.sceneIndices[0]
                                    : `${segment.sceneIndices[0]}-${segment.sceneIndices[segment.sceneIndices.length - 1]}`}
                                </span>
                                <span className="text-xs text-[hsl(var(--muted-foreground))]">
                                  {segment.characterCount} chars
                                </span>
                              </div>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => handleCopySegment(segment)}
                                className="h-7 px-2"
                              >
                                {copiedSegment === segment.id ? (
                                  <>
                                    <Check className="h-3 w-3 mr-1" />
                                    Copied
                                  </>
                                ) : (
                                  <>
                                    <Copy className="h-3 w-3 mr-1" />
                                    Copy text
                                  </>
                                )}
                              </Button>
                            </div>
                            <div className="text-xs text-[hsl(var(--muted-foreground))] line-clamp-2 italic">
                              "{segment.text.slice(0, 150)}
                              {segment.text.length > 150 ? "..." : ""}"
                            </div>
                            <input
                              id={inputId}
                              type="file"
                              accept="audio/*"
                              onChange={(e) =>
                                handleSegmentFileSelect(segment.id, e)
                              }
                              className="hidden"
                            />
                            {file ? (
                              <div
                                className="flex items-center gap-2 p-2 bg-[hsl(var(--muted))] rounded"
                                onDrop={(e) => handleDrop(e, segment.id)}
                                onDragOver={handleDragOver}
                              >
                                <FileAudio2 className="h-5 w-5 text-green-500" />
                                <span className="text-xs truncate flex-1">
                                  {file.name}
                                </span>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  className="h-6 px-2 text-xs"
                                  onClick={() =>
                                    document.getElementById(inputId)?.click()
                                  }
                                >
                                  Change
                                </Button>
                              </div>
                            ) : (
                              <Button
                                variant="outline"
                                size="sm"
                                className="w-full h-10 border-dashed"
                                onClick={() =>
                                  document.getElementById(inputId)?.click()
                                }
                                onDrop={(e) => handleDrop(e, segment.id)}
                                onDragOver={handleDragOver}
                              >
                                <Upload className="h-4 w-4 mr-2" />
                                Drop or upload Part {segment.id}
                              </Button>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              )}
            </div>
          </div>

          <aside className="mt-6 lg:mt-0">
            <div className="space-y-4 lg:sticky lg:top-4">
              <div className="bg-[hsl(var(--card))] rounded-lg p-4 space-y-4">
                <div className="flex items-center justify-between gap-3">
                  <h2 className="font-semibold flex items-center gap-2">
                    <Bot className="h-4 w-4" />
                    Automate
                  </h2>
                  {automationRunning ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleCancelAutomation}
                    >
                      <Square className="h-4 w-4 mr-2" />
                      Cancel
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      onClick={handleAutomate}
                      disabled={!canRunAutomation}
                    >
                      Automate
                    </Button>
                  )}
                </div>

                <p className="text-sm text-[hsl(var(--muted-foreground))]">
                  Préremplit script, metadata et audio via Gemini + ElevenLabs.
                </p>

                <div className="space-y-1">
                  <span className="text-xs text-[hsl(var(--muted-foreground))]">
                    Voice
                  </span>
                  <div className="space-y-1">
                    {(automationConfig?.voices || []).map((voice) => (
                      <div key={voice.key} className="flex items-center gap-2">
                        <label className="flex items-center gap-2 flex-1 cursor-pointer min-w-0">
                          <input
                            type="radio"
                            name="automation-voice"
                            value={voice.key}
                            checked={automationVoiceKey === voice.key}
                            onChange={() => setAutomationVoiceKey(voice.key)}
                            disabled={automationRunning}
                            className="shrink-0"
                          />
                          <span className="text-sm truncate">
                            {voice.display_name}
                          </span>
                        </label>
                        {voice.preview_url && (
                          <button
                            type="button"
                            onClick={() =>
                              playVoicePreview(voice.preview_url!, voice.key)
                            }
                            disabled={automationRunning}
                            className="shrink-0 p-1 rounded hover:bg-[hsl(var(--muted))] disabled:opacity-50"
                            title="Preview voice"
                          >
                            {playingVoiceKey === voice.key ? (
                              <Pause className="h-3 w-3" />
                            ) : (
                              <Play className="h-3 w-3" />
                            )}
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                </div>

                {automationMessage && (
                  <div className="text-sm p-3 rounded-md bg-[hsl(var(--muted))]">
                    <p className="font-medium">
                      {automationStep ? automationStep.replace("_", " ") : "automation"}
                    </p>
                    <p className="text-[hsl(var(--muted-foreground))]">
                      {automationMessage}
                    </p>
                  </div>
                )}

                {automationMetadataWarning && (
                  <div className="text-sm p-3 rounded-md bg-amber-500/10 text-amber-600">
                    {automationMetadataWarning}
                  </div>
                )}

                {automationBlockedReason && !automationRunning && (
                  <p className="text-xs text-[hsl(var(--muted-foreground))]">
                    {automationBlockedReason}
                  </p>
                )}
              </div>

              <div className="bg-[hsl(var(--muted))] rounded-lg p-4">
                <h3 className="font-medium mb-2">Checklist</h3>
                <ul className="space-y-1 text-sm">
                  <li className="flex items-center gap-2">
                    <div
                      className={`h-3 w-3 rounded-full ${promptCopied ? "bg-green-500" : "bg-[hsl(var(--border))]"}`}
                    />
                    Prompt copied
                  </li>
                  <li className="flex items-center gap-2">
                    <div
                      className={`h-3 w-3 rounded-full ${jsonValid ? "bg-green-500" : "bg-[hsl(var(--border))]"}`}
                    />
                    New script JSON validated
                  </li>
                  <li className="flex items-center gap-2">
                    <div
                      className={`h-3 w-3 rounded-full ${metadataDone ? "bg-green-500" : "bg-[hsl(var(--border))]"}`}
                    />
                    Optional metadata {metadataDone ? "ready" : "skipped"}
                  </li>
                  <li className="flex items-center gap-2">
                    <div
                      className={`h-3 w-3 rounded-full ${
                        uploadMode === "single"
                          ? audioFile
                            ? "bg-green-500"
                            : "bg-[hsl(var(--border))]"
                          : uploadedSegmentsCount === expectedSegmentOrder.length &&
                              expectedSegmentOrder.length > 0
                            ? "bg-green-500"
                            : "bg-[hsl(var(--border))]"
                      }`}
                    />
                    {uploadMode === "single"
                      ? "TTS audio uploaded"
                      : `TTS audio uploaded (${uploadedSegmentsCount}/${expectedSegmentOrder.length} parts)`}
                  </li>
                </ul>
              </div>
            </div>
          </aside>
        </div>
      </div>

      {/* Script Editor Modal */}
      {transcription && (
        <ScriptEditorModal
          isOpen={scriptEditorOpen}
          onClose={() => setScriptEditorOpen(false)}
          onSave={(updatedJson) => {
            handleJsonChange(updatedJson);
            setScriptEditorOpen(false);
          }}
          scenesJson={newScriptJson}
          transcription={transcription}
          targetLanguage={targetLanguage}
        />
      )}

      <MetadataEditorModal
        isOpen={metadataEditorOpen}
        onClose={() => setMetadataEditorOpen(false)}
        metadata={metadataEditorValue}
        onSave={handleMetadataEditorSave}
      />
    </div>
  );
}
