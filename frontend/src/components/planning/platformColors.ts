import type { Platform } from "@/types";

const HSL_TUPLES: Record<Platform, string> = {
  youtube: "var(--platform-youtube)",
  facebook: "var(--platform-facebook)",
  instagram: "var(--platform-instagram)",
  tiktok: "var(--platform-tiktok)",
};

export function platformBgHsl(platform: Platform): string {
  return `hsl(${HSL_TUPLES[platform]})`;
}

export function platformTranslucentHsl(platform: Platform, alpha = 0.18): string {
  return `hsl(${HSL_TUPLES[platform]} / ${alpha})`;
}

export const PLATFORM_LABELS: Record<Platform, string> = {
  youtube: "YouTube",
  facebook: "Facebook",
  instagram: "Instagram",
  tiktok: "TikTok",
};

export const PLATFORM_SHORT: Record<Platform, string> = {
  youtube: "YT",
  facebook: "FB",
  instagram: "IG",
  tiktok: "TT",
};
