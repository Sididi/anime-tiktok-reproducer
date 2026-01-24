import { useState, useCallback } from 'react';
import {
  ArrowLeftToLine,
  ArrowRightToLine,
  Scissors,
} from 'lucide-react';
import type { Scene } from '@/types';
import { Button, Input } from '@/components/ui';
import { formatTime, parseTime } from '@/utils';
import { cn } from '@/utils';

// Custom merge icons showing two rectangles becoming one
function MergePrevIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      {/* Left rectangle (will absorb) */}
      <rect x="2" y="6" width="8" height="12" rx="1" />
      {/* Right rectangle (dashed, being merged) */}
      <rect x="14" y="6" width="8" height="12" rx="1" strokeDasharray="2 2" opacity="0.5" />
      {/* Arrow pointing left */}
      <path d="M14 12 L11 12 M11 12 L13 10 M11 12 L13 14" />
    </svg>
  );
}

function MergeNextIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      {/* Left rectangle (dashed, being merged) */}
      <rect x="2" y="6" width="8" height="12" rx="1" strokeDasharray="2 2" opacity="0.5" />
      {/* Right rectangle (will absorb) */}
      <rect x="14" y="6" width="8" height="12" rx="1" />
      {/* Arrow pointing right */}
      <path d="M10 12 L13 12 M13 12 L11 10 M13 12 L11 14" />
    </svg>
  );
}

interface ScenePanelProps {
  scene: Scene | null;
  sceneCount: number;
  currentTime: number;
  onSetStart: (sceneIndex: number, time: number) => void;
  onSetEnd: (sceneIndex: number, time: number) => void;
  onMergePrev: (sceneIndex: number) => void;
  onMergeNext: (sceneIndex: number) => void;
  onSplit: (sceneIndex: number, time: number) => void;
  className?: string;
}

export function ScenePanel({
  scene,
  sceneCount,
  currentTime,
  onSetStart,
  onSetEnd,
  onMergePrev,
  onMergeNext,
  onSplit,
  className,
}: ScenePanelProps) {
  const [startInput, setStartInput] = useState('');
  const [endInput, setEndInput] = useState('');
  const [startError, setStartError] = useState(false);
  const [endError, setEndError] = useState(false);

  // Update inputs when scene changes
  const handleStartChange = useCallback(
    (value: string) => {
      setStartInput(value);
      setStartError(false);
    },
    []
  );

  const handleEndChange = useCallback(
    (value: string) => {
      setEndInput(value);
      setEndError(false);
    },
    []
  );

  const handleStartBlur = useCallback(() => {
    if (!scene || !startInput) {
      setStartInput('');
      return;
    }
    const time = parseTime(startInput);
    if (time === null || time < 0 || time >= scene.end_time) {
      setStartError(true);
      return;
    }
    onSetStart(scene.index, time);
    setStartInput('');
  }, [scene, startInput, onSetStart]);

  const handleEndBlur = useCallback(() => {
    if (!scene || !endInput) {
      setEndInput('');
      return;
    }
    const time = parseTime(endInput);
    if (time === null || time <= scene.start_time) {
      setEndError(true);
      return;
    }
    onSetEnd(scene.index, time);
    setEndInput('');
  }, [scene, endInput, onSetEnd]);

  if (!scene) {
    return (
      <div className={cn('p-4 bg-[hsl(var(--card))] rounded-lg', className)}>
        <p className="text-[hsl(var(--muted-foreground))] text-sm">No scene selected</p>
      </div>
    );
  }

  const canSplit = currentTime > scene.start_time && currentTime < scene.end_time;
  const canMergePrev = scene.index > 0;
  const canMergeNext = scene.index < sceneCount - 1;

  return (
    <div className={cn('p-4 bg-[hsl(var(--card))] rounded-lg space-y-4', className)}>
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold">Scene {scene.index + 1}</h3>
        <span className="text-sm text-[hsl(var(--muted-foreground))]">
          Duration: {formatTime(scene.end_time - scene.start_time)}
        </span>
      </div>

      {/* Timing inputs */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-[hsl(var(--muted-foreground))] mb-1 block">Start</label>
          <Input
            value={startInput || formatTime(scene.start_time)}
            onChange={(e) => handleStartChange(e.target.value)}
            onBlur={handleStartBlur}
            onFocus={() => setStartInput(formatTime(scene.start_time))}
            className={cn('font-mono text-sm', startError && 'border-[hsl(var(--destructive))]')}
          />
        </div>
        <div>
          <label className="text-xs text-[hsl(var(--muted-foreground))] mb-1 block">End</label>
          <Input
            value={endInput || formatTime(scene.end_time)}
            onChange={(e) => handleEndChange(e.target.value)}
            onBlur={handleEndBlur}
            onFocus={() => setEndInput(formatTime(scene.end_time))}
            className={cn('font-mono text-sm', endError && 'border-[hsl(var(--destructive))]')}
          />
        </div>
      </div>

      {/* Control buttons */}
      <div className="flex items-center justify-between gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => onSetStart(scene.index, currentTime)}
          title="Set start to current time (A)"
        >
          <ArrowLeftToLine className="h-4 w-4" />
        </Button>

        <Button
          variant="outline"
          size="sm"
          onClick={() => onMergePrev(scene.index)}
          disabled={!canMergePrev}
          title="Merge with previous scene (Q)"
        >
          <MergePrevIcon className="h-4 w-4" />
        </Button>

        <Button
          variant="outline"
          size="sm"
          onClick={() => onSplit(scene.index, currentTime)}
          disabled={!canSplit}
          title="Split scene at current time (E)"
        >
          <Scissors className="h-4 w-4" />
        </Button>

        <Button
          variant="outline"
          size="sm"
          onClick={() => onMergeNext(scene.index)}
          disabled={!canMergeNext}
          title="Merge with next scene (D)"
        >
          <MergeNextIcon className="h-4 w-4" />
        </Button>

        <Button
          variant="outline"
          size="sm"
          onClick={() => onSetEnd(scene.index, currentTime)}
          title="Set end to current time (Z)"
        >
          <ArrowRightToLine className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
