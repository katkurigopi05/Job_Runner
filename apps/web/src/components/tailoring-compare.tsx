"use client";

import { useActionState, useState } from "react";
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
        {/* Never merged into the line above. "The guard refused it" is a
            verdict on what this model wrote; "it never answered" is a verdict
            on the network, and on a screen whose whole purpose is comparing two
            models, showing the second as the first is worse than showing
            nothing. */}
        {side.provider_failures ? (
          <span className="text-stop"> · {side.provider_failures} never answered</span>
        ) : null}
        {side.reused ? <span className="text-ink-faint"> · reused, nothing sent</span> : null}
      </p>

      {side.provider_failures && side.changed === 0 ? (
        <p className="mt-2 max-w-prose font-mono text-xs text-stop">
          This model did not produce a rewrite, so this column is your résumé unchanged. That is
          not a judgment on the model — it never answered. Worth re-running before reading
          anything into the comparison.
        </p>
      ) : null}

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

/**
 * Which remote model the comparison runs against.
 *
 * Hardcoded rather than fetched, matching the `/chat` picker: an unconfigured
 * provider surfaces as a refusal naming the ones that are, which is a better
 * failure than a picker that quietly omits an option the owner expected.
 *
 * "whatever tailoring uses" is first and is the default, because it answers the
 * usual question and costs no decision. The named entries exist for the one it
 * cannot answer: §7 keeps OpenRouter out of the automatic order, so comparing
 * against it used to mean setting `LLM_TASK_TAILOR=openrouter` — adopting a
 * provider in order to evaluate it.
 *
 * Each carries where the résumé goes. §2.8 permits this upload; it does not
 * excuse making the recipient invisible at the moment of choosing, and
 * OpenRouter is the one that cannot name who ultimately receives it.
 */
const CLOUD_SIDES = [
  {
    value: "",
    label: "whatever tailoring uses",
    hint: "The provider a real application would tailor with. No decision needed.",
  },
  {
    value: "gemini",
    label: "gemini",
    hint: "Sends your résumé and this posting to Google.",
  },
  {
    value: "anthropic",
    label: "anthropic",
    hint: "Sends your résumé and this posting to Anthropic.",
  },
  {
    value: "openrouter",
    // Named by `OPENROUTER_MODEL`, not pinned here. The label used to carry the
    // shipped default — until that route was withdrawn and the picker began
    // advertising a model every call 404s on.
    label: "openrouter",
    hint:
      "Sends your résumé to OpenRouter, which forwards it to an upstream provider it does not name. Free routes commonly log prompts and share them with that undisclosed creator. Which model runs is whatever OPENROUTER_MODEL names.",
  },
  {
    value: "tokenrouter",
    label: "tokenrouter",
    hint:
      "Sends your résumé to TokenRouter, which forwards it to an upstream provider it does not name. Its GLM route is free, and free routes commonly log prompts. Slow: roughly 150s per bullet. Which model runs is whatever TOKENROUTER_MODEL names.",
  },
  {
    value: "ollama_cloud",
    label: "ollama cloud",
    hint:
      "Sends your résumé and this posting to Ollama's servers. The address is localhost either way, so nothing in the request distinguishes this from the local model — the tag does. Which model runs is whatever OLLAMA_CLOUD_MODEL names.",
  },
] as const;

/** The pickers, plus what each ticked box means for where the résumé goes.
 *
 * Checkboxes rather than a select, because the question changed. One cloud
 * answers "would my cloud provider have done better than local"; several
 * answer "which of these should I be using", and that is the question the
 * owner has once more than one free route is configured.
 *
 * Nothing is ticked by default, and there is deliberately no "all providers"
 * button. Each box is another §2.8 upload of the résumé to another third
 * party, so four columns send it three times — that should be three
 * deliberate clicks, not one convenient one.
 */
function CloudSides({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const toggle = (name: string) =>
    onChange(value.includes(name) ? value.filter((v) => v !== name) : [...value, name]);
  const picked = CLOUD_SIDES.filter((side) => side.value && value.includes(side.value));

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span className="font-mono text-xs text-ink-faint">cloud sides</span>
        {CLOUD_SIDES.filter((side) => side.value).map((side) => (
          <label key={side.value} className="flex items-center gap-1.5 font-mono text-xs">
            <input
              type="checkbox"
              name="clouds"
              value={side.value}
              checked={value.includes(side.value)}
              onChange={() => toggle(side.value)}
              className="accent-attn focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
            />
            {side.label}
          </label>
        ))}
      </div>
      {picked.length === 0 ? (
        <p className="max-w-prose font-mono text-xs text-ink-faint">
          None ticked — the remote half will be whatever real tailoring would use.
        </p>
      ) : (
        <ul className="max-w-prose space-y-1">
          {picked.map((side) => (
            <li key={side.value} className="font-mono text-xs text-ink-faint">
              <span className="text-ink">{side.label}</span> — {side.hint}
            </li>
          ))}
        </ul>
      )}
    </div>
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
  const [clouds, setClouds] = useState<string[]>([]);

  if (!candidates || candidates.length === 0) {
    return (
      <div className="space-y-3">
        <form action={run} className="space-y-3">
          <CloudSides value={clouds} onChange={setClouds} />
          <div className="flex flex-wrap items-center gap-3">
            <Pending>compare the tailorings</Pending>
            {state ? (
              <span className={`text-sm ${state.ok ? "text-go" : "text-stop"}`}>
                {state.message}
              </span>
            ) : null}
          </div>
        </form>
        <p className="max-w-prose font-mono text-xs text-ink-faint">
          Runs the tailorer once per column and shows them side by side. Every cloud column
          uploads your résumé again, so this happens only when you ask — and a posting already
          tailored for by that provider sends nothing. Ticking providers here applies to this
          comparison only; it does not change what your applications tailor with.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
        {candidates.map((side, index) => (
          <Column
            key={`${side.requested}-${index}`}
            side={side}
            applicationId={applicationId}
            chosen={Boolean(side.resume_id) && side.resume_id === selectedResumeId}
          />
        ))}
      </div>
      {/* Same picker after a run, so a second column can be fetched from a
          different provider without leaving the screen — which is the point of
          being able to name one at all. */}
      <form action={run} className="space-y-3">
        <CloudSides value={clouds} onChange={setClouds} />
        <div className="flex flex-wrap items-center gap-3">
          <Pending>run it again</Pending>
          {state ? (
            <span className={`text-sm ${state.ok ? "text-go" : "text-stop"}`}>{state.message}</span>
          ) : null}
        </div>
      </form>
    </div>
  );
}
