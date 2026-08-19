// Mirror of backend SchedulingService._MIN_LEAD_MINUTES
// (backend/app/services/scheduling_service.py): nothing can be scheduled
// closer than this to now. Keep both sides in sync.
export const MIN_LEAD_MINUTES = 15;
export const MIN_LEAD_MS = MIN_LEAD_MINUTES * 60 * 1000;
