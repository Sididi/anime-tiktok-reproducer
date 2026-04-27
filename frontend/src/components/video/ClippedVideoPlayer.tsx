import { forwardRef, useImperativeHandle, useRef } from "react";
import {
  ManagedVideoPlayer,
  type ManagedVideoPlayerHandle,
  type WaitUntilReadyOptions,
} from "./ManagedVideoPlayer";

export interface ClippedVideoPlayerProps {
  src: string;
  fallbackSrc?: string | null;
  startTime: number;
  endTime: number;
  className?: string;
  muted?: boolean;
  playbackRate?: number;
  onClipEnded?: () => void;
  eager?: boolean;
  loadStallTimeoutMs?: number | null;
  requestLoad?: boolean;
  requestWarmup?: boolean;
  leasePriority?: number;
  warmupPriority?: number;
  dedicatedAudio?: boolean;
  disableInteraction?: boolean;
  placeholderLabel?: string;
}

export interface ClippedVideoPlayerHandle {
  playFromStart: () => void;
  seekToStart: () => Promise<void>;
  play: () => void;
  playChecked: () => Promise<boolean>;
  pause: () => void;
  waitUntilReady: (options?: WaitUntilReadyOptions) => Promise<void>;
  hasLoadError: () => boolean;
  isPlaying: () => boolean;
  getReadyState: () => number;
  retryLoad: () => Promise<void>;
  forceLoad: () => void;
  releaseLoad: () => void;
  releaseAndPreventReload: () => void;
}

export type { WaitUntilReadyOptions };

export const ClippedVideoPlayer = forwardRef<
  ClippedVideoPlayerHandle,
  ClippedVideoPlayerProps
>(function ClippedVideoPlayer(
  {
    src,
    fallbackSrc,
    startTime,
    endTime,
    className,
    muted = true,
    playbackRate = 1,
    onClipEnded,
    eager = false,
    loadStallTimeoutMs,
    requestLoad = true,
    requestWarmup = false,
    leasePriority = 100,
    warmupPriority = 100,
    dedicatedAudio = false,
    disableInteraction = false,
    placeholderLabel = "Media deferred",
  },
  ref,
) {
  void loadStallTimeoutMs;
  const innerRef = useRef<ManagedVideoPlayerHandle>(null);

  useImperativeHandle(
    ref,
    () => ({
      playFromStart: () => innerRef.current?.playFromStart(),
      seekToStart: () => innerRef.current?.seekToStart() ?? Promise.resolve(),
      play: () => innerRef.current?.play(),
      playChecked: () =>
        innerRef.current?.playChecked() ?? Promise.resolve(false),
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
      fallbackSrc={fallbackSrc}
      className={className}
      muted={muted}
      controls
      disableInteraction={disableInteraction}
      playbackRate={playbackRate}
      onClipEnded={onClipEnded}
      requestLoad={requestLoad}
      requestWarmup={eager || requestWarmup}
      attachedPriority={leasePriority}
      warmupPriority={warmupPriority}
      startTime={startTime}
      endTime={endTime}
      seekOffsetSeconds={0.067}
      dedicatedAudio={dedicatedAudio}
      placeholderLabel={placeholderLabel}
    />
  );
});
