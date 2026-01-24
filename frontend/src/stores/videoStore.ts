import { create } from 'zustand';

interface VideoState {
  // Video element ref is managed via React ref, not here
  currentTime: number;
  duration: number;
  fps: number;
  isPlaying: boolean;

  // Actions
  setCurrentTime: (time: number) => void;
  setDuration: (duration: number) => void;
  setFps: (fps: number) => void;
  setIsPlaying: (playing: boolean) => void;

  // Frame navigation helpers
  getCurrentFrame: () => number;
  getFrameTime: (frame: number) => number;
}

export const useVideoStore = create<VideoState>((set, get) => ({
  currentTime: 0,
  duration: 0,
  fps: 30,
  isPlaying: false,

  setCurrentTime: (time: number) => set({ currentTime: time }),
  setDuration: (duration: number) => set({ duration }),
  setFps: (fps: number) => set({ fps }),
  setIsPlaying: (playing: boolean) => set({ isPlaying: playing }),

  getCurrentFrame: () => {
    const { currentTime, fps } = get();
    return Math.floor(currentTime * fps);
  },

  getFrameTime: (frame: number) => {
    const { fps } = get();
    return frame / fps;
  },
}));
