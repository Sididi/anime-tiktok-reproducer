import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

interface DurationPreviewVideoProps {
  src: string;
  maxDuration?: number;
  playbackRate?: number;
}

type MediaState = "loading" | "ready" | "error";

const MAX_LOAD_RETRIES = 3;
const RETRY_DELAY_MS = 2000;

export function DurationPreviewVideo({
  src,
  maxDuration,
  playbackRate,
}: DurationPreviewVideoProps) {
  const [mediaState, setMediaState] = useState<MediaState>("loading");
  const [attempt, setAttempt] = useState(0);
  const retryTimer = useRef<number>();

  useEffect(() => {
    return () => {
      if (retryTimer.current !== undefined) {
        window.clearTimeout(retryTimer.current);
      }
    };
  }, []);

  // A distinct query param forces the browser to re-request the file instead
  // of replaying the failed response from its cache.
  const effectiveSrc =
    attempt === 0
      ? src
      : `${src}${src.includes("?") ? "&" : "?"}retry=${attempt}`;

  return (
    <>
      <video
        key={effectiveSrc}
        src={effectiveSrc}
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
        onError={() => {
          // The cache file may have just been finalized server-side (or the
          // request hit a transient hiccup): retry before giving up.
          if (attempt < MAX_LOAD_RETRIES) {
            retryTimer.current = window.setTimeout(() => {
              setAttempt((a) => a + 1);
              setMediaState("loading");
            }, RETRY_DELAY_MS);
          } else {
            setMediaState("error");
          }
        }}
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
