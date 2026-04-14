import { memo, useState, useCallback, useRef, useEffect, useMemo } from "react";
import { createPortal } from "react-dom";
import { X, Play, Pause, Check, Sparkles, AlertTriangle } from "lucide-react";
import { Button, Input } from "@/components/ui";
import { useSourcePlaybackStrategy } from "@/hooks/useSourcePlaybackStrategy";
import { formatTime, parseTime } from "@/utils";
import { api } from "@/api/client";
import { MEDIA_PRIORITY } from "@/utils/mediaPriorities";
import type { Scene, SceneMatch, AlternativeMatch } from "@/types";
import {
  ManagedVideoPlayer,
  type ManagedVideoPlayerHandle,
} from "./ManagedVideoPlayer";
import { ProjectManagedVideoPlayer } from "./ProjectManagedVideoPlayer";

const ANOMALY_MIN_SPEED = 0.35;
const ANOMALY_MAX_SPEED = 2.5;
const ANOMALY_MAX_SOURCE_DURATION = 60;

type ManagedVideoPhase =
  | "poster"
  | "leasing"
  | "warming"
  | "ready"
  | "playing"
  | "frozen"
  | "error";

export interface ManualMatchSaveMeta {
  anomalous: boolean;
  sourceDuration: number;
  speedRatio: number;
}

interface CandidateWithMeta {
  candidate: AlternativeMatch;
  meta: ManualMatchSaveMeta;
}

interface ManualMatchModalProps {
  isOpen: boolean;
  onClose: () => void;
  scene: Scene;
  match?: SceneMatch;
  projectId: string;
  episodes: string[];
  onSave: (
    episode: string,
    startTime: number,
    endTime: number,
    meta: ManualMatchSaveMeta,
  ) => Promise<void> | void;
}

function evaluateSelection(
  sceneDuration: number,
  startTime: number,
  endTime: number,
): ManualMatchSaveMeta {
  const sourceDuration = Math.max(0, endTime - startTime);
  const speedRatio =
    sourceDuration > 0
      ? sceneDuration / sourceDuration
      : Number.POSITIVE_INFINITY;
  const anomalous =
    sourceDuration > ANOMALY_MAX_SOURCE_DURATION ||
    speedRatio < ANOMALY_MIN_SPEED ||
    speedRatio > ANOMALY_MAX_SPEED;

  return {
    anomalous,
    sourceDuration,
    speedRatio,
  };
}

function resolveEpisode(
  episodes: string[],
  episodeHint: string | null | undefined,
): string {
  if (!episodes.length) {
    return "";
  }
  if (!episodeHint) {
    return episodes[0] || "";
  }
  return (
    episodes.find(
      (episode) =>
        episode.includes(episodeHint) ||
        episodeHint.includes(episode.split("/").pop() || ""),
    ) ||
    episodes[0] ||
    ""
  );
}

function ManualMatchModalContent({
  isOpen,
  onClose,
  scene,
  match,
  projectId,
  episodes,
  onSave,
}: ManualMatchModalProps) {
  const sourcePlayerRef = useRef<ManagedVideoPlayerHandle>(null);
  const pendingSeekTimeRef = useRef<number | null>(null);
  const resumePlaybackAfterLoadRef = useRef(false);
  const [fallbackEpisodes, setFallbackEpisodes] = useState<string[]>([]);

  const availableEpisodes = episodes.length > 0 ? episodes : fallbackEpisodes;

  const initialEpisode = resolveEpisode(
    availableEpisodes,
    match?.episode && match.confidence > 0 ? match.episode : null,
  );

  const [selectedEpisode, setSelectedEpisode] = useState<string>(initialEpisode);
  const [startTime, setStartTime] = useState<string>(
    match?.confidence && match.confidence > 0
      ? formatTime(match.start_time)
      : "00:00.00",
  );
  const [endTime, setEndTime] = useState<string>(
    match?.confidence && match.confidence > 0
      ? formatTime(match.end_time)
      : formatTime(scene.end_time - scene.start_time),
  );
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [sourcePhase, setSourcePhase] =
    useState<ManagedVideoPhase>("poster");

  const sceneDuration = scene.end_time - scene.start_time;
  const parsedStartTime = useMemo(() => parseTime(startTime) ?? 0, [startTime]);
  const sourceStrategy = useSourcePlaybackStrategy({
    projectId,
    episode: selectedEpisode,
    enabled: isOpen && Boolean(selectedEpisode),
    initialTargetTime: parsedStartTime,
  });
  const sourceDescriptor = sourceStrategy.descriptor;
  const chunkStreamingMode = sourceStrategy.mode === "chunked";
  const sourceVideoUrl = sourceStrategy.sourceUrl;

  const resetSourcePlaybackState = useCallback(() => {
    setSourcePhase("poster");
    setIsPlaying(false);
    setCurrentTime(0);
    setDuration(0);
    pendingSeekTimeRef.current = null;
    resumePlaybackAfterLoadRef.current = false;
  }, []);

  const clampSourceTime = useCallback(
    (value: number, maxDuration?: number) => {
      const boundedValue = Math.max(value, 0);
      if (maxDuration && maxDuration > 0) {
        return Math.min(boundedValue, maxDuration);
      }
      if (sourceDescriptor?.duration && sourceDescriptor.duration > 0) {
        return Math.min(boundedValue, sourceDescriptor.duration);
      }
      if (duration > 0) {
        return Math.min(boundedValue, duration);
      }
      return boundedValue;
    },
    [duration, sourceDescriptor],
  );

  const seekSourceGlobal = useCallback(
    (targetTime: number, autoplay: boolean) => {
      const bounded = clampSourceTime(targetTime);
      pendingSeekTimeRef.current = bounded;
      resumePlaybackAfterLoadRef.current = autoplay;
      setCurrentTime(bounded);

      if (chunkStreamingMode && sourceDescriptor) {
        if (sourceStrategy.containsTime(bounded)) {
          void sourcePlayerRef.current?.seekTo(
            sourceStrategy.toLocalTime(bounded),
            autoplay,
          );
          return;
        }

        sourceStrategy.retargetChunkWindow(bounded);
        return;
      }

      void sourcePlayerRef.current?.seekTo(bounded, autoplay);
    },
    [chunkStreamingMode, clampSourceTime, sourceDescriptor, sourceStrategy],
  );

  const { normalAlternatives, riskyAlternatives } = useMemo(() => {
    const normal: CandidateWithMeta[] = [];
    const risky: CandidateWithMeta[] = [];

    for (const candidate of match?.alternatives ?? []) {
      const meta = evaluateSelection(
        sceneDuration,
        candidate.start_time,
        candidate.end_time,
      );
      const withMeta = { candidate, meta };
      if (meta.anomalous) {
        risky.push(withMeta);
      } else {
        normal.push(withMeta);
      }
    }

    return {
      normalAlternatives: normal,
      riskyAlternatives: risky,
    };
  }, [match?.alternatives, sceneDuration]);

  const extraEpisodesToWarm = useMemo(
    () =>
      [...(match?.alternatives ?? [])]
        .sort((left, right) => right.confidence - left.confidence)
        .map((candidate) => resolveEpisode(availableEpisodes, candidate.episode))
        .filter((episode) => Boolean(episode) && episode !== selectedEpisode)
        .filter((episode, index, all) => all.indexOf(episode) === index)
        .slice(0, 2),
    [availableEpisodes, match?.alternatives, selectedEpisode],
  );

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (episodes.length > 0) {
      setFallbackEpisodes([]);
    }
  }, [episodes]);

  useEffect(() => {
    if (!isOpen || episodes.length > 0) return;

    let cancelled = false;
    void api
      .getEpisodes(projectId)
      .then(({ episodes: loadedEpisodes }) => {
        if (!cancelled) {
          setFallbackEpisodes(loadedEpisodes);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setFallbackEpisodes([]);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [episodes.length, isOpen, projectId]);

  useEffect(() => {
    if (!isOpen) return;

    const nextEpisode = resolveEpisode(
      availableEpisodes,
      match?.episode && match.confidence > 0 ? match.episode : null,
    );

    resetSourcePlaybackState();
    setSelectedEpisode(nextEpisode);
    setSourcePhase(nextEpisode ? "leasing" : "poster");

    if (match?.confidence && match.confidence > 0) {
      setStartTime(formatTime(match.start_time));
      setEndTime(formatTime(match.end_time));
      pendingSeekTimeRef.current = match.start_time;
    } else {
      setStartTime("00:00.00");
      setEndTime(formatTime(sceneDuration));
      pendingSeekTimeRef.current = 0;
    }
  }, [availableEpisodes, isOpen, match, resetSourcePlaybackState, sceneDuration]);
  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(() => {
    if (!isOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    void api.warmProjectPreview(projectId).catch(() => {});
  }, [isOpen, projectId]);

  useEffect(() => {
    if (!isOpen) return;
    for (const episode of extraEpisodesToWarm) {
      void api.getSourceDescriptor(projectId, episode).catch(() => {});
    }
  }, [extraEpisodesToWarm, isOpen, projectId, selectedEpisode]);

  useEffect(() => {
    if (chunkStreamingMode && sourceDescriptor?.duration) {
      setDuration(sourceDescriptor.duration);
    }
  }, [chunkStreamingMode, sourceDescriptor]);

  const handleSourceMetadata = useCallback(
    (loadedDuration: number) => {
      if (chunkStreamingMode && sourceDescriptor?.duration) {
        setDuration(sourceDescriptor.duration);
        return;
      }
      setDuration(loadedDuration);
    },
    [chunkStreamingMode, sourceDescriptor],
  );

  const syncPendingSourceSeek = useCallback(() => {
    const parsedStart = parseTime(startTime);
    const fallbackStart =
      match?.confidence && match.confidence > 0
        ? match.start_time
        : (parsedStart ?? 0);
    const requestedTime = pendingSeekTimeRef.current ?? fallbackStart;
    const boundedStart = clampSourceTime(requestedTime);
    pendingSeekTimeRef.current = null;
    setCurrentTime(boundedStart);

    const localTarget = sourceStrategy.toLocalTime(boundedStart);

    void sourcePlayerRef.current?.seekTo(
      localTarget,
      resumePlaybackAfterLoadRef.current,
    );
    if (resumePlaybackAfterLoadRef.current) {
      resumePlaybackAfterLoadRef.current = false;
    }
  }, [
    chunkStreamingMode,
    clampSourceTime,
    match,
    sourceStrategy,
    startTime,
  ]);

  const handleSourceTimeUpdate = useCallback(
    (playerTime: number) => {
      setCurrentTime(clampSourceTime(sourceStrategy.toGlobalTime(playerTime)));
    },
    [clampSourceTime, sourceStrategy],
  );

  const handleSourcePhaseChange = useCallback(
    (next: ManagedVideoPhase) => {
      setSourcePhase(next);
      setIsPlaying(next === "playing");
      if (
        (next === "ready" || next === "playing") &&
        pendingSeekTimeRef.current !== null
      ) {
        syncPendingSourceSeek();
      }
    },
    [syncPendingSourceSeek],
  );

  const handleSetStart = useCallback(() => {
    setStartTime(formatTime(currentTime));
  }, [currentTime]);

  const handleSetEnd = useCallback(() => {
    setEndTime(formatTime(currentTime));
  }, [currentTime]);

  const handleSeek = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const time = parseFloat(e.target.value);
      if (!Number.isFinite(time)) return;
      seekSourceGlobal(time, false);
    },
    [seekSourceGlobal],
  );

  const handlePlayPause = useCallback(() => {
    if (!sourcePlayerRef.current) return;
    if (!isPlaying) {
      seekSourceGlobal(currentTime, true);
      return;
    }

    sourcePlayerRef.current.pause();
    setIsPlaying(false);
  }, [currentTime, isPlaying, seekSourceGlobal]);

  const handlePreview = useCallback(() => {
    const start = parseTime(startTime);
    if (start === null) return;
    seekSourceGlobal(start, true);
  }, [seekSourceGlobal, startTime]);

  const handleSave = useCallback(() => {
    const start = parseTime(startTime);
    const end = parseTime(endTime);
    if (start === null || end === null || !selectedEpisode) {
      return;
    }

    const meta = evaluateSelection(sceneDuration, start, end);
    void onSave(selectedEpisode, start, end, meta);
    onClose();
  }, [endTime, onClose, onSave, sceneDuration, selectedEpisode, startTime]);

  const handleSelectCandidate = useCallback(
    (candidate: AlternativeMatch) => {
      const matchingEpisode = resolveEpisode(
        availableEpisodes,
        candidate.episode,
      );

      setStartTime(formatTime(candidate.start_time));
      setEndTime(formatTime(candidate.end_time));
      pendingSeekTimeRef.current = candidate.start_time;
      resumePlaybackAfterLoadRef.current = true;

      if (matchingEpisode && matchingEpisode !== selectedEpisode) {
        setSelectedEpisode(matchingEpisode);
        resetSourcePlaybackState();
        setSourcePhase("leasing");
        pendingSeekTimeRef.current = candidate.start_time;
        resumePlaybackAfterLoadRef.current = true;
        return;
      }

      seekSourceGlobal(candidate.start_time, true);
    },
    [availableEpisodes, resetSourcePlaybackState, seekSourceGlobal, selectedEpisode],
  );

  const sourceControlsDisabled =
    !sourceVideoUrl ||
    sourceStrategy.loading ||
    sourcePhase === "leasing" ||
    sourcePhase === "warming" ||
    sourcePhase === "error";

  const renderCandidateButton = (item: CandidateWithMeta) => {
    const { candidate, meta } = item;
    return (
      <button
        key={`${candidate.algorithm ?? "candidate"}-${candidate.episode}-${candidate.start_time}-${candidate.end_time}`}
        onClick={() => handleSelectCandidate(candidate)}
        className="flex w-full items-center justify-between rounded bg-[hsl(var(--background))] px-3 py-2 text-left text-sm transition-colors hover:bg-[hsl(var(--accent))]"
      >
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium">
            {candidate.episode.split("/").pop()}
          </div>
          <div className="text-xs text-[hsl(var(--muted-foreground))]">
            {formatTime(candidate.start_time)} - {formatTime(candidate.end_time)}
            {candidate.algorithm && (
              <span className="ml-1 opacity-60">[{candidate.algorithm}]</span>
            )}
          </div>
          {meta.anomalous && (
            <div className="mt-1 text-[10px] text-amber-500">
              Risky: {formatTime(meta.sourceDuration)} source (
              {meta.speedRatio.toFixed(2)}x)
            </div>
          )}
        </div>
        <div className="ml-2 text-xs font-mono text-emerald-500">
          {Math.round(candidate.confidence * 100)}%
        </div>
      </button>
    );
  };

  const modalContent = (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/70 p-4">
      <div className="flex max-h-[95vh] w-full max-w-7xl flex-col overflow-hidden rounded-lg bg-[hsl(var(--card))]">
        <div className="flex items-center justify-between border-b border-[hsl(var(--border))] p-4">
          <h2 className="text-lg font-semibold">
            Manual Match Selection - Scene {scene.index + 1}
          </h2>
          <button
            onClick={onClose}
            className="rounded p-1 hover:bg-[hsl(var(--muted))]"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          <div className="grid h-full grid-cols-[320px_1fr] gap-6">
            <div className="space-y-4">
              <div>
                <h3 className="mb-2 text-sm font-medium text-[hsl(var(--muted-foreground))]">
                  TikTok Scene ({formatTime(sceneDuration)})
                </h3>
                <ProjectManagedVideoPlayer
                  projectId={projectId}
                  className="aspect-[9/16] overflow-hidden rounded"
                  requestLoad={isOpen}
                  requestWarmup={isOpen}
                  attachedPriority={MEDIA_PRIORITY.MANUAL_MODAL}
                  warmupPriority={MEDIA_PRIORITY.MANUAL_MODAL}
                  startTime={scene.start_time}
                  endTime={scene.end_time}
                  playbackRate={1}
                  muted
                  controls
                  placeholderLabel="TikTok preview deferred"
                />
              </div>

              {(normalAlternatives.length > 0 ||
                riskyAlternatives.length > 0) && (
                <div className="space-y-3 rounded-lg bg-[hsl(var(--muted))] p-3">
                  <div className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-amber-500" />
                    <span className="text-sm font-medium">AI Candidates</span>
                  </div>

                  {normalAlternatives.length > 0 && (
                    <div className="max-h-44 space-y-2 overflow-y-auto">
                      {normalAlternatives.map(renderCandidateButton)}
                    </div>
                  )}

                  {riskyAlternatives.length > 0 && (
                    <div className="space-y-2">
                      <div className="flex items-center gap-1 text-xs text-amber-500">
                        <AlertTriangle className="h-3 w-3" />
                        Risky candidates
                      </div>
                      <div className="max-h-36 space-y-2 overflow-y-auto">
                        {riskyAlternatives.map(renderCandidateButton)}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="space-y-4">
              <div>
                <label className="mb-1 block text-sm font-medium">
                  Select Episode
                </label>
                <select
                  value={selectedEpisode}
                  onChange={(e) => {
                    const nextEpisode = e.target.value;
                    setSelectedEpisode(nextEpisode);
                    resetSourcePlaybackState();
                    setSourcePhase(nextEpisode ? "leasing" : "poster");
                    pendingSeekTimeRef.current = parseTime(startTime) ?? 0;
                  }}
                  className="w-full rounded border border-[hsl(var(--border))] bg-[hsl(var(--input))] p-2 text-sm"
                >
                  {availableEpisodes.map((episode) => (
                    <option key={episode} value={episode}>
                      {episode.split("/").pop()}
                    </option>
                  ))}
                </select>
              </div>

              {selectedEpisode && (
                <div className="space-y-2">
                  <div className="relative aspect-video overflow-hidden rounded bg-black">
                    {sourceVideoUrl ? (
                      <ManagedVideoPlayer
                        ref={sourcePlayerRef}
                        src={sourceVideoUrl}
                        className="h-full w-full"
                        requestLoad={isOpen && Boolean(sourceVideoUrl)}
                        requestWarmup={isOpen && Boolean(sourceVideoUrl)}
                        attachedPriority={MEDIA_PRIORITY.MANUAL_MODAL - 1}
                        warmupPriority={MEDIA_PRIORITY.MANUAL_MODAL - 1}
                        playbackRate={1}
                        muted
                        controls={false}
                        onTimeUpdate={handleSourceTimeUpdate}
                        onLoadedMetadata={handleSourceMetadata}
                        onPhaseChange={handleSourcePhaseChange}
                        placeholderLabel="Source preview deferred"
                      />
                    ) : (
                      <div className="flex h-full w-full items-center justify-center text-xs text-white/70">
                        {sourceStrategy.loading ? (
                          <span>Inspecting source stream...</span>
                        ) : (
                          <span>Source stream unavailable.</span>
                        )}
                      </div>
                    )}
                    {chunkStreamingMode && sourcePhase !== "error" && sourceVideoUrl && (
                      <div className="absolute right-1 top-1 z-10 rounded bg-black/60 px-1.5 py-0.5 text-[10px] text-white">
                        Streaming preview
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={handlePlayPause}
                      disabled={sourceControlsDisabled}
                    >
                      {isPlaying ? (
                        <Pause className="h-4 w-4" />
                      ) : (
                        <Play className="h-4 w-4" />
                      )}
                    </Button>
                    <input
                      type="range"
                      min={0}
                      max={duration || 100}
                      step={0.01}
                      value={currentTime}
                      onChange={handleSeek}
                      className="flex-1"
                      disabled={sourceControlsDisabled}
                    />
                    <span className="w-28 text-right font-mono text-sm">
                      {formatTime(currentTime)} / {formatTime(duration)}
                    </span>
                  </div>
                </div>
              )}

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="mb-1 block text-sm font-medium">
                    Start Time
                  </label>
                  <div className="flex gap-2">
                    <Input
                      value={startTime}
                      onChange={(e) => setStartTime(e.target.value)}
                      placeholder="00:00.00"
                      className="flex-1 font-mono"
                    />
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleSetStart}
                    >
                      Set Current
                    </Button>
                  </div>
                </div>
                <div>
                  <label className="mb-1 block text-sm font-medium">
                    End Time
                  </label>
                  <div className="flex gap-2">
                    <Input
                      value={endTime}
                      onChange={(e) => setEndTime(e.target.value)}
                      placeholder="00:00.00"
                      className="flex-1 font-mono"
                    />
                    <Button variant="outline" size="sm" onClick={handleSetEnd}>
                      Set Current
                    </Button>
                  </div>
                </div>
              </div>

              <Button
                variant="outline"
                onClick={handlePreview}
                className="w-full"
                disabled={sourceControlsDisabled}
              >
                <Play className="mr-2 h-4 w-4" />
                Preview from Start Time
              </Button>
            </div>
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-[hsl(var(--border))] p-4">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSave}>
            <Check className="mr-2 h-4 w-4" />
            Save Match
          </Button>
        </div>
      </div>
    </div>
  );

  if (!isOpen) return null;
  return createPortal(modalContent, document.body);
}

function ManualMatchModalBase(props: ManualMatchModalProps) {
  if (!props.isOpen) {
    return null;
  }

  return <ManualMatchModalContent {...props} />;
}

export const ManualMatchModal = memo(
  ManualMatchModalBase,
  (prevProps, nextProps) => !prevProps.isOpen && !nextProps.isOpen,
);
