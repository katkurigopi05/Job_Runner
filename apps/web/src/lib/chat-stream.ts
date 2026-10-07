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
 * Whole frames out of what has arrived, and the unfinished rest.
 *
 * A network chunk is not a line: it can end in the middle of one or carry
 * several. Only text up to the last newline is parsed.
 */
export function takeFrames<Reply>(buffer: string): {
  frames: ChatFrame<Reply>[];
  rest: string;
} {
  const lines = buffer.split("\n");
  const rest = lines.pop() ?? "";
  const frames = lines
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as ChatFrame<Reply>);
  return { frames, rest };
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
      const taken = takeFrames<Reply>(buffer + decoder.decode(value, { stream: true }));
      buffer = taken.rest;
      yield* taken.frames;
    }
    // A last line with no newline after it is still a frame.
    yield* takeFrames<Reply>(`${buffer}${decoder.decode()}\n`).frames;
  } finally {
    reader.releaseLock();
  }
}
