import { expect, test, type Page } from "@playwright/test";

// Real backend, no fetch mocks: every tab of one browser context must share a
// single `/api/events/stream` connection through the SharedWorker, and a tab
// forced onto the per-tab transport must add exactly one.

interface HubDebug {
  transport: "connecting" | "shared" | "direct";
  status: string;
  subscriptions: Record<string, number>;
}

async function hubDebug(page: Page): Promise<HubDebug | null> {
  return page.evaluate(() => {
    const hub = (window as unknown as {
      __atrEventHub?: { debugSnapshot(): HubDebug };
    }).__atrEventHub;
    return hub ? hub.debugSnapshot() : null;
  });
}

async function subscribers(page: Page): Promise<number> {
  const response = await page.request.get("/api/events/stats");
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { subscribers: number };
  return body.subscribers;
}

test("all tabs of a browser share one backend connection", async ({ browser }) => {
  const context = await browser.newContext();
  try {
    const first = await context.newPage();
    await first.goto("/");
    await expect.poll(async () => (await hubDebug(first))?.transport).toBe("shared");
    await expect.poll(async () => (await hubDebug(first))?.status).toBe("connected");
    const baseline = await subscribers(first);

    const second = await context.newPage();
    await second.goto("/");
    const third = await context.newPage();
    await third.goto("/");
    await expect.poll(async () => (await hubDebug(second))?.transport).toBe("shared");
    await expect.poll(async () => (await hubDebug(third))?.transport).toBe("shared");
    // The home page subscribes to two topics in every tab, yet the backend
    // still sees the same single subscriber.
    expect((await hubDebug(third))?.subscriptions.startup_jobs).toBe(1);
    expect((await hubDebug(third))?.subscriptions.index_jobs).toBe(1);
    expect(await subscribers(third)).toBe(baseline);

    const direct = await context.newPage();
    await direct.goto("/?hub=direct");
    await expect.poll(async () => (await hubDebug(direct))?.transport).toBe("direct");
    await expect.poll(async () => (await hubDebug(direct))?.status).toBe("connected");
    expect(await subscribers(direct)).toBe(baseline + 1);
  } finally {
    await context.close();
  }
});
