import { useCallback, useRef, useEffect } from 'react';
import { useVideoStore } from '@/stores';

export function useVideoPlayer() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const { currentTime, fps, isPlaying, setCurrentTime, setIsPlaying, setDuration } =
    useVideoStore();

  // Sync video element time updates to store
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
      videoRef.current.currentTime = Math.max(0, Math.min(time, videoRef.current.duration));
      setCurrentTime(videoRef.current.currentTime);
    }
  }, [setCurrentTime]);

  const nextFrame = useCallback(() => {
    if (videoRef.current) {
      videoRef.current.pause();
      const frameTime = 1 / fps;
      const newTime = Math.min(videoRef.current.currentTime + frameTime, videoRef.current.duration);
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

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Don't capture if user is typing in an input
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) {
        return;
      }

      switch (e.code) {
        case 'Space':
          e.preventDefault();
          togglePlay();
          break;
        case 'ArrowLeft':
          e.preventDefault();
          prevFrame();
          break;
        case 'ArrowRight':
          e.preventDefault();
          nextFrame();
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [togglePlay, nextFrame, prevFrame]);

  return {
    videoRef,
    currentTime,
    isPlaying,
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
}
