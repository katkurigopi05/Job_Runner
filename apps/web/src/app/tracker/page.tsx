import Link from "next/link";
import {
  ApiError,
  api,
  type Application,
  type Classification,
  type InboundMessage,
} from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";

export const dynamic = "force-dynamic";

/* The board is keyed on *outcome*, not status. Status is our side of the work
   and stops moving at `submitted`; outcome is what the employer said back, and
   it is the only column that answers "how is the search going". */
const COLUMNS: { key: string; title: string; blurb: string; tone: string }[] = [
  {
    key: "offer",
    title: "Offer",
    blurb: "they said yes",
    tone: "border-go/40 bg-go-soft text-go",
  },
  {
    key: "interview",
    title: "Interview",
    blurb: "they want to talk",
    tone: "border-attn/40 bg-attn-soft text-attn",
  },
  {
    key: "info_request",
    title: "They asked something",
    blurb: "needs a reply from you",
    tone: "border-attn/40 bg-attn-soft text-attn",
  },
  {
    key: "waiting",
    title: "Waiting",
    blurb: "sent, nothing back yet",
    tone: "border-wait/40 bg-wait-soft text-wait",
  },
  {
    key: "rejection",
    title: "Rejection",
    blurb: "closed",
    tone: "border-stop/40 bg-stop-soft text-stop",
  },
];

const CLASSIFICATION_LABEL: Record<Classification, string> = {
  interview: "interview",
  rejection: "rejection",
  offer: "offer",
  info_request: "asked something",
  acknowledgement: "acknowledgement",
  otp: "verification code",
  noise: "noise",
};

function columnFor(application: Application): string | null {
  if (application.outcome) return application.outcome;
  // Sent and silent is its own state, and the most common one.
  return application.status === "submitted" ? "waiting" : null;
}

export default async function TrackerPage() {
  let applications: Application[];
  let messages: InboundMessage[];
  let unrouted: InboundMessage[];
  try {
    [applications, messages, unrouted] = await Promise.all([
      api.applications(),
      api.inbox(),
      api.unrouted(),
    ]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const latestFor = new Map<string, InboundMessage>();
  for (const message of messages) {
    if (!message.application_id) continue;
    const held = latestFor.get(message.application_id);
    if (!held || message.at > held.at) latestFor.set(message.application_id, message);
  }

  const tracked = applications.filter((a) => columnFor(a) !== null);

  return (
    <div className="space-y-12">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">Tracker</h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          What came back. Grouped by what the employer said, not by what we did — an application
          stays <span className="font-mono text-sm">submitted</span> whether the answer is an offer
          or silence.
        </p>
      </header>

      {tracked.length === 0 ? (
        <p className="border border-dashed border-rule px-6 py-16 text-center font-mono text-sm text-ink-faint">
          nothing submitted yet
        </p>
      ) : (
        <div className="space-y-10">
          {COLUMNS.map((column) => {
            const rows = tracked.filter((a) => columnFor(a) === column.key);
            if (rows.length === 0) return null;
            return (
              <section key={column.key} aria-labelledby={`col-${column.key}`}>
                <div className="flex flex-wrap items-baseline gap-4 border-b border-rule pb-3">
                  <h2 id={`col-${column.key}`} className="font-display text-xl">
                    {column.title}
                  </h2>
                  <span className="font-mono text-xs text-ink-faint">{column.blurb}</span>
                  <span className="ml-auto font-mono text-sm tabular-nums text-ink-soft">
                    {rows.length}
                  </span>
                </div>
                <ul className="divide-y divide-rule">
                  {rows.map((application) => {
                    const latest = latestFor.get(application.id);
                    return (
                      <li key={application.id} className="py-4">
                        <div className="flex flex-wrap items-center gap-4">
                          <Link
                            href={`/applications/${application.id}`}
                            className="min-w-0 flex-1 truncate underline-offset-4 hover:underline"
                          >
                            {application.url}
                          </Link>
                          <span
                            className={`rounded-full border px-3 py-0.5 font-mono text-xs ${column.tone}`}
                          >
                            {column.key.replace(/_/g, " ")}
                          </span>
                        </div>
                        {latest ? (
                          <div className="mt-3 border-l border-rule pl-4">
                            <p className="font-mono text-xs text-ink-faint">
                              {latest.from_addr} · {new Date(latest.at).toLocaleDateString()}
                              {latest.classification
                                ? ` · read as ${CLASSIFICATION_LABEL[latest.classification]}`
                                : null}
                            </p>
                            {/* Their words, not a summary of them. The
                                classification above is a guess, and this is
                                what you check it against. */}
                            {latest.subject ? (
                              <p className="mt-1 font-display">{latest.subject}</p>
                            ) : null}
                            {latest.body ? (
                              <p className="mt-1 line-clamp-3 text-sm text-ink-soft">
                                {latest.body}
                              </p>
                            ) : null}
                          </div>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}
        </div>
      )}

      {unrouted.length > 0 ? (
        <section aria-labelledby="unrouted">
          <div className="flex flex-wrap items-baseline gap-4 border-b border-rule pb-3">
            <h2 id="unrouted" className="font-display text-xl">
              Matched nothing
            </h2>
            <span className="font-mono text-xs text-ink-faint">
              arrived, but no application claimed them
            </span>
            <span className="ml-auto font-mono text-sm tabular-nums text-ink-soft">
              {unrouted.length}
            </span>
          </div>
          <p className="mt-3 max-w-prose text-sm text-ink-soft">
            Either a recruiter wrote from an address the alias could not be read from, or alias
            matching has a bug. Worth a look either way — nothing else surfaces these.
          </p>
          <ul className="mt-4 divide-y divide-rule">
            {unrouted.map((message) => (
              <li key={message.id} className="py-3">
                <p className="font-mono text-xs text-ink-faint">
                  {message.from_addr} · {new Date(message.at).toLocaleDateString()}
                </p>
                {message.subject ? <p className="mt-1">{message.subject}</p> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
