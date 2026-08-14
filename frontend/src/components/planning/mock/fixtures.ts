import type { PlanningEvent, Platform } from "@/types";
import { addDaysParis, parisDayKey, parisWallTimeToUtcIso } from "@/utils/parisTime";

/**
 * Realistic fake data for the M1 static mockup (and later e2e fixtures).
 * Dates are generated relative to "now" so the board is always populated.
 */

export interface MockAccount {
  id: string;
  name: string;
  language: string;
}

export const MOCK_ACCOUNTS: MockAccount[] = [
  { id: "naruto-fr", name: "narutoclips.fr", language: "fr" },
  { id: "anime-en", name: "animeverse.en", language: "en" },
];

function at(dayOffset: number, hhmm: string): string {
  return parisWallTimeToUtcIso(parisDayKey(addDaysParis(new Date(), dayOffset)), hhmm);
}

interface EventSpec {
  project_id: string;
  anime_title: string;
  account: MockAccount;
  slot: string;
  platforms: Partial<Record<Platform, PlanningEvent["status"]>>;
  manual?: boolean;
  timing_locked?: boolean;
}

function make(spec: EventSpec): PlanningEvent[] {
  return (Object.entries(spec.platforms) as [Platform, PlanningEvent["status"]][]).map(
    ([platform, status]) => ({
      project_id: spec.project_id,
      anime_title: spec.anime_title,
      account_id: spec.account.id,
      account_name: spec.account.name,
      account_avatar_url: `/api/accounts/${spec.account.id}/avatar`,
      platform,
      slot: spec.slot,
      scheduled_at: spec.slot,
      drive_folder_url: "https://drive.google.com/drive/folders/mock",
      status,
      manual: spec.manual ?? false,
      timing_locked: spec.timing_locked ?? false,
    }),
  );
}

const [ACC_FR, ACC_EN] = MOCK_ACCOUNTS;

export const MOCK_EVENTS: PlanningEvent[] = [
  ...make({
    project_id: "p-naruto-trahison",
    anime_title: "Naruto : la trahison de Sasuke",
    account: ACC_FR,
    slot: at(-2, "18:00"),
    platforms: { tiktok: "complete", youtube: "complete", instagram: "complete", facebook: "complete" },
  }),
  ...make({
    project_id: "p-onepiece-kaido",
    anime_title: "One Piece : Luffy vs Kaido",
    account: ACC_FR,
    slot: at(-1, "12:15"),
    platforms: { tiktok: "failed", youtube: "complete", instagram: "complete" },
  }),
  ...make({
    project_id: "p-bleach-bankai",
    anime_title: "Bleach : le Bankai final",
    account: ACC_EN,
    slot: at(-1, "19:00"),
    platforms: { tiktok: "scheduled", youtube: "scheduled" },
  }),
  ...make({
    project_id: "p-jjk-gojo",
    anime_title: "Jujutsu Kaisen : Gojo déchaîné",
    account: ACC_FR,
    slot: at(0, "12:00"),
    platforms: { tiktok: "running", youtube: "scheduled", instagram: "scheduled" },
  }),
  ...make({
    project_id: "p-demonslayer-feu",
    anime_title: "Demon Slayer : la danse du dieu du feu",
    account: ACC_FR,
    slot: at(0, "18:00"),
    platforms: { tiktok: "scheduled", instagram: "scheduled" },
    timing_locked: true,
  }),
  ...make({
    project_id: "p-chainsaw-pochita",
    anime_title: "Chainsaw Man : le pacte avec Pochita",
    account: ACC_EN,
    slot: at(1, "09:30"),
    platforms: { tiktok: "scheduled", youtube: "scheduled" },
    manual: true,
  }),
  ...make({
    project_id: "p-frieren-voyage",
    anime_title: "Frieren : au-delà du voyage",
    account: ACC_FR,
    slot: at(1, "18:00"),
    platforms: { tiktok: "scheduled", youtube: "scheduled", instagram: "scheduled", facebook: "scheduled" },
  }),
  // Same project split across two instants → two cards on the board.
  ...make({
    project_id: "p-snk-grondement",
    anime_title: "SNK : le Grondement de la Terre",
    account: ACC_FR,
    slot: at(2, "18:00"),
    platforms: { tiktok: "scheduled" },
  }),
  ...make({
    project_id: "p-snk-grondement",
    anime_title: "SNK : le Grondement de la Terre",
    account: ACC_FR,
    slot: at(2, "19:00"),
    platforms: { youtube: "scheduled", instagram: "scheduled" },
  }),
  ...make({
    project_id: "p-sololeveling-eveil",
    anime_title: "Solo Leveling : l'éveil du chasseur",
    account: ACC_EN,
    slot: at(3, "17:45"),
    platforms: { tiktok: "scheduled", youtube: "scheduled", instagram: "scheduled" },
  }),
  ...make({
    project_id: "p-mobpsycho",
    anime_title: "Mob Psycho 100 : 100 %",
    account: ACC_FR,
    slot: at(4, "21:00"),
    platforms: { tiktok: "scheduled" },
  }),
];

export interface MockFreeSlot {
  platform: Platform;
  slot: string;
}

/** Free configured slots for the selected account (mock: naruto-fr). */
export const MOCK_FREE_SLOTS: MockFreeSlot[] = [
  { platform: "tiktok", slot: at(0, "21:00") },
  { platform: "tiktok", slot: at(1, "12:00") },
  { platform: "youtube", slot: at(1, "12:00") },
  { platform: "tiktok", slot: at(2, "12:00") },
  { platform: "tiktok", slot: at(3, "18:00") },
  { platform: "instagram", slot: at(3, "18:00") },
  { platform: "tiktok", slot: at(4, "12:00") },
  { platform: "tiktok", slot: at(4, "18:00") },
  { platform: "tiktok", slot: at(5, "12:00") },
  { platform: "tiktok", slot: at(5, "18:00") },
  { platform: "tiktok", slot: at(5, "21:00") },
  { platform: "tiktok", slot: at(6, "18:00") },
];

/** Projects ready to schedule (quick-assign mock). */
export const MOCK_ELIGIBLE_PROJECTS = [
  { id: "p-vinland-thorfinn", title: "Vinland Saga : la vengeance de Thorfinn", language: "fr", created_at: at(-6, "10:00") },
  { id: "p-hxh-nen", title: "Hunter x Hunter : le pouvoir du Nen", language: "fr", created_at: at(-4, "15:00") },
  { id: "p-spyfamily-anya", title: "Spy x Family : le secret d'Anya", language: "en", created_at: at(-2, "09:00") },
];
