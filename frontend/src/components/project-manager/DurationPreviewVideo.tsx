import { useState } from "react";
import { Loader2 } from "lucide-react";

interface DurationPreviewVideoProps {
  src: string;
  maxDuration?: number;
  playbackRate?: number;
}

type MediaState = "loading" | "ready" | "error";

export function DurationPreviewVideo({
  src,
  maxDuration,
  playbackRate,
}: DurationPreviewVideoProps) {
  const [mediaState, setMediaState] = useState<MediaState>("loading");

  return (
    <>
      <video
        src={src}
        className={`w-full h-full object-contain transition-opacity ${
          mediaState === "ready" ? "opacity-100" : "opacity-0"
        }`}
        controls
        preload="metadata"
        onLoadedMetadata={(event) => {
          const video = event.currentTarget;
          if (playbackRate !== undefined) {
            video.playbackRate = playbackRate;
          }
          // Metadata-only preload does not guarantee that Chromium decodes a
          // poster frame. A tiny seek requests and paints the first keyframe
          // without preloading the full video.
          if (Number.isFinite(video.duration) && video.duration > 0.01) {
            video.currentTime = Math.min(0.01, video.duration / 2);
          }
        }}
        onLoadedData={() => setMediaState("ready")}
        onError={() => setMediaState("error")}
        onTimeUpdate={(event) => {
          if (
            maxDuration !== undefined &&
            event.currentTarget.currentTime >= maxDuration
          ) {
            event.currentTarget.pause();
            event.currentTarget.currentTime = maxDuration;
          }
        }}
      />
      {mediaState !== "ready" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-white/70">
          {mediaState === "loading" ? (
            <>
              <Loader2 className="h-6 w-6 animate-spin" />
              <span className="text-xs">Chargement de l'aperçu...</span>
            </>
          ) : (
            <span className="text-xs">Aperçu indisponible</span>
          )}
        </div>
      )}
    </>
  );
}
