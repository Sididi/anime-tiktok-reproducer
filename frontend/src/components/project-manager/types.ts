export type SortColumn =
  | "uploaded"
  | "language"
  | "library_type"
  | "anime_title"
  | "local_size_bytes"
  | "scheduled_at"
  | "created_at";

export type SortDirection = "asc" | "desc";

export type UploadMode = "auto" | "scheduled" | "urgent";

export interface AnchorPayload {
  tiktok_slot: string;
  overrides?: Partial<Record<import("@/types").Platform, string>>;
  steals?: Partial<Record<import("@/types").Platform, import("@/types").StealSpec>>;
}
