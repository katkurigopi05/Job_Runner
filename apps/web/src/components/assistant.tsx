"use client";

import { useEffect, useRef, useState } from "react";

/* Styling cues borrowed from the photo-editor: an emoji-prefixed toolbar row,
   a grey working canvas the content sits on, and a left-aligned status line
   pinned to the bottom. That app leans on system defaults, so there is no
   palette to copy — these are the shapes, carried into this app's colours. */

interface Turn {
  role: "you" | "assistant";
  text: string;
  provider?: string;
}

const PROMPTS = [
  { icon: "📋", label: "What needs me?", text: "Which applications are waiting on me right now?" },
  { icon: "📊", label: "How's it going?", text: "Summarize where my applications stand." },
  { icon: "📮", label: "Any replies?", text: "Have I had any replies, and what did they say?" },
];

export function Assistant({
  applicationId,
  compact = false,
}: {
  applicationId?: string;
  compact?: boolean;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("local model · nothing sent anywhere");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, busy]);

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy) return;

    setTurns((held) => [...held, { role: "you", text: question }]);
    setDraft("");
    setBusy(true);
    setStatus("thinking on this machine…");

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message: question, application_id: applicationId ?? null }),
      });
      const body = await response.json();

      if (!response.ok) {
        // The most common failure is Ollama not running, and the API's message
        // names the command that fixes it. Surfacing it beats "request failed".
        setTurns((held) => [
          ...held,
          { role: "assistant", text: body?.error?.message ?? "The assistant is unavailable." },
        ]);
        setStatus("local model unreachable");
        return;
      }

      setTurns((held) => [
        ...held,
        { role: "assistant", text: body.reply, provider: body.provider },
      ]);
      setStatus(
        body.provider === "refused"
          ? "refused · this one comes from your profile"
          : `answered by ${body.provider} · on this machine`,
      );
    } catch {
      setTurns((held) => [
        ...held,
        { role: "assistant", text: "Could not reach the API. Is `make api` running?" },
      ]);
      setStatus("api unreachable");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col border border-rule bg-paper-raised">
      {/* Toolbar — the photo-editor's emoji row. */}
      <div className="flex flex-wrap gap-2 border-b border-rule px-3 py-2.5">
        {PROMPTS.map((prompt) => (
          <button
            key={prompt.label}
            type="button"
            disabled={busy}
            onClick={() => send(prompt.text)}
            className="rounded border border-rule px-2.5 py-1 font-mono text-xs text-ink-soft transition-colors hover:border-attn hover:text-ink disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
          >
            {prompt.icon} {prompt.label}
          </button>
        ))}
      </div>

      {/* The grey canvas. */}
      <div
        className={`min-h-0 flex-1 space-y-4 overflow-y-auto bg-rule/25 p-3 ${compact ? "" : "min-h-96"}`}
      >
        {turns.length === 0 ? (
          <p className="px-1 py-8 text-center font-mono text-xs text-ink-faint">
            runs on your machine. asks nothing of the internet.
          </p>
        ) : (
          turns.map((turn, index) => (
            <div key={index} className={turn.role === "you" ? "text-right" : ""}>
              <p className="font-mono text-xs text-ink-faint">
                {turn.role}
                {turn.provider === "refused" ? " · refused" : ""}
              </p>
              <div
                className={`mt-1 inline-block max-w-[90%] rounded px-3 py-2 text-left text-sm leading-relaxed whitespace-pre-wrap ${
                  turn.role === "you"
                    ? "bg-ink text-paper"
                    : turn.provider === "refused"
                      ? "border border-attn/40 bg-attn-soft text-ink"
                      : "border border-rule bg-paper"
                }`}
              >
                {turn.text}
              </div>
            </div>
          ))
        )}
        {busy ? <p className="px-1 font-mono text-xs text-ink-faint">…</p> : null}
        <div ref={endRef} />
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void send(draft);
        }}
        className="flex gap-2 border-t border-rule px-3 py-2.5"
      >
        <label htmlFor="assistant-input" className="sr-only">
          Ask the assistant
        </label>
        <input
          id="assistant-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="ask about your applications…"
          disabled={busy}
          className="min-w-0 flex-1 rounded border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
        />
        <button
          type="submit"
          disabled={busy || !draft.trim()}
          className="rounded bg-ink px-4 py-1.5 font-mono text-xs uppercase tracking-widest text-paper transition-opacity hover:opacity-85 disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
        >
          send
        </button>
      </form>

      {/* Status line, bottom-left — the photo-editor's. */}
      <p
        role="status"
        className="border-t border-rule px-3 py-1.5 text-left font-mono text-xs text-ink-faint"
      >
        {status}
      </p>
    </div>
  );
}
