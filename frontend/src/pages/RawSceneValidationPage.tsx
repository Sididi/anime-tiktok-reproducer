import { useEffect, useState, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Loader2, ArrowRight, Volume2, VolumeX, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui";
import { ClippedVideoPlayer } from "@/components/video";
import { api } from "@/api/client";
import { formatTime } from "@/utils";
import type {
  Transcription,
  RawSceneDetectionResult,
  SceneTranscription,
} from "@/types";

interface SceneValidationState {
  is_raw: boolean;
  text: string;
}

export function RawSceneValidationPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detection, setDetection] = useState<RawSceneDetectionResult | null>(
    null,
  );
  const [transcription, setTranscription] = useState<Transcription | null>(
    null,
  );
  const [validations, setValidations] = useState<
    Record<number, SceneValidationState>
  >({});

  useEffect(() => {
    if (!projectId) return;

    const loadData = async () => {
      setLoading(true);
      try {
        const data = await api.getRawScenes(projectId);
        setDetection(data.detection);
        setTranscription(data.transcription);

        if (data.transcription && data.detection) {
          const rawIndices = new Set(
            data.detection.candidates.map((c) => c.scene_index),
          );
          const initial: Record<number, SceneValidationState> = {};
          for (const scene of data.transcription.scenes) {
            initial[scene.scene_index] = {
              is_raw: rawIndices.has(scene.scene_index),
              text: scene.text,
            };
          }
          setValidations(initial);
        }
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [projectId]);

  const toggleRaw = useCallback((sceneIndex: number) => {
    setValidations((prev) => ({
      ...prev,
      [sceneIndex]: {
        ...prev[sceneIndex],
        is_raw: !prev[sceneIndex]?.is_raw,
      },
    }));
  }, []);

  const handleTextChange = useCallback((sceneIndex: number, text: string) => {
    setValidations((prev) => ({
      ...prev,
      [sceneIndex]: {
        ...prev[sceneIndex],
        text,
      },
    }));
  }, []);

  const handleReset = useCallback(async () => {
    if (!projectId) return;

    setSaving(true);
    setError(null);

    try {
      await api.resetRawScenes(projectId);
      // Reload data after reset
      const data = await api.getRawScenes(projectId);
      setDetection(data.detection);
      setTranscription(data.transcription);

      if (data.transcription && data.detection) {
        const rawIndices = new Set(
          data.detection.candidates.map((c) => c.scene_index),
        );
        const initial: Record<number, SceneValidationState> = {};
        for (const scene of data.transcription.scenes) {
          initial[scene.scene_index] = {
            is_raw: rawIndices.has(scene.scene_index),
            text: scene.text,
          };
        }
        setValidations(initial);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [projectId]);

  const handleConfirm = useCallback(async () => {
    if (!projectId || !transcription) return;

    setSaving(true);
    setError(null);

    try {
      // Send validations for all scenes that were detected as raw
      const rawCandidateIndices = new Set(
        detection?.candidates.map((c) => c.scene_index) ?? [],
      );

      const sceneValidations = Object.entries(validations)
        .filter(([idx]) => rawCandidateIndices.has(Number(idx)))
        .map(([idx, state]) => ({
          scene_index: Number(idx),
          is_raw: state.is_raw,
          text: state.text || undefined,
        }));

      if (sceneValidations.length > 0) {
        const result = await api.validateRawScenes(
          projectId,
          sceneValidations,
        );
        setTranscription(result.transcription);
      }

      // Also send text edits for non-raw scenes
      const textUpdates = Object.entries(validations)
        .filter(
          ([idx]) =>
            !rawCandidateIndices.has(Number(idx)) ||
            !validations[Number(idx)]?.is_raw,
        )
        .filter(([, state]) => state.text)
        .map(([idx, state]) => ({
          scene_index: Number(idx),
          text: state.text,
        }));

      if (textUpdates.length > 0) {
        await api.updateTranscription(projectId, textUpdates);
      }

      await api.confirmRawScenes(projectId);
      navigate(`/project/${projectId}/script`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [projectId, transcription, detection, validations, navigate]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  if (!transcription || !detection) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-[hsl(var(--muted-foreground))]">
          No raw scene data found.
        </p>
      </div>
    );
  }

  const rawCandidateIndices = new Set(
    detection.candidates.map((c) => c.scene_index),
  );
  const rawCount = Object.values(validations).filter((v) => v.is_raw).length;

  return (
    <div className="min-h-screen p-4">
      <div className="max-w-4xl mx-auto space-y-6">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold">Raw Scene Validation</h1>
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              {detection.candidates.length} raw scene
              {detection.candidates.length !== 1 ? "s" : ""} detected
              {detection.speaker_count > 0 &&
                ` · ${detection.speaker_count} speakers found`}
              {rawCount > 0 && ` · ${rawCount} marked as raw`}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={handleReset} disabled={saving}>
              <RotateCcw className="h-4 w-4 mr-2" />
              Reset
            </Button>
            <Button onClick={handleConfirm} disabled={saving}>
              {saving ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  Saving...
                </>
              ) : (
                <>
                  Confirm & Continue
                  <ArrowRight className="h-4 w-4 ml-2" />
                </>
              )}
            </Button>
          </div>
        </header>

        {error && (
          <div className="p-3 bg-[hsl(var(--destructive))]/10 rounded-lg">
            <p className="text-sm text-[hsl(var(--destructive))]">{error}</p>
          </div>
        )}

        <div className="space-y-4">
          {transcription.scenes
            .filter((scene) => rawCandidateIndices.has(scene.scene_index))
            .map((scene) => (
              <SceneCard
                key={scene.scene_index}
                scene={scene}
                projectId={projectId!}
                validation={validations[scene.scene_index]}
                onToggleRaw={() => toggleRaw(scene.scene_index)}
                onTextChange={(text) =>
                  handleTextChange(scene.scene_index, text)
                }
              />
            ))}
        </div>

        <div className="flex justify-end pb-20">
          <Button onClick={handleConfirm} disabled={saving}>
            {saving ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                Saving...
              </>
            ) : (
              <>
                Confirm & Continue
                <ArrowRight className="h-4 w-4 ml-2" />
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SceneCard({
  scene,
  projectId,
  validation,
  onToggleRaw,
  onTextChange,
}: {
  scene: SceneTranscription;
  projectId: string;
  validation: SceneValidationState | undefined;
  onToggleRaw: () => void;
  onTextChange: (text: string) => void;
}) {
  const videoUrl = api.getVideoUrl(projectId);
  const isRaw = validation?.is_raw ?? scene.is_raw;
  const sceneDuration = scene.end_time - scene.start_time;

  return (
    <div
      className={`bg-[hsl(var(--card))] rounded-lg p-4 ${
        isRaw
          ? "ring-2 ring-amber-500/50"
          : "ring-2 ring-green-500/50"
      }`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="font-medium">Scene {scene.scene_index + 1}</span>
          <span
            className={`text-xs px-2 py-0.5 rounded-full font-medium ${
              isRaw
                ? "bg-amber-500/20 text-amber-400"
                : "bg-green-500/20 text-green-400"
            }`}
          >
            {isRaw ? (
              <span className="flex items-center gap-1">
                <Volume2 className="h-3 w-3" />
                RAW
              </span>
            ) : (
              <span className="flex items-center gap-1">
                <VolumeX className="h-3 w-3" />
                TTS
              </span>
            )}
          </span>
        </div>
        <span className="text-xs text-[hsl(var(--muted-foreground))]">
          {formatTime(scene.start_time)} - {formatTime(scene.end_time)} (
          {formatTime(sceneDuration)})
        </span>
      </div>

      <div className="grid grid-cols-[180px_1fr] gap-4">
        <div className="aspect-[9/16] bg-black rounded overflow-hidden">
          <ClippedVideoPlayer
            src={videoUrl}
            startTime={scene.start_time}
            endTime={scene.end_time}
            className="w-full h-full"
            muted={false}
          />
        </div>

        <div className="flex flex-col gap-3">
          <textarea
            value={validation?.text ?? scene.text}
            onChange={(e) => onTextChange(e.target.value)}
            className="flex-1 w-full min-h-[120px] p-3 rounded-md border border-[hsl(var(--input))] bg-transparent resize-y text-sm"
            placeholder={
              isRaw
                ? "Raw scene — no TTS text"
                : "No transcription for this scene"
            }
            disabled={isRaw}
          />

          <div className="flex gap-2">
            <Button
              variant={isRaw ? "default" : "outline"}
              size="sm"
              onClick={isRaw ? undefined : onToggleRaw}
              className={isRaw ? "pointer-events-none" : ""}
            >
              <Volume2 className="h-3.5 w-3.5 mr-1.5" />
              Keep as Raw
            </Button>
            <Button
              variant={!isRaw ? "default" : "outline"}
              size="sm"
              onClick={!isRaw ? undefined : onToggleRaw}
              className={!isRaw ? "pointer-events-none" : ""}
            >
              <VolumeX className="h-3.5 w-3.5 mr-1.5" />
              Mark as TTS
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
