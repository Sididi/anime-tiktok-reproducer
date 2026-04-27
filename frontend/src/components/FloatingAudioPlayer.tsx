import { useRef, useState, useCallback, useEffect } from "react";
import { Play, Pause, Volume2, VolumeX } from "lucide-react";
import type { Scene } from "@/types";
import { getBrowserMediaCoordinator } from "@/utils/mediaCoordinator";
import { buildVideoSourceCandidates } from "@/utils/mediaSources";
import { MEDIA_PRIORITY } from "@/utils/mediaPriorities";

interface FloatingAudioPlayerProps {
  videoUrl: string;
  fallbackVideoUrl?: string;
  scenes: Scene[];
  onSceneChange?: (index: number) => void;
  autoScroll: boolean;
  onAutoScrollChange: (enabled: boolean) => void;
}

function formatTimeCompact(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function FloatingAudioPlayer({
  videoUrl,
  fallbackVideoUrl,
  scenes,
  onSceneChange,
  autoScroll,
  onAutoScrollChange,
}: FloatingAudioPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const resumeAfterAttachRef = useRef(false);
  const pendingCurrentTimeRef = useRef(0);
  const retryAttemptedRef = useRef(false);
  const progressRef = useRef<HTMLDivElement>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [activeSceneIdx, setActiveSceneIdx] = useState(-1);
  const [pageVisible, setPageVisible] = useState(
    document.visibilityState === "visible",
  );
  const [attachedGranted, setAttachedGranted] = useState(false);
  const [renderVersion, setRenderVersion] = useState(0);
  const [sourceIndex, setSourceIndex] = useState(0);
  const shouldRequestLoad = pageVisible || playing;
  const sourceCandidates = buildVideoSourceCandidates(videoUrl, fallbackVideoUrl);
  const activeVideoUrl = sourceCandidates[sourceIndex] ?? videoUrl;

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) {
      resumeAfterAttachRef.current = true;
      setPlaying(true);
      return;
    }
    if (video.paused) {
      video.play().catch(console.error);
      setPlaying(true);
    } else {
      video.pause();
      setPlaying(false);
    }
  }, []);

  const handleTimeUpdate = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    setCurrentTime(video.currentTime);

    // Look ahead 0.8s so highlight/scroll arrives before audio,
    // since reading is faster than listening
    const lookAhead = video.currentTime + 0.8;
    const idx = scenes.findIndex(
      (s) => lookAhead >= s.start_time && lookAhead < s.end_time
    );
    if (idx !== activeSceneIdx) {
      setActiveSceneIdx(idx);
      if (idx >= 0 && onSceneChange) {
        onSceneChange(idx);
      }
    }
  }, [scenes, activeSceneIdx, onSceneChange]);

  const handleLoadedMetadata = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    setDuration(video.duration);
    video.currentTime = pendingCurrentTimeRef.current;
    video.playbackRate = speed;
    video.volume = volume;
    video.muted = muted;
    if (resumeAfterAttachRef.current) {
      resumeAfterAttachRef.current = false;
      void video.play().catch(console.error);
    }
  }, [muted, speed, volume]);

  const handleError = useCallback(() => {
    const video = videoRef.current;
    if (video) {
      pendingCurrentTimeRef.current = video.currentTime;
      setCurrentTime(video.currentTime);
    }
    resumeAfterAttachRef.current = playing;

    if (!retryAttemptedRef.current) {
      retryAttemptedRef.current = true;
      setRenderVersion((value) => value + 1);
      return;
    }

    const nextSourceIndex = sourceIndex + 1;
    if (nextSourceIndex < sourceCandidates.length) {
      retryAttemptedRef.current = false;
      setSourceIndex(nextSourceIndex);
      setRenderVersion((value) => value + 1);
      return;
    }

    setPlaying(false);
  }, [playing, sourceCandidates.length, sourceIndex]);

  const handleProgressClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const bar = progressRef.current;
      const video = videoRef.current;
      if (!bar || !video || !duration) return;
      const rect = bar.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const nextTime = ratio * duration;
      pendingCurrentTimeRef.current = nextTime;
      video.currentTime = nextTime;
      setCurrentTime(nextTime);
    },
    [duration]
  );

  const handleSpeedChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const newSpeed = parseFloat(e.target.value);
      setSpeed(newSpeed);
      if (videoRef.current) {
        videoRef.current.playbackRate = newSpeed;
      }
    },
    []
  );

  const handleVolumeChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const v = parseFloat(e.target.value);
      setVolume(v);
      if (videoRef.current) {
        videoRef.current.volume = v;
        setMuted(v === 0);
      }
    },
    []
  );

  const toggleMute = useCallback(() => {
    if (videoRef.current) {
      const newMuted = !muted;
      videoRef.current.muted = newMuted;
      setMuted(newMuted);
    }
  }, [muted]);

  useEffect(() => {
    pendingCurrentTimeRef.current = currentTime;
  }, [currentTime]);

  useEffect(() => {
    retryAttemptedRef.current = false;
    setSourceIndex(0);
    setRenderVersion((value) => value + 1);
  }, [fallbackVideoUrl, videoUrl]);

  // Keyboard shortcut: Space to play/pause
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (
        e.code === "Space" &&
        !(e.target instanceof HTMLInputElement) &&
        !(e.target instanceof HTMLTextAreaElement) &&
        !(e.target instanceof HTMLSelectElement)
      ) {
        e.preventDefault();
        togglePlay();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [togglePlay]);

  useEffect(() => {
    const handleVisibility = () => {
      setPageVisible(document.visibilityState === "visible");
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  const sessionUpdateRef = useRef<((next: Partial<{
    requestLoad: boolean;
    requestWarmup: boolean;
    attachedPriority: number;
    warmupPriority: number;
    kind: "video" | "audio";
  }>) => void) | null>(null);

  useEffect(() => {
    const coordinator = getBrowserMediaCoordinator();
    const handle = coordinator.registerSession(
      {
        id: "floating-audio-player",
        requestLoad: shouldRequestLoad,
        requestWarmup: false,
        attachedPriority: MEDIA_PRIORITY.DEDICATED_AUDIO,
        warmupPriority: 0,
        kind: "audio",
      },
      (grant) => {
        setAttachedGranted(grant.attachedGranted);
      },
    );
    sessionUpdateRef.current = handle.update;
    return () => {
      handle.release();
      sessionUpdateRef.current = null;
    };
  }, []);

  useEffect(() => {
    sessionUpdateRef.current?.({
      requestLoad: shouldRequestLoad,
      requestWarmup: false,
      attachedPriority: MEDIA_PRIORITY.DEDICATED_AUDIO,
      warmupPriority: 0,
      kind: "audio",
    });
  }, [shouldRequestLoad]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.playbackRate = speed;
  }, [speed, attachedGranted]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.volume = volume;
    video.muted = muted;
  }, [attachedGranted, muted, volume]);

  useEffect(() => {
    if (attachedGranted) return;
    const video = videoRef.current;
    if (!video) return;
    pendingCurrentTimeRef.current = video.currentTime;
    setCurrentTime(video.currentTime);
    video.pause();
  }, [attachedGranted]);

  const progress = duration > 0 ? (currentTime / duration) * 100 : 0;

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 bg-[hsl(var(--card))] border-t border-[hsl(var(--border))] shadow-lg">
      {shouldRequestLoad && attachedGranted && (
        <video
          ref={videoRef}
          key={`${activeVideoUrl}-${renderVersion}`}
          src={activeVideoUrl}
          className="hidden"
          onTimeUpdate={handleTimeUpdate}
          onLoadedMetadata={handleLoadedMetadata}
          onError={handleError}
          onEnded={() => setPlaying(false)}
          preload="metadata"
          crossOrigin="anonymous"
        />
      )}

      {/* Progress bar (clickable) */}
      <div
        ref={progressRef}
        className="h-1 bg-[hsl(var(--muted))] cursor-pointer group hover:h-2 transition-all"
        onClick={handleProgressClick}
      >
        <div
          className="h-full bg-[hsl(var(--primary))] transition-[width] duration-100"
          style={{ width: `${progress}%` }}
        />
      </div>

      {/* Controls */}
      <div className="flex items-center gap-3 px-4 py-2 h-14">
        {/* Play/Pause */}
        <button
          onClick={togglePlay}
          className="p-1.5 rounded-full hover:bg-[hsl(var(--muted))] transition-colors"
        >
          {playing ? (
            <Pause className="h-5 w-5" />
          ) : (
            <Play className="h-5 w-5" />
          )}
        </button>

        {/* Time */}
        <span className="text-xs font-mono text-[hsl(var(--muted-foreground))] min-w-[80px]">
          {formatTimeCompact(currentTime)} / {formatTimeCompact(duration)}
        </span>

        {/* Scene indicator */}
        {activeSceneIdx >= 0 && (
          <span className="text-xs px-2 py-0.5 rounded bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary))]">
            Scene {activeSceneIdx + 1}
          </span>
        )}

        {/* Auto-scroll toggle */}
        <label className="flex items-center gap-1.5 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => onAutoScrollChange(e.target.checked)}
            className="accent-[hsl(var(--primary))] h-3.5 w-3.5"
          />
          <span className="text-xs text-[hsl(var(--muted-foreground))]">Scroll</span>
        </label>

        <div className="flex-1" />

        {/* Speed slider */}
        <span className="text-xs font-mono text-[hsl(var(--muted-foreground))] min-w-[32px] text-right">
          {speed}x
        </span>
        <input
          type="range"
          min="0.5"
          max="3"
          step="0.25"
          value={speed}
          onChange={handleSpeedChange}
          className="w-20 h-1 accent-[hsl(var(--primary))]"
          title={`Speed: ${speed}x`}
        />

        {/* Volume */}
        <button
          onClick={toggleMute}
          className="p-1 rounded hover:bg-[hsl(var(--muted))] transition-colors"
        >
          {muted || volume === 0 ? (
            <VolumeX className="h-4 w-4" />
          ) : (
            <Volume2 className="h-4 w-4" />
          )}
        </button>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={muted ? 0 : volume}
          onChange={handleVolumeChange}
          className="w-16 h-1 accent-[hsl(var(--primary))]"
        />
      </div>
    </div>
  );
}
