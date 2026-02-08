import { useRef, useState, useCallback, useEffect } from "react";
import { Play, Pause, Volume2, VolumeX } from "lucide-react";
import type { Scene } from "@/types";

interface FloatingAudioPlayerProps {
  videoUrl: string;
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
  scenes,
  onSceneChange,
  autoScroll,
  onAutoScrollChange,
}: FloatingAudioPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const progressRef = useRef<HTMLDivElement>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [activeSceneIdx, setActiveSceneIdx] = useState(-1);

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
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
    if (videoRef.current) {
      setDuration(videoRef.current.duration);
    }
  }, []);

  const handleProgressClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const bar = progressRef.current;
      const video = videoRef.current;
      if (!bar || !video || !duration) return;
      const rect = bar.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      video.currentTime = ratio * duration;
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

  const progress = duration > 0 ? (currentTime / duration) * 100 : 0;

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 bg-[hsl(var(--card))] border-t border-[hsl(var(--border))] shadow-lg">
      {/* Hidden video element (audio source) */}
      <video
        ref={videoRef}
        src={videoUrl}
        className="hidden"
        onTimeUpdate={handleTimeUpdate}
        onLoadedMetadata={handleLoadedMetadata}
        onEnded={() => setPlaying(false)}
        preload="metadata"
      />

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
