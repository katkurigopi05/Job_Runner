import Link from "next/link";
import { notFound } from "next/navigation";
import { API_BASE, ApiError, api, type ApplicationPacket } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { CopyRow } from "@/components/copy-row";

export const dynamic = "force-dynamic";

/**
 * The finish-by-hand screen.
 *
 * Every ATS this project supports mounts a captcha on the apply form, and
 * CLAUDE.md §2.5 rules out working around one — no solving services, no
 * evasion. So submission is the owner's step, permanently, and the honest
 * response is to make that step short rather than to pretend otherwise.
 *
 * The page is ordered the way the work actually happens: confirm the job,
 * take the file, paste the answers, then handle whatever nobody could answer.
 * Anything that would send the owner back to re-derive something the run
 * already knew is a bug in this screen.
 */
export default async function ApplyPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  let packet: ApplicationPacket;
  try {
    packet = await api.packet(id);
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 404) notFound();
      return <ErrorPanel error={error} />;
    }
    throw error;
  }

  const { posting, resume } = packet;
  const blocking = packet.unanswered.filter((question) => question.required);

  return (
    <div className="space-y-12">
      <header className="space-y-4">
        <Link
          href={`/applications/${packet.application_id}`}
          className="font-mono text-xs text-ink-faint hover:text-ink-soft"
        >
          ← application
        </Link>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
          <h1 className="font-display text-[length:var(--text-display)] leading-none">
            {posting?.title ?? "Finish this application"}
          </h1>
          {posting?.company ? (
            <span className="font-mono text-sm text-go">{posting.company}</span>
          ) : null}
        </div>
        <p className="max-w-2xl text-sm text-ink-soft">
          The form was filled and screenshotted. Submitting is yours — every ATS here puts a
          captcha on the apply page, and this project does not work around captchas.
        </p>
      </header>

      {/* Step one. Opening the posting is the only irreversible-feeling thing
          on this page, so it sits alone and reads as the primary action. */}
      <section aria-labelledby="posting" className="space-y-4">
        <h2 id="posting" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          1 · The posting
        </h2>
        <div className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-6 shadow-[var(--shadow-soft)]">
          <dl className="grid gap-x-8 gap-y-3 sm:grid-cols-[auto_1fr]">
            {posting?.location ? (
              <>
                <dt className="font-mono text-xs text-ink-faint">Location</dt>
                <dd className="text-sm">{posting.location}</dd>
              </>
            ) : null}
            <dt className="font-mono text-xs text-ink-faint">ATS</dt>
            <dd className="text-sm">{packet.ats ?? "unknown"}</dd>
          </dl>

          <a
            href={packet.apply_url}
            target="_blank"
            rel="noreferrer noopener"
            className="mt-6 inline-block rounded-[var(--radius)] bg-go px-5 py-2.5 font-mono text-sm text-on-accent transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-go"
          >
            Open the application form ↗
          </a>

          {posting?.description ? (
            <details className="mt-6 border-t border-rule-soft pt-4">
              <summary className="cursor-pointer font-mono text-xs text-ink-faint hover:text-ink-soft">
                Job description
              </summary>
              <p className="mt-4 max-w-3xl whitespace-pre-wrap text-sm leading-relaxed text-ink-soft">
                {posting.description}
              </p>
            </details>
          ) : null}
        </div>
      </section>

      <section aria-labelledby="resume" className="space-y-4">
        <h2 id="resume" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          2 · The résumé to upload
        </h2>
        {resume ? (
          <div className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-6 shadow-[var(--shadow-soft)]">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <div className="text-sm">
                  {resume.is_tailored ? "Tailored for this posting" : "Your base résumé"}
                </div>
                {/* Rejections are shown, never hidden. A guard that silently
                    substituted the original would be indistinguishable from a
                    guard that never fired. */}
                <div className="mt-1 font-mono text-xs text-ink-faint">
                  {resume.is_tailored
                    ? `${resume.rewritten_bullets} bullets rewritten · ${resume.rejected_rewrites} rejected by the guard`
                    : "Tailoring produced nothing for this run"}
                </div>
              </div>
              <a
                href={`${API_BASE}${resume.download_path}`}
                className="rounded-[var(--radius)] border border-go px-5 py-2.5 font-mono text-sm text-go transition-colors hover:bg-go hover:text-on-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-go"
              >
                Download PDF ↓
              </a>
            </div>
          </div>
        ) : (
          <p className="text-sm text-attn">
            No résumé is attached to this profile. Upload one on the{" "}
            <Link href="/resumes" className="underline">
              résumés page
            </Link>{" "}
            first.
          </p>
        )}
      </section>

      <section aria-labelledby="answers" className="space-y-4">
        <h2 id="answers" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          3 · Answers to paste
        </h2>
        {packet.answers.length ? (
          <ul className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised px-6 shadow-[var(--shadow-soft)]">
            {packet.answers.map((answer, index) => (
              <CopyRow key={`${answer.question}-${index}`} {...answer} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-soft">
            Nothing recorded — this application has not been filled yet.
          </p>
        )}
      </section>

      {/* §2.4 — the exact wording, never a paraphrase. An answer to a
          reworded question is an answer to a different question. */}
      {packet.unanswered.length ? (
        <section aria-labelledby="unanswered" className="space-y-4">
          <h2 id="unanswered" className="font-mono text-xs uppercase tracking-widest text-attn">
            4 · You have to answer these
          </h2>
          <ul className="space-y-4">
            {packet.unanswered.map((question, index) => (
              <li
                key={`${question.question}-${index}`}
                className="rounded-[var(--radius)] border-l-2 border-attn bg-attn-soft/40 px-5 py-4"
              >
                <div className="text-sm">{question.question}</div>
                <div className="mt-2 font-mono text-xs text-ink-faint">
                  {question.kind ?? "text"}
                  {question.required ? " · required" : ""}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <footer className="border-t border-rule-soft pt-6 font-mono text-xs text-ink-faint">
        {blocking.length
          ? `${blocking.length} required question${blocking.length === 1 ? "" : "s"} still needs your answer.`
          : packet.ready_to_submit
            ? "Form filled, nothing outstanding. Solve the captcha and submit."
            : "This application has not been filled yet."}
      </footer>
    </div>
  );
}
