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
 * Categorize based on clip speed ratio (estimated / original).
 * Sped up (ratio > 1): 1.0–1.75 green, 1.75–2.0 yellow, 2.0+ red
 * Slowed down (ratio < 1): 0.85–1.0 green, 0.75–0.85 yellow, <0.75 red
 */
export function getSpeedCategory(speedRatio: number): DeltaCategory {
  if (speedRatio >= 1) {
    if (speedRatio <= 1.75) return "green";
    if (speedRatio <= 2.0) return "yellow";
    return "red";
  } else {
    if (speedRatio >= 0.85) return "green";
    if (speedRatio >= 0.75) return "yellow";
    return "red";
  }
}

export const DELTA_COLORS: Record<DeltaCategory, string> = {
  green: "text-green-500",
  yellow: "text-yellow-500",
  red: "text-red-500",
};
