import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import { createPortal } from "react-dom";
import { X, Play, Pause, Check, Sparkles, AlertTriangle } from "lucide-react";
import { Button, Input } from "@/components/ui";
import { ClippedVideoPlayer } from "./ClippedVideoPlayer";
import { formatTime, parseTime } from "@/utils";
import { api } from "@/api/client";
import type { Scene, SceneMatch, AlternativeMatch } from "@/types";

const ANOMALY_MIN_SPEED = 0.35;
const ANOMALY_MAX_SPEED = 2.5;
const ANOMALY_MAX_SOURCE_DURATION = 60;

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

  const sceneDuration = scene.end_time - scene.start_time;
  const alternatives = match?.alternatives || [];

  const resetSourcePlaybackState = useCallback(() => {
    setSourceRetryCount(0);
    setSourceHasError(false);
    setIsPlaying(false);
    setCurrentTime(0);
    setDuration(0);
  }, []);

  const { normalAlternatives, riskyAlternatives } = useMemo(() => {
    const normal: CandidateWithMeta[] = [];
    const risky: CandidateWithMeta[] = [];

    for (const candidate of alternatives) {
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
  }, [alternatives, sceneDuration]);

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

  useEffect(() => {
    if (!isOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isOpen]);

  const handleTimeUpdate = useCallback(() => {
    if (videoRef.current) {
      setCurrentTime(videoRef.current.currentTime);
    }
  }, []);

  const handleLoadedMetadata = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;

    setDuration(video.duration);
    setSourceHasError(false);

    const parsedStart = parseTime(startTime);
    const preferredStart =
      match?.confidence && match.confidence > 0
        ? match.start_time
        : parsedStart ?? 0;
    const boundedStart = Number.isFinite(video.duration)
      ? Math.min(Math.max(preferredStart, 0), video.duration)
      : Math.max(preferredStart, 0);

    video.currentTime = boundedStart;
    setCurrentTime(boundedStart);
  }, [match, startTime]);

  const handleSetStart = useCallback(() => {
    setStartTime(formatTime(currentTime));
  }, [currentTime]);

  const handleSetEnd = useCallback(() => {
    setEndTime(formatTime(currentTime));
  }, [currentTime]);

  const handleSeek = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const time = parseFloat(e.target.value);
    if (videoRef.current) {
      videoRef.current.currentTime = time;
      setCurrentTime(time);
    }
  }, []);

  const handlePlayPause = useCallback(() => {
    if (!videoRef.current) return;

    if (videoRef.current.paused) {
      void videoRef.current.play();
      setIsPlaying(true);
      return;
    }

    videoRef.current.pause();
    setIsPlaying(false);
  }, []);

  const handlePreview = useCallback(() => {
    const start = parseTime(startTime);
    if (videoRef.current && start !== null) {
      videoRef.current.currentTime = start;
      void videoRef.current.play();
      setIsPlaying(true);
    }
  }, [startTime]);

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

      window.setTimeout(() => {
        if (videoRef.current) {
          videoRef.current.currentTime = candidate.start_time;
          void videoRef.current.play();
          setIsPlaying(true);
        }
      }, 100);
    },
    [episodes, resetSourcePlaybackState],
  );

  const sourceVideoUrl = selectedEpisode
    ? (() => {
        const base = api.getSourceVideoUrl(projectId, selectedEpisode);
        if (sourceRetryCount === 0) return base;
        return `${base}&_retry=${sourceRetryCount}`;
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
