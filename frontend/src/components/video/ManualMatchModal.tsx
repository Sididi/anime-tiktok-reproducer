import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import { createPortal } from "react-dom";
import { X, Play, Pause, Check, Sparkles, AlertTriangle } from "lucide-react";
import { Button, Input } from "@/components/ui";
import { ClippedVideoPlayer } from "./ClippedVideoPlayer";
import { formatTime, parseTime } from "@/utils";
import { api } from "@/api/client";
import type {
  Scene,
  SceneMatch,
  AlternativeMatch,
  SourceStreamDescriptor,
} from "@/types";

const ANOMALY_MIN_SPEED = 0.35;
const ANOMALY_MAX_SPEED = 2.5;
const ANOMALY_MAX_SOURCE_DURATION = 60;
const MAX_DYNAMIC_CHUNK_SECONDS = 120;

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
    sourceDuration > 0 ? sceneDuration / sourceDuration : Number.POSITIVE_INFINITY;
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

export function ManualMatchModal({
  isOpen,
  onClose,
  scene,
  match,
  projectId,
  episodes,
  onSave,
}: ManualMatchModalProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const pendingSeekTimeRef = useRef<number | null>(null);
  const resumePlaybackAfterLoadRef = useRef(false);

  const initialEpisode =
    match?.episode && match.confidence > 0
      ? episodes.find(
          (ep) =>
            ep.includes(match.episode) ||
            match.episode.includes(ep.split("/").pop() || ""),
        ) ||
        episodes[0] ||
        ""
      : episodes[0] || "";

  const [selectedEpisode, setSelectedEpisode] =
    useState<string>(initialEpisode);
  const [startTime, setStartTime] = useState<string>(
    match?.confidence && match.confidence > 0
      ? formatTime(match.start_time)
      : "00:00.00",
  );
  const [endTime, setEndTime] = useState<string>(
    match?.confidence && match.confidence > 0
      ? formatTime(match.end_time)
      : "00:00.00",
  );
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [sourceRetryCount, setSourceRetryCount] = useState(0);
  const [sourceHasError, setSourceHasError] = useState(false);
  const [sourceDescriptor, setSourceDescriptor] =
    useState<SourceStreamDescriptor | null>(null);
  const [sourceDescriptorLoading, setSourceDescriptorLoading] = useState(false);
  const [sourceChunkStart, setSourceChunkStart] = useState(0);
  const [sourceChunkDuration, setSourceChunkDuration] = useState(0);

  const sceneDuration = scene.end_time - scene.start_time;

  const isChunkedSource = sourceDescriptor?.mode === "chunked";
  const effectiveChunkDuration =
    sourceChunkDuration > 0
      ? sourceChunkDuration
      : (sourceDescriptor?.chunk_duration ?? 0);

  const resetSourcePlaybackState = useCallback(() => {
    setSourceRetryCount(0);
    setSourceHasError(false);
    setIsPlaying(false);
    setCurrentTime(0);
    setDuration(0);
    setSourceDescriptor(null);
    setSourceChunkStart(0);
    setSourceChunkDuration(0);
    pendingSeekTimeRef.current = null;
    resumePlaybackAfterLoadRef.current = false;
  }, []);

  const clampGlobalTime = useCallback(
    (value: number) => {
      const maxDuration = sourceDescriptor?.duration ?? duration;
      if (maxDuration > 0) {
        return Math.min(Math.max(value, 0), maxDuration);
      }
      return Math.max(value, 0);
    },
    [duration, sourceDescriptor],
  );

  const getChunkWindowStart = useCallback(
    (
      targetTime: number,
      descriptor: SourceStreamDescriptor,
      windowDuration: number,
    ) => {
      const boundedDuration = Math.min(
        Math.max(windowDuration, descriptor.chunk_duration),
        MAX_DYNAMIC_CHUNK_SECONDS,
      );
      const maxStart = Math.max((descriptor.duration || 0) - boundedDuration, 0);
      const boundedTarget = clampGlobalTime(targetTime);
      const centered = Math.max(boundedTarget - boundedDuration / 2, 0);
      const step = Math.max(descriptor.chunk_step || 0.001, 0.001);
      const snapped = Math.floor(centered / step) * step;
      return Math.min(Math.max(snapped, 0), maxStart);
    },
    [clampGlobalTime],
  );

  const setChunkWindowAround = useCallback(
    (targetTime: number, requestedDuration?: number): boolean => {
      if (!sourceDescriptor || sourceDescriptor.mode !== "chunked") {
        return false;
      }

      const nextDuration = Math.min(
        Math.max(
          requestedDuration ?? sourceDescriptor.chunk_duration,
          sourceDescriptor.chunk_duration,
        ),
        MAX_DYNAMIC_CHUNK_SECONDS,
      );
      const nextStart = getChunkWindowStart(
        targetTime,
        sourceDescriptor,
        nextDuration,
      );

      const changed =
        Math.abs(nextStart - sourceChunkStart) > 0.001 ||
        Math.abs(nextDuration - sourceChunkDuration) > 0.001;

      if (changed) {
        setSourceChunkStart(nextStart);
        setSourceChunkDuration(nextDuration);
      }

      return changed;
    },
    [
      getChunkWindowStart,
      sourceChunkDuration,
      sourceChunkStart,
      sourceDescriptor,
    ],
  );

  const seekSourceGlobal = useCallback(
    (targetTime: number, autoplay: boolean, requestedChunkDuration?: number) => {
      const video = videoRef.current;
      if (!video) return;

      const bounded = clampGlobalTime(targetTime);
      if (!isChunkedSource || !sourceDescriptor) {
        video.currentTime = bounded;
        setCurrentTime(bounded);
        if (autoplay) {
          void video.play();
          setIsPlaying(true);
        }
        return;
      }

      const windowDuration =
        sourceChunkDuration > 0 ? sourceChunkDuration : sourceDescriptor.chunk_duration;
      const guard = sourceDescriptor.seek_guard_seconds;
      const safeStart = sourceChunkStart + guard;
      const safeEnd = sourceChunkStart + windowDuration - guard;

      if (bounded >= safeStart && bounded <= safeEnd && windowDuration > 0) {
        video.currentTime = Math.max(bounded - sourceChunkStart, 0);
        setCurrentTime(bounded);
        if (autoplay) {
          void video.play();
          setIsPlaying(true);
        }
        return;
      }

      pendingSeekTimeRef.current = bounded;
      resumePlaybackAfterLoadRef.current = autoplay;
      const changed = setChunkWindowAround(bounded, requestedChunkDuration);
      if (!changed) {
        video.currentTime = Math.max(bounded - sourceChunkStart, 0);
        setCurrentTime(bounded);
        if (autoplay) {
          void video.play();
          setIsPlaying(true);
        }
      }
    },
    [
      clampGlobalTime,
      isChunkedSource,
      setChunkWindowAround,
      sourceChunkDuration,
      sourceChunkStart,
      sourceDescriptor,
    ],
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

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!isOpen) return;

    if (match?.confidence && match.confidence > 0 && match.episode) {
      const matchEpisode = episodes.find(
        (ep) =>
          ep.includes(match.episode) ||
          match.episode.includes(ep.split("/").pop() || ""),
      );
      if (matchEpisode) {
        setSelectedEpisode(matchEpisode);
      }
      setStartTime(formatTime(match.start_time));
      setEndTime(formatTime(match.end_time));
    } else {
      setStartTime("00:00.00");
      setEndTime(formatTime(sceneDuration));
    }

    resetSourcePlaybackState();
  }, [isOpen, match, episodes, sceneDuration, resetSourcePlaybackState]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!isOpen || !selectedEpisode) return;

    let active = true;
    setSourceDescriptorLoading(true);
    setSourceHasError(false);

    const preferredStart =
      match?.confidence && match.confidence > 0
        ? match.start_time
        : 0;

    void api
      .getSourceDescriptor(projectId, selectedEpisode)
      .then((descriptor) => {
        if (!active) return;

        setSourceDescriptor(descriptor);
        setDuration(descriptor.duration || 0);

        if (descriptor.mode === "chunked") {
          const dynamicDuration = Math.min(
            Math.max(
              descriptor.chunk_duration,
              sceneDuration + descriptor.seek_guard_seconds * 2,
            ),
            MAX_DYNAMIC_CHUNK_SECONDS,
          );
          const nextStart = getChunkWindowStart(
            preferredStart,
            descriptor,
            dynamicDuration,
          );
          setSourceChunkDuration(dynamicDuration);
          setSourceChunkStart(nextStart);
          pendingSeekTimeRef.current = clampGlobalTime(preferredStart);
        } else {
          setSourceChunkDuration(0);
          setSourceChunkStart(0);
          pendingSeekTimeRef.current = clampGlobalTime(preferredStart);
        }
      })
      .catch(() => {
        if (!active) return;
        setSourceDescriptor(null);
        setSourceHasError(true);
      })
      .finally(() => {
        if (!active) return;
        setSourceDescriptorLoading(false);
      });

    return () => {
      active = false;
    };
  }, [
    clampGlobalTime,
    getChunkWindowStart,
    isOpen,
    match,
    projectId,
    sceneDuration,
    selectedEpisode,
  ]);
  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(() => {
    if (!isOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isOpen]);

  const handleTimeUpdate = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;

    const globalTime = isChunkedSource
      ? sourceChunkStart + video.currentTime
      : video.currentTime;
    setCurrentTime(globalTime);

    if (
      isChunkedSource &&
      sourceDescriptor &&
      !video.paused
    ) {
      const guard = sourceDescriptor.seek_guard_seconds;
      const windowDuration =
        sourceChunkDuration > 0
          ? sourceChunkDuration
          : sourceDescriptor.chunk_duration;
      const safeEnd = sourceChunkStart + windowDuration - guard;
      if (globalTime >= safeEnd) {
        pendingSeekTimeRef.current = globalTime;
        resumePlaybackAfterLoadRef.current = true;
        setChunkWindowAround(globalTime, windowDuration);
      }
    }
  }, [
    isChunkedSource,
    setChunkWindowAround,
    sourceChunkDuration,
    sourceChunkStart,
    sourceDescriptor,
  ]);

  const handleLoadedMetadata = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;

    setSourceHasError(false);

    const parsedStart = parseTime(startTime);
    const fallbackStart =
      match?.confidence && match.confidence > 0
        ? match.start_time
        : parsedStart ?? 0;
    const requestedGlobalTime = pendingSeekTimeRef.current ?? fallbackStart;

    if (isChunkedSource && sourceDescriptor) {
      const globalDuration = sourceDescriptor.duration || duration;
      if (globalDuration > 0) {
        setDuration(globalDuration);
      }

      const boundedGlobal = globalDuration > 0
        ? Math.min(Math.max(requestedGlobalTime, 0), globalDuration)
        : Math.max(requestedGlobalTime, 0);
      const localTime = Math.max(
        0,
        Math.min(video.duration || 0, boundedGlobal - sourceChunkStart),
      );
      video.currentTime = localTime;
      setCurrentTime(sourceChunkStart + localTime);
    } else {
      setDuration(video.duration);
      const boundedStart = Number.isFinite(video.duration)
        ? Math.min(Math.max(requestedGlobalTime, 0), video.duration)
        : Math.max(requestedGlobalTime, 0);
      video.currentTime = boundedStart;
      setCurrentTime(boundedStart);
    }

    pendingSeekTimeRef.current = null;

    if (resumePlaybackAfterLoadRef.current) {
      resumePlaybackAfterLoadRef.current = false;
      void video.play();
      setIsPlaying(true);
    }
  }, [
    duration,
    isChunkedSource,
    match,
    sourceChunkStart,
    sourceDescriptor,
    startTime,
  ]);

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
    if (!videoRef.current) return;

    if (videoRef.current.paused) {
      seekSourceGlobal(currentTime, true);
      return;
    }

    videoRef.current.pause();
    setIsPlaying(false);
  }, [currentTime, seekSourceGlobal]);

  const handlePreview = useCallback(() => {
    const start = parseTime(startTime);
    if (start === null) return;

    const end = parseTime(endTime);
    if (isChunkedSource && sourceDescriptor) {
      const requestedDuration =
        end !== null && end > start
          ? Math.min(
              Math.max(
                sourceDescriptor.chunk_duration,
                end - start + sourceDescriptor.seek_guard_seconds * 2,
              ),
              MAX_DYNAMIC_CHUNK_SECONDS,
            )
          : sourceDescriptor.chunk_duration;
      seekSourceGlobal(start, true, requestedDuration);
      return;
    }

    seekSourceGlobal(start, true);
  }, [
    endTime,
    isChunkedSource,
    seekSourceGlobal,
    sourceDescriptor,
    startTime,
  ]);

  const handleSave = useCallback(() => {
    const start = parseTime(startTime);
    const end = parseTime(endTime);
    if (start === null || end === null || !selectedEpisode) {
      return;
    }

    const meta = evaluateSelection(sceneDuration, start, end);
    void onSave(selectedEpisode, start, end, meta);
    onClose();
  }, [startTime, endTime, selectedEpisode, sceneDuration, onSave, onClose]);

  const handleSelectCandidate = useCallback(
    (candidate: AlternativeMatch) => {
      const matchingEpisode = episodes.find(
        (ep) =>
          ep.includes(candidate.episode) ||
          candidate.episode.includes(ep.split("/").pop() || ""),
      );

      if (matchingEpisode) {
        setSelectedEpisode(matchingEpisode);
        resetSourcePlaybackState();
      }

      setStartTime(formatTime(candidate.start_time));
      setEndTime(formatTime(candidate.end_time));
      pendingSeekTimeRef.current = candidate.start_time;
      resumePlaybackAfterLoadRef.current = true;

      window.setTimeout(() => {
        seekSourceGlobal(candidate.start_time, true);
      }, 120);
    },
    [episodes, resetSourcePlaybackState, seekSourceGlobal],
  );

  const sourceVideoUrl = selectedEpisode
    ? (() => {
        const base = isChunkedSource
          ? api.getSourceChunkUrl(
              projectId,
              selectedEpisode,
              sourceChunkStart,
              effectiveChunkDuration || undefined,
            )
          : api.getSourceVideoUrl(projectId, selectedEpisode);

        if (sourceRetryCount === 0) return base;
        const separator = base.includes("?") ? "&" : "?";
        return `${base}${separator}_retry=${sourceRetryCount}`;
      })()
    : "";

  const tiktokVideoUrl = api.getVideoUrl(projectId);

  const renderCandidateButton = (item: CandidateWithMeta) => {
    const { candidate, meta } = item;
    return (
      <button
        key={`${candidate.algorithm ?? "candidate"}-${candidate.episode}-${candidate.start_time}-${candidate.end_time}`}
        onClick={() => handleSelectCandidate(candidate)}
        className="flex items-center justify-between w-full px-3 py-2 bg-[hsl(var(--background))] hover:bg-[hsl(var(--accent))] rounded text-sm text-left transition-colors"
      >
        <div className="flex-1 min-w-0">
          <div className="font-medium truncate text-xs">
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
              Risky: {formatTime(meta.sourceDuration)} source ({meta.speedRatio.toFixed(2)}x)
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
      <div className="bg-[hsl(var(--card))] rounded-lg w-full max-w-7xl max-h-[95vh] overflow-hidden flex flex-col">
        <div className="flex items-center justify-between p-4 border-b border-[hsl(var(--border))]">
          <h2 className="text-lg font-semibold">
            Manual Match Selection - Scene {scene.index + 1}
          </h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-[hsl(var(--muted))] rounded"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          <div className="grid grid-cols-[320px_1fr] gap-6 h-full">
            <div className="space-y-4">
              <div>
                <h3 className="text-sm font-medium mb-2 text-[hsl(var(--muted-foreground))]">
                  TikTok Scene ({formatTime(sceneDuration)})
                </h3>
                <div className="aspect-[9/16] bg-black rounded overflow-hidden">
                  <ClippedVideoPlayer
                    src={tiktokVideoUrl}
                    startTime={scene.start_time}
                    endTime={scene.end_time}
                    eager
                    className="w-full h-full"
                  />
                </div>
              </div>

              {(normalAlternatives.length > 0 || riskyAlternatives.length > 0) && (
                <div className="bg-[hsl(var(--muted))] rounded-lg p-3 space-y-3">
                  <div className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-amber-500" />
                    <span className="text-sm font-medium">AI Candidates</span>
                  </div>

                  {normalAlternatives.length > 0 && (
                    <div className="space-y-2 max-h-44 overflow-y-auto">
                      {normalAlternatives.map(renderCandidateButton)}
                    </div>
                  )}

                  {riskyAlternatives.length > 0 && (
                    <div className="space-y-2">
                      <div className="flex items-center gap-1 text-xs text-amber-500">
                        <AlertTriangle className="h-3 w-3" />
                        Risky candidates
                      </div>
                      <div className="space-y-2 max-h-36 overflow-y-auto">
                        {riskyAlternatives.map(renderCandidateButton)}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1">
                  Select Episode
                </label>
                <select
                  value={selectedEpisode}
                  onChange={(e) => {
                    setSelectedEpisode(e.target.value);
                    resetSourcePlaybackState();
                  }}
                  className="w-full p-2 bg-[hsl(var(--input))] border border-[hsl(var(--border))] rounded text-sm"
                >
                  {episodes.map((ep) => (
                    <option key={ep} value={ep}>
                      {ep.split("/").pop()}
                    </option>
                  ))}
                </select>
              </div>

              {selectedEpisode && (
                <div className="space-y-2">
                  <div className="relative aspect-video bg-black rounded overflow-hidden">
                    {!sourceDescriptorLoading && sourceDescriptor?.mode === "chunked" && (
                      <div className="absolute top-2 right-2 z-10 text-[10px] px-1.5 py-0.5 rounded bg-black/60 text-white">
                        Chunked preview
                      </div>
                    )}
                    {sourceDescriptorLoading ? (
                      <div className="absolute inset-0 flex items-center justify-center text-xs text-white/80">
                        Preparing source stream...
                      </div>
                    ) : (
                      <video
                        key={sourceVideoUrl}
                        ref={videoRef}
                        src={sourceVideoUrl}
                        className="w-full h-full object-contain"
                        onTimeUpdate={handleTimeUpdate}
                        onLoadedMetadata={handleLoadedMetadata}
                        onCanPlay={() => setSourceHasError(false)}
                        onPlay={() => setIsPlaying(true)}
                        onPause={() => setIsPlaying(false)}
                        onError={() => {
                          setSourceHasError(true);
                          setIsPlaying(false);
                        }}
                        muted
                        playsInline
                        preload="auto"
                      />
                    )}
                    {sourceHasError && (
                      <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-black/80 text-white">
                        <span className="text-xs">Failed to load source</span>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setSourceHasError(false);
                            setSourceRetryCount((value) => value + 1);
                          }}
                        >
                          Retry
                        </Button>
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={handlePlayPause}
                      disabled={sourceDescriptorLoading}
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
                      disabled={sourceDescriptorLoading}
                    />
                    <span className="text-sm font-mono w-28 text-right">
                      {formatTime(currentTime)} / {formatTime(duration)}
                    </span>
                  </div>
                </div>
              )}

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-1">
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
                  <label className="block text-sm font-medium mb-1">
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
                disabled={sourceDescriptorLoading}
              >
                <Play className="h-4 w-4 mr-2" />
                Preview from Start Time
              </Button>
            </div>
          </div>
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-[hsl(var(--border))]">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSave}>
            <Check className="h-4 w-4 mr-2" />
            Save Match
          </Button>
        </div>
      </div>
    </div>
  );

  if (!isOpen) return null;
  return createPortal(modalContent, document.body);
}
