"use client";

import { useState, useTransition } from "react";
import type { PostingAts } from "@/lib/api";
import { checkAts } from "./actions";

/**
 * §23's "before" score, on the screen where the owner is still deciding.
 *
 * `/review` has shown an ATS score since tailoring landed, but only once an
 * application exists — by then the decision is made. The rubric above this
 * says how well the job fits the owner; this says how well their résumé reads
 * to the machine that screens it, which is a different question and the one
 * that decides whether a human ever sees the application.
 *
 * Collapsed until asked. Scoring parses the résumé and reads the posting
 * body, so doing it for every card would spend that on every job in the feed
 * to answer a question about one.
 */
export function AtsPanel({ postingId, profileId }: { postingId: string; profileId: string }) {
  const [report, setReport] = useState<PostingAts | null>(null);
  const [error, setError] = useState("");
  const [pending, startTransition] = useTransition();

  function run() {
    startTransition(async () => {
      const result = await checkAts(postingId, profileId);
      setError(result.ok ? "" : result.message);
      setReport(result.report);
    });
  }

  if (!report) {
    return (
      <div className="mt-4">
        <button
          type="button"
          onClick={run}
          disabled={pending}
          className="font-mono text-xs text-ink-soft underline-offset-4 hover:text-ink hover:underline disabled:opacity-50"
        >
          {pending ? "scoring…" : "how does an ATS read my résumé for this?"}
        </button>
        {error ? <p className="mt-2 font-mono text-xs text-stop">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className="mt-4 border-t border-rule pt-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <p className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          ats · before tailoring
        </p>
        {/* Naming the résumé is not decoration: with more than one base résumé
            the scored document is chosen per posting, so a bare number would
            not say what it described. */}
        <p className="font-mono text-xs text-ink-faint">
          v{report.resume_version} — {report.resume_reason}
        </p>
      </div>

      <dl className="mt-3 flex flex-wrap gap-6">
        <div>
          <dt className="font-mono text-xs text-ink-faint">parse</dt>
          <dd className="font-display text-xl tabular-nums">
            {Math.round(report.parse * 100)}%
          </dd>
        </div>
        <div>
          <dt className="font-mono text-xs text-ink-faint">keywords</dt>
          <dd className="font-display text-xl tabular-nums">
            {/* 0% against no posting body means "not asked". Rendering it as a
                score would tell the owner their résumé is hopeless for a job
                nothing was ever compared against. */}
            {report.scored_against_posting ? `${Math.round(report.keywords * 100)}%` : "—"}
          </dd>
          {!report.scored_against_posting ? (
            <dd className="font-mono text-xs text-ink-faint">
              this posting has no description to match against
            </dd>
          ) : null}
        </div>
      </dl>

      {report.findings.length > 0 ? (
        <ul className="mt-3 space-y-1">
          {report.findings.map((finding) => (
            <li key={`${finding.code}:${finding.detail}`} className="font-mono text-xs text-attn">
              {finding.detail}
              <span className="text-ink-faint"> (−{Math.round(finding.cost * 100)} parse)</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 font-mono text-xs text-go">nothing observable stops a parser</p>
      )}

      {report.scored_against_posting && report.supported.length > 0 ? (
        <p className="mt-3 font-mono text-xs text-ink-faint">
          backs {report.supported.length} of{" "}
          {report.supported.length + report.missing.length} terms this posting asks for
        </p>
      ) : null}
    </div>
  );
}
