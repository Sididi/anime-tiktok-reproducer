import type {RecordingCut} from '@/api/facecam';

/** Match the server's inward frame rounding and union overlapping removals. */
export function removedIntervals(cuts: RecordingCut[]): {start: number; end: number}[] {
  const result: {start: number; end: number}[] = [];
  const sorted = cuts.filter(c => c.enabled).map(c => ({start: Math.ceil(c.start * 60 - 1e-7) / 60,
    end: Math.floor(c.end * 60 + 1e-7) / 60})).filter(c => c.end > c.start).sort((a, b) => a.start - b.start);
  for (const cut of sorted) {
    const previous = result.at(-1);
    if (previous && cut.start <= previous.end) previous.end = Math.max(previous.end, cut.end);
    else result.push({...cut});
  }
  return result;
}

export function cutTime(seconds: number) {
  const ticks = Math.round(Math.max(0, seconds) * 100);
  return `${Math.floor(ticks / 6000)}:${String(Math.floor(ticks / 100) % 60).padStart(2, '0')}.${String(ticks % 100).padStart(2, '0')}`;
}
