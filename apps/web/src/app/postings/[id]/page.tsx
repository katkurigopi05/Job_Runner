import Link from "next/link";
import { ApiError, api, type PostingHistory, type VersionChange } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { SplitButton } from "./split-button";

export const dynamic = "force-dynamic";

/**
 * One requisition: every place it is listed, and what it said over time.
 *
 * Sources are listed in full even when grouped, each with its own decisions
 * and applications — grouping is a link between listings, never a deletion,
 * and a copy that was already applied to must not disappear behind another.
 */

const FIELD_LABEL: Record<string, string> = {
  title: "Title",
  location: "Location",
  pay: "Pay",
  required_skills: "Required skills",
  preferred_skills: "Preferred skills",
  education: "Education",
  text: "Text",
};

function Change({ change }: { change: VersionChange }) {
  const label = FIELD_LABEL[change.field] ?? change.field;
  if (change.field === "text") {
    return (
      <li className="text-sm text-ink-soft">
        <span className="font-mono text-xs text-ink-faint">{label}</span> edited, nothing comparable
        changed
      </li>
    );
  }
  if (Array.isArray(change.before) || Array.isArray(change.after)) {
    const removed = (change.before as string[] | null) ?? [];
    const added = (change.after as string[] | null) ?? [];
    return (
      <li className="text-sm">
        <span className="font-mono text-xs text-ink-faint">{label}</span>{" "}
        {added.map((skill) => (
          <span key={`+${skill}`} className="mr-1.5 text-go">
            +{skill}
          </span>
        ))}
        {removed.map((skill) => (
          <span key={`-${skill}`} className="mr-1.5 text-stop line-through">
            {skill}
          </span>
        ))}
      </li>
    );
  }
  return (
    <li className="text-sm">
      <span className="font-mono text-xs text-ink-faint">{label}</span>{" "}
      <span className="text-ink-faint line-through">{String(change.before ?? "not stated")}</span>
      <span className="px-1.5 text-ink-faint">→</span>
      <span className="text-ink">{String(change.after ?? "not stated")}</span>
    </li>
  );
}

export default async function PostingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let history: PostingHistory;
  try {
    history = await api.postingHistory(id);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const grouped = history.sources.length > 1;
  const versions = [...history.versions].reverse();

  return (
    <div className="space-y-12">
      <header>
        <Link href="/matches" className="font-mono text-xs text-ink-faint hover:text-ink">
          ← matches
        </Link>
        <h1 className="mt-3 font-display text-4xl leading-tight tracking-tight">
          {history.title ?? "Untitled posting"}
        </h1>
        <p className="mt-3 max-w-prose text-sm text-ink-soft">
          {grouped
            ? `Listed in ${history.sources.length} places, grouped because the evidence below says they are one requisition.`
            : history.locked
              ? "Kept separate: you split this listing out of a group, and it will not be grouped again."
              : "Listed in one place."}
        </p>
      </header>

      <section aria-labelledby="sources">
        <h2 id="sources" className="border-b border-rule pb-2 font-display text-2xl">
          Sources
        </h2>
        <ul className="mt-4 space-y-3">
          {history.sources.map((source) => (
            <li
              key={source.posting_id}
              className="rounded-[var(--radius)] border border-rule bg-paper-raised px-4 py-3"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <a
                  href={source.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="break-all text-sm text-ink underline-offset-4 hover:underline"
                >
                  {source.url} ↗
                </a>
                <span className="font-mono text-xs text-ink-faint">{source.source}</span>
              </div>
              <p className="mt-1 text-xs text-ink-soft">
                {source.location ?? "location not stated"}
                {source.closed ? <span className="ml-2 text-stop">closed</span> : null}
                {source.decisions.length > 0 ? (
                  <span className="ml-2">decision: {source.decisions.join(", ")}</span>
                ) : null}
                {source.application_statuses.length > 0 ? (
                  <span className="ml-2 text-attn">
                    application: {source.application_statuses.join(", ")}
                  </span>
                ) : null}
              </p>
              {source.evidence ? (
                <p className="mt-1 text-xs text-ink-faint">grouped because: {source.evidence}</p>
              ) : null}
              {grouped ? (
                <div className="mt-2">
                  <SplitButton postingId={source.posting_id} viewing={id} />
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="versions">
        <h2 id="versions" className="border-b border-rule pb-2 font-display text-2xl">
          What it said over time
        </h2>
        {versions.length === 0 ? (
          <p className="mt-4 text-sm text-ink-faint">
            No versions recorded yet. The crawler records one when this listing is first seen or
            next changes.
          </p>
        ) : (
          <ol className="mt-4 space-y-4 border-l border-rule pl-5">
            {versions.map((version) => (
              <li key={version.version} className="relative">
                <span className="absolute -left-[25px] top-1.5 h-2 w-2 rounded-full bg-ink-faint" />
                <p className="font-mono text-xs text-ink-faint">
                  v{version.version} · {new Date(version.captured_at).toLocaleDateString()}
                </p>
                <p className="mt-1 text-sm text-ink">Pay: {version.pay}</p>
                {version.changes.length > 0 ? (
                  <ul className="mt-2 space-y-1">
                    {version.changes.map((change) => (
                      <Change key={change.field} change={change} />
                    ))}
                  </ul>
                ) : version.version === 1 ? (
                  <p className="mt-1 text-xs text-ink-faint">first reading</p>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
