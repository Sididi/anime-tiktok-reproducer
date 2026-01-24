import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatTime(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  const ms = Math.floor((seconds % 1) * 100);
  return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}.${ms.toString().padStart(2, '0')}`;
}

export function parseTime(timeStr: string): number | null {
  const match = timeStr.match(/^(\d+):(\d{2})(?:\.(\d{1,2}))?$/);
  if (!match) return null;

  const mins = parseInt(match[1], 10);
  const secs = parseInt(match[2], 10);
  const ms = match[3] ? parseInt(match[3].padEnd(2, '0'), 10) : 0;

  if (secs >= 60) return null;

  return mins * 60 + secs + ms / 100;
}
