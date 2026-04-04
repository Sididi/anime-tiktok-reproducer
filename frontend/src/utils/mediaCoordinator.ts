export interface MediaTabPresence {
  tabId: string;
  createdAt: number;
  focused: boolean;
  visible: boolean;
  hasAudioDemand: boolean;
  updatedAt: number;
}

export interface MediaSessionDemand {
  id: string;
  requestLoad: boolean;
  requestWarmup: boolean;
  attachedPriority: number;
  warmupPriority: number;
  kind: "video" | "audio";
}

export interface MediaTabBudget {
  attached: number;
  warmup: number;
}

export interface MediaSessionGrant {
  attachedGranted: boolean;
  warmupGranted: boolean;
}

interface LocalSessionRecord extends MediaSessionDemand {
  onGrantChange: (grant: MediaSessionGrant) => void;
  currentGrant: MediaSessionGrant;
}

const CHANNEL_NAME = "atr-browser-media-v1";
const HEARTBEAT_MS = 1000;
const STALE_MS = 3500;
const GLOBAL_ATTACHED_LIMIT = 8;
const GLOBAL_WARMUP_LIMIT = 4;

function getTabSortBucket(tab: MediaTabPresence): number {
  if (tab.visible && tab.focused) return 0;
  if (tab.visible) return 1;
  if (tab.hasAudioDemand) return 2;
  return 3;
}

function getTabCaps(tab: MediaTabPresence): MediaTabBudget {
  if (tab.visible && tab.focused) {
    return { attached: 6, warmup: 3 };
  }
  if (tab.visible) {
    return { attached: 2, warmup: 1 };
  }
  if (tab.hasAudioDemand) {
    return { attached: 1, warmup: 0 };
  }
  return { attached: 0, warmup: 0 };
}

export function sortMediaTabs(tabs: MediaTabPresence[]): MediaTabPresence[] {
  return [...tabs].sort((left, right) => {
    const bucketDelta = getTabSortBucket(left) - getTabSortBucket(right);
    if (bucketDelta !== 0) return bucketDelta;
    const createdDelta = left.createdAt - right.createdAt;
    if (createdDelta !== 0) return createdDelta;
    return left.tabId.localeCompare(right.tabId);
  });
}

export function computeTabBudgets(
  tabs: MediaTabPresence[],
): Map<string, MediaTabBudget> {
  const budgets = new Map<string, MediaTabBudget>();
  let attachedRemaining = GLOBAL_ATTACHED_LIMIT;
  let warmupRemaining = GLOBAL_WARMUP_LIMIT;

  for (const tab of sortMediaTabs(tabs)) {
    const caps = getTabCaps(tab);
    const budget = {
      attached: Math.max(0, Math.min(caps.attached, attachedRemaining)),
      warmup: Math.max(0, Math.min(caps.warmup, warmupRemaining)),
    };
    budgets.set(tab.tabId, budget);
    attachedRemaining -= budget.attached;
    warmupRemaining -= budget.warmup;
  }

  return budgets;
}

export function computeSessionGrants(
  sessions: MediaSessionDemand[],
  budget: MediaTabBudget,
): Map<string, MediaSessionGrant> {
  const grants = new Map<string, MediaSessionGrant>();
  const loadCandidates = sessions
    .filter((session) => session.requestLoad)
    .sort((left, right) => right.attachedPriority - left.attachedPriority);
  const attachedGrantedIds = new Set(
    loadCandidates.slice(0, budget.attached).map((session) => session.id),
  );

  const warmupCandidates = sessions
    .filter(
      (session) =>
        session.requestWarmup &&
        attachedGrantedIds.has(session.id),
    )
    .sort((left, right) => right.warmupPriority - left.warmupPriority);
  const warmupGrantedIds = new Set(
    warmupCandidates.slice(0, budget.warmup).map((session) => session.id),
  );

  for (const session of sessions) {
    grants.set(session.id, {
      attachedGranted: attachedGrantedIds.has(session.id),
      warmupGranted: warmupGrantedIds.has(session.id),
    });
  }

  return grants;
}

class BrowserMediaCoordinator {
  private readonly tabId: string;
  private readonly createdAt: number;
  private readonly channel: BroadcastChannel | null;
  private readonly sessions = new Map<string, LocalSessionRecord>();
  private readonly remoteTabs = new Map<string, MediaTabPresence>();
  private heartbeatId: number | null = null;

  constructor() {
    this.tabId = this.resolveTabId();
    this.createdAt = this.resolveCreatedAt();
    this.channel =
      typeof BroadcastChannel !== "undefined"
        ? new BroadcastChannel(CHANNEL_NAME)
        : null;
    this.channel?.addEventListener("message", this.handleChannelMessage);
    window.addEventListener("focus", this.handleVisibilityChange);
    window.addEventListener("blur", this.handleVisibilityChange);
    document.addEventListener("visibilitychange", this.handleVisibilityChange);
    this.heartbeatId = window.setInterval(() => {
      this.pruneRemoteTabs();
      this.broadcastPresence();
      this.recompute();
    }, HEARTBEAT_MS);
    this.broadcastPresence();
  }

  registerSession(
    session: MediaSessionDemand,
    onGrantChange: (grant: MediaSessionGrant) => void,
  ): {
    update: (next: Partial<MediaSessionDemand>) => void;
    release: () => void;
  } {
    this.sessions.set(session.id, {
      ...session,
      onGrantChange,
      currentGrant: { attachedGranted: false, warmupGranted: false },
    });
    this.broadcastPresence();
    this.recompute();

    return {
      update: (next) => {
        const existing = this.sessions.get(session.id);
        if (!existing) return;
        this.sessions.set(session.id, {
          ...existing,
          ...next,
        });
        this.broadcastPresence();
        this.recompute();
      },
      release: () => {
        this.sessions.delete(session.id);
        this.broadcastPresence();
        this.recompute();
      },
    };
  }

  private resolveTabId(): string {
    const existing = window.sessionStorage.getItem("atr_media_tab_id");
    if (existing) return existing;
    const generated =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `tab-${Math.random().toString(36).slice(2)}`;
    window.sessionStorage.setItem("atr_media_tab_id", generated);
    return generated;
  }

  private resolveCreatedAt(): number {
    const raw = window.sessionStorage.getItem("atr_media_tab_created_at");
    if (raw) {
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
    const createdAt = Date.now();
    window.sessionStorage.setItem(
      "atr_media_tab_created_at",
      String(createdAt),
    );
    return createdAt;
  }

  private getLocalPresence(): MediaTabPresence {
    const hasAudioDemand = Array.from(this.sessions.values()).some(
      (session) => session.kind === "audio" && session.requestLoad,
    );
    return {
      tabId: this.tabId,
      createdAt: this.createdAt,
      focused: document.hasFocus(),
      visible: document.visibilityState === "visible",
      hasAudioDemand,
      updatedAt: Date.now(),
    };
  }

  private broadcastPresence(): void {
    this.channel?.postMessage(this.getLocalPresence());
  }

  private pruneRemoteTabs(): void {
    const now = Date.now();
    for (const [tabId, tab] of this.remoteTabs.entries()) {
      if (now - tab.updatedAt > STALE_MS) {
        this.remoteTabs.delete(tabId);
      }
    }
  }

  private recompute(): void {
    const localPresence = this.getLocalPresence();
    const tabs = [localPresence];
    for (const tab of this.remoteTabs.values()) {
      tabs.push(tab);
    }
    const budgets = computeTabBudgets(tabs);
    const localBudget = budgets.get(this.tabId) ?? { attached: 0, warmup: 0 };
    const grants = computeSessionGrants(
      Array.from(this.sessions.values()),
      localBudget,
    );

    for (const [sessionId, session] of this.sessions.entries()) {
      const nextGrant =
        grants.get(sessionId) ?? {
          attachedGranted: false,
          warmupGranted: false,
        };
      if (
        nextGrant.attachedGranted === session.currentGrant.attachedGranted &&
        nextGrant.warmupGranted === session.currentGrant.warmupGranted
      ) {
        continue;
      }
      session.currentGrant = nextGrant;
      session.onGrantChange(nextGrant);
    }
  }

  private readonly handleChannelMessage = (event: MessageEvent<unknown>) => {
    const payload = event.data;
    if (
      !payload ||
      typeof payload !== "object" ||
      Array.isArray(payload) ||
      typeof (payload as MediaTabPresence).tabId !== "string"
    ) {
      return;
    }
    const presence = payload as MediaTabPresence;
    if (presence.tabId === this.tabId) {
      return;
    }
    this.remoteTabs.set(presence.tabId, presence);
    this.pruneRemoteTabs();
    this.recompute();
  };

  private readonly handleVisibilityChange = () => {
    this.broadcastPresence();
    this.recompute();
  };

  dispose(): void {
    if (this.heartbeatId !== null) {
      window.clearInterval(this.heartbeatId);
      this.heartbeatId = null;
    }
    this.channel?.removeEventListener("message", this.handleChannelMessage);
    this.channel?.close();
    window.removeEventListener("focus", this.handleVisibilityChange);
    window.removeEventListener("blur", this.handleVisibilityChange);
    document.removeEventListener(
      "visibilitychange",
      this.handleVisibilityChange,
    );
  }
}

let singleton: BrowserMediaCoordinator | null = null;

export function getBrowserMediaCoordinator(): BrowserMediaCoordinator {
  if (singleton === null) {
    singleton = new BrowserMediaCoordinator();
  }
  return singleton;
}
