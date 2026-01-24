import { useRef, useEffect, useCallback, useState, useImperativeHandle, forwardRef } from 'react';
import { RotateCcw } from 'lucide-react';
import { cn } from '@/utils';

interface ClippedVideoPlayerProps {
  src: string;
  startTime: number;
  endTime: number;
  className?: string;
}

export interface ClippedVideoPlayerHandle {
  play: () => void;
  pause: () => void;
  reset: () => void;
}

/**
 * A video player that strictly clips playback between startTime and endTime.
 * - Starts at startTime when loaded
 * - Pauses at endTime
 * - Allows manual replay from startTime
 */
export const ClippedVideoPlayer = forwardRef<ClippedVideoPlayerHandle, ClippedVideoPlayerProps>(
  function ClippedVideoPlayer({ src, startTime, endTime, className }, ref) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [isEnded, setIsEnded] = useState(false);

  // Expose imperative methods for sync play
  useImperativeHandle(ref, () => ({
    play: () => {
      if (videoRef.current) {
        videoRef.current.currentTime = startTime;
        videoRef.current.play();
        setIsEnded(false);
      }
    },
    pause: () => {
      if (videoRef.current) {
        videoRef.current.pause();
      }
    },
    reset: () => {
      if (videoRef.current) {
        videoRef.current.currentTime = startTime;
        setIsEnded(false);
      }
    },
  }), [startTime]);

  // Set initial time when video metadata loads
  const handleLoadedMetadata = useCallback(() => {
    if (videoRef.current) {
      videoRef.current.currentTime = startTime;
    }
  }, [startTime]);

  // Monitor playback to enforce end boundary
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const handleTimeUpdate = () => {
      if (video.currentTime >= endTime) {
        video.pause();
        video.currentTime = endTime;
        setIsEnded(true);
      }
    };

    const handlePlay = () => {
      setIsEnded(false);
    };

    const handleSeeked = () => {
      // If user seeks before start, reset to start
      if (video.currentTime < startTime) {
        video.currentTime = startTime;
      }
      // If user seeks past end, set to end
      if (video.currentTime > endTime) {
        video.currentTime = endTime;
        setIsEnded(true);
      }
    };

    video.addEventListener('timeupdate', handleTimeUpdate);
    video.addEventListener('play', handlePlay);
    video.addEventListener('seeked', handleSeeked);

    return () => {
      video.removeEventListener('timeupdate', handleTimeUpdate);
      video.removeEventListener('play', handlePlay);
      video.removeEventListener('seeked', handleSeeked);
    };
  }, [startTime, endTime]);

  // Reset to start when src or times change
  useEffect(() => {
    if (videoRef.current) {
      videoRef.current.currentTime = startTime;
      setIsEnded(false);
    }
  }, [src, startTime]);

  const handleReplay = useCallback(() => {
    if (videoRef.current) {
      videoRef.current.currentTime = startTime;
      videoRef.current.play();
      setIsEnded(false);
    }
  }, [startTime]);

  return (
    <div className={cn('relative group', className)}>
      <video
        ref={videoRef}
        src={src}
        className="w-full h-full object-contain"
        onLoadedMetadata={handleLoadedMetadata}
        preload="metadata"
        controls
        muted
      />
      
      {/* Replay overlay when ended */}
      {isEnded && (
        <div 
          className="absolute inset-0 bg-black/50 flex items-center justify-center cursor-pointer"
          onClick={handleReplay}
        >
          <div className="flex flex-col items-center gap-2 text-white">
            <RotateCcw className="h-10 w-10" />
            <span className="text-sm">Replay</span>
          </div>
        </div>
      )}
    </div>
  );
});
