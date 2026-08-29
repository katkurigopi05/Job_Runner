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
  model?: string;
  /** False when the context for this turn left the machine. */
  local?: boolean;
  /** Whether recruiter mail was actually in that turn's context. */
  sharedMail?: boolean;
}

/**
 * Who may answer.
 *
 * The hint is not decoration. Chat context carries application URLs, profile
 * fields and recruiter correspondence, so picking anything but `local` sends
 * those to a third party — and the moment to say so is while the owner is
 * choosing, not afterwards in a changelog. `openrouter` gets the longest hint
 * because it is the only one that cannot name who ultimately receives it.
 */
const PROVIDERS = [
  {
    value: "ollama",
    label: "local",
    hint: "Runs on this machine. Nothing leaves.",
    local: true,
  },
  {
    value: "gemini",
    label: "gemini",
    hint: "Sends your applications, profile and recruiter mail to Google.",
    local: false,
  },
  {
    value: "anthropic",
    label: "anthropic",
    hint: "Sends your applications, profile and recruiter mail to Anthropic.",
    local: false,
  },
  {
    value: "openrouter",
    label: "openrouter",
    hint: "Sends your applications, profile and recruiter mail to OpenRouter, which forwards them to an upstream provider it does not name.",
    local: false,
  },
  {
    // Separate from `local` on purpose, and the hint has to work harder here
    // than anywhere else in this list: this is the only option that reaches a
    // third party through `localhost:11434`. Someone reading the request would
    // see the same URL the local model uses.
    value: "ollama_cloud",
    label: "ollama cloud",
    hint: "Sends your applications, profile and recruiter mail to Ollama's servers — not this machine, despite the localhost address. Which model runs is whatever OLLAMA_CLOUD_MODEL names.",
    local: false,
  },
] as const;

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
  // Defaults local, and resets to nothing else on reload. The choice belongs to
  // a question, not to the app.
  const [provider, setProvider] = useState<string>("ollama");
  // Whether recruiter mail may go to a remote model. Off by default and never
  // remembered — a decision about other people's correspondence should be made
  // again each time, not inherited from an earlier session.
  const [shareMail, setShareMail] = useState(false);
  const [status, setStatus] = useState("local model · nothing sent anywhere");

  const chosen = PROVIDERS.find((entry) => entry.value === provider) ?? PROVIDERS[0];
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
    setStatus(chosen.local ? "thinking on this machine…" : `sending to ${chosen.label}…`);

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          message: question,
          application_id: applicationId ?? null,
          provider,
          share_mail: shareMail,
        }),
      });
      const body = await response.json();

      if (!response.ok) {
        // The most common failure is Ollama not running, and the API's message
        // names the command that fixes it. Surfacing it beats "request failed".
        setTurns((held) => [
          ...held,
          { role: "assistant", text: body?.error?.message ?? "The assistant is unavailable." },
        ]);
        setStatus(chosen.local ? "local model unreachable" : `${chosen.label} did not answer`);
        return;
      }

      setTurns((held) => [
        ...held,
        {
          role: "assistant",
          text: body.reply,
          provider: body.provider,
          model: body.model,
          local: body.local,
          sharedMail: body.shared_mail,
        },
      ]);
      // `body.local` is computed server-side from the model, not inferred from
      // the provider name — an Ollama-served `:cloud` model is not local, and
      // the status line must not say otherwise.
      setStatus(
        body.provider === "refused"
          ? "refused · this one comes from your profile"
          : body.local
            ? `answered by ${body.model ?? body.provider} · on this machine`
            : `answered by ${body.model ?? body.provider} · this left your machine`,
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
      {/* Who answers. Above the prompts because it changes what they cost. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-rule px-3 py-2.5">
        <label htmlFor="assistant-provider" className="font-mono text-xs text-ink-faint">
          model
        </label>
        <select
          id="assistant-provider"
          value={provider}
          disabled={busy}
          onChange={(event) => setProvider(event.target.value)}
          className="rounded border border-rule bg-paper px-2 py-1 font-mono text-xs text-ink disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
        >
          {PROVIDERS.map((entry) => (
            <option key={entry.value} value={entry.value}>
              {entry.label}
            </option>
          ))}
        </select>
        <p
          className={`min-w-0 flex-1 font-mono text-xs ${chosen.local ? "text-ink-faint" : "text-attn"}`}
        >
          {chosen.hint}
        </p>
      </div>

      {/* The mail gate. Shown only for a remote model, because for the local
          one there is no boundary to gate: it always sees the mail and saying
          "always on" beside a control that does nothing would be worse than
          not showing a control. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-rule px-3 py-2.5">
        {chosen.local ? (
          <p className="font-mono text-xs text-ink-faint">
            recruiter mail: included — it never leaves this machine
          </p>
        ) : (
          <>
            <label className="flex items-center gap-2 font-mono text-xs text-ink">
              <input
                type="checkbox"
                checked={shareMail}
                disabled={busy}
                onChange={(event) => setShareMail(event.target.checked)}
                className="accent-attn disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
              />
              send recruiter mail to {chosen.label}
            </label>
            <p className="min-w-0 flex-1 font-mono text-xs text-ink-faint">
              {shareMail
                ? "Their emails about you go with the question."
                : "Withheld. Replies stay on this machine; the rest of the context still goes."}
            </p>
          </>
        )}
      </div>

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
        // `min-h-0` and nothing else: a flex child defaults to min-height:auto,
        // which refuses to shrink below its content and pushes the column past
        // its container. This carried `min-h-96` alongside, and the two are the
        // same CSS property — the floor won, the panel outgrew the page's fixed
        // height, and the overflow painted over the note beneath it. The
        // container governs the height; this scrolls inside whatever it gets.
        className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-rule/25 p-3"
      >
        {turns.length === 0 ? (
          <p className="px-1 py-8 text-center font-mono text-xs text-ink-faint">
            {chosen.local
              ? "runs on your machine. asks nothing of the internet."
              : `answers come from ${chosen.label}. your job search goes with the question.`}
          </p>
        ) : (
          turns.map((turn, index) => (
            <div key={index} className={turn.role === "you" ? "text-right" : ""}>
              {/* Per turn, not just in the status line: a conversation can mix
                  local and remote answers, and afterwards only the label says
                  which of them cost anything. */}
              <p className="font-mono text-xs text-ink-faint">
                {turn.role}
                {turn.provider === "refused" ? " · refused" : ""}
                {turn.role === "assistant" && turn.provider !== "refused" && turn.local === false ? (
                  <span className="text-attn">
                    {" · "}
                    {turn.model ?? turn.provider} · left this machine
                    {turn.sharedMail ? " · with recruiter mail" : " · mail withheld"}
                  </span>
                ) : null}
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
