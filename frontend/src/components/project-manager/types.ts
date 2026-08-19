export type SortColumn =
  | "uploaded"
  | "language"
  | "library_type"
  | "anime_title"
  | "local_size_bytes"
  | "scheduled_at"
  | "created_at";

export type SortDirection = "asc" | "desc";

export type UploadMode = "auto" | "scheduled" | "urgent-immediate";

export interface AnchorPayload {
  tiktok_slot: string;
  overrides?: Partial<Record<import("@/types").Platform, string>>;
  steals?: Partial<Record<import("@/types").Platform, import("@/types").StealSpec>>;
}

/** The whole urgent-immediate plan, carried client-side through the modal
 * chain and applied server-side in ONE call (urgent-apply) at final confirm.
 * Closing the flow before that applies nothing. */
export interface UrgentPlan {
  tiktokOnly: boolean;
  shifts: import("@/types").UrgentShiftSpec[];
  ownReservations?: { first_slot?: string; manual_at?: string };
}
