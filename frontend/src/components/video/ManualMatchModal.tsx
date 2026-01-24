import { useState, useCallback, useRef, useEffect } from "react";
import { X, Play, Pause, Check, Sparkles } from "lucide-react";
import { Button, Input } from "@/components/ui";
import { ClippedVideoPlayer } from "./ClippedVideoPlayer";
import { formatTime, parseTime } from "@/utils";
import { api } from "@/api/client";
import type { Scene, SceneMatch, AlternativeMatch } from "@/types";

interface ManualMatchModalProps {
  isOpen: boolean;
  onClose: () => void;
  scene: Scene;
  match?: SceneMatch;
  projectId: string;
  episodes: string[];
  onSave: (episode: string, startTime: number, endTime: number) => void;
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

  // Initialize with existing match data if available
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

  // Calculate scene duration for reference
  const sceneDuration = scene.end_time - scene.start_time;

  // Get alternatives from match
  const alternatives = match?.alternatives || [];

  // Reset when modal opens or episode changes
  useEffect(() => {
    if (isOpen) {
      // Reset to match data if available
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
    }
  }, [isOpen, match, episodes, sceneDuration]);

  // Seek to start time when video loads and we have a match
  useEffect(() => {
    if (videoRef.current && match?.confidence && match.confidence > 0) {
      videoRef.current.currentTime = match.start_time;
      setCurrentTime(match.start_time);
    }
  }, [selectedEpisode, match]);

  const handleTimeUpdate = useCallback(() => {
    if (videoRef.current) {
      setCurrentTime(videoRef.current.currentTime);
    }
  }, []);

  const handleLoadedMetadata = useCallback(() => {
    if (videoRef.current) {
      setDuration(videoRef.current.duration);
    }
  }, []);

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
    if (videoRef.current) {
      if (videoRef.current.paused) {
        videoRef.current.play();
        setIsPlaying(true);
      } else {
        videoRef.current.pause();
        setIsPlaying(false);
      }
    }
  }, []);

  const handlePreview = useCallback(() => {
    const start = parseTime(startTime);
    if (videoRef.current && start !== null) {
      videoRef.current.currentTime = start;
      videoRef.current.play();
      setIsPlaying(true);
    }
  }, [startTime]);

  const handleSave = useCallback(() => {
    const start = parseTime(startTime);
    const end = parseTime(endTime);
    if (start !== null && end !== null && selectedEpisode) {
      onSave(selectedEpisode, start, end);
      onClose();
    }
  }, [startTime, endTime, selectedEpisode, onSave, onClose]);

  // Handle selecting a candidate - auto-populate fields and start playback
  const handleSelectCandidate = useCallback(
    (candidate: AlternativeMatch) => {
      // Find matching episode in episodes list
      const matchingEpisode = episodes.find(
        (ep) =>
          ep.includes(candidate.episode) ||
          candidate.episode.includes(ep.split("/").pop() || ""),
      );

      if (matchingEpisode) {
        setSelectedEpisode(matchingEpisode);
      }

      setStartTime(formatTime(candidate.start_time));
      setEndTime(formatTime(candidate.end_time));

      // Wait for video to update then seek and play
      setTimeout(() => {
        if (videoRef.current) {
          videoRef.current.currentTime = candidate.start_time;
          videoRef.current.play();
          setIsPlaying(true);
        }
      }, 100);
    },
    [episodes],
  );

  const sourceVideoUrl = selectedEpisode
    ? api.getSourceVideoUrl(projectId, selectedEpisode)
    : "";

  const tiktokVideoUrl = api.getVideoUrl(projectId);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="bg-[hsl(var(--card))] rounded-lg w-full max-w-7xl max-h-[95vh] overflow-hidden flex flex-col">
        {/* Header */}
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

        {/* Content - Two column layout */}
        <div className="flex-1 overflow-y-auto p-4">
          <div className="grid grid-cols-[320px_1fr] gap-6 h-full">
            {/* Left Column - TikTok video and AI candidates */}
            <div className="space-y-4">
              {/* TikTok Scene Preview */}
              <div>
                <h3 className="text-sm font-medium mb-2 text-[hsl(var(--muted-foreground))]">
                  TikTok Scene ({formatTime(sceneDuration)})
                </h3>
                <div className="aspect-[9/16] bg-black rounded overflow-hidden">
                  <ClippedVideoPlayer
                    src={tiktokVideoUrl}
                    startTime={scene.start_time}
                    endTime={scene.end_time}
                    className="w-full h-full"
                  />
                </div>
              </div>

              {/* AI Candidates suggestion */}
              {alternatives.length > 0 && (
                <div className="bg-[hsl(var(--muted))] rounded-lg p-3">
                  <div className="flex items-center gap-2 mb-2">
                    <Sparkles className="h-4 w-4 text-amber-500" />
                    <span className="text-sm font-medium">AI Candidates</span>
                  </div>
                  <div className="space-y-2 max-h-48 overflow-y-auto">
                    {alternatives.map((alt, idx) => (
                      <button
                        key={idx}
                        onClick={() => handleSelectCandidate(alt)}
                        className="flex items-center justify-between w-full px-3 py-2 bg-[hsl(var(--background))] hover:bg-[hsl(var(--accent))] rounded text-sm text-left transition-colors"
                      >
                        <div className="flex-1 min-w-0">
                          <div className="font-medium truncate text-xs">
                            {alt.episode.split("/").pop()}
                          </div>
                          <div className="text-xs text-[hsl(var(--muted-foreground))]">
                            {formatTime(alt.start_time)} -{" "}
                            {formatTime(alt.end_time)}
                            {alt.algorithm && (
                              <span className="ml-1 opacity-60">
                                [{alt.algorithm}]
                              </span>
                            )}
                          </div>
                        </div>
                        <div className="ml-2 text-xs font-mono text-emerald-500">
                          {Math.round(alt.confidence * 100)}%
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Right Column - Source video selection and controls */}
            <div className="space-y-4">
              {/* Episode selector */}
              <div>
                <label className="block text-sm font-medium mb-1">
                  Select Episode
                </label>
                <select
                  value={selectedEpisode}
                  onChange={(e) => setSelectedEpisode(e.target.value)}
                  className="w-full p-2 bg-[hsl(var(--input))] border border-[hsl(var(--border))] rounded text-sm"
                >
                  {episodes.map((ep) => (
                    <option key={ep} value={ep}>
                      {ep.split("/").pop()}
                    </option>
                  ))}
                </select>
              </div>

              {/* Video preview */}
              {selectedEpisode && (
                <div className="space-y-2">
                  <div className="aspect-video bg-black rounded overflow-hidden">
                    <video
                      ref={videoRef}
                      src={sourceVideoUrl}
                      className="w-full h-full object-contain"
                      onTimeUpdate={handleTimeUpdate}
                      onLoadedMetadata={handleLoadedMetadata}
                      onPlay={() => setIsPlaying(true)}
                      onPause={() => setIsPlaying(false)}
                      muted
                    />
                  </div>

                  {/* Video controls */}
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

              {/* Time inputs */}
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

              {/* Preview button */}
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

        {/* Footer */}
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
}
