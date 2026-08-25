"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import type { TailoringCandidate } from "@/lib/api";
import { compareTailoring, selectTailoring, type ReviewResult } from "@/app/review/actions";

/**
 * The same posting, tailored by the local model and by the cloud one.
 *
 * §7 made the provider settable per task, but the question that decides the
 * setting — is the cloud one better for *this* résumé and *this* job — could
 * only be answered by editing `.env`, re-running, and remembering the first
 * result. This puts both documents next to each other.
 *
 * Both columns are vetted by the fabrication guard before either is rendered,
 * because each is offered as something the owner may choose and send. The
 * refusal count is shown per side: "the local model was refused nine times" is
 * one of the more useful things a comparison can tell you, and hiding it would
 * make a model that keeps inventing look identical to one that does not.
 */
function Pending({ children, tone = "quiet" }: { children: React.ReactNode; tone?: "quiet" | "go" }) {
  const { pending } = useFormStatus();
  const base =
    "rounded-md px-4 py-2 font-mono text-xs uppercase tracking-widest transition-colors disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2";
  const tones = {
    quiet: "border border-rule text-ink-soft hover:text-ink hover:border-ink-faint",
    go: "bg-go text-paper hover:bg-go/85 focus-visible:outline-go",
  };
  return (
    <button type="submit" disabled={pending} className={`${base} ${tones[tone]}`}>
      {pending ? "working…" : children}
    </button>
  );
}

function Column({
  side,
  applicationId,
  chosen,
}: {
  side: TailoringCandidate;
  applicationId: string;
  chosen: boolean;
}) {
  const selectAction = selectTailoring.bind(null, applicationId);
  const [state, run] = useActionState<ReviewResult | null, FormData>(selectAction, null);

  // A side that could not run still gets a column. Dropping it would leave one
  // result on screen with nothing to say it was unopposed.
  if (side.error) {
    return (
      <section className="border border-rule bg-paper p-4">
        <h4 className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          {side.requested}
        </h4>
        <p className="mt-3 text-sm text-attn">{side.error}</p>
      </section>
    );
  }

  return (
    <section
      className={`border bg-paper p-4 ${chosen ? "border-go" : "border-rule"}`}
      aria-label={`Tailored by ${side.answered_by ?? side.requested}`}
    >
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="font-mono text-xs uppercase tracking-widest text-ink">
          {side.answered_by ?? side.requested}
        </h4>
        {chosen ? (
          <span className="font-mono text-xs uppercase tracking-widest text-go">will be sent</span>
        ) : null}
      </header>

      <p className="mt-2 font-mono text-xs text-ink-soft">
        {side.changed} rewritten · {side.unchanged} unchanged
        {side.rejected > 0 ? (
          <span className="text-attn"> · {side.rejected} refused by the guard</span>
        ) : null}
        {side.reused ? <span className="text-ink-faint"> · reused, nothing sent</span> : null}
      </p>

      {side.changes && side.changes.length > 0 ? (
        <ul className="mt-3 space-y-3">
          {side.changes.map((change, index) => (
            <li key={index} className="border border-rule">
              <p className="border-b border-rule px-3 py-2 text-sm text-ink-faint line-through decoration-stop/50">
                {change.original}
              </p>
              <p className="px-3 py-2 text-sm">{change.tailored}</p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm text-ink-soft">
          Nothing was rewritten. This version is your résumé as written.
        </p>
      )}

      {side.resume_id ? (
        <form action={run} className="mt-4 flex items-center gap-3">
          <input type="hidden" name="resume_id" value={side.resume_id} />
          <Pending tone={chosen ? "quiet" : "go"}>
            {chosen ? "keep this one" : "send this one"}
          </Pending>
          {state ? (
            <span className={`text-sm ${state.ok ? "text-go" : "text-stop"}`}>{state.message}</span>
          ) : null}
        </form>
      ) : (
        <p className="mt-4 font-mono text-xs text-attn">
          No document was produced, so there is nothing to choose here.
        </p>
      )}
    </section>
  );
}

export function TailoringCompare({
  applicationId,
  candidates,
  selectedResumeId,
}: {
  applicationId: string;
  candidates: TailoringCandidate[] | null | undefined;
  selectedResumeId: string | null;
}) {
  const compareAction = compareTailoring.bind(null, applicationId);
  const [state, run] = useActionState<ReviewResult | null, FormData>(compareAction, null);

  if (!candidates || candidates.length === 0) {
    return (
      <div className="space-y-2">
        <form action={run} className="flex flex-wrap items-center gap-3">
          <Pending>compare local vs cloud</Pending>
          {state ? (
            <span className={`text-sm ${state.ok ? "text-go" : "text-stop"}`}>{state.message}</span>
          ) : null}
        </form>
        <p className="font-mono text-xs text-ink-faint">
          Runs the tailorer twice and shows both. The cloud side uploads your résumé again, so
          this happens only when you ask — and a posting already tailored for sends nothing.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        {candidates.map((side, index) => (
          <Column
            key={`${side.requested}-${index}`}
            side={side}
            applicationId={applicationId}
            chosen={Boolean(side.resume_id) && side.resume_id === selectedResumeId}
          />
        ))}
      </div>
      <form action={run} className="flex flex-wrap items-center gap-3">
        <Pending>run it again</Pending>
        {state ? (
          <span className={`text-sm ${state.ok ? "text-go" : "text-stop"}`}>{state.message}</span>
        ) : null}
      </form>
    </div>
  );
}
