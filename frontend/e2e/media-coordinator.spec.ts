import { expect, test } from "@playwright/test";
import {
  computeSessionGrants,
  computeTabBudgets,
  sortMediaTabs,
  type MediaSessionDemand,
  type MediaTabPresence,
} from "../src/utils/mediaCoordinator";

function makeTab(
  overrides: Partial<MediaTabPresence>,
): MediaTabPresence {
  return {
    tabId: overrides.tabId ?? "tab",
    createdAt: overrides.createdAt ?? 1,
    focused: overrides.focused ?? false,
    visible: overrides.visible ?? false,
    hasAudioDemand: overrides.hasAudioDemand ?? false,
    updatedAt: overrides.updatedAt ?? 100,
  };
}

function makeSession(
  overrides: Partial<MediaSessionDemand>,
): MediaSessionDemand {
  return {
    id: overrides.id ?? "session",
    requestLoad: overrides.requestLoad ?? true,
    requestWarmup: overrides.requestWarmup ?? false,
    attachedPriority: overrides.attachedPriority ?? 100,
    warmupPriority: overrides.warmupPriority ?? 100,
    kind: overrides.kind ?? "video",
  };
}

test("sortMediaTabs prioritizes focused visible, visible, then hidden audio tabs", () => {
  const ordered = sortMediaTabs([
    makeTab({ tabId: "hidden", createdAt: 4 }),
    makeTab({ tabId: "visible", createdAt: 3, visible: true }),
    makeTab({
      tabId: "focused",
      createdAt: 2,
      visible: true,
      focused: true,
    }),
    makeTab({
      tabId: "audio",
      createdAt: 1,
      hasAudioDemand: true,
    }),
  ]);

  expect(ordered.map((tab) => tab.tabId)).toEqual([
    "focused",
    "visible",
    "audio",
    "hidden",
  ]);
});

test("computeTabBudgets reserves most attached and warmup capacity for the active tab", () => {
  const budgets = computeTabBudgets([
    makeTab({
      tabId: "focused",
      createdAt: 1,
      visible: true,
      focused: true,
    }),
    makeTab({
      tabId: "visible",
      createdAt: 2,
      visible: true,
    }),
    makeTab({
      tabId: "audio",
      createdAt: 3,
      hasAudioDemand: true,
    }),
  ]);

  expect(budgets.get("focused")).toEqual({ attached: 6, warmup: 3 });
  expect(budgets.get("visible")).toEqual({ attached: 2, warmup: 1 });
  expect(budgets.get("audio")).toEqual({ attached: 0, warmup: 0 });
});

test("computeSessionGrants gives attached and warmup slots to highest-priority sessions", () => {
  const grants = computeSessionGrants(
    [
      makeSession({
        id: "manual-source",
        attachedPriority: 999,
        requestWarmup: true,
        warmupPriority: 999,
      }),
      makeSession({
        id: "active-fast-watch",
        attachedPriority: 920,
        requestWarmup: true,
        warmupPriority: 920,
      }),
      makeSession({
        id: "prefetch-1",
        attachedPriority: 860,
        requestWarmup: true,
        warmupPriority: 860,
      }),
      makeSession({
        id: "offscreen",
        attachedPriority: 120,
        requestWarmup: true,
        warmupPriority: 120,
      }),
    ],
    { attached: 2, warmup: 1 },
  );

  expect(grants.get("manual-source")).toEqual({
    attachedGranted: true,
    warmupGranted: true,
  });
  expect(grants.get("active-fast-watch")).toEqual({
    attachedGranted: true,
    warmupGranted: false,
  });
  expect(grants.get("prefetch-1")).toEqual({
    attachedGranted: false,
    warmupGranted: false,
  });
  expect(grants.get("offscreen")).toEqual({
    attachedGranted: false,
    warmupGranted: false,
  });
});
