/**
 * Generic SSE stream reader.
 *
 * Reads a streaming `text/event-stream` response, parses each `data:` event
 * as JSON, and forwards it to the provided callback.
 *
 * Returns the last successfully parsed event, or `null` if the stream was
 * empty or aborted before any events arrived.
 */
export async function readSSEStream<T extends { status?: string; error?: string | null; message?: string | null }>(
  response: Response,
  onEvent: (data: T) => void,
  signal?: AbortSignal,
): Promise<T | null> {
  if (!response.ok) {
    throw new Error(`SSE request failed with status ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("No response body");
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let lastEvent: T | null = null;

  try {
    while (true) {
      if (signal?.aborted) break;

      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const data = JSON.parse(line.slice(6)) as T;
          lastEvent = data;
          onEvent(data);
          if (data.status === "error") {
            throw new Error(data.error || data.message || "Request failed");
          }
        } catch (e) {
          if (e instanceof SyntaxError) continue;
          throw e;
        }
      }
    }
  } finally {
    try {
      reader.cancel();
    } catch {
      // Ignore cancel errors on already-closed readers
    }
  }

  return lastEvent;
}
