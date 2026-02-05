import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Loader2,
  Copy,
  Check,
  ArrowRight,
  Upload,
  FileAudio,
} from "lucide-react";
import { Button } from "@/components/ui";
import { useProjectStore, useSceneStore } from "@/stores";
import { api } from "@/api/client";
import type { Transcription, Project } from "@/types";

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

### 1. LA "RÈGLE DU HOOK" (Scène 0 - Exception)

- La **Scène 0** est l'accroche virale. Tu dois conserver son **intention** et sa **structure percutante** le plus fidèlement possible.
- Ne la reformule pas inutilement, sauf pour l'anonymiser. Elle doit "claquer" immédiatement.

### 2. FLUIDITÉ & RESTRUCTURATION (Anti-Plagiat)

- **Ne traduis jamais phrase par phrase.** Lis le script par blocs de 2 ou 3 scènes pour comprendre le sens global.
- **Reformulation totalement :** Modifie la structure syntaxique pour éviter le plagiat. Utilise des verbes forts et des synonymes percutants.
- **Voix Active :** Pour le dynamisme TikTok, privilégie la voix active.
  - _Mauvais :_ "Il a été surpris par l'attaque."
  - _Bon :_ "L'attaque l'a surpris."
- **Objectif :** Le texte français doit sembler avoir été écrit nativement, pas traduit.

### 3. LA "RÈGLE DU CAFÉ" (Ton & Registre)

- **Ton :** Tu ne rédiges pas un livre, tu racontes une histoire à un pote dans un café. C'est du "Storytime".
- **Vocabulaire :** BANNIS le langage soutenu ("Néanmoins", "Cependant", "Demeurer", "Auparavant", "Impérial", "Dédain").
  - _Remplace par :_ "Mais", "Juste avant", "Incroyable", "Mépris".
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

### 5. SYNCHRONISATION & DENSITÉ (Le défi du temps)

Le français est naturellement plus long. Pour respecter la \`duration_seconds\` :

- **Condensation Intelligente :** Tu DOIS retirer les "mots vides", les adjectifs superflus ou les connecteurs lourds. Va droit au but.
- **Ancrage Visuel (Crucial) :** Même en condensant, **l'action montrée à l'écran doit être décrite dans la scène correspondante**. Si la scène 4 montre un smash, le mot "smash" (ou verbe associé) doit être dans le segment 4.
- **Débordement Autorisé :** Tu as le droit de finir une phrase sur la scène suivante (décalage de ±0.5s) si cela rend l'audio plus fluide, tant que l'action visuelle principale reste synchronisée.

### 6. FORMATTAGE AUDIO

- Le texte est destiné à un TTS (Text-To-Speech).
- Évite les phrases trop complexes à prononcer.
- Utilise une ponctuation rythmique (virgules, points) pour guider l'IA vocale.

# FORMAT DE SORTIE

- Retourne **UNIQUEMENT** un JSON valide.
- Garde **STRICTEMENT** la même structure (mêmes clés, mêmes \`duration_seconds\`, même nombre d'objets).
- Ne mets aucun markdown (pas de \`\`\`json), pas d'intro, pas de conclusion. Juste le raw JSON string.

DONNÉES D'ENTRÉE :
`;

// Multilingual prompt template (when target is not French)
const PROMPT_MULTILINGUAL_TEMPLATE = `# RÔLE

Tu es un Expert en Adaptation de Scripts Vidéo (Post-Synchro).
Ta mission : Réécrire un script de [SOURCE] vers [CIBLE] pour un format vidéo court (TikTok).
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

### 1. LA "RÈGLE DU HOOK" (Scène 0 - Exception)

- La **Scène 0** est l'accroche virale. Tu dois conserver son **intention** et sa **structure percutante** le plus fidèlement possible.
- Ne la reformule pas inutilement, sauf pour l'anonymiser. Elle doit "claquer" immédiatement dans la langue [CIBLE].

### 2. FLUIDITÉ & RESTRUCTURATION (Anti-Plagiat)

- **Ne traduis jamais phrase par phrase.** Lis le script par blocs de 2 ou 3 scènes pour comprendre le sens global.
- **Reformulation totale :** Modifie la structure syntaxique pour éviter le plagiat. Utilise des verbes forts et des synonymes percutants propres à la [CIBLE].
- **Voix Active :** Pour le dynamisme TikTok, privilégie systématiquement la voix active.
  - _Concept :_ Au lieu de dire "L'ennemi a été frappé par lui" (Passif), dis "Il a frappé l'ennemi" (Actif).
- **Objectif :** Le texte en [CIBLE] doit sembler avoir été écrit nativement, pas traduit.

### 3. LA "RÈGLE DU CAFÉ" (Ton & Registre)

- **Ton :** Tu ne rédiges pas un livre, tu racontes une histoire à un pote dans un café. C'est du "Storytime".
- **Vocabulaire :** BANNIS le langage soutenu (ex: "Néanmoins", "Cependant", "Demeurer", "Auparavant", "Impérial", "Dédain" pour FR).
  - _Remplace par :_ "Mais", "Juste avant", "Incroyable", "Mépris".
- **Les Transitions (Crucial) :** Remplace les connecteurs écrits (ex: "Par conséquent", "Ensuite" pour FR) par des connecteurs oraux fluides : **"Du coup", "Alors", "Et là", "Bref", "Au final".**
- **Structure :** Fais des phrases courtes et directes (Sujet + Verbe + Complément).
- **Interdit :** Pas de passé simple (sauf effet dramatique), pas d'inversion sujet-verbe complexe. Ça doit sonner parlé.

### 4. GESTION DES PRÉNOMS (Anonymisation)

- **Suppression Totale :** Aucun prénom ne doit apparaître.
- **Première Scène :** Remplace le nom par une description naturelle (ex: "La jeune prodige", "Le nouvel élève").
- **Ensuite :** Utilise STRICTEMENT des pronoms personnels appropriés à la grammaire de la [CIBLE] (ex: Il/Elle pour FR, He/She pour EN) pour 90% des cas. Ne réutilise une description ("La fille") que si l'ambiguïté est totale.
- **Interdit :** Les répétitions de démonstratifs.

### 5. SYNCHRONISATION & DENSITÉ (Le défi du temps)

Tu dois gérer le débit de parole selon la langue :

- **Facteur de Densité :** Si la [CIBLE] est naturellement plus longue/verbeuse que la [SOURCE] (ex: EN -> FR ou EN -> ES), tu DOIS **condenser intelligemment**. Retire les "mots vides", les adjectifs superflus ou les connecteurs lourds.
- **Ancrage Visuel (Crucial) :** Même en condensant, **l'action montrée à l'écran doit être décrite dans la scène correspondante**. Si la scène 4 montre un smash, le mot correspondant à "smash" en [CIBLE] doit être dans le segment 4.
- **Débordement Autorisé :** Tu as le droit de finir une phrase sur la scène suivante (décalage de ±0.5s) si cela rend l'audio plus fluide, tant que l'action visuelle principale reste synchronisée.

### 6. FORMATTAGE AUDIO

- Le texte est destiné à un TTS (Text-To-Speech).
- Évite les phrases trop complexes à prononcer.
- Utilise une ponctuation rythmique (virgules, points) pour guider l'IA vocale.

# FORMAT DE SORTIE

- Retourne **UNIQUEMENT** un JSON valide.
- Garde **STRICTEMENT** la même structure (mêmes clés, mêmes \`duration_seconds\`, même nombre d'objets).
- La clé \`language\` du JSON doit correspondre au code ISO de la [CIBLE].
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

  // For multilingual template, also replace [CIBLE]
  if (targetLang !== "fr") {
    prompt = prompt.replace(/\[CIBLE\]/g, targetLanguage);
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

export function ScriptRestructurePage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { project, loadProject } = useProjectStore();
  const { loadScenes } = useSceneStore();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [transcription, setTranscription] = useState<Transcription | null>(
    null,
  );
  const [targetLanguage, setTargetLanguage] = useState<TargetLanguage>("fr");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // New script state
  const [newScriptJson, setNewScriptJson] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [jsonValid, setJsonValid] = useState(false);

  // Audio file state
  const [audioFile, setAudioFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);

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
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [projectId, loadProject, loadScenes]);

  const handleCopyPrompt = useCallback(async () => {
    if (!transcription) return;

    const prompt = generatePrompt(transcription, project, targetLanguage);
    await navigator.clipboard.writeText(prompt);
    setCopied(true);
  }, [transcription, project, targetLanguage]);

  const handleJsonChange = useCallback((value: string) => {
    setNewScriptJson(value);
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

  const handleContinue = useCallback(async () => {
    if (!projectId || !jsonValid || !audioFile) return;

    setUploading(true);
    setError(null);

    try {
      // Submit new script and audio
      const formData = new FormData();
      formData.append("script", newScriptJson);
      formData.append("audio", audioFile);

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
  }, [projectId, jsonValid, audioFile, newScriptJson, navigate]);

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
  const canContinue = jsonValid && audioFile !== null;

  return (
    <div className="min-h-screen p-4">
      <div className="max-w-4xl mx-auto space-y-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold">Script Restructuration</h1>
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
              setCopied(false); // Reset copied state when language changes
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
              {copied ? (
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
            Use this prompt with an AI (Claude, ChatGPT, etc.) to generate a new{" "}
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
            {jsonValid && (
              <span className="text-sm text-green-500 flex items-center gap-1">
                <Check className="h-4 w-4" />
                Valid JSON
              </span>
            )}
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

        {/* Step 3: Upload Audio */}
        <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
          <h2 className="font-semibold">Step 3: Upload TTS Audio</h2>
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            Generate TTS audio from the new script (using ElevenLabs or similar)
            and upload it here.
          </p>

          <input
            ref={fileInputRef}
            type="file"
            accept="audio/*"
            onChange={handleFileSelect}
            className="hidden"
          />

          {audioFile ? (
            <div className="flex items-center gap-3 p-3 bg-[hsl(var(--muted))] rounded-lg">
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
            >
              <Upload className="h-6 w-6 mr-2" />
              Click to upload audio file
            </Button>
          )}
        </div>

        {/* Summary */}
        <div className="bg-[hsl(var(--muted))] rounded-lg p-4">
          <h3 className="font-medium mb-2">Checklist</h3>
          <ul className="space-y-1 text-sm">
            <li className="flex items-center gap-2">
              <div
                className={`h-3 w-3 rounded-full ${copied ? "bg-green-500" : "bg-[hsl(var(--border))]"}`}
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
                className={`h-3 w-3 rounded-full ${audioFile ? "bg-green-500" : "bg-[hsl(var(--border))]"}`}
              />
              TTS audio uploaded
            </li>
          </ul>
        </div>
      </div>
    </div>
  );
}
