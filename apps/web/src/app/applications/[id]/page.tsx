import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiError, api, type Application, type ApplicationEvent } from "@/lib/api";
import { FillRate, StatusPill } from "@/components/status";
import { ErrorPanel } from "@/components/error-panel";

export const dynamic = "force-dynamic";

export default async function ApplicationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  let application: Application;
  let events: ApplicationEvent[];
  try {
    [application, events] = await Promise.all([api.application(id), api.events(id)]);
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 404) notFound();
      return <ErrorPanel error={error} />;
    }
    throw error;
  }

  const review = application.review ?? {};

  return (
    <div className="space-y-10">
      <header className="space-y-4">
        <Link href="/applications" className="font-mono text-xs text-ink-faint hover:text-ink-soft">
          ← pipeline
        </Link>
        <h1 className="font-display text-3xl leading-tight break-words">{application.url}</h1>
        <div className="flex flex-wrap items-center gap-5">
          <StatusPill status={application.status} reason={application.failure_reason} />
          <FillRate rate={review.fill_rate} />
          <span className="font-mono text-xs text-ink-faint">{application.ats ?? "unknown ats"}</span>
        </div>
      </header>

      {review.unanswered?.length ? (
        <section aria-labelledby="unanswered">
          <h2 id="unanswered" className="font-mono text-xs uppercase tracking-widest text-attn">
            Unanswered
          </h2>
          <ul className="mt-4 space-y-4">
            {review.unanswered.map((question, index) => (
              <li key={question.key ?? index} className="quoted">
                {question.question}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* Append-only, and the reason an application's history is auditable at
          all. Newest last, so it reads as a story rather than a feed. */}
      <section aria-labelledby="history">
        <h2 id="history" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          History
        </h2>
        <ol className="mt-5 space-y-0 border-l border-rule pl-6">
          {events.map((event) => {
            const payload = event.payload ?? {};
            const from = payload.from as string | undefined;
            const to = payload.to as string | undefined;
            return (
              <li key={event.id} className="relative pb-6 last:pb-0">
                <span
                  aria-hidden
                  className="absolute top-2 -left-[1.8125rem] size-2 rounded-full bg-ink-faint"
                />
                <p className="font-mono text-xs text-ink-faint">
                  {new Date(event.at).toLocaleString()}
                </p>
                <p className="mt-1">
                  {from && to ? (
                    <>
                      <span className="text-ink-soft">{from}</span>
                      <span className="mx-2 text-ink-faint">to</span>
                      <span>{to}</span>
                    </>
                  ) : (
                    event.type
                  )}
                </p>
                {typeof payload.reason === "string" ? (
                  <p className="mt-1 text-sm text-ink-soft">{payload.reason}</p>
                ) : null}
              </li>
            );
          })}
        </ol>
      </section>
    </div>
  );
}
