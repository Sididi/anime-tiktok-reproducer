import { useEffect, useState, useCallback } from "react";
import { Volume2, VolumeX } from "lucide-react";
import { useVideo } from "@/contexts";
import { VideoControls } from "./VideoControls";
import { cn } from "@/utils";

interface VideoPlayerProps {
  src: string;
  className?: string;
}

export function VideoPlayer({ src, className }: VideoPlayerProps) {
  const { videoRef, play, pause, nextFrame, prevFrame, seekTo, handlers } =
    useVideo();
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);

  useEffect(() => {
    if (!videoRef.current) return;
    videoRef.current.volume = volume;
    videoRef.current.muted = muted;
  }, [videoRef, volume, muted]);

  const handleVolumeChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const nextVolume = parseFloat(e.target.value);
      setVolume(nextVolume);
      if (nextVolume > 0 && muted) {
        setMuted(false);
      }
    },
    [muted],
  );

  const toggleMute = useCallback(() => {
    setMuted((prev) => !prev);
  }, []);

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="relative bg-black rounded-lg overflow-hidden aspect-[9/16] max-h-[60vh]">
        <video
          ref={videoRef}
          src={src}
          className="w-full h-full object-contain"
          onTimeUpdate={handlers.onTimeUpdate}
          onLoadedMetadata={handlers.onLoadedMetadata}
          onPlay={handlers.onPlay}
          onPause={handlers.onPause}
          playsInline
        />
        <div className="absolute top-2 right-2 z-10 flex items-center gap-2 rounded-md bg-black/65 px-2 py-1">
          <button
            type="button"
            onClick={toggleMute}
            className="text-white hover:text-white/80 transition-colors"
            title={muted || volume === 0 ? "Unmute" : "Mute"}
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
            className="w-20 h-1 accent-white"
            title="Volume"
          />
        </div>
      </div>
      <VideoControls
        onPlay={play}
        onPause={pause}
        onNextFrame={nextFrame}
        onPrevFrame={prevFrame}
        onSeek={seekTo}
      />
    </div>
  );
}
