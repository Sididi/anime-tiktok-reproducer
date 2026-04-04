import { forwardRef, useImperativeHandle, useRef } from "react";
import {
  ManagedVideoPlayer,
  type ManagedVideoPlayerHandle,
  type WaitUntilReadyOptions,
} from "./ManagedVideoPlayer";

export interface MatchesClipWaitUntilReadyOptions
  extends WaitUntilReadyOptions {}

export interface MatchesClipPlayerHandle {
  playFromStart: () => void;
  seekToStart: () => Promise<void>;
  play: () => void;
  pause: () => void;
  waitUntilReady: (
    options?: MatchesClipWaitUntilReadyOptions,
  ) => Promise<void>;
  hasLoadError: () => boolean;
  isPlaying: () => boolean;
  getReadyState: () => number;
  retryLoad: () => Promise<void>;
  forceLoad: () => void;
  releaseLoad: () => void;
  releaseAndPreventReload: () => void;
}

interface MatchesClipPlayerProps {
  src: string;
  className?: string;
  muted?: boolean;
  playbackRate?: number;
  onClipEnded?: () => void;
  controls?: boolean;
  preloadMode?: "none" | "metadata" | "auto";
  disableInteraction?: boolean;
  requestLoad?: boolean;
  requestWarmup?: boolean;
  leasePriority?: number;
  warmupPriority?: number;
  placeholderLabel?: string;
}

export const MatchesClipPlayer = forwardRef<
  MatchesClipPlayerHandle,
  MatchesClipPlayerProps
>(function MatchesClipPlayer(
  {
    src,
    className,
    muted = true,
    playbackRate = 1,
    onClipEnded,
    controls = true,
    preloadMode = "metadata",
    disableInteraction = false,
    requestLoad,
    requestWarmup,
    leasePriority = 100,
    warmupPriority = 100,
    placeholderLabel = "Media deferred",
  },
  ref,
) {
  const innerRef = useRef<ManagedVideoPlayerHandle>(null);
  const effectiveRequestLoad = requestLoad ?? preloadMode !== "none";
  const effectiveRequestWarmup =
    requestWarmup ?? preloadMode === "auto";

  useImperativeHandle(
    ref,
    () => ({
      playFromStart: () => innerRef.current?.playFromStart(),
      seekToStart: () => innerRef.current?.seekToStart() ?? Promise.resolve(),
      play: () => innerRef.current?.play(),
      pause: () => innerRef.current?.pause(),
      waitUntilReady: (options) =>
        innerRef.current?.waitUntilReady(options) ?? Promise.resolve(),
      hasLoadError: () => innerRef.current?.hasLoadError() ?? true,
      isPlaying: () => innerRef.current?.isPlaying() ?? false,
      getReadyState: () =>
        innerRef.current?.getReadyState() ?? HTMLMediaElement.HAVE_NOTHING,
      retryLoad: () => innerRef.current?.retryLoad() ?? Promise.resolve(),
      forceLoad: () => innerRef.current?.forceLoad(),
      releaseLoad: () => innerRef.current?.releaseLoad(),
      releaseAndPreventReload: () =>
        innerRef.current?.releaseAndPreventReload(),
    }),
    [],
  );

  return (
    <ManagedVideoPlayer
      ref={innerRef}
      src={src}
      className={className}
      muted={muted}
      controls={controls}
      disableInteraction={disableInteraction}
      playbackRate={playbackRate}
      onClipEnded={onClipEnded}
      requestLoad={effectiveRequestLoad}
      requestWarmup={effectiveRequestWarmup}
      attachedPriority={leasePriority}
      warmupPriority={warmupPriority}
      placeholderLabel={placeholderLabel}
    />
  );
});
