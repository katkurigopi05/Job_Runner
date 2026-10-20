import type { CrawlStatus, StageBacklog } from "@/lib/api";

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

export interface BacklogView {
  /** One line for the header: each stage with work, what is left and when. */
  text: string;
  /** The detail behind it, one stage per line, for the tooltip. */
  title: string;
}

/** `95` minutes as "1h 35m"; a day or more as "1d 4h". */
export function durationInWords(minutes: number): string {
  const total = Math.round(minutes);
  if (total < 1) return "<1m";
  if (total < 60) return `${total}m`;
  const hours = Math.floor(total / 60);
  const rest = total % 60;
  if (hours < 24) return rest ? `${hours}h ${rest}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const leftover = hours % 24;
  return leftover ? `${days}d ${leftover}h` : `${days}d`;
}

const count = (value: number) => value.toLocaleString("en-US");

/**
 * What is left of the crawl, so the owner need not ask (2026-10-19).
 *
 * Only stages with work are named. An estimate is shown only when the stage
 * has been moving over the last half hour; a stage waiting behind another
 * says so instead of guessing.
 */
export function backlogView(status: CrawlStatus | null): BacklogView | null {
  const stages = (status?.backlog ?? []).filter(
    (stage) => stage.waiting + stage.scheduled + stage.running > 0,
  );
  if (stages.length === 0) return null;

  const when = (stage: StageBacklog, long: boolean) => {
    if (stage.eta_minutes !== null) {
      return long ? `, about ${durationInWords(stage.eta_minutes)} to go` : ` ~${durationInWords(stage.eta_minutes)}`;
    }
    if (stage.after) return long ? `, starts after ${stage.after}` : ` after ${stage.after}`;
    return "";
  };

  const text = stages.map((stage) => `${stage.stage} ${count(stage.items)} left${when(stage, false)}`).join(" · ");
  const title = stages
    .map((stage) => {
      const tasks = stage.waiting + stage.scheduled + stage.running;
      const unit = stage.kind === "deep_dive_company" ? "noted companies" : "companies";
      const pace =
        stage.per_minute > 0 ? `${stage.per_minute} a minute over the last 30 minutes` : "not moving yet";
      return `${stage.stage}: ${count(tasks)} tasks for ${count(stage.items)} ${unit}, ${count(stage.running)} running, ${pace}${when(stage, true)}.`;
    })
    .join("\n");
  return { text, title };
}
