/**
 * Reading `POST /chat/stream`.
 *
 * The API sends one JSON object to a line: the answer in pieces as the model
 * writes it, then the same reply `/chat` returns. A model that stops part way
 * sends an error in place of that reply.
 *
 * Browser code. It reads the response the Next rewrite proxies; the API sets
 * `Cache-Control: no-transform` so the proxy does not hold the pieces back.
 */

export type ChatFrame<Reply> =
  | { type: "delta"; text: string }
  | { type: "done"; reply: Reply }
  | { type: "error"; message: string };

/**
 * Whole lines out of what has arrived, and the unfinished rest.
 *
 * A network chunk is not a line: it can end in the middle of one or carry
 * several. Only text up to the last newline is handed on.
 */
export function takeLines(buffer: string): { lines: string[]; rest: string } {
  const lines = buffer.split("\n");
  const rest = lines.pop() ?? "";
  return { lines: lines.filter((line) => line.trim() !== ""), rest };
}

/** Each frame as it arrives, until the response ends. */
export async function* readFrames<Reply>(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<ChatFrame<Reply>> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const taken = takeLines(buffer + decoder.decode(value, { stream: true }));
      buffer = taken.rest;
      // Parsed one at a time: a line that cannot be read must not take with
      // it the good frames that arrived in the same chunk.
      for (const line of taken.lines) yield JSON.parse(line) as ChatFrame<Reply>;
    }
    // A last line with no newline after it is still a frame.
    for (const line of takeLines(`${buffer}${decoder.decode()}\n`).lines) {
      yield JSON.parse(line) as ChatFrame<Reply>;
    }
  } finally {
    // Stopping early, for whatever reason, also ends the response. The API
    // stops the model writing when its reader goes away.
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
