const PENDING_DELETE_KEY = "project-manager-pending-deletions";
export const PROJECT_DELETE_CONCURRENCY = 6;

export function pendingProjectDeletions(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(PENDING_DELETE_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string") : [];
  } catch {
    return [];
  }
}

export function savePendingProjectDeletions(ids: Iterable<string>) {
  try {
    const pending = [...ids];
    if (pending.length) localStorage.setItem(PENDING_DELETE_KEY, JSON.stringify(pending));
    else localStorage.removeItem(PENDING_DELETE_KEY);
  } catch {
    // Deletion can still run when browser storage is unavailable.
  }
}

/** A bounded worker pool; failures do not discard the remaining selection. */
export async function deleteProjectsConcurrently(
  ids: string[],
  deleteOne: (id: string) => Promise<void>,
  onSettled: (id: string, error: unknown | null) => void,
) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(PROJECT_DELETE_CONCURRENCY, ids.length) }, async () => {
    while (next < ids.length) {
      const id = ids[next++];
      let error: unknown | null = null;
      try { await deleteOne(id); } catch (caught) { error = caught; }
      onSettled(id, error);
    }
  }));
}
