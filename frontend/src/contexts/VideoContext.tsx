import { createContext, useContext, useRef, useCallback, type ReactNode } from 'react';
import { useVideoStore } from '@/stores';

interface VideoContextValue {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  play: () => void;
  pause: () => void;
  togglePlay: () => void;
  seekTo: (time: number) => void;
  nextFrame: () => void;
  prevFrame: () => void;
  handlers: {
    onTimeUpdate: () => void;
    onLoadedMetadata: () => void;
    onPlay: () => void;
    onPause: () => void;
  };
}

const VideoContext = createContext<VideoContextValue | null>(null);

interface VideoProviderProps {
  children: ReactNode;
}

export function VideoProvider({ children }: VideoProviderProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const { fps, setCurrentTime, setIsPlaying, setDuration } = useVideoStore();

  // Event handlers
  const handleTimeUpdate = useCallback(() => {
    if (videoRef.current) {
      setCurrentTime(videoRef.current.currentTime);
    }
  }, [setCurrentTime]);

  const handleLoadedMetadata = useCallback(() => {
    if (videoRef.current) {
      setDuration(videoRef.current.duration);
    }
  }, [setDuration]);

  const handlePlay = useCallback(() => setIsPlaying(true), [setIsPlaying]);
  const handlePause = useCallback(() => setIsPlaying(false), [setIsPlaying]);

  // Control functions
  const play = useCallback(() => {
    videoRef.current?.play();
  }, []);

  const pause = useCallback(() => {
    videoRef.current?.pause();
  }, []);

  const togglePlay = useCallback(() => {
    if (videoRef.current) {
      if (videoRef.current.paused) {
        videoRef.current.play();
      } else {
        videoRef.current.pause();
      }
    }
  }, []);

  const seekTo = useCallback((time: number) => {
    if (videoRef.current) {
      const duration = videoRef.current.duration || Infinity;
      videoRef.current.currentTime = Math.max(0, Math.min(time, duration));
      setCurrentTime(videoRef.current.currentTime);
    }
  }, [setCurrentTime]);

  const nextFrame = useCallback(() => {
    if (videoRef.current) {
      videoRef.current.pause();
      const frameTime = 1 / fps;
      const duration = videoRef.current.duration || Infinity;
      const newTime = Math.min(videoRef.current.currentTime + frameTime, duration);
      videoRef.current.currentTime = newTime;
      setCurrentTime(newTime);
    }
  }, [fps, setCurrentTime]);

  const prevFrame = useCallback(() => {
    if (videoRef.current) {
      videoRef.current.pause();
      const frameTime = 1 / fps;
      const newTime = Math.max(videoRef.current.currentTime - frameTime, 0);
      videoRef.current.currentTime = newTime;
      setCurrentTime(newTime);
    }
  }, [fps, setCurrentTime]);

  const value: VideoContextValue = {
    videoRef,
    play,
    pause,
    togglePlay,
    seekTo,
    nextFrame,
    prevFrame,
    handlers: {
      onTimeUpdate: handleTimeUpdate,
      onLoadedMetadata: handleLoadedMetadata,
      onPlay: handlePlay,
      onPause: handlePause,
    },
  };

  return (
    <VideoContext.Provider value={value}>
      {children}
    </VideoContext.Provider>
  );
}

export function useVideo() {
  const context = useContext(VideoContext);
  if (!context) {
    throw new Error('useVideo must be used within a VideoProvider');
  }
  return context;
}
