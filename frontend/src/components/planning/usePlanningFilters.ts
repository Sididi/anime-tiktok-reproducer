import { useEffect, useState } from "react";
import { ALL_PLATFORMS, type Platform } from "@/types";

const LS_ACCOUNT = "atr.planning.account_id";
const LS_PLATFORMS = "atr.planning.platforms";

function readPersistedPlatforms(): Platform[] {
  try {
    const raw = localStorage.getItem(LS_PLATFORMS);
    if (!raw) return [...ALL_PLATFORMS];
    const arr = JSON.parse(raw) as Platform[];
    return arr.length ? arr : [...ALL_PLATFORMS];
  } catch {
    return [...ALL_PLATFORMS];
  }
}

function readPersistedAccount(): string | null {
  try {
    const raw = localStorage.getItem(LS_ACCOUNT);
    return raw && raw !== "null" ? raw : null;
  } catch {
    return null;
  }
}

export function usePlanningFilters() {
  const [accountId, setAccountId] = useState<string | null>(readPersistedAccount());
  const [platforms, setPlatforms] = useState<Platform[]>(readPersistedPlatforms());

  useEffect(() => {
    localStorage.setItem(LS_ACCOUNT, accountId ?? "null");
  }, [accountId]);

  useEffect(() => {
    localStorage.setItem(LS_PLATFORMS, JSON.stringify(platforms));
  }, [platforms]);

  return { accountId, setAccountId, platforms, setPlatforms };
}
