import { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Loader2, Copy, Check, ArrowRight, Upload, FileAudio } from 'lucide-react';
import { Button } from '@/components/ui';
import { useProjectStore, useSceneStore } from '@/stores';
import { api } from '@/api/client';
import type { Transcription } from '@/types';

const RESTRUCTURE_PROMPT_TEMPLATE = `Tu es un expert en réécriture de scripts pour des vidéos courtes sur les réseaux sociaux. 
Ta mission est de réécrire le script suivant en français tout en gardant le même sens et la même structure narrative.

RÈGLES IMPORTANTES:
1. Le nouveau script doit avoir une durée de narration TTS similaire pour chaque scène
2. Chaque scène doit garder approximativement la même durée de parole (±20%)
3. Le texte doit être naturel et engageant pour un format court (TikTok/Reels/Shorts)
4. Évite les phrases trop longues - vise 1-2 phrases par scène maximum
5. Garde le même ton et le même style narratif
6. Le script doit être suffisamment différent pour ne pas être un copier-coller

FORMAT DE SORTIE:
Tu dois retourner UNIQUEMENT un JSON valide avec la même structure que l'entrée, mais avec les textes réécrits.
Ne mets aucun texte avant ou après le JSON.

DONNÉES D'ENTRÉE:
`;

function generatePrompt(transcription: Transcription): string {
  const sceneData = transcription.scenes.map(scene => ({
    scene_index: scene.scene_index,
    text: scene.text,
    duration_seconds: (scene.end_time - scene.start_time).toFixed(2),
    estimated_word_count: scene.text.split(/\s+/).filter(w => w).length,
  }));

  return RESTRUCTURE_PROMPT_TEMPLATE + JSON.stringify({
    language: 'fr',
    scenes: sceneData,
  }, null, 2);
}

export function ScriptRestructurePage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { loadProject } = useProjectStore();
  const { loadScenes } = useSceneStore();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [transcription, setTranscription] = useState<Transcription | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // New script state
  const [newScriptJson, setNewScriptJson] = useState('');
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
        await loadProject(projectId);
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

    const prompt = generatePrompt(transcription);
    await navigator.clipboard.writeText(prompt);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [transcription]);

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
        if (typeof scene.scene_index !== 'number') {
          setJsonError('Each scene must have a numeric "scene_index"');
          return;
        }
        if (typeof scene.text !== 'string') {
          setJsonError('Each scene must have a "text" string');
          return;
        }
      }

      setJsonValid(true);
    } catch (e) {
      setJsonError(`Invalid JSON: ${(e as Error).message}`);
    }
  }, []);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      // Validate audio file
      if (!file.type.startsWith('audio/')) {
        setError('Please select an audio file');
        return;
      }
      setAudioFile(file);
      setError(null);
    }
  }, []);

  const handleContinue = useCallback(async () => {
    if (!projectId || !jsonValid || !audioFile) return;

    setUploading(true);
    setError(null);

    try {
      // Submit new script and audio
      const formData = new FormData();
      formData.append('script', newScriptJson);
      formData.append('audio', audioFile);

      const response = await fetch(`/api/projects/${projectId}/script/restructured`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({ detail: 'Upload failed' }));
        throw new Error(err.detail || 'Upload failed');
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

  const prompt = generatePrompt(transcription);
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

        {/* Step 1: Copy Prompt */}
        <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">Step 1: Copy Restructuration Prompt</h2>
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
            Use this prompt with an AI (Claude, ChatGPT, etc.) to generate a new French script.
            The AI will return JSON that you can paste below.
          </p>
          <div className="max-h-48 overflow-y-auto bg-[hsl(var(--muted))] rounded-lg p-3">
            <pre className="text-xs whitespace-pre-wrap font-mono">{prompt}</pre>
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
            Paste the JSON response from the AI here. It should contain the restructured script.
          </p>
          <textarea
            value={newScriptJson}
            onChange={(e) => handleJsonChange(e.target.value)}
            placeholder='{"language": "fr", "scenes": [...]}'
            className="w-full min-h-[200px] p-3 rounded-md border border-[hsl(var(--input))] bg-transparent font-mono text-sm resize-y"
          />
          {jsonError && (
            <p className="text-sm text-[hsl(var(--destructive))]">{jsonError}</p>
          )}
        </div>

        {/* Step 3: Upload Audio */}
        <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
          <h2 className="font-semibold">Step 3: Upload TTS Audio</h2>
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            Generate TTS audio from the new script (using ElevenLabs or similar) and upload it here.
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
              <div className={`h-3 w-3 rounded-full ${copied ? 'bg-green-500' : 'bg-[hsl(var(--border))]'}`} />
              Prompt copied
            </li>
            <li className="flex items-center gap-2">
              <div className={`h-3 w-3 rounded-full ${jsonValid ? 'bg-green-500' : 'bg-[hsl(var(--border))]'}`} />
              New script JSON validated
            </li>
            <li className="flex items-center gap-2">
              <div className={`h-3 w-3 rounded-full ${audioFile ? 'bg-green-500' : 'bg-[hsl(var(--border))]'}`} />
              TTS audio uploaded
            </li>
          </ul>
        </div>
      </div>
    </div>
  );
}
