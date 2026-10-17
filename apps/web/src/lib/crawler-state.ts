import type { CrawlStatus } from "@/lib/api";

export interface CrawlerView {
  label: string;
  tone: string;
  moving: boolean;
  asleep: boolean;
  title: string;
}

function ageInWords(iso: string | null | undefined, now: number): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;

  const hours = Math.floor((now - then) / 3_600_000);
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** The crawler indicator's complete state machine, kept independent of React. */
export function crawlerView(
  status: CrawlStatus | null,
  reachable: boolean,
  now: number = Date.now(),
): CrawlerView {
  if (!reachable) {
    return {
      label: "crawl status unavailable",
      tone: "text-attn",
      moving: false,
      asleep: false,
      title: "The crawler status could not be refreshed. Check that the local API is running.",
    };
  }

  if (status === null) {
    return {
      label: "checking crawl",
      tone: "text-ink-faint",
      moving: false,
      asleep: false,
      title: "Checking the local crawl queue.",
    };
  }

  if (status.running) {
    return {
      label: "crawling",
      tone: "text-go",
      moving: true,
      asleep: false,
      title:
        "The crawler is discovering boards, fetching postings, or waiting between requests. It stays active until the current work finishes.",
    };
  }

  if (status.stalled) {
    return {
      label: `${status.pending} queued`,
      tone: "text-attn",
      moving: false,
      asleep: false,
      title: "A crawl is queued but no worker is draining it. Start one with: make worker",
    };
  }

  const freshness = ageInWords(status.newest_posting_at, now);
  return {
    label: freshness ? `postings ${freshness}` : "idle",
    tone: "text-ink-faint",
    moving: false,
    asleep: true,
    title: freshness
      ? `Nothing is crawling. The newest posting was first seen ${freshness}. Queue a crawl with: make crawl`
      : "Nothing is crawling and no postings have been seen yet.",
  };
}
