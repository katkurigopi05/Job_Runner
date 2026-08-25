"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import type { Application, ResumeParsed, Screening } from "@/lib/api";
import { ResumePreview } from "@/components/resume-preview";
import { ResumeDiffView } from "@/components/resume-diff";
import { TailoringCompare } from "@/components/tailoring-compare";
import { CoverLetterView } from "@/components/cover-letter";
import { FillRate, StatusPill } from "@/components/status";
import { approve, reject, submitOtp, type ReviewResult } from "./actions";

function Submitting({ children, tone }: { children: React.ReactNode; tone: "go" | "stop" }) {
  const { pending } = useFormStatus();
  const base =
    "rounded-md px-5 py-2.5 font-mono text-xs uppercase tracking-widest transition-colors disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2";
  const tones = {
    go: "bg-go text-paper hover:bg-go/85 focus-visible:outline-go",
    stop: "border border-stop/50 text-stop hover:bg-stop-soft focus-visible:outline-stop",
  };
  return (
    <button type="submit" disabled={pending} className={`${base} ${tones[tone]}`}>
      {pending ? "working…" : children}
    </button>
  );
}

export function ReviewCard({
  application,
  resume,
  tailored,
}: {
  application: Application;
  resume: ResumeParsed | null;
  tailored: boolean;
}) {
  const review = application.review ?? {};
  const unanswered = review.unanswered ?? [];
  const required = unanswered.filter((q) => q.required !== false);

  const approveAction = approve.bind(null, application.id);
  const rejectAction = reject.bind(null, application.id);
  const otpAction = submitOtp.bind(null, application.id);

  const [approveState, runApprove] = useActionState<ReviewResult | null, FormData>(
    approveAction,
    null,
  );
  const [rejectState, runReject] = useActionState<ReviewResult | null, FormData>(rejectAction, null);
  const [otpState, runOtp] = useActionState<ReviewResult | null, FormData>(otpAction, null);

  const host = (() => {
    try {
      return new URL(application.url).host;
    } catch {
      return application.url;
    }
  })();

  return (
    <article className="border border-rule bg-paper-raised">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-rule px-6 py-5">
        <div className="min-w-0">
          <h2 className="font-display text-2xl leading-tight">{host}</h2>
          <a
            href={application.url}
            target="jobrunner-form"
            rel="noreferrer"
            className="mt-1 block truncate font-mono text-xs text-ink-faint underline-offset-4 hover:text-ink-soft hover:underline"
          >
            {application.url}
          </a>
        </div>
        <div className="flex flex-col items-end gap-2">
          <StatusPill status={application.status} reason={application.failure_reason} />
          <FillRate rate={review.fill_rate} />
        </div>
      </header>

      <ScreeningPanel screening={review.screening} />

      {application.status === "needs_otp" ? (
        <form action={runOtp} className="border-b border-rule px-6 py-5">
          <label htmlFor={`otp-${application.id}`} className="font-mono text-xs text-ink-soft">
            The site sent a one-time code
          </label>
          <div className="mt-2 flex gap-3">
            <input
              id={`otp-${application.id}`}
              name="code"
              inputMode="numeric"
              autoComplete="one-time-code"
              className="w-40 rounded-md border border-rule bg-paper px-3 py-2 font-mono focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
            />
            <Submitting tone="go">send code</Submitting>
          </div>
          {otpState ? (
            <p className={`mt-3 font-mono text-xs ${otpState.ok ? "text-go" : "text-stop"}`}>
              {otpState.message}
            </p>
          ) : null}
        </form>
      ) : null}

      <form action={runApprove}>
        {required.length > 0 ? (
          <section className="border-b border-rule px-6 py-6">
            <h3 className="font-mono text-xs uppercase tracking-widest text-attn">
              {required.length} question{required.length === 1 ? "" : "s"} the agent would not guess
            </h3>
            <p className="mt-2 max-w-prose text-sm text-ink-soft">
              Quoted exactly as the employer wrote them. Answer in your own words — nothing here is
              generated for you.
            </p>
            <ul className="mt-5 space-y-6">
              {required.map((question, index) => {
                const key = question.key ?? `q${index}`;
                const id = `${application.id}-${key}`;
                return (
                  <li key={id}>
                    <label htmlFor={id} className="quoted block">
                      {question.question}
                    </label>
                    {question.options?.length ? (
                      <select
                        id={id}
                        name={`answer:${key}`}
                        defaultValue=""
                        className="mt-3 w-full max-w-lg rounded-md border border-rule bg-paper px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
                      >
                        <option value="">— choose —</option>
                        {question.options.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <textarea
                        id={id}
                        name={`answer:${key}`}
                        rows={question.kind === "textarea" || question.kind === "cover_letter" ? 5 : 2}
                        className="mt-3 w-full rounded-md border border-rule bg-paper px-3 py-2 leading-relaxed focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
                      />
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        ) : (
          <section className="border-b border-rule px-6 py-5">
            <p className="text-sm text-ink-soft">
              Every required field was filled from your profile. Nothing is waiting on an answer —
              only on your approval.
            </p>
          </section>
        )}

        {review.resume_diff ? (
          <details className="border-b border-rule px-6 py-4" open>
            <summary className="cursor-pointer font-mono text-xs uppercase tracking-widest text-ink-soft hover:text-ink">
              What tailoring changed
            </summary>
            <div className="mt-4">
              <ResumeDiffView diff={review.resume_diff} />
            </div>
          </details>
        ) : null}

        {/* Open by default, unlike the comparison below. This is a document
            about to be sent under the owner's name and it was written by a
            model — reading it is part of approving, not an optional detail.
            Absent entirely when the form never asked, which most do not. */}
        {review.cover_letter ? (
          <details className="border-b border-rule px-6 py-4" open>
            <summary className="cursor-pointer font-mono text-xs uppercase tracking-widest text-ink-soft hover:text-ink">
              Cover letter
            </summary>
            <div className="mt-4">
              <CoverLetterView letter={review.cover_letter} />
            </div>
          </details>
        ) : null}

        {/* Below the diff, not instead of it: the diff says what tailoring did
            to the document being sent, and this asks whether a different model
            would have done better. Collapsed by default because running it
            uploads the résumé again. */}
        <details className="border-b border-rule px-6 py-4">
          <summary className="cursor-pointer font-mono text-xs uppercase tracking-widest text-ink-soft hover:text-ink">
            Compare models
          </summary>
          <div className="mt-4">
            <TailoringCompare
              applicationId={application.id}
              candidates={review.tailoring_comparison}
              selectedResumeId={application.tailored_resume_id}
            />
          </div>
        </details>

        <AttachedResume resume={resume} tailored={tailored} />

        <FilledSummary review={review} />

        <footer className="flex flex-wrap items-center gap-4 px-6 py-5">
          <Submitting tone="go">approve &amp; submit</Submitting>
          <input
            name="note"
            placeholder="note to self (optional)"
            className="min-w-0 flex-1 rounded-md border border-rule bg-paper px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          />
        </footer>
      </form>

      <form action={runReject} className="border-t border-rule px-6 py-4">
        <div className="flex items-center gap-4">
          <Submitting tone="stop">reject</Submitting>
          <p className="font-mono text-xs text-ink-faint">
            Marks it failed as rejected_at_review. Nothing is sent.
          </p>
        </div>
      </form>

      {approveState || rejectState ? (
        <p
          role="status"
          className={`px-6 pb-5 font-mono text-xs ${
            (approveState ?? rejectState)?.ok ? "text-go" : "text-stop"
          }`}
        >
          {(approveState ?? rejectState)?.message}
        </p>
      ) : null}
    </article>
  );
}

function FilledSummary({ review }: { review: NonNullable<Application["review"]> }) {
  const filled = review.filled ?? [];
  if (filled.length === 0) return null;

  return (
    <details className="border-b border-rule px-6 py-4">
      <summary className="cursor-pointer font-mono text-xs uppercase tracking-widest text-ink-soft hover:text-ink">
        {filled.length} field{filled.length === 1 ? "" : "s"} filled from your profile
      </summary>
      <dl className="mt-4 grid gap-x-8 gap-y-3 sm:grid-cols-[minmax(0,14rem)_1fr]">
        {filled.map((field, index) => (
          <div key={`${field.key ?? index}`} className="contents">
            <dt className="font-mono text-xs text-ink-faint">{field.question ?? field.key}</dt>
            <dd className="text-sm break-words">{String(field.value ?? "")}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}


function AttachedResume({ resume, tailored }: { resume: ResumeParsed | null; tailored: boolean }) {
  if (resume === null) {
    return (
      <section className="border-b border-rule px-6 py-5">
        <h3 className="font-mono text-xs uppercase tracking-widest text-stop">
          No résumé attached
        </h3>
        <p className="mt-2 max-w-prose text-sm text-ink-soft">
          Every ATS form has a required résumé field. Set a base résumé on the profile before
          approving this, or the submission will fail on the far side.
        </p>
      </section>
    );
  }

  return (
    <details className="border-b border-rule px-6 py-4" open>
      <summary className="cursor-pointer font-mono text-xs uppercase tracking-widest text-ink-soft hover:text-ink">
        Résumé to be sent
        <span className="ml-2 text-ink-faint">
          {tailored ? "· tailored for this posting" : "· profile base, unmodified"}
        </span>
      </summary>
      <div className="mt-4">
        <ResumePreview parsed={resume} />
      </div>
    </details>
  );
}


/**
 * Questions read off the form before anything was answered.
 *
 * A knock-out is one this profile visibly fails — worth seeing before you
 * spend the effort. A caution is a question restricted in some jurisdictions;
 * it changes nothing on its own, and you may well decide it is just badly
 * worded.
 */
function ScreeningPanel({ screening }: { screening?: Screening | null }) {
  if (!screening) return null;
  const { knock_outs: knockOuts, cautions } = screening;
  if (knockOuts.length === 0 && cautions.length === 0) return null;

  return (
    <section className="border-b border-rule px-6 py-5">
      {knockOuts.length > 0 ? (
        <div className="rounded-[var(--radius)] border border-stop/40 bg-stop-soft px-3 py-2">
          <p className="font-mono text-xs text-stop">
            {knockOuts.length === 1
              ? "this form asks a question your profile likely fails"
              : `this form asks ${knockOuts.length} questions your profile likely fails`}
          </p>
          <ul className="mt-2 space-y-1">
            {knockOuts.map((question) => (
              <li key={question.key} className="font-mono text-xs text-stop/80">
                “{question.label}” — {question.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {cautions.length > 0 ? (
        <div
          className={`rounded-[var(--radius)] border border-attn/40 bg-attn-soft px-3 py-2 ${
            knockOuts.length > 0 ? "mt-3" : ""
          }`}
        >
          <p className="font-mono text-xs text-attn">worth knowing before you answer</p>
          <ul className="mt-2 space-y-1">
            {cautions.map((question) => (
              <li key={question.key} className="font-mono text-xs text-attn/80">
                “{question.label}” — {question.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
