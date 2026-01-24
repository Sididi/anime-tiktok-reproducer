import { useState, useEffect, useCallback } from 'react';
import { Play, Pause, ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui';
import { useVideoStore } from '@/stores';
import { formatTime, parseTime } from '@/utils';

interface VideoControlsProps {
  onPlay: () => void;
  onPause: () => void;
  onNextFrame: () => void;
  onPrevFrame: () => void;
  onSeek?: (time: number) => void;
}

export function VideoControls({ onPlay, onPause, onNextFrame, onPrevFrame, onSeek }: VideoControlsProps) {
  const { currentTime, duration, isPlaying } = useVideoStore();
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState('');

  // Update edit value when currentTime changes (and not editing)
  useEffect(() => {
    if (!isEditing) {
      setEditValue(formatTime(currentTime));
    }
  }, [currentTime, isEditing]);

  const handleStartEdit = useCallback(() => {
    setEditValue(formatTime(currentTime));
    setIsEditing(true);
  }, [currentTime]);

  const handleSubmit = useCallback(() => {
    const parsedTime = parseTime(editValue);
    if (parsedTime !== null && onSeek) {
      // Clamp to valid range
      const clampedTime = Math.max(0, Math.min(parsedTime, duration));
      onSeek(clampedTime);
    }
    setIsEditing(false);
  }, [editValue, duration, onSeek]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSubmit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      setIsEditing(false);
      setEditValue(formatTime(currentTime));
    }
    // Stop propagation to prevent global keyboard handlers
    e.stopPropagation();
  }, [handleSubmit, currentTime]);

  const handleBlur = useCallback(() => {
    handleSubmit();
  }, [handleSubmit]);

  return (
    <div className="flex items-center gap-2 p-2 bg-[hsl(var(--card))] rounded-lg">
      <Button variant="ghost" size="icon" onClick={onPrevFrame} title="Previous frame (←)">
        <ChevronLeft className="h-5 w-5" />
      </Button>

      <Button
        variant="default"
        size="icon"
        onClick={isPlaying ? onPause : onPlay}
        title={isPlaying ? 'Pause (Space)' : 'Play (Space)'}
      >
        {isPlaying ? <Pause className="h-5 w-5" /> : <Play className="h-5 w-5" />}
      </Button>

      <Button variant="ghost" size="icon" onClick={onNextFrame} title="Next frame (→)">
        <ChevronRight className="h-5 w-5" />
      </Button>

      <div className="ml-4 text-sm font-mono text-[hsl(var(--muted-foreground))]">
        {isEditing ? (
          <input
            type="text"
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={handleBlur}
            autoFocus
            className="w-20 px-1 py-0.5 bg-[hsl(var(--input))] border border-[hsl(var(--border))] rounded text-[hsl(var(--foreground))] text-center focus:outline-none focus:ring-1 focus:ring-[hsl(var(--ring))]"
            placeholder="00:00.00"
          />
        ) : (
          <span 
            className="text-[hsl(var(--foreground))] cursor-pointer hover:bg-[hsl(var(--muted))] px-1 py-0.5 rounded"
            onClick={handleStartEdit}
            title="Click to edit time"
          >
            {formatTime(currentTime)}
          </span>
        )}
        <span className="mx-1">/</span>
        <span>{formatTime(duration)}</span>
      </div>
    </div>
  );
}
