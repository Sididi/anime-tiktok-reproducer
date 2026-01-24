import { useMemo } from 'react';
import { formatTime } from '@/utils';

interface TimeRulerProps {
  duration: number;
  visibleStart?: number;
  visibleEnd?: number;
}

export function TimeRuler({ duration, visibleStart = 0, visibleEnd }: TimeRulerProps) {
  const effectiveVisibleEnd = visibleEnd ?? duration;
  const visibleDuration = effectiveVisibleEnd - visibleStart;

  const marks = useMemo(() => {
    if (duration <= 0 || visibleDuration <= 0) return [];

    // Calculate appropriate interval based on visible duration (affected by zoom)
    let interval: number;
    if (visibleDuration <= 5) interval = 0.5;
    else if (visibleDuration <= 10) interval = 1;
    else if (visibleDuration <= 30) interval = 5;
    else if (visibleDuration <= 60) interval = 10;
    else if (visibleDuration <= 180) interval = 30;
    else interval = 60;

    const result: { time: number; label: string; position: number }[] = [];
    
    // Start from the first interval mark at or after visibleStart
    const startMark = Math.ceil(visibleStart / interval) * interval;
    
    for (let t = startMark; t <= effectiveVisibleEnd; t += interval) {
      const position = ((t - visibleStart) / visibleDuration) * 100;
      if (position >= 0 && position <= 100) {
        result.push({ time: t, label: formatTime(t), position });
      }
    }
    return result;
  }, [duration, visibleStart, effectiveVisibleEnd, visibleDuration]);

  return (
    <div className="relative h-5 bg-[hsl(var(--muted))] text-xs text-[hsl(var(--muted-foreground))] rounded-t-lg overflow-hidden">
      {marks.map(({ time, label, position }) => (
        <div
          key={time}
          className="absolute top-0 h-full flex flex-col justify-end"
          style={{ left: `${position}%` }}
        >
          <div className="w-px h-2 bg-[hsl(var(--border))]" />
          <span className="absolute bottom-0 left-1 whitespace-nowrap text-[10px]">{label}</span>
        </div>
      ))}
    </div>
  );
}
