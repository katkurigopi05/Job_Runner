import { api, ApiError, type DiscoveryStatus } from "@/lib/api";

/**
 * What the feed is waiting on, above the feed.
 *
 * An empty or thin match list has at least four causes and they want different
 * responses: nothing imported, companies still waiting on discovery, boards
 * fetched but not yet scored, or scored under fallback weighting because the
 * corpus is small. Before this the screen said "nothing scored yet" and left
 * the owner to guess which — and the most common one, a crawl still part-way
 * through thousands of companies, looks identical to a broken pipeline.
 *
 * Postings are reported as soon as they are stored, deliberately *before* they
 * are scored. A posting is visible long before it is in the feed, and hiding
 * that is what makes a working crawl look like a stalled one.
 */
export async function DiscoveryStrip() {
  let status: DiscoveryStatus;
  try {
    status = await api.discoveryStatus();
  } catch (error) {
    // Progress information must never be the reason a feed fails to render.
    if (error instanceof ApiError) return null;
    throw error;
  }
  if (status.companies_total === 0) return null;

  const working = status.discovery_queue + status.fetch_queue;
  const unresolved = status.pending + status.unverified + status.retrying;
  const weak = status.weighting !== "idf";

  return (
    <section className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised px-5 py-4">
      <dl className="flex flex-wrap gap-x-8 gap-y-2 font-mono text-xs text-ink-soft">
        <Stat label="companies" value={status.companies_total} />
        <Stat label="boards found" value={status.verified} />
        <Stat label="still unresolved" value={unresolved} />
        <Stat label="postings" value={status.postings_open} />
        <Stat label="scored" value={status.matches_total} />
        {working > 0 ? <Stat label="queued" value={working} /> : null}
        {status.needs_review > 0 ? (
          <Stat label="need a URL from you" value={status.needs_review} />
        ) : null}
      </dl>

      {working > 0 || unresolved > 0 ? (
        <p className="mt-3 max-w-prose text-sm text-ink-soft">
          Discovery is still working through the registry, so this feed is a partial view rather
          than a final one. {status.postings_unembedded > 0
            ? `${status.postings_unembedded} fetched postings are not scored yet — a listing with no description never will be, and scoring happens on the next sweep. `
            : ""}
          {status.retrying > 0 && status.retrying_next_at
            ? `${status.retrying} companies failed and will be retried from ${new Date(
                status.retrying_next_at,
              ).toLocaleString()}.`
            : null}
        </p>
      ) : null}

      {weak ? (
        <p className="mt-2 max-w-prose text-sm text-ink-faint">{status.weighting_reason}</p>
      ) : null}
    </section>
  );
}


function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex gap-2">
      <dt className="text-ink-faint">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}
