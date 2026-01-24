import { useState, useCallback, useRef, useEffect } from 'react';
import { X, Play, Pause, Check } from 'lucide-react';
import { Button, Input } from '@/components/ui';
import { formatTime, parseTime } from '@/utils';
import { api } from '@/api/client';
import type { Scene } from '@/types';

interface ManualMatchModalProps {
  isOpen: boolean;
  onClose: () => void;
  scene: Scene;
  projectId: string;
  episodes: string[];
  onSave: (episode: string, startTime: number, endTime: number) => void;
}

export function ManualMatchModal({
  isOpen,
  onClose,
  scene,
  projectId,
  episodes,
  onSave,
}: ManualMatchModalProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [selectedEpisode, setSelectedEpisode] = useState<string>(episodes[0] || '');
  const [startTime, setStartTime] = useState<string>('00:00.00');
  const [endTime, setEndTime] = useState<string>('00:00.00');
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);

  // Calculate scene duration for reference
  const sceneDuration = scene.end_time - scene.start_time;

  // Reset when episode changes
  useEffect(() => {
    setStartTime('00:00.00');
    setEndTime(formatTime(sceneDuration));
    setCurrentTime(0);
  }, [selectedEpisode, sceneDuration]);

  const handleTimeUpdate = useCallback(() => {
    if (videoRef.current) {
      setCurrentTime(videoRef.current.currentTime);
    }
  }, []);

  const handleLoadedMetadata = useCallback(() => {
    if (videoRef.current) {
      setDuration(videoRef.current.duration);
    }
  }, []);

  const handleSetStart = useCallback(() => {
    setStartTime(formatTime(currentTime));
  }, [currentTime]);

  const handleSetEnd = useCallback(() => {
    setEndTime(formatTime(currentTime));
  }, [currentTime]);

  const handleSeek = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const time = parseFloat(e.target.value);
    if (videoRef.current) {
      videoRef.current.currentTime = time;
      setCurrentTime(time);
    }
  }, []);

  const handlePlayPause = useCallback(() => {
    if (videoRef.current) {
      if (videoRef.current.paused) {
        videoRef.current.play();
        setIsPlaying(true);
      } else {
        videoRef.current.pause();
        setIsPlaying(false);
      }
    }
  }, []);

  const handlePreview = useCallback(() => {
    const start = parseTime(startTime);
    if (videoRef.current && start !== null) {
      videoRef.current.currentTime = start;
      videoRef.current.play();
      setIsPlaying(true);
    }
  }, [startTime]);

  const handleSave = useCallback(() => {
    const start = parseTime(startTime);
    const end = parseTime(endTime);
    if (start !== null && end !== null && selectedEpisode) {
      onSave(selectedEpisode, start, end);
      onClose();
    }
  }, [startTime, endTime, selectedEpisode, onSave, onClose]);

  const sourceVideoUrl = selectedEpisode 
    ? api.getSourceVideoUrl(projectId, selectedEpisode) 
    : '';

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
      <div className="bg-[hsl(var(--card))] rounded-lg w-full max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-[hsl(var(--border))]">
          <h2 className="text-lg font-semibold">Manual Match Selection - Scene {scene.index + 1}</h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-[hsl(var(--muted))] rounded"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* Reference info */}
          <div className="text-sm text-[hsl(var(--muted-foreground))]">
            Scene duration: <strong>{formatTime(sceneDuration)}</strong>
          </div>

          {/* Episode selector */}
          <div>
            <label className="block text-sm font-medium mb-1">Select Episode</label>
            <select
              value={selectedEpisode}
              onChange={(e) => setSelectedEpisode(e.target.value)}
              className="w-full p-2 bg-[hsl(var(--input))] border border-[hsl(var(--border))] rounded text-sm"
            >
              {episodes.map((ep) => (
                <option key={ep} value={ep}>
                  {ep.split('/').pop()}
                </option>
              ))}
            </select>
          </div>

          {/* Video preview */}
          {selectedEpisode && (
            <div className="space-y-2">
              <div className="aspect-video bg-black rounded overflow-hidden">
                <video
                  ref={videoRef}
                  src={sourceVideoUrl}
                  className="w-full h-full object-contain"
                  onTimeUpdate={handleTimeUpdate}
                  onLoadedMetadata={handleLoadedMetadata}
                  onPlay={() => setIsPlaying(true)}
                  onPause={() => setIsPlaying(false)}
                  muted
                />
              </div>

              {/* Video controls */}
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="icon" onClick={handlePlayPause}>
                  {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                </Button>
                <input
                  type="range"
                  min={0}
                  max={duration || 100}
                  step={0.01}
                  value={currentTime}
                  onChange={handleSeek}
                  className="flex-1"
                />
                <span className="text-sm font-mono w-24 text-right">
                  {formatTime(currentTime)} / {formatTime(duration)}
                </span>
              </div>
            </div>
          )}

          {/* Time inputs */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium mb-1">Start Time</label>
              <div className="flex gap-2">
                <Input
                  value={startTime}
                  onChange={(e) => setStartTime(e.target.value)}
                  placeholder="00:00.00"
                  className="flex-1 font-mono"
                />
                <Button variant="outline" size="sm" onClick={handleSetStart}>
                  Set Current
                </Button>
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">End Time</label>
              <div className="flex gap-2">
                <Input
                  value={endTime}
                  onChange={(e) => setEndTime(e.target.value)}
                  placeholder="00:00.00"
                  className="flex-1 font-mono"
                />
                <Button variant="outline" size="sm" onClick={handleSetEnd}>
                  Set Current
                </Button>
              </div>
            </div>
          </div>

          {/* Preview button */}
          <Button variant="outline" onClick={handlePreview} className="w-full">
            <Play className="h-4 w-4 mr-2" />
            Preview from Start Time
          </Button>
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-2 p-4 border-t border-[hsl(var(--border))]">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSave}>
            <Check className="h-4 w-4 mr-2" />
            Save Match
          </Button>
        </div>
      </div>
    </div>
  );
}
