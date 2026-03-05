/**
 * Generic SSE stream reader.
 *
 * Reads a streaming `text/event-stream` response, parses each `data:` event
 * as JSON, and forwards it to the provided callback.
 *
 * Returns the last successfully parsed event, or `null` if the stream was
 * empty or aborted before any events arrived.
 */
interface ReadSSEStreamOptions<T> {
  signal?: AbortSignal;
  stopWhen?: (data: T) => boolean;
}

export async function readSSEStream<T extends { status?: string; error?: string | null; message?: string | null }>(
  response: Response,
  onEvent: (data: T) => void,
  signalOrOptions?: AbortSignal | ReadSSEStreamOptions<T>,
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
  let shouldStop = false;
  const options: ReadSSEStreamOptions<T> =
    signalOrOptions && "aborted" in signalOrOptions
      ? { signal: signalOrOptions }
      : (signalOrOptions ?? {});
  const signal = options.signal;
  const stopWhen = options.stopWhen;

  const processBufferedEvents = (flush: boolean) => {
    buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const splitTarget = flush ? `${buffer}\n\n` : buffer;
    const chunks = splitTarget.split("\n\n");
    buffer = flush ? "" : chunks.pop() || "";

    for (const chunk of chunks) {
      const dataLines = chunk
        .split("\n")
        .filter((line) => line.startsWith("data:"));
      if (dataLines.length === 0) continue;
      try {
        const payload = dataLines
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        const data = JSON.parse(payload) as T;
        lastEvent = data;
        onEvent(data);
        if (data.status === "error") {
          throw new Error(data.error || data.message || "Request failed");
        }
        if (stopWhen?.(data)) {
          shouldStop = true;
          try {
            reader.cancel();
          } catch {
            // Ignore cancel errors on already-closed readers
          }
          break;
        }
      } catch (e) {
        if (e instanceof SyntaxError) continue;
        throw e;
      }
    }
  };

  const handleAbort = () => {
    shouldStop = true;
    try {
      reader.cancel();
    } catch {
      // Ignore cancel errors on already-closed readers
    }
  };
  if (signal) {
    if (signal.aborted) {
      handleAbort();
    } else {
      signal.addEventListener("abort", handleAbort, { once: true });
    }
  }

  try {
    while (true) {
      if (shouldStop || signal?.aborted) break;

      const { done, value } = await reader.read();
      if (done) {
        processBufferedEvents(true);
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      processBufferedEvents(false);
    }
  } finally {
    if (signal) {
      signal.removeEventListener("abort", handleAbort);
    }
    try {
      reader.cancel();
    } catch {
      // Ignore cancel errors on already-closed readers
    }
  }

  return lastEvent;
}
