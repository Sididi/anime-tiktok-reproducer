import { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Loader2, Play, ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui';
import { useProjectStore, useSceneStore } from '@/stores';
import { api } from '@/api/client';
import { formatTime } from '@/utils';
import type { Transcription } from '@/types';

interface TranscriptionProgress {
  status: string;
  progress: number;
  message: string;
  transcription?: Transcription;
  error: string | null;
}

const LANGUAGES = [
  { value: 'auto', label: 'Auto-detect' },
  { value: 'en', label: 'English' },
  { value: 'fr', label: 'Français' },
  { value: 'es', label: 'Español' },
];

export function TranscriptionPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { loadProject } = useProjectStore();
  const { scenes, loadScenes } = useSceneStore();

  const [transcription, setTranscription] = useState<Transcription | null>(null);
  const [loading, setLoading] = useState(true);
  const [transcribing, setTranscribing] = useState(false);
  const [progress, setProgress] = useState<TranscriptionProgress | null>(null);
  const [language, setLanguage] = useState('auto');
  const [editedTexts, setEditedTexts] = useState<Record<number, string>>({});
  const [error, setError] = useState<string | null>(null);

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
        if (loaded) {
          // Initialize edited texts
          const texts: Record<number, string> = {};
          loaded.scenes.forEach((s) => {
            texts[s.scene_index] = s.text;
          });
          setEditedTexts(texts);
        }
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [projectId, loadProject, loadScenes]);

  const handleStartTranscription = useCallback(async () => {
    if (!projectId) return;

    setTranscribing(true);
    setProgress({ status: 'starting', progress: 0, message: 'Starting...', error: null });
    setError(null);

    try {
      const response = await api.startTranscription(projectId, language);

      if (!response.ok) {
        throw new Error('Failed to start transcription');
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6)) as TranscriptionProgress;
              setProgress(data);

              if (data.status === 'complete' && data.transcription) {
                setTranscription(data.transcription);
                const texts: Record<number, string> = {};
                data.transcription.scenes.forEach((s) => {
                  texts[s.scene_index] = s.text;
                });
                setEditedTexts(texts);
              }

              if (data.status === 'error') {
                throw new Error(data.error || 'Transcription failed');
              }
            } catch (e) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setTranscribing(false);
    }
  }, [projectId, language]);

  const handleTextChange = (sceneIndex: number, text: string) => {
    setEditedTexts((prev) => ({ ...prev, [sceneIndex]: text }));
  };

  const handleSave = useCallback(async () => {
    if (!projectId || !transcription) return;

    try {
      const updates = Object.entries(editedTexts).map(([index, text]) => ({
        scene_index: parseInt(index, 10),
        text,
      }));
      const { transcription: updated } = await api.updateTranscription(projectId, updates);
      setTranscription(updated);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [projectId, transcription, editedTexts]);

  const handleConfirm = useCallback(async () => {
    if (!projectId) return;

    try {
      await handleSave();
      await api.confirmTranscription(projectId);
      navigate(`/project/${projectId}/script`);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [projectId, handleSave, navigate]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  return (
    <div className="min-h-screen p-4">
      <div className="max-w-4xl mx-auto space-y-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold">Transcription</h1>
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              {transcription
                ? `${transcription.scenes.length} scenes transcribed in ${transcription.language}`
                : 'Transcribe the TikTok audio'}
            </p>
          </div>
          {transcription && (
            <Button onClick={handleConfirm}>
              Continue
              <ArrowRight className="h-4 w-4 ml-2" />
            </Button>
          )}
        </header>

        {error && (
          <div className="p-3 bg-[hsl(var(--destructive))]/10 rounded-lg">
            <p className="text-sm text-[hsl(var(--destructive))]">{error}</p>
          </div>
        )}

        {!transcription && (
          <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
            <div>
              <label className="text-sm text-[hsl(var(--muted-foreground))] mb-2 block">
                Language
              </label>
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                disabled={transcribing}
                className="w-full h-10 px-3 rounded-md border border-[hsl(var(--input))] bg-transparent"
              >
                {LANGUAGES.map((lang) => (
                  <option key={lang.value} value={lang.value}>
                    {lang.label}
                  </option>
                ))}
              </select>
            </div>

            {progress && progress.status !== 'complete' && (
              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>{progress.message}</span>
                </div>
                <div className="h-2 bg-[hsl(var(--muted))] rounded-full overflow-hidden">
                  <div
                    className="h-full bg-[hsl(var(--primary))] transition-all duration-300"
                    style={{ width: `${progress.progress * 100}%` }}
                  />
                </div>
              </div>
            )}

            <Button
              onClick={handleStartTranscription}
              disabled={transcribing}
              className="w-full"
            >
              {transcribing ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  Transcribing...
                </>
              ) : (
                <>
                  <Play className="h-4 w-4 mr-2" />
                  Start Transcription
                </>
              )}
            </Button>
          </div>
        )}

        {transcription && (
          <div className="space-y-4">
            {scenes.map((scene) => {
              const sceneTranscription = transcription.scenes.find(
                (s) => s.scene_index === scene.index
              );
              if (!sceneTranscription) return null;

              return (
                <div key={scene.index} className="bg-[hsl(var(--card))] rounded-lg p-4 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">Scene {scene.index + 1}</span>
                    <span className="text-xs text-[hsl(var(--muted-foreground))]">
                      {formatTime(scene.start_time)} - {formatTime(scene.end_time)}
                    </span>
                  </div>
                  <textarea
                    value={editedTexts[scene.index] ?? sceneTranscription.text}
                    onChange={(e) => handleTextChange(scene.index, e.target.value)}
                    className="w-full min-h-[80px] p-2 rounded-md border border-[hsl(var(--input))] bg-transparent resize-y"
                    placeholder="No transcription for this scene"
                  />
                </div>
              );
            })}

            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={handleSave}>
                Save Changes
              </Button>
              <Button onClick={handleConfirm}>
                Confirm & Continue
                <ArrowRight className="h-4 w-4 ml-2" />
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
