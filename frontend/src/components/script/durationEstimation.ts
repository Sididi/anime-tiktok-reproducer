// Words per minute by language
const WPM_RATES: Record<string, number> = {
  fr: 160,
  en: 170,
  es: 165,
};

// ElevenLabs speaks slightly faster than natural pace
const ELEVENLABS_SPEED_FACTOR = 1.15;

/**
 * Estimate TTS duration for a text in seconds.
 */
export function estimateTtsDuration(text: string, language: string): number {
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  if (words === 0) return 0;
  const wpm = WPM_RATES[language] || 170;
  return (words / wpm) * 60 / ELEVENLABS_SPEED_FACTOR;
}

export type DeltaCategory = "green" | "yellow" | "red";

/**
 * Categorize the absolute delta percentage.
 */
export function getDeltaCategory(deltaPct: number): DeltaCategory {
  const abs = Math.abs(deltaPct);
  if (abs < 15) return "green";
  if (abs < 30) return "yellow";
  return "red";
}

export const DELTA_COLORS: Record<DeltaCategory, string> = {
  green: "text-green-500",
  yellow: "text-yellow-500",
  red: "text-red-500",
};
