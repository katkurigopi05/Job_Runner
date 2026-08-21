"use client";

import { useCallback, useEffect, useState } from "react";
import { API_BASE, api, type ApplicationPacket } from "@/lib/api";
import { DOC_WINDOW, openExternal } from "@/lib/external";

/**
 * The manual-completion queue, built for volume.
 *
 * Every supported ATS mounts a captcha on the apply form and CLAUDE.md §2.5
 * rules out working around one, so the last click is always the owner's. At
 * five applications a day that is a footnote. At a hundred it is the entire
 * cost of the system — the model, the crawler and the tailorer together take
 * seconds, and a hundred manual finishes take hours.
 *
 * So this is optimised for exactly one thing: the number of seconds between
 * seeing a card and seeing the next one. Nothing is fetched per card, the
 * answers are already on screen, and every action has a key.
 */

const KEYS = [
  ["o", "open the form"],
  ["c", "copy all answers"],
  ["r", "download résumé"],
  ["enter", "mark submitted, next"],
  ["s", "skip for now"],
] as const;

export function FinishQueue({ initial }: { initial: ApplicationPacket[] }) {
  const [queue, setQueue] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(0);
  const [flash, setFlash] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const current = queue[0];

  const say = useCallback((message: string) => {
    setFlash(message);
    window.setTimeout(() => setFlash(null), 1200);
  }, []);

  const openForm = useCallback(() => {
    if (!current) return;
    // One reused window, not a new tab per card. Fifty applications used to
    // mean fifty tabs.
    openExternal(current.apply_url);
  }, [current]);

  const copyAnswers = useCallback(async () => {
    if (!current?.answers.length) return;
    // One block, "Question: answer" per line. Pasting field by field is the
    // slow path this exists to avoid.
    const text = current.answers.map((a) => `${a.question}: ${a.value}`).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      say(`${current.answers.length} answers copied`);
    } catch {
      setError("clipboard refused — select the answers manually");
    }
  }, [current, say]);

  const downloadResume = useCallback(() => {
    if (!current?.resume) return;
    openExternal(`${API_BASE}${current.resume.download_path}`, DOC_WINDOW);
  }, [current]);

  const submitted = useCallback(async () => {
    if (!current || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.markSubmitted(current.application_id);
      setDone((n) => n + 1);
      // Advance only after the write lands. Losing a recorded submission is
      // worse than a slow queue: the funnel would undercount and the owner
      // would re-apply to something already sent.
      setQueue((q) => q.slice(1));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "could not record that");
    } finally {
      setBusy(false);
    }
  }, [current, busy]);

  const skip = useCallback(() => {
    // Moves to the back rather than out. Nothing is decided by skipping, and
    // an application that drops off the queue silently is one that never gets
    // sent.
    setQueue((q) => (q.length > 1 ? [...q.slice(1), q[0]] : q));
  }, []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;

      if (event.key === "o") openForm();
      else if (event.key === "c") void copyAnswers();
      else if (event.key === "r") downloadResume();
      else if (event.key === "s") skip();
      else if (event.key === "Enter") void submitted();
      else return;
      event.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openForm, copyAnswers, downloadResume, skip, submitted]);

  if (!current) {
    return (
      <div className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-10 text-center">
        <p className="font-display text-2xl">Queue empty</p>
        <p className="mt-3 text-sm text-ink-soft">
          {done > 0 ? `${done} recorded this session.` : "Nothing is waiting on you."}
        </p>
      </div>
    );
  }

  const blocking = current.unanswered.filter((q) => q.required);

  return (
    <div className="space-y-6">
      <article className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-7 shadow-[var(--shadow-panel)]">
        <div className="flex items-start justify-between gap-6">
          <div className="min-w-0">
            <h2 className="font-display text-2xl leading-tight">
              {current.posting?.title ?? current.apply_url}
            </h2>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-ink-faint">
              {current.posting?.company ? <span className="text-go">{current.posting.company}</span> : null}
              {current.posting?.location ? <span>{current.posting.location}</span> : null}
              {current.ats ? <span>{current.ats}</span> : null}
            </div>
          </div>
          <span className="shrink-0 font-mono text-xs text-ink-faint">{queue.length} left</span>
        </div>

        {blocking.length ? (
          <div className="mt-5 rounded-[var(--radius)] border-l-2 border-attn bg-attn-soft/40 px-4 py-3">
            <div className="font-mono text-xs uppercase tracking-widest text-attn">
              You must answer {blocking.length}
            </div>
            <ul className="mt-2 space-y-1 text-sm">
              {blocking.map((q, i) => (
                <li key={`${q.question}-${i}`}>{q.question}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <button
            type="button"
            onClick={openForm}
            className="rounded-[var(--radius)] bg-go px-4 py-3 font-mono text-sm text-on-accent transition-opacity hover:opacity-90"
          >
            open form <span className="opacity-60">o</span>
          </button>
          <button
            type="button"
            onClick={() => void copyAnswers()}
            disabled={!current.answers.length}
            className="rounded-[var(--radius)] border border-rule px-4 py-3 font-mono text-sm text-ink-soft transition-colors hover:border-go hover:text-go disabled:opacity-40"
          >
            copy {current.answers.length} answers <span className="opacity-60">c</span>
          </button>
          <button
            type="button"
            onClick={downloadResume}
            disabled={!current.resume}
            className="rounded-[var(--radius)] border border-rule px-4 py-3 font-mono text-sm text-ink-soft transition-colors hover:border-go hover:text-go disabled:opacity-40"
          >
            résumé <span className="opacity-60">r</span>
          </button>
        </div>

        {current.resume && !current.resume.is_tailored ? (
          <p className="mt-3 font-mono text-xs text-attn">
            base résumé — tailoring produced nothing for this run
          </p>
        ) : null}
      </article>

      {flash ? <p className="font-mono text-xs text-go">{flash}</p> : null}
      {error ? <p className="font-mono text-xs text-stop">{error}</p> : null}

      <div className="flex gap-4">
        <button
          type="button"
          onClick={skip}
          className="rounded-[var(--radius)] border border-rule px-6 py-3 font-mono text-sm text-ink-faint transition-colors hover:text-ink-soft"
        >
          skip <span className="opacity-60">s</span>
        </button>
        <button
          type="button"
          onClick={() => void submitted()}
          disabled={busy}
          className="flex-1 rounded-[var(--radius)] border border-go bg-go/10 px-6 py-3 font-mono text-sm text-go transition-colors hover:bg-go hover:text-on-accent disabled:opacity-40"
        >
          I submitted it <span className="opacity-60">↵</span>
        </button>
      </div>

      <dl className="flex flex-wrap gap-x-6 gap-y-1 border-t border-rule-soft pt-4 font-mono text-xs text-ink-faint">
        {KEYS.map(([key, label]) => (
          <div key={key} className="flex gap-2">
            <dt className="text-ink-soft">{key}</dt>
            <dd>{label}</dd>
          </div>
        ))}
        <div className="ml-auto">{done} recorded</div>
      </dl>
    </div>
  );
}
