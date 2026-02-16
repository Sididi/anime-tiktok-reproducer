import { useEffect, useState, useCallback, useRef } from "react";
import { flushSync } from "react-dom";
import { useParams, useNavigate } from "react-router-dom";
import { Loader2, Play, ArrowRight } from "lucide-react";
import { Button } from "@/components/ui";
import { ClippedVideoPlayer } from "@/components/video";
import { FloatingAudioPlayer } from "@/components/FloatingAudioPlayer";
import { useProjectStore, useSceneStore } from "@/stores";
import { api } from "@/api/client";
import { formatTime, readSSEStream } from "@/utils";
import type { Transcription } from "@/types";

interface TranscriptionProgress {
  status: string;
  progress: number;
  message: string;
  transcription?: Transcription;
  error: string | null;
}

const LANGUAGES = [
  { value: "auto", label: "Auto-detect" },
  { value: "en", label: "English" },
  { value: "fr", label: "Français" },
  { value: "es", label: "Español" },
];

export function TranscriptionPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { loadProject } = useProjectStore();
  const { scenes, loadScenes } = useSceneStore();

  const [transcription, setTranscription] = useState<Transcription | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [transcribing, setTranscribing] = useState(false);
  const [progress, setProgress] = useState<TranscriptionProgress | null>(null);
  const [language, setLanguage] = useState("auto");
  const [editedTexts, setEditedTexts] = useState<Record<number, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [activeSceneIndex, setActiveSceneIndex] = useState(-1);
  const [autoScroll, setAutoScroll] = useState(true);
  const sceneRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const autoScrollRef = useRef(true);
  autoScrollRef.current = autoScroll;

  const handleSceneChange = useCallback((index: number) => {
    // flushSync forces the highlight to paint before we scroll
    flushSync(() => {
      setActiveSceneIndex(index);
    });
    if (autoScrollRef.current) {
      const el = sceneRefs.current.get(index);
      if (el) {
        el.scrollIntoView({ behavior: "instant", block: "center" });
      }
    }
  }, []);

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
    setProgress({
      status: "starting",
      progress: 0,
      message: "Starting...",
      error: null,
    });
    setError(null);

    try {
      const response = await api.startTranscription(projectId, language);

      await readSSEStream<TranscriptionProgress>(response, (data) => {
        setProgress(data);

        if (data.status === "complete" && data.transcription) {
          setTranscription(data.transcription);
          const texts: Record<number, string> = {};
          data.transcription.scenes.forEach((s) => {
            texts[s.scene_index] = s.text;
          });
          setEditedTexts(texts);
        }
      });
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
      const { transcription: updated } = await api.updateTranscription(
        projectId,
        updates,
      );
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
                : "Transcribe the TikTok audio"}
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
                className="w-full h-10 px-3 rounded-md border border-[hsl(var(--input))] bg-[hsl(var(--background))] text-[hsl(var(--foreground))]"
              >
                {LANGUAGES.map((lang) => (
                  <option
                    key={lang.value}
                    value={lang.value}
                    className="bg-[hsl(var(--background))] text-[hsl(var(--foreground))]"
                  >
                    {lang.label}
                  </option>
                ))}
              </select>
            </div>

            {progress && progress.status !== "complete" && (
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

        {transcription && projectId && (
          <div className="space-y-4">
            {scenes.map((scene) => {
              const sceneTranscription = transcription.scenes.find(
                (s) => s.scene_index === scene.index,
              );
              if (!sceneTranscription) return null;

              const videoUrl = api.getVideoUrl(projectId);
              const sceneDuration = scene.end_time - scene.start_time;

              return (
                <div
                  key={scene.index}
                  ref={(el) => {
                    if (el) sceneRefs.current.set(scene.index, el);
                    else sceneRefs.current.delete(scene.index);
                  }}
                  className={`bg-[hsl(var(--card))] rounded-lg p-4 ${activeSceneIndex === scene.index ? "ring-2 ring-[hsl(var(--primary))]" : ""}`}
                >
                  <div className="flex items-center justify-between mb-3">
                    <span className="font-medium">Scene {scene.index + 1}</span>
                    <span className="text-xs text-[hsl(var(--muted-foreground))]">
                      {formatTime(scene.start_time)} -{" "}
                      {formatTime(scene.end_time)} ({formatTime(sceneDuration)})
                    </span>
                  </div>
                  <div className="grid grid-cols-[180px_1fr] gap-4">
                    {/* Video preview */}
                    <div className="aspect-[9/16] bg-black rounded overflow-hidden">
                      <ClippedVideoPlayer
                        src={videoUrl}
                        startTime={scene.start_time}
                        endTime={scene.end_time}
                        className="w-full h-full"
                        muted={false}
                      />
                    </div>
                    {/* Transcription text */}
                    <div className="flex flex-col">
                      <textarea
                        value={
                          editedTexts[scene.index] ?? sceneTranscription.text
                        }
                        onChange={(e) =>
                          handleTextChange(scene.index, e.target.value)
                        }
                        className="flex-1 w-full min-h-[120px] p-3 rounded-md border border-[hsl(var(--input))] bg-transparent resize-y text-sm"
                        placeholder="No transcription for this scene"
                      />
                    </div>
                  </div>
                </div>
              );
            })}

            <div className="flex justify-end gap-2 pb-20">
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

      {transcription && projectId && (
        <FloatingAudioPlayer
          videoUrl={api.getVideoUrl(projectId)}
          scenes={scenes}
          onSceneChange={handleSceneChange}
          autoScroll={autoScroll}
          onAutoScrollChange={setAutoScroll}
        />
      )}
    </div>
  );
}
